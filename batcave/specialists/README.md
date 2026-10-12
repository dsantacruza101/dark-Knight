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

`repos` es informativo: si el issue cae en un repo que no esta en la lista
del especialista elegido, solo se loguea (el label manda).

## Esquema

| Campo | Tipo | Obligatorio | Uso |
|---|---|---|---|
| `name` | str | si | Identificador (`nightwing`). Debe coincidir con el nombre del archivo. Se usa como scope del commit (`feat(<name>): ...`), en `batcave/memory/<name>.md` y en `batcave/knowledge/<name>/`. |
| `display_name` | str | no | Nombre para Telegram/GitHub (default: `name` capitalizado). |
| `emoji` | str | no | Prefijo de mensajes (default `🦇`). |
| `role` | str | si | Rol corto (`backend`, `frontend`, `qa`...). Va en el preambulo de Aider/Claude. |
| `label` | str | si | Label de GitHub que enruta issues a este especialista. |
| `default` | bool | no | `true` en UN solo perfil: recibe los issues sin label de especialidad. |
| `languages`, `frameworks`, `orms`, `messaging` | list[str] | no | Stack. Va al preambulo y sirve como palabra clave para elegir notas de conocimiento. |
| `repos` | list[str] | no | Repos de GitHub (sin owner) de los que es dueño. |
| `conventions` | list[str] | no | Reglas que se le recuerdan a Aider/Claude en el preambulo. |
| `verification` | dict | si | Comandos de verificacion por stack (ver abajo). |

El preambulo (rol + stack + convenciones) se recorta a 25 lineas.

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
validos, tests con `describe(`/`it(`) y, para tests Python permitidos,
`pytest --collect-only`.

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

Nota: el patron `knowledge/` de `.gitignore` tambien ignora este directorio.

## Memoria

Cada issue procesado se registra en `batcave/memory/<name>.md` (se crea con
la plantilla estandar si no existe) con quien lo resolvio: `aider-qwen`,
`claude-escalation` o `none`.

## Agregar un especialista (ejemplo: Batgirl, frontend)

```yaml
name: batgirl
display_name: Batgirl
emoji: "🦇"
role: frontend
label: frontend
languages: [TypeScript, JavaScript]
frameworks: [React, Vite, Tailwind]
repos: [my_Portfolio_React_v2]
conventions:
  - "Componentes funcionales con hooks; nada de `any`."
verification:
  node:
    detect: [package.json]
    steps:
      - builtin: npm_ci_if_needed
      - cmd: npm run build
        when: script:build
      - cmd: npm run lint --if-present
      - cmd: CI=true npm test -- --passWithNoTests --watchAll=false
        when: script:test
        counts_tests: node
```

Luego crear el label `frontend` en los repos de GitHub correspondientes.
