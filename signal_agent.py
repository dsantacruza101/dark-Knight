#!/usr/bin/env python3
"""Signal (Duke Thomas): protector diurno de la Batifamilia.

Nota de nombre: el modulo se llama `signal_agent.py` y no `signal.py` a
proposito. `signal.py` colisionaria con el modulo estandar `signal` de
Python (usado internamente por `subprocess`, del que dependen `asyncio` y
`python-telegram-bot`), ya que el directorio del script se antepone a
`sys.path` al ejecutarlo directamente. Los archivos systemd si se llaman
`signal.service`/`signal.timer`, que no tienen ese conflicto.

Corre de 6 AM a 10 PM CST (`signal.timer`, dispara a las 06:00
America/El_Salvador) como complemento diurno de Batman, que trabaja de
noche. Cada hora:

  1. Revisa los servicios systemd/Docker del catalogo de Lucius Fox.
  2. Chequea por HTTP dsantacruz.dev, api.dsantacruz.com y
     webhook.dsantacruz.com (status y tiempo de respuesta).
  3. Verifica que el puerto 2222 de ssh.dsantacruz.com este accesible.
  4. Mide CPU/RAM/disco con psutil contra los umbrales de config.yaml.
  5. Revisa las ultimas 100 lineas del access.log de Nginx en busca de 5xx.
  6. Revisa si hay pipelines de GitHub Actions fallidos en los repos de
     config.yaml.

Cada anomalia encontrada se evalua con TypeSafe (Jev): severity
(Score: info/low/medium/high/critical), needs_batman_attention (Noul) y
action (Choice: monitor/alert_alfred/write_batman/escalate). Si
needs_batman_attention > 0.7 se deja contexto para Batman en
batcave/comms.json; si severity > 0.8 se alerta a Alfred de inmediato. Si
TYPESAFE_API_KEY no esta definida, cae a un fallback deterministico segun
el tipo de anomalia (igual que Lucius Fox).

Signal usa Ollama (llama3.2) para redactar el resumen ejecutivo del dia,
no Claude: ese presupuesto se reserva para Batman de noche. Al terminar el
turno deja un resumen para Batman en batcave/comms.json
(`{"from": "signal", "to": "batman", "type": "daily_brief", ...}`) y un
reporte en reports/signal-YYYY-MM-DD.md.
"""

from __future__ import annotations

import asyncio
import html
import itertools
import json
import logging
import os
import re
import socket
import subprocess
import sys
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import psutil
import requests
import yaml
from dotenv import load_dotenv
from github import Github, GithubException
from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import TelegramError
from typesafe_sdk import AsyncTypeSafeClient, Choice, Noul, Score

from batcave import memory_store

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = Path("/etc/night-agent.env")
CONFIG_PATH = BASE_DIR / "config.yaml"
LOG_PATH = BASE_DIR / "signal.log"
COMMS_PATH = BASE_DIR / "batcave" / "comms.json"
REPORTS_DIR = BASE_DIR / "reports"
NGINX_LOG = Path("/var/log/nginx/access.log")

STOP_TIME = "22:00"  # 10 PM CST, hora en que Batman toma el relevo

HTTP_ENDPOINTS = [
    "https://dsantacruz.dev",
    "https://api.dsantacruz.com/api/portfolio/contact-me",
    "https://webhook.dsantacruz.com",
]
HTTP_TIMEOUT_SECONDS = 10
HTTP_SLOW_THRESHOLD_MS = 3000
# Codigos adicionales validos por endpoint (ademas de cualquier 2xx/3xx):
# - /api/portfolio/contact-me exige POST; un GET responde 403, igual que en
#   los pipelines de CI/CD.
# - webhook.dsantacruz.com solo acepta POST con firma HMAC; un GET simple
#   siempre responde 404, no es una falla real.
HTTP_ACCEPTED_STATUS_CODES = {
    "https://api.dsantacruz.com/api/portfolio/contact-me": {403},
    "https://webhook.dsantacruz.com": {404},
}

# ssh.dsantacruz.com solo es accesible via Cloudflare Tunnel, que no
# funciona desde dentro del propio servidor: se chequea localmente.
SSH_HOST = "127.0.0.1"
SSH_PORT = 2222
SSH_TIMEOUT_SECONDS = 5

NGINX_TAIL_LINES = 100
GITHUB_ACTIONS_LOOKBACK_RUNS = 5
GITHUB_ACTIONS_LOOKBACK_HOURS = 24

OLLAMA_TIMEOUT = 120

SEVERITY_LEVELS = ["info", "low", "medium", "high", "critical"]
SEVERITY_ALERT_THRESHOLD = 0.8
BATMAN_ATTENTION_THRESHOLD = 0.7

# Mismo catalogo systemd/docker que lucius_fox.py (se duplica aqui en vez de
# importar el modulo: lucius_fox.py configura logging propio al importarse,
# como monitoring.py, y cada script standalone de este repo se mantiene
# independiente en sus checks basicos).
MONITORED_SERVICES: list[tuple[str, str]] = [
    ("telegram-bot", "systemd"),
    ("cloudflared", "systemd"),
    ("night-agent.timer", "systemd"),
    ("webhook-portfolio", "systemd"),
    ("ollama", "systemd"),
    ("daniel-portfolio-app", "docker"),
    ("portfolioservicelauncher-client-gateway-1", "docker"),
    ("portfolioservicelauncher-nodemailer-micro-service-1", "docker"),
    ("portfolioservicelauncher-nats-server-1", "docker"),
    ("sonarqube", "docker"),
]

LOG_LINE_RE = re.compile(r'^(?P<ip>\S+) \S+ \S+ \[[^\]]+\] "(?P<request>[^"]*)" (?P<status>\d{3})')

ACTION_CRITERIA = {
    "monitor": "La anomalia es menor, basta con registrarla en el log y el reporte diurno.",
    "alert_alfred": "Hay que avisarle a Alfred de inmediato: afecta disponibilidad o rendimiento de forma notoria.",
    "write_batman": "Batman deberia enterarse al iniciar su turno nocturno, no amerita interrumpir a Alfred ahora.",
    "escalate": "Requiere atencion inmediata de Alfred y ademas dejar contexto para Batman.",
}
HIGH_SEVERITY_TYPES = {"service_down", "http_unreachable", "ssh_unreachable", "nginx_5xx"}
BATMAN_ATTENTION_TYPES = {"service_down", "http_unreachable", "ssh_unreachable"}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.FileHandler(LOG_PATH), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("signal_agent")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpx2").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


def load_env() -> None:
    if "TELEGRAM_BOT_TOKEN" in os.environ:
        return
    if ENV_PATH.exists():
        load_dotenv(ENV_PATH)
    else:
        log.warning("No se encontro %s, se usan las variables de entorno actuales", ENV_PATH)


def load_config(path: Path = CONFIG_PATH) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


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
                chat_id=self.chat_id, text=text, parse_mode=ParseMode.HTML
            )
        except TelegramError as exc:
            log.error("No se pudo enviar mensaje a Telegram: %s", exc)


def read_comms() -> list[dict]:
    try:
        return json.loads(COMMS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []


def write_comms(entries: list[dict]) -> None:
    COMMS_PATH.parent.mkdir(parents=True, exist_ok=True)
    COMMS_PATH.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")


def append_comm(entry: dict) -> None:
    entries = read_comms()
    entries.append(entry)
    write_comms(entries)


def update_memory(event: str, **kwargs) -> None:
    """Actualiza batcave/memory/signal.md segun el resultado del ciclo."""
    try:
        if event == "cycle_complete":
            memory_store.touch_last_activity("signal")
            memory_store.bump_stat("signal", "ciclos")
        elif event == "anomaly":
            memory_store.append_error(
                "signal",
                error=kwargs["description"],
                causa=kwargs.get("tipo", "anomalia detectada"),
                solucion=kwargs.get("accion", "monitoreada"),
                resultado=f"severidad {kwargs.get('severidad')}",
            )
            if kwargs.get("escalated_to_alfred"):
                memory_store.bump_stat("signal", "escalaciones")
        elif event == "self_report":
            memory_store.append_error(
                "signal",
                error=str(kwargs.get("error")),
                causa="excepcion no manejada",
                solucion="se pidio auto-reparacion a Lucius",
                resultado="pendiente de Lucius",
            )
            memory_store.bump_stat("signal", "escalaciones")
    except OSError as exc:
        log.warning("No se pudo actualizar la memoria de Signal: %s", exc)


def check_messages_for_signal() -> list[dict]:
    """Lee mensajes que Batman o Lucius hayan dejado para Signal y los marca como leidos."""
    entries = read_comms()
    pending = [e for e in entries if e.get("to") == "signal" and not e.get("read", False)]
    if pending:
        for entry in pending:
            entry["read"] = True
        write_comms(entries)
    return pending


def _run(cmd: list[str], timeout: int = 30) -> str:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return proc.stdout.strip() or proc.stderr.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("Fallo ejecutando %s: %s", " ".join(cmd), exc)
        return ""


def check_systemd(name: str) -> bool:
    return _run(["systemctl", "is-active", name]) == "active"


def check_docker(name: str) -> bool:
    return _run(["docker", "inspect", "-f", "{{.State.Running}}", name]) == "true"


CHECK_FNS = {"systemd": check_systemd, "docker": check_docker}


def check_monitored_services() -> list[dict]:
    anomalies = []
    for name, kind in MONITORED_SERVICES:
        if not CHECK_FNS[kind](name):
            anomalies.append(
                {
                    "type": "service_down",
                    "name": name,
                    "description": f"{name} ({kind}) esta caido",
                    "details": {"kind": kind},
                }
            )
    return anomalies


def check_http_endpoints() -> tuple[list[dict], list[str]]:
    anomalies: list[dict] = []
    summary_lines: list[str] = []
    for url in HTTP_ENDPOINTS:
        try:
            start = time.monotonic()
            resp = requests.get(url, timeout=HTTP_TIMEOUT_SECONDS)
            elapsed_ms = (time.monotonic() - start) * 1000
            summary_lines.append(f"{url}: {resp.status_code} ({elapsed_ms:.0f}ms)")
            accepted_codes = HTTP_ACCEPTED_STATUS_CODES.get(url, set())
            is_accepted = resp.status_code < 400 or resp.status_code in accepted_codes
            if not is_accepted:
                anomalies.append(
                    {
                        "type": "http_error",
                        "name": url,
                        "description": f"{url} respondio {resp.status_code}",
                        "details": {"status_code": resp.status_code, "response_time_ms": elapsed_ms},
                    }
                )
            elif elapsed_ms > HTTP_SLOW_THRESHOLD_MS:
                anomalies.append(
                    {
                        "type": "http_slow",
                        "name": url,
                        "description": f"{url} respondio lento: {elapsed_ms:.0f}ms",
                        "details": {"status_code": resp.status_code, "response_time_ms": elapsed_ms},
                    }
                )
        except requests.RequestException as exc:
            summary_lines.append(f"{url}: error ({exc})")
            anomalies.append(
                {
                    "type": "http_unreachable",
                    "name": url,
                    "description": f"{url} no responde: {exc}",
                    "details": {"error": str(exc)},
                }
            )
    return anomalies, summary_lines


def check_ssh_port() -> tuple[Optional[dict], str]:
    target = f"{SSH_HOST}:{SSH_PORT}"
    try:
        with socket.create_connection((SSH_HOST, SSH_PORT), timeout=SSH_TIMEOUT_SECONDS):
            return None, f"{target} accesible"
    except OSError as exc:
        anomaly = {
            "type": "ssh_unreachable",
            "name": target,
            "description": f"No se pudo conectar a {target}: {exc}",
            "details": {"error": str(exc)},
        }
        return anomaly, f"{target} inaccesible ({exc})"




def check_resources(config: dict) -> tuple[list[dict], str]:
    thresholds = config["thresholds"]
    cpu = psutil.cpu_percent(interval=1)
    ram = psutil.virtual_memory().percent
    disk = psutil.disk_usage("/").percent

    anomalies = []
    if cpu >= thresholds["cpu_alert"]:
        anomalies.append(
            {
                "type": "cpu_high",
                "name": "cpu",
                "description": f"CPU alta: {cpu:.1f}% (umbral {thresholds['cpu_alert']}%)",
                "details": {"value": cpu},
            }
        )
    if ram >= thresholds["ram_alert"]:
        anomalies.append(
            {
                "type": "ram_high",
                "name": "ram",
                "description": f"RAM alta: {ram:.1f}% (umbral {thresholds['ram_alert']}%)",
                "details": {"value": ram},
            }
        )
    if disk >= thresholds["disk_alert"]:
        anomalies.append(
            {
                "type": "disk_high",
                "name": "disk",
                "description": f"Disco alto: {disk:.1f}% (umbral {thresholds['disk_alert']}%)",
                "details": {"value": disk},
            }
        )
    return anomalies, f"CPU {cpu:.1f}% | RAM {ram:.1f}% | Disco {disk:.1f}%"


def read_log_tail(path: Path, n: int = NGINX_TAIL_LINES) -> list[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return list(deque(fh, maxlen=n))
    except OSError as exc:
        log.warning("No se pudo leer %s: %s", path, exc)
        return []


def check_nginx_errors() -> tuple[Optional[dict], str]:
    lines = read_log_tail(NGINX_LOG)
    error_lines = []
    for line in lines:
        match = LOG_LINE_RE.match(line.strip())
        if match and match.group("status").startswith("5"):
            error_lines.append(line.strip())

    if not error_lines:
        return None, f"Sin errores 5xx en las ultimas {len(lines)} lineas de Nginx"

    anomaly = {
        "type": "nginx_5xx",
        "name": "nginx",
        "description": f"{len(error_lines)} error(es) 5xx en las ultimas {len(lines)} lineas de Nginx",
        "details": {"count": len(error_lines), "sample": error_lines[-5:]},
    }
    return anomaly, anomaly["description"]


def check_github_actions(config: dict) -> list[dict]:
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        log.warning("GITHUB_TOKEN no esta definido, se omite la revision de GitHub Actions")
        return []

    anomalies: list[dict] = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=GITHUB_ACTIONS_LOOKBACK_HOURS)
    gh = Github(token)
    for repo_name in config["github"]["repos"]:
        try:
            repo = gh.get_repo(repo_name)
            runs = repo.get_workflow_runs()
            for run in itertools.islice(runs, GITHUB_ACTIONS_LOOKBACK_RUNS):
                if run.created_at < cutoff:
                    continue
                if run.conclusion == "failure":
                    anomalies.append(
                        {
                            "type": "github_actions_failure",
                            "name": f"{repo_name}#{run.run_number}",
                            "description": (
                                f"Pipeline fallido en {repo_name}: {run.name} (run #{run.run_number})"
                            ),
                            "html_description": (
                                f"Pipeline fallido en <code>{html.escape(repo_name)}</code>: "
                                f"{html.escape(run.name)} (run #{run.run_number})"
                            ),
                            "details": {
                                "repo": repo_name,
                                "workflow": run.name,
                                "url": run.html_url,
                            },
                        }
                    )
        except GithubException as exc:
            log.error("Error consultando GitHub Actions en %s: %s", repo_name, exc)
    return anomalies


def fallback_decision(anomaly: dict) -> dict:
    severity_label = "high" if anomaly["type"] in HIGH_SEVERITY_TYPES else "medium"
    needs_batman = anomaly["type"] in BATMAN_ATTENTION_TYPES
    action = "alert_alfred" if severity_label == "high" else ("write_batman" if needs_batman else "monitor")
    return {
        "severity_score": SEVERITY_LEVELS.index(severity_label) / (len(SEVERITY_LEVELS) - 1),
        "severity_label": severity_label,
        "needs_batman_attention": 1.0 if needs_batman else 0.0,
        "action": action,
    }


async def evaluate_with_typesafe(client: AsyncTypeSafeClient, anomaly: dict) -> dict:
    try:
        response = await client.system_one(
            state={
                "anomaly_type": anomaly["type"],
                "name": anomaly["name"],
                "description": anomaly["description"],
                "details": anomaly.get("details", {}),
            },
            questions={
                "severity": Score(
                    instructions=(
                        "Que tan severa es esta anomalia para la disponibilidad o el "
                        "rendimiento del servidor."
                    ),
                    criteria=SEVERITY_LEVELS,
                ),
                "needs_batman_attention": Noul(
                    instructions=(
                        "Batman, el agente nocturno, deberia enterarse de esta anomalia al "
                        "iniciar su turno porque podria requerir intervencion mas profunda."
                    )
                ),
                "action": Choice(
                    instructions="Que accion es mas apropiada para esta anomalia.",
                    criteria=ACTION_CRITERIA,
                ),
            },
        )
    except Exception as exc:  # noqa: BLE001 - un fallo de TypeSafe no debe tumbar el ciclo
        log.error("Fallo la evaluacion TypeSafe de %s: %s", anomaly["name"], exc)
        return fallback_decision(anomaly)

    severity_raw = response.scores["severity"].score
    severity_idx = max(0, min(len(SEVERITY_LEVELS) - 1, round(severity_raw)))
    return {
        "severity_score": severity_raw / (len(SEVERITY_LEVELS) - 1),
        "severity_label": SEVERITY_LEVELS[severity_idx],
        "needs_batman_attention": response.nouls["needs_batman_attention"].noul,
        "action": response.choices["action"].choice,
    }


async def evaluate_anomalies(anomalies: list[dict], client: Optional[AsyncTypeSafeClient]) -> list[dict]:
    evaluated = []
    for anomaly in anomalies:
        decision = await evaluate_with_typesafe(client, anomaly) if client is not None else fallback_decision(anomaly)
        evaluated.append({**anomaly, **decision})
    return evaluated


async def act_on_anomaly(anomaly: dict, notifier: TelegramNotifier, incidents: list[dict]) -> None:
    targets: set[str] = set()
    if anomaly["severity_score"] > SEVERITY_ALERT_THRESHOLD:
        targets.add("alfred")
    if anomaly["needs_batman_attention"] > BATMAN_ATTENTION_THRESHOLD:
        targets.add("batman")

    action = anomaly.get("action")
    if action == "alert_alfred":
        targets.add("alfred")
    elif action == "write_batman":
        targets.add("batman")
    elif action == "escalate":
        targets.update({"alfred", "batman"})

    if "alfred" in targets:
        description = anomaly.get("html_description") or html.escape(anomaly["description"])
        await notifier.send(
            f"☀️ <b>Signal</b>\n{description}\n"
            f"Severidad: {anomaly['severity_label']} ({anomaly['severity_score']:.2f})"
        )
    if "batman" in targets:
        append_comm(
            {
                "timestamp": datetime.now().isoformat(),
                "from": "signal",
                "to": "batman",
                "service": anomaly["name"],
                "priority": anomaly["severity_label"],
                "message": anomaly["description"],
                "read": False,
            }
        )
    if not targets:
        log.info("%s: anomalia de severidad %s, solo se deja registro en el log", anomaly["name"], anomaly["severity_label"])

    incidents.append(
        {
            "type": anomaly["type"],
            "name": anomaly["name"],
            "description": anomaly["description"],
            "severity": anomaly["severity_label"],
            "notified": sorted(targets),
        }
    )
    update_memory(
        "anomaly",
        description=anomaly["description"],
        tipo=anomaly["type"],
        accion=anomaly.get("action"),
        severidad=anomaly["severity_label"],
        escalated_to_alfred="alfred" in targets,
    )


def run_ollama(model: str, prompt: str, timeout: int = OLLAMA_TIMEOUT) -> Optional[str]:
    try:
        proc = subprocess.run(
            ["ollama", "run", model],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.error("Fallo ejecutando ollama: %s", exc)
        return None
    if proc.returncode != 0:
        log.error("ollama devolvio codigo %s: %s", proc.returncode, proc.stderr[-500:])
        return None
    return proc.stdout.strip() or None


def summarize_day_with_ollama(model: str, incidents: list[dict], cycles_completed: int) -> str:
    if not incidents:
        return f"Vigilancia diurna sin incidentes en {cycles_completed} ciclo(s)."

    prompt = (
        "Eres Signal, el vigilante diurno de un servidor. Resume en 3-4 lineas, "
        "en espanol y dirigido a Batman (que empieza su turno nocturno ahora), "
        "los siguientes incidentes detectados hoy:\n\n"
        + "\n".join(f"- [{i['severity']}] {i['description']}" for i in incidents)
    )
    output = run_ollama(model, prompt)
    if output:
        return output
    return f"{len(incidents)} incidente(s) detectados hoy (ver reporte reports/signal-*.md)."


async def run_hourly_cycle(
    config: dict,
    client: Optional[AsyncTypeSafeClient],
    notifier: TelegramNotifier,
    cycle_num: int,
    incidents: list[dict],
) -> str:
    now = datetime.now()
    log.info("Signal: iniciando ciclo #%d", cycle_num)

    anomalies: list[dict] = []
    sections = [f"## Ciclo {cycle_num} - {now.strftime('%Y-%m-%d %H:%M:%S')}"]

    service_anomalies = check_monitored_services()
    anomalies += service_anomalies
    sections.append(
        "### Servicios (catalogo de Lucius Fox)\n"
        + ("Todos operativos." if not service_anomalies else "\n".join(f"- {a['description']}" for a in service_anomalies))
    )

    http_anomalies, http_summary = check_http_endpoints()
    anomalies += http_anomalies
    sections.append("### Endpoints HTTP\n" + "\n".join(http_summary))

    ssh_anomaly, ssh_summary = check_ssh_port()
    if ssh_anomaly:
        anomalies.append(ssh_anomaly)
    sections.append("### SSH\n" + ssh_summary)

    resource_anomalies, resource_summary = check_resources(config)
    anomalies += resource_anomalies
    sections.append("### Recursos\n" + resource_summary)

    nginx_anomaly, nginx_summary = check_nginx_errors()
    if nginx_anomaly:
        anomalies.append(nginx_anomaly)
    sections.append("### Nginx\n" + nginx_summary)

    gh_anomalies = check_github_actions(config)
    anomalies += gh_anomalies
    sections.append(
        "### GitHub Actions\n"
        + ("Sin pipelines fallidos." if not gh_anomalies else "\n".join(f"- {a['description']}" for a in gh_anomalies))
    )

    if anomalies:
        evaluated = await evaluate_anomalies(anomalies, client)
        for anomaly in evaluated:
            await act_on_anomaly(anomaly, notifier, incidents)
        sections.append(
            "### Anomalias evaluadas (TypeSafe)\n"
            + "\n".join(
                f"- [{a['severity_label']}] {a['description']} "
                f"(batman={a['needs_batman_attention']:.2f}, accion={a['action']})"
                for a in evaluated
            )
        )
    else:
        log.info("Ciclo #%d sin anomalias", cycle_num)

    return "\n\n".join(sections)


def write_daily_report(cycles: list[str]) -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    report_path = REPORTS_DIR / f"signal-{today}.md"
    header = f"# Reporte de vigilancia diurna (Signal) - {today}\n"
    report_path.write_text(header + "\n" + "\n\n---\n\n".join(cycles) + "\n", encoding="utf-8")
    return report_path


async def report_self_repair_needed(error: Exception, notifier: TelegramNotifier) -> None:
    """Pide ayuda a Lucius Fox ante una excepcion no manejada o error critico."""
    append_comm(
        {
            "timestamp": datetime.now().isoformat(),
            "from": "signal",
            "to": "lucius",
            "type": "self_repair_needed",
            "error": str(error),
            "service": "signal.service",
            "read": False,
        }
    )
    update_memory("self_report", error=error)
    await notifier.send("☀️ Signal encontró un error, pidiendo ayuda a Lucius")


def write_daily_brief(incidents: list[dict], summary: str) -> None:
    append_comm(
        {
            "timestamp": datetime.now().isoformat(),
            "from": "signal",
            "to": "batman",
            "type": "daily_brief",
            "summary": summary,
            "incidents": incidents,
            "read": False,
        }
    )


def _stop_datetime(reference: datetime) -> datetime:
    hh, mm = (int(part) for part in STOP_TIME.split(":"))
    return reference.replace(hour=hh, minute=mm, second=0, microsecond=0)


async def run_signal(config: dict, notifier: TelegramNotifier) -> None:
    for message in check_messages_for_signal():
        log.info("Mensaje para Signal: %s", message.get("message"))

    start = datetime.now()
    stop_at = _stop_datetime(start)
    log.info("Signal: vigilancia diurna activa hasta %s", stop_at)
    await notifier.send("☀️ <b>Signal iniciado</b> — Vigilancia diurna activa")

    typesafe_available = bool(os.environ.get("TYPESAFE_API_KEY"))
    if not typesafe_available:
        log.warning("TYPESAFE_API_KEY no esta definida, Signal opera en modo fallback (sin Jev)")

    cycles: list[str] = []
    incidents: list[dict] = []
    cycle_num = 0

    async def run_cycles(client: Optional[AsyncTypeSafeClient]) -> None:
        nonlocal cycle_num
        while True:
            cycle_num += 1
            cycles.append(await run_hourly_cycle(config, client, notifier, cycle_num, incidents))
            write_daily_report(cycles)
            update_memory("cycle_complete")
            if datetime.now() >= stop_at:
                break
            await asyncio.sleep(3600)

    if typesafe_available:
        async with AsyncTypeSafeClient() as client:
            await run_cycles(client)
    else:
        await run_cycles(None)

    ollama_model = config["models"]["ollama"]
    summary = summarize_day_with_ollama(ollama_model, incidents, cycle_num)
    write_daily_brief(incidents, summary)

    log.info("Signal: vigilancia diurna completada (%d ciclos, %d incidentes)", cycle_num, len(incidents))
    await notifier.send("☀️ <b>Signal completado</b> — Batman toma el relevo")


async def main() -> None:
    load_env()
    notifier = TelegramNotifier()

    try:
        config = load_config()
    except (OSError, yaml.YAMLError) as exc:
        log.exception("No se pudo cargar config.yaml")
        await notifier.send(
            f"☀️ <b>Signal encontró un problema</b>: no pudo iniciar (error leyendo config.yaml)\n"
            f"<code>{html.escape(str(exc))}</code>"
        )
        await report_self_repair_needed(exc, notifier)
        sys.exit(1)

    try:
        await run_signal(config, notifier)
    except Exception as exc:  # noqa: BLE001 - notificar cualquier fallo antes de salir
        log.exception("Fallo Signal")
        await notifier.send(
            f"☀️ <b>Signal encontró un problema</b>\n"
            f"<code>{html.escape(type(exc).__name__)}: {html.escape(str(exc))}</code>\n"
            f"Revisa {LOG_PATH}"
        )
        await report_self_repair_needed(exc, notifier)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
