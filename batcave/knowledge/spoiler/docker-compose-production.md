keywords: docker, compose, dockerfile, healthcheck, restart, container, contenedor, image, imagen, root, alpine, hadolint, nats
> 🌱 Nota semilla (2026-10-17, escrita a mano). Checklist; refrescar si cambia la version de Compose.

# Docker Compose en produccion: checklist

Recordatorio: `docker-compose*` esta en el candado duro del modo Desarrollo (se revierte).
Spoiler puede editar Dockerfiles, scripts y workflows; los cambios de compose los propone
en el issue para un humano. En produccion se usa `docker-compose.server.yml`.

## Servicios
- [ ] `healthcheck` que pruebe el servicio de verdad: `node --version` siempre pasa.
  HTTP: `["CMD", "wget", "-qO-", "http://localhost:3000/health"]` (alpine trae wget, no curl).
  NATS: confirmar que el comando del healthcheck existe en la imagen (`docker compose exec
  nats-server <cmd>`); alternativa: el monitor `http://localhost:8222/healthz`.
- [ ] `restart: unless-stopped` (o `always`) en todo servicio de larga duracion.
- [ ] `depends_on: {x: {condition: service_healthy}}` solo hacia servicios con healthcheck.
- [ ] Sin `version:` en el archivo (obsoleto, Compose v2 avisa).
- [ ] Puertos: publicar solo lo que Nginx necesita y en loopback: `"127.0.0.1:3000:3000"`.
  NATS (4222/8222) no se publica: lo usan los containers por la red interna.
- [ ] Secretos por `environment: - X=${X}` desde el entorno del host; nunca valores en el YAML.
- [ ] `logging: {driver: json-file, options: {max-size: "10m", max-file: "3"}}`: el SSD es de 512GB.
- [ ] Limites (`mem_limit`/`deploy.resources.limits`): el N95 tiene 16GB compartidos con Ollama.
- [ ] Volumenes de config `:ro`; nunca montar `/var/run/docker.sock`.

## Dockerfile
- [ ] Tag fijo y pequeno (`node:22-alpine`, `python:3.12-slim`), nunca `latest`.
  El radar de Spoiler avisa de tags nuevos de imagenes oficiales (solo reporte).
- [ ] Multi-stage: deps -> build -> runtime solo con lo necesario (`npm ci --omit=dev`).
- [ ] `USER node` (o `RUN adduser -D app` + `USER app`) antes del `CMD`.
- [ ] `.dockerignore` con `node_modules`, `.git`, `.env*`, `coverage`.
- [ ] `CMD ["node", "dist/main.js"]` en forma exec (senales -> apagado limpio).
- [ ] `HEALTHCHECK` en el Dockerfile solo si compose no lo define.

## Verificar
- `docker compose --env-file .env.template -f docker-compose.server.yml config -q`
  (la verificacion de Spoiler usa la plantilla versionada, nunca el `.env` real).
- `hadolint Dockerfile` (si esta instalado).
- Deploy real: SIEMPRE via webhook, nunca `docker compose up` a mano en produccion.
