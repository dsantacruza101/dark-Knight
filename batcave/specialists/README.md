# Especialistas del modo Desarrollo

Cada miembro del equipo de desarrollo de Batman (Nightwing, Batgirl, Red Robin,
Robin, Spoiler...) es un perfil YAML en este directorio. `modes/development.py`
carga todos los `*.yaml` de aqui (`specialists.dir` en `config.yaml`) y enruta
cada issue con label `night-agent` al especialista correspondiente. Agregar un
miembro nuevo = agregar un archivo YAML; no hay que tocar codigo.

## Enrutamiento

1. Se toman los labels del issue (ademas de `night-agent`).
2. Si alguno coincide con el `label` de un perfil, el issue va a ese
   especialista. Si coinciden varios, gana el primero por orden alfabetico de
   archivo y se loguea un aviso.
3. Si ninguno coincide (issue solo con `night-agent`), va al perfil con
   `default: true`, o al de `specialists.default` en `config.yaml`
   (Nightwing). Esto mantiene compatibles los issues viejos.

`repos` es informativo para el enrutamiento: si el issue cae en un repo que
no esta en la lista del especialista elegido, solo se loguea (el label manda).

Equipo actual:

| Perfil | Label | Repos (rama de trabajo) | `test_runner` |
|---|---|---|---|
| `nightwing.yaml` (default) | `backend` | dsantacruz-client-gateway, NodeMailer_MicroService, telegram-bot, dark-Knight (`dev`) | `auto` |
| `batgirl.yaml` | `frontend` | my_Portfolio_React_v2 (`development`) | `vitest` |

## Esquema

| Campo | Tipo | Obligatorio | Uso |
|---|---|---|---|
| `name` | str | si | Identificador (`nightwing`). Debe coincidir con el nombre del archivo. Se usa como scope del commit (`feat(<name>): ...`), en `batcave/memory/<name>.md` y en `batcave/knowledge/<name>/`. |
| `display_name` | str | no | Nombre para Telegram/GitHub (default: `name` capitalizado). |
| `emoji` | str | no | Prefijo de mensajes (default `🦇`). |
| `role` | str | si | Rol corto (`backend`, `frontend`, `qa`...). Va en el preambulo de Aider/Claude. |
| `label` | str | si | Label de GitHub que enruta issues a este especialista. |
| `default` | bool | no | `true` en UN solo perfil: recibe los issues sin label de especialidad. |
| `languages`, `frameworks`, `ui`, `testing`, `orms`, `messaging` | list[str] | no | Stack. Va al preambulo (en ese orden). |
| `repos` | list[str \| dict] | no | Repos de GitHub (sin owner) de los que es dueño. Cada entrada es un nombre o `{name, work_branch}` (ver abajo). |
| `test_runner` | str | no | `auto` (default), `vitest`, `jest` o `pytest`: runner para correr cada archivo de test permitido (ver abajo). |
| `conventions` | list[str] | no | Reglas que se le recuerdan a Aider/Claude en el preambulo. |
| `verification` | dict | si | Comandos de verificacion por stack (ver abajo). |

El preambulo (rol + stack + convenciones) se recorta a 25 lineas.

### `repos` y `work_branch` (rama de trabajo por repo)

```yaml
repos:
  - dsantacruz-client-gateway          # usa github.work_branch (dev)
  - name: my_Portfolio_React_v2
    work_branch: development           # su rama de integracion no es dev
```

La rama es del repo, no del especialista: `resolve_work_branch` busca la
entrada del repo en el perfil que trabaja el issue y luego en todos los
demas; si ninguna fija `work_branch`, usa `github.work_branch` (`dev`). La
usan el modo Desarrollo (workspace, verificacion contra `origin/<rama>`,
commit/push, parches del radar), el modo Post-Reset, el radar (sincroniza
`origin/<rama>` y la menciona en los issues) y Red Hood (ubica el repo de
produccion via `repo_paths`). `main`/`master` como `work_branch` invalida
el perfil (se omite con error en el log). Una entrada dict sin `name`
tambien.

### `test_runner` (runner por archivo de test)

Cada archivo de test permitido (`*.spec.ts(x)`, `*.test.ts(x)`, `.js(x)`,
`test_*.py`) se corre aparte apuntando solo a ese archivo, y debe ejecutar
> 0 tests y 0 fallidos:

- `vitest` -> `CI=true npx vitest run <archivo> --reporter=json --outputFile=<tmp>`
  (el JSON es compatible con el de jest; el posicional de vitest es un
  filtro por substring, por eso se filtra el reporte por la ruta exacta).
- `jest` -> `CI=true npm test -- --runTestsByPath <archivo> --ci --json --outputFile=<tmp>`.
- `pytest` -> `python3 -m pytest <archivo> -q`; los `.py` usan pytest siempre.
- `auto` -> vitest si esta en `dependencies`/`devDependencies` de
  `package.json` o existe `vitest.config.*`; si no, jest. `pytest` en el
  perfil se comporta como `auto` para tests JS/TS.

Red Hood usa el mismo runner (`run_test_file`) para validar los tests que
genera.

### `verification`

```yaml
verification:
  <stack>:                     # nombre libre: node, python, spring, ...
    detect: [package.json]     # el stack aplica si existe alguno de estos archivos en la raiz del repo
    steps:
      - cmd: npm run build     # se parsea con shlex; prefijos VAR=valor se pasan como entorno
        when: script:build     # opcional, ver condiciones
        timeout: 600           # opcional, segundos (default 600)
        counts_tests: node     # opcional: node|pytest, confirma que los tests permitidos se detectaron
                               # (node entiende el resumen de jest `Tests: N passed` y el de vitest `Tests  N passed (N)`)
      - builtin: npm_ci_if_needed
```

Condiciones `when`:

- `script:<nombre>` — `package.json` tiene ese script.
- `exists:<ruta>` — la ruta existe en el repo (relativa a la raiz).
- (sin `when`) — siempre.

Builtins:

- `npm_ci_if_needed` — `npm ci` solo si falta `node_modules` o cambio
  `package-lock.json` (hash guardado en `node_modules`). Su fallo no corta la
  verificacion (el build fallara de todas formas).
- `py_compile_changed` — `py_compile` sobre los `.py` modificados/creados.

Un paso con codigo de salida != 0 marca la verificacion como fallida; su
salida completa va al log del intento y sus ultimas lineas se usan como
retroalimentacion para el siguiente intento (Aider) o la escalacion (Claude).

Independiente del perfil, siempre se aplica la validacion por archivo
permitido (existe, no vacio, `.py` con `def`/`class`, `.service`/`.timer`
validos, tests JS/TS con `describe(`/`it(`/`test(`) y el runner por archivo
de cada test permitido (`test_runner`, ver arriba).

## Conocimiento del especialista: `batcave/knowledge/<name>/`

Notas Markdown cortas (convenciones, gotchas de un framework). Por issue se
eligen hasta 3 notas relevantes, con un maximo de 60 lineas en total:

- Palabras clave de una nota: las partes de su nombre de archivo
  (`nestjs-dtos.md` -> `nestjs`, `dtos`) mas una primera linea opcional
  `keywords: nestjs, class-validator, dto`.
- Una nota es relevante si alguna palabra clave (>= 3 letras) aparece en el
  titulo o cuerpo del issue; se ordenan por cantidad de coincidencias.
- `CLAUDE.md` nunca se incluye (Aider agrega al chat cualquier archivo que
  se mencione, y el CLAUDE.md menciona muchos).

Notas semilla: un especialista nuevo puede arrancar con notas escritas a
mano marcadas `🌱 Nota semilla` (p. ej. `batcave/knowledge/batgirl/`:
React 19 + Vitest, Tailwind v4, Angular + signals + PrimeNG). El radar
semanal agrega notas `<paquete>-<version>.md` cuando hay majors pendientes.

Nota: el patron `knowledge/` de `.gitignore` tambien ignora este directorio
(para versionar las notas semilla hay que usar `git add -f`).

## Memoria

Cada issue procesado se registra en `batcave/memory/<name>.md` (se crea con
la plantilla estandar si no existe) con quien lo resolvio: `aider-qwen`,
`claude-escalation` o `none`.

## Agregar un especialista (Red Robin, Robin, Spoiler...)

Usar `batgirl.yaml` (frontend, rama por repo, `test_runner: vitest`) o
`nightwing.yaml` (backend, varios stacks) como plantilla:

1. Copiar el YAML a `batcave/specialists/<name>.yaml` y ajustar `name`,
   `label`, stack, `repos` (con `work_branch` si el repo no integra en
   `dev`), `test_runner` y `verification`.
2. Crear el label (`<label>`) en los repos de GitHub correspondientes.
3. Opcional: notas semilla en `batcave/knowledge/<name>/` (max 60 lineas,
   primera linea `keywords:`).
4. Radar: copiar `radar-batgirl*.service/.timer` cambiando `--specialist`
   y la hora (escalonada: Nightwing 07:00 / lunes 01:00, Batgirl 07:10 /
   lunes 01:20), y agregar ambos timers al `CATALOG` de `lucius_fox.py`
   (prioridad medium).
5. Verificar: `python3 night_agent.py --dry-run` (lista especialistas, repos
   con su rama y `test_runner`) y
   `python3 -m batcave.radar --specialist <name> --dry-run`.
