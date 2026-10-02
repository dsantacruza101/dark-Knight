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
    re.compile(r"(?<=Authorization:)\s*(Basic|Bearer)\s+\S+", re.IGNORECASE),
    re.compile(r"x-access-token:\S+", re.IGNORECASE),
    re.compile(r"[A-Za-z0-9+/]{40,}={0,2}"),
)


def mask_secrets(text: str) -> str:
    """Reemplaza cualquier patron de secreto conocido (tokens de GitHub,
    llaves de Anthropic, api keys genericas, tokens de bot de Telegram,
    headers HTTP `Authorization: Basic/Bearer`, `x-access-token:` y
    cadenas base64 largas como las usadas en `GIT_CONFIG_VALUE_0`) por un
    marcador, para que nunca queden en logs ni en archivos de reporte."""
    for pattern in SECRET_LOG_PATTERNS:
        text = pattern.sub("***REDACTED***", text)
    return text


if __name__ == "__main__":
    cases = [
        "Authorization: Basic eF9hY2Nlc3MtdG9rZW46Z2hwXzEyMzQ1Njc4OTBhYmNkZWZnaGlqa2xtbm9wcXJzdHV2d3h5eg==",
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123456789ABCD",
        "x-access-token:ghp_1234567890abcdefghijklmnopqrstuvwxyz",
    ]
    for case in cases:
        masked = mask_secrets(case)
        assert "***REDACTED***" in masked, f"no se enmascaro: {case!r} -> {masked!r}"
        print(f"OK: {case[:20]}... -> {masked}")
    print("Todos los casos quedaron enmascarados.")
