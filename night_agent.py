#!/usr/bin/env python3
"""Orquestador de Batman, el agente nocturno autonomo.

Lee config.yaml, decide en que modo trabajar esta noche (Desarrollo,
Post-Reset o Monitoreo), lo ejecuta y reporta el resultado a Telegram
via el bot Alfred.

Con `--dry-run` solo se imprime el modo que se seleccionaria y (en modo
Desarrollo) los issues que se encontraron, sin clonar nada, sin ejecutar
Aider/claude y sin enviar Telegram.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import yaml
from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import TelegramError

from batcave import memory_store
from modes.development import get_github_client, get_open_issues, run_development_mode
from modes.monitoring import run_monitor_mode
from modes.post_reset import run_post_reset_mode

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.yaml"
LOG_PATH = BASE_DIR / "night_agent.log"

WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}

MODE_LABELS = {
    "dev": "🦇 Batman — Modo Desarrollo activo",
    "post_reset": "🦇 Batman — Modo Post-Reset activo",
    "monitor": "🦇 Batman — Modo Monitoreo activo",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.FileHandler(LOG_PATH), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("night_agent")


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
                chat_id=self.chat_id, text=text, parse_mode=ParseMode.MARKDOWN
            )
        except TelegramError as exc:
            log.error("No se pudo enviar mensaje a Telegram: %s", exc)


def parse_weekly_reset(spec: str) -> int:
    """Devuelve el indice de dia (lunes=0) del reset semanal, p.ej. 'Sun 23:00' -> 6."""
    day_str = spec.split()[0].strip().lower()[:3]
    return WEEKDAYS[day_str]


def is_post_reset_day(config: dict, now: datetime) -> bool:
    reset_weekday = parse_weekly_reset(config["schedule"]["weekly_reset"])
    return now.weekday() == (reset_weekday + 1) % 7


def determine_mode(config: dict, now: Optional[datetime] = None) -> tuple[str, Any]:
    now = now or datetime.now()
    try:
        gh = get_github_client()
        issues = get_open_issues(gh, config)
    except RuntimeError as exc:
        log.warning("No se pudo consultar GitHub para decidir el modo: %s", exc)
        issues = []
    if issues:
        return "dev", issues
    if is_post_reset_day(config, now):
        return "post_reset", None
    return "monitor", None


def update_batman_memory(mode: str, context: Any, report_body: str) -> None:
    """Actualiza batcave/memory/batman.md segun el modo ejecutado esta noche."""
    try:
        memory_store.touch_last_activity("batman")
        memory_store.bump_stat("batman", "ciclos")

        if mode == "dev":
            for issue in context or []:
                tag = f"{issue['repo']}#{issue['number']}"
                if f"✅ {tag}" in report_body:
                    memory_store.append_success(
                        "batman",
                        problema=f"Issue {tag}: {issue['title']}",
                        como_se_resolvio="Resuelto por Nightwing (Aider) en modo Desarrollo y pusheado a dev",
                    )
                else:
                    memory_store.append_error(
                        "batman",
                        error=f"Issue {tag}: {issue['title']}",
                        causa="aider no genero cambios o fallo al resolverlo",
                        solucion="ninguna, requiere revision manual",
                        resultado="sin resolver",
                    )
        elif mode == "post_reset":
            score_match = re.search(r"calidad Ollama: ([\d.]+)", report_body)
            score = score_match.group(1) if score_match else "N/D"
            memory_store.append_success(
                "batman",
                problema="Auditoria semanal Post-Reset",
                como_se_resolvio=f"Score de Nightwing (calidad Ollama vs Claude): {score}",
            )
        elif mode == "monitor":
            last_line = report_body.splitlines()[-1] if report_body else "completado"
            memory_store.append_success(
                "batman", problema="Ronda de Monitoreo nocturno", como_se_resolvio=last_line
            )
    except OSError as exc:
        log.warning("No se pudo actualizar la memoria de Batman: %s", exc)


MODE_HANDLERS = {
    "dev": run_development_mode,
    "post_reset": run_post_reset_mode,
    "monitor": run_monitor_mode,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Batman: orquestador nocturno")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Solo imprime el modo que se seleccionaria y los issues encontrados, "
        "sin clonar, sin ejecutar Aider/claude y sin enviar Telegram",
    )
    return parser.parse_args()


def print_dry_run(config: dict, started_at: datetime) -> None:
    mode, context = determine_mode(config, started_at)
    print(f"[dry-run] Modo que se seleccionaria: {mode} ({MODE_LABELS[mode]})")
    if mode == "dev":
        issues = context or []
        print(f"[dry-run] {len(issues)} issue(s) con label '{config['github']['issue_label']}' encontrados:")
        for issue in issues:
            print(f"[dry-run]   - {issue['repo']}#{issue['number']}: {issue['title']} ({issue['url']})")
    else:
        print("[dry-run] Sin issues pendientes; no se clona nada ni se ejecuta Aider/claude.")


async def main() -> None:
    args = parse_args()
    started_at = datetime.now()

    try:
        config = load_config()
    except (OSError, yaml.YAMLError) as exc:
        log.exception("No se pudo cargar config.yaml")
        if args.dry_run:
            print(f"[dry-run] No se pudo cargar config.yaml: {exc}")
            sys.exit(1)
        notifier = TelegramNotifier()
        await notifier.send(f"🦇 *Batman encontró un problema*: no pudo iniciar (error leyendo config.yaml)\n`{exc}`")
        sys.exit(1)

    if args.dry_run:
        print_dry_run(config, started_at)
        return

    notifier = TelegramNotifier()

    try:
        mode, context = determine_mode(config, started_at)
        mode_label = MODE_LABELS[mode]

        await notifier.send(
            f"🦇 *Batman iniciado*\n{mode_label}\n"
            f"Hora: {started_at.strftime('%Y-%m-%d %H:%M:%S')}"
        )
        log.info("Modo seleccionado: %s", mode)

        handler = MODE_HANDLERS[mode]
        report_body = await handler(config, notifier)

        update_batman_memory(mode, context, report_body)

        finished_at = datetime.now()
        duration = str(finished_at - started_at).split(".", maxsplit=1)[0]
        await notifier.send(
            f"✅ *Batman finalizado*\n{mode_label}\n"
            f"Duracion: {duration}\n\n{report_body}"
        )

    except Exception as exc:  # noqa: BLE001 - notificar cualquier fallo antes de salir
        log.exception("Fallo el night agent")
        await notifier.send(f"🦇 *Batman encontró un problema*\n`{type(exc).__name__}: {exc}`\nRevisa {LOG_PATH}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
