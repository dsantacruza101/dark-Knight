"""Enmascarado de secretos en texto que se va a loguear o persistir en disco.

Modulo puro (sin logging propio, sin Telegram, sin TypeSafe) para que
cualquier agente de la Batifamilia lo pueda importar sin pisar su
configuracion de logging, igual que memory_store.py.
"""

from __future__ import annotations

import re

REDACTED = "***REDACTED***"

SECRET_LOG_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_-]{10,}"),
    re.compile(r"apikey_[A-Za-z0-9]{10,}", re.IGNORECASE),
    re.compile(r"bot\d+:AA[A-Za-z0-9_-]{20,}"),
    re.compile(r"(?<=Authorization:)\s*(Basic|Bearer)\s+\S+", re.IGNORECASE),
    re.compile(r"x-access-token:\S+", re.IGNORECASE),
)

# Cadenas base64 largas (p. ej. el header de GIT_CONFIG_VALUE_0). Se filtran
# con `looks_like_path` porque una ruta como
# /home/dsantacruz/projects/portfolioServiceLauncher tambien es [A-Za-z0-9/]{40,}.
BASE64_PATTERN = re.compile(r"[A-Za-z0-9+/]{40,}={0,2}")
# Segmento de ruta "de palabras": minusculas/digitos con jorobas camelCase de
# 3+ caracteres (home, dsantacruz, portfolioServiceLauncher, Documents). Un
# segmento base64 aleatorio casi nunca cumple (mayusculas seguidas, jorobas
# de 1-2 letras).
PATH_SEGMENT = re.compile(r"(?:[a-z0-9]+|[A-Z][a-z0-9]{2,})(?:[A-Z][a-z0-9]{2,})*")


def looks_like_path(candidate: str) -> bool:
    """True si `candidate` son segmentos separados por `/` hechos de palabras
    normales (una ruta del sistema de archivos), no un token base64."""
    if "/" not in candidate or "+" in candidate or candidate.endswith("="):
        return False
    segments = [s for s in candidate.split("/") if s]
    return len(segments) >= 2 and all(PATH_SEGMENT.fullmatch(s) for s in segments)


def _mask_base64(match: re.Match) -> str:
    return match.group(0) if looks_like_path(match.group(0)) else REDACTED


def mask_secrets(text: str) -> str:
    """Reemplaza cualquier patron de secreto conocido (tokens de GitHub,
    llaves de Anthropic, api keys genericas, tokens de bot de Telegram,
    headers HTTP `Authorization: Basic/Bearer`, `x-access-token:` y
    cadenas base64 largas como las usadas en `GIT_CONFIG_VALUE_0`) por un
    marcador, para que nunca queden en logs ni en archivos de reporte. Las
    rutas de archivos (segmentos de palabras separados por `/`) no se tocan."""
    for pattern in SECRET_LOG_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return BASE64_PATTERN.sub(_mask_base64, text)


if __name__ == "__main__":
    import base64
    import string

    # Los secretos falsos se arman en tiempo de ejecucion para que ningun
    # literal con forma de token quede en el codigo (el escaner de secretos
    # de Red Hood los marcaria como hardcodeados).
    fake_github_token = "ghp_" + "1234567890" + string.ascii_lowercase
    fake_basic = base64.b64encode(("x_access-token:" + fake_github_token).encode()).decode()
    fake_bearer = string.ascii_lowercase + string.digits + "ABCD"
    # base64 "desnudo" con `/` y sin `+` ni padding: debe enmascararse igual.
    fake_b64_slashes = base64.b64encode(bytes(range(200, 245))).decode().replace("+", "/")
    cases = [
        "Authorization: Basic " + fake_basic,
        "Authorization: Bearer " + fake_bearer,
        "x-access-token:" + fake_github_token,
        "AUTHORIZATION: basic " + fake_basic,
        "GIT_CONFIG_VALUE_0=" + fake_basic,
        "token " + fake_b64_slashes,
    ]
    for case in cases:
        masked = mask_secrets(case)
        assert REDACTED in masked, f"no se enmascaro: {case!r} -> {masked!r}"
        print(f"OK: {case[:20]}... -> {masked}")
    paths = [
        "cd /home/dsantacruz/projects/portfolioServiceLauncher && docker compose up -d",
        "/home/dsantacruz/projects/night-agent/batcave/memory/julia_cache/notices",
        "/home/dsantacruz/projects/wazuh-docker/single-node/docker-compose.yml",
        "/usr/lib/python3/dist-packages/someModuleName/submodule/file.py",
    ]
    for case in paths:
        masked = mask_secrets(case)
        assert masked == case, f"ruta enmascarada: {case!r} -> {masked!r}"
        print(f"OK (ruta intacta): {case}")
    print("Todos los casos quedaron enmascarados y las rutas intactas.")
