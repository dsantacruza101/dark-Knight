#!/usr/bin/env python3
"""Orquestador de Batman, el agente nocturno autonomo.

Lee config.yaml, decide en que modo trabajar esta noche (Desarrollo,
Post-Reset o Monitoreo), lo ejecuta y reporta el resultado a Telegram
via el bot Alfred.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import yaml
from github import Github, GithubException
from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import TelegramError

from batcave import memory_store
from modes.monitoring import run_monitor_mode
from modes.post_reset import run_post_reset_mode

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.yaml"
LOG_PATH = BASE_DIR / "night_agent.log"
PROJECTS_DIR = Path.home() / "projects"

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


def run_cli(
    cmd: list[str], cwd: Optional[Path] = None, timeout: int = 1800
) -> subprocess.CompletedProcess:
    log.info("Ejecutando: %s (cwd=%s)", " ".join(cmd), cwd)
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)


def repo_local_path(repo_full_name: str) -> Path:
    return PROJECTS_DIR / repo_full_name.split("/")[-1]


def get_open_issues(config: dict) -> list[dict]:
    """Devuelve los issues abiertos con el label configurado, en todos los repos."""
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        log.warning("GITHUB_TOKEN no esta definido, se omite la busqueda de issues")
        return []

    label = config["github"]["issue_label"]
    gh = Github(token)
    found: list[dict] = []
    for repo_name in config["github"]["repos"]:
        try:
            repo = gh.get_repo(repo_name)
            for issue in repo.get_issues(state="open", labels=[label]):
                if issue.pull_request is not None:
                    continue
                found.append(
                    {
                        "repo": repo_name,
                        "number": issue.number,
                        "title": issue.title,
                        "body": issue.body or "",
                        "url": issue.html_url,
                    }
                )
        except GithubException as exc:
            log.error("Error consultando issues en %s: %s", repo_name, exc)
    return found


def parse_weekly_reset(spec: str) -> int:
    """Devuelve el indice de dia (lunes=0) del reset semanal, p.ej. 'Sun 23:00' -> 6."""
    day_str = spec.split()[0].strip().lower()[:3]
    return WEEKDAYS[day_str]


def is_post_reset_day(config: dict, now: datetime) -> bool:
    reset_weekday = parse_weekly_reset(config["schedule"]["weekly_reset"])
    return now.weekday() == (reset_weekday + 1) % 7


def determine_mode(config: dict, now: Optional[datetime] = None) -> tuple[str, Any]:
    now = now or datetime.now()
    issues = get_open_issues(config)
    if issues:
        return "dev", issues
    if is_post_reset_day(config, now):
        return "post_reset", None
    return "monitor", None


def run_dev_mode(config: dict, issues: list[dict]) -> str:
    """Modo Desarrollo: usa claude para resolver los issues etiquetados."""
    branch = config["github"]["work_branch"]
    claude_bin = config["models"]["claude"]
    results = []

    for issue in issues:
        repo_path = repo_local_path(issue["repo"])
        tag = f"{issue['repo']}#{issue['number']}"

        if not repo_path.exists():
            msg = f"⚠️ {tag}: repo no encontrado en {repo_path}"
            log.warning(msg)
            results.append(msg)
            continue

        try:
            run_cli(["git", "fetch", "origin"], cwd=repo_path)
            run_cli(["git", "checkout", branch], cwd=repo_path)
            run_cli(["git", "pull", "origin", branch], cwd=repo_path)

            prompt = (
                f"Resuelve el issue #{issue['number']} de este repositorio.\n"
                f"Titulo: {issue['title']}\n\n"
                f"Descripcion:\n{issue['body']}\n\n"
                "Implementa el cambio y deja el working tree listo para revisar."
            )
            proc = run_cli(
                [claude_bin, "-p", prompt, "--permission-mode", "acceptEdits"],
                cwd=repo_path,
                timeout=3600,
            )

            if proc.returncode != 0:
                msg = f"❌ {tag}: claude fallo (codigo {proc.returncode})\n{proc.stderr[-500:]}"
                log.error(msg)
                results.append(msg)
                continue

            status = run_cli(["git", "status", "--porcelain"], cwd=repo_path)
            if status.stdout.strip():
                run_cli(["git", "add", "-A"], cwd=repo_path)
                commit_msg = f"feat: resuelve #{issue['number']} - {issue['title']}"
                run_cli(["git", "commit", "-m", commit_msg], cwd=repo_path)
                run_cli(["git", "push", "origin", branch], cwd=repo_path)
                results.append(f"✅ {tag} resuelto y pusheado a `{branch}`: {issue['url']}")
            else:
                results.append(f"ℹ️ {tag}: sin cambios generados")

        except subprocess.TimeoutExpired:
            msg = f"⏱️ {tag}: timeout ejecutando claude"
            log.error(msg)
            results.append(msg)
        except OSError as exc:
            msg = f"❌ {tag}: error inesperado: {exc}"
            log.exception(msg)
            results.append(msg)

    return "\n".join(results) if results else "Sin resultados."


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
                        como_se_resolvio="Resuelto por Claude en modo Desarrollo y pusheado a dev",
                    )
                else:
                    memory_store.append_error(
                        "batman",
                        error=f"Issue {tag}: {issue['title']}",
                        causa="claude no genero cambios o fallo al resolverlo",
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
    "dev": run_dev_mode,
    "post_reset": run_post_reset_mode,
    "monitor": run_monitor_mode,
}


async def main() -> None:
    started_at = datetime.now()
    notifier = TelegramNotifier()

    try:
        config = load_config()
    except (OSError, yaml.YAMLError) as exc:
        log.exception("No se pudo cargar config.yaml")
        await notifier.send(f"🦇 *Batman encontró un problema*: no pudo iniciar (error leyendo config.yaml)\n`{exc}`")
        sys.exit(1)

    try:
        mode, context = determine_mode(config, started_at)
        mode_label = MODE_LABELS[mode]

        await notifier.send(
            f"🦇 *Batman iniciado*\n{mode_label}\n"
            f"Hora: {started_at.strftime('%Y-%m-%d %H:%M:%S')}"
        )
        log.info("Modo seleccionado: %s", mode)

        handler = MODE_HANDLERS[mode]
        if mode == "dev":
            report_body = handler(config, context)
        else:
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
