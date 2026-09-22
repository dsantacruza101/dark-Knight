"""Modo de Monitoreo: vigilancia nocturna del servidor cada hora.

Cada ciclo (1h):
  - Se filtran las lineas sospechosas del access.log de Nginx (status >= 400
    o patrones de escaneo/exploit) y se evaluan con TypeSafe (Jev), que
    devuelve juicios tipados en vez de texto libre: is_attack, attack_type,
    severity y should_block.
  - Si severity o should_block superan su umbral, se alerta de inmediato por
    Telegram con la IP y el tipo de ataque.
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
import re
import subprocess
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import psutil
from dotenv import load_dotenv
from typesafe_sdk import AsyncTypeSafeClient, Choice, Noul, Score

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = Path("/etc/night-agent.env")
NGINX_LOG = Path("/var/log/nginx/access.log")
NGINX_TAIL_LINES = 1000
KNOWLEDGE_DIR = BASE_DIR / "knowledge"
REPORTS_DIR = BASE_DIR / "reports"
FAIL2BAN_STATE_PATH = BASE_DIR / ".fail2ban_seen_ips.json"
SECURITY_SCAN_EVERY_N_CYCLES = 6

log = logging.getLogger("night_agent.monitoring")

LOG_LINE_RE = re.compile(r'^(?P<ip>\S+) \S+ \S+ \[[^\]]+\] "(?P<request>[^"]*)" (?P<status>\d{3})')
SUSPICIOUS_REQUEST_RE = re.compile(
    r"\.php|wp-admin|wp-login|xmlrpc\.php|\.env|\.git/|phpmyadmin|/etc/passwd|"
    r"/etc/shadow|union(\s+all)?\s+select|<script|\.\./\.\./|base64_decode|"
    r"eval\(|\.sql(\?|$)|\.bak(\?|$)|/admin",
    re.IGNORECASE,
)
TYPESAFE_MAX_LINES_PER_CYCLE = 40
TYPESAFE_CONCURRENCY = 5
SEVERITY_LEVELS = ["info", "low", "medium", "high", "critical"]
ATTACK_TYPE_CRITERIA = {
    "brute_force": "Intentos repetidos de login o fuerza bruta contra credenciales",
    "scan": "Escaneo de rutas, puertos o reconocimiento automatizado",
    "exploit": "Intento de explotar una vulnerabilidad conocida (SQLi, RCE, path traversal, etc.)",
    "normal": "Trafico legitimo sin indicios de ataque",
    "unknown": "Comportamiento sospechoso que no encaja claramente en otra categoria",
}
SEVERITY_ALERT_THRESHOLD = 0.7
BLOCK_ALERT_THRESHOLD = 0.8


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


def parse_log_line(line: str) -> Optional[re.Match]:
    return LOG_LINE_RE.match(line.strip())


def is_suspicious_line(line: str) -> bool:
    match = parse_log_line(line)
    if match is None:
        return False
    request = match.group("request")
    status = match.group("status")
    return status.startswith(("4", "5")) or bool(SUSPICIOUS_REQUEST_RE.search(request))


def select_suspicious_lines(log_lines: list[str]) -> list[tuple[str, str]]:
    """Filtra lineas no-normales y devuelve pares (linea, ip origen)."""
    suspicious: list[tuple[str, str]] = []
    for line in log_lines:
        match = parse_log_line(line)
        if match is None or not is_suspicious_line(line):
            continue
        suspicious.append((line.strip(), match.group("ip")))
    return suspicious


async def evaluate_line_with_typesafe(
    client: AsyncTypeSafeClient, line: str, ip: str
) -> Optional[dict]:
    try:
        response = await client.system_one(
            state={"log_line": line, "source_ip": ip},
            questions={
                "is_attack": Noul(
                    instructions=(
                        "La linea del log de Nginx corresponde a un ataque o "
                        "escaneo malicioso, no a trafico legitimo."
                    )
                ),
                "attack_type": Choice(
                    instructions="Que tipo de actividad describe mejor esta linea del log.",
                    criteria=ATTACK_TYPE_CRITERIA,
                ),
                "severity": Score(
                    instructions=(
                        "Que tan severa es esta linea como amenaza de seguridad "
                        "para el servidor."
                    ),
                    criteria=SEVERITY_LEVELS,
                ),
                "should_block": Noul(
                    instructions="La IP de origen de esta linea deberia bloquearse de inmediato."
                ),
            },
        )
    except Exception as exc:  # noqa: BLE001 - un fallo de TypeSafe no debe tumbar el ciclo
        log.error("Fallo la evaluacion TypeSafe de una linea de log: %s", exc)
        return None

    # Score.score llega en la escala del indice del nivel (0..len(criteria)-1),
    # no normalizado; lo normalizamos a 0..1 para poder compararlo con umbrales.
    severity_raw = response.scores["severity"].score
    severity_idx = max(0, min(len(SEVERITY_LEVELS) - 1, round(severity_raw)))
    severity_normalized = severity_raw / (len(SEVERITY_LEVELS) - 1)
    return {
        "ip": ip,
        "log_line": line,
        "is_attack": response.nouls["is_attack"].noul,
        "attack_type": response.choices["attack_type"].choice,
        "severity_score": severity_normalized,
        "severity_label": SEVERITY_LEVELS[severity_idx],
        "should_block": response.nouls["should_block"].noul,
    }


def format_typesafe_alert(result: dict) -> str:
    return (
        "🚨 *TypeSafe detecto actividad sospechosa*\n"
        f"IP: `{result['ip']}`\n"
        f"Tipo: {result['attack_type']}\n"
        f"Severidad: {result['severity_label']} ({result['severity_score']:.2f})\n"
        f"Bloqueo recomendado: {'si' if result['should_block'] >= 0.5 else 'no'} "
        f"({result['should_block']:.2f})\n"
        f"Linea: `{result['log_line'][:200]}`"
    )


def format_typesafe_summary(total_lines: int, evaluated: list[dict]) -> str:
    header = "### 🔎 Analisis TypeSafe de logs (Nginx)"
    if not evaluated:
        return f"{header}\nSin lineas sospechosas en este ciclo (de {total_lines} revisadas)."

    attacks = [r for r in evaluated if r["is_attack"] >= 0.5]
    to_block = [r for r in evaluated if r["should_block"] >= BLOCK_ALERT_THRESHOLD]
    rows = sorted(evaluated, key=lambda r: r["severity_score"], reverse=True)[:15]

    table = ["| IP | Tipo | Severidad | Bloquear |", "|---|---|---|---|"]
    for r in rows:
        table.append(
            f"| {r['ip']} | {r['attack_type']} | {r['severity_label']} "
            f"({r['severity_score']:.2f}) | {r['should_block']:.2f} |"
        )

    return (
        f"{header}\n"
        f"- Lineas totales revisadas: {total_lines}\n"
        f"- Lineas sospechosas evaluadas: {len(evaluated)}\n"
        f"- Marcadas como ataque (is_attack>=0.5): {len(attacks)}\n"
        f"- IPs recomendadas para bloqueo: {len(to_block)}\n\n" + "\n".join(table)
    )


def save_typesafe_results(timestamp: datetime, results: list[dict]) -> None:
    KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
    path = KNOWLEDGE_DIR / f"typesafe_{timestamp.strftime('%Y-%m-%d_%H-%M')}.json"
    data = {
        "timestamp": timestamp.isoformat(),
        "task": "nginx_log_analysis_typesafe",
        "results": results,
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


async def run_typesafe_log_analysis(notifier, log_lines: list[str], now: datetime) -> str:
    suspicious = select_suspicious_lines(log_lines)
    if not suspicious:
        return format_typesafe_summary(len(log_lines), [])

    if len(suspicious) > TYPESAFE_MAX_LINES_PER_CYCLE:
        log.warning(
            "%d lineas sospechosas superan el limite de %d por ciclo, se evaluan las mas recientes",
            len(suspicious),
            TYPESAFE_MAX_LINES_PER_CYCLE,
        )
        suspicious = suspicious[-TYPESAFE_MAX_LINES_PER_CYCLE:]

    results: list[dict] = []
    sem = asyncio.Semaphore(TYPESAFE_CONCURRENCY)

    async def bounded_eval(client: AsyncTypeSafeClient, line: str, ip: str) -> Optional[dict]:
        async with sem:
            return await evaluate_line_with_typesafe(client, line, ip)

    try:
        async with AsyncTypeSafeClient() as client:
            tasks = [asyncio.create_task(bounded_eval(client, line, ip)) for line, ip in suspicious]
            for coro in asyncio.as_completed(tasks):
                result = await coro
                if result is None:
                    continue
                results.append(result)
                if (
                    result["severity_score"] > SEVERITY_ALERT_THRESHOLD
                    or result["should_block"] > BLOCK_ALERT_THRESHOLD
                ):
                    await notifier.send(format_typesafe_alert(result))
    except Exception as exc:  # noqa: BLE001 - no debe tumbar el ciclo de monitoreo
        log.error("Fallo inicializando el cliente de TypeSafe: %s", exc)
        return f"### 🔎 Analisis TypeSafe de logs (Nginx)\nError al conectar con TypeSafe: {exc}"

    if results:
        save_typesafe_results(now, results)

    return format_typesafe_summary(len(log_lines), results)


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
    typesafe_summary = await run_typesafe_log_analysis(notifier, log_lines, now)

    shared_summary = await run_shared_checks(config, notifier)

    sections = [f"## Ciclo {cycle_num} - {now.strftime('%Y-%m-%d %H:%M:%S')}"]
    sections.append(typesafe_summary)
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
