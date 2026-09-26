"""Enmascarado de secretos en texto que se va a loguear o persistir en disco.

Modulo puro (sin logging propio, sin Telegram, sin TypeSafe) para que
cualquier agente de la Batifamilia lo pueda importar sin pisar su
configuracion de logging, igual que memory_store.py.
"""

from __future__ import annotations

import re

SECRET_LOG_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_-]{10,}"),
    re.compile(r"apikey_[A-Za-z0-9]{10,}", re.IGNORECASE),
    re.compile(r"bot\d+:AA[A-Za-z0-9_-]{20,}"),
)


def mask_secrets(text: str) -> str:
    """Reemplaza cualquier patron de secreto conocido (tokens de GitHub,
    llaves de Anthropic, api keys genericas, tokens de bot de Telegram) por
    un marcador, para que nunca queden en logs ni en archivos de reporte."""
    for pattern in SECRET_LOG_PATTERNS:
        text = pattern.sub("***REDACTED***", text)
    return text
