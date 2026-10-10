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

## Digest diario
- A `batwoman.digest_hour` (America/El_Salvador): conteo por nivel, top 5 reglas, top 5 IPs, cambios FIM. Se envia a Alfred, se guarda en `reports/batwoman-YYYY-MM-DD.md` y se reinicia.
