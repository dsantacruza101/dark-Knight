# Batman — Skills

## Modos
- **Desarrollo** (`modes/development.py`): resuelve issues etiquetados `night-agent` en los repos de `config.yaml` usando `claude -p --permission-mode acceptEdits`, respetando rutas prohibidas (`.env`, `docker-compose`, `nginx`, `fail2ban`, `cloudflared`, `ssh`) y la rama `dev`.
- **Post-Reset** (`modes/post_reset.py`): corre el lunes tras el reset semanal; usa Claude fresco para auditar los commits de la semana hechos con Ollama (persona "Commissioner Gordon"), calcula el Score de Nightwing (calidad Ollama vs Claude) y genera el reporte semanal.
- **Monitoreo** (`modes/monitoring.py`): ciclo horario nocturno que analiza `access.log` de Nginx con TypeSafe (Jev), revisa containers/RAM/disco/fail2ban, y cada 6 ciclos corre `clamscan`.

## Herramientas
- GitHub API (PyGithub) para leer issues abiertos y decidir el modo del turno.
- `claude -p` como motor de resolucion de issues y auditoria de calidad.
- Telegram (`TelegramNotifier`) para reportar inicio/fin de turno a Alfred.
- `batcave/comms.json` como bandeja compartida con Lucius y Signal.

## Limites
- Nunca toca `main`/`master`, solo la rama `dev`.
- Nunca edita `.env`, `docker-compose`, `nginx`, `fail2ban`, `cloudflared` ni `ssh`.
- Un fallo de Telegram o de un modo no debe tumbar el proceso sin notificar antes de salir.
