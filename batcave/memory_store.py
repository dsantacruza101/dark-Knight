#!/usr/bin/env python3
"""Utilidades para que cada miembro de la Batifamilia actualice su memoria
individual en batcave/memory/<agente>.md.

Modulo puro (sin logging propio, sin Telegram, sin TypeSafe) para que
lucius_fox.py, signal_agent.py y night_agent.py lo puedan importar sin
pisar su configuracion de logging.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

MEMORY_DIR = Path(__file__).resolve().parent / "memory"

STATS_LABELS = {
    "ciclos": "Total de ciclos ejecutados",
    "errores": "Errores encontrados",
    "reparaciones": "Auto-reparaciones exitosas",
    "escalaciones": "Escalaciones a Alfred",
}


def _memory_path(agent: str) -> Path:
    return MEMORY_DIR / f"{agent}.md"


def _read(agent: str) -> str:
    return _memory_path(agent).read_text(encoding="utf-8")


def _write(agent: str, content: str) -> None:
    _memory_path(agent).write_text(content, encoding="utf-8")


def touch_last_activity(agent: str, when: datetime | None = None) -> None:
    when = when or datetime.now()
    content = _read(agent)
    content, _ = re.subn(
        r"- Última actividad: .*",
        f"- Última actividad: {when.strftime('%Y-%m-%d %H:%M:%S')}",
        content,
        count=1,
    )
    _write(agent, content)


def bump_stat(agent: str, key: str, amount: int = 1) -> None:
    label = STATS_LABELS[key]
    content = _read(agent)

    def _incr(match: re.Match) -> str:
        return f"- {label}: {int(match.group(1)) + amount}"

    content, replaced = re.subn(rf"- {re.escape(label)}: (\d+)", _incr, content, count=1)
    if replaced:
        _write(agent, content)


def _insert_table_row(agent: str, section_header: str, row: str) -> None:
    content = _read(agent)
    section_idx = content.index(section_header)
    sep_match = re.search(r"\|[-\s|]+\|\n", content[section_idx:])
    insert_at = section_idx + sep_match.end()
    _write(agent, content[:insert_at] + row + content[insert_at:])


def append_error(
    agent: str, error: str, causa: str, solucion: str, resultado: str, fecha: datetime | None = None
) -> None:
    fecha = fecha or datetime.now()
    row = f"| {fecha.strftime('%Y-%m-%d %H:%M')} | {error} | {causa} | {solucion} | {resultado} |\n"
    _insert_table_row(agent, "## Errores registrados", row)
    bump_stat(agent, "errores")


def append_success(agent: str, problema: str, como_se_resolvio: str, fecha: datetime | None = None) -> None:
    fecha = fecha or datetime.now()
    row = f"| {fecha.strftime('%Y-%m-%d %H:%M')} | {problema} | {como_se_resolvio} |\n"
    _insert_table_row(agent, "## Correcciones exitosas", row)


def add_learning(agent: str, texto: str) -> None:
    content = _read(agent)
    marker = "## Aprendizajes\n"
    idx = content.index(marker) + len(marker)
    placeholder = "- Lista de patrones aprendidos con el tiempo\n"
    if content[idx : idx + len(placeholder)] == placeholder:
        content = content[:idx] + f"- {texto}\n" + content[idx + len(placeholder) :]
    else:
        content = content[:idx] + f"- {texto}\n" + content[idx:]
    _write(agent, content)
