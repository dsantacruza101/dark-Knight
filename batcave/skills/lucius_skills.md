# Lucius Fox — Skills

## Catalogo de reparacion (`ServiceCheck`)
- **systemd**: `telegram-bot`, `cloudflared`, `night-agent.timer`, `webhook-portfolio`, `signal.service`, `red-hood.timer`, `ollama` → `systemctl is-active` / `systemctl restart`.
- **docker**: `daniel-portfolio-app`, `portfolioservicelauncher-client-gateway-1`, `portfolioservicelauncher-nodemailer-micro-service-1`, `portfolioservicelauncher-nats-server-1`, `sonarqube` → `docker inspect` / `docker start`.
- **binary**: `claude` (`claude --version`) → reinstala con `node .../@anthropic-ai/claude-code/install.cjs`.

## Decisiones
- Por cada servicio caido, pregunta a TypeSafe (Jev): `can_self_repair` (Noul), `priority` (Choice: critical/high/medium/low), `who_to_notify` (Choice: alfred/batman/both/none).
- Hasta 3 intentos de reparacion por servicio.
- Sin `TYPESAFE_API_KEY`: fallback deterministico basado en la prioridad base del catalogo.

## Escalado
- **critical** → avisa a Alfred de inmediato.
- **high** → avisa a Alfred y deja mensaje para Batman en `batcave/comms.json`.
- **medium** → solo se registra en el log.

## Otros
- Atiende pedidos de auto-reparacion de Signal (`self_repair_needed` en `batcave/comms.json`), evaluando con TypeSafe si `can_repair > 0.7` antes de reiniciar `signal.service`.
- Lee mensajes de Batman en `batcave/comms.json` al iniciar cada ronda.
