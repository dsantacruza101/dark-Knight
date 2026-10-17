keywords: cloudflare, cloudflared, tunnel, nginx, real_ip, realip, ip, cf-connecting-ip, x-forwarded-for, fail2ban, rate, limit
> 🌱 Nota semilla (2026-10-17, escrita a mano). Refrescar si cambia la topologia del tunel.

# Cloudflare Tunnel + Nginx: la IP real del cliente

Recordatorio: `nginx`, `cloudflared` y `fail2ban` estan en el candado duro del modo
Desarrollo. Esta nota es para diagnosticar y proponer el cambio en el issue; lo aplica un humano.

## Por que importa
Con el tunel, TODO el trafico llega a Nginx desde `cloudflared` en `127.0.0.1`. Sin
configurar real IP:
- `$remote_addr` = 127.0.0.1 en `access.log` -> Batman (modo Monitoreo) y fail2ban ven una
  sola IP; un ban de fail2ban sobre 127.0.0.1 tumbaria todo el sitio.
- `limit_req` (10r/s) se aplica a todos los visitantes juntos como si fueran uno.

## Configuracion (http {} de nginx)
```nginx
# El tunel conecta desde loopback: confiar solo en ese salto.
set_real_ip_from 127.0.0.1;
set_real_ip_from ::1;
real_ip_header CF-Connecting-IP;
```
- `CF-Connecting-IP` lo pone Cloudflare con la IP del cliente; con el tunel no hace falta
  listar los rangos de Cloudflare (solo confiamos en loopback, que es cloudflared).
- Si algun dia se expone Nginx directo (sin tunel), ahi si: `set_real_ip_from` con los
  rangos de https://www.cloudflare.com/ips-v4 y ips-v6, y bloquear el resto en UFW.
- `real_ip_recursive` no hace falta con un solo proxy de confianza.
- `limit_req_zone $binary_remote_addr ...` ya usa la IP real despues de esto.

## Backend
- Pasar la IP al API: `proxy_set_header X-Real-IP $remote_addr;` y
  `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;`.
- NestJS/Express detras de proxy: `app.set('trust proxy', 'loopback')` (no `true`: cualquiera
  podria falsificar X-Forwarded-For).

## cloudflared
- Ingress en `~/.cloudflared/config.yml`; cada hostname apunta a `http://localhost:80` (Nginx)
  salvo ssh (`ssh://localhost:2222`). Ultima regla siempre `service: http_status:404`.
- Validar: `cloudflared tunnel ingress validate` y `cloudflared tunnel ingress rule https://api.dsantacruz.com`.
- El egress de cloudflared es QUIC 7844/udp (allowlist de Batwoman: `host:cloudflared`).

## Verificar (humano)
- `sudo nginx -t && sudo systemctl reload nginx` (regla global: siempre `nginx -t` antes).
- `tail -f /var/log/nginx/access.log` y abrir el sitio desde el celular (datos moviles):
  debe aparecer la IP publica del celular, no 127.0.0.1.
- `sudo fail2ban-client status nginx-...`: los baneos deben ser IPs publicas.
