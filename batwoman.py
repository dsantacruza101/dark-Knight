#!/usr/bin/env python3
"""Batwoman (Kate Kane): analista de seguridad de la Batifamilia.

Corre como servicio systemd de larga duracion (batwoman.service) y cada
`batwoman.poll_interval` segundos (config.yaml, 60 por defecto) lee las
alertas nuevas de Wazuh en /var/ossec/logs/alerts/alerts.json dentro del
container single-node-wazuh.manager-1 (via `docker exec`, el usuario
dsantacruz esta en el grupo docker).

El progreso se guarda en batcave/memory/batwoman_state.json (offset en
bytes, tamano e inode del archivo). Si el archivo se achica o cambia de
inode (rotacion diaria de Wazuh), se vuelve a leer desde 0. En el primer
arranque (sin estado) se empieza desde el final del archivo para no
re-notificar el historial. Una alerta JSON por linea; las lineas
malformadas se omiten.

Triage de cada alerta:

  1. Ruta critica (`batwoman.critical_paths`): cualquier alerta de syscheck
     sobre esas rutas se escala SIEMPRE, sin importar nivel ni allowlist.
  2. Allowlist (batcave/batwoman_allowlist.yaml): actividad conocida de la
     Batifamilia (rule id + regex sobre full_log/syscheck.path) se ignora.
  3. level < 7: se ignora. level 7-9: va al digest diario. level >= 10:
     se evalua.

Evaluacion con TypeSafe (Jev): is_real_threat (Noul), severity (Score:
info/low/medium/high/critical) y action (Choice: notify_now/digest/ignore).
Sin TYPESAFE_API_KEY (o si TypeSafe falla): notify_now si level >= 12 o
ruta critica, si no digest. Una ruta critica nunca se descarta con
`ignore` (como minimo va al digest).

Cambios explicados por sudo: el estado guarda un buffer de los sudo
exitosos de los ultimos `sudo_buffer_seconds` (10 min: usuario, TTY, PWD y
COMMAND). Un cambio FIM en ruta critica precedido (hasta
`sudo_explain_window`, 120s, por la hora del syslog) por un sudo de
`sudo_explain_user` desde una TTY interactiva cuyo comando lo explica
(menciona el nombre del archivo; systemctl enable/disable: la unit del
symlink; cp/install/mv/ln/tee/sed -i: la ruta destino) no se alerta: va al
digest en "Cambios explicados por sudo" con el comando enmascarado. Si no
hay explicacion y la alerta tiene menos de `sudo_explain_grace` segundos se
aplaza un poll (el sudo puede llegar despues); si tampoco aparece, se
alerta como siempre. sudoers, ssh, authorized_keys y los .env de secretos
(NEVER_EXPLAIN_PATHS) nunca se explican: alertan siempre de inmediato.

Las notificaciones a Alfred empiezan con "🦇 Batwoman" y se deduplican en
batcave/memory/batwoman_alert_state.json por rule id + srcip/ruta: solo se
reenvia si pasaron 6 horas. Los hallazgos de severidad high/critical se
dejan tambien para Batman en batcave/comms.json (type security_alert), con
la misma deduplicacion para no inundar la bandeja con rafagas.

Una vez al dia, a la hora `batwoman.digest_hour` (America/El_Salvador),
envia el digest: conteo por nivel, top 5 reglas, top 5 IPs de origen y
numero de cambios FIM; lo guarda en reports/batwoman-YYYY-MM-DD.md y lo
reinicia.

Junto con el digest corre el chequeo de integridad de paquetes:
`sudo -n dpkg --verify` (timeout `batwoman.integrity_timeout`, 600s por
defecto). Se ignoran los conffiles (flag `c`) y todo lo que este bajo /etc.
Cualquier otro archivo con checksum alterado ('5' en la 3a columna) es un
hallazgo critico: aviso inmediato a Alfred y mensaje `integrity_alert` para
Batman en batcave/comms.json, salvo las rutas absolutas exactas listadas en
`integrity_paths` del allowlist (nunca por paquete), que se cuentan aparte
como esperadas. El digest incluye el resultado (paquetes verificados limpios
/ con archivos alterados / esperados).

Ademas monitorea las conexiones salientes (batcave/egress.py): un lector de
`conntrack -E -e NEW` en segundo plano (TCP y UDP, se reinicia con backoff)
ve cada flujo nuevo, y cada `batwoman.egress.interval` segundos se procesan
los acumulados (snapshot de `ss`/`conntrack -L` solo como respaldo si el
stream estuvo caido): linea base por (proceso o container, ASN destino,
puerto destino, protocolo), aprendizaje los primeros
`egress.learning_days` dias y evaluacion con TypeSafe de los destinos
nuevos (is_suspicious/severity/action notify_now|digest|learn). Los flujos
cubiertos por `egress_orgs` del allowlist (organizacion + origen + puerto;
los CDN exigen origen y puerto) van directo a la linea base.
Las notificaciones usan la misma deduplicacion que las alertas de Wazuh y
el digest incluye una seccion Egress.

Todo lo que se loguea, se envia por Telegram o se escribe a disco pasa por
batcave.secrets.mask_secrets (los full_log de FIM pueden traer contenido de
archivos sensibles).

Flags:
  --dry-run          lee las ultimas 200 alertas e imprime las decisiones de
                     triage; sin Telegram ni cambios de estado.
  --once             un solo poll (y un ciclo de egress) con efectos normales
                     y termina.
  --egress-snapshot  escucha 60s el stream de conntrack e imprime esos flujos
                     mas el snapshot actual de ss/conntrack, clasificados
                     (new/known/allowlisted); sin alertas ni guardar nada.
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import yaml
from dotenv import load_dotenv
from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import TelegramError
from typesafe_sdk import AsyncTypeSafeClient, Choice, Noul, Score

from batcave import egress, memory_store
from batcave.secrets import mask_secrets

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = Path("/etc/night-agent.env")
CONFIG_PATH = BASE_DIR / "config.yaml"
LOG_PATH = BASE_DIR / "batwoman.log"
COMMS_PATH = BASE_DIR / "batcave" / "comms.json"
ALLOWLIST_PATH = BASE_DIR / "batcave" / "batwoman_allowlist.yaml"
STATE_PATH = BASE_DIR / "batcave" / "memory" / "batwoman_state.json"
ALERT_STATE_PATH = BASE_DIR / "batcave" / "memory" / "batwoman_alert_state.json"
REPORTS_DIR = BASE_DIR / "reports"

LOCAL_TZ = ZoneInfo("America/El_Salvador")

DEFAULTS = {
    "poll_interval": 60,
    "digest_hour": 8,
    "container": "single-node-wazuh.manager-1",
    "alerts_path": "/var/ossec/logs/alerts/alerts.json",
    "min_level": 7,
    "evaluate_level": 10,
    "notify_level": 12,
    "alert_cooldown_hours": 6,
    "critical_paths": [],
    "integrity_timeout": 600,
    "sudo_buffer_seconds": 600,
    "sudo_explain_window": 120,
    "sudo_explain_grace": 30,
    "sudo_explain_user": "dsantacruz",
    "never_explain_paths": [],
}

# Tope de bytes leidos por poll: si Wazuh genera una rafaga enorme, el resto
# se procesa en los polls siguientes en vez de cargarlo todo en memoria.
MAX_READ_BYTES = 5 * 1024 * 1024
DRY_RUN_TAIL_ALERTS = 200
FULL_LOG_EXCERPT_CHARS = 500
ALERT_STATE_RETENTION = timedelta(days=7)
DOCKER_TIMEOUT = 30

INTEGRITY_CMD = ["sudo", "-n", "dpkg", "--verify"]
INTEGRITY_IGNORED_PREFIX = "/etc/"
# Formato de dpkg --verify (estilo rpm): 9 caracteres de resultado ("??5??????"
# o "missing  "), espacio, atributo ('c' = conffile o espacio), espacio, ruta.
DPKG_VERIFY_LINE = re.compile(r"^(?P<result>.{9}) (?P<attr>[c ]) (?P<path>/.*)$")
INTEGRITY_MAX_LISTED = 15

# Cambios en rutas criticas explicados por un sudo interactivo del admin
# (instalar units: sudo cp ... /etc/systemd/system/, systemctl enable).
# Estas rutas NUNCA se explican: siempre alertan, aunque haya un sudo.
NEVER_EXPLAIN_PATHS = (
    "/etc/sudoers",
    "/etc/sudoers.d",
    "/etc/ssh",
    "/etc/night-agent.env",
    "/etc/webhook.env",
    "/etc/telegram-bot.env",
)
INTERACTIVE_TTY = re.compile(r"^(/dev/)?(pts/\d+|tty\d+)$")
SUDO_FAILURE_GROUPS = {"authentication_failed", "authentication_failures", "invalid_login"}
SYSLOG_TIME = re.compile(r"^(?P<month>[A-Z][a-z]{2})\s+(?P<day>\d{1,2})\s+(?P<time>\d{2}:\d{2}:\d{2})")
SYSLOG_MONTHS = {m: i for i, m in enumerate(("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1)}
SYSLOG_MAX_DRIFT = timedelta(minutes=10)
# El syslog del sudo tiene resolucion de segundos y la alerta FIM puede
# llevar la hora del manager: tolerancia minima "despues" del cambio.
SUDO_CLOCK_SKEW = timedelta(seconds=2)
SUDO_BUFFER_MAX = 500
SUDO_EXPLAINED_MAX = 100
SUDO_COMMAND_CHARS = 300
SUDO_DIGEST_GROUPS = 10
SYSTEMCTL_LINK_VERBS = {"enable", "disable", "reenable"}
DEST_COMMANDS = {"cp", "install", "mv", "ln"}
DEST_VALUE_OPTS = {"-S", "--suffix"}
INSTALL_VALUE_OPTS = {"-m", "--mode", "-o", "--owner", "-g", "--group"}

SEVERITY_LEVELS = ["info", "low", "medium", "high", "critical"]
COMMS_SEVERITIES = {"high", "critical"}
ACTION_CRITERIA = {
    "notify_now": "Hay que avisar a Alfred de inmediato: posible intrusion, escalada de privilegios o cambio no autorizado en un archivo sensible.",
    "digest": "Vale la pena revisarlo pero puede esperar al digest diario de la manana.",
    "ignore": "Es ruido o actividad legitima conocida del servidor, no amerita ni el digest.",
}


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
log = logging.getLogger("batwoman")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpx2").setLevel(logging.WARNING)
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
    return {**DEFAULTS, **(config.get("batwoman") or {})}


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


def read_comms() -> list[dict]:
    return _read_json(COMMS_PATH, [])


def append_comm(entry: dict) -> None:
    entries = read_comms()
    entries.append(entry)
    _write_json(COMMS_PATH, entries)


def empty_digest() -> dict:
    return {
        "since": datetime.now(LOCAL_TZ).isoformat(),
        "by_level": {},
        "rules": {},
        "srcips": {},
        "fim_changes": 0,
        "notified": 0,
        "allowlisted": 0,
        "egress_new": [],
    }


def read_state() -> Optional[dict]:
    state = _read_json(STATE_PATH, None)
    if not isinstance(state, dict):
        return None
    state.setdefault("digest", empty_digest())
    state.setdefault("last_digest_date", datetime.now(LOCAL_TZ).date().isoformat())
    return state


def write_state(state: dict) -> None:
    _write_json(STATE_PATH, state)


def should_send_alert(key: str, cooldown: timedelta) -> bool:
    """True si nunca se notifico `key` o si ya paso el cooldown (6h)."""
    entry = _read_json(ALERT_STATE_PATH, {}).get(key)
    if not entry:
        return True
    try:
        last_sent = datetime.fromisoformat(entry["timestamp"])
    except (KeyError, ValueError):
        return True
    return datetime.now(timezone.utc) - last_sent > cooldown


def record_alert(key: str) -> None:
    now = datetime.now(timezone.utc)
    state = _read_json(ALERT_STATE_PATH, {})
    pruned = {}
    for k, v in state.items():
        try:
            if now - datetime.fromisoformat(v["timestamp"]) <= ALERT_STATE_RETENTION:
                pruned[k] = v
        except (KeyError, ValueError, TypeError):
            continue
    pruned[key] = {"timestamp": now.isoformat()}
    _write_json(ALERT_STATE_PATH, pruned)


def update_memory(event: str, **kwargs) -> None:
    """Actualiza batcave/memory/batwoman.md segun el resultado del poll."""
    try:
        if event == "cycle_complete":
            memory_store.touch_last_activity("batwoman")
            memory_store.bump_stat("batwoman", "ciclos")
        elif event == "threat_notified":
            memory_store.append_error(
                "batwoman",
                error=_table_cell(kwargs["description"]),
                causa=_table_cell(f"regla {kwargs['rule_id']} nivel {kwargs['level']} {kwargs.get('target', '')}".strip()),
                solucion="notificado a Alfred" + (" y Batman" if kwargs.get("to_batman") else ""),
                resultado=f"severidad {kwargs['severity']}",
            )
            memory_store.bump_stat("batwoman", "escalaciones")
        elif event == "integrity_alert":
            memory_store.append_error(
                "batwoman",
                error=f"dpkg --verify: {kwargs['count']} archivo(s) con checksum alterado",
                causa=_table_cell(kwargs["packages"]),
                solucion="notificado a Alfred y Batman",
                resultado="severidad critical",
            )
            memory_store.bump_stat("batwoman", "escalaciones")
        elif event == "egress_alert":
            memory_store.append_error(
                "batwoman",
                error=_table_cell(f"conexion saliente nueva: {kwargs['description']}"),
                causa=_table_cell(f"IP {kwargs['ip']}"),
                solucion="notificado a Alfred" + (" y Batman" if kwargs.get("to_batman") else ""),
                resultado=f"severidad {kwargs['severity']}",
            )
            memory_store.bump_stat("batwoman", "escalaciones")
    except (OSError, ValueError) as exc:
        log.warning("No se pudo actualizar la memoria de Batwoman: %s", exc)


def _table_cell(text: str) -> str:
    return mask_secrets(str(text)).replace("|", "/").replace("\n", " ")[:200]


# --- Lectura de alertas de Wazuh ---------------------------------------------


def _docker_exec(config: dict, shell_cmd: str) -> Optional[bytes]:
    cmd = ["docker", "exec", config["container"], "sh", "-c", shell_cmd]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=DOCKER_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("Fallo docker exec en %s: %s", config["container"], exc)
        return None
    if proc.returncode != 0:
        log.warning(
            "docker exec en %s devolvio %s: %s",
            config["container"], proc.returncode, proc.stderr.decode("utf-8", "replace")[-300:],
        )
        return None
    return proc.stdout


def stat_alerts_file(config: dict) -> Optional[tuple[int, int]]:
    """Devuelve (tamano, inode) de alerts.json dentro del container."""
    out = _docker_exec(config, f"stat -c '%s %i' {config['alerts_path']}")
    if out is None:
        return None
    try:
        size, inode = out.decode().split()
        return int(size), int(inode)
    except ValueError:
        log.warning("Salida inesperada de stat: %r", out[:100])
        return None


def read_new_bytes(config: dict, offset: int) -> Optional[bytes]:
    return _docker_exec(
        config, f"tail -c +{int(offset) + 1} {config['alerts_path']} | head -c {MAX_READ_BYTES}"
    )


def read_tail_alerts(config: dict, n: int) -> list[dict]:
    out = _docker_exec(config, f"tail -n {int(n)} {config['alerts_path']}")
    return parse_alerts(out.decode("utf-8", "replace").splitlines()) if out else []


def parse_alerts(lines: list[str]) -> list[dict]:
    alerts = []
    skipped = 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            alert = json.loads(line)
        except json.JSONDecodeError:
            skipped += 1
            continue
        if isinstance(alert, dict):
            alerts.append(alert)
        else:
            skipped += 1
    if skipped:
        log.warning("Se omitieron %d linea(s) malformada(s) de alerts.json", skipped)
    return alerts


def fetch_new_alerts(config: dict, state: dict) -> tuple[list[dict], dict]:
    """Lee las alertas nuevas desde el offset guardado. Devuelve las alertas y
    el estado de lectura actualizado (sin persistirlo)."""
    stat = stat_alerts_file(config)
    if stat is None:
        return [], state
    size, inode = stat
    offset = state.get("offset", 0)

    if size < offset or (state.get("inode") is not None and inode != state["inode"]):
        log.info("alerts.json roto (tamano %d < offset %d o inode nuevo), se relee desde 0", size, offset)
        offset = 0

    new_state = {**state, "size": size, "inode": inode, "offset": offset}
    if size == offset:
        return [], new_state

    data = read_new_bytes(config, offset)
    if not data:
        return [], new_state

    # Solo se consume hasta el ultimo salto de linea: una alerta a medio
    # escribir se lee completa en el proximo poll.
    last_newline = data.rfind(b"\n")
    if last_newline == -1:
        return [], new_state
    consumed = data[: last_newline + 1]
    new_state["offset"] = offset + len(consumed)
    return parse_alerts(consumed.decode("utf-8", "replace").splitlines()), new_state


# --- Triage -------------------------------------------------------------------


def _read_allowlist_file() -> dict:
    try:
        with open(ALLOWLIST_PATH, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
    except (OSError, yaml.YAMLError) as exc:
        log.error("No se pudo leer %s: %s", ALLOWLIST_PATH, exc)
        return {}
    if not isinstance(raw, dict):
        log.error("%s no es un mapa YAML, se ignora", ALLOWLIST_PATH)
        return {}
    return raw


def load_allowlist() -> list[dict]:
    """Lee y compila el allowlist. Se llama en cada poll para que editarlo
    no requiera reiniciar el servicio."""
    raw = _read_allowlist_file()
    entries = []
    for entry in raw.get("ignore") or []:
        try:
            entries.append(
                {
                    "rule_id": str(entry["rule_id"]) if entry.get("rule_id") is not None else None,
                    "full_log": re.compile(entry["full_log"]) if entry.get("full_log") else None,
                    "path": re.compile(entry["path"]) if entry.get("path") else None,
                    "reason": entry.get("reason", "sin razon"),
                }
            )
        except (re.error, TypeError, AttributeError) as exc:
            log.error("Entrada invalida en el allowlist (%s): %s", exc, entry)
    return entries


def load_integrity_allowlist() -> set[str]:
    """Rutas absolutas exactas (`integrity_paths`) cuyo checksum alterado en
    dpkg --verify es esperado. Nunca se acepta un paquete entero ni prefijos:
    solo rutas absolutas normalizadas, comparadas por igualdad."""
    paths = set()
    for entry in _read_allowlist_file().get("integrity_paths") or []:
        if not isinstance(entry, str) or not os.path.isabs(entry) or os.path.normpath(entry) != entry:
            log.error("Entrada invalida en integrity_paths (debe ser una ruta absoluta exacta): %r", entry)
            continue
        paths.add(entry)
    return paths


def alert_fields(alert: dict) -> dict:
    rule = alert.get("rule") or {}
    mitre = rule.get("mitre") or {}
    syscheck = alert.get("syscheck") or {}
    data = alert.get("data") or {}
    try:
        level = int(rule.get("level", 0))
    except (TypeError, ValueError):
        level = 0
    return {
        "rule_id": str(rule.get("id", "?")),
        "level": level,
        "description": rule.get("description", "sin descripcion"),
        "mitre_tactics": mitre.get("tactic") or [],
        "mitre_techniques": mitre.get("technique") or [],
        "groups": rule.get("groups") or [],
        "srcip": data.get("srcip") or "",
        "srcuser": data.get("srcuser") or "",
        "agent": (alert.get("agent") or {}).get("name", ""),
        "syscheck_path": syscheck.get("path") or "",
        "syscheck_event": syscheck.get("event") or "",
        "full_log": alert.get("full_log") or "",
        "timestamp": alert.get("timestamp", ""),
    }


def is_critical_path(path: str, critical_paths: list[str]) -> bool:
    if not path:
        return False
    for critical in critical_paths:
        critical = critical.rstrip("/")
        if path == critical or path.startswith(critical + "/"):
            return True
    return False


def match_allowlist(fields: dict, allowlist: list[dict]) -> Optional[str]:
    for entry in allowlist:
        if entry["rule_id"] is not None and entry["rule_id"] != fields["rule_id"]:
            continue
        if entry["full_log"] is not None and not entry["full_log"].search(fields["full_log"]):
            continue
        if entry["path"] is not None and not entry["path"].search(fields["syscheck_path"]):
            continue
        return entry["reason"]
    return None


def triage(fields: dict, config: dict, allowlist: list[dict]) -> tuple[str, str]:
    """Clasifica la alerta. Devuelve (decision, motivo) con decision en
    critical/allowlisted/below_level/digest/evaluate."""
    if fields["syscheck_path"] and is_critical_path(fields["syscheck_path"], config["critical_paths"]):
        return "critical", "ruta critica"
    reason = match_allowlist(fields, allowlist)
    if reason:
        return "allowlisted", reason
    if fields["level"] < config["min_level"]:
        return "below_level", f"nivel {fields['level']} < {config['min_level']}"
    if fields["level"] < config["evaluate_level"]:
        return "digest", f"nivel {fields['level']}"
    return "evaluate", f"nivel {fields['level']} >= {config['evaluate_level']}"


# --- Cambios explicados por sudo ------------------------------------------------


def parse_alert_timestamp(value: str) -> Optional[datetime]:
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%f%z")
    except (TypeError, ValueError):
        return None


def syslog_time(text: Optional[str], reference: datetime) -> Optional[datetime]:
    """Hora del syslog ("Oct 30 03:01:36", hora local del host, sin anio).
    Es la hora real del comando: la alerta de Wazuh del sudo puede llegar
    DESPUES que la del FIM que provoco (journald va con ~2s de retraso).
    None si no se puede leer o si se aleja de la alerta mas de lo razonable
    (zona horaria distinta): entonces se usa la hora de la alerta."""
    match = SYSLOG_TIME.match(text or "")
    if not match or match.group("month") not in SYSLOG_MONTHS:
        return None
    try:
        naive = datetime(
            reference.year, SYSLOG_MONTHS[match.group("month")], int(match.group("day")),
            *(int(part) for part in match.group("time").split(":")),
        )
    except ValueError:
        return None
    parsed = naive.astimezone()  # naive = hora local del host
    if parsed - reference > timedelta(days=1):  # cruce de anio (31 dic -> 1 ene)
        parsed = parsed.replace(year=parsed.year - 1)
    if abs(parsed - reference) > SYSLOG_MAX_DRIFT:
        return None
    return parsed


def _sudo_log_field(full_log: str, name: str) -> str:
    match = re.search(rf"\b{name}=([^;]*?)\s*(?:;|$)", full_log)
    return match.group(1).strip() if match else ""


def sudo_event(alert: dict) -> Optional[dict]:
    """Evento de sudo exitoso (5402 y relacionadas: decoder `sudo` con
    COMMAND) para el buffer, o None si la alerta no lo es."""
    decoder = alert.get("decoder") or {}
    if "sudo" not in (decoder.get("parent"), decoder.get("name")):
        return None
    rule = alert.get("rule") or {}
    if SUDO_FAILURE_GROUPS & set(rule.get("groups") or []):
        return None
    data = alert.get("data") or {}
    full_log = alert.get("full_log") or ""
    # COMMAND va al final de la linea y puede traer ';' propios.
    command = data.get("command") or (full_log.split("COMMAND=", 1)[1].strip() if "COMMAND=" in full_log else "")
    alert_ts = parse_alert_timestamp(alert.get("timestamp", ""))
    if not command or alert_ts is None:
        return None
    user_match = re.search(r"sudo(?:\[\d+\])?:\s+(\S+) :", full_log)
    event_ts = syslog_time((alert.get("predecoder") or {}).get("timestamp"), alert_ts) or alert_ts
    return {
        "id": str(alert.get("id", "")),
        "rule_id": str(rule.get("id", "?")),
        "timestamp": event_ts.isoformat(),
        "user": data.get("srcuser") or (user_match.group(1) if user_match else ""),
        "tty": data.get("tty") or _sudo_log_field(full_log, "TTY"),
        "pwd": data.get("pwd") or _sudo_log_field(full_log, "PWD"),
        "command": command,
    }


def update_sudo_buffer(buffer: list[dict], alerts: list[dict], config: dict, now: Optional[datetime] = None) -> list[dict]:
    """Agrega los sudo del lote al buffer y descarta los mas viejos que
    `sudo_buffer_seconds` (10 min). Se llama ANTES de procesar el lote, asi
    un sudo cuya alerta llega despues de la del FIM (mismo lote) igual cuenta."""
    known = {ev.get("id") for ev in buffer}
    for alert in alerts:
        ev = sudo_event(alert)
        if ev and ev["id"] not in known:
            buffer.append(ev)
            known.add(ev["id"])
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(seconds=config["sudo_buffer_seconds"])
    kept = []
    for ev in buffer:
        try:
            if datetime.fromisoformat(ev["timestamp"]) >= cutoff:
                kept.append(ev)
        except (KeyError, TypeError, ValueError):
            continue
    return kept[-SUDO_BUFFER_MAX:]


def is_never_explained(path: str, config: dict) -> bool:
    """Rutas que SIEMPRE alertan aunque las cambie un sudo interactivo
    (sudoers, ssh, authorized_keys, archivos .env de secretos). Regla fija:
    config.yaml (`never_explain_paths`) solo puede ampliar la lista."""
    if is_critical_path(path, list(NEVER_EXPLAIN_PATHS) + list(config.get("never_explain_paths") or [])):
        return True
    base = os.path.basename(path)
    return "/.ssh/" in path or base.startswith("authorized_keys") or base.endswith(".env")


def _abs_path(token: str, pwd: str) -> str:
    return os.path.normpath(os.path.join(pwd or "/", token))


def _systemctl_explains(path: str, args: list[str]) -> bool:
    """enable/disable/reenable crean o borran el symlink de la unit: explica
    el cambio si el symlink se llama como la unit o apunta a ella."""
    positional = [a for a in args if not a.startswith("-")]
    if not positional or positional[0] not in SYSTEMCTL_LINK_VERBS:
        return False
    units = {os.path.basename(u) if "." in os.path.basename(u) else f"{os.path.basename(u)}.service" for u in positional[1:]}
    names = {os.path.basename(path)}
    try:
        names.add(os.path.basename(os.readlink(path)))
    except OSError:
        pass  # disable ya lo borro, o no es symlink
    return bool(names & units)


def _copy_explains(path: str, prog: str, args: list[str], pwd: str) -> bool:
    """cp/install/mv/ln: explica el cambio solo si `path` es el destino (el
    archivo destino, o <directorio destino>/<nombre de una fuente>)."""
    value_opts = DEST_VALUE_OPTS | (INSTALL_VALUE_OPTS if prog == "install" else set())
    target_dir: Optional[str] = None
    positional: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            positional += args[i + 1:]
            break
        if arg in ("-t", "--target-directory"):
            target_dir = args[i + 1] if i + 1 < len(args) else None
            i += 2
            continue
        if arg.startswith("--target-directory="):
            target_dir = arg.split("=", 1)[1]
        elif arg in value_opts:
            i += 1
        elif not arg.startswith("-") or arg == "-":
            positional.append(arg)
        i += 1

    if target_dir:
        dest_dir, sources, candidates = _abs_path(target_dir, pwd), positional, set()
    else:
        if len(positional) < 2:
            return False
        dest = _abs_path(positional[-1], pwd)
        dest_dir, sources, candidates = dest, positional[:-1], {dest}
    candidates |= {os.path.join(dest_dir, os.path.basename(s.rstrip("/"))) for s in sources}
    # Copias recursivas: archivos dentro de <destino>/<fuente>/. Nunca el
    # prefijo del directorio destino solo (explicaria todo lo que hay ahi).
    return path in candidates or any(
        path.startswith(c + "/") for c in candidates if c != dest_dir
    )


def _sed_explains(path: str, args: list[str], pwd: str) -> bool:
    """sed solo modifica con -i/--in-place; los archivos son los posicionales
    (el primero es el script si no se uso -e/-f)."""
    in_place = script_given = False
    positional: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("-e", "--expression", "-f", "--file", "-l", "--line-length"):
            script_given = script_given or arg not in ("-l", "--line-length")
            i += 2
            continue
        if arg.startswith(("--expression=", "--file=")):
            script_given = True
        elif arg.startswith("--in-place"):
            in_place = True
        elif arg.startswith("-") and len(arg) > 1 and not arg.startswith("--"):
            flags = arg[1:]
            for j, flag in enumerate(flags):
                if flag == "i":  # -i[SUFIJO]: lo que sigue es el sufijo del respaldo
                    in_place = True
                    break
                if flag in "efl":
                    script_given = script_given or flag != "l"
                    if j == len(flags) - 1:
                        i += 1  # el valor es el siguiente argumento
                    break
        elif not arg.startswith("--"):
            positional.append(arg)
        i += 1
    files = positional if script_given else positional[1:]
    return in_place and path in {_abs_path(f, pwd) for f in files}


def command_explains(path: str, command: str, pwd: str) -> bool:
    """True si el comando de sudo explica el cambio FIM en `path`."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    if not tokens:
        return False
    prog, args = os.path.basename(tokens[0]), tokens[1:]
    if prog == "systemctl":
        return _systemctl_explains(path, args)
    if prog in DEST_COMMANDS:
        return _copy_explains(path, prog, args, pwd)
    if prog == "tee":
        return path in {_abs_path(a, pwd) for a in args if not a.startswith("-")}
    if prog == "sed":
        return _sed_explains(path, args, pwd)
    base = os.path.basename(path)
    return any(os.path.basename(a.rstrip("/")) == base for a in args)


def find_sudo_explanation(fields: dict, buffer: list[dict], config: dict) -> Optional[dict]:
    """Sudo interactivo de `sudo_explain_user` (TTY pts/tty) hasta
    `sudo_explain_window` segundos ANTES del cambio cuyo comando lo explica.
    Devuelve el mas reciente, o None (nunca para rutas is_never_explained)."""
    path = fields["syscheck_path"]
    change_ts = parse_alert_timestamp(fields["timestamp"])
    if not path or change_ts is None or is_never_explained(path, config):
        return None
    window = timedelta(seconds=config["sudo_explain_window"])
    best, best_ts = None, None
    for ev in buffer:
        if ev.get("user") != config["sudo_explain_user"] or not INTERACTIVE_TTY.match(ev.get("tty") or ""):
            continue
        try:
            ts = datetime.fromisoformat(ev["timestamp"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (change_ts - window <= ts <= change_ts + SUDO_CLOCK_SKEW):
            continue
        if command_explains(path, ev.get("command", ""), ev.get("pwd", "")) and (best_ts is None or ts > best_ts):
            best, best_ts = ev, ts
    return best


def should_defer(fields: dict, config: dict, now: Optional[datetime] = None) -> bool:
    """Una ruta critica sin explicacion cuya alerta tiene menos de
    `sudo_explain_grace` segundos se aplaza un poll: la alerta del sudo
    puede no haberse escrito todavia en alerts.json."""
    change_ts = parse_alert_timestamp(fields["timestamp"])
    if change_ts is None or is_never_explained(fields["syscheck_path"], config):
        return False
    age = (now or datetime.now(timezone.utc)) - change_ts
    return age < timedelta(seconds=config["sudo_explain_grace"])


def record_sudo_explained(digest: dict, fields: dict, ev: dict) -> None:
    digest["sudo_explained_count"] = digest.get("sudo_explained_count", 0) + 1
    items = digest.setdefault("sudo_explained", [])
    if len(items) < SUDO_EXPLAINED_MAX:
        items.append(
            {
                "timestamp": fields["timestamp"],
                "path": fields["syscheck_path"],
                "event": fields["syscheck_event"] or "cambio",
                "sudo_id": ev.get("id", ""),
                "user": ev.get("user", ""),
                "tty": ev.get("tty", ""),
                "command": mask_secrets(ev.get("command", ""))[:SUDO_COMMAND_CHARS],
            }
        )


# --- Evaluacion ---------------------------------------------------------------


def typesafe_state(fields: dict, critical: bool) -> dict:
    return {
        "rule_id": fields["rule_id"],
        "level": fields["level"],
        "description": fields["description"],
        "mitre_tactics": fields["mitre_tactics"],
        "mitre_techniques": fields["mitre_techniques"],
        "groups": fields["groups"],
        "srcip": fields["srcip"],
        "srcuser": fields["srcuser"],
        "syscheck_path": fields["syscheck_path"],
        "syscheck_event": fields["syscheck_event"],
        "critical_path": critical,
        "full_log_excerpt": mask_secrets(fields["full_log"])[:FULL_LOG_EXCERPT_CHARS],
        "context": (
            "Servidor personal Ubuntu con agentes autonomos (Batifamilia) que reinician "
            "servicios systemd via sudo y hacen git pull en /home/dsantacruz/projects. "
            "Los critical_path son llaves SSH, sudoers, archivos de secretos y units systemd."
        ),
    }


def fallback_decision(fields: dict, critical: bool, config: dict) -> dict:
    notify = critical or fields["level"] >= config["notify_level"]
    if fields["level"] >= 15:
        severity = "critical"
    elif notify:
        severity = "high"
    else:
        severity = "medium"
    return {
        "is_real_threat": 1.0 if notify else 0.5,
        "severity": severity,
        "action": "notify_now" if notify else "digest",
        "source": "fallback",
    }


async def evaluate_with_typesafe(client: AsyncTypeSafeClient, fields: dict, critical: bool, config: dict) -> dict:
    try:
        response = await client.system_one(
            state=typesafe_state(fields, critical),
            questions={
                "is_real_threat": Noul(
                    instructions=(
                        "Esta alerta de Wazuh corresponde a una amenaza real (intrusion, "
                        "abuso de privilegios o manipulacion no autorizada) y no a "
                        "actividad legitima del propio servidor."
                    )
                ),
                "severity": Score(
                    instructions="Que tan grave es esta alerta para la seguridad del servidor.",
                    criteria=SEVERITY_LEVELS,
                ),
                "action": Choice(
                    instructions="Que deberia hacer la analista de seguridad con esta alerta.",
                    criteria=ACTION_CRITERIA,
                ),
            },
        )
    except Exception as exc:  # noqa: BLE001 - un fallo de TypeSafe no debe tumbar el ciclo
        log.error("Fallo la evaluacion TypeSafe de la regla %s: %s", fields["rule_id"], exc)
        return fallback_decision(fields, critical, config)

    severity_raw = response.scores["severity"].score
    severity_idx = max(0, min(len(SEVERITY_LEVELS) - 1, round(severity_raw)))
    action = response.choices["action"].choice
    if action not in ACTION_CRITERIA:
        action = fallback_decision(fields, critical, config)["action"]
    if critical and action == "ignore":
        # Una ruta critica nunca se descarta en silencio.
        action = "digest"
    return {
        "is_real_threat": response.nouls["is_real_threat"].noul,
        "severity": SEVERITY_LEVELS[severity_idx],
        "action": action,
        "source": "typesafe",
    }


async def evaluate(client: Optional[AsyncTypeSafeClient], fields: dict, critical: bool, config: dict) -> dict:
    if client is None:
        return fallback_decision(fields, critical, config)
    return await evaluate_with_typesafe(client, fields, critical, config)


# --- Notificacion ---------------------------------------------------------------


def format_local_time(timestamp: str) -> str:
    try:
        dt = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%S.%f%z")
    except ValueError:
        return timestamp or "desconocida"
    return dt.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _code(value) -> str:
    return f"<code>{html.escape(mask_secrets(str(value)))}</code>"


def build_alert_message(fields: dict, decision: dict, critical: bool) -> str:
    lines = [
        "🦇 Batwoman — <b>alerta de seguridad</b>" + (" (ruta critica)" if critical else ""),
        f"Regla: {_code(fields['description'])} (id {_code(fields['rule_id'])})",
        f"Nivel: {_code(fields['level'])} | Severidad: {_code(decision['severity'])}",
        f"MITRE: {_code(', '.join(fields['mitre_tactics']) or 'n/a')}",
    ]
    if fields["srcip"]:
        lines.append(f"IP origen: {_code(fields['srcip'])}")
    if fields["syscheck_path"]:
        lines.append(f"Ruta: {_code(fields['syscheck_path'])} ({_code(fields['syscheck_event'] or 'cambio')})")
    lines.append(f"Hora: {_code(format_local_time(fields['timestamp']))}")
    return "\n".join(lines)


def dedup_key(fields: dict) -> str:
    return f"{fields['rule_id']}|{fields['srcip'] or fields['syscheck_path'] or '-'}"


async def act(fields: dict, decision: dict, critical: bool, config: dict, notifier: TelegramNotifier) -> bool:
    """Notifica a Alfred y/o Batman segun la decision. Devuelve True si se
    envio algo (para contarlo en el digest)."""
    to_alfred = decision["action"] == "notify_now"
    to_batman = decision["severity"] in COMMS_SEVERITIES
    if not (to_alfred or to_batman):
        return False

    key = dedup_key(fields)
    cooldown = timedelta(hours=config["alert_cooldown_hours"])
    if not should_send_alert(key, cooldown):
        log.info("Regla %s (%s) ya notificada hace menos de %s, no se reenvia", fields["rule_id"], key, cooldown)
        return False

    if to_alfred:
        await notifier.send(build_alert_message(fields, decision, critical))
    if to_batman:
        append_comm(
            {
                "timestamp": datetime.now().isoformat(),
                "from": "batwoman",
                "to": "batman",
                "type": "security_alert",
                "service": fields["syscheck_path"] or fields["srcip"] or f"regla {fields['rule_id']}",
                "priority": decision["severity"],
                "message": mask_secrets(
                    f"Wazuh regla {fields['rule_id']} (nivel {fields['level']}): {fields['description']}"
                    + (f" desde {fields['srcip']}" if fields["srcip"] else "")
                    + (f" en {fields['syscheck_path']}" if fields["syscheck_path"] else "")
                ),
                "is_real_threat": round(decision["is_real_threat"], 2),
                "mitre_tactics": fields["mitre_tactics"],
                "read": False,
            }
        )
    record_alert(key)
    log.warning(
        "Alerta notificada: regla %s nivel %s severidad %s (%s)%s",
        fields["rule_id"], fields["level"], decision["severity"], key, " + Batman" if to_batman else "",
    )
    update_memory(
        "threat_notified",
        description=fields["description"],
        rule_id=fields["rule_id"],
        level=fields["level"],
        target=fields["srcip"] or fields["syscheck_path"],
        severity=decision["severity"],
        to_batman=to_batman,
    )
    return True


# --- Conexiones salientes (batcave/egress.py) ------------------------------------------


async def act_egress(item: dict, config: dict, notifier: TelegramNotifier) -> bool:
    """Notifica un destino saliente nuevo (notify_now -> Alfred, severidad
    high/critical -> Batman), con la misma deduplicacion que las alertas."""
    decision = item["decision"]
    to_alfred = decision["action"] == "notify_now"
    to_batman = decision["severity"] in COMMS_SEVERITIES
    if not (to_alfred or to_batman):
        return False

    key = f"egress|{item['key']}"
    cooldown = timedelta(hours=config["alert_cooldown_hours"])
    if not should_send_alert(key, cooldown):
        log.info("Destino saliente %s ya notificado hace menos de %s, no se reenvia", item["key"], cooldown)
        return False

    if to_alfred:
        await notifier.send(egress.build_alert_message(item))
    if to_batman:
        append_comm(
            {
                "timestamp": datetime.now().isoformat(),
                "from": "batwoman",
                "to": "batman",
                "type": "egress_alert",
                "service": item["source"],
                "priority": decision["severity"],
                "message": mask_secrets(
                    f"Conexion saliente nueva: {egress.describe(item)} (IP {item['ip']}"
                    + (f", {item['rdns']}" if item["rdns"] else "")
                    + f"); origen en la linea base: {item['source_age']}"
                ),
                "is_suspicious": round(decision["is_suspicious"], 2),
                "read": False,
            }
        )
    record_alert(key)
    log.warning(
        "Egress notificado: %s severidad %s%s", egress.describe(item), decision["severity"], " + Batman" if to_batman else ""
    )
    update_memory(
        "egress_alert",
        description=egress.describe(item),
        ip=item["ip"],
        severity=decision["severity"],
        to_batman=to_batman,
    )
    return True


async def egress_poll(
    monitor: egress.EgressMonitor, config: dict, client: Optional[AsyncTypeSafeClient], notifier: TelegramNotifier
) -> None:
    """Un ciclo de egress: flujos del stream (o snapshot de respaldo), linea
    base, evaluacion, notificacion y registro en el digest (en
    batwoman_state.json)."""
    evaluated = await monitor.run_cycle(client)
    if not evaluated:
        return
    state = read_state()
    for item in evaluated:
        try:
            if await act_egress(item, config, notifier) and state is not None:
                state["digest"]["notified"] += 1
        except Exception:  # noqa: BLE001 - un destino raro no debe bloquear el resto
            log.exception("Fallo notificando el destino saliente %s", item["key"])
        if state is not None:
            egress.add_to_digest(state["digest"].setdefault("egress_new", []), item)
    if state is not None:
        write_state(state)


# --- Integridad de paquetes (dpkg --verify)---------------------------------------


def parse_dpkg_verify(output: str, expected_paths: set[str]) -> dict:
    """Clasifica la salida de dpkg --verify. Solo los archivos fuera de /etc,
    que no son conffiles y tienen checksum alterado cuentan como hallazgo;
    los de `expected_paths` (integrity_paths del allowlist) se cuentan aparte
    como esperados."""
    result = {"modified": [], "expected": [], "ignored": 0, "missing": 0, "other": 0, "unparsed": 0}
    for line in output.splitlines():
        if not line.strip():
            continue
        match = DPKG_VERIFY_LINE.match(line)
        if not match:
            result["unparsed"] += 1
            continue
        path = match["path"]
        if match["attr"] == "c" or path.startswith(INTEGRITY_IGNORED_PREFIX):
            result["ignored"] += 1
        elif match["result"].startswith("missing"):
            result["missing"] += 1
        elif match["result"][2] == "5":
            result["expected" if path in expected_paths else "modified"].append(path)
        else:
            result["other"] += 1
    if result["unparsed"]:
        log.warning("dpkg --verify: %d linea(s) con formato inesperado", result["unparsed"])
    return result


def run_dpkg_verify(timeout: int) -> tuple[Optional[str], Optional[str]]:
    """Corre `sudo -n dpkg --verify`. Devuelve (salida, None) o (None, error).
    dpkg sale con 1 cuando encuentra diferencias, eso no es un error."""
    try:
        proc = subprocess.run(INTEGRITY_CMD, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, f"timeout de {timeout}s"
    except OSError as exc:
        return None, str(exc)
    if proc.returncode not in (0, 1) or (proc.returncode == 1 and not proc.stdout.strip()):
        return None, f"exit {proc.returncode}: {proc.stderr.strip()[-300:] or 'sin stderr'}"
    return proc.stdout, None


def packages_owning(paths: list[str]) -> dict[str, str]:
    """Mapea ruta -> paquete(s) duenos via `dpkg -S` (no requiere sudo)."""
    if not paths:
        return {}
    try:
        proc = subprocess.run(["dpkg", "-S", *paths], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("No se pudo resolver el paquete de los archivos alterados: %s", exc)
        return {}
    owners = {}
    for line in proc.stdout.splitlines():
        if line.startswith("diversion ") or ": " not in line:
            continue
        pkgs, path = line.split(": ", 1)
        owners[path] = pkgs
    return owners


def installed_package_count() -> Optional[int]:
    try:
        proc = subprocess.run(
            ["dpkg-query", "-W", "-f", "${db:Status-Abbrev}\n"], capture_output=True, text=True, timeout=60
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("No se pudo contar los paquetes instalados: %s", exc)
        return None
    return sum(1 for line in proc.stdout.splitlines() if line.startswith("ii"))


async def check_package_integrity(config: dict, notifier: TelegramNotifier) -> dict:
    """Chequeo diario de integridad. Si hay binarios/librerias alterados,
    avisa a Alfred y deja mensaje para Batman. Devuelve el resumen para el
    digest."""
    timeout = int(config["integrity_timeout"])
    log.info("Verificando integridad de paquetes (dpkg --verify, timeout %ss)", timeout)
    output, error = await asyncio.to_thread(run_dpkg_verify, timeout)
    if error:
        log.error("No se pudo correr dpkg --verify: %s", error)
        return {"ok": False, "error": error}

    parsed = parse_dpkg_verify(output, load_integrity_allowlist())
    modified = parsed["modified"]
    for path in parsed["expected"]:
        log.info("Integridad: checksum alterado esperado en %s (integrity_paths)", path)
    owners = await asyncio.to_thread(packages_owning, modified) if modified else {}
    affected = {pkg.strip() for p in modified for pkg in owners.get(p, "?").split(",")}
    installed = await asyncio.to_thread(installed_package_count)
    summary = {
        "ok": True,
        "installed": installed,
        "clean": installed - len(affected) if installed is not None else None,
        "modified": len(modified),
        "affected_packages": sorted(affected),
        "expected": len(parsed["expected"]),
        "ignored": parsed["ignored"],
        "missing": parsed["missing"],
    }
    log.info(
        "dpkg --verify: %d archivo(s) alterado(s), %d esperado(s), %d ignorado(s) (conffiles//etc), %d faltante(s)",
        len(modified), len(parsed["expected"]), parsed["ignored"], parsed["missing"],
    )
    if not modified:
        return summary

    listed = modified[:INTEGRITY_MAX_LISTED]
    lines = [
        "🦇 Batwoman — <b>CRITICO: integridad de paquetes comprometida</b>",
        f"{_code(len(modified))} binario(s)/libreria(s) con checksum distinto al de su paquete:",
        *[f"• {_code(p)} ({_code(owners.get(p, '?'))})" for p in listed],
    ]
    if len(modified) > len(listed):
        lines.append(f"… y {len(modified) - len(listed)} mas (ver batwoman.log)")
    await notifier.send("\n".join(lines))
    append_comm(
        {
            "timestamp": datetime.now().isoformat(),
            "from": "batwoman",
            "to": "batman",
            "type": "integrity_alert",
            "service": "dpkg",
            "priority": "critical",
            "message": mask_secrets(
                f"dpkg --verify: {len(modified)} archivo(s) de paquetes con checksum alterado "
                f"(paquetes: {', '.join(sorted(affected))})"
            ),
            "files": [{"path": p, "package": owners.get(p, "?")} for p in modified],
            "read": False,
        }
    )
    for path in modified:
        log.critical("Integridad: checksum alterado en %s (paquete %s)", path, owners.get(path, "?"))
    update_memory("integrity_alert", count=len(modified), packages=", ".join(sorted(affected)))
    return summary


def render_integrity(integrity: Optional[dict]) -> tuple[str, str]:
    """Linea del digest para el chequeo de integridad: (HTML, Markdown)."""
    if not integrity:
        return "no se corrio", "no se corrio"
    if not integrity["ok"]:
        return f"⚠️ no se pudo verificar ({_code(integrity['error'])})", f"no se pudo verificar ({integrity['error']})"
    clean = f"{integrity['clean']}/{integrity['installed']}" if integrity["clean"] is not None else "?"
    status = "✅" if integrity["modified"] == 0 else "🚨"
    extra = (
        f"{integrity.get('expected', 0)} esperado(s) (integrity_paths), "
        f"{integrity['ignored']} conffile(s)//etc ignorado(s), {integrity['missing']} faltante(s)"
    )
    if integrity["modified"]:
        extra = (
            f"{integrity['modified']} archivo(s) alterado(s) en "
            f"{', '.join(integrity['affected_packages'])}; " + extra
        )
    return (
        f"{status} {_code(clean)} paquete(s) limpios — {html.escape(extra)}",
        f"{status} {clean} paquete(s) limpios — {extra}",
    )


# --- Digest diario ----------------------------------------------------------------


def add_to_digest(digest: dict, fields: dict) -> None:
    level = str(fields["level"])
    digest["by_level"][level] = digest["by_level"].get(level, 0) + 1
    rule_key = f"{fields['rule_id']}|{fields['description']}"
    digest["rules"][rule_key] = digest["rules"].get(rule_key, 0) + 1
    if fields["srcip"]:
        digest["srcips"][fields["srcip"]] = digest["srcips"].get(fields["srcip"], 0) + 1
    if "syscheck" in fields["groups"] or fields["syscheck_path"]:
        digest["fim_changes"] += 1


def render_sudo_explained(digest: dict) -> tuple[list[str], list[str]]:
    """Seccion 'Cambios explicados por sudo' del digest: (HTML, Markdown).
    En Telegram se agrupa por comando (un cp instala varios archivos)."""
    count = digest.get("sudo_explained_count", 0)
    if not count:
        return [], []
    items = digest.get("sudo_explained") or []
    groups: dict[str, list[dict]] = {}
    for item in items:
        groups.setdefault(item.get("sudo_id") or item["command"], []).append(item)

    html_lines = [f"<b>Cambios explicados por sudo:</b> {_code(count)}"]
    for group in list(groups.values())[:SUDO_DIGEST_GROUPS]:
        first = group[0]
        paths = ", ".join(_code(i["path"]) for i in group[:5]) + (f" (+{len(group) - 5})" if len(group) > 5 else "")
        html_lines.append(
            f"• {_code(format_local_time(first['timestamp']))} {_code(first['command'])} "
            f"({html.escape(first['user'])} en {html.escape(first['tty'])}) → {paths}"
        )
    if len(groups) > SUDO_DIGEST_GROUPS:
        html_lines.append(f"• ... y {len(groups) - SUDO_DIGEST_GROUPS} comando(s) mas (ver reporte)")

    md = [
        "## Cambios explicados por sudo",
        "",
        "Cambios en rutas criticas precedidos por un sudo interactivo del admin "
        "(no se alertaron de inmediato).",
        "",
        "| Hora | Ruta | Evento | Usuario / TTY | Comando |",
        "|---|---|---|---|---|",
        *[
            f"| {format_local_time(i['timestamp'])} | {_table_cell(i['path'])} | {_table_cell(i['event'])} "
            f"| {_table_cell(i['user'])} {_table_cell(i['tty'])} | `{_table_cell(i['command'])}` |"
            for i in items
        ],
    ]
    if count > len(items):
        md.append(f"\n- ... y {count - len(items)} cambio(s) mas no listados.")
    md.append("")
    return html_lines, md


def render_digest(digest: dict, config: dict) -> tuple[str, str]:
    """Devuelve (mensaje HTML para Telegram, reporte Markdown)."""
    total = sum(digest["by_level"].values())
    by_level = sorted(digest["by_level"].items(), key=lambda kv: -int(kv[0]))
    top_rules = Counter(digest["rules"]).most_common(5)
    top_ips = Counter(digest["srcips"]).most_common(5)
    since = format_local_time_iso(digest["since"])

    msg = [
        "🦇 Batwoman — <b>digest diario de seguridad</b>",
        f"Desde {_code(since)}: {_code(total)} alerta(s) nivel &gt;= 7, "
        f"{_code(digest['notified'])} notificada(s), {_code(digest['allowlisted'])} en allowlist",
        "<b>Por nivel:</b> " + (", ".join(f"{_code(lvl)}: {n}" for lvl, n in by_level) or "ninguna"),
        "<b>Top reglas:</b>",
    ]
    msg += [f"• {_code(rk.split('|', 1)[0])} {html.escape(mask_secrets(rk.split('|', 1)[1]))} — {n}" for rk, n in top_rules] or ["• ninguna"]
    msg.append("<b>Top IPs de origen:</b>")
    msg += [f"• {_code(ip)} — {n}" for ip, n in top_ips] or ["• ninguna"]
    msg.append(f"<b>Cambios FIM:</b> {_code(digest['fim_changes'])}")
    explained_html, explained_md = render_sudo_explained(digest)
    msg += explained_html
    integrity_html, integrity_md = render_integrity(digest.get("integrity"))
    msg.append(f"<b>Integridad de paquetes:</b> {integrity_html}")
    egress_html, egress_md = [], []
    ecfg = egress.load_config(config)
    if ecfg["enabled"]:
        try:
            egress_html, egress_md = egress.render_digest_section(digest.get("egress_new") or [], digest["since"], ecfg)
        except Exception as exc:  # noqa: BLE001 - el digest sale aunque falle la seccion Egress
            log.exception("Fallo la seccion Egress del digest")
            egress_html = [f"<b>Egress:</b> ⚠️ no se pudo generar ({_code(str(exc)[:200])})"]
            egress_md = ["## Egress (conexiones salientes)", "", f"- No se pudo generar: {str(exc)[:200]}", ""]
    msg += egress_html

    md = [
        f"# Digest de seguridad (Batwoman) - {datetime.now(LOCAL_TZ).strftime('%Y-%m-%d')}",
        "",
        f"- Periodo: desde {since}",
        f"- Alertas nivel >= 7: {total}",
        f"- Notificadas a Alfred/Batman: {digest['notified']}",
        f"- Ignoradas por allowlist: {digest['allowlisted']}",
        f"- Cambios FIM: {digest['fim_changes']}",
        f"- Cambios explicados por sudo: {digest.get('sudo_explained_count', 0)}",
        f"- Integridad de paquetes (dpkg --verify): {integrity_md}",
        "",
        "## Conteo por nivel",
        "| Nivel | Alertas |",
        "|---|---|",
        *[f"| {lvl} | {n} |" for lvl, n in by_level],
        "",
        "## Top 5 reglas",
        "| Regla | Descripcion | Alertas |",
        "|---|---|---|",
        *[f"| {rk.split('|', 1)[0]} | {_table_cell(rk.split('|', 1)[1])} | {n} |" for rk, n in top_rules],
        "",
        "## Top 5 IPs de origen",
        "| IP | Alertas |",
        "|---|---|",
        *[f"| {ip} | {n} |" for ip, n in top_ips],
        "",
        *explained_md,
        *egress_md,
    ]
    return "\n".join(msg), mask_secrets("\n".join(md))


def format_local_time_iso(value: str) -> str:
    try:
        return datetime.fromisoformat(value).astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return value


def digest_due(state: dict, config: dict) -> bool:
    now = datetime.now(LOCAL_TZ)
    return now.hour >= int(config["digest_hour"]) and state["last_digest_date"] != now.date().isoformat()


async def send_digest(state: dict, config: dict, notifier: TelegramNotifier) -> None:
    try:
        state["digest"]["integrity"] = await check_package_integrity(config, notifier)
    except Exception as exc:  # noqa: BLE001 - el digest sale aunque falle el chequeo
        log.exception("Fallo el chequeo de integridad de paquetes")
        state["digest"]["integrity"] = {"ok": False, "error": str(exc)[:300]}
    message, report = render_digest(state["digest"], config)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORTS_DIR / f"batwoman-{datetime.now(LOCAL_TZ).strftime('%Y-%m-%d')}.md"
    report_path.write_text(report, encoding="utf-8")
    await notifier.send(message)
    log.info("Digest diario enviado y guardado en %s", report_path)
    state["digest"] = empty_digest()
    state["last_digest_date"] = datetime.now(LOCAL_TZ).date().isoformat()


# --- Ciclo principal -----------------------------------------------------------------


def initial_state(config: dict) -> Optional[dict]:
    """Primer arranque: se empieza desde el final del archivo para no
    re-notificar el historial del dia. None si no se pudo leer el archivo
    (se reintenta en el proximo poll en vez de arrancar desde el byte 0)."""
    stat = stat_alerts_file(config)
    if stat is None:
        return None
    size, inode = stat
    log.info("Sin estado previo: se empieza a leer alerts.json desde el byte %d", size)
    return {
        "offset": size,
        "size": size,
        "inode": inode,
        "digest": empty_digest(),
        "last_digest_date": datetime.now(LOCAL_TZ).date().isoformat(),
    }


async def process_alert(
    alert: dict,
    state: dict,
    config: dict,
    allowlist: list[dict],
    client: Optional[AsyncTypeSafeClient],
    notifier: TelegramNotifier,
    sudo_buffer: list[dict],
    allow_defer: bool = True,
) -> str:
    """Triage + evaluacion + notificacion de una alerta. Devuelve la
    decision de triage (para el resumen del poll): ademas de las de
    triage(), `explained` (ruta critica explicada por sudo, va al digest) y
    `deferred` (aplazada al proximo poll esperando el sudo)."""
    fields = alert_fields(alert)
    decision_kind, reason = triage(fields, config, allowlist)
    if decision_kind == "allowlisted":
        if fields["level"] >= config["min_level"]:
            state["digest"]["allowlisted"] += 1
        return decision_kind
    if decision_kind == "below_level":
        return decision_kind

    if decision_kind == "critical":
        explanation = find_sudo_explanation(fields, sudo_buffer, config)
        if explanation:
            add_to_digest(state["digest"], fields)
            record_sudo_explained(state["digest"], fields, explanation)
            log.info(
                "Ruta critica %s (%s) explicada por sudo de %s en %s: %s",
                fields["syscheck_path"], fields["syscheck_event"], explanation["user"],
                explanation["tty"], explanation["command"],
            )
            return "explained"
        if allow_defer and should_defer(fields, config):
            state.setdefault("fim_pending", []).append(alert)
            return "deferred"

    add_to_digest(state["digest"], fields)
    if decision_kind == "digest":
        return decision_kind

    critical = decision_kind == "critical"
    decision = await evaluate(client, fields, critical, config)
    log.info(
        "Regla %s nivel %s (%s): amenaza=%.2f severidad=%s accion=%s [%s]",
        fields["rule_id"], fields["level"], reason, decision["is_real_threat"],
        decision["severity"], decision["action"], decision["source"],
    )
    if await act(fields, decision, critical, config, notifier):
        state["digest"]["notified"] += 1
    return decision_kind


async def poll(config: dict, client: Optional[AsyncTypeSafeClient], notifier: TelegramNotifier) -> None:
    state = read_state() or initial_state(config)
    if state is None:
        return
    alerts, state = fetch_new_alerts(config, state)
    allowlist = load_allowlist()
    counts: Counter = Counter()
    # Los sudo del lote entran al buffer antes de procesar nada: la alerta
    # del sudo suele llegar despues que la del FIM que provoco.
    sudo_buffer = update_sudo_buffer(state.get("sudo_buffer") or [], alerts, config)
    state["sudo_buffer"] = sudo_buffer
    # Las aplazadas del poll anterior se resuelven ahora, sin volver a aplazar.
    pending = [(alert, False) for alert in state.pop("fim_pending", None) or []]

    for alert, allow_defer in pending + [(alert, True) for alert in alerts]:
        try:
            counts[await process_alert(alert, state, config, allowlist, client, notifier, sudo_buffer, allow_defer)] += 1
        except Exception:  # noqa: BLE001 - una alerta rara no debe bloquear el resto del lote
            counts["error"] += 1
            log.exception("Fallo procesando la alerta %s", alert.get("id", "?"))

    if alerts:
        log.info("Poll: %d alerta(s) nuevas (%s)", len(alerts), dict(counts))

    if digest_due(state, config):
        await send_digest(state, config, notifier)

    write_state(state)
    update_memory("cycle_complete")


async def dry_run(config: dict, client: Optional[AsyncTypeSafeClient]) -> None:
    """Lee las ultimas alertas e imprime las decisiones de triage, sin
    Telegram ni cambios de estado."""
    alerts = read_tail_alerts(config, DRY_RUN_TAIL_ALERTS)
    allowlist = load_allowlist()
    counts: Counter = Counter()
    digest = empty_digest()
    sudo_buffer = [ev for ev in map(sudo_event, alerts) if ev]
    print(f"=== Batwoman dry-run: {len(alerts)} alerta(s), {len(allowlist)} regla(s) de allowlist ===")
    for alert in alerts:
        fields = alert_fields(alert)
        decision_kind, reason = triage(fields, config, allowlist)
        if decision_kind == "critical":
            explanation = find_sudo_explanation(fields, sudo_buffer, config)
            if explanation:
                decision_kind, reason = "explained", f"sudo: {explanation['command'][:80]}"
        counts[decision_kind] += 1
        target = fields["srcip"] or fields["syscheck_path"]
        line = (
            f"[{format_local_time(fields['timestamp'])}] regla {fields['rule_id']:>6} "
            f"nivel {fields['level']:>2} -> {decision_kind:<11} ({reason}) "
            f"{fields['description'][:60]}" + (f" | {target}" if target else "")
        )
        if decision_kind in ("critical", "evaluate"):
            decision = await evaluate(client, fields, decision_kind == "critical", config)
            line += (
                f"\n    => amenaza={decision['is_real_threat']:.2f} severidad={decision['severity']} "
                f"accion={decision['action']} [{decision['source']}]"
            )
        if decision_kind not in ("allowlisted", "below_level"):
            add_to_digest(digest, fields)
        print(mask_secrets(line))
    print(f"=== Resumen: {dict(counts)} | FIM en digest: {digest['fim_changes']} ===")


async def run(config: dict, args: argparse.Namespace) -> None:
    if args.egress_snapshot:
        await egress.print_snapshot(egress.load_config(config))
        return

    typesafe_available = bool(os.environ.get("TYPESAFE_API_KEY"))
    if not typesafe_available:
        log.warning("TYPESAFE_API_KEY no esta definida, Batwoman opera en modo fallback (sin Jev)")

    async def loop(client: Optional[AsyncTypeSafeClient]) -> None:
        if args.dry_run:
            await dry_run(config, client)
            return

        notifier = TelegramNotifier()
        if not typesafe_available:
            await notifier.send(
                "🦇 Batwoman: <code>TYPESAFE_API_KEY</code> no esta definida. "
                "Triage de alertas en modo fallback, sin TypeSafe/Jev."
            )
        ecfg = egress.load_config(config)
        monitor = egress.EgressMonitor(ecfg) if ecfg["enabled"] else None
        # Con --once no tiene sentido el stream: un solo ciclo con snapshot.
        use_stream = bool(monitor and ecfg["stream"] and not args.once)
        log.info(
            "Batwoman en guardia: poll cada %ss sobre %s; egress %s",
            config["poll_interval"], config["container"],
            (f"cada {ecfg['interval']}s ({'stream conntrack + snapshot de respaldo' if use_stream else 'solo snapshot'})")
            if monitor else "desactivado",
        )
        if use_stream:
            await monitor.start_stream()
        next_poll = next_egress = time.monotonic()
        try:
            while True:
                if time.monotonic() >= next_poll:
                    next_poll = time.monotonic() + config["poll_interval"]
                    try:
                        await poll(config, client, notifier)
                    except Exception:  # noqa: BLE001 - un poll fallido no debe tumbar el servicio
                        log.exception("Fallo el poll de Batwoman, se reintenta en el proximo ciclo")
                if monitor and time.monotonic() >= next_egress:
                    next_egress = time.monotonic() + ecfg["interval"]
                    try:
                        await egress_poll(monitor, config, client, notifier)
                    except Exception:  # noqa: BLE001 - un ciclo de egress fallido no debe tumbar el servicio
                        log.exception("Fallo el ciclo de egress, se reintenta en el proximo")
                if args.once:
                    return
                wake = min(next_poll, next_egress) if monitor else next_poll
                await asyncio.sleep(max(1.0, wake - time.monotonic()))
        finally:
            if monitor:
                await monitor.stop_stream()

    if typesafe_available:
        async with AsyncTypeSafeClient() as client:
            await loop(client)
    else:
        await loop(None)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Batwoman: triage de alertas de Wazuh")
    parser.add_argument("--dry-run", action="store_true", help="ultimas 200 alertas, sin Telegram ni estado")
    parser.add_argument("--once", action="store_true", help="un solo poll (y un ciclo de egress) y termina")
    parser.add_argument(
        "--egress-snapshot", action="store_true",
        help="escucha 60s el stream de conntrack e imprime esos flujos + el snapshot de ss, sin alertar ni guardar",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    load_env()
    config = load_config()
    asyncio.run(run(config, args))


if __name__ == "__main__":
    main()
