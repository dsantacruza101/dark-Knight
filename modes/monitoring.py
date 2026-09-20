"""Modo de Monitoreo: vigilancia nocturna del servidor cada hora.

Cada ciclo (1h):
  - Oracle (Claude) analiza el access.log de Nginx y genera conocimiento de calidad.
  - Ollama hace el mismo analisis para comparar y aprender del ejemplo de Oracle.
  - Se revisan containers, RAM, disco y fail2ban; cualquier umbral superado
    dispara una alerta inmediata por Telegram.

Cada 6 ciclos se corre ademas un escaneo antivirus (clamscan).

Al terminar la noche (hora de `schedule.stop` en config.yaml) se deja un
reporte consolidado en reports/YYYY-MM-DD.md.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import psutil
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = Path("/etc/night-agent.env")
NGINX_LOG = Path("/var/log/nginx/access.log")
NGINX_TAIL_LINES = 1000
KNOWLEDGE_DIR = BASE_DIR / "knowledge"
CLAUDE_EXAMPLES_DIR = KNOWLEDGE_DIR / "claude_examples"
REPORTS_DIR = BASE_DIR / "reports"
FAIL2BAN_STATE_PATH = BASE_DIR / ".fail2ban_seen_ips.json"
SECURITY_SCAN_EVERY_N_CYCLES = 6

log = logging.getLogger("night_agent.monitoring")

LOG_ANALYSIS_PROMPT = (
    "Analiza estas ultimas lineas del access.log de Nginx. Genera un reporte "
    "en markdown con estas secciones: IPs repetidas o con comportamiento "
    "sospechoso, rutas inusuales o propias de escaneo/bots, y picos de "
    "errores 4xx/5xx. Se conciso y estructurado. Si no encuentras nada "
    "sospechoso, dilo explicitamente.\n\n"
    "```\n{log_sample}\n```"
)


def load_env() -> None:
    if "TELEGRAM_BOT_TOKEN" in os.environ:
        return
    if ENV_PATH.exists():
        load_dotenv(ENV_PATH)
    else:
        log.warning("No se encontro %s, se usan las variables de entorno actuales", ENV_PATH)


def _next_stop_datetime(config: dict, reference: datetime) -> datetime:
    hh, mm = (int(part) for part in config["schedule"]["stop"].split(":"))
    stop_dt = reference.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if stop_dt <= reference:
        stop_dt += timedelta(days=1)
    return stop_dt


def read_log_tail(path: Path, n: int = NGINX_TAIL_LINES) -> list[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return list(deque(fh, maxlen=n))
    except OSError as exc:
        log.warning("No se pudo leer %s: %s", path, exc)
        return []


def run_claude_log_analysis(claude_bin: str, log_lines: list[str]) -> Optional[str]:
    if not log_lines:
        return None
    prompt = LOG_ANALYSIS_PROMPT.format(log_sample="".join(log_lines))
    try:
        proc = subprocess.run(
            [claude_bin, "-p", prompt], capture_output=True, text=True, timeout=600
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.error("Fallo el analisis de logs con claude: %s", exc)
        return None
    if proc.returncode != 0:
        log.error("claude devolvio codigo %s: %s", proc.returncode, proc.stderr[-500:])
        return None
    return proc.stdout.strip() or None


def run_ollama_log_analysis(ollama_model: str, log_lines: list[str]) -> Optional[str]:
    if not log_lines:
        return None
    prompt = LOG_ANALYSIS_PROMPT.format(log_sample="".join(log_lines))
    try:
        proc = subprocess.run(
            ["ollama", "run", ollama_model],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=600,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.error("Fallo el analisis de logs con ollama: %s", exc)
        return None
    if proc.returncode != 0:
        log.error("ollama devolvio codigo %s: %s", proc.returncode, proc.stderr[-500:])
        return None
    return proc.stdout.strip() or None


def save_claude_example(timestamp: datetime, claude_report: str) -> None:
    CLAUDE_EXAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    path = CLAUDE_EXAMPLES_DIR / f"{timestamp.strftime('%Y-%m-%d_%H-%M')}.md"
    path.write_text(
        f"# Analisis de access.log - {timestamp.isoformat()}\n\n{claude_report}\n",
        encoding="utf-8",
    )


def save_knowledge_pair(
    timestamp: datetime, claude_report: Optional[str], ollama_report: Optional[str]
) -> None:
    KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
    path = KNOWLEDGE_DIR / f"pair_{timestamp.strftime('%Y-%m-%d_%H-%M')}.json"
    data = {
        "timestamp": timestamp.isoformat(),
        "task": "nginx_log_analysis",
        "claude_output": claude_report,
        "ollama_output": ollama_report,
        "quality_score": 1.0 if claude_report and ollama_report else 0.0,
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_seen_banned_ips() -> set[str]:
    try:
        return set(json.loads(FAIL2BAN_STATE_PATH.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return set()


def _save_seen_banned_ips(ips: set[str]) -> None:
    FAIL2BAN_STATE_PATH.write_text(json.dumps(sorted(ips)), encoding="utf-8")


def _run(cmd: list[str], timeout: int = 30) -> str:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return proc.stdout.strip() or proc.stderr.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("Fallo ejecutando %s: %s", " ".join(cmd), exc)
        return f"(error ejecutando {cmd[0]}: {exc})"


def _get_banned_ips(fail2ban_status: str) -> set[str]:
    ips: set[str] = set()
    for line in fail2ban_status.splitlines():
        line = line.strip()
        if line.startswith("|") and "Banned IP list" in line:
            ips.update(line.split(":", 1)[-1].split())
    return ips


async def run_shared_checks(config: dict, notifier) -> str:
    thresholds = config["thresholds"]
    alerts: list[str] = []

    docker_out = _run(["docker", "ps", "--format", "{{.Names}}: {{.Status}}"])
    unhealthy = [
        line for line in docker_out.splitlines() if "unhealthy" in line.lower()
    ]
    if unhealthy:
        alerts.append("🚨 Containers no saludables:\n" + "\n".join(unhealthy))

    free_out = _run(["free", "-h"])
    df_out = _run(["df", "-h"])

    ram = psutil.virtual_memory().percent
    disk = psutil.disk_usage("/").percent
    if ram >= thresholds["ram_alert"]:
        alerts.append(f"⚠️ RAM alta: {ram:.1f}% (umbral {thresholds['ram_alert']}%)")
    if disk >= thresholds["disk_alert"]:
        alerts.append(f"⚠️ Disco alto: {disk:.1f}% (umbral {thresholds['disk_alert']}%)")

    fail2ban_status = _run(["sudo", "fail2ban-client", "status"])
    jail_names = []
    for line in fail2ban_status.splitlines():
        if "Jail list" in line:
            jail_names = [j.strip() for j in line.split(":", 1)[-1].split(",") if j.strip()]

    all_banned: set[str] = set()
    for jail in jail_names:
        jail_status = _run(["sudo", "fail2ban-client", "status", jail])
        all_banned |= _get_banned_ips(jail_status)

    seen = _load_seen_banned_ips()
    new_bans = all_banned - seen
    if new_bans:
        alerts.append("🚫 Nuevas IPs baneadas por fail2ban:\n" + "\n".join(sorted(new_bans)))
    _save_seen_banned_ips(all_banned)

    if alerts:
        await notifier.send("🚨 *Alerta de Batman:*\n\n" + "\n\n".join(alerts))

    return (
        "**docker ps:**\n```\n" + (docker_out or "(sin containers)") + "\n```\n\n"
        "**free -h:**\n```\n" + free_out + "\n```\n\n"
        "**df -h:**\n```\n" + df_out + "\n```\n\n"
        "**fail2ban status:**\n```\n" + fail2ban_status + "\n```"
    )


async def run_security_scan(config: dict, notifier) -> str:
    log.info("Iniciando escaneo de seguridad (clamscan)")
    output = _run(["sudo", "clamscan", "-r", "/home", "/var/www", "--infected"], timeout=3600)
    infected = [line for line in output.splitlines() if "FOUND" in line]

    header = "### 🛡️ Escaneo de seguridad (clamscan)"
    if infected:
        body = f"🚨 {len(infected)} archivo(s) infectado(s):\n" + "\n".join(infected)
        await notifier.send(f"🚨 *Alerta de Batman:*\n\n{body}")
    else:
        body = "Sin hallazgos. Todo limpio."

    return f"{header}\n{body}"


async def run_hourly_cycle(config: dict, notifier, cycle_num: int) -> str:
    now = datetime.now()
    log.info("Iniciando ciclo de monitoreo #%d", cycle_num)

    log_lines = read_log_tail(NGINX_LOG)
    claude_report = run_claude_log_analysis(config["models"]["claude"], log_lines)
    ollama_report = run_ollama_log_analysis(config["models"]["ollama"], log_lines)

    if claude_report:
        save_claude_example(now, claude_report)
    if claude_report or ollama_report:
        save_knowledge_pair(now, claude_report, ollama_report)

    shared_summary = await run_shared_checks(config, notifier)

    sections = [f"## Ciclo {cycle_num} - {now.strftime('%Y-%m-%d %H:%M:%S')}"]
    sections.append(
        "### Oracle analizando...\n"
        + (claude_report or "No disponible (sin lineas de log o fallo la ejecucion).")
    )
    sections.append(
        "### Ollama aprendiendo de Oracle...\n"
        + (ollama_report or "No disponible (sin lineas de log o fallo la ejecucion).")
    )
    sections.append("### Estado del servidor\n" + shared_summary)
    return "\n\n".join(sections)


def write_daily_report(cycles: list[str]) -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    report_path = REPORTS_DIR / f"{today}.md"
    header = f"# Reporte de monitoreo nocturno - {today}\n"
    report_path.write_text(header + "\n" + "\n\n---\n\n".join(cycles) + "\n", encoding="utf-8")
    return report_path


async def run_monitor_mode(config: dict, notifier) -> str:
    """Punto de entrada del modo Monitoreo: corre en loop hasta schedule.stop."""
    load_env()

    start = datetime.now()
    stop_at = _next_stop_datetime(config, start)
    log.info("Monitoreo nocturno activo hasta %s", stop_at)

    cycles: list[str] = []
    cycle_num = 0
    while True:
        cycle_num += 1
        cycles.append(await run_hourly_cycle(config, notifier, cycle_num))
        if cycle_num % SECURITY_SCAN_EVERY_N_CYCLES == 0:
            cycles.append(await run_security_scan(config, notifier))
        write_daily_report(cycles)

        if datetime.now() >= stop_at:
            break
        await asyncio.sleep(3600)

    return f"Monitoreo nocturno completado: {cycle_num} ciclo(s) ejecutados."
