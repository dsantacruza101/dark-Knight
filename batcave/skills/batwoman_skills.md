# Batwoman (Kate Kane) — Skills

## Fuente
- Alertas de Wazuh: `/var/ossec/logs/alerts/alerts.json` dentro de `single-node-wazuh.manager-1`, leidas con `docker exec` (`stat` + `tail -c +offset | head -c 5MB`).
- Progreso en `batcave/memory/batwoman_state.json` (offset, tamano, inode). Si el archivo se achica o cambia de inode (rotacion diaria) relee desde 0. Primer arranque: empieza al final del archivo.
- Una alerta JSON por linea; las malformadas se omiten. Solo se consume hasta el ultimo `\n` (una alerta a medio escribir se lee en el siguiente poll).

## Triage (en este orden)
1. **Ruta critica** (`batwoman.critical_paths`): toda alerta de syscheck sobre esas rutas se evalua siempre, sin importar nivel ni allowlist.
2. **Allowlist** (`batcave/batwoman_allowlist.yaml`): `rule_id` + regex sobre `full_log` / `syscheck.path`. Hoy: regla 5402 (sudo a root) con los comandos de `sudoers.d/batifamilia` (systemctl restart/start/enable --now de units conocidas, `fail2ban-client status`). Los cambios FIM en `night-agent` NO se ignoran.
3. **Nivel**: < 7 se ignora; 7-9 va al digest; >= 10 se evalua.

## Decisiones (TypeSafe / Jev)
- `is_real_threat` (Noul), `severity` (Score: info/low/medium/high/critical), `action` (Choice: notify_now/digest/ignore).
- Fallback sin `TYPESAFE_API_KEY` o si Jev falla: `notify_now` si nivel >= 12 o ruta critica, si no `digest`.
- Una ruta critica nunca termina en `ignore` (se sube a `digest`).

## Notificacion
- `notify_now` → Telegram a Alfred, empieza con "🦇 Batwoman", valores en `<code>` escapados.
- Severidad high/critical → mensaje `security_alert` para Batman en `batcave/comms.json`.
- Dedup en `batcave/memory/batwoman_alert_state.json` por `rule_id|srcip-o-ruta`: se reenvia solo tras 6h.

## Conexiones salientes (egress, `batcave/egress.py`)
- Cada `batwoman.egress.interval` s (60). Host: `sudo -n ss -tnpH state established` (proceso; se descartan las entrantes, puerto local en `ss -tlnH`). Containers: `sudo -n conntrack -L -p tcp -o extended` (direccion original src/dst/dport, solo ESTABLISHED); IP de origen -> container via `docker network inspect` (cache 10 min).
- Solo destinos publicos (`is_global`, fuera de subredes Docker): sin loopback, RFC1918, link-local, CGNAT.
- Identidad: ASN + organizacion por DNS de Team Cymru (TXT `<ip-invertida>.origin.asn.cymru.com` / `origin6` en IPv6, y `AS<n>.asn.cymru.com`) + reverse DNS solo para mostrar. Cache `batcave/memory/egress_asn_cache.json` (TTL 7 dias). Cliente DNS propio (stdlib, resolver de `/etc/resolv.conf`).
- Llave de linea base: `(proceso o docker:<container>, ASN, puerto)`, nunca la IP. `batcave/memory/egress_baseline.json` (`started_at` + `keys` con first_seen/last_seen/count/origin).
- Aprendizaje `egress.learning_days` (7) desde el primer ciclo: todo se registra, nada alerta.
- Despues: llave nunca vista = destino nuevo. Organizacion en `egress_orgs` del allowlist (substring del nombre) → directo a la linea base. Si no, TypeSafe: `is_suspicious` (Noul), `severity` (Score), `action` (Choice: notify_now/digest/learn). Fallback: `notify_now` si el puerto no esta en `egress.safe_ports` (80/443/53/853/22/7844) o el origen no existe en la linea base; si no `digest`. Toda llave evaluada entra a la linea base (alerta una sola vez).
- Notificacion igual que Wazuh: `notify_now` → Telegram; high/critical → `egress_alert` para Batman en `comms.json`; dedup `egress|<llave>` en `batwoman_alert_state.json`.
- Topes por ciclo: `max_lookups_per_cycle` IPs nuevas, `max_evaluations_per_cycle` evaluaciones; el resto en el siguiente ciclo.
- `--egress-snapshot`: imprime `origen -> org [ASN] :puerto  IP (rDNS)` con estado new/known/allowlisted; no alerta ni guarda (ni cache).

## Digest diario
- A `batwoman.digest_hour` (America/El_Salvador): conteo por nivel, top 5 reglas, top 5 IPs, cambios FIM, integridad de paquetes y seccion **Egress** (progreso del aprendizaje o destinos nuevos desde el ultimo digest, top 5 procesos/containers por destinos distintos ASN+puerto). Se envia a Alfred, se guarda en `reports/batwoman-YYYY-MM-DD.md` y se reinicia.
