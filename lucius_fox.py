#!/usr/bin/env python3
"""Lucius Fox: guardian de la infraestructura de la Batifamilia.

Corre cada 30 minutos como servicio systemd (lucius-fox.service +
lucius-fox.timer) y revisa el catalogo de servicios criticos del servidor
(systemd, containers Docker, el binario de Claude Code y la propia API de
TypeSafe). Por cada servicio caido:

  1. Le pregunta a TypeSafe (Jev) si el servicio puede auto-repararse
     (Noul), que prioridad tiene (Choice: critical/high/medium/low) y a
     quien avisar (Choice: alfred/batman/both/none).
  2. Si puede auto-repararse, lo intenta hasta 3 veces con la accion de
     reparacion correspondiente (restart systemd, docker start, o
     reinstalar el binario de Claude Code).
  3. Si repara con exito, avisa a Alfred por Telegram.
  4. Si no logra repararlo, escala segun la prioridad: critico avisa a
     Alfred de inmediato; alto avisa a Alfred y deja un mensaje para Batman
     en batcave/comms.json; medio solo se registra en el log.

Si TYPESAFE_API_KEY no esta definida, Lucius no puede consultar a Jev y cae
a un modo fallback determinista basado en la prioridad base de cada
servicio, avisando a Alfred del problema.

Lucius tambien revisa batcave/comms.json al iniciar por si Batman le dejo
algun mensaje (p. ej. una amenaza de seguridad detectada de noche), o si
Signal (el vigilante diurno) pidio auto-reparacion tras un error critico:
en ese caso le pregunta a TypeSafe si puede reparar signal.service con un
restart y, si TypeSafe lo aprueba (can_repair > 0.7), lo hace y avisa a
Alfred.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import TelegramError
from typesafe_sdk import AsyncTypeSafeClient, Choice, Noul

from batcave import memory_store

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = Path("/etc/night-agent.env")
LOG_PATH = BASE_DIR / "lucius_fox.log"
COMMS_PATH = BASE_DIR / "batcave" / "comms.json"

CLAUDE_NODE_BIN = "/home/dsantacruz/.nvm/versions/node/v22.23.2/bin/node"
CLAUDE_INSTALL_SCRIPT = (
    "/home/dsantacruz/.nvm/versions/node/v22.23.2/lib/node_modules/"
    "@anthropic-ai/claude-code/install.cjs"
)

MAX_REPAIR_ATTEMPTS = 3
REPAIR_RETRY_DELAY_SECONDS = 5

# Signal es un vigilante diurno (6 AM-10 PM CST), no un servicio 24/7: fuera
# de ese horario terminar es el comportamiento esperado, no una caida.
SIGNAL_SERVICE_NAME = "signal.service"
SIGNAL_OPERATING_HOURS_UTC = (12, 22)  # 6 AM-4 PM CST == 12:00-22:00 UTC (no cruza medianoche)

PRIORITIES = ("critical", "high", "medium", "low")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.FileHandler(LOG_PATH), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("lucius_fox")


@dataclass(frozen=True)
class ServiceCheck:
    name: str
    kind: str  # "systemd" | "docker" | "binary"
    baseline_priority: str
    description: str


CATALOG: list[ServiceCheck] = [
    ServiceCheck("telegram-bot", "systemd", "critical", "Bot de Telegram Alfred, canal de comunicacion de la Batifamilia"),
    ServiceCheck("cloudflared", "systemd", "critical", "Tunel de Cloudflare, unico acceso remoto al servidor"),
    ServiceCheck("night-agent.timer", "systemd", "high", "Timer que dispara a Batman cada noche a las 22:00"),
    ServiceCheck("webhook-portfolio", "systemd", "high", "Servidor de webhooks que dispara los deploys via Docker"),
    ServiceCheck("signal.service", "systemd", "high", "Vigilante diurno Signal, complemento de Batman en horario diurno"),
    ServiceCheck("ollama", "systemd", "medium", "Motor de IA local usado por Batman en modo Desarrollo"),
    ServiceCheck("daniel-portfolio-app", "docker", "high", "Frontend del portafolio en produccion"),
    ServiceCheck("portfolioservicelauncher-client-gateway-1", "docker", "high", "API Gateway NestJS del backend"),
    ServiceCheck("portfolioservicelauncher-nodemailer-micro-service-1", "docker", "high", "Microservicio de envio de correo"),
    ServiceCheck("portfolioservicelauncher-nats-server-1", "docker", "high", "Broker NATS (TLS) del backend de microservicios"),
    ServiceCheck("sonarqube", "docker", "medium", "Analisis estatico de codigo, no forma parte del trafico de produccion"),
    ServiceCheck("claude", "binary", "critical", "Binario de Claude Code usado por Batman para resolver issues"),
]


def load_env() -> None:
    if "TELEGRAM_BOT_TOKEN" in os.environ:
        return
    if ENV_PATH.exists():
        load_dotenv(ENV_PATH)
    else:
        log.warning("No se encontro %s, se usan las variables de entorno actuales", ENV_PATH)


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
                chat_id=self.chat_id, text=text, parse_mode=ParseMode.MARKDOWN
            )
        except TelegramError as exc:
            log.error("No se pudo enviar mensaje a Telegram: %s", exc)


def _run(cmd: list[str], timeout: int = 30) -> str:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return proc.stdout.strip() or proc.stderr.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("Fallo ejecutando %s: %s", " ".join(cmd), exc)
        return ""


def is_signal_operating_hours() -> bool:
    """True si la hora actual esta dentro del horario diurno de Signal."""
    hour = datetime.now(timezone.utc).hour
    start, end = SIGNAL_OPERATING_HOURS_UTC
    return start <= hour < end


def check_systemd(name: str) -> bool:
    return _run(["systemctl", "is-active", name]) == "active"


def restart_systemd(name: str) -> bool:
    _run(["sudo", "systemctl", "restart", name])
    time.sleep(REPAIR_RETRY_DELAY_SECONDS)
    return check_systemd(name)


def check_docker(name: str) -> bool:
    return _run(["docker", "inspect", "-f", "{{.State.Running}}", name]) == "true"


def start_docker(name: str) -> bool:
    _run(["docker", "start", name])
    time.sleep(REPAIR_RETRY_DELAY_SECONDS)
    return check_docker(name)


def check_claude_binary(_name: str) -> bool:
    try:
        proc = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=15)
        return proc.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def repair_claude_binary(_name: str) -> bool:
    _run([CLAUDE_NODE_BIN, CLAUDE_INSTALL_SCRIPT], timeout=300)
    time.sleep(REPAIR_RETRY_DELAY_SECONDS)
    return check_claude_binary(_name)


CHECK_FNS = {
    "systemd": check_systemd,
    "docker": check_docker,
    "binary": check_claude_binary,
}
REPAIR_FNS = {
    "systemd": restart_systemd,
    "docker": start_docker,
    "binary": repair_claude_binary,
}


def attempt_repair(check: ServiceCheck) -> bool:
    repair_fn = REPAIR_FNS[check.kind]
    for attempt in range(1, MAX_REPAIR_ATTEMPTS + 1):
        log.info("Intento de reparacion %d/%d para %s", attempt, MAX_REPAIR_ATTEMPTS, check.name)
        if repair_fn(check.name):
            return True
    return False


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
    """Actualiza batcave/memory/lucius.md segun el resultado de la ronda."""
    try:
        if event == "round_complete":
            memory_store.touch_last_activity("lucius")
            memory_store.bump_stat("lucius", "ciclos")
        elif event == "repair_success":
            memory_store.append_success(
                "lucius",
                problema=f"{kwargs['service']} caido",
                como_se_resolvio=kwargs["solucion"],
            )
            memory_store.bump_stat("lucius", "errores")
            memory_store.bump_stat("lucius", "reparaciones")
        elif event == "repair_failed":
            memory_store.append_error(
                "lucius",
                error=f"{kwargs['service']} caido",
                causa=kwargs.get("causa", "desconocida"),
                solucion=kwargs.get("solucion", "ninguna, se escalo"),
                resultado=kwargs.get("resultado", "escalado"),
            )
            if kwargs.get("escalated_to_alfred"):
                memory_store.bump_stat("lucius", "escalaciones")
    except OSError as exc:
        log.warning("No se pudo actualizar la memoria de Lucius: %s", exc)


SELF_REPAIR_CRITERIA = {
    "can_repair": (
        "El error reportado por Signal puede resolverse de forma segura "
        "reiniciando signal.service con systemctl restart, sin intervencion humana."
    )
}


async def evaluate_self_repair_with_typesafe(
    client: AsyncTypeSafeClient, service: str, error: str
) -> dict:
    try:
        response = await client.system_one(
            state={"service_name": service, "error": error},
            questions={"can_repair": Noul(instructions=SELF_REPAIR_CRITERIA["can_repair"])},
        )
    except Exception as exc:  # noqa: BLE001 - un fallo de TypeSafe no debe tumbar el ciclo
        log.error("Fallo la evaluacion TypeSafe del auto-reparo de %s: %s", service, exc)
        return {"can_repair": 1.0}
    return {"can_repair": response.nouls["can_repair"].noul}


async def handle_signal_self_repair(
    entry: dict, client: Optional[AsyncTypeSafeClient], notifier: TelegramNotifier
) -> None:
    service = entry.get("service", "signal.service")
    error = entry.get("error", "error desconocido")

    decision = (
        await evaluate_self_repair_with_typesafe(client, service, error)
        if client is not None
        else {"can_repair": 1.0}
    )

    if decision["can_repair"] <= 0.7:
        log.info(
            "TypeSafe decidio no auto-reparar %s (can_repair=%.2f)", service, decision["can_repair"]
        )
        return

    log.info("Reparando %s a pedido de Signal: %s", service, error)
    if restart_systemd(service):
        msg = "🦊 Lucius reparó Signal"
        log.info(msg)
        await notifier.send(msg)
        update_memory(
            "repair_success",
            service=service,
            solucion=f"restart systemd a pedido de Signal ({error})",
        )
    else:
        log.warning("Lucius intento reparar %s pero sigue caido", service)
        update_memory(
            "repair_failed",
            service=service,
            causa=error,
            solucion="restart systemd a pedido de Signal",
            resultado="sigue caido",
            escalated_to_alfred=False,
        )


async def handle_pending_messages(
    client: Optional[AsyncTypeSafeClient], notifier: TelegramNotifier
) -> None:
    """Procesa mensajes dirigidos a Lucius (de Batman o Signal) y los marca como leidos."""
    entries = read_comms()
    pending = [e for e in entries if e.get("to") == "lucius" and not e.get("read", False)]

    for entry in pending:
        log.info(
            "Mensaje de %s para Lucius: %s", entry.get("from"), entry.get("message") or entry.get("error")
        )
        if entry.get("from") == "signal" and entry.get("type") == "self_repair_needed":
            await handle_signal_self_repair(entry, client, notifier)
        entry["read"] = True

    if pending:
        write_comms(entries)


PRIORITY_CRITERIA = {
    "critical": (
        "Servicio cuya caida es inaceptable de inmediato: el binario claude, "
        "cloudflared (unico acceso remoto) o telegram-bot (canal de la Batifamilia)."
    ),
    "high": (
        "Containers Docker de produccion o el webhook de deploys; afecta "
        "funcionalidad pero hay margen de horas antes de un impacto grave."
    ),
    "medium": (
        "Servicio de soporte no critico para el negocio (p. ej. IA local o "
        "analisis de codigo), su caida no afecta a usuarios finales."
    ),
    "low": "Degradacion menor sin impacto apreciable.",
}
WHO_TO_NOTIFY_CRITERIA = {
    "alfred": "Alfred debe enterarse porque el servicio es critico o de alta prioridad.",
    "batman": "Solo Batman necesita el contexto para su ronda de seguridad nocturna.",
    "both": "Tanto Alfred como Batman deben enterarse.",
    "none": "No amerita interrumpir a nadie, basta con dejar registro en el log.",
}


async def evaluate_with_typesafe(client: AsyncTypeSafeClient, check: ServiceCheck) -> dict:
    try:
        response = await client.system_one(
            state={
                "service_name": check.name,
                "service_kind": check.kind,
                "description": check.description,
                "baseline_priority": check.baseline_priority,
            },
            questions={
                "can_self_repair": Noul(
                    instructions=(
                        "Este servicio caido puede repararse de forma segura con una "
                        "accion automatica basica (restart de systemd, docker start, o "
                        "reinstalar el binario), sin intervencion humana."
                    )
                ),
                "priority": Choice(
                    instructions="Que tan prioritario es que este servicio vuelva a funcionar.",
                    criteria=PRIORITY_CRITERIA,
                ),
                "who_to_notify": Choice(
                    instructions="A quien de la Batifamilia hay que avisar sobre este servicio caido.",
                    criteria=WHO_TO_NOTIFY_CRITERIA,
                ),
            },
        )
    except Exception as exc:  # noqa: BLE001 - un fallo de TypeSafe no debe tumbar el ciclo
        log.error("Fallo la evaluacion TypeSafe de %s: %s", check.name, exc)
        return fallback_decision(check)

    return {
        "can_self_repair": response.nouls["can_self_repair"].noul >= 0.5,
        "priority": response.choices["priority"].choice,
        "who_to_notify": response.choices["who_to_notify"].choice,
    }


def fallback_decision(check: ServiceCheck) -> dict:
    return {
        "can_self_repair": True,
        "priority": check.baseline_priority,
        "who_to_notify": "alfred" if check.baseline_priority in ("critical", "high") else "none",
    }


def normalize_priority(raw: str, fallback: str) -> str:
    return raw if raw in PRIORITIES else fallback


async def escalate(check: ServiceCheck, priority: str, who_to_notify: str, notifier: TelegramNotifier) -> set[str]:
    targets: set[str] = set()
    if priority == "critical":
        targets.add("alfred")
    elif priority == "high":
        targets.update({"alfred", "batman"})

    if who_to_notify == "alfred":
        targets.add("alfred")
    elif who_to_notify == "batman":
        targets.add("batman")
    elif who_to_notify == "both":
        targets.update({"alfred", "batman"})

    if "alfred" in targets:
        emoji = "🚨" if priority == "critical" else "⚠️"
        await notifier.send(
            f"{emoji} *Lucius Fox*\n`{check.name}` caido, no se pudo auto-reparar.\n"
            f"Prioridad: {priority}\n{check.description}"
        )
    if "batman" in targets:
        append_comm(
            {
                "timestamp": datetime.now().isoformat(),
                "from": "lucius",
                "to": "batman",
                "service": check.name,
                "priority": priority,
                "message": f"{check.name} caido, Lucius no pudo repararlo (prioridad {priority}).",
                "read": False,
            }
        )
    if not targets:
        log.info("%s caido, prioridad %s, solo se deja registro en el log", check.name, priority)

    return targets


async def process_check(
    check: ServiceCheck, client: Optional[AsyncTypeSafeClient], notifier: TelegramNotifier
) -> Optional[str]:
    if check.name == SIGNAL_SERVICE_NAME and not is_signal_operating_hours():
        log.info("%s fuera de horario diurno (6 AM-10 PM CST), se omite el check", check.name)
        return None

    if CHECK_FNS[check.kind](check.name):
        log.info("%s OK", check.name)
        return None

    log.warning("%s caido", check.name)
    decision = await evaluate_with_typesafe(client, check) if client is not None else fallback_decision(check)

    repaired = decision["can_self_repair"] and attempt_repair(check)
    if repaired:
        msg = f"🦊 Lucius reparó: {check.name}"
        log.info(msg)
        await notifier.send(msg)
        update_memory(
            "repair_success",
            service=check.name,
            solucion=f"reparacion automatica ({check.kind})",
        )
        return f"reparado: {check.name}"

    priority = normalize_priority(decision["priority"], check.baseline_priority)
    targets = await escalate(check, priority, decision.get("who_to_notify", "none"), notifier)
    update_memory(
        "repair_failed",
        service=check.name,
        causa=check.description,
        solucion="auto-reparo no fue posible o TypeSafe lo desestimo",
        resultado=f"escalado (prioridad {priority})" if targets else f"sin escalar (prioridad {priority})",
        escalated_to_alfred="alfred" in targets,
    )
    return f"sin reparar ({priority}): {check.name}"


async def run_checks(catalog: list[ServiceCheck], notifier: TelegramNotifier) -> list[str]:
    typesafe_available = bool(os.environ.get("TYPESAFE_API_KEY"))
    if not typesafe_available:
        log.error("TYPESAFE_API_KEY no esta definida, Lucius opera en modo fallback (sin Jev)")
        await notifier.send(
            "🚨 *Lucius Fox*: `TYPESAFE_API_KEY` no esta definida en el entorno. "
            "Decisiones de reparacion en modo fallback, sin TypeSafe/Jev."
        )

    results: list[str] = []
    if typesafe_available:
        async with AsyncTypeSafeClient() as client:
            await handle_pending_messages(client, notifier)
            for check in catalog:
                result = await process_check(check, client, notifier)
                if result:
                    results.append(result)
    else:
        await handle_pending_messages(None, notifier)
        for check in catalog:
            result = await process_check(check, None, notifier)
            if result:
                results.append(result)
    return results


async def main() -> None:
    load_env()
    notifier = TelegramNotifier()

    results = await run_checks(CATALOG, notifier)
    if results:
        log.info("Ronda de Lucius completada: %s", "; ".join(results))
    else:
        log.info("Ronda de Lucius completada: todos los servicios operativos")
    update_memory("round_complete")


if __name__ == "__main__":
    asyncio.run(main())
