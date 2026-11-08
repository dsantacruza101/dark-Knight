#!/usr/bin/env python3
"""Julia Pennyworth: analista de inteligencia de amenazas de la Batifamilia.

Corre una vez al dia (julia.service + julia.timer, 07:50 America/El_Salvador)
y cruza lo que hay instalado en el servidor con lo que se sabe que se esta
explotando. Es de SOLO LECTURA: nunca actualiza, instala, reinicia ni
rebootea nada y nunca usa sudo; solo dice que correr.

1. Inventario (local, sin red): kernel corriendo (uname -r) vs el
   linux-image mas nuevo instalado; /var/run/reboot-required(.pkgs) y hace
   cuantos dias esta pendiente (mtime); paquetes instalados (dpkg-query);
   paquetes actualizables (apt list --upgradable) clasificados por origen
   (ubuntu-security, ubuntu-updates o third-party); Node del host
   (`julia.host_node`) y de cada container corriendo (docker exec <c> node
   -v, si no tiene node se omite); fecha de creacion de la imagen de cada
   container; versiones de cloudflared, nginx, openssh-server y docker;
   sysctl net.mptcp.enabled.

2. Feeds (timeout `julia.http_timeout`, cache en batcave/memory/julia_cache/;
   cada feed se baja como mucho una vez por corrida y solo si el cache tiene
   mas de `feed_cache_hours`; si la red falla se usa el cache viejo): CISA
   KEV, avisos de seguridad de Ubuntu (USN) para `julia.release` (paginas
   de 20, las `notices_pages` mas recientes), el estado
   por release de cada CVE de KEV que mapea a algo instalado
   (ubuntu.com/security/cves/<CVE>.json, tope `max_cve_lookups` por
   corrida) y las releases de Node.js (dist/index.json + schedule.json del
   repo nodejs/Release para las fechas de fin de vida).

3. Correlacion (cada hallazgo con severidad):
   - KEV cuyo producto esta instalado y la version corriendo es menor que la
     corregida en Ubuntu (o upstream) -> CRITICAL, alerta inmediata. Si el
     parche esta instalado pero no corriendo (kernel sin reiniciar) se dice
     explicitamente. Sin parche en Ubuntu -> HIGH (digest).
   - Reinicio pendiente con linux-image en .pkgs hace mas de
     `reboot_alert_days` -> HIGH, alerta inmediata; si no, digest.
   - USN cuya version corregida es mas nueva que la instalada -> HIGH.
   - Node (host o container) bajo la ultima release `security: true` de su
     linea, o en una linea fuera de soporte -> HIGH. Containers con imagen
     de mas de `image_max_age_days` -> LOW: `build --pull` si el servicio
     tiene `build:` en el compose, si no `docker compose pull`. Imagenes
     fijadas a una version exacta (wazuh 4.14.8) no se miden por edad: se
     avisa si Docker Hub tiene un parche mas nuevo del mismo minor.
   - Paquetes de terceros con actualizaciones (docker, containerd,
     cloudflared) -> MEDIUM. net.mptcp.enabled=1 -> LOW (hardening).

4. Salida: un mensaje a Alfred "🕵️ Julia — informe de inteligencia" (KEV,
   reinicio, parches, runtimes, hardening; cada item con que correr para
   arreglarlo), reporte completo en reports/julia-YYYY-MM-DD.md y los
   hallazgos CRITICAL/HIGH para Batman en batcave/comms.json
   (type threat_intel). Las alertas inmediatas y los mensajes de comms.json
   se deduplican en batcave/memory/julia_state.json: se repiten cada
   `alert_cooldown_hours` (24) mientras sigan abiertos.

Todo lo que se loguea, se envia por Telegram o se escribe a disco pasa por
batcave.secrets.mask_secrets.

Flags:
  --dry-run       inventario + feeds + correlacion; imprime el informe y las
                  alertas que se mandarian. Sin Telegram, estado, comms,
                  reporte ni memoria (el cache de feeds si se actualiza).
  --egress-check  resuelve los hosts de los feeds (ASN via Team Cymru) y dice
                  si egress_orgs de batwoman_allowlist.yaml cubre a
                  host:python3 en 443/tcp. No guarda nada.
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import logging
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo

import yaml
from dotenv import load_dotenv
from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import TelegramError

from batcave import memory_store
from batcave.secrets import mask_secrets

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = Path("/etc/night-agent.env")
CONFIG_PATH = BASE_DIR / "config.yaml"
LOG_PATH = BASE_DIR / "julia.log"
COMMS_PATH = BASE_DIR / "batcave" / "comms.json"
STATE_PATH = BASE_DIR / "batcave" / "memory" / "julia_state.json"
CACHE_DIR = BASE_DIR / "batcave" / "memory" / "julia_cache"
REPORTS_DIR = BASE_DIR / "reports"
REBOOT_REQUIRED = Path("/var/run/reboot-required")
MPTCP_SYSCTL = Path("/proc/sys/net/mptcp/enabled")
NODE_MISSING = 127  # docker exec: el ejecutable no existe en el container
# Tag fijado a una version exacta: `4.14.8`, `v1.2.3`, `1.27.3-alpine`.
PINNED_TAG = re.compile(r"^(?P<v>v?)(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)(?P<suffix>-[A-Za-z0-9._-]+)?$")
DEFAULT_COMPOSE_FILES = {"docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml",
                         "docker-compose.override.yml", "docker-compose.override.yaml",
                         "compose.override.yml", "compose.override.yaml"}

LOCAL_TZ = ZoneInfo("America/El_Salvador")

DEFAULTS = {
    "release": "resolute",
    "http_timeout": 30,
    "feed_cache_hours": 20,
    "cve_cache_hours": 24,
    "cve_cache_old_days": 7,
    "max_cve_lookups": 60,
    "alert_cooldown_hours": 24,
    "reboot_alert_days": 3,
    "image_max_age_days": 14,
    "notices_pages": 3,
    "webhook_projects": ["my_Portfolio_React_v2", "portfolioServiceLauncher"],
    "kev_unknown_days": 90,
    "max_items_per_section": 8,
    "kernel_source": "linux",
    "host_node": "~/.local/share/node-current/bin/node",
    "third_party_packages": ["docker-ce", "docker-ce-cli", "containerd.io", "docker-buildx-plugin", "docker-compose-plugin", "cloudflared"],
    "feeds": {
        "kev": "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json",
        # La API acepta limit 1..20 (422 si no); se pagina con offset.
        "notices": "https://ubuntu.com/security/notices.json?release={release}&limit=20&offset={offset}",
        "cve": "https://ubuntu.com/security/cves/{cve}.json",
        "docker_tags": "https://hub.docker.com/v2/namespaces/{namespace}/repositories/{repo}/tags?page_size=100&name={prefix}",
        "node_index": "https://nodejs.org/dist/index.json",
        "node_schedule": "https://raw.githubusercontent.com/nodejs/Release/main/schedule.json",
    },
}

AGENT = "julia"
DISPLAY_NAME = "Julia Pennyworth"
ROLE = "Analista de inteligencia de amenazas (CISA KEV, avisos de Ubuntu, Node.js)"
USER_AGENT = "julia-pennyworth/1.0 (night-agent; +https://dsantacruz.dev)"
MAX_FEED_BYTES = 30 * 1024 * 1024
CMD_TIMEOUT = 30
TELEGRAM_LIMIT = 4000
# El timer corre cada 24h exactas: sin holgura, la corrida de manana caeria
# unos segundos antes del cooldown y la alerta abierta no se repetiria.
DEDUP_SLACK = timedelta(minutes=30)
STATE_RESOLVED_MAX = 50

SEVERITIES = ["LOW", "MEDIUM", "HIGH", "CRITICAL"]
SEV_ICON = {"CRITICAL": "🔴", "HIGH": "🟠", "MEDIUM": "🟡", "LOW": "🔵"}
COMMS_SEVERITIES = {"CRITICAL", "HIGH"}
SECTIONS = [
    ("kev", "🚨 KEV"),
    ("reboot", "🔄 Reinicio"),
    ("patches", "📦 Parches"),
    ("runtimes", "🟢 Runtimes"),
    ("hardening", "🛡️ Hardening"),
]

# Producto de KEV (vendorProject, product; regex sin distinguir mayusculas)
# -> componente instalado. `exclude` descarta productos del mismo vendor que
# no corren aqui (Docker Desktop, NGINX Plus...).
KEV_PRODUCTS = [
    {"component": "kernel", "vendor": r"^linux$", "product": r"kernel"},
    {"component": "docker", "vendor": r"docker|moby", "product": r"docker|engine|moby", "exclude": r"desktop"},
    {"component": "containerd", "vendor": r".", "product": r"^containerd$"},
    {"component": "openssh", "vendor": r".", "product": r"^openssh"},
    {"component": "nginx", "vendor": r"nginx|f5", "product": r"^nginx", "exclude": r"plus|controller|ingress|unit|instance manager|app protect"},
    {"component": "node", "vendor": r"node", "product": r"^node(\.?js)?$"},
    {"component": "python", "vendor": r"python", "product": r"^(c?python)$"},
    {"component": "openssl", "vendor": r"openssl", "product": r"^openssl$"},
    {"component": "sudo", "vendor": r".", "product": r"^sudo$"},
    {"component": "systemd", "vendor": r".", "product": r"^systemd"},
    {"component": "cloudflared", "vendor": r"cloudflare", "product": r".", "exclude": r"warp|wordpress|plugin|workers|pages"},
]

# Componente -> paquetes binarios de dpkg que lo representan.
COMPONENT_PACKAGES = {
    "openssh": r"^openssh-(server|client)$",
    "nginx": r"^nginx(-core|-full|-light|-extras)?$",
    "python": r"^python3\.\d+(-minimal)?$",
    "openssl": r"^(openssl|libssl3(t64)?)$",
    "sudo": r"^sudo(-ldap)?$",
    "systemd": r"^systemd$",
    "docker": r"^(docker\.io|docker-ce)$",
    "containerd": r"^(containerd|containerd\.io)$",
    "cloudflared": r"^cloudflared$",
}
# Paquetes de terceros: no hay version de Ubuntu propia, se usa como
# referencia la version upstream corregida del paquete equivalente de Ubuntu.
UPSTREAM_PROXIES = {
    "docker-ce": ["docker.io", "docker.io-app"],
    "containerd.io": ["containerd", "containerd-app"],
    "cloudflared": [],
}
NO_FIX_STATUSES = {"needed", "pending", "deferred", "ignored", "needs-triage"}
NOT_AFFECTED_STATUSES = {"not-affected", "DNE"}
KERNEL_IMAGE = re.compile(r"^linux-image-(?:unsigned-)?(?P<abi>\d+\.\d+\.\d+-\d+)-(?P<flavor>[a-z0-9-]+)$")
# Paquetes del kernel que no son la imagen corriendo (metapaquetes, headers,
# otras variantes): en los USN se evaluan solo via la imagen o el source.
KERNEL_RELATED = re.compile(r"^linux(-|$)")
KERNEL_KEEP = {"linux-firmware"}


class MaskSecretsFilter(logging.Filter):
    """Enmascara secretos en cada registro antes de que llegue a un handler."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = mask_secrets(record.getMessage())
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = mask_secrets(logging.Formatter().formatException(record.exc_info))
        return True


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.FileHandler(LOG_PATH), logging.StreamHandler(sys.stdout)],
)
for _handler in logging.getLogger().handlers:
    _handler.addFilter(MaskSecretsFilter())
log = logging.getLogger("julia")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


def load_env() -> None:
    if "TELEGRAM_BOT_TOKEN" in os.environ:
        return
    if ENV_PATH.exists():
        try:
            load_dotenv(ENV_PATH)
        except OSError as exc:
            log.warning("No se pudo leer %s (%s), se usan las variables de entorno actuales", ENV_PATH, exc)
    else:
        log.warning("No se encontro %s, se usan las variables de entorno actuales", ENV_PATH)


def load_config(path: Path = CONFIG_PATH) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        config = yaml.safe_load(fh) or {}
    raw = config.get("julia") or {}
    return {**DEFAULTS, **raw, "feeds": {**DEFAULTS["feeds"], **(raw.get("feeds") or {})}}


class TelegramNotifier:
    """Envia notificaciones al bot Alfred usando variables de entorno."""

    def __init__(self) -> None:
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            raise RuntimeError(
                "TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID deben estar definidos "
                "como variables de entorno del sistema"
            )
        self.chat_id = chat_id
        self.bot = Bot(token=token)

    async def send(self, text: str) -> None:
        try:
            await self.bot.send_message(
                chat_id=self.chat_id, text=mask_secrets(text), parse_mode=ParseMode.HTML
            )
        except TelegramError as exc:
            log.error("No se pudo enviar mensaje a Telegram: %s", exc)


# --- Persistencia -----------------------------------------------------------


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


def append_comms(entries: list[dict]) -> None:
    if not entries:
        return
    comms = _read_json(COMMS_PATH, [])
    comms.extend(entries)
    _write_json(COMMS_PATH, comms)


def update_memory(event: str, **kwargs) -> None:
    """Actualiza batcave/memory/julia.md (la crea con la plantilla si falta)."""
    try:
        memory_store.ensure_memory_file(AGENT, DISPLAY_NAME, ROLE)
        if event == "cycle_complete":
            memory_store.touch_last_activity(AGENT)
            memory_store.bump_stat(AGENT, "ciclos")
        elif event == "alert_sent":
            memory_store.append_error(
                AGENT,
                error=memory_store.table_cell(mask_secrets(kwargs["title"]))[:200],
                causa=memory_store.table_cell(mask_secrets(kwargs["detail"]))[:200],
                solucion="notificado a Alfred y Batman",
                resultado=f"severidad {kwargs['severity']}",
            )
            memory_store.bump_stat(AGENT, "escalaciones")
        elif event == "feed_error":
            memory_store.append_error(
                AGENT,
                error=memory_store.table_cell(f"feed {kwargs['feed']} no disponible"),
                causa=memory_store.table_cell(mask_secrets(kwargs["status"]))[:200],
                solucion="se uso el cache (si habia)",
                resultado="informe parcial",
            )
    except (OSError, ValueError) as exc:
        log.warning("No se pudo actualizar la memoria de Julia: %s", exc)


# --- Versiones -----------------------------------------------------------------


def _dpkg_order(c: str) -> int:
    if c == "~":
        return -1
    if c.isdigit():
        return 0
    if c.isascii() and c.isalpha():
        return ord(c)
    return ord(c) + 256


def _dpkg_cmp_fragment(a: str, b: str) -> int:
    i = j = 0
    while i < len(a) or j < len(b):
        while (i < len(a) and not a[i].isdigit()) or (j < len(b) and not b[j].isdigit()):
            ac = _dpkg_order(a[i]) if i < len(a) else 0
            bc = _dpkg_order(b[j]) if j < len(b) else 0
            if ac != bc:
                return -1 if ac < bc else 1
            i += 1
            j += 1
        si = i
        while i < len(a) and a[i].isdigit():
            i += 1
        sj = j
        while j < len(b) and b[j].isdigit():
            j += 1
        na, nb = int(a[si:i] or 0), int(b[sj:j] or 0)
        if na != nb:
            return -1 if na < nb else 1
    return 0


def split_debian_version(version: str) -> tuple[int, str, str]:
    """`1:2.3-4ubuntu1` -> (1, "2.3", "4ubuntu1")."""
    version = version.strip()
    epoch = 0
    if ":" in version:
        head, _, version = version.partition(":")
        epoch = int(head) if head.isdigit() else 0
    upstream, revision = version, ""
    if "-" in version:
        upstream, _, revision = version.rpartition("-")
    return epoch, upstream, revision


def dpkg_compare(a: str, b: str) -> int:
    """Compara versiones de Debian como `dpkg --compare-versions` (-1/0/1)."""
    ea, ua, ra = split_debian_version(a)
    eb, ub, rb = split_debian_version(b)
    if ea != eb:
        return -1 if ea < eb else 1
    return _dpkg_cmp_fragment(ua, ub) or _dpkg_cmp_fragment(ra, rb)


def upstream_core(version: str) -> str:
    """Version upstream sin epoch, revision ni sufijos de empaquetado
    (`5:28.0.1-1~ubuntu.26.04~resolute` -> `28.0.1`, `26.1.3+dfsg1-0ubuntu1` -> `26.1.3`)."""
    return re.split(r"[+~]", split_debian_version(version)[1])[0]


def parse_node_version(value: str) -> Optional[tuple[int, int, int]]:
    match = re.search(r"v?(\d+)\.(\d+)\.(\d+)", value or "")
    return tuple(int(x) for x in match.groups()) if match else None


def _fmt_node(version: tuple[int, int, int]) -> str:
    return "v" + ".".join(str(x) for x in version)


# --- Inventario (local, sin red) -------------------------------------------------------


def run_cmd(cmd: list[str], timeout: int = CMD_TIMEOUT, stderr_too: bool = False,
            expected_codes: tuple[int, ...] = ()) -> Optional[str]:
    """stdout (y stderr si `stderr_too`) de un comando de solo lectura, o None
    si no existe, falla o se pasa del timeout. Los codigos de `expected_codes`
    son fallos normales (p. ej. 127 = el container no tiene node) y van a DEBUG."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.info("No se pudo correr %s: %s", cmd[0], exc)
        return None
    if proc.returncode != 0:
        level = logging.DEBUG if proc.returncode in expected_codes else logging.INFO
        log.log(level, "%s termino con codigo %s: %s", " ".join(cmd[:3]), proc.returncode, proc.stderr.strip()[:200])
        return None
    return proc.stdout + (proc.stderr if stderr_too else "")


DPKG_FORMAT = "${db:Status-Abbrev}\t${Package}\t${Version}\t${source:Package}\t${source:Version}\n"


def parse_dpkg_query(out: str) -> dict[str, dict]:
    """Salida de dpkg-query con DPKG_FORMAT -> {paquete: {version, source,
    source_version}}; solo paquetes instalados (`ii`)."""
    packages = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 5 or not parts[0].startswith("ii"):
            continue
        _, name, version, source, source_version = parts[:5]
        packages[name] = {"version": version, "source": source or name, "source_version": source_version or version}
    return packages


def kernel_state(release: str, packages: dict[str, dict]) -> dict:
    """Kernel corriendo vs el linux-image mas nuevo instalado del mismo flavor."""
    match = re.match(r"^(?P<abi>\d+\.\d+\.\d+-\d+)-(?P<flavor>.+)$", release)
    flavor = match.group("flavor") if match else None
    running_pkg = next(
        (p for p in (f"linux-image-{release}", f"linux-image-unsigned-{release}") if p in packages), None
    )
    installed = []
    for name, info in packages.items():
        m = KERNEL_IMAGE.match(name)
        if m and (flavor is None or m.group("flavor") == flavor):
            installed.append((name, info["version"]))
    newest = None
    for name, version in installed:
        if newest is None or dpkg_compare(version, newest[1]) > 0:
            newest = (name, version)
    return {
        "running_release": release,
        "flavor": flavor,
        "running_package": running_pkg,
        "running_version": packages[running_pkg]["version"] if running_pkg else None,
        "newest_package": newest[0] if newest else None,
        "newest_version": newest[1] if newest else None,
        "installed": sorted(n for n, _ in installed),
    }


def reboot_state(path: Path = REBOOT_REQUIRED, now: Optional[float] = None) -> dict:
    if not path.exists():
        return {"required": False, "days": None, "since": None, "packages": []}
    mtime = path.stat().st_mtime
    pkgs_path = path.with_name(path.name + ".pkgs")
    try:
        packages = sorted({p.strip() for p in pkgs_path.read_text(encoding="utf-8").splitlines() if p.strip()})
    except OSError:
        packages = []
    return {
        "required": True,
        "days": round(((now or time.time()) - mtime) / 86400, 1),
        "since": datetime.fromtimestamp(mtime, LOCAL_TZ).isoformat(timespec="minutes"),
        "packages": packages,
    }


APT_LINE = re.compile(r"^(?P<name>[^/\s]+)/(?P<suites>\S+)\s+(?P<version>\S+)\s+(?P<arch>\S+)\s+\[upgradable from: (?P<old>[^\]]+)\]")


def parse_apt_upgradable(out: str) -> list[dict]:
    """`docker-ce/resolute 5:28.1.0-1 amd64 [upgradable from: 5:28.0.1-1]`."""
    items = []
    for line in out.splitlines():
        match = APT_LINE.match(line.strip())
        if match:
            items.append({
                "name": match.group("name"),
                "suites": match.group("suites").split(","),
                "version": match.group("version"),
                "old": match.group("old").strip(),
            })
    return items


def classify_suites(suites: list[str], release: str) -> Optional[str]:
    """ubuntu-security / ubuntu-updates por el pocket; None si no se puede
    saber solo por el nombre (pocket base o repo de terceros)."""
    if any(s == f"{release}-security" for s in suites):
        return "ubuntu-security"
    if any(s in (f"{release}-updates", f"{release}-proposed", f"{release}-backports") for s in suites):
        return "ubuntu-updates"
    return None


def parse_policy_origin(out: str) -> Optional[str]:
    """Host del repo del candidato en `apt-cache policy <pkg>`."""
    candidate = None
    lines = out.splitlines()
    for line in lines:
        if line.strip().startswith("Candidate:"):
            candidate = line.split(":", 1)[1].strip()
            break
    if not candidate:
        return None
    for i, line in enumerate(lines):
        if line.strip().lstrip("*").strip().split(" ")[0] == candidate and "Candidate" not in line:
            for nxt in lines[i + 1:]:
                match = re.search(r"\d+\s+(https?://\S+)", nxt)
                if match:
                    return urlparse(match.group(1)).hostname
                if not nxt.startswith("        "):
                    break
    return None


def classify_upgradable(items: list[dict], release: str, third_party: list[str],
                        policy: Callable[[str], Optional[str]]) -> list[dict]:
    for item in items:
        origin = classify_suites(item["suites"], release)
        host = None
        if origin is None:
            host = policy(item["name"])
            if item["name"] in third_party or (host and not re.search(r"(ubuntu|canonical)\.com$", host)):
                origin = "third-party"
            else:
                origin = "ubuntu-updates"
        item["origin"] = origin
        item["origin_host"] = host
    return items


def parse_docker_time(value: str) -> Optional[datetime]:
    """`2026-10-01T12:34:56.123456789Z` -> datetime aware (Go usa nanosegundos)."""
    match = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:\d{2})?$", (value or "").strip())
    if not match:
        return None
    base, frac, tz = match.groups()
    text = base + (f".{frac[:6]}" if frac else "") + ("+00:00" if tz in (None, "Z") else tz)
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def parse_sysctl_value(value: Optional[str]) -> Optional[str]:
    """`'1\\n'` / `'net.mptcp.enabled = 1'` -> `'1'`; vacio o raro -> None."""
    text = (value or "").strip()
    if "=" in text:
        text = text.split("=", 1)[1].strip()
    return text if re.fullmatch(r"-?\d+", text) else None


def read_mptcp(path: Path = MPTCP_SYSCTL) -> Optional[str]:
    """net.mptcp.enabled: de /proc/sys (sin sudo) o, si no se puede leer,
    de `sysctl -n`. None si el kernel no tiene MPTCP (no hay que reportar)."""
    try:
        return parse_sysctl_value(path.read_text(encoding="utf-8"))
    except OSError as exc:
        log.debug("No se pudo leer %s: %s", path, exc)
    return parse_sysctl_value(run_cmd(["sysctl", "-n", "net.mptcp.enabled"]))


def compose_service_has_build(files: Optional[str], service: Optional[str]) -> Optional[bool]:
    """Si `service` tiene `build:` en alguno de los archivos de compose del
    label `config_files` (separados por coma; los override cuentan). None si
    ningun archivo se pudo leer (no se sabe)."""
    if not files or not service:
        return None
    readable = False
    for name in files.split(","):
        try:
            data = yaml.safe_load(Path(name.strip()).read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            log.debug("No se pudo leer el compose %s: %s", name, exc)
            continue
        readable = True
        svc = ((data.get("services") if isinstance(data, dict) else None) or {}).get(service)
        if isinstance(svc, dict) and svc.get("build"):
            return True
    return False if readable else None


def collect_containers(now: datetime) -> list[dict]:
    names = (run_cmd(["docker", "ps", "--format", "{{.Names}}"]) or "").split()
    if not names:
        return []
    try:
        inspected = json.loads(run_cmd(["docker", "inspect", *names]) or "[]")
    except json.JSONDecodeError:
        inspected = []
    image_ids = sorted({c.get("Image") for c in inspected if c.get("Image")})
    try:
        images = json.loads(run_cmd(["docker", "image", "inspect", *image_ids]) or "[]") if image_ids else []
    except json.JSONDecodeError:
        images = []
    created = {img.get("Id"): parse_docker_time(img.get("Created", "")) for img in images}
    containers = []
    for c in inspected:
        labels = (c.get("Config") or {}).get("Labels") or {}
        when = created.get(c.get("Image"))
        containers.append({
            "name": (c.get("Name") or "").lstrip("/"),
            "image": (c.get("Config") or {}).get("Image", ""),
            "image_id": (c.get("Image") or "")[:19],
            "created": when.isoformat(timespec="minutes") if when else None,
            "age_days": round((now - when).total_seconds() / 86400, 1) if when else None,
            "compose_dir": labels.get("com.docker.compose.project.working_dir"),
            "compose_service": labels.get("com.docker.compose.service"),
            "compose_files": labels.get("com.docker.compose.project.config_files"),
            "has_build": compose_service_has_build(labels.get("com.docker.compose.project.config_files"),
                                                   labels.get("com.docker.compose.service")),
        })
    return containers


def collect_inventory(config: dict) -> dict:
    """Inventario local completo. Cada parte que falla queda en `errors` y no
    tumba el resto."""
    now = datetime.now(timezone.utc)
    errors = []
    release = (run_cmd(["uname", "-r"]) or "").strip()
    packages = parse_dpkg_query(run_cmd(["dpkg-query", "-W", "-f", DPKG_FORMAT], timeout=60) or "")
    if not packages:
        errors.append("dpkg-query no devolvio paquetes")

    upgradable_out = run_cmd(["apt", "list", "--upgradable"], timeout=60)
    if upgradable_out is None:
        errors.append("apt list --upgradable fallo")
    policy_cache: dict[str, Optional[str]] = {}

    def policy(name: str) -> Optional[str]:
        if name not in policy_cache:
            policy_cache[name] = parse_policy_origin(run_cmd(["apt-cache", "policy", name]) or "")
        return policy_cache[name]

    upgradable = classify_upgradable(
        parse_apt_upgradable(upgradable_out or ""), config["release"], config["third_party_packages"], policy
    )

    node = []
    host_node = os.path.expanduser(config["host_node"])
    host_version = (run_cmd([host_node, "-v"]) or "").strip()
    if host_version:
        node.append({"where": "host", "version": host_version, "binary": host_node})
    else:
        errors.append(f"no se pudo leer la version de {host_node}")
    containers = collect_containers(now)
    for c in containers:
        out = run_cmd(["docker", "exec", c["name"], "node", "-v"], timeout=15, expected_codes=(NODE_MISSING,))
        if out and parse_node_version(out):
            node.append({"where": f"docker:{c['name']}", "version": out.strip(), "container": c})

    cloudflared = re.search(r"version (\S+)", run_cmd(["cloudflared", "--version"]) or "")
    nginx = re.search(r"nginx/(\S+)", run_cmd(["nginx", "-v"], stderr_too=True) or "")
    docker = (run_cmd(["docker", "version", "--format", "{{.Server.Version}}"]) or "").strip()
    mptcp = read_mptcp()

    return {
        "collected_at": now.isoformat(timespec="seconds"),
        "kernel": kernel_state(release, packages),
        "reboot": reboot_state(),
        "packages": packages,
        "upgradable": upgradable,
        "node": node,
        "containers": containers,
        "versions": {
            "cloudflared": cloudflared.group(1) if cloudflared else (packages.get("cloudflared") or {}).get("version"),
            "nginx": nginx.group(1) if nginx else (packages.get("nginx") or {}).get("version"),
            "openssh-server": (packages.get("openssh-server") or {}).get("version"),
            "docker": docker or (packages.get("docker-ce") or packages.get("docker.io") or {}).get("version"),
        },
        "mptcp": mptcp,
        "errors": errors,
    }


# --- Feeds -----------------------------------------------------------------------------


class FeedNotFound(Exception):
    pass


def http_get_json(url: str, timeout: float):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(MAX_FEED_BYTES + 1)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise FeedNotFound(url) from exc
        raise
    if len(body) > MAX_FEED_BYTES:
        raise ValueError(f"respuesta de mas de {MAX_FEED_BYTES} bytes")
    return json.loads(body)


class FeedCache:
    """Cache en disco de los feeds. Cada nombre se resuelve una sola vez por
    corrida; solo va a la red si el cache tiene mas de `max_age`. Si la red
    falla se usa el cache viejo (y se avisa en `status`)."""

    def __init__(self, cache_dir: Path, timeout: float, max_cve_lookups: int,
                 fetch: Callable[[str, float], object] = http_get_json) -> None:
        self.dir = cache_dir
        self.timeout = timeout
        self.fetch = fetch
        self.max_cve_lookups = max_cve_lookups
        self.cve_lookups = 0
        self.resolved: dict[str, object] = {}
        self.status: dict[str, str] = {}

    def _path(self, name: str) -> Path:
        return self.dir / (re.sub(r"[^A-Za-z0-9._-]", "_", name) + ".json")

    def get(self, name: str, url: str, max_age: timedelta, is_cve: bool = False):
        if name in self.resolved:
            return self.resolved[name]
        path = self._path(name)
        cached = _read_json(path, None)
        fetched_at = None
        if isinstance(cached, dict):
            try:
                fetched_at = datetime.fromisoformat(cached["fetched_at"])
            except (KeyError, TypeError, ValueError):
                cached = None
        else:
            cached = None
        now = datetime.now(timezone.utc)
        if cached and now - fetched_at < max_age:
            data, status = cached.get("data"), "cache"
        elif is_cve and self.cve_lookups >= self.max_cve_lookups:
            data = cached.get("data") if cached else None
            status = "cache viejo (tope de consultas)" if cached else "sin consultar (tope de consultas por corrida)"
        else:
            if is_cve:
                self.cve_lookups += 1
            try:
                data, status = self.fetch(url, self.timeout), "red"
                _write_json(path, {"fetched_at": now.isoformat(), "url": url, "data": data})
            except FeedNotFound:
                data, status = None, "404"
                _write_json(path, {"fetched_at": now.isoformat(), "url": url, "data": None})
            except (urllib.error.URLError, OSError, ValueError) as exc:
                if cached:
                    data, status = cached.get("data"), f"cache viejo ({exc})"
                else:
                    data, status = None, f"error: {exc}"
                log.warning("Feed %s no disponible (%s): %s", name, url, exc)
        self.resolved[name] = data
        self.status[name] = status
        return data


def feed_hosts(config: dict) -> list[str]:
    return sorted({urlparse(u).hostname for u in config["feeds"].values() if urlparse(u).hostname})


# --- Correlacion ------------------------------------------------------------------------


def finding(key: str, severity: str, section: str, title: str, detail: str, fix: str,
            immediate: bool = False, packages: Optional[list[str]] = None) -> dict:
    return {"key": key, "severity": severity, "section": section, "title": title,
            "detail": detail, "fix": fix, "immediate": immediate, "packages": sorted(packages or [])}


def kev_component(entry: dict) -> Optional[str]:
    vendor = str(entry.get("vendorProject") or "")
    product = str(entry.get("product") or "")
    for rule in KEV_PRODUCTS:
        if not re.search(rule["vendor"], vendor, re.I) or not re.search(rule["product"], product, re.I):
            continue
        if rule.get("exclude") and re.search(rule["exclude"], product, re.I):
            continue
        return rule["component"]
    return None


def upgrade_cmd(packages: list[str]) -> str:
    return "sudo apt update && sudo apt install --only-upgrade " + " ".join(sorted(set(packages)))


def compose_file_flags(files: Optional[str]) -> str:
    """`-f` solo si los archivos no son los que compose descubre solo
    (docker-compose.yml + override)."""
    names = [Path(f.strip()).name for f in (files or "").split(",") if f.strip()]
    if not names or all(n in DEFAULT_COMPOSE_FILES for n in names):
        return ""
    return "".join(f" -f {n}" for n in names)


def compose_update_cmd(container: dict, config: dict) -> str:
    """Comando para traer la imagen nueva de un container: `build --pull` si
    el servicio tiene `build:` en el compose, `pull` si usa una imagen
    publicada. El webhook solo se menciona en los proyectos que se despliegan
    por webhook (`webhook_projects`)."""
    if not (container.get("compose_dir") and container.get("compose_service")):
        return f"docker pull {container.get('image')} y recrear {container.get('name')}"
    flag = compose_file_flags(container.get("compose_files"))
    svc, has_build = container["compose_service"], container.get("has_build")
    if has_build is False:
        cmd = f"cd {container['compose_dir']} && docker compose{flag} pull {svc} && docker compose{flag} up -d {svc}"
    else:
        cmd = f"cd {container['compose_dir']} && docker compose{flag} build --pull {svc} && docker compose{flag} up -d {svc}"
        if has_build is None:
            cmd += "  (no se pudo leer el compose: si el servicio no tiene build:, usar pull)"
    if Path(container["compose_dir"]).name in (config.get("webhook_projects") or []):
        cmd += "  (en produccion: redeploy via webhook)"
    return cmd


def component_targets(inv: dict, config: dict) -> dict[str, list[dict]]:
    """Por componente de KEV, lo que hay instalado y como compararlo."""
    targets: dict[str, list[dict]] = {}
    kernel = inv["kernel"]
    if kernel.get("running_version"):
        targets["kernel"] = [{
            "id": "kernel", "label": f"kernel {kernel['running_release']}", "kind": "ubuntu",
            "sources": [config["kernel_source"]], "version": kernel["running_version"],
            "newest": kernel.get("newest_version"),
            "fix": "sudo apt update && sudo apt full-upgrade && sudo reboot",
        }]
    packages = inv["packages"]
    for component, pattern in COMPONENT_PACKAGES.items():
        by_source: dict[str, list[str]] = {}
        upstream = []
        for name, info in packages.items():
            if not re.match(pattern, name):
                continue
            if name in UPSTREAM_PROXIES:
                upstream.append(name)
            else:
                by_source.setdefault(info["source"], []).append(name)
        for source, binaries in sorted(by_source.items()):
            targets.setdefault(component, []).append({
                "id": source, "label": f"{source} ({', '.join(sorted(binaries))})", "kind": "ubuntu",
                "sources": [source], "version": packages[binaries[0]]["source_version"], "newest": None,
                "fix": upgrade_cmd(binaries),
            })
        for name in upstream:
            targets.setdefault(component, []).append({
                "id": name, "label": f"{name} (tercero)", "kind": "upstream",
                "sources": UPSTREAM_PROXIES[name], "version": packages[name]["version"], "newest": None,
                "fix": upgrade_cmd([name] + (["docker-ce-cli", "containerd.io"] if name == "docker-ce" else [])),
            })
    for rt in inv["node"]:
        container = rt.get("container")
        targets.setdefault("node", []).append({
            "id": f"node:{rt['where']}", "label": f"Node {rt['where']}", "kind": "upstream", "same_major": True,
            "sources": ["nodejs"], "version": rt["version"].lstrip("v"), "newest": None,
            "fix": compose_update_cmd(container, config) if container else "actualizar Node del host y el symlink ~/.local/share/node-current",
        })
    return targets


def ubuntu_statuses(cve_json: Optional[dict], release: str, sources: list[str]) -> list[dict]:
    """Estados de `ubuntu.com/security/cves/<CVE>.json` para el release y los
    paquetes source indicados: [{source, status, version}]."""
    result = []
    for pkg in (cve_json or {}).get("packages") or []:
        if pkg.get("name") not in sources:
            continue
        for st in pkg.get("statuses") or []:
            if st.get("release_codename") == release:
                result.append({"source": pkg["name"], "status": st.get("status", ""), "version": (st.get("description") or "").strip()})
    return result


def assess_target(target: dict, statuses: list[dict]) -> tuple[str, Optional[str]]:
    """(veredicto, version corregida): fixed, vulnerable, pending_reboot,
    no_fix, not_affected o unknown."""
    if not statuses:
        return "unknown", None
    released = [s["version"] for s in statuses if s["status"] == "released" and s["version"]]
    if released:
        fixed = released[0]
        for v in released[1:]:
            if dpkg_compare(v, fixed) > 0:
                fixed = v
        if target["kind"] == "upstream":
            have, want = upstream_core(target["version"]), upstream_core(fixed)
            if target.get("same_major") and have.split(".")[0] != want.split(".")[0]:
                return "unknown", fixed
            return ("fixed" if _dpkg_cmp_fragment(have, want) >= 0 else "vulnerable"), fixed
        if dpkg_compare(target["version"], fixed) >= 0:
            return "fixed", fixed
        if target.get("newest") and dpkg_compare(target["newest"], fixed) >= 0:
            return "pending_reboot", fixed
        return "vulnerable", fixed
    if all(s["status"] in NOT_AFFECTED_STATUSES for s in statuses):
        return "not_affected", None
    if any(s["status"] in NO_FIX_STATUSES for s in statuses):
        return "no_fix", None
    return "unknown", None


def kev_findings(kev: Optional[dict], targets: dict[str, list[dict]], cve_lookup: Callable[[str, bool], Optional[dict]],
                 config: dict, today: date) -> tuple[list[dict], dict]:
    """Cruza KEV con lo instalado. `cve_lookup(cve, old)` devuelve el JSON de
    Ubuntu del CVE (o None). Devuelve (hallazgos, estadisticas)."""
    findings: list[dict] = []
    stats = {"kev_total": 0, "matched": 0, "fixed": 0, "not_affected": 0, "unknown": 0}
    entries = (kev or {}).get("vulnerabilities") or []
    stats["kev_total"] = len(entries)
    matched = [(e, kev_component(e)) for e in entries]
    matched = [(e, c) for e, c in matched if c and c in targets]
    matched.sort(key=lambda ec: ec[0].get("dateAdded", ""), reverse=True)
    stats["matched"] = len(matched)
    for entry, component in matched:
        cve = entry.get("cveID", "")
        try:
            added = date.fromisoformat(entry.get("dateAdded", ""))
        except ValueError:
            added = None
        recent = bool(added and (today - added).days <= int(config["kev_unknown_days"]))
        old = bool(added and (today - added).days > 365)
        cve_json = cve_lookup(cve, old)
        name = entry.get("vulnerabilityName") or f"{entry.get('vendorProject')} {entry.get('product')}"
        for target in targets[component]:
            verdict, fixed = assess_target(target, ubuntu_statuses(cve_json, config["release"], target["sources"]))
            key = f"kev:{cve}:{target['id']}"
            ref = "Ubuntu" if target["kind"] == "ubuntu" else f"referencia Ubuntu {'/'.join(target['sources'])}"
            if verdict == "vulnerable":
                findings.append(finding(
                    key, "CRITICAL", "kev", f"{cve} — {name}",
                    f"{target['label']}: corre {target['version']} < {fixed} ({ref} {config['release']}). "
                    f"Explotada activamente (CISA KEV desde {entry.get('dateAdded')}).",
                    target["fix"], immediate=True,
                ))
            elif verdict == "pending_reboot":
                findings.append(finding(
                    key, "CRITICAL", "kev", f"{cve} — {name}",
                    f"El parche ESTA instalado ({target['newest']} >= {fixed}) pero NO esta corriendo: "
                    f"el kernel activo es {target['label']} ({target['version']}). Falta reiniciar.",
                    "sudo reboot  (en una ventana de mantenimiento)", immediate=True,
                ))
            elif verdict == "no_fix":
                findings.append(finding(
                    key, "HIGH", "kev", f"{cve} — {name}",
                    f"{target['label']} {target['version']}: Ubuntu {config['release']} aun no tiene parche. "
                    f"Mitigacion CISA: {entry.get('requiredAction', 'ver aviso')}",
                    f"vigilar https://ubuntu.com/security/{cve} y aplicar la mitigacion del vendor",
                ))
            elif verdict == "unknown" and recent:
                findings.append(finding(
                    key, "MEDIUM", "kev", f"{cve} — {name}",
                    f"{target['label']} {target['version']}: sin version corregida conocida para {config['release']} "
                    f"(KEV reciente, {entry.get('dateAdded')}). Revisar a mano.",
                    f"revisar https://ubuntu.com/security/{cve} y el aviso del vendor",
                ))
            elif verdict in stats:
                stats[verdict] += 1
            else:
                stats["unknown"] += 1
    return findings, stats


def reboot_findings(inv: dict, config: dict) -> list[dict]:
    rb, kernel = inv["reboot"], inv["kernel"]
    if rb["required"]:
        kernel_pkgs = [p for p in rb["packages"] if p.startswith("linux-image")]
        days = rb["days"] or 0
        pkgs = ", ".join(rb["packages"][:6]) + (" ..." if len(rb["packages"]) > 6 else "")
        if kernel_pkgs and days > config["reboot_alert_days"]:
            return [finding(
                "reboot:required", "HIGH", "reboot", f"Reinicio pendiente hace {days:g} dias con kernel nuevo",
                f"/var/run/reboot-required desde {rb['since']}; paquetes: {pkgs}. "
                f"Corre {kernel['running_release']}, instalado {kernel.get('newest_package') or kernel_pkgs[0]}.",
                "sudo reboot  (en una ventana de mantenimiento)", immediate=True,
            )]
        return [finding(
            "reboot:required", "MEDIUM", "reboot", f"Reinicio pendiente hace {days:g} dias",
            f"/var/run/reboot-required desde {rb['since']}; paquetes: {pkgs or 'sin .pkgs'}.",
            "sudo reboot  (en una ventana de mantenimiento)",
        )]
    if kernel.get("newest_version") and kernel.get("running_version") and dpkg_compare(kernel["newest_version"], kernel["running_version"]) > 0:
        return [finding(
            "reboot:kernel", "MEDIUM", "reboot", "Kernel nuevo instalado sin reiniciar",
            f"Corre {kernel['running_release']} ({kernel['running_version']}), instalado {kernel['newest_package']} ({kernel['newest_version']}).",
            "sudo reboot  (en una ventana de mantenimiento)",
        )]
    return []


def notice_packages(notice: dict, release: str) -> list[dict]:
    rp = notice.get("release_packages")
    if isinstance(rp, dict):
        return list(rp.get(release) or [])
    return [p for p in (notice.get("packages") or []) if isinstance(p, dict) and p.get("release", p.get("release_codename")) == release]


def notice_findings(notices: Optional[dict], inv: dict, config: dict) -> list[dict]:
    """USN para el release cuyo fix es mas nuevo que lo instalado -> HIGH."""
    findings = []
    packages, kernel, release = inv["packages"], inv["kernel"], config["release"]
    by_source: dict[str, list[str]] = {}
    for name, info in packages.items():
        by_source.setdefault(info["source"], []).append(name)
    for notice in (notices or {}).get("notices") or []:
        outdated, upgrade, kernel_note = [], set(), None
        for pkg in notice_packages(notice, release):
            name, fixed = pkg.get("name", ""), pkg.get("version", "")
            if not name or not fixed:
                continue
            kmatch = KERNEL_IMAGE.match(name)
            is_kernel = (pkg.get("is_source") and name == config["kernel_source"]) or (
                kmatch and kmatch.group("flavor") == kernel.get("flavor"))
            if is_kernel:
                running = kernel.get("running_version")
                if running and dpkg_compare(running, fixed) < 0:
                    newest = kernel.get("newest_version")
                    if newest and dpkg_compare(newest, fixed) >= 0:
                        kernel_note = f"kernel {kernel['running_release']} {running} < {fixed} (instalado {newest}: falta reiniciar)"
                    else:
                        kernel_note = f"kernel {kernel['running_release']} {running} < {fixed}"
                continue
            if KERNEL_RELATED.match(name) and name not in KERNEL_KEEP:
                continue
            if pkg.get("is_source"):
                for binary in by_source.get(name, []):
                    have = packages[binary]["source_version"]
                    if dpkg_compare(have, fixed) < 0:
                        outdated.append(f"{binary} {packages[binary]['version']} < {fixed}")
                        upgrade.add(binary)
            elif name in packages and dpkg_compare(packages[name]["version"], fixed) < 0:
                outdated.append(f"{name} {packages[name]['version']} < {fixed}")
                upgrade.add(name)
        if not outdated and not kernel_note:
            continue
        usn = notice.get("id", "USN-?")
        cves = notice.get("cves_ids") or [c.get("id") if isinstance(c, dict) else c for c in notice.get("cves") or []]
        detail = "; ".join(([kernel_note] if kernel_note else []) + sorted(set(outdated))[:8])
        if cves:
            detail += f". CVEs: {', '.join(str(c) for c in cves[:5])}" + (" ..." if len(cves) > 5 else "")
        if kernel_note and not upgrade:
            fix = "sudo reboot" if "falta reiniciar" in kernel_note else "sudo apt update && sudo apt full-upgrade && sudo reboot"
        else:
            fix = upgrade_cmd(list(upgrade)) + (" && sudo apt full-upgrade && sudo reboot" if kernel_note else "")
        findings.append(finding(f"usn:{usn}", "HIGH", "patches", f"{usn} — {notice.get('title', '')}", detail, fix,
                                packages=list(upgrade)))
    return findings


def upgradable_findings(inv: dict, covered: set[str]) -> list[dict]:
    findings = []
    third = [u for u in inv["upgradable"] if u["origin"] == "third-party"]
    if third:
        findings.append(finding(
            "apt:third-party", "MEDIUM", "patches", f"{len(third)} paquete(s) de terceros con actualizacion",
            "; ".join(f"{u['name']} {u['old']} -> {u['version']}" + (f" ({u['origin_host']})" if u.get("origin_host") else "") for u in third[:8]),
            upgrade_cmd([u["name"] for u in third]) + ("  (actualizar docker reinicia los containers)" if any(u["name"].startswith(("docker", "containerd")) for u in third) else ""),
        ))
    security = [u for u in inv["upgradable"] if u["origin"] == "ubuntu-security" and u["name"] not in covered]
    if security:
        findings.append(finding(
            "apt:ubuntu-security", "MEDIUM", "patches", f"{len(security)} parche(s) de seguridad de Ubuntu pendientes (fuera de los ultimos USN)",
            ", ".join(f"{u['name']} {u['version']}" for u in security[:10]) + (" ..." if len(security) > 10 else ""),
            upgrade_cmd([u["name"] for u in security]),
        ))
    return findings


def _add_months(d: date, months: int) -> date:
    month = d.month - 1 + months
    return date(d.year + month // 12, month % 12 + 1, min(d.day, 28))


def node_line_eol(major: int, schedule: Optional[dict], index: list[dict]) -> Optional[date]:
    """Fin de vida de la linea: schedule.json de nodejs/Release; si no esta,
    estimado desde la primera release de la linea (par: 36 meses, impar: 8)."""
    entry = (schedule or {}).get(f"v{major}") or {}
    try:
        if entry.get("end"):
            return date.fromisoformat(entry["end"])
    except ValueError:
        pass
    dates = [r.get("date", "") for r in index if (parse_node_version(r.get("version", "")) or (None,))[0] == major]
    dates = [d for d in dates if d]
    if not dates:
        return None
    try:
        first = date.fromisoformat(min(dates))
    except ValueError:
        return None
    return _add_months(first, 36 if major % 2 == 0 else 8)


def node_findings(inv: dict, index: Optional[list], schedule: Optional[dict], config: dict, today: date) -> tuple[list[dict], list[str]]:
    """Runtimes de Node bajo la ultima release de seguridad de su linea o en
    una linea EOL -> HIGH; imagenes viejas -> LOW. Devuelve (hallazgos, OK)."""
    findings, ok = [], []
    releases = []
    for r in index or []:
        v = parse_node_version(r.get("version", ""))
        if v:
            releases.append({"v": v, "security": bool(r.get("security")), "lts": r.get("lts")})
    supported_lts = sorted(
        {r["v"][0] for r in releases if r["lts"]}
        - {m for m in {r["v"][0] for r in releases} if (node_line_eol(m, schedule, index or []) or date.max) < today},
        reverse=True,
    )
    for rt in inv["node"]:
        v = parse_node_version(rt["version"])
        container = rt.get("container")
        fix_cmd = compose_update_cmd(container, config) if container else None
        if not v:
            continue
        line = [r for r in releases if r["v"][0] == v[0]]
        if not line:
            ok.append(f"{rt['where']} {rt['version']} (sin datos de la linea v{v[0]} en el feed)")
            continue
        latest = max(r["v"] for r in line)
        sec = max((r["v"] for r in line if r["security"]), default=None)
        eol = node_line_eol(v[0], schedule, index or [])
        key = f"node:{rt['where']}"
        if eol and eol < today:
            target = max((r["v"] for r in releases if r["v"][0] == supported_lts[0]), default=None) if supported_lts else None
            target_s = _fmt_node(target) if target else "una linea LTS soportada"
            findings.append(finding(
                key, "HIGH", "runtimes", f"Node {rt['where']} en linea fuera de soporte",
                f"{rt['version']}: la linea v{v[0]} llego a fin de vida el {eol.isoformat()}; ya no recibe parches de seguridad.",
                (fix_cmd + f"  (subir la imagen base a node:{target[0]})" if fix_cmd and target else
                 f"migrar el host a Node {target_s} y apuntar ~/.local/share/node-current a esa version"),
            ))
        elif sec and v < sec:
            findings.append(finding(
                key, "HIGH", "runtimes", f"Node {rt['where']} sin el ultimo parche de seguridad",
                f"{rt['version']} < {_fmt_node(sec)} (ultima release de seguridad de v{v[0]}; la mas nueva es {_fmt_node(latest)}).",
                (fix_cmd + f"  (imagen base node:{v[0]} >= {_fmt_node(sec)})" if fix_cmd else
                 f"instalar Node {_fmt_node(latest)} en el host y apuntar ~/.local/share/node-current a esa version"),
            ))
        else:
            ok.append(f"{rt['where']} {rt['version']} (al dia con la ultima de seguridad {_fmt_node(sec) if sec else '-'}"
                      + (f", EOL {eol.isoformat()}" if eol else "") + ")")
    return findings, ok


def split_image_ref(image: str) -> tuple[Optional[str], Optional[str], str]:
    """`wazuh/wazuh-manager:4.14.8` -> ('wazuh', 'wazuh-manager', '4.14.8');
    `postgres:16` -> ('library', 'postgres', '16'). Namespace/repo None si la
    imagen no es de Docker Hub (`ghcr.io/...`) o no se puede partir."""
    ref = image.split("@", 1)[0]
    name, _, tag = ref.rpartition(":") if ":" in ref.rsplit("/", 1)[-1] else (ref, "", "latest")
    parts = name.split("/")
    if len(parts) > 1 and ("." in parts[0] or ":" in parts[0] or parts[0] == "localhost"):
        if parts[0] not in ("docker.io", "index.docker.io"):
            return None, None, tag
        parts = parts[1:]
    if len(parts) == 1:
        parts = ["library", parts[0]]
    if len(parts) != 2 or not all(parts):
        return None, None, tag
    return parts[0], parts[1], tag


def newer_patch_tag(tag: str, available: list[str]) -> Optional[str]:
    """Parche mas nuevo del mismo major.minor (y mismo sufijo) que `tag`."""
    cur = PINNED_TAG.match(tag)
    if not cur:
        return None
    best, best_patch = None, int(cur.group("patch"))
    for name in available:
        m = PINNED_TAG.match(name)
        if not m or (m.group("v"), m.group("major"), m.group("minor"), m.group("suffix")) != (
                cur.group("v"), cur.group("major"), cur.group("minor"), cur.group("suffix")):
            continue
        if int(m.group("patch")) > best_patch:
            best, best_patch = name, int(m.group("patch"))
    return best


def image_compose_files(container: dict) -> str:
    """Archivos de compose del container donde aparece su `image:` (si no se
    pueden leer o no aparece, todos los del label)."""
    names = [f.strip() for f in (container.get("compose_files") or "").split(",") if f.strip()]
    found = []
    for name in names:
        try:
            if container.get("image") and container["image"] in Path(name).read_text(encoding="utf-8"):
                found.append(name)
        except OSError:
            pass
    return ", ".join(Path(n).name for n in (found or names))


def image_findings(inv: dict, config: dict,
                   tag_lookup: Callable[[str, str, str], Optional[list[str]]]) -> tuple[list[dict], list[str]]:
    """Imagenes de containers. Fijadas a una version exacta (`4.14.8`): la
    edad no dice nada (el tag no cambia), se avisa si hay un parche mas nuevo
    del mismo minor en Docker Hub. Tags mutables (`16`, `alpine3.21`,
    `community`, `latest`, imagenes construidas): edad > `image_max_age_days`
    -> LOW con el comando para traer la imagen nueva."""
    findings, ok = [], []
    for c in inv["containers"]:
        namespace, repo, tag = split_image_ref(c.get("image") or "")
        pinned = PINNED_TAG.match(tag) if c.get("has_build") is not True else None
        if pinned:
            prefix = f"{pinned.group('v')}{pinned.group('major')}.{pinned.group('minor')}."
            available = tag_lookup(namespace, repo, prefix) if namespace else None
            newer = newer_patch_tag(tag, available or [])
            if newer:
                files = image_compose_files(c) or "el compose"
                findings.append(finding(
                    f"image-tag:{c['name']}", "LOW", "runtimes", f"{c['image']} tiene un parche mas nuevo: {newer}",
                    f"{c['name']} esta fijada a {tag}; Docker Hub ya publica {newer} (mismo {prefix[:-1]}).",
                    f"subir el tag a {newer} en {files} y luego: {compose_update_cmd(c, config)}",
                ))
            elif available is None:
                ok.append(f"{c['name']} {c['image']} (fijada; no se pudo consultar Docker Hub)")
            else:
                ok.append(f"{c['name']} {c['image']} (fijada, ultimo parche de {prefix[:-1]})")
            continue
        if c.get("age_days") is not None and c["age_days"] > config["image_max_age_days"]:
            action = "reconstruir" if c.get("has_build") is True else "bajar la imagen de nuevo"
            findings.append(finding(
                f"image:{c['name']}", "LOW", "runtimes", f"Imagen de {c['name']} con {c['age_days']:g} dias",
                f"{c['image']} creada {c['created']}: {action} trae los parches de la imagen base.",
                compose_update_cmd(c, config),
            ))
    return findings, ok


def hardening_findings(inv: dict) -> list[dict]:
    if inv.get("mptcp") == "1":
        return [finding(
            "sysctl:mptcp", "LOW", "hardening", "MPTCP habilitado (net.mptcp.enabled=1)",
            "Ningun servicio lo usa; deshabilitarlo reduce superficie de ataque del kernel.",
            "echo 'net.mptcp.enabled = 0' | sudo tee /etc/sysctl.d/99-no-mptcp.conf && sudo sysctl --system",
        )]
    return []


NOTICES_PAGE_SIZE = 20  # maximo que acepta ubuntu.com/security/notices.json


def fetch_notices(feeds: FeedCache, config: dict, max_age: timedelta) -> Optional[dict]:
    """Los USN mas recientes de `release`: hasta `notices_pages` paginas de 20
    (offset 0, 20, 40...), cada una cacheada aparte. Para antes si una pagina
    falla, viene incompleta o se llego a `total_results`. None si ni la
    primera pagina se pudo leer."""
    release, notices, seen = config["release"], [], set()
    for page in range(max(1, int(config["notices_pages"]))):
        offset = page * NOTICES_PAGE_SIZE
        url = config["feeds"]["notices"].format(release=release, offset=offset)
        data = feeds.get(f"notices-{release}-{offset}", url, max_age)
        if not isinstance(data, dict):
            if page == 0:
                return None
            break
        batch = [n for n in data.get("notices") or [] if isinstance(n, dict)]
        for n in batch:
            if n.get("id") not in seen:
                seen.add(n.get("id"))
                notices.append(n)
        total = data.get("total_results")
        if len(batch) < NOTICES_PAGE_SIZE or (isinstance(total, int) and offset + len(batch) >= total):
            break
    return {"notices": notices}


def correlate(inv: dict, feeds: FeedCache, config: dict, today: date) -> tuple[list[dict], list[str], dict]:
    """Baja los feeds (via cache) y arma todos los hallazgos ordenados por severidad."""
    max_age = timedelta(hours=float(config["feed_cache_hours"]))
    urls = config["feeds"]
    kev = feeds.get("kev", urls["kev"], max_age)
    notices = fetch_notices(feeds, config, max_age)
    index = feeds.get("node-index", urls["node_index"], max_age)
    schedule = feeds.get("node-schedule", urls["node_schedule"], max_age) if urls.get("node_schedule") else None

    def cve_lookup(cve: str, old: bool) -> Optional[dict]:
        age = timedelta(days=float(config["cve_cache_old_days"])) if old else timedelta(hours=float(config["cve_cache_hours"]))
        data = feeds.get(f"cves/{cve}", urls["cve"].format(cve=cve), age, is_cve=True)
        return data if isinstance(data, dict) else None

    def tag_lookup(namespace: str, repo: str, prefix: str) -> Optional[list[str]]:
        url = urls["docker_tags"].format(namespace=quote(namespace), repo=quote(repo), prefix=quote(prefix))
        data = feeds.get(f"dockerhub/{namespace}/{repo}/{prefix}", url, max_age)
        if not isinstance(data, dict):
            return None
        return [r["name"] for r in data.get("results") or [] if isinstance(r, dict) and r.get("name")]

    findings, stats = kev_findings(kev if isinstance(kev, dict) else None, component_targets(inv, config), cve_lookup, config, today)
    findings += reboot_findings(inv, config)
    usn = notice_findings(notices if isinstance(notices, dict) else None, inv, config)
    findings += usn
    findings += upgradable_findings(inv, {p for f in usn for p in f["packages"]})
    nf, ok = node_findings(inv, index if isinstance(index, list) else None, schedule if isinstance(schedule, dict) else None, config, today)
    findings += nf
    imf, image_ok = image_findings(inv, config, tag_lookup)
    findings += imf
    ok += image_ok
    findings += hardening_findings(inv)
    findings.sort(key=lambda f: (-SEVERITIES.index(f["severity"]), [s for s, _ in SECTIONS].index(f["section"]), f["key"]))
    stats["cve_lookups"] = feeds.cve_lookups
    return findings, ok, stats


# --- Deduplicacion ------------------------------------------------------------------------


def plan_notifications(findings: list[dict], state: dict, now: datetime, cooldown: timedelta) -> tuple[list[dict], list[dict], dict, list[dict]]:
    """Decide que alertas inmediatas y que mensajes de comms.json salen hoy.

    Cada hallazgo CRITICAL/HIGH (o inmediato) abierto vive en state["open"];
    se re-avisa si paso `cooldown` (menos DEDUP_SLACK) desde el ultimo aviso.
    Los que ya no aparecen pasan a `resolved`. Devuelve (alertas, comms,
    estado nuevo, resueltos)."""
    old_open = state.get("open") or {}
    new_open, alerts, comms = {}, [], []

    def due(ts: Optional[str]) -> bool:
        if not ts:
            return True
        try:
            return now - datetime.fromisoformat(ts) >= cooldown - DEDUP_SLACK
        except ValueError:
            return True

    for f in findings:
        if not (f["immediate"] or f["severity"] in COMMS_SEVERITIES):
            continue
        prev = old_open.get(f["key"]) or {}
        entry = {
            "first_seen": prev.get("first_seen") or now.isoformat(),
            "severity": f["severity"], "title": f["title"],
            "last_alert": prev.get("last_alert"), "last_comms": prev.get("last_comms"),
        }
        if f["immediate"] and due(entry["last_alert"]):
            alerts.append(f)
            entry["last_alert"] = now.isoformat()
        if f["severity"] in COMMS_SEVERITIES and due(entry["last_comms"]):
            comms.append(f)
            entry["last_comms"] = now.isoformat()
        new_open[f["key"]] = entry
    resolved = [{"key": k, "title": v.get("title", k), "severity": v.get("severity"), "resolved_at": now.isoformat()}
                for k, v in old_open.items() if k not in new_open]
    history = (resolved + list(state.get("resolved") or []))[:STATE_RESOLVED_MAX]
    return alerts, comms, {"open": new_open, "resolved": history, "last_run": now.isoformat()}, resolved


def comms_entry(f: dict, now: datetime) -> dict:
    return {
        "timestamp": now.astimezone(LOCAL_TZ).replace(tzinfo=None).isoformat(timespec="seconds"),
        "from": "julia",
        "to": "batman",
        "type": "threat_intel",
        "service": f["key"],
        "priority": f["severity"].lower(),
        "message": mask_secrets(f"{f['title']}: {f['detail']}"),
        "fix": mask_secrets(f["fix"]),
        "read": False,
    }


# --- Salida ---------------------------------------------------------------------------------


def _item_html(f: dict) -> str:
    return (f"{SEV_ICON[f['severity']]} <b>{f['severity']}</b> {html.escape(f['title'])}\n"
            f"   {html.escape(f['detail'])}\n"
            f"   ➜ <code>{html.escape(f['fix'])}</code>")


def build_report_message(findings: list[dict], ok: list[str], resolved: list[dict], day: str,
                         feed_status: dict, max_items: int) -> str:
    """Mensaje de Telegram (HTML) por secciones; recorta items por seccion
    hasta que entre en el limite de Telegram."""
    counts = {s: sum(1 for f in findings if f["severity"] == s) for s in reversed(SEVERITIES)}
    feed_problems = {k: v for k, v in feed_status.items() if not k.startswith("cves/") and v not in ("red", "cache")}
    for limit in range(max(1, max_items), 0, -1):
        lines = [f"🕵️ <b>Julia — informe de inteligencia</b> ({html.escape(day)})",
                 " · ".join(f"{SEV_ICON[s]} {n} {s}" for s, n in counts.items())]
        for section, title in SECTIONS:
            items = [f for f in findings if f["section"] == section]
            lines.append(f"\n<b>{title}</b>")
            if section == "runtimes":
                lines += [f"✅ {html.escape(o)}" for o in ok[:limit]]
            if not items and not (section == "runtimes" and ok):
                lines.append("Sin hallazgos.")
            lines += [_item_html(f) for f in items[:limit]]
            if len(items) > limit:
                lines.append(f"… y {len(items) - limit} mas en el reporte")
        if resolved:
            lines.append("\n<b>✅ Resueltos desde la ultima corrida</b>")
            lines += [f"• {html.escape(r['title'])}" for r in resolved[:limit]]
        if feed_problems:
            lines.append("\n⚠️ Feeds: " + html.escape(", ".join(f"{k}: {v}" for k, v in feed_problems.items())))
        lines.append(f"\n📄 <code>reports/julia-{html.escape(day)}.md</code>")
        text = "\n".join(lines)
        if len(text) <= TELEGRAM_LIMIT:
            return text
    return text[: TELEGRAM_LIMIT - 40] + "\n… (recortado, ver reporte)"


def build_alert_message(alerts: list[dict]) -> str:
    lines = ["🚨 <b>Julia — alerta de inteligencia</b>"]
    lines += [_item_html(f) for f in alerts]
    text = "\n\n".join(lines)
    return text if len(text) <= TELEGRAM_LIMIT else text[: TELEGRAM_LIMIT - 40] + "\n… (recortado, ver reporte)"


def _md(text) -> str:
    return mask_secrets(str(text)).replace("|", "\\|").replace("\n", " ")


def render_report(inv: dict, findings: list[dict], ok: list[str], resolved: list[dict], stats: dict,
                  feed_status: dict, day: str) -> str:
    k, rb = inv["kernel"], inv["reboot"]
    out = [f"# Julia Pennyworth — informe de inteligencia {day}", "",
           f"Inventario tomado: {inv['collected_at']}", ""]
    for section, title in SECTIONS:
        items = [f for f in findings if f["section"] == section]
        out.append(f"## {title}")
        if section == "runtimes" and ok:
            out += [f"- OK: {_md(o)}" for o in ok]
        if not items and not (section == "runtimes" and ok):
            out.append("Sin hallazgos.")
        for f in items:
            out += [f"- **{f['severity']}** {_md(f['title'])}", f"  - {_md(f['detail'])}", f"  - Arreglo: `{f['fix']}`"]
        out.append("")
    if resolved:
        out.append("## Resueltos desde la ultima corrida")
        out += [f"- {_md(r['title'])} ({r.get('severity')})" for r in resolved]
        out.append("")
    out += ["## Inventario", "",
            "| Elemento | Valor |", "|---|---|",
            f"| Kernel corriendo | {_md(k['running_release'])} ({_md(k.get('running_version') or '?')}) |",
            f"| linux-image mas nuevo | {_md(k.get('newest_package') or '?')} ({_md(k.get('newest_version') or '?')}) |",
            f"| Reinicio pendiente | {'si, ' + str(rb['days']) + ' dias (' + _md(', '.join(rb['packages'])) + ')' if rb['required'] else 'no'} |",
            f"| Paquetes instalados | {len(inv['packages'])} |",
            f"| net.mptcp.enabled | {_md(inv.get('mptcp'))} |"]
    out += [f"| {_md(name)} | {_md(v or 'no instalado')} |" for name, v in inv["versions"].items()]
    out += [f"| Node {_md(n['where'])} | {_md(n['version'])} |" for n in inv["node"]]
    out += ["", "### Actualizables", "", "| Paquete | Instalado | Disponible | Origen |", "|---|---|---|---|"]
    out += [f"| {_md(u['name'])} | {_md(u['old'])} | {_md(u['version'])} | {u['origin']}{' (' + _md(u['origin_host']) + ')' if u.get('origin_host') else ''} |"
            for u in inv["upgradable"]] or ["| — | | | |"]
    out += ["", "### Containers", "", "| Container | Imagen | Creada | Dias |", "|---|---|---|---|"]
    out += [f"| {_md(c['name'])} | {_md(c['image'])} | {_md(c['created'])} | {c['age_days']} |" for c in inv["containers"]] or ["| — | | | |"]
    out += ["", "## Feeds", "",
            f"- KEV: {stats.get('kev_total', 0)} entradas, {stats.get('matched', 0)} mapean a algo instalado "
            f"(corregidas: {stats.get('fixed', 0)}, no afectado: {stats.get('not_affected', 0)}, sin datos: {stats.get('unknown', 0)}); "
            f"consultas de CVE a Ubuntu en esta corrida: {stats.get('cve_lookups', 0)}"]
    out += [f"- {_md(name)}: {_md(status)}" for name, status in feed_status.items() if not name.startswith("cves/")]
    if inv["errors"]:
        out += ["", "## Errores del inventario", ""] + [f"- {_md(e)}" for e in inv["errors"]]
    return "\n".join(out) + "\n"


# --- Egress ------------------------------------------------------------------------------------


async def egress_check(config: dict) -> None:
    """Resuelve los hosts de los feeds y dice si egress_orgs cubre a host:python3 443/tcp."""
    from batcave import egress

    ecfg = {"asn_cache_days": 0.0001, "dns_timeout": 3, "max_lookups_per_cycle": 50}
    resolver = egress.AsnResolver(ecfg, persist=False)
    allowlist = egress.load_org_allowlist()
    ips: dict[str, list[str]] = {}
    for host in feed_hosts(config):
        try:
            ips[host] = sorted({a[4][0] for a in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)})
        except OSError as exc:
            print(f"{host}: no resuelve ({exc})")
            ips[host] = []
    identities = await resolver.resolve_many({ip for v in ips.values() for ip in v})
    for host, addrs in ips.items():
        for ip in addrs:
            ident = identities.get(ip)
            if not ident:
                print(f"{host} {ip}: sin ASN")
                continue
            reason = egress.match_org({"org": ident["org"], "source": "host:python3", "port": 443, "protocol": "tcp"}, allowlist)
            print(f"{host} {ip} AS{ident['asn']} {ident['org']} -> {'cubierto: ' + reason if reason else 'NO cubierto'}")


# --- Ejecucion --------------------------------------------------------------------------------


async def run(config: dict, args: argparse.Namespace) -> None:
    if args.egress_check:
        await egress_check(config)
        return
    notifier = None if args.dry_run else TelegramNotifier()
    now = datetime.now(timezone.utc)
    today = now.astimezone(LOCAL_TZ).date()
    day = today.isoformat()

    inv = await asyncio.to_thread(collect_inventory, config)
    feeds = FeedCache(CACHE_DIR, float(config["http_timeout"]), int(config["max_cve_lookups"]))
    findings, ok, stats = await asyncio.to_thread(correlate, inv, feeds, config, today)
    state = _read_json(STATE_PATH, {}) if STATE_PATH.exists() else {}
    alerts, comms, new_state, resolved = plan_notifications(
        findings, state if isinstance(state, dict) else {}, now, timedelta(hours=float(config["alert_cooldown_hours"]))
    )
    message = build_report_message(findings, ok, resolved, day, feeds.status, int(config["max_items_per_section"]))
    report = render_report(inv, findings, ok, resolved, stats, feeds.status, day)
    log.info(
        "Julia: %d hallazgo(s) (%s); %d alerta(s) inmediata(s), %d para Batman, %d resuelto(s)",
        len(findings), ", ".join(f"{s}={sum(1 for f in findings if f['severity'] == s)}" for s in reversed(SEVERITIES)),
        len(alerts), len(comms), len(resolved),
    )

    if args.dry_run:
        print(mask_secrets(report))
        print("=== Mensaje de Telegram (no se envia) ===")
        print(mask_secrets(message))
        if alerts:
            print("=== Alerta inmediata (no se envia) ===")
            print(mask_secrets(build_alert_message(alerts)))
        print(f"=== comms.json: {len(comms)} mensaje(s) para Batman (no se escriben) ===")
        return

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    (REPORTS_DIR / f"julia-{day}.md").write_text(mask_secrets(report), encoding="utf-8")
    if alerts:
        await notifier.send(build_alert_message(alerts))
        for f in alerts:
            update_memory("alert_sent", title=f["title"], detail=f["detail"], severity=f["severity"])
    await notifier.send(message)
    append_comms([comms_entry(f, now) for f in comms])
    _write_json(STATE_PATH, new_state)
    for name, status in feeds.status.items():
        if not name.startswith("cves/") and status.startswith(("error", "cache viejo")):
            update_memory("feed_error", feed=name, status=status)
    update_memory("cycle_complete")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Julia Pennyworth: inteligencia de amenazas (solo lectura)")
    parser.add_argument("--dry-run", action="store_true", help="imprime el informe; sin Telegram, estado, comms ni reporte")
    parser.add_argument("--egress-check", action="store_true", help="ASN de los hosts de los feeds vs egress_orgs de Batwoman")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    load_env()
    config = load_config()
    asyncio.run(run(config, args))


if __name__ == "__main__":
    main()
