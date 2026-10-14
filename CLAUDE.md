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
| **Batwoman** (Kate Kane) | `batwoman.py` (`batwoman.service`, `Type=simple`, poll cada `batwoman.poll_interval` s) | Analista de seguridad. Lee las alertas de Wazuh, hace triage (allowlist, rutas criticas, nivel), las evalua con TypeSafe (Jev) y avisa a Alfred/Batman de las amenazas reales; digest diario a las 08:00. |
| **Radar** | `batcave/radar.py` (`radar-nightwing.timer` diario 07:00, `radar-nightwing-weekly.timer` lunes 01:00) | Radar tecnologico por especialista: inventario de dependencias, vulnerabilidades (OSV) y ultimas versiones; abre issues de parches para que el especialista los aplique de noche. |

## Modos de Batman (`modes/`)

- `modes/development.py` — modo Desarrollo: resuelve issues etiquetados `night-agent` en los repos de `config.yaml` usando Aider (`models.aider`, `ollama/qwen2.5-coder:3b`) sobre un workspace aislado, respetando rutas prohibidas (nunca `.env`, `docker-compose`, `nginx`, `fail2ban`, `cloudflared`, `ssh`, ni la rama `main`/`master`) y una lista de archivos permitidos (seccion `Archivos:` del issue, o extraccion por mencion excluyendo lineas de verificacion de servicios). Cada issue lo trabaja un especialista (ver "Equipo de desarrollo" abajo), cuyo perfil define la verificacion antes de comitear; si Aider falla, escala una vez a Claude.
- `modes/post_reset.py` — modo Post-Reset: corre el lunes tras el reset semanal de tokens; usa Claude (fresco) para auditar los commits de la semana hechos con Ollama, y genera el reporte semanal.
- `modes/monitoring.py` — modo Monitoreo: ciclo horario que analiza `access.log` de Nginx con TypeSafe (Jev: `is_attack`, `attack_type`, `severity`, `should_block`), revisa containers/RAM/disco/fail2ban, y cada 6 ciclos lee el resumen del log diario de ClamAV (`/var/log/clamav-daily.log`, generado por el cron que corre como root) en vez de correr `clamscan` directamente.

## Equipo de desarrollo: especialistas (`batcave/specialists/`)

Cada especialista del modo Desarrollo es un perfil YAML en
`batcave/specialists/` (esquema en `batcave/specialists/README.md`; agregar a
Batgirl, Red Robin, Robin o Spoiler = agregar su YAML, sin tocar codigo).
Hoy existe **Nightwing** (`nightwing.yaml`): backend (`label: backend`),
TypeScript/JavaScript/Python/Java, NestJS, Express, FastAPI, Spring Boot,
TypeORM/Prisma, NATS; dueño de `dsantacruz-client-gateway`,
`NodeMailer_MicroService`, `telegram-bot` y `dark-Knight`.

Flujo por issue en `modes/development.py`:

1. **Enrutamiento**: el issue va al especialista cuyo `label` de perfil este
   en el issue (ademas de `night-agent`). Sin label de especialidad va a
   `specialists.default` (Nightwing), compatible con los issues viejos. Si
   el repo no esta en `repos` del especialista solo se loguea.
2. **Contexto**: al mensaje de Aider se le antepone un preambulo del perfil
   (rol, stack, convenciones; max 25 lineas) y hasta 3 notas de
   `batcave/knowledge/<especialista>/*.md` cuyas palabras clave (nombre de
   archivo + linea `keywords:` opcional) aparecen en el issue (max 60 lineas
   en total). El CLAUDE.md nunca se incluye (Aider agrega al chat todo
   archivo mencionado).
3. **Verificacion**: los pasos de `verification` del perfil para cada stack
   detectado (Nightwing: Node -> `npm ci` si hace falta, `npm run build`,
   `npm run lint --if-present`, `CI=true npm test -- --passWithNoTests
   --watchAll=false`; Python -> `py_compile` de los cambiados + `pytest -q`
   si hay `tests/`; Spring Boot -> `./mvnw -q test` si hay `mvnw`), mas la
   validacion por archivo permitido de siempre.
4. **Escalacion qwen -> Claude**: si los `development.aider_attempts`
   intentos fallan, se llama UNA vez a `claude -p --permission-mode
   acceptEdits` (prompt por stdin) en el mismo workspace con el issue, el
   preambulo, los archivos permitidos, su contenido actual y las ultimas 80
   lineas del ultimo error de verificacion. Despues se aplica el mismo
   candado (revertir prohibidos y no listados) y la misma verificacion. Max
   `development.max_claude_escalations` por noche (2), timeout
   `development.claude_timeout` (acotado por el limite de 3h por issue); log
   en `reports/claude-<repo>-<issue>.log`. Si Claude no esta, no hay
   archivos permitidos, se agoto el cupo o tambien falla -> `needs-review`.
5. **Registro**: quien resolvio (`aider-qwen`, `claude-escalation`, `none`)
   queda en `reports/development-YYYY-MM-DD.md` (resumen + tabla), en el
   progreso de Telegram, en el commit (`feat(<especialista>): ...` +
   `Resuelto por: ...`) y en `batcave/memory/<especialista>.md` (se crea con
   la plantilla si no existe).

**Parches de dependencias del radar** (label `dependencies` + marcador
`<!-- radar:`): NUNCA pasan por Aider ni Claude y no consumen escalaciones.
`process_dependency_patch` lee el bloque ```` ```radar-patch ```` (JSON:
`package`, `from`, `to`, `ecosystem`, `direct`, `dev`, `manifest`,
`advisories`; en issues viejos sin bloque, las lineas Paquete/Version del
cuerpo) y aplica: npm directa -> `npm install [-D] <pkg>@<to>` (`-D` segun
package.json); npm transitiva -> `npm update <pkg>` y, si OSV aun ve una
version vulnerable en el lock, `"overrides": {<pkg>: <to>}` + `npm install`;
PyPI -> fija `<pkg>==<to>` en requirements.txt. Despues: verificacion del
perfil, re-chequeo OSV (ninguna version del lock afectada por los advisories
del issue), stage solo de package.json/package-lock.json (o
requirements.txt), commit `fix(deps): <pkg> <from> -> <to> (radar)` +
`Resuelto por: deterministic-patch`, push a `dev`. Si falla -> `needs-review`.
Log en `reports/deps-<repo>-<issue>.log`.

`python3 night_agent.py --dry-run` muestra a que especialista iria cada
issue abierto y que notas de conocimiento se usarian (o, para parches de
dependencias, el plan determinista).

## Radar tecnologico (`batcave/radar.py`)

`python3 -m batcave.radar --specialist <nombre> [--dry-run] [--weekly]`
(con el venv). Sirve para cualquier perfil de `batcave/specialists/`; hoy
solo Nightwing tiene timers. Config en `config.yaml` (`radar:`).

- **Inventario**: por cada repo de `repos` del perfil, copia de SOLO
  LECTURA en `radar.workspace_dir/<repo>` (`~/projects/.radar-workspace`):
  `clone` o `fetch` + `reset --hard origin/dev` (si no hay `dev`,
  `main`/`master`). Se niega a correr si el workspace se cruza con un
  directorio de produccion (`repo_paths`, `red_hood.local_repos`). Auth de
  git con `github_auth_env()` (header por entorno). Nunca comitea ni pushea.
  Directas: `package.json` (version instalada de `package-lock.json`; si no
  hay lock, la base del rango), `requirements.in` si existe (paquetes
  realmente importados; la version sale del pin de `requirements.txt` /
  `requirements*.lock`) o si no `requirements.txt`, `requirements-dev.txt`
  (dev), `pyproject.toml` (PEP 621 + Poetry) y `pom.xml` (con
  `${propiedades}`). Transitivas: el resto de `package-lock.json` (anidadas
  incluidas), de `requirements.txt` (pip freeze) y de `requirements*.lock`.
  `devDependencies`, extras opcionales y scope `test` cuentan como dev. Sin
  version fija no se consulta. Vulnerabilidades se revisan en TODAS;
  `outdated_*` solo en directas. Cada hallazgo indica `directa`/`transitiva`.
- **Chequeo diario (sin tokens de Claude)**: OSV `querybatch` + `/vulns/{id}`
  (npm, PyPI, Maven; los alias PYSEC/GHSA se deduplican; severidad de
  `database_specific.severity` o calculada del vector CVSS v3), y ultima
  version en npm registry / PyPI JSON / Maven Central. Clasificacion:
  `security_patch` (todos los fixes en el mismo major; en 0.x el minor
  cuenta como major), `security_watch` (ningun advisory tiene fix y la
  dependencia es solo de desarrollo, directa o transitiva),
  `security_major` (algun fix exige otro major, o no hay fix en una
  dependencia de produccion), `outdated_major`, `outdated_minor`.
- **Issues** (dedup por marcador oculto `<!-- radar:<repo>:<paquete>:<advisories> -->`
  en issues abiertos; mismo repo+paquete = se actualiza titulo/cuerpo/labels):
  - `security_patch` → labels `night-agent` + label del especialista +
    `dependencies`, con advisories, version actual/objetivo, comando exacto
    (`npm install pkg@x.y.z`, `--save-dev` si es dev) y seccion `Archivos:`
    (`package.json`, `package-lock.json` / manifiesto Python / `pom.xml`)
    y bloque ```` ```radar-patch ```` (JSON). Batman lo aplica esa noche de
    forma determinista (modo Desarrollo, rama `dev`, sin Aider ni Claude).
  - `security_watch` → sin issue; si habia uno abierto se cierra
    (`not_planned`) con un comentario explicando la politica. Se lista en
    el resumen semanal bajo "Sin parche disponible (vigilando)".
  - `security_major` → `needs-human` + label del especialista +
    `dependencies` (nunca `night-agent`; si un issue cambia de categoria se
    quita el label opuesto). Si existe la nota semanal del paquete, se
    incluye en el cuerpo.
  - Los labels faltantes se crean en el repo.
- **Telegram** ("📡 Radar de <Especialista>", HTML escapado): inmediato solo
  para advisories `radar.alert_severities` (CRITICAL/HIGH) en dependencias
  de produccion, una vez por advisory. Todo lo demas va al resumen semanal.
- **Modo semanal** (`--weekly`, lunes 01:00 tras el reset del domingo
  23:00): repite el chequeo y, para hasta `radar.max_weekly_summaries` (5)
  paquetes `security_major`/`outdated_major` sin nota previa, baja las
  release notes de GitHub (repo resuelto desde la metadata de npm/PyPI;
  fallback `CHANGELOG.md`) y llama `claude -p` (solo `Read,Grep,Glob`;
  `Bash/Edit/Write` prohibidos; timeout `radar.claude_timeout`) con los
  usos del paquete en nuestro codigo. Guarda la nota en
  `batcave/knowledge/<especialista>/<paquete>-<version>.md` (con linea
  `keywords:`, asi el modo Desarrollo la usa). Luego envia el resumen
  semanal: vulnerabilidades nuevas/corregidas (7 dias), majors pendientes,
  notas nuevas.
- **Estado**: `batcave/memory/radar_<especialista>_state.json` (advisories
  vistos, alertas enviadas, eventos `found`/`fixed`; un advisory solo pasa a
  `fixed` si su repo se inventario bien ese dia). Reporte en
  `reports/radar-<especialista>-YYYY-MM-DD[-semanal].md`, log en `radar.log`
  (secretos enmascarados con `batcave/secrets.py`).
- `--dry-run`: sincroniza el workspace e imprime inventario por repo y
  hallazgos clasificados; sin issues, Telegram, Claude ni estado. Con
  `--weekly` muestra que notas se pedirian y el tamano de sus release notes.
- **Otro especialista**: copiar `radar-nightwing*.service/.timer` cambiando
  `--specialist`, y agregar los timers al `CATALOG` de Lucius.

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

## Modo mantenimiento: `batcave/maintenance.json`

Lista de servicios que Lucius Fox debe omitir por completo (sin chequear,
reparar ni alertar) mientras esten en mantenimiento:

```json
{
  "paused": [
    {
      "service": "red-hood.timer",
      "reason": "actualizando dependencias de Aider",
      "until": "2026-09-27T00:00:00+00:00",
      "by": "daniel"
    }
  ]
}
```

- `until` puede ser `null` (mantenimiento indefinido) o una fecha ISO 8601
  futura. Mientras este vigente, Lucius solo loguea
  `"en mantenimiento: <razon>"` y sigue con el resto del catalogo.
- Si `until` ya paso, Lucius elimina la entrada automaticamente al arrancar
  cada ronda y vuelve a vigilar ese servicio con normalidad.
- Arranca vacio (`{"paused": []}`). Se edita a mano (o via Alfred a futuro),
  no lo escribe ningun agente todavia.

## Lucius Fox: catalogo y flujo (`lucius_fox.py`)

Catalogo de `ServiceCheck` (`kind` → chequeo/reparacion):

- **systemd**: `telegram-bot`, `cloudflared`, `night-agent.timer`,
  `webhook-portfolio`, `ollama`, `radar-nightwing.timer` y
  `radar-nightwing-weekly.timer` (medium) → `systemctl is-active` / `systemctl restart`
  (para `*.timer`, `systemctl enable --now` en vez de solo `restart`, para
  que la reparacion sobreviva reinicios del servidor).
- **docker**: `daniel-portfolio-app`, `portfolioservicelauncher-client-gateway-1`,
  `portfolioservicelauncher-nodemailer-micro-service-1`,
  `portfolioservicelauncher-nats-server-1`, `sonarqube` → `docker inspect` /
  `docker start`.
- **binary**: `claude` (`claude --version`) → reinstala con
  `node .../@anthropic-ai/claude-code/install.cjs`.

Antes de chequear un servicio, Lucius revisa si esta en
`batcave/maintenance.json`: si esta en mantenimiento vigente, se omite por
completo (ver arriba).

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

## Batwoman: triage de alertas de Wazuh (`batwoman.py`)

Servicio de larga duracion (`batwoman.service`). Cada `batwoman.poll_interval`
segundos (60 por defecto) lee las alertas nuevas de
`/var/ossec/logs/alerts/alerts.json` dentro de `single-node-wazuh.manager-1`
con `docker exec` (dsantacruz esta en el grupo docker). El progreso (offset
en bytes, tamano e inode) vive en `batcave/memory/batwoman_state.json`; si el
archivo se achica o cambia de inode (rotacion diaria) se relee desde 0. En el
primer arranque empieza al final del archivo. Una alerta JSON por linea; las
malformadas se omiten.

Triage, en este orden:

1. **Ruta critica** (`batwoman.critical_paths` en `config.yaml`: sudoers.d,
   ssh, authorized_keys, los `.env` de `/etc`, cloudflared, units systemd,
   settings de Claude): toda alerta de syscheck ahi se evalua SIEMPRE, sin
   importar nivel ni allowlist, y nunca termina en `ignore`.
2. **Allowlist** (`batcave/batwoman_allowlist.yaml`, se relee en cada poll):
   `rule_id` + regex sobre `full_log`/`syscheck.path`. Hoy solo ignora la
   regla 5402 (sudo a root) con los comandos de `sudoers.d/batifamilia`. Los
   cambios FIM bajo `night-agent` (git pull) NO se ignoran a proposito.
3. **Nivel**: < 7 se ignora; 7-9 va al digest; >= 10 se evalua.

Evaluacion con TypeSafe (Jev): `is_real_threat` (Noul), `severity` (Score:
info/low/medium/high/critical), `action` (Choice: notify_now/digest/ignore),
con estado = regla, nivel, descripcion, tacticas MITRE, grupos, srcip, ruta y
evento FIM, y un extracto enmascarado de `full_log` (max 500 chars). Fallback
sin `TYPESAFE_API_KEY`: `notify_now` si nivel >= 12 o ruta critica, si no
`digest`.

`notify_now` → Telegram ("🦇 Batwoman ..."); severidad high/critical →
mensaje `type: security_alert` para Batman en `batcave/comms.json`. Ambos se
deduplican en `batcave/memory/batwoman_alert_state.json` por
`rule_id|srcip-o-ruta` (reenvio solo tras 6h). Digest diario a
`batwoman.digest_hour` (America/El_Salvador): conteo por nivel, top 5 reglas,
top 5 IPs, cambios FIM; se guarda en `reports/batwoman-YYYY-MM-DD.md`.

Junto con el digest corre `sudo -n dpkg --verify` (timeout
`batwoman.integrity_timeout`, 600s). Se ignoran conffiles (flag `c`) y todo
bajo `/etc`; cualquier otro archivo con checksum alterado (`5` en la 3a
columna) es critico: Telegram inmediato + mensaje `type: integrity_alert`
(priority critical) para Batman en `comms.json`. Excepcion: las rutas
absolutas exactas de `integrity_paths` en `batcave/batwoman_allowlist.yaml`
(hoy `ossec.conf`, `client.keys` y `localtime` de `/var/ossec/etc`) no son
hallazgo critico y se cuentan aparte como "esperados"; nunca se allowlistea
por nombre de paquete. El digest muestra los paquetes limpios
(`instalados - con archivos alterados`), los esperados, o el error si no se
pudo verificar. Requiere `dpkg --verify` en `sudoers.d/batifamilia` (NOPASSWD).
`--dry-run` lee las ultimas 200 alertas e imprime el triage (sin Telegram ni
estado); `--once` hace un solo poll (y un ciclo de egress).

### Egress: conexiones salientes (`batcave/egress.py`)

Flujos salientes TCP y UDP hacia IPs publicas (`is_global`, fuera de las
subredes de Docker y de las IPs del host; se ignora el DNS 53/853 hacia los
resolvers del sistema: `/etc/resolv.conf` + upstreams de systemd-resolved):

- **Stream** (principal, `egress.stream`): `ConntrackStream` corre
  `sudo -n conntrack -E -e NEW -o timestamp,extended` en segundo plano dentro
  de batwoman.py (backoff exponencial 1s→300s si muere). Ve conexiones
  cortas (TypeSafe, GitHub, Telegram) y QUIC de cloudflared. Origen:
  `docker:<nombre>` si el src esta en una subred de Docker (mapa via
  `docker network inspect`, cache 10 min, refresco forzado ante IP
  desconocida), si no `host:<proceso>`.
- **Atribucion de procesos del host**: cache de
  `sudo -n ss -tunpH state established` (refresco cada `egress.ss_refresh`,
  10s), cruzando (protocolo, puerto local, ip:puerto remoto). Como un
  snapshot anterior al flujo no puede contenerlo, cada flujo espera
  `attribution_delay` (0.5s) y se busca en un snapshot tomado despues
  (refresco bajo demanda, max uno cada `ss_min_interval`, 1s). UDP no
  conectado (QUIC) cae a `sudo -n ss -uanpH` por puerto local. Si no
  aparece: `host:unknown`.
- **Snapshot de respaldo**: cada `egress.interval` (60s) el ciclo toma los
  flujos acumulados; solo si el stream estuvo caido desde el ciclo anterior
  (o es el primer ciclo, para ver las conexiones que ya existian) agrega
  `sudo -n ss -tunpH state established` (descartando entrantes por puerto en
  escucha segun `ss -tulnH`) y `sudo -n conntrack -L -o extended`
  (TCP ESTABLISHED + UDP: containers, y flujos salientes del host que ss no
  vio, como QUIC en sockets UDP no conectados; proceso atribuido igual que
  en el stream, por `ss -tunpH` y puerto local en `ss -uanpH`).
- **Identidad**: ASN y organizacion via DNS de Team Cymru (TXT a
  `<ip-invertida>.origin.asn.cymru.com` / `origin6` y `AS<n>.asn.cymru.com`,
  cliente DNS propio con stdlib) + reverse DNS solo para mostrar. Cache en
  `batcave/memory/egress_asn_cache.json` (TTL `egress.asn_cache_days`, 7).
- **Linea base** (`batcave/memory/egress_baseline.json`): llave
  `(origen, ASN, puerto, protocolo)`, nunca la IP; `first_seen`,
  `last_seen`, `count`, `origin` (learning/allowlist/evaluated). Durante
  `egress.learning_days` (7) desde el primer ciclo solo se aprende. Las
  llaves del formato anterior (`proceso|ASn|puerto`) se migran solas a
  `host:proceso|ASn|puerto|tcp`.
- **Destino nuevo** (llave nunca vista, ya fuera de aprendizaje): si una
  entrada de `egress_orgs` del allowlist cubre el flujo entra directo a la
  linea base. Cada entrada: `org` (substring), y opcionales `sources`
  (fnmatch sobre el origen: `host:cloudflared`, `docker:*`) y `ports`
  (`7844/udp`, `443/tcp`); deben coincidir todos los presentes. Los CDN
  (`cdn: true`; Cloudflare, Fastly, Akamai y CloudFront se fuerzan como CDN)
  nunca se allowlistean por organizacion completa: exigen `sources` y
  `ports` (si faltan, la entrada se rechaza al cargar con un warning) y una
  entrada amplia no los cubre. Hoy: Cloudflare -> `host:cloudflared`
  7844/udp+tcp, `host:python3`/`host:penguin` 443/tcp; Fastly ->
  `host:python3`/`host:pip` 443/tcp; GitHub, Telegram, Anthropic, Canonical
  y Docker por organizacion. Si no hay entrada que cubra el flujo (p. ej.
  otro proceso hacia Cloudflare), TypeSafe (Jev): `is_suspicious` (Noul),
  `severity` (Score), `action` (Choice: notify_now/digest/learn), con
  origen, ASN/org, rDNS, puerto, protocolo y antiguedad del origen en la
  linea base.
  Fallback: `notify_now` si el puerto no esta en `egress.safe_ports`
  (80/443/53/853/22/7844) o el origen no existe en la linea base; si no
  `digest`. Toda llave evaluada se agrega a la linea base (alerta una vez).
  Notificacion y dedup como las alertas de Wazuh (llave `egress|...`;
  high/critical → `type: egress_alert` para Batman en `comms.json`).
  Topes por ciclo: `max_lookups_per_cycle`, `max_evaluations_per_cycle`.
- **Digest**: seccion Egress (progreso del aprendizaje o destinos nuevos
  desde el ultimo digest + top 5 origenes por destinos distintos).
- `python3 batwoman.py --egress-snapshot` imprime las lineas de sudoers
  requeridas, escucha el stream 60s y muestra esos flujos (hora, cantidad)
  mas el snapshot actual de ss/conntrack, clasificados
  (`origen -> org [ASN] :puerto/protocolo`, new/known/allowlisted) sin
  alertar ni guardar nada.
- Requiere en `sudoers.d/batifamilia` (NOPASSWD) exactamente
  `/usr/bin/ss -tunpH state established`, `/usr/bin/ss -uanpH`,
  `/usr/sbin/conntrack -L -o extended` y
  `/usr/sbin/conntrack -E -e NEW -o timestamp\,extended` (coma escapada en
  sudoers; allowlisteados para la regla 5402 en `batwoman_allowlist.yaml`).

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
sudo cp night-agent.service night-agent.timer lucius-fox.service lucius-fox.timer red-hood.service red-hood.timer batwoman.service \
  radar-nightwing.service radar-nightwing.timer radar-nightwing-weekly.service radar-nightwing-weekly.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now night-agent.timer lucius-fox.timer red-hood.timer batwoman.service \
  radar-nightwing.timer radar-nightwing-weekly.timer
```

## Convenciones

- Python async (`asyncio`), docstrings y logs en espanol.
- `config.yaml` para umbrales/horarios/repos; nada de eso hardcodeado en el codigo.
- Un fallo de TypeSafe (o de Telegram) nunca debe tumbar el ciclo completo: se
  loguea y se sigue (ver `except Exception` en `modes/monitoring.py` y
  `lucius_fox.py`).
- Telegram: los tres agentes que notifican por Telegram (`lucius_fox.py`,
  `signal_agent.py`, `red_hood.py`) usan `parse_mode=ParseMode.HTML`, nunca
  Markdown. Todo contenido dinamico (nombres de archivo, repos, ramas,
  errores) va siempre por `html.escape()` antes de interpolarse en el
  mensaje; negritas con `<b>` y monoespaciado con `<code>`. Markdown se
  rompe con `Can't parse entities` en cuanto un nombre de archivo trae `_`
  o `*` (comun en `*.spec.ts`, `test_*.py`), HTML no tiene ese problema una
  vez escapado.
- `knowledge/` y `reports/` son generados, no se versionan (`.gitignore`).
