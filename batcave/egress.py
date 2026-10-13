"""Monitoreo de conexiones salientes (egress) de Batwoman.

Fuentes de flujos salientes hacia IPs publicas:

  1. Stream de eventos (principal): `sudo -n conntrack -E -e NEW -o
     timestamp,extended` corre en segundo plano (ConntrackStream) y ve cada
     flujo NUEVO, TCP y UDP (QUIC de cloudflared, llamadas cortas a
     TypeSafe/GitHub/Telegram). Si el proceso muere se reinicia con backoff
     exponencial. El origen es `docker:<container>` si la IP de origen esta
     en una subred de Docker, o `host:<proceso>` si es una IP del host: el
     proceso sale de un snapshot cacheado de `sudo -n ss -tunpH state
     established` (refresco cada `ss_refresh` s, y bajo demanda cuando llega
     un flujo nuevo, porque un snapshot anterior al flujo no puede
     contenerlo), cruzando puerto local + ip:puerto remoto. Para UDP no
     conectado (QUIC) se cae a `sudo -n ss -uanpH` por puerto local. Si no
     aparece: `host:unknown`.
  2. Snapshot periodico (respaldo): solo si el stream estuvo caido desde el
     ciclo anterior (y en el primer ciclo, para ver las conexiones que ya
     existian antes de arrancar). Host: `sudo -n ss -tunpH state established`
     (TCP + UDP conectado), descartando entrantes (puerto local en escucha
     segun `ss -tulnH`). Containers: `sudo -n conntrack -L -o extended`.

Se omiten loopback, RFC1918, link-local, CGNAT, las subredes de Docker y el
DNS (53/853) hacia los resolvers del sistema (/etc/resolv.conf y los
upstream de systemd-resolved).

Lo llama el ciclo principal de batwoman.py cada `batwoman.egress.interval`
segundos (60 por defecto). Cada ciclo:

  1. Toma los flujos acumulados por el stream (y el snapshot de respaldo si
     hizo falta), deduplicados por (origen, ip, puerto, protocolo).
  2. Identidad del destino: ASN y organizacion via DNS de Team Cymru (TXT a
     <ip-invertida>.origin.asn.cymru.com / origin6 para IPv6 y
     AS<n>.asn.cymru.com), mas reverse DNS (solo para mostrar). Cache
     persistente en batcave/memory/egress_asn_cache.json (TTL 7 dias).
  3. Linea base (batcave/memory/egress_baseline.json) por la llave
     (origen, ASN destino, puerto destino, protocolo); nunca por IP.
     Durante `learning_days` desde el primer ciclo solo se aprende. Despues,
     una llave nunca vista es un "destino nuevo": si una entrada de
     `egress_orgs` del allowlist cubre el flujo (organizacion, y si la
     entrada los define, origen y puerto/protocolo; obligatorios para los
     CDN) entra directo a la linea base; si no, se
     evalua con TypeSafe (o el fallback) y luego se agrega a la linea base
     (cada llave alerta una sola vez).

La notificacion (Telegram / comms.json / dedup) la hace batwoman.py con lo
que devuelve `run_cycle`.
"""

from __future__ import annotations

import asyncio
import fnmatch
import html
import ipaddress
import json
import logging
import random
import re
import socket
import struct
import subprocess
import time
from collections import Counter, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import yaml
from typesafe_sdk import AsyncTypeSafeClient, Choice, Noul, Score

from batcave.secrets import mask_secrets

BASE_DIR = Path(__file__).resolve().parent
MEMORY_DIR = BASE_DIR / "memory"
BASELINE_PATH = MEMORY_DIR / "egress_baseline.json"
ASN_CACHE_PATH = MEMORY_DIR / "egress_asn_cache.json"
ALLOWLIST_PATH = BASE_DIR / "batwoman_allowlist.yaml"
RESOLV_CONF = Path("/etc/resolv.conf")
# Upstreams reales de systemd-resolved (/etc/resolv.conf solo trae el stub).
RESOLVED_UPSTREAM_CONF = Path("/run/systemd/resolve/resolv.conf")

log = logging.getLogger("batwoman.egress")

DEFAULTS = {
    "enabled": True,
    "interval": 60,
    "stream": True,                 # conntrack -E en segundo plano (si no, solo snapshots)
    "ss_refresh": 10,               # segundos entre refrescos periodicos del cache de ss
    "attribution_delay": 0.5,       # segundos de espera tras un flujo nuevo antes de buscar su proceso
    "ss_min_interval": 1,           # segundos minimos entre refrescos de ss bajo demanda
    "learning_days": 7,
    "safe_ports": [80, 443, 53, 853, 22, 7844],
    "asn_cache_days": 7,
    "docker_refresh": 600,          # segundos entre `docker network inspect`
    "dns_timeout": 3,               # segundos por consulta DNS
    "command_timeout": 20,          # segundos por ss / conntrack / docker
    "max_lookups_per_cycle": 50,    # IPs nuevas a resolver por ciclo (el resto, el proximo)
    "max_evaluations_per_cycle": 10,  # destinos nuevos evaluados por ciclo (el resto, el proximo)
}

# Comandos con sudo: deben coincidir EXACTO con /etc/sudoers.d/batifamilia.
HOST_CMD = ["sudo", "-n", "ss", "-tunpH", "state", "established"]
UDP_SOCKETS_CMD = ["sudo", "-n", "ss", "-uanpH"]
CONNTRACK_CMD = ["sudo", "-n", "conntrack", "-L", "-o", "extended"]
EVENTS_CMD = ["sudo", "-n", "conntrack", "-E", "-e", "NEW", "-o", "timestamp,extended"]
LISTEN_CMD = ["ss", "-tulnH"]
ADDR_CMD = ["ip", "-o", "addr", "show"]

# Lineas para /etc/sudoers.d/batifamilia (las comas de los argumentos van
# escapadas en sudoers). Las imprime --egress-snapshot.
SUDOERS_LINES = [
    "dsantacruz ALL=(root) NOPASSWD: /usr/bin/ss -tunpH state established",
    "dsantacruz ALL=(root) NOPASSWD: /usr/bin/ss -uanpH",
    "dsantacruz ALL=(root) NOPASSWD: /usr/sbin/conntrack -L -o extended",
    "dsantacruz ALL=(root) NOPASSWD: /usr/sbin/conntrack -E -e NEW -o timestamp\\,extended",
]

PROTOCOLS = ("tcp", "udp")
DNS_PORTS = {53, 853}
STREAM_BACKOFF_MAX = 300        # segundos maximos entre reintentos del stream
STREAM_STABLE = 300             # si el stream duro mas que esto, el backoff vuelve a 1s
ATTRIBUTION_TICK = 0.25         # segundos entre pasadas del atribuidor
PENDING_MAX = 5000              # flujos esperando atribucion (rafagas: se descartan los mas viejos)
FLOWS_MAX = 5000                # flujos distintos acumulados entre ciclos
RECENT_MAX = 5000               # eventos recientes (para --egress-snapshot)
SNAPSHOT_LISTEN_SECONDS = 60

SEVERITY_LEVELS = ["info", "low", "medium", "high", "critical"]
ACTION_CRITERIA = {
    "notify_now": "Hay que avisar a Alfred de inmediato: posible exfiltracion, C2, minero o proceso comprometido.",
    "digest": "Es raro pero plausible; basta con revisarlo en el digest diario.",
    "learn": "Es trafico legitimo esperable para ese proceso/container; aprenderlo sin avisar.",
}
UNROUTED_ASN = "0"
DIGEST_MAX_NEW = 50
SS_PROCESS = re.compile(r'users:\(\("([^"]+)"')
# CDNs/hosting que alojan millones de sitios ajenos (incluido C2): aunque el
# allowlist olvide `cdn: true`, se tratan como CDN (exigen sources + ports).
KNOWN_CDN_ORGS = ("cloudflare", "fastly", "akamai", "cloudfront")

_last_errors: dict[str, str] = {}


def load_config(batwoman_config: dict) -> dict:
    return {**DEFAULTS, **(batwoman_config.get("egress") or {})}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _warn_once(kind: str, message: str) -> None:
    """Loguea un error de comando solo cuando cambia (evita spam cada 60s)."""
    if _last_errors.get(kind) != message:
        log.warning("%s", message)
        _last_errors[kind] = message


def _clear_error(kind: str) -> None:
    if _last_errors.pop(kind, None) is not None:
        log.info("Egress: %s vuelve a funcionar", kind)


def _run(kind: str, cmd: list[str], timeout: int) -> Optional[str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        _warn_once(kind, f"Egress: fallo {' '.join(cmd)}: {exc}")
        return None
    if proc.returncode != 0:
        _warn_once(kind, f"Egress: {' '.join(cmd)} devolvio {proc.returncode}: {proc.stderr.strip()[-300:]}")
        return None
    _clear_error(kind)
    return proc.stdout


# --- Direcciones ----------------------------------------------------------------


def _parse_ip(value: str) -> Optional[ipaddress._BaseAddress]:
    value = value.strip("[]").split("%", 1)[0]
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        return ip.ipv4_mapped
    return ip


def _split_hostport(value: str) -> tuple[Optional[ipaddress._BaseAddress], Optional[int]]:
    host, _, port = value.rpartition(":")
    try:
        return _parse_ip(host), int(port)
    except ValueError:
        return None, None


def is_public(ip: ipaddress._BaseAddress, docker_subnets: list) -> bool:
    """Solo IPs globales: fuera de loopback, RFC1918, link-local, CGNAT,
    reservadas, multicast y de las subredes de Docker."""
    if not ip.is_global or ip.is_multicast:
        return False
    return not any(ip.version == net.version and ip in net for net in docker_subnets)


# --- Docker ------------------------------------------------------------------------


def system_resolvers() -> set[str]:
    """IPs de los resolvers del sistema: el stub de /etc/resolv.conf y los
    upstream de systemd-resolved."""
    servers = set()
    for path in (RESOLV_CONF, RESOLVED_UPSTREAM_CONF):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            parts = line.split()
            if len(parts) >= 2 and parts[0] == "nameserver":
                ip = _parse_ip(parts[1])
                if ip is not None:
                    servers.add(str(ip))
    return servers


def host_addresses(timeout: int) -> set[str]:
    """IPs configuradas en las interfaces del host (`ip -o addr show`)."""
    out = _run("ip addr", ADDR_CMD, timeout) or ""
    addrs = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[2] in ("inet", "inet6"):
            ip = _parse_ip(parts[3].split("/", 1)[0])
            if ip is not None:
                addrs.add(str(ip))
    return addrs


class DockerMap:
    """IP de container -> nombre, subredes de Docker, IPs del host y
    resolvers del sistema. Se refresca cada `docker_refresh` segundos (o
    antes, si aparece una IP desconocida dentro de una subred de Docker, como
    mucho una vez por minuto)."""

    def __init__(self, ecfg: dict) -> None:
        self.refresh_every = int(ecfg["docker_refresh"])
        self.timeout = int(ecfg["command_timeout"])
        self.by_ip: dict[str, str] = {}
        self.subnets: list = []
        self.local_ips: set[str] = set()
        self.dns_servers: set[str] = set()
        self.loaded_at = 0.0

    def refresh(self, force: bool = False) -> None:
        age = time.monotonic() - self.loaded_at
        if self.loaded_at and age < self.refresh_every and not (force and age > 60):
            return
        self.loaded_at = time.monotonic()
        self.local_ips = host_addresses(self.timeout) or self.local_ips
        self.dns_servers = system_resolvers()
        ids = _run("docker network ls", ["docker", "network", "ls", "-q"], self.timeout)
        if not ids or not ids.split():
            return
        out = _run("docker network inspect", ["docker", "network", "inspect", *ids.split()], self.timeout)
        if out is None:
            return
        try:
            networks = json.loads(out)
        except json.JSONDecodeError:
            _warn_once("docker network inspect", "Egress: salida invalida de docker network inspect")
            return
        by_ip, subnets = {}, []
        for net in networks:
            for cfg in ((net.get("IPAM") or {}).get("Config") or []):
                try:
                    subnets.append(ipaddress.ip_network(cfg.get("Subnet", ""), strict=False))
                except ValueError:
                    continue
            for container in (net.get("Containers") or {}).values():
                for field in ("IPv4Address", "IPv6Address"):
                    addr = (container.get(field) or "").split("/", 1)[0]
                    if addr:
                        by_ip[addr] = container.get("Name", "?")
        self.by_ip, self.subnets = by_ip, subnets

    def in_docker(self, ip: ipaddress._BaseAddress) -> bool:
        return any(ip.version == net.version and ip in net for net in self.subnets)

    def is_host(self, ip: ipaddress._BaseAddress) -> bool:
        # Sin `ip addr` disponible, cualquier IP no global se asume del host.
        return str(ip) in self.local_ips if self.local_ips else not ip.is_global


def classify_flow(src, dst, dport: int, docker: DockerMap) -> Optional[str]:
    """'container' / 'host' si es un flujo saliente a una IP publica que nos
    interesa; '?' si el origen es una IP privada desconocida (quizas una red
    de Docker nueva: hay que refrescar el mapa); None si se ignora (destino
    privado/loopback/link-local/Docker, DNS al resolver del sistema, o flujo
    entrante/reenviado)."""
    if src is None or dst is None or not is_public(dst, docker.subnets):
        return None
    if str(dst) in docker.local_ips:
        return None
    if dport in DNS_PORTS and str(dst) in docker.dns_servers:
        return None
    if docker.in_docker(src):
        return "container"
    if docker.is_host(src):
        return "host"
    return "?" if not src.is_global else None


# --- Parsers de ss y conntrack ----------------------------------------------------------


def parse_ss_line(line: str) -> Optional[dict]:
    """Una linea de `ss -H`: [Netid] [State] Recv-Q Send-Q Local Peer [proceso].
    Netid solo aparece si se piden varias tablas (-tu); State solo sin filtro
    `state`."""
    tokens = line.split()
    proto = None
    if tokens and tokens[0] in PROTOCOLS:
        proto = tokens.pop(0)
    if tokens and not tokens[0].isdigit():
        tokens.pop(0)
    if len(tokens) < 4:
        return None
    local_ip, local_port = _split_hostport(tokens[2])
    peer_ip, peer_port = _split_hostport(tokens[3])
    match = SS_PROCESS.search(" ".join(tokens[4:]))
    return {
        "proto": proto, "local_ip": local_ip, "local_port": local_port,
        "peer_ip": peer_ip, "peer_port": peer_port, "process": match.group(1) if match else None,
    }


CT_TIMESTAMP = re.compile(r"^\s*\[(\d+(?:\.\d+)?)\]")


def parse_conntrack_line(line: str) -> Optional[dict]:
    """Una linea de conntrack (-L o -E, `-o extended` y opcional `timestamp`).
    Devuelve la direccion ORIGINAL (primera aparicion de src/dst/sport/dport);
    None si no es TCP/UDP o esta incompleta."""
    tokens = line.split()
    proto = next((t for t in tokens if t in PROTOCOLS), None)
    if proto is None:
        return None
    original: dict[str, str] = {}
    for token in tokens:
        key, sep, value = token.partition("=")
        if sep and key in ("src", "dst", "sport", "dport") and key not in original:
            original[key] = value
    try:
        sport, dport = int(original.get("sport", "")), int(original.get("dport", ""))
    except ValueError:
        return None
    src, dst = _parse_ip(original.get("src", "")), _parse_ip(original.get("dst", ""))
    if src is None or dst is None:
        return None
    ts = CT_TIMESTAMP.match(line)
    return {
        "ts": float(ts.group(1)) if ts else time.time(),
        "proto": proto, "src": src, "dst": dst, "sport": sport, "dport": dport,
        "established": "ESTABLISHED" in tokens,
    }


def host_source(process: Optional[str]) -> str:
    return f"host:{process or 'unknown'}"


# --- Snapshot (respaldo) ----------------------------------------------------------------


def listening_ports(timeout: int) -> set[tuple[str, int]]:
    out = _run("ss -tulnH", LISTEN_CMD, timeout) or ""
    ports = set()
    for line in out.splitlines():
        parsed = parse_ss_line(line)
        if parsed and parsed["proto"] and parsed["local_port"] is not None:
            ports.add((parsed["proto"], parsed["local_port"]))
    return ports


def host_connections(ecfg: dict, docker: DockerMap) -> list[dict]:
    timeout = int(ecfg["command_timeout"])
    out = _run("ss (sudo)", HOST_CMD, timeout)
    if out is None:
        return []
    listening = listening_ports(timeout)
    conns = []
    for line in out.splitlines():
        parsed = parse_ss_line(line)
        if not parsed or parsed["proto"] is None or parsed["peer_port"] is None:
            continue
        if (parsed["proto"], parsed["local_port"]) in listening:
            continue
        if classify_flow(parsed["local_ip"], parsed["peer_ip"], parsed["peer_port"], docker) != "host":
            continue
        conns.append(
            {
                "source": host_source(parsed["process"]),
                "kind": "host",
                "ip": str(parsed["peer_ip"]),
                "port": parsed["peer_port"],
                "protocol": parsed["proto"],
                "local": f"{parsed['local_ip']}:{parsed['local_port']}",
            }
        )
    return conns


def container_connections(ecfg: dict, docker: DockerMap) -> list[dict]:
    out = _run("conntrack (sudo)", CONNTRACK_CMD, int(ecfg["command_timeout"]))
    if out is None:
        return []
    conns = []
    for line in out.splitlines():
        flow = parse_conntrack_line(line)
        # UDP no tiene estado en conntrack; TCP solo establecidas.
        if flow is None or (flow["proto"] == "tcp" and not flow["established"]):
            continue
        if classify_flow(flow["src"], flow["dst"], flow["dport"], docker) != "container":
            continue
        name = docker.by_ip.get(str(flow["src"]))
        if name is None:
            docker.refresh(force=True)
            name = docker.by_ip.get(str(flow["src"]))
        if name is None:
            log.debug("Egress: IP de container sin nombre %s, se reintenta el proximo ciclo", flow["src"])
            continue
        conns.append(
            {
                "source": f"docker:{name}", "kind": "container", "ip": str(flow["dst"]),
                "port": flow["dport"], "protocol": flow["proto"], "local": str(flow["src"]),
            }
        )
    return conns


def take_snapshot(ecfg: dict, docker: DockerMap) -> list[dict]:
    docker.refresh()
    return host_connections(ecfg, docker) + container_connections(ecfg, docker)


# --- Stream de eventos (conntrack -E) ------------------------------------------------------


class ConntrackStream:
    """Lector de larga duracion de `conntrack -E -e NEW` mas el atribuidor
    de procesos. Los flujos salientes atribuidos se acumulan (deduplicados)
    hasta que el ciclo de egress los toma con `drain()`."""

    def __init__(self, ecfg: dict, docker: DockerMap) -> None:
        self.docker = docker
        self.timeout = int(ecfg["command_timeout"])
        self.ss_refresh = float(ecfg["ss_refresh"])
        self.delay = float(ecfg["attribution_delay"])
        self.min_interval = float(ecfg["ss_min_interval"])
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.running = False
        self.gap = True             # hubo un hueco sin stream desde el ultimo drain()
        self.pending: deque = deque()
        self.flows: dict[tuple, dict] = {}
        self.recent: deque = deque(maxlen=RECENT_MAX)
        self.dropped = 0
        self.ss_index: dict[tuple, str] = {}
        self.ss_at = float("-inf")
        self.udp_index: dict[int, str] = {}
        self.udp_at = float("-inf")
        self.tasks: list[asyncio.Task] = []

    def start(self) -> None:
        self.tasks = [asyncio.create_task(self._reader()), asyncio.create_task(self._attributor())]

    async def stop(self) -> None:
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks = []
        await self._kill()

    # Lector ------------------------------------------------------------------------

    async def _reader(self) -> None:
        backoff = 1.0
        while True:
            started = time.monotonic()
            try:
                reason = await self._read_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - el stream se reinicia, nunca tumba a Batwoman
                reason = f"{type(exc).__name__}: {exc}"
            finally:
                self.running = False
                self.gap = True
            if time.monotonic() - started > STREAM_STABLE:
                backoff = 1.0
            log.warning(
                "Egress: el stream de conntrack termino (%s); reintento en %.0fs, mientras tanto snapshot de respaldo",
                reason, backoff,
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, STREAM_BACKOFF_MAX)

    async def _read_once(self) -> str:
        await asyncio.to_thread(self.docker.refresh)
        # stderr va al mismo pipe: si fuera aparte y nadie lo leyera, un error
        # repetido (p. ej. ENOBUFS) podria llenarlo y bloquear a conntrack.
        self.proc = await asyncio.create_subprocess_exec(
            *EVENTS_CMD, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        self.running = True
        log.info("Egress: stream de conntrack activo (pid %s)", self.proc.pid)
        last_other = ""
        try:
            while True:
                raw = await self.proc.stdout.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", "replace")
                try:
                    if not self._handle(line) and not CT_TIMESTAMP.match(line):
                        last_other = line.strip()
                except Exception:  # noqa: BLE001 - una linea rara no corta el stream
                    log.exception("Egress: no se pudo procesar la linea de conntrack %r", line[:300])
        finally:
            await self._kill()
        return f"codigo {self.proc.returncode}" + (f": {last_other[-300:]}" if last_other else "")

    async def _kill(self) -> None:
        proc = self.proc
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=5)
        except ProcessLookupError:
            pass
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()

    def _handle(self, line: str) -> bool:
        """Encola el flujo si es saliente a una IP publica. Devuelve False si
        la linea no es un evento de conntrack (p. ej. un error)."""
        flow = parse_conntrack_line(line)
        if flow is None:
            return False
        kind = classify_flow(flow["src"], flow["dst"], flow["dport"], self.docker)
        if kind is None:
            return True
        if len(self.pending) >= PENDING_MAX:
            self.pending.popleft()
            self.dropped += 1
        self.pending.append({**flow, "kind": kind, "arrival": time.monotonic()})
        return True

    # Atribuidor ------------------------------------------------------------------------

    async def _attributor(self) -> None:
        while True:
            await asyncio.sleep(ATTRIBUTION_TICK)
            try:
                await self._attribute()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - se reintenta en la proxima pasada
                log.exception("Egress: fallo atribuyendo flujos")

    async def _refresh_ss(self) -> None:
        taken = time.monotonic()
        out = await asyncio.to_thread(_run, "ss (sudo)", HOST_CMD, self.timeout)
        index = {}
        for line in (out or "").splitlines():
            parsed = parse_ss_line(line)
            if parsed and parsed["proto"] and parsed["process"] and parsed["peer_ip"] is not None:
                index[(parsed["proto"], parsed["local_port"], str(parsed["peer_ip"]), parsed["peer_port"])] = parsed["process"]
        self.ss_index, self.ss_at = index, taken

    async def _refresh_udp(self) -> None:
        taken = time.monotonic()
        out = await asyncio.to_thread(_run, "ss -u (sudo)", UDP_SOCKETS_CMD, self.timeout)
        index = {}
        for line in (out or "").splitlines():
            parsed = parse_ss_line(line)
            if parsed and parsed["process"] and parsed["local_port"] is not None:
                index[parsed["local_port"]] = parsed["process"]
        self.udp_index, self.udp_at = index, taken

    def _can_refresh(self, last: float) -> bool:
        return time.monotonic() - last >= self.min_interval

    async def _attribute(self) -> None:
        """Un snapshot de ss tomado ANTES de un flujo nuevo no puede
        contenerlo: cada flujo del host espera `attribution_delay` y se busca
        en el primer snapshot tomado despues (refresco bajo demanda, como
        mucho uno cada `ss_min_interval`)."""
        if self.running and self.ss_refresh > 0 and time.monotonic() - self.ss_at >= self.ss_refresh:
            await self._refresh_ss()
        waiting: deque = deque()
        while self.pending:
            ev = self.pending.popleft()
            if ev["kind"] != "host":
                await self._attribute_container(ev)
                continue
            due = ev["arrival"] + self.delay
            if time.monotonic() < due:
                waiting.append(ev)
                continue
            if self.ss_at < due:
                if not self._can_refresh(self.ss_at):
                    waiting.append(ev)
                    continue
                await self._refresh_ss()
            process = self.ss_index.get((ev["proto"], ev["sport"], str(ev["dst"]), ev["dport"]))
            if process is None and ev["proto"] == "udp":
                # QUIC (cloudflared) usa sockets UDP no conectados: solo hay puerto local.
                if self.udp_at < due:
                    if not self._can_refresh(self.udp_at):
                        waiting.append(ev)
                        continue
                    await self._refresh_udp()
                process = self.udp_index.get(ev["sport"])
            self._record(ev, "host", host_source(process))
        self.pending = waiting

    async def _attribute_container(self, ev: dict) -> None:
        src = str(ev["src"])
        if ev["kind"] == "?" or src not in self.docker.by_ip:
            await asyncio.to_thread(self.docker.refresh, True)
        kind = classify_flow(ev["src"], ev["dst"], ev["dport"], self.docker)
        if kind == "container":
            self._record(ev, "container", f"docker:{self.docker.by_ip.get(src, 'unknown')}")
        elif kind == "host":
            self.pending.append({**ev, "kind": "host"})
        else:
            log.debug("Egress: flujo de origen desconocido %s -> %s:%s, se ignora", src, ev["dst"], ev["dport"])

    def _record(self, ev: dict, kind: str, source: str) -> None:
        conn = {
            "source": source, "kind": kind, "ip": str(ev["dst"]), "port": ev["dport"],
            "protocol": ev["proto"], "local": f"{ev['src']}:{ev['sport']}",
        }
        self.recent.append({**conn, "ts": ev["ts"]})
        key = (source, conn["ip"], conn["port"], conn["protocol"])
        if key in self.flows:
            return
        if len(self.flows) >= FLOWS_MAX:
            self.dropped += 1
            return
        self.flows[key] = conn

    def drain(self) -> tuple[list[dict], bool]:
        """(flujos acumulados desde el ultimo drain, hubo hueco sin stream)."""
        conns, self.flows = list(self.flows.values()), {}
        if self.dropped:
            log.warning("Egress: %d flujo(s) descartado(s) por rafaga (topes del stream)", self.dropped)
            self.dropped = 0
        had_gap = self.gap or not self.running
        self.gap = not self.running
        return conns, had_gap


# --- DNS (Team Cymru + PTR) ------------------------------------------------------------


class DnsError(Exception):
    pass


def _system_nameserver() -> str:
    try:
        for line in RESOLV_CONF.read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] == "nameserver":
                return parts[1]
    except OSError:
        pass
    return "127.0.0.53"


def _read_name(data: bytes, offset: int) -> tuple[str, int]:
    labels, jumped, end, hops = [], False, offset, 0
    while True:
        if offset >= len(data) or hops > 64:
            raise DnsError("nombre DNS malformado")
        length = data[offset]
        if length & 0xC0 == 0xC0:
            pointer = struct.unpack(">H", data[offset:offset + 2])[0] & 0x3FFF
            if not jumped:
                end = offset + 2
            offset, jumped, hops = pointer, True, hops + 1
            continue
        if length == 0:
            if not jumped:
                end = offset + 1
            return ".".join(labels), end
        labels.append(data[offset + 1:offset + 1 + length].decode("ascii", "replace"))
        offset += length + 1


def dns_query(name: str, qtype: int, server: str, timeout: float) -> list:
    """Consulta DNS minima por UDP (TXT=16, PTR=12). Devuelve [] si NXDOMAIN o
    sin respuestas; DnsError si el servidor falla o no responde."""
    tid = random.randint(0, 0xFFFF)
    qname = b"".join(bytes([len(p)]) + p.encode("ascii") for p in name.rstrip(".").split(".")) + b"\0"
    packet = struct.pack(">HHHHHH", tid, 0x0100, 1, 0, 0, 0) + qname + struct.pack(">HH", qtype, 1)
    family = socket.AF_INET6 if ":" in server else socket.AF_INET
    with socket.socket(family, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        try:
            sock.sendto(packet, (server, 53))
            while True:
                data, _ = sock.recvfrom(4096)
                if len(data) >= 12 and struct.unpack(">H", data[:2])[0] == tid:
                    break
        except OSError as exc:
            raise DnsError(f"{name}: {exc}") from exc
    _, flags, qdcount, ancount, _, _ = struct.unpack(">HHHHHH", data[:12])
    rcode = flags & 0x000F
    if rcode == 3:
        return []
    if rcode != 0:
        raise DnsError(f"{name}: rcode {rcode}")
    offset = 12
    for _ in range(qdcount):
        _, offset = _read_name(data, offset)
        offset += 4
    answers = []
    for _ in range(ancount):
        _, offset = _read_name(data, offset)
        rtype, _, _, rdlength = struct.unpack(">HHIH", data[offset:offset + 10])
        offset += 10
        rdata = data[offset:offset + rdlength]
        if rtype == qtype == 16:
            parts, i = [], 0
            while i < len(rdata):
                parts.append(rdata[i + 1:i + 1 + rdata[i]].decode("utf-8", "replace"))
                i += rdata[i] + 1
            answers.append("".join(parts))
        elif rtype == qtype == 12:
            answers.append(_read_name(data, offset)[0])
        offset += rdlength
    return answers


class AsnResolver:
    """IP -> (ASN, prefijo, rDNS) y ASN -> organizacion, con cache persistente."""

    def __init__(self, ecfg: dict, persist: bool = True) -> None:
        self.ttl = timedelta(days=float(ecfg["asn_cache_days"]))
        self.timeout = float(ecfg["dns_timeout"])
        self.max_lookups = int(ecfg["max_lookups_per_cycle"])
        self.persist = persist
        self.server = _system_nameserver()
        raw = _read_json(ASN_CACHE_PATH, {})
        self.cache = {"ips": dict(raw.get("ips") or {}), "asns": dict(raw.get("asns") or {})}
        self.dirty = False

    def _fresh(self, entry: Optional[dict]) -> bool:
        ts = _parse_ts((entry or {}).get("ts"))
        return ts is not None and _now() - ts < self.ttl

    def _fetch_ip(self, ip: str) -> dict:
        addr = ipaddress.ip_address(ip)
        if addr.version == 4:
            origin = addr.reverse_pointer.removesuffix(".in-addr.arpa") + ".origin.asn.cymru.com"
        else:
            origin = addr.reverse_pointer.removesuffix(".ip6.arpa") + ".origin6.asn.cymru.com"
        # "13335 209242 | 104.16.0.0/13 | US | arin | 2014-03-28"; con varios
        # prefijos se usa el mas especifico.
        best = None
        for txt in dns_query(origin, 16, self.server, self.timeout):
            fields = [f.strip() for f in txt.split("|")]
            if len(fields) < 2 or not fields[0]:
                continue
            try:
                plen = ipaddress.ip_network(fields[1], strict=False).prefixlen
            except ValueError:
                plen = -1
            if best is None or plen > best[0]:
                best = (plen, fields[0].split()[0], fields[1])
        try:
            ptr = dns_query(addr.reverse_pointer, 12, self.server, self.timeout)
            rdns = ptr[0] if ptr else ""
        except DnsError:
            rdns = ""
        return {
            "asn": best[1] if best else UNROUTED_ASN,
            "prefix": best[2] if best else "",
            "rdns": rdns,
            "ts": _now().isoformat(),
        }

    def _fetch_org(self, asn: str) -> dict:
        if asn == UNROUTED_ASN:
            return {"org": "sin ASN (prefijo no anunciado)", "ts": _now().isoformat()}
        # "13335 | US | arin | 2010-07-14 | CLOUDFLARENET - Cloudflare, Inc., US"
        org = ""
        for txt in dns_query(f"AS{asn}.asn.cymru.com", 16, self.server, self.timeout):
            fields = [f.strip() for f in txt.split("|")]
            if len(fields) >= 5:
                org = fields[4]
                break
        return {"org": org or f"AS{asn}", "ts": _now().isoformat()}

    async def _gather(self, items: list[str], fetch) -> dict:
        sem = asyncio.Semaphore(8)

        async def one(item: str):
            async with sem:
                try:
                    return item, await asyncio.to_thread(fetch, item)
                except (DnsError, ValueError, OSError, IndexError, struct.error) as exc:
                    _warn_once("dns", f"Egress: no se pudo resolver {item} ({exc}), se reintenta el proximo ciclo")
                    return item, None

        return {item: res for item, res in await asyncio.gather(*(one(i) for i in items)) if res is not None}

    async def resolve_many(self, ips: set[str]) -> dict[str, dict]:
        """Identidad de cada IP: {asn, org, prefix, rdns}. Las IPs que no se
        pudieron resolver (o que superan el tope por ciclo) no aparecen."""
        pending = sorted(ip for ip in ips if not self._fresh(self.cache["ips"].get(ip)))
        if len(pending) > self.max_lookups:
            log.info("Egress: %d IP(s) por resolver, se resuelven %d este ciclo", len(pending), self.max_lookups)
            pending = pending[: self.max_lookups]
        for ip, entry in (await self._gather(pending, self._fetch_ip)).items():
            self.cache["ips"][ip] = entry
            self.dirty = True

        asns = {self.cache["ips"][ip]["asn"] for ip in ips if self._fresh(self.cache["ips"].get(ip))}
        missing = sorted(a for a in asns if not self._fresh(self.cache["asns"].get(a)))
        for asn, entry in (await self._gather(missing, self._fetch_org)).items():
            self.cache["asns"][asn] = entry
            self.dirty = True

        result = {}
        for ip in ips:
            entry = self.cache["ips"].get(ip)
            if not self._fresh(entry):
                continue
            org = self.cache["asns"].get(entry["asn"])
            if not self._fresh(org):
                continue
            result[ip] = {"asn": entry["asn"], "org": org["org"], "prefix": entry.get("prefix", ""), "rdns": entry.get("rdns", "")}
        return result

    def save(self) -> None:
        if not (self.persist and self.dirty):
            return
        self.cache = {
            "ips": {k: v for k, v in self.cache["ips"].items() if self._fresh(v)},
            "asns": {k: v for k, v in self.cache["asns"].items() if self._fresh(v)},
        }
        _write_json(ASN_CACHE_PATH, self.cache)
        self.dirty = False


# --- Allowlist y linea base ---------------------------------------------------------------


def _parse_port_spec(value) -> Optional[tuple[int, str]]:
    """`7844/udp` -> (7844, "udp"). El protocolo es obligatorio."""
    port, _, protocol = str(value).strip().lower().partition("/")
    if not port.isdigit() or not 0 < int(port) < 65536 or protocol not in PROTOCOLS:
        return None
    return int(port), protocol


def _parse_org_entry(entry) -> tuple[Optional[dict], Optional[str]]:
    """Valida una entrada de `egress_orgs`. Devuelve (entrada, None) o
    (None, motivo del rechazo)."""
    if isinstance(entry, str):
        entry = {"org": entry}
    if not isinstance(entry, dict) or not str(entry.get("org") or "").strip():
        return None, "falta `org`"
    org = str(entry["org"]).strip().lower()
    cdn = bool(entry.get("cdn"))
    if not cdn and any(name in org for name in KNOWN_CDN_ORGS):
        _warn_once(f"allowlist-cdn:{org}", f"Egress: egress_orgs `{org}` es un CDN conocido sin `cdn: true`; se trata como CDN")
        cdn = True
    sources = entry.get("sources")
    ports = entry.get("ports")
    if sources is not None and (not isinstance(sources, list) or not all(isinstance(s, str) and s.strip() for s in sources)):
        return None, "`sources` debe ser una lista de patrones (p. ej. host:cloudflared, docker:*)"
    if ports is not None and not isinstance(ports, list):
        return None, "`ports` debe ser una lista de puerto/protocolo (p. ej. 443/tcp)"
    parsed_ports = set()
    for spec in ports or []:
        parsed = _parse_port_spec(spec)
        if parsed is None:
            return None, f"puerto invalido {spec!r} (formato puerto/tcp o puerto/udp)"
        parsed_ports.add(parsed)
    if cdn and (not sources or not parsed_ports):
        return None, "una entrada `cdn: true` exige `sources` y `ports` (el CDN aloja sitios ajenos, incluido C2)"
    return {
        "org": org,
        "reason": entry.get("reason", "sin razon"),
        "cdn": cdn,
        "sources": [s.strip() for s in sources] if sources else None,
        "ports": parsed_ports or None,
    }, None


def load_org_allowlist() -> list[dict]:
    """`egress_orgs` de batwoman_allowlist.yaml. Se relee cada ciclo.

    `org` es substring (sin distinguir mayusculas) del nombre de organizacion
    del ASN. `sources` (patrones fnmatch sobre el origen) y `ports`
    (`puerto/protocolo`) son opcionales, salvo en las entradas `cdn: true`,
    donde son obligatorios: una entrada CDN incompleta se rechaza."""
    try:
        with open(ALLOWLIST_PATH, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
    except (OSError, yaml.YAMLError) as exc:
        log.error("No se pudo leer %s: %s", ALLOWLIST_PATH, exc)
        return []
    entries = []
    for entry in (raw.get("egress_orgs") if isinstance(raw, dict) else None) or []:
        parsed, problem = _parse_org_entry(entry)
        if parsed is None:
            _warn_once(f"allowlist:{entry!r}", f"Egress: entrada de egress_orgs rechazada ({problem}): {entry!r}")
            continue
        entries.append(parsed)
    return entries


def match_org(conn: dict, allowlist: list[dict]) -> Optional[str]:
    """Razon de la primera entrada que cubre el flujo (org + origen + puerto),
    o None. Si la organizacion es un CDN (alguna entrada `cdn: true` o un CDN
    conocido coincide), solo cuentan las entradas con `sources` y `ports`: una
    entrada amplia nunca allowlistea trafico hacia un CDN."""
    org = conn["org"].lower()
    is_cdn = any(name in org for name in KNOWN_CDN_ORGS) or any(e["cdn"] and e["org"] in org for e in allowlist)
    port = (int(conn["port"]), conn.get("protocol", "tcp"))
    for entry in allowlist:
        if entry["org"] not in org:
            continue
        if is_cdn and (not entry["sources"] or not entry["ports"]):
            continue
        if entry["sources"] and not any(fnmatch.fnmatchcase(conn["source"], pat) for pat in entry["sources"]):
            continue
        if entry["ports"] and port not in entry["ports"]:
            continue
        return entry["reason"]
    return None


def baseline_key(source: str, asn: str, port: int, protocol: str) -> str:
    return f"{source}|AS{asn}|{port}|{protocol}"


def _migrate_baseline(keys: dict) -> dict:
    """Llaves del formato anterior (solo TCP, procesos del host sin prefijo):
    `proceso|ASn|puerto` -> `host:proceso|ASn|puerto|tcp`. Asi no se pierde
    lo aprendido."""
    migrated = {}
    for key, entry in keys.items():
        if "protocol" not in entry:
            entry["protocol"] = "tcp"
            source = entry.get("source", "")
            if entry.get("kind") == "host" and not source.startswith("host:"):
                entry["source"] = host_source(None if source == "desconocido" else source)
            key = baseline_key(entry["source"], entry["asn"], entry["port"], entry["protocol"])
        migrated[key] = entry
    return migrated


def load_baseline() -> dict:
    data = _read_json(BASELINE_PATH, {})
    if not isinstance(data, dict):
        data = {}
    data["keys"] = _migrate_baseline(data.get("keys") or {})
    return data


def learning_status(baseline: dict, ecfg: dict) -> tuple[bool, float]:
    """(en aprendizaje, dias restantes)."""
    started = _parse_ts(baseline.get("started_at"))
    if started is None:
        return True, float(ecfg["learning_days"])
    left = started + timedelta(days=float(ecfg["learning_days"])) - _now()
    return left.total_seconds() > 0, max(0.0, left.total_seconds() / 86400)


def source_first_seen(baseline: dict) -> dict[str, datetime]:
    first: dict[str, datetime] = {}
    for entry in baseline["keys"].values():
        ts = _parse_ts(entry.get("first_seen"))
        if ts and (entry["source"] not in first or ts < first[entry["source"]]):
            first[entry["source"]] = ts
    return first


def _human_age(first: Optional[datetime]) -> str:
    if first is None:
        return "nunca visto en la linea base"
    hours = (_now() - first).total_seconds() / 3600
    return f"{hours:.1f} horas" if hours < 48 else f"{hours / 24:.1f} dias"


# --- Evaluacion -------------------------------------------------------------------------------


def fallback_decision(finding: dict, ecfg: dict) -> dict:
    odd_port = finding["port"] not in {int(p) for p in ecfg["safe_ports"]}
    unknown_source = finding["source_first_seen"] is None
    notify = odd_port or unknown_source
    return {
        "is_suspicious": 1.0 if notify else 0.4,
        "severity": "high" if odd_port and unknown_source else "medium" if notify else "low",
        "action": "notify_now" if notify else "digest",
        "source": "fallback",
    }


def typesafe_state(finding: dict, known_destinations: int) -> dict:
    return {
        "origen": finding["source"],
        "tipo_origen": "container Docker" if finding["kind"] == "container" else "proceso del host",
        "asn": f"AS{finding['asn']}",
        "organizacion_destino": finding["org"],
        "reverse_dns": finding["rdns"] or "sin PTR",
        "puerto_destino": finding["port"],
        "protocolo": finding["protocol"] + (" (probablemente QUIC)" if finding["protocol"] == "udp" and finding["port"] == 443 else ""),
        "antiguedad_del_origen_en_linea_base": _human_age(finding["source_first_seen"]),
        "destinos_conocidos_del_origen": known_destinations,
        "context": (
            "Servidor personal Ubuntu (portafolio, API NestJS, bot de Telegram, Ollama, Wazuh, "
            "agentes autonomos que usan GitHub, Telegram, Anthropic, PyPI y npm). El trafico "
            "publico entra por Cloudflare Tunnel. Esta es una conexion SALIENTE que no estaba en "
            "la linea base aprendida (proceso/container, ASN, puerto, protocolo). "
            "'host:unknown' significa que no se pudo identificar el proceso del host."
        ),
    }


async def evaluate(client: Optional[AsyncTypeSafeClient], finding: dict, known: int, ecfg: dict) -> dict:
    if client is None:
        return fallback_decision(finding, ecfg)
    try:
        response = await client.system_one(
            state=typesafe_state(finding, known),
            questions={
                "is_suspicious": Noul(
                    instructions=(
                        "Esta conexion saliente nueva es sospechosa (exfiltracion, command and "
                        "control, minero, proceso comprometido) en vez de trafico legitimo del servicio."
                    )
                ),
                "severity": Score(
                    instructions="Que tan grave seria para el servidor si esta conexion fuera maliciosa.",
                    criteria=SEVERITY_LEVELS,
                ),
                "action": Choice(
                    instructions="Que deberia hacer la analista de seguridad con este destino nuevo.",
                    criteria=ACTION_CRITERIA,
                ),
            },
        )
    except Exception as exc:  # noqa: BLE001 - un fallo de TypeSafe no debe tumbar el ciclo
        log.error("Fallo la evaluacion TypeSafe del destino %s: %s", finding["key"], exc)
        return fallback_decision(finding, ecfg)
    severity_idx = max(0, min(len(SEVERITY_LEVELS) - 1, round(response.scores["severity"].score)))
    action = response.choices["action"].choice
    if action not in ACTION_CRITERIA:
        action = fallback_decision(finding, ecfg)["action"]
    return {
        "is_suspicious": response.nouls["is_suspicious"].noul,
        "severity": SEVERITY_LEVELS[severity_idx],
        "action": action,
        "source": "typesafe",
    }


# --- Ciclo ----------------------------------------------------------------------------------------


def _enrich(conns: list[dict], identities: dict[str, dict]) -> list[dict]:
    """Agrega identidad y llave a cada conexion; descarta las no resueltas
    y deja una sola conexion por llave."""
    seen, out = set(), []
    for conn in conns:
        ident = identities.get(conn["ip"])
        if ident is None:
            continue
        key = baseline_key(conn["source"], ident["asn"], conn["port"], conn["protocol"])
        if key in seen:
            continue
        seen.add(key)
        out.append({**conn, **ident, "key": key})
    return out


class EgressMonitor:
    """Estado vivo entre ciclos (mapa de Docker, cache de ASN y el stream de
    conntrack si esta activo)."""

    def __init__(self, ecfg: dict, persist: bool = True) -> None:
        self.ecfg = ecfg
        self.docker = DockerMap(ecfg)
        self.resolver = AsnResolver(ecfg, persist=persist)
        self.stream: Optional[ConntrackStream] = None

    async def start_stream(self) -> None:
        if self.stream is None:
            self.stream = ConntrackStream(self.ecfg, self.docker)
            self.stream.start()

    async def stop_stream(self) -> None:
        if self.stream is not None:
            await self.stream.stop()
            self.stream = None

    async def identify(self, conns: list[dict]) -> list[dict]:
        identities = await self.resolver.resolve_many({c["ip"] for c in conns})
        return _enrich(conns, identities)

    async def snapshot(self) -> list[dict]:
        return await self.identify(await asyncio.to_thread(take_snapshot, self.ecfg, self.docker))

    async def collect(self) -> list[dict]:
        """Flujos del stream; el snapshot solo si el stream estuvo caido desde
        el ciclo anterior (o es el primer ciclo: conexiones ya existentes)."""
        conns, need_snapshot = [], True
        if self.stream is not None:
            conns, need_snapshot = self.stream.drain()
        if need_snapshot:
            if self.stream is not None:
                log.debug("Egress: stream de conntrack sin cobertura completa, se agrega snapshot de respaldo")
            conns += await asyncio.to_thread(take_snapshot, self.ecfg, self.docker)
        return await self.identify(conns)

    async def run_cycle(self, client: Optional[AsyncTypeSafeClient]) -> list[dict]:
        """Un ciclo completo. Devuelve los destinos nuevos evaluados, cada uno
        con su `decision`, para que batwoman.py notifique y arme el digest."""
        conns = await self.collect()
        baseline = load_baseline()
        now = _now().isoformat()
        if not baseline.get("started_at"):
            baseline["started_at"] = now
            log.info("Egress: primer ciclo, linea base en aprendizaje por %s dia(s)", self.ecfg["learning_days"])
        learning, _ = learning_status(baseline, self.ecfg)
        allowlist = load_org_allowlist()
        first_seen = source_first_seen(baseline)
        keys = baseline["keys"]

        def add(conn: dict, origin: str, **extra) -> None:
            keys[conn["key"]] = {
                "source": conn["source"], "kind": conn["kind"], "asn": conn["asn"], "org": conn["org"],
                "port": conn["port"], "protocol": conn["protocol"], "first_seen": now, "last_seen": now, "count": 1, "origin": origin, **extra,
            }

        new = []
        for conn in conns:
            entry = keys.get(conn["key"])
            if entry:
                entry["last_seen"] = now
                entry["count"] = int(entry.get("count", 0)) + 1
            elif learning:
                add(conn, "learning")
            elif match_org(conn, allowlist):
                add(conn, "allowlist")
                log.info("Egress: %s en allowlist, a la linea base", describe(conn))
            else:
                new.append(conn)

        limit = int(self.ecfg["max_evaluations_per_cycle"])
        if len(new) > limit:
            log.info("Egress: %d destino(s) nuevo(s), se evaluan %d este ciclo", len(new), limit)
        known_per_source = Counter(e["source"] for e in keys.values())
        evaluated = []
        for conn in new[:limit]:
            finding = {**conn, "source_first_seen": first_seen.get(conn["source"])}
            decision = await evaluate(client, finding, known_per_source[conn["source"]], self.ecfg)
            log.info(
                "Egress nuevo: %s %s (%s): sospechoso=%.2f severidad=%s accion=%s [%s]",
                describe(conn), conn["ip"], conn["rdns"] or "sin PTR",
                decision["is_suspicious"], decision["severity"], decision["action"], decision["source"],
            )
            add(conn, "evaluated", action=decision["action"], severity=decision["severity"])
            evaluated.append({**finding, "source_age": _human_age(finding["source_first_seen"]), "decision": decision})

        _write_json(BASELINE_PATH, baseline)
        self.resolver.save()
        return evaluated


# --- Presentacion ------------------------------------------------------------------------------------


def _code(value) -> str:
    return f"<code>{html.escape(mask_secrets(str(value)))}</code>"


def describe(item: dict) -> str:
    return f"{item['source']} -> {item['org']} [AS{item['asn']}] :{item['port']}/{item.get('protocol', 'tcp')}"


def build_alert_message(item: dict) -> str:
    d = item["decision"]
    lines = [
        "🦇 Batwoman — <b>conexion saliente nueva</b>",
        f"Origen: {_code(item['source'])} ({'container' if item['kind'] == 'container' else 'proceso del host'})",
        f"Destino: {_code(item['org'])} [{_code('AS' + item['asn'])}] puerto {_code(str(item['port']) + '/' + item['protocol'])}",
        f"IP: {_code(item['ip'])}" + (f" ({_code(item['rdns'])})" if item["rdns"] else ""),
        f"Origen en la linea base: {_code(item['source_age'])}",
        f"Severidad: {_code(d['severity'])} | Sospechosa: {_code(round(d['is_suspicious'], 2))} [{html.escape(d['source'])}]",
    ]
    return "\n".join(lines)


def digest_record(item: dict) -> dict:
    d = item["decision"]
    return {
        "source": item["source"], "asn": item["asn"], "org": item["org"], "port": item["port"],
        "protocol": item["protocol"], "ip": item["ip"], "rdns": item["rdns"], "action": d["action"], "severity": d["severity"],
    }


def add_to_digest(new_list: list, item: dict) -> None:
    if len(new_list) < DIGEST_MAX_NEW:
        new_list.append(digest_record(item))


def render_digest_section(new_items: list[dict], since: str, ecfg: dict) -> tuple[list[str], list[str]]:
    """Seccion Egress del digest: (lineas HTML, lineas Markdown)."""
    baseline = load_baseline()
    learning, days_left = learning_status(baseline, ecfg)
    since_dt = _parse_ts(since) or (_now() - timedelta(days=1))
    per_source: dict[str, set] = {}
    for entry in baseline["keys"].values():
        ts = _parse_ts(entry.get("last_seen"))
        if ts and ts >= since_dt:
            per_source.setdefault(entry["source"], set()).add((entry["asn"], entry["port"], entry["protocol"]))
    top = sorted(per_source.items(), key=lambda kv: -len(kv[1]))[:5]

    if not baseline.get("started_at"):
        status_html = status_md = "sin datos todavia (el monitoreo no ha corrido)"
    elif learning:
        status_md = (
            f"aprendiendo: faltan {days_left:.1f} de {ecfg['learning_days']} dia(s); "
            f"{len(baseline['keys'])} llave(s) en la linea base"
        )
        status_html = "🎓 " + html.escape(status_md)
    else:
        status_md = f"{len(new_items)} destino(s) nuevo(s) desde el ultimo digest; {len(baseline['keys'])} llave(s) en la linea base"
        status_html = html.escape(status_md)

    msg = [f"<b>Egress:</b> {status_html}"]
    md = ["## Egress (conexiones salientes)", "", f"- Estado: {status_md}", ""]
    if new_items and not learning:
        msg += [
            f"• {_code(describe(i))} — {html.escape(i['action'])}/{html.escape(i['severity'])}"
            for i in new_items[:10]
        ]
        if len(new_items) > 10:
            msg.append(f"… y {len(new_items) - 10} mas (ver reporte)")
        md += [
            "### Destinos nuevos",
            "| Origen | Organizacion | ASN | Puerto | IP (rDNS) | Accion | Severidad |",
            "|---|---|---|---|---|---|---|",
            *[
                f"| {i['source']} | {i['org'].replace('|', '/')} | AS{i['asn']} | {i['port']}/{i.get('protocol', 'tcp')} | "
                f"{i['ip']} ({i['rdns'] or '-'}) | {i['action']} | {i['severity']} |"
                for i in new_items
            ],
            "",
        ]
    msg.append("<b>Top procesos por destinos distintos:</b>")
    msg += [f"• {_code(src)} — {len(dests)}" for src, dests in top] or ["• ninguno"]
    md += [
        "### Top 5 procesos/containers por destinos distintos (ASN+puerto+protocolo)",
        "| Origen | Destinos |",
        "|---|---|",
        *[f"| {src} | {len(dests)} |" for src, dests in top],
        "",
    ]
    return msg, md


def _status(conn: dict, baseline: dict, allowlist: list[dict]) -> str:
    if match_org(conn, allowlist):
        return "allowlisted"
    return "known" if conn["key"] in baseline["keys"] else "new"


async def print_snapshot(ecfg: dict, listen_seconds: int = SNAPSHOT_LISTEN_SECONDS) -> None:
    """--egress-snapshot: escucha el stream de conntrack `listen_seconds`
    segundos, imprime esos eventos y el snapshot actual de ss/conntrack,
    clasificados (new/known/allowlisted), sin alertar ni guardar nada (ni
    linea base ni cache de ASN)."""
    print("Lineas requeridas en /etc/sudoers.d/batifamilia (reemplazan las de ss/conntrack anteriores):")
    for line in SUDOERS_LINES:
        print(f"  {line}")
    monitor = EgressMonitor(ecfg, persist=False)
    print(f"\n=== Escuchando eventos de conntrack durante {listen_seconds}s ... ===", flush=True)
    await monitor.start_stream()
    try:
        await asyncio.sleep(listen_seconds)
        # Ultima oportunidad para atribuir los flujos que llegaron al final.
        await asyncio.sleep(monitor.stream.delay + monitor.stream.min_interval + ATTRIBUTION_TICK)
        events = list(monitor.stream.recent)
        stream_ok = monitor.stream.running
        pending = len(monitor.stream.pending)
    finally:
        await monitor.stop_stream()

    baseline = load_baseline()
    learning, days_left = learning_status(baseline, ecfg)
    allowlist = load_org_allowlist()
    state = f"aprendiendo, faltan {days_left:.1f} dia(s)" if learning else "linea base activa"

    identities = await monitor.resolver.resolve_many({ev["ip"] for ev in events})
    grouped: dict[str, dict] = {}
    for ev in events:
        group = grouped.setdefault(f"{ev['source']}|{ev['ip']}|{ev['port']}|{ev['protocol']}", {**ev, "n": 0, "first": ev["ts"]})
        group["n"] += 1
        group["first"] = min(group["first"], ev["ts"])
    print(
        f"=== Stream: {len(events)} flujo(s) saliente(s) publico(s) en {listen_seconds}s "
        f"({len(grouped)} distinto(s)); stream {'activo' if stream_ok else 'CAIDO (ver log / sudoers)'}"
        + (f"; {pending} sin atribuir al cierre" if pending else "") + " ==="
    )
    counts: Counter = Counter()
    for group in sorted(grouped.values(), key=lambda g: g["first"]):
        when = datetime.fromtimestamp(group["first"]).strftime("%H:%M:%S")
        ident = identities.get(group["ip"])
        if ident is None:
            print(mask_secrets(f"{when} [sin ASN    ] {group['source']} -> {group['ip']}:{group['port']}/{group['protocol']} x{group['n']}"))
            counts["sin ASN"] += 1
            continue
        conn = {**group, **ident, "key": baseline_key(group["source"], ident["asn"], group["port"], group["protocol"])}
        status = _status(conn, baseline, allowlist)
        counts[status] += 1
        rdns = f" ({conn['rdns']})" if conn["rdns"] else ""
        print(mask_secrets(f"{when} [{status:<11}] {describe(conn)}  {conn['ip']}{rdns} x{group['n']}"))
    print(f"=== Resumen stream: {dict(counts)} ===\n")

    conns = await monitor.snapshot()
    print(f"=== Snapshot ss/conntrack: {len(conns)} destino(s) publico(s), {len(baseline['keys'])} llave(s) en la linea base ({state}) ===")
    counts = Counter()
    for conn in sorted(conns, key=lambda c: (c["source"], c["org"], c["port"], c["protocol"])):
        status = _status(conn, baseline, allowlist)
        counts[status] += 1
        rdns = f" ({conn['rdns']})" if conn["rdns"] else ""
        print(mask_secrets(f"[{status:<11}] {describe(conn)}  {conn['ip']}{rdns}"))
    print(f"=== Resumen snapshot: {dict(counts)} ===")
