"""Monitoreo de conexiones salientes (egress) de Batwoman.

Lo llama el ciclo principal de batwoman.py cada `batwoman.egress.interval`
segundos (60 por defecto). Cada ciclo:

  1. Snapshot de conexiones TCP establecidas hacia IPs publicas:
     - Host: `sudo -n ss -tnpH state established` (proceso, local, remoto).
       Se descartan las conexiones entrantes (puerto local en escucha segun
       `ss -tlnH`).
     - Containers: `sudo -n conntrack -L -p tcp -o extended` (direccion
       original: src/dst/dport). La IP de origen se mapea al nombre del
       container con `docker network inspect` (cache de 10 min).
     Se omiten loopback, RFC1918, link-local, CGNAT y las subredes de Docker.
  2. Identidad del destino: ASN y organizacion via DNS de Team Cymru (TXT a
     <ip-invertida>.origin.asn.cymru.com / origin6 para IPv6 y
     AS<n>.asn.cymru.com), mas reverse DNS (solo para mostrar). Cache
     persistente en batcave/memory/egress_asn_cache.json (TTL 7 dias).
  3. Linea base (batcave/memory/egress_baseline.json) por la llave
     (proceso o container, ASN destino, puerto destino); nunca por IP.
     Durante `learning_days` desde el primer ciclo solo se aprende. Despues,
     una llave nunca vista es un "destino nuevo": si la organizacion esta en
     `egress_orgs` del allowlist entra directo a la linea base; si no, se
     evalua con TypeSafe (o el fallback) y luego se agrega a la linea base
     (cada llave alerta una sola vez).

La notificacion (Telegram / comms.json / dedup) la hace batwoman.py con lo
que devuelve `run_cycle`.
"""

from __future__ import annotations

import asyncio
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
from collections import Counter
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

log = logging.getLogger("batwoman.egress")

DEFAULTS = {
    "enabled": True,
    "interval": 60,
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
HOST_CMD = ["sudo", "-n", "ss", "-tnpH", "state", "established"]
CONNTRACK_CMD = ["sudo", "-n", "conntrack", "-L", "-p", "tcp", "-o", "extended"]
LISTEN_CMD = ["ss", "-tlnH"]

SEVERITY_LEVELS = ["info", "low", "medium", "high", "critical"]
ACTION_CRITERIA = {
    "notify_now": "Hay que avisar a Alfred de inmediato: posible exfiltracion, C2, minero o proceso comprometido.",
    "digest": "Es raro pero plausible; basta con revisarlo en el digest diario.",
    "learn": "Es trafico legitimo esperable para ese proceso/container; aprenderlo sin avisar.",
}
UNROUTED_ASN = "0"
DIGEST_MAX_NEW = 50
SS_PROCESS = re.compile(r'users:\(\("([^"]+)"')

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


class DockerMap:
    """IP de container -> nombre, y subredes de Docker. Se refresca cada
    `docker_refresh` segundos (o antes, si aparece una IP desconocida dentro
    de una subred de Docker, como mucho una vez por minuto)."""

    def __init__(self, ecfg: dict) -> None:
        self.refresh_every = int(ecfg["docker_refresh"])
        self.timeout = int(ecfg["command_timeout"])
        self.by_ip: dict[str, str] = {}
        self.subnets: list = []
        self.loaded_at = 0.0

    def refresh(self, force: bool = False) -> None:
        age = time.monotonic() - self.loaded_at
        if self.loaded_at and age < self.refresh_every and not (force and age > 60):
            return
        self.loaded_at = time.monotonic()
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


# --- Snapshot ------------------------------------------------------------------------


def listening_ports(timeout: int) -> set[int]:
    out = _run("ss -tlnH", LISTEN_CMD, timeout) or ""
    ports = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 4:
            _, port = _split_hostport(parts[3])
            if port is not None:
                ports.add(port)
    return ports


def host_connections(ecfg: dict, docker: DockerMap) -> list[dict]:
    timeout = int(ecfg["command_timeout"])
    out = _run("ss (sudo)", HOST_CMD, timeout)
    if out is None:
        return []
    listening = listening_ports(timeout)
    conns = []
    for line in out.splitlines():
        # Con `state established` no hay columna de estado:
        # Recv-Q Send-Q Local:Port Peer:Port [users:(("proc",pid=N,fd=M))]
        parts = line.split(None, 4)
        if len(parts) < 4:
            continue
        local_ip, local_port = _split_hostport(parts[2])
        remote_ip, remote_port = _split_hostport(parts[3])
        if remote_ip is None or remote_port is None or local_port in listening:
            continue
        if not is_public(remote_ip, docker.subnets):
            continue
        match = SS_PROCESS.search(parts[4]) if len(parts) > 4 else None
        conns.append(
            {
                "source": match.group(1) if match else "desconocido",
                "kind": "host",
                "ip": str(remote_ip),
                "port": remote_port,
                "local": f"{local_ip}:{local_port}",
            }
        )
    return conns


def container_connections(ecfg: dict, docker: DockerMap) -> list[dict]:
    out = _run("conntrack (sudo)", CONNTRACK_CMD, int(ecfg["command_timeout"]))
    if out is None:
        return []
    conns = []
    for line in out.splitlines():
        tokens = line.split()
        if "ESTABLISHED" not in tokens:
            continue
        original: dict[str, str] = {}
        for token in tokens:
            key, sep, value = token.partition("=")
            # La primera aparicion de cada campo es la direccion original.
            if sep and key in ("src", "dst", "dport") and key not in original:
                original[key] = value
        src = _parse_ip(original.get("src", ""))
        dst = _parse_ip(original.get("dst", ""))
        try:
            dport = int(original.get("dport", ""))
        except ValueError:
            continue
        if src is None or dst is None or not docker.in_docker(src):
            continue
        if not is_public(dst, docker.subnets):
            continue
        name = docker.by_ip.get(str(src))
        if name is None:
            docker.refresh(force=True)
            name = docker.by_ip.get(str(src))
        if name is None:
            log.debug("Egress: IP de container sin nombre %s, se reintenta el proximo ciclo", src)
            continue
        conns.append({"source": f"docker:{name}", "kind": "container", "ip": str(dst), "port": dport, "local": str(src)})
    return conns


def take_snapshot(ecfg: dict, docker: DockerMap) -> list[dict]:
    docker.refresh()
    return host_connections(ecfg, docker) + container_connections(ecfg, docker)


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


def load_org_allowlist() -> list[dict]:
    """`egress_orgs` de batwoman_allowlist.yaml: substring (sin distinguir
    mayusculas) del nombre de organizacion del ASN. Se relee cada ciclo."""
    try:
        with open(ALLOWLIST_PATH, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
    except (OSError, yaml.YAMLError) as exc:
        log.error("No se pudo leer %s: %s", ALLOWLIST_PATH, exc)
        return []
    entries = []
    for entry in (raw.get("egress_orgs") if isinstance(raw, dict) else None) or []:
        if isinstance(entry, str):
            entry = {"org": entry}
        if not isinstance(entry, dict) or not str(entry.get("org") or "").strip():
            log.error("Entrada invalida en egress_orgs: %r", entry)
            continue
        entries.append({"org": str(entry["org"]).strip().lower(), "reason": entry.get("reason", "sin razon")})
    return entries


def match_org(org: str, allowlist: list[dict]) -> Optional[str]:
    org = org.lower()
    for entry in allowlist:
        if entry["org"] in org:
            return entry["reason"]
    return None


def baseline_key(source: str, asn: str, port: int) -> str:
    return f"{source}|AS{asn}|{port}"


def load_baseline() -> dict:
    data = _read_json(BASELINE_PATH, {})
    if not isinstance(data, dict):
        data = {}
    data.setdefault("keys", {})
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
        "antiguedad_del_origen_en_linea_base": _human_age(finding["source_first_seen"]),
        "destinos_conocidos_del_origen": known_destinations,
        "context": (
            "Servidor personal Ubuntu (portafolio, API NestJS, bot de Telegram, Ollama, Wazuh, "
            "agentes autonomos que usan GitHub, Telegram, Anthropic, PyPI y npm). El trafico "
            "publico entra por Cloudflare Tunnel. Esta es una conexion SALIENTE que no estaba en "
            "la linea base aprendida (proceso/container, ASN, puerto)."
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
        key = baseline_key(conn["source"], ident["asn"], conn["port"])
        if key in seen:
            continue
        seen.add(key)
        out.append({**conn, **ident, "key": key})
    return out


class EgressMonitor:
    """Estado vivo entre ciclos (mapa de Docker y cache de ASN)."""

    def __init__(self, ecfg: dict, persist: bool = True) -> None:
        self.ecfg = ecfg
        self.docker = DockerMap(ecfg)
        self.resolver = AsnResolver(ecfg, persist=persist)

    async def snapshot(self) -> list[dict]:
        conns = await asyncio.to_thread(take_snapshot, self.ecfg, self.docker)
        identities = await self.resolver.resolve_many({c["ip"] for c in conns})
        return _enrich(conns, identities)

    async def run_cycle(self, client: Optional[AsyncTypeSafeClient]) -> list[dict]:
        """Un ciclo completo. Devuelve los destinos nuevos evaluados, cada uno
        con su `decision`, para que batwoman.py notifique y arme el digest."""
        conns = await self.snapshot()
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
                "port": conn["port"], "first_seen": now, "last_seen": now, "count": 1, "origin": origin, **extra,
            }

        new = []
        for conn in conns:
            entry = keys.get(conn["key"])
            if entry:
                entry["last_seen"] = now
                entry["count"] = int(entry.get("count", 0)) + 1
            elif learning:
                add(conn, "learning")
            elif match_org(conn["org"], allowlist):
                add(conn, "allowlist")
                log.info("Egress: %s -> %s [AS%s] :%s en allowlist, a la linea base", conn["source"], conn["org"], conn["asn"], conn["port"])
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
                "Egress nuevo: %s -> %s [AS%s] %s:%s (%s): sospechoso=%.2f severidad=%s accion=%s [%s]",
                conn["source"], conn["org"], conn["asn"], conn["ip"], conn["port"], conn["rdns"] or "sin PTR",
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
    return f"{item['source']} -> {item['org']} [AS{item['asn']}] :{item['port']}"


def build_alert_message(item: dict) -> str:
    d = item["decision"]
    lines = [
        "🦇 Batwoman — <b>conexion saliente nueva</b>",
        f"Origen: {_code(item['source'])} ({'container' if item['kind'] == 'container' else 'proceso del host'})",
        f"Destino: {_code(item['org'])} [{_code('AS' + item['asn'])}] puerto {_code(item['port'])}",
        f"IP: {_code(item['ip'])}" + (f" ({_code(item['rdns'])})" if item["rdns"] else ""),
        f"Origen en la linea base: {_code(item['source_age'])}",
        f"Severidad: {_code(d['severity'])} | Sospechosa: {_code(round(d['is_suspicious'], 2))} [{html.escape(d['source'])}]",
    ]
    return "\n".join(lines)


def digest_record(item: dict) -> dict:
    d = item["decision"]
    return {
        "source": item["source"], "asn": item["asn"], "org": item["org"], "port": item["port"],
        "ip": item["ip"], "rdns": item["rdns"], "action": d["action"], "severity": d["severity"],
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
            per_source.setdefault(entry["source"], set()).add((entry["asn"], entry["port"]))
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
                f"| {i['source']} | {i['org'].replace('|', '/')} | AS{i['asn']} | {i['port']} | "
                f"{i['ip']} ({i['rdns'] or '-'}) | {i['action']} | {i['severity']} |"
                for i in new_items
            ],
            "",
        ]
    msg.append("<b>Top procesos por destinos distintos:</b>")
    msg += [f"• {_code(src)} — {len(dests)}" for src, dests in top] or ["• ninguno"]
    md += [
        "### Top 5 procesos/containers por destinos distintos (ASN+puerto)",
        "| Origen | Destinos |",
        "|---|---|",
        *[f"| {src} | {len(dests)} |" for src, dests in top],
        "",
    ]
    return msg, md


async def print_snapshot(ecfg: dict) -> None:
    """--egress-snapshot: clasifica las conexiones actuales sin alertar ni
    guardar nada (ni linea base ni cache de ASN)."""
    monitor = EgressMonitor(ecfg, persist=False)
    conns = await monitor.snapshot()
    baseline = load_baseline()
    learning, days_left = learning_status(baseline, ecfg)
    allowlist = load_org_allowlist()
    state = f"aprendiendo, faltan {days_left:.1f} dia(s)" if learning else "linea base activa"
    print(f"=== Egress snapshot: {len(conns)} destino(s) publico(s), {len(baseline['keys'])} llave(s) en la linea base ({state}) ===")
    counts: Counter = Counter()
    for conn in sorted(conns, key=lambda c: (c["source"], c["org"], c["port"])):
        if match_org(conn["org"], allowlist):
            status = "allowlisted"
        elif conn["key"] in baseline["keys"]:
            status = "known"
        else:
            status = "new"
        counts[status] += 1
        rdns = f" ({conn['rdns']})" if conn["rdns"] else ""
        print(mask_secrets(f"[{status:<11}] {describe(conn)}  {conn['ip']}{rdns}"))
    print(f"=== Resumen: {dict(counts)} ===")
