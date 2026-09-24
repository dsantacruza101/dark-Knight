# Night Agent — Contexto de la Batifamilia

Este proyecto contiene a los agentes autonomos que cuidan la infraestructura
de Daniel Santacruz (ver `/home/dsantacruz/CLAUDE.md` para el contexto global
del servidor: servicios, dominios, repos, reglas de despliegue).

## La Batifamilia

| Rol | Archivo / servicio | Que hace |
|---|---|---|
| **Batman** | `night_agent.py` (`night-agent.timer`, corre 22:00 diario) | Orquestador nocturno. Decide modo (Desarrollo / Post-Reset / Monitoreo) segun `config.yaml` e issues abiertos, y lo ejecuta. |
| **Alfred** | Bot de Telegram (`~/projects/telegram-bot`, `telegram-bot.service`) | Canal de comunicacion: recibe las notificaciones de Batman y Lucius, y responde preguntas usando `batcave/comms.json` como contexto. |
| **Lucius Fox** | `lucius_fox.py` (`lucius-fox.service` + `lucius-fox.timer`, cada 30 min) | Guardian de infraestructura. Vigila servicios/containers/binarios criticos, usa TypeSafe (Jev) para decidir si auto-repararlos, y escala lo que no puede resolver. |
| **Red Hood** | `red_hood.py` (`red-hood.service` + `red-hood.timer`, diario a las 02:00 America/El_Salvador) | QA brutal, sin piedad. Audita tests, cobertura, lint, vulnerabilidades y secretos hardcodeados en los repos locales, genera tests faltantes con Aider y reporta todo sin filtro. |

## Modos de Batman (`modes/`)

- `modes/development.py` — modo Desarrollo: resuelve issues etiquetados `night-agent` en los repos de `config.yaml` usando `claude -p`, respetando rutas prohibidas (nunca `.env`, `docker-compose`, `nginx`, `fail2ban`, `cloudflared`, `ssh`, ni la rama `main`/`master`).
- `modes/post_reset.py` — modo Post-Reset: corre el lunes tras el reset semanal de tokens; usa Claude (fresco) para auditar los commits de la semana hechos con Ollama, y genera el reporte semanal.
- `modes/monitoring.py` — modo Monitoreo: ciclo horario que analiza `access.log` de Nginx con TypeSafe (Jev: `is_attack`, `attack_type`, `severity`, `should_block`), revisa containers/RAM/disco/fail2ban, y cada 6 ciclos corre `clamscan`.

## Comunicacion: `batcave/comms.json`

Bandeja compartida entre Batman y Lucius (Alfred la lee como contexto, no
escribe en ella). Es una lista de mensajes:

```json
[
  {
    "timestamp": "2026-09-22T03:00:00",
    "from": "lucius",
    "to": "batman",
    "service": "webhook-portfolio",
    "priority": "high",
    "message": "webhook-portfolio caido, Lucius no pudo repararlo (prioridad high).",
    "read": false
  }
]
```

- Lucius escribe aqui cuando un servicio de prioridad **alta** queda caido
  sin poder repararse (ver escalado abajo).
- Batman puede escribir mensajes con `"to": "lucius"` cuando detecta una
  amenaza de seguridad durante el modo Monitoreo que Lucius deberia saber.
- Cada agente marca `"read": true` los mensajes dirigidos a el una vez los
  procesa; el archivo nunca se vacia, solo crece (es historial + inbox).
- Arranca vacio (`[]`).

## Lucius Fox: catalogo y flujo (`lucius_fox.py`)

Catalogo de `ServiceCheck` (`kind` → chequeo/reparacion):

- **systemd**: `telegram-bot`, `cloudflared`, `night-agent.timer`,
  `webhook-portfolio`, `ollama` → `systemctl is-active` / `systemctl restart`.
- **docker**: `daniel-portfolio-app`, `portfolioservicelauncher-client-gateway-1`,
  `portfolioservicelauncher-nodemailer-micro-service-1`,
  `portfolioservicelauncher-nats-server-1`, `sonarqube` → `docker inspect` /
  `docker start`.
- **binary**: `claude` (`claude --version`) → reinstala con
  `node .../@anthropic-ai/claude-code/install.cjs`.

Por cada servicio caido, Lucius le pregunta a TypeSafe (Jev) `can_self_repair`
(Noul), `priority` (Choice: critical/high/medium/low) y `who_to_notify`
(Choice: alfred/batman/both/none). Reparacion: hasta 3 intentos. Si repara,
avisa a Alfred (`🦊 Lucius reparó: {servicio}`). Si no:

- **critical** (`claude`, `cloudflared`, `telegram-bot`) → avisa a Alfred de inmediato.
- **high** (containers, `webhook-portfolio`) → avisa a Alfred **y** deja mensaje para Batman en `batcave/comms.json`.
- **medium** (`ollama`, `sonarqube`) → solo se registra en el log.

`who_to_notify` puede ampliar este escalado (p. ej. avisar a Batman aunque
la prioridad sea media). Si `TYPESAFE_API_KEY` no esta definida, Lucius no
puede consultar a Jev: usa un fallback determinista basado en la prioridad
base del catalogo y avisa a Alfred del problema.

## Red Hood: catalogo y flujo (`red_hood.py`)

Audita los repos de `config.yaml` (`red_hood.local_repos`): `my_Portfolio_React_v2`,
`portfolioServiceLauncher/client-gateway`, `portfolioServiceLauncher/nodeMailer-ms`,
`telegram-bot` y el propio `night-agent`. Detecta el stack por archivos
(`package.json` -> Node, `requirements.txt`/`*.py` -> Python).

Por repo: `git fetch` + se posiciona en `dev` si existe (local u `origin/dev`);
si no existe, audita `main` en modo solo-lectura (nunca comitea ahi). Compara
el HEAD contra el ultimo hash auditado en
`batcave/memory/red_hood_state.json`; sin commits nuevos, se omite.

Corre tests con cobertura (`npm test -- --passWithNoTests --coverage` /
`pytest` si existe `tests/`, timeout 10 min), lint, auditoria de dependencias
(`npm audit` / `pip-audit`), `py_compile` en Python, y un escaneo de secretos
hardcodeados (regex: `sk-ant-`, `ghp_`, `apikey_`, `bot<digitos>:AA`,
passwords) que dispara alerta critica inmediata (los secretos nunca se
muestran completos en logs ni Telegram, solo los primeros 6 caracteres + `***`).

Cada hallazgo se evalua con TypeSafe (Jev): `severity` (Score), `category`
(Choice: failing_test/missing_tests/vulnerability/hardcoded_secret/
lint_error/low_coverage), `should_generate_tests` (Noul) y `who_to_notify`
(Choice: alfred/nightwing/batman/none). Sin `TYPESAFE_API_KEY`, cae a un
fallback deterministico (igual que Lucius Fox y Signal).

Si `should_generate_tests > 0.7` y la cobertura esta bajo `red_hood.coverage_threshold`,
genera tests para hasta `red_hood.max_tests_per_repo` archivos con Aider
(backend Ollama). Solo se tocan archivos de test (`*.spec.ts`, `*.test.ts`,
`test_*.py`, validado con `modes.development.is_forbidden_path`); si Aider
toca algo mas o el test generado falla, se descarta con `git checkout` y se
registra en memoria. Si pasa, se comitea (y pushea) a `dev`.

Reporta hallazgos high/critical y tests fallidos como issues en GitHub
(labels `red-hood` + `bug`, buscando duplicados antes; `night-agent` si
Nightwing podria resolverlo), deja mensajes `qa_finding` en
`batcave/comms.json` para Batman/Nightwing/Lucius (secretos siempre avisan
a Lucius tambien), y notifica a Alfred por Telegram. Reporte diario en
`reports/red-hood-YYYY-MM-DD.md`. Con `--dry-run` audita igual pero no
genera tests, no crea issues, no comitea/pushea y no envia Telegram.

## Variables de entorno (`/etc/night-agent.env`)

Nunca se edita este archivo desde el codigo (regla global: nunca tocar
`.env`). Variables esperadas:

- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` — notificaciones via Alfred.
- `GITHUB_TOKEN` — issues de `night-agent` en los repos de `config.yaml`.
- `TYPESAFE_API_KEY` — decisiones tipadas con TypeSafe/Jev (usado por
  `modes/monitoring.py` y `lucius_fox.py`).

## Deploy de los servicios systemd

Los `.service`/`.timer` viven en este repo pero se instalan a mano en
`/etc/systemd/system/` (no se hace automaticamente desde el codigo):

```bash
sudo cp night-agent.service night-agent.timer lucius-fox.service lucius-fox.timer red-hood.service red-hood.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now night-agent.timer lucius-fox.timer red-hood.timer
```

## Convenciones

- Python async (`asyncio`), docstrings y logs en espanol.
- `config.yaml` para umbrales/horarios/repos; nada de eso hardcodeado en el codigo.
- Un fallo de TypeSafe (o de Telegram) nunca debe tumbar el ciclo completo: se
  loguea y se sigue (ver `except Exception` en `modes/monitoring.py` y
  `lucius_fox.py`).
- `knowledge/` y `reports/` son generados, no se versionan (`.gitignore`).
