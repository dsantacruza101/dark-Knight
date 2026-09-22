# Signal — Skills

## Chequeos por ciclo (cada hora, 6 AM - 10 PM CST)
- Servicios systemd/Docker del catalogo compartido con Lucius Fox.
- Endpoints HTTP (`dsantacruz.dev`, `api.dsantacruz.com`, `webhook.dsantacruz.com`): status code y tiempo de respuesta.
- Puerto SSH 2222 (`ssh.dsantacruz.com`, chequeado localmente via 127.0.0.1).
- CPU/RAM/disco con `psutil` contra los umbrales de `config.yaml`.
- Ultimas 100 lineas de `access.log` de Nginx en busca de 5xx.
- Pipelines de GitHub Actions fallidos en los repos de `config.yaml` (via PyGithub).

## Decisiones
- Cada anomalia se evalua con TypeSafe (Jev): `severity` (Score), `needs_batman_attention` (Noul), `action` (Choice: monitor/alert_alfred/write_batman/escalate).
- Sin `TYPESAFE_API_KEY`: fallback deterministico segun el tipo de anomalia.
- Resumen ejecutivo del dia redactado con Ollama (`llama3.2`), no con Claude — ese presupuesto se reserva para Batman.

## Comunicacion
- Deja `daily_brief` para Batman en `batcave/comms.json` al terminar el turno.
- Pide auto-reparacion a Lucius (`self_repair_needed`) ante una excepcion no manejada.
- Reporte diario en `reports/signal-YYYY-MM-DD.md`.
