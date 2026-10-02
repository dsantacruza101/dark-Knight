"""Modo de Desarrollo Nocturno: Nightwing resuelve issues 'night-agent' usando Aider.

Claude (Batman) descansa en este modo. Cada issue etiquetado se resuelve localmente
con Aider (`AIDER_BIN`), usando Ollama como backend (`models.aider` en config.yaml).

Workspace aislado (`development.workspace_dir`, por defecto
`~/projects/.nightwing-workspace`): Nightwing NUNCA hace checkout, pull,
commits ni ediciones sobre los directorios de produccion listados en
`config.yaml` (`repo_paths`). Todo el trabajo ocurre sobre una copia clonada
en `workspace_dir/<nombre-repo>`:

  - Si la copia no existe: `git clone` desde la URL publica
    `https://github.com/<repo_full_name>.git`; si `GITHUB_TOKEN` esta
    definido, se autentica con un header HTTP pasado como variables de
    entorno del subproceso (`github_auth_env()`, via `extra_env` de
    `run_cli`), nunca como argumento de git ni embebido en la URL ni
    guardado en `.git/config`.
  - Si ya existe: `git fetch origin` + `checkout <work_branch>` +
    `reset --hard origin/<work_branch>`.
  - Si `origin` no tiene la rama de trabajo (`github.work_branch`), se crea
    en el workspace desde `origin/main` y se pushea con `-u origin <rama>`.

Flujo por issue:
  1. Se lee el titulo y la descripcion del issue (y se identifica su repo,
     que ya viene dado por de donde se obtuvo el issue). Las rutas absolutas
     de produccion mencionadas en el cuerpo (p. ej. `~/projects/night-agent/x`)
     se convierten a rutas relativas del repo (`x`), ya que Aider trabaja
     sobre el workspace aislado. El cuerpo del issue es la UNICA fuente de la
     tarea: el CLAUDE.md del repo nunca se incluye en el mensaje a Aider,
     porque Aider agrega automaticamente al chat cualquier archivo del repo
     que se mencione, y el CLAUDE.md suele mencionar muchos.
  2. Se prepara el workspace aislado del repo (ver arriba).
  3. Se calcula la lista de archivos permitidos (`extract_allowed_files`):
     si el issue tiene una seccion `Archivos:` (una ruta por linea), se usa
     esa lista tal cual; si no, se cae a la extraccion por mencion de
     nombre de archivo (`*.py`, `*.ts`, `*.service`, `*.timer`, `*.md`,
     `*.yaml`), excluyendo los que aparecen en lineas de verificacion de
     servicios (`systemctl`, `is-active`, `docker`). Esa lista se pasa a
     Aider como argumentos posicionales.
  4. Se arma un mensaje minimo (sin ejemplos de knowledge/) y se pasa a
     Aider (`--message`, stdin en `/dev/null`), que edita los archivos
     directamente en disco sobre el workspace aislado. El stdout/stderr de
     cada intento se escribe en vivo (no se captura en memoria) en
     `reports/aider-<repo>-<issue>-<intento>.log`, y se enmascaran los
     secretos al terminar el proceso (incluso si hubo timeout, queda la
     salida parcial).
  5. Candado: los cambios en rutas prohibidas se revierten
     (`revert_forbidden_changes`), y cualquier archivo modificado o creado
     que no este en la lista de archivos permitidos tambien se revierte
     (`revert_unlisted_changes`); los archivos permitidos que queden vacios
     o solo con espacios/comentarios se borran (`delete_empty_allowed_files`).
     Este candado se aplica SIEMPRE, incluso si aider termino por timeout o
     error, antes de verificar o comitear.
  6. Verificacion antes de comitear (`verify_changes`): cada archivo
     permitido debe existir, tener contenido real y pasar una validacion
     minima segun su tipo (`validate_allowed_file_content`): `.py` debe
     compilar con `py_compile` y tener al menos un `def`/`class`; `.service`
     debe tener `[Unit]`/`[Service]` y `ExecStart=`; `.timer` debe tener
     `[Timer]` y `OnCalendar=`/`OnUnitActiveSec=`; `.spec.ts`/`.test.ts`
     debe tener `describe(`/`it(`/`test(`. Ademas, si el repo es Node,
     `npm run build` y `npm test -- --passWithNoTests --watchAll=false`; si
     hay archivos de test entre los permitidos, se confirma que jest/vitest
     o pytest los detecta (conteo de tests > 0). Si falla, el intento cuenta
     como fallido: la salida COMPLETA del comando que fallo (py_compile/
     npm run build/npm test) queda en `reports/aider-<repo>-<issue>-<intento>.log`,
     y los cambios NO se descartan (el siguiente intento parte del archivo ya
     generado, no de cero). El siguiente intento incluye en el mensaje a
     Aider una seccion "El intento anterior fallo la verificacion con este
     error, corrigelo:" con las ultimas `VERIFICATION_FEEDBACK_LINES` (60)
     lineas de esa salida. Si en cambio Aider termino por timeout o error (no
     llego a generar un intento verificable), los cambios si se descartan con
     `discard_changes` y no hay retroalimentacion para el siguiente intento.
  7. Nightwing comitea con el mensaje 'feat(nightwing): ...' y hace push
     (contra el remoto de GitHub, no contra el directorio de produccion).

Restricciones duras (no configurables):
  - Nunca se toca ni se hace checkout de main/master; si el branch de trabajo
    configurado fuera main/master, el modo se niega a correr.
  - Nunca se dejan cambios en archivos .env, docker-compose*, ni configuracion
    de nginx/fail2ban/cloudflared/ssh (ver `is_forbidden_path`); Aider corre
    con `--no-auto-commits` para que estos cambios se puedan revertir antes
    de comitear.
  - Nunca escribe sobre los directorios de produccion de `repo_paths`
    (solo se leen, unicamente para relativizar rutas mencionadas en el
    cuerpo del issue).
  - Maximo `MAX_HOURS_PER_ISSUE` horas por issue; si se supera se aborta ese
    issue y se continua con el siguiente.
  - Si Aider no genera una solucion utilizable tras `development.aider_attempts`
    intentos (default `DEFAULT_AIDER_ATTEMPTS`), se comenta 'needs-review' en
    el issue y se continua. El timeout por intento es
    `development.aider_timeout` (default `DEFAULT_AIDER_TIMEOUT`).

El progreso se reporta a Telegram cada `PROGRESS_INTERVAL` (30 min). Como las
llamadas a git/aider son bloqueantes, la notificacion se emite entre issues,
no con un timer independiente.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from github import Github, GithubException

from batcave.secrets import mask_secrets

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = Path("/etc/night-agent.env")
REPORTS_DIR = BASE_DIR / "reports"

AIDER_BIN = os.path.expanduser("~/.aider-venv/bin/aider")
OLLAMA_API_BASE = "http://localhost:11434"

DEFAULT_WORKSPACE_DIR = "~/projects/.nightwing-workspace"
DEFAULT_AIDER_TIMEOUT = 2700
DEFAULT_AIDER_ATTEMPTS = 3
PER_FILE_AIDER_TIMEOUT = 900

VERIFICATION_FEEDBACK_LINES = 60

MAX_HOURS_PER_ISSUE = 3
CLONE_TIMEOUT = 300
NPM_CI_TIMEOUT = 300
PROGRESS_INTERVAL = timedelta(minutes=30)

FORBIDDEN_NAME_SUBSTRINGS = (".env", "docker-compose")
FORBIDDEN_PATH_SEGMENTS = ("nginx", "fail2ban", "cloudflared", ".cloudflared", ".ssh", "ufw")

FILE_MENTION_RE = re.compile(r"(?<![\w/])[\w./\-]+\.(?:py|ts|service|timer|md|yaml)(?![\w])")
FILES_SECTION_HEADER_RE = re.compile(r"^\s*archivos\s*:\s*$", re.IGNORECASE)
SERVICE_CHECK_LINE_RE = re.compile(r"systemctl|is-active|docker", re.IGNORECASE)
TEST_FILE_NAME_RE = re.compile(r"^test_.+\.py$|.+\.(?:spec|test)\.[jt]sx?$")
NODE_TEST_COUNT_RE = re.compile(r"Tests:\s*(?:(\d+) failed,\s*)?(?:(\d+) skipped,\s*)?(\d+) passed")
PYTEST_COLLECTED_RE = re.compile(r"(\d+)\s+tests?\s+collected")

DEF_OR_CLASS_RE = re.compile(r"^\s*(?:async\s+)?(?:def|class)\s+\w+", re.MULTILINE)
UNIT_SECTION_RE = re.compile(r"^\s*\[(?:Unit|Service)\]\s*$", re.MULTILINE)
EXEC_START_RE = re.compile(r"^\s*ExecStart\s*=", re.MULTILINE)
TIMER_SECTION_RE = re.compile(r"^\s*\[Timer\]\s*$", re.MULTILINE)
TIMER_SCHEDULE_RE = re.compile(r"^\s*(?:OnCalendar|OnUnitActiveSec)\s*=", re.MULTILINE)
TEST_BODY_RE = re.compile(r"\b(?:describe|it|test)\s*\(")

COMMENT_PREFIXES_BY_SUFFIX = {
    ".py": ("#",),
    ".yaml": ("#",),
    ".yml": ("#",),
    ".service": ("#", ";"),
    ".timer": ("#", ";"),
    ".ts": ("//", "/*", "*"),
    ".js": ("//", "/*", "*"),
    ".md": (),
}

log = logging.getLogger("night_agent.development")
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


def get_github_client() -> Github:
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise RuntimeError("GITHUB_TOKEN debe estar definido en el entorno")
    return Github(token)


def get_open_issues(gh: Github, config: dict) -> list[dict]:
    """Issues abiertos con el label configurado, en todos los repos de config.yaml."""
    label = config["github"]["issue_label"]
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
                        "repo_obj": repo,
                        "issue_obj": issue,
                        "number": issue.number,
                        "title": issue.title,
                        "body": issue.body or "",
                        "url": issue.html_url,
                    }
                )
        except GithubException as exc:
            log.error("Error consultando issues en %s: %s", repo_name, exc)
    return found


def get_workspace_dir(config: dict) -> Path:
    return Path(config.get("development", {}).get("workspace_dir", DEFAULT_WORKSPACE_DIR)).expanduser()


def get_repo_paths(config: dict) -> dict[str, Path]:
    """Mapeo repo remoto -> ruta local de produccion (`config.yaml`, `repo_paths`).

    Se usa UNICAMENTE para relativizar rutas de produccion mencionadas en el
    cuerpo del issue (`relativize_paths_in_body`); nunca para checkout, pull,
    commits ni ninguna otra escritura.
    """
    return {name: Path(path).expanduser() for name, path in config.get("repo_paths", {}).items()}


def build_clone_url(repo_full_name: str) -> str:
    """URL de clone publica; nunca lleva el token embebido (ver `github_auth_env`)."""
    return f"https://github.com/{repo_full_name}.git"


def github_auth_env() -> Optional[dict]:
    """Variables de entorno para autenticar con GITHUB_TOKEN via
    `GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_0`/`GIT_CONFIG_VALUE_0`, nunca como
    argumento de git: un argumento queda en el log del comando y es visible
    con `ps`/`/proc/<pid>/cmdline` para cualquier usuario del sistema; una
    variable de entorno del subproceso no. Se pasa solo a `run_cli` via
    `extra_env` para el comando puntual (clone/fetch/push), nunca se guarda
    en la URL remota ni en `.git/config`."""
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        return None
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
    }


def git_cmd(args: list[str]) -> list[str]:
    """Antepone 'git' a un subcomando que habla con el remoto de GitHub
    (clone/fetch/push). Ya no inyecta el header de auth aqui: eso viaja por
    `github_auth_env()`, pasado como `extra_env` al llamar `run_cli`."""
    return ["git", *args]


def read_repo_claude_md(repo_path: Path) -> str:
    claude_md = repo_path / "CLAUDE.md"
    if not claude_md.exists():
        return ""
    try:
        return claude_md.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("No se pudo leer CLAUDE.md en %s: %s", repo_path, exc)
        return ""


def relativize_paths_in_body(body: str, config: dict, repo_full_name: str) -> str:
    """Convierte rutas absolutas de produccion mencionadas en el issue
    (p. ej. ~/projects/night-agent/x o /home/user/projects/night-agent/x) en
    rutas relativas al repo (x): Aider trabaja sobre el workspace aislado,
    no sobre la ruta de produccion."""
    repo_short_name = repo_full_name.split("/")[-1]
    raw = config.get("repo_paths", {}).get(repo_short_name)
    if not raw:
        return body
    for prefix in {raw, str(Path(raw).expanduser())}:
        body = body.replace(prefix.rstrip("/") + "/", "")
        body = body.replace(prefix, ".")
    return body


def extract_mentioned_files(text: str) -> list[str]:
    """Extrae del texto del issue los nombres de archivo mencionados
    (*.py, *.ts, *.service, *.timer, *.md, *.yaml), para pasarselos a Aider
    como argumentos posicionales y que edite directamente esos archivos."""
    seen: list[str] = []
    for match in FILE_MENTION_RE.findall(text):
        if match not in seen:
            seen.append(match)
    return seen


def extract_allowed_files_from_section(text: str) -> list[str]:
    """Extrae rutas de una seccion 'Archivos:' del issue (una ruta por
    linea, relativa al repo). Si no hay tal seccion, devuelve []."""
    collecting = False
    files: list[str] = []
    for line in text.splitlines():
        if not collecting:
            if FILES_SECTION_HEADER_RE.match(line):
                collecting = True
            continue
        stripped = line.strip()
        if not stripped:
            break
        if stripped.endswith(":") and "." not in stripped and "/" not in stripped:
            break
        cleaned = stripped.lstrip("-*• ").strip().strip("`")
        if cleaned and cleaned not in files:
            files.append(cleaned)
    return files


def extract_mentioned_files_excluding_service_lines(text: str) -> list[str]:
    """Extraccion actual por mencion de archivo, excluyendo nombres que
    aparecen en lineas de verificacion de servicios (systemctl, is-active,
    docker), que suelen nombrar unidades systemd o containers y no archivos
    a editar."""
    service_mentions: set[str] = set()
    for line in text.splitlines():
        if SERVICE_CHECK_LINE_RE.search(line):
            service_mentions.update(FILE_MENTION_RE.findall(line))
    return [f for f in extract_mentioned_files(text) if f not in service_mentions]


def extract_allowed_files(text: str) -> list[str]:
    """Archivos que Aider tiene permitido tocar: SOLO los listados bajo una
    seccion 'Archivos:' del issue si existe; si no, la extraccion por
    mencion de nombre de archivo, excluyendo lineas de verificacion de
    servicios. Esta lista es el candado que se aplica despues de cada
    intento de Aider (ver `revert_unlisted_changes`)."""
    section_files = extract_allowed_files_from_section(text)
    if section_files:
        return section_files
    return extract_mentioned_files_excluding_service_lines(text)


def build_aider_message(
    issue: dict, body: str, only_file: Optional[str] = None, previous_error: Optional[str] = None
) -> str:
    focus = f"De la tarea descrita, crea o edita SOLO el archivo {only_file}.\n\n" if only_file else ""
    feedback = (
        f"El intento anterior fallo la verificacion con este error, corrigelo:\n{previous_error}\n\n"
        if previous_error
        else ""
    )
    return (
        f"Resuelve el issue #{issue['number']} del repositorio {issue['repo']}: "
        f"{issue['title']}\n\n"
        f"{body[:3000]}\n\n"
        f"{focus}"
        f"{feedback}"
        "Reglas obligatorias:\n"
        "- Usa rutas relativas dentro del repositorio.\n"
        "- NUNCA edites ni crees archivos .env, docker-compose*, ni "
        "configuracion de nginx, fail2ban, cloudflared o ssh.\n"
        "- No ejecutes comandos de git ni toques la rama main."
    )


def mask_log_file(log_path: Path) -> None:
    try:
        content = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    log_path.write_text(mask_secrets(content), encoding="utf-8")


def append_to_log(log_path: Path, title: str, body: str) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as log_file:
        log_file.write(f"\n## {title}\n{mask_secrets(body)}\n")


def run_aider(
    repo_path: Path,
    model: str,
    message: str,
    files: list[str],
    timeout: int,
    log_path: Path,
) -> bool:
    """Ejecuta Aider sobre el repo; Aider edita los archivos directamente en disco.

    stdin va a /dev/null (Aider nunca debe quedar esperando input interactivo).
    stdout/stderr se escriben directamente a `log_path` mientras el proceso
    corre (no se capturan en memoria), para que un timeout deje la salida
    parcial disponible para diagnostico; los secretos se enmascaran al final,
    una vez el proceso termina."""
    cmd = [
        AIDER_BIN,
        "--model", model,
        "--message", message,
        "--yes",
        "--no-auto-commits",
        "--map-tokens", "1024",
        "--edit-format", "whole",
        "--no-show-model-warnings",
        "--no-check-update",
        "--no-pretty",
        *files,
    ]
    env = {**os.environ, "OLLAMA_API_BASE": OLLAMA_API_BASE}
    log_path.parent.mkdir(parents=True, exist_ok=True)

    timed_out = False
    returncode: Optional[int] = None
    try:
        with open(log_path, "w", encoding="utf-8") as log_file:
            proc = subprocess.Popen(
                cmd,
                cwd=repo_path,
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                text=True,
                env=env,
            )
            try:
                returncode = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                timed_out = True
    except OSError as exc:
        log.error("Fallo ejecutando aider: %s", exc)
        append_to_log(log_path, "error", str(exc))
        mask_log_file(log_path)
        return False

    mask_log_file(log_path)
    if timed_out:
        log.error("aider excedio el timeout de %ss", timeout)
        append_to_log(log_path, "timeout", f"aider excedio el timeout de {timeout}s, queda la salida parcial arriba")
        return False
    if returncode != 0:
        log.error("aider devolvio codigo %s (ver %s)", returncode, log_path)
        return False
    return True


def is_forbidden_path(rel_path: str) -> bool:
    normalized = rel_path.strip().lstrip("./").lower()
    name = Path(normalized).name
    if any(sub in name for sub in FORBIDDEN_NAME_SUBSTRINGS):
        return True
    parts = Path(normalized).parts
    return any(seg in parts or seg in name for seg in FORBIDDEN_PATH_SEGMENTS)


def changed_files(repo_path: Path) -> list[str]:
    status = run_cli(["git", "status", "--porcelain"], cwd=repo_path)
    files: list[str] = []
    for line in status.stdout.splitlines():
        rel_path = line[3:].strip()
        if " -> " in rel_path:
            rel_path = rel_path.split(" -> ")[-1]
        files.append(rel_path)
    return files


def revert_forbidden_changes(repo_path: Path) -> list[str]:
    """Revierte cambios de Aider en rutas prohibidas antes de comitear."""
    reverted: list[str] = []
    for rel_path in changed_files(repo_path):
        if not is_forbidden_path(rel_path):
            continue
        log.warning("Cambio revertido por politica de seguridad: %s", rel_path)
        run_cli(["git", "checkout", "--", rel_path], cwd=repo_path)
        run_cli(["git", "clean", "-f", "--", rel_path], cwd=repo_path)
        reverted.append(rel_path)
    return reverted


def revert_unlisted_changes(repo_path: Path, allowed_files: list[str], log_path: Path) -> list[str]:
    """Candado: revierte (git checkout -- y git clean) cualquier archivo
    modificado o creado que no este en `allowed_files`. Se registra en
    `log_path` para que quede rastro de lo que Aider intento tocar de mas."""
    allowed = {f.strip().lstrip("./") for f in allowed_files}
    reverted: list[str] = []
    for rel_path in changed_files(repo_path):
        if rel_path.strip().lstrip("./") in allowed:
            continue
        run_cli(["git", "checkout", "--", rel_path], cwd=repo_path)
        run_cli(["git", "clean", "-fd", "--", rel_path], cwd=repo_path)
        reverted.append(rel_path)
    if reverted:
        append_to_log(log_path, "candado (fuera de la lista permitida, revertidos)", "\n".join(reverted))
    return reverted


def discard_changes(repo_path: Path) -> None:
    """Descarta todos los cambios sin commitear del workspace (usado cuando
    un intento de Aider falla la verificacion, para dejar el repo limpio
    antes del siguiente intento)."""
    for rel_path in changed_files(repo_path):
        run_cli(["git", "checkout", "--", rel_path], cwd=repo_path)
        run_cli(["git", "clean", "-fd", "--", rel_path], cwd=repo_path)


def strip_comments_and_blank(content: str, suffix: str) -> str:
    """Contenido 'real' de un archivo: sin lineas en blanco ni comentarios,
    segun el juego de prefijos de comentario de su extension (fallback
    '#', '//', ';' para extensiones no listadas)."""
    prefixes = COMMENT_PREFIXES_BY_SUFFIX.get(suffix, ("#", "//", ";"))
    meaningful = [
        line.strip()
        for line in content.splitlines()
        if line.strip() and not line.strip().startswith(prefixes)
    ]
    return "\n".join(meaningful)


def delete_empty_allowed_files(repo_path: Path, allowed_files: list[str], log_path: Path) -> list[str]:
    """Candado (punto 3): borra los archivos permitidos que Aider haya dejado
    vacios o solo con espacios/comentarios, para que no cuenten como cambio
    valido (ni queden comiteados vacios si el resto del intento parece ok)."""
    emptied: list[str] = []
    for rel in allowed_files:
        full = repo_path / rel
        if not full.exists() or full.is_dir():
            continue
        try:
            content = full.read_text(encoding="utf-8")
        except OSError:
            continue
        if strip_comments_and_blank(content, full.suffix):
            continue
        full.unlink()
        emptied.append(rel)
    if emptied:
        append_to_log(log_path, "candado (archivos permitidos vacios, eliminados)", "\n".join(emptied))
    return emptied


def validate_allowed_file_content(repo_path: Path, allowed_files: list[str]) -> list[str]:
    """Verificacion por archivo permitido (puntos 1 y 2 antes de comitear):
    debe existir, tener contenido real (no vacio/solo comentarios), y pasar
    la validacion minima segun su tipo."""
    errors: list[str] = []
    for rel in allowed_files:
        full = repo_path / rel
        if not full.exists():
            errors.append(f"{rel}: no existe tras la corrida de aider")
            continue
        try:
            content = full.read_text(encoding="utf-8")
        except OSError as exc:
            errors.append(f"{rel}: no se pudo leer ({exc})")
            continue

        if not strip_comments_and_blank(content, full.suffix):
            errors.append(f"{rel}: vacio o solo contiene espacios/comentarios")
            continue

        name = full.name
        if full.suffix == ".py":
            if not DEF_OR_CLASS_RE.search(content):
                errors.append(f"{rel}: no contiene ninguna definicion (def/class)")
        elif full.suffix == ".service":
            if not UNIT_SECTION_RE.search(content):
                errors.append(f"{rel}: falta la seccion [Unit] o [Service]")
            elif not EXEC_START_RE.search(content):
                errors.append(f"{rel}: falta la linea ExecStart=")
        elif full.suffix == ".timer":
            if not TIMER_SECTION_RE.search(content):
                errors.append(f"{rel}: falta la seccion [Timer]")
            elif not TIMER_SCHEDULE_RE.search(content):
                errors.append(f"{rel}: falta OnCalendar= u OnUnitActiveSec=")
        elif name.endswith(".spec.ts") or name.endswith(".test.ts"):
            if not TEST_BODY_RE.search(content):
                errors.append(f"{rel}: no contiene describe(/it(/test(")
    return errors


def run_cli(
    cmd: list[str], cwd: Optional[Path] = None, timeout: int = 600, extra_env: Optional[dict] = None
) -> subprocess.CompletedProcess:
    """Ejecuta `cmd`. Si `extra_env` viene dado (p. ej. `github_auth_env()`),
    se funde con `os.environ` y se pasa solo al proceso hijo: el header de
    autenticacion nunca forma parte de `cmd`, asi que no aparece en el log
    de abajo ni en `ps`/`/proc/<pid>/cmdline`."""
    log.info("Ejecutando: %s (cwd=%s)", mask_secrets(" ".join(cmd)), cwd)
    env = {**os.environ, **extra_env} if extra_env else None
    try:
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.error("Fallo ejecutando %s: %s", mask_secrets(" ".join(cmd)), exc)
        return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr=str(exc))


def setup_workspace_repo(repo_full_name: str, workspace_dir: Path, branch: str) -> Optional[Path]:
    """Prepara una copia aislada de `repo_full_name` en `workspace_dir`.

    Nightwing NUNCA hace checkout/pull/commit sobre los directorios de
    produccion (regla dura): todo el trabajo ocurre sobre
    `workspace_dir/<nombre-repo>`. Si la copia no existe se clona (URL
    autenticada con GITHUB_TOKEN); si ya existe se hace `fetch` + `checkout`
    + `reset --hard` para dejarla identica al remoto.

    Si `origin` no tiene la rama de trabajo, se crea en el workspace desde
    `origin/main` y se pushea con `-u origin <branch>` (nunca se toca
    main/master directamente).

    Devuelve la ruta del workspace, o None si no se pudo preparar (el
    llamador debe omitir el repo en ese caso).
    """
    if branch in ("main", "master"):
        log.error("work_branch configurado como '%s': el modo se niega a usarlo", branch)
        return None

    repo_short_name = repo_full_name.split("/")[-1]
    workspace_repo_path = workspace_dir / repo_short_name

    if not workspace_repo_path.exists():
        workspace_dir.mkdir(parents=True, exist_ok=True)
        log.info("Clonando %s -> %s (workspace aislado)", repo_full_name, workspace_repo_path)
        clone = run_cli(
            git_cmd(["clone", build_clone_url(repo_full_name), str(workspace_repo_path)]),
            timeout=CLONE_TIMEOUT,
            extra_env=github_auth_env(),
        )
        if clone.returncode != 0:
            log.error("Fallo el clone de %s: %s", repo_full_name, mask_secrets(clone.stderr[-500:]))
            return None
    else:
        run_cli(git_cmd(["fetch", "origin"]), cwd=workspace_repo_path, extra_env=github_auth_env())

    if run_cli(["git", "rev-parse", "--verify", f"origin/{branch}"], cwd=workspace_repo_path).returncode == 0:
        if run_cli(["git", "checkout", branch], cwd=workspace_repo_path).returncode != 0:
            run_cli(["git", "checkout", "-b", branch, f"origin/{branch}"], cwd=workspace_repo_path)
        run_cli(["git", "reset", "--hard", f"origin/{branch}"], cwd=workspace_repo_path)
        run_cli(["git", "clean", "-fd"], cwd=workspace_repo_path)
    else:
        log.warning("%s: origin no tiene la rama '%s', se crea desde origin/main", repo_full_name, branch)
        if run_cli(["git", "checkout", "main"], cwd=workspace_repo_path).returncode != 0:
            run_cli(["git", "checkout", "-b", "main", "origin/main"], cwd=workspace_repo_path)
        run_cli(["git", "reset", "--hard", "origin/main"], cwd=workspace_repo_path)
        run_cli(["git", "clean", "-fd"], cwd=workspace_repo_path)
        if run_cli(["git", "checkout", "-b", branch], cwd=workspace_repo_path).returncode != 0:
            log.error("%s: no se pudo crear la rama '%s' en el workspace", repo_full_name, branch)
            return None
        push = run_cli(git_cmd(["push", "-u", "origin", branch]), cwd=workspace_repo_path, extra_env=github_auth_env())
        if push.returncode != 0:
            log.error(
                "%s: fallo pusheando la nueva rama '%s': %s", repo_full_name, branch, mask_secrets(push.stderr[-500:])
            )
            return None

    current = run_cli(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=workspace_repo_path).stdout.strip()
    if current in ("main", "master"):
        log.error("Seguridad: el workspace de %s quedo en '%s', se aborta para no tocar main", repo_full_name, current)
        return None
    return workspace_repo_path


def repo_is_clean(repo_path: Path) -> bool:
    status = run_cli(["git", "status", "--porcelain"], cwd=repo_path)
    return status.returncode == 0 and not status.stdout.strip()


def commit_and_push(repo_path: Path, branch: str, message: str) -> bool:
    status = run_cli(["git", "status", "--porcelain"], cwd=repo_path)
    if not status.stdout.strip():
        return False
    run_cli(["git", "add", "-A"], cwd=repo_path)
    run_cli(["git", "commit", "-m", message], cwd=repo_path)
    push = run_cli(git_cmd(["push", "origin", branch]), cwd=repo_path, extra_env=github_auth_env())
    if push.returncode != 0:
        log.error("Fallo el push a %s: %s", branch, mask_secrets(push.stderr[-500:]))
    return True


def count_node_tests_detected(output: str) -> int:
    match = NODE_TEST_COUNT_RE.search(output)
    if not match:
        return 0
    return int(match.group(1) or 0) + int(match.group(3) or 0)


def count_pytest_collected(output: str) -> int:
    match = PYTEST_COLLECTED_RE.search(output)
    return int(match.group(1)) if match else 0


def node_has_script(repo_path: Path, script: str) -> bool:
    package_json = repo_path / "package.json"
    if not package_json.exists():
        return False
    try:
        pkg = json.loads(package_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return script in pkg.get("scripts", {})


def verify_python_files(repo_path: Path, py_files: list[str]) -> tuple[list[str], list[str]]:
    """py_compile sobre los .py permitidos que Aider efectivamente dejo en disco.

    Devuelve (errores cortos para log/needs-review, salida completa de cada
    py_compile fallido para el log de aider y la retroalimentacion del
    siguiente intento)."""
    errors: list[str] = []
    full_outputs: list[str] = []
    for rel in py_files:
        full = repo_path / rel
        if not full.exists():
            continue
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "py_compile", str(full)],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            errors.append(f"py_compile fallo ejecutando en {rel}: {exc}")
            continue
        if proc.returncode != 0:
            errors.append(f"py_compile fallo en {rel}")
            full_outputs.append(f"=== py_compile {rel} ===\n{proc.stderr.strip()}")
    return errors, full_outputs


def npm_ci_if_needed(repo_path: Path) -> None:
    """Antes de verificar en repos Node, instala dependencias con `npm ci` si
    `node_modules` no existe o si `package-lock.json` cambio desde la ultima
    instalacion (hash guardado dentro de `node_modules`, que sobrevive al
    `git clean -fd` de `setup_workspace_repo` por estar gitignoreado)."""
    lockfile = repo_path / "package-lock.json"
    node_modules = repo_path / "node_modules"
    hash_marker = node_modules / ".night-agent-lockfile-hash"

    current_hash = hashlib.sha256(lockfile.read_bytes()).hexdigest() if lockfile.exists() else None
    previous_hash = hash_marker.read_text(encoding="utf-8").strip() if hash_marker.exists() else None

    if node_modules.exists() and current_hash == previous_hash:
        return

    log.info("%s: instalando dependencias (npm ci)", repo_path.name)
    run_cli(["npm", "ci"], cwd=repo_path, timeout=NPM_CI_TIMEOUT)
    if current_hash is not None and node_modules.exists():
        hash_marker.write_text(current_hash, encoding="utf-8")


def verify_node_repo(repo_path: Path, test_files: list[str]) -> tuple[list[str], list[str]]:
    """Para repos Node: build + test suite; si hay archivos de test entre los
    permitidos, confirma que jest/vitest detecto al menos un test.

    Devuelve (errores cortos para log/needs-review, salida completa de cada
    comando fallido para el log de aider y la retroalimentacion del
    siguiente intento)."""
    errors: list[str] = []
    full_outputs: list[str] = []
    npm_ci_if_needed(repo_path)
    if node_has_script(repo_path, "build"):
        build = run_cli(["npm", "run", "build"], cwd=repo_path, timeout=600)
        if build.returncode != 0:
            errors.append("npm run build fallo")
            full_outputs.append(f"=== npm run build ===\n{mask_secrets(build.stdout + build.stderr)}")

    if node_has_script(repo_path, "test"):
        test = run_cli(
            ["npm", "test", "--", "--passWithNoTests", "--watchAll=false"], cwd=repo_path, timeout=600
        )
        if test.returncode != 0:
            errors.append("npm test fallo")
            full_outputs.append(f"=== npm test ===\n{mask_secrets(test.stdout + test.stderr)}")
        if test_files:
            detected = count_node_tests_detected(test.stdout + test.stderr)
            if detected == 0:
                errors.append("Los archivos de test no fueron detectados por jest/vitest (0 tests encontrados)")
    return errors, full_outputs


def verify_python_test_files(repo_path: Path, test_files: list[str]) -> list[str]:
    """Para archivos de test Python entre los permitidos, confirma que
    pytest los detecta (conteo de tests recolectados > 0)."""
    if not test_files:
        return []
    proc = run_cli(
        ["python3", "-m", "pytest", *test_files, "--collect-only", "-q"], cwd=repo_path, timeout=120
    )
    detected = count_pytest_collected(proc.stdout + proc.stderr)
    if detected == 0:
        return ["Los archivos de test no fueron detectados por pytest (0 tests recolectados)"]
    return []


def last_lines(text: str, n: int) -> str:
    return "\n".join(text.splitlines()[-n:])


def verify_changes(repo_path: Path, allowed_files: list[str], log_path: Path) -> tuple[bool, str, str]:
    """Verificacion antes de comitear: cada archivo permitido debe existir,
    tener contenido real y pasar su validacion minima por tipo
    (`validate_allowed_file_content`); py_compile en los .py permitidos; si
    el repo es Node, npm run build + npm test; y para archivos de test
    (permitidos), confirma que el framework correspondiente los detecta.
    Si falla, el intento cuenta como fallido, la salida COMPLETA de los
    comandos fallidos (py_compile/npm run build/npm test) queda en el log de
    aider, y se devuelve junto con un resumen corto (para needs-review)."""
    py_files = [f for f in allowed_files if f.endswith(".py")]
    test_files = [f for f in allowed_files if TEST_FILE_NAME_RE.match(Path(f).name)]

    errors = validate_allowed_file_content(repo_path, allowed_files)
    full_outputs: list[str] = []

    py_errors, py_full = verify_python_files(repo_path, py_files)
    errors.extend(py_errors)
    full_outputs.extend(py_full)

    if (repo_path / "package.json").exists():
        node_test_files = [f for f in test_files if Path(f).suffix in (".js", ".jsx", ".ts", ".tsx")]
        node_errors, node_full = verify_node_repo(repo_path, node_test_files)
        errors.extend(node_errors)
        full_outputs.extend(node_full)
    else:
        python_test_files = [f for f in test_files if f.endswith(".py")]
        errors.extend(verify_python_test_files(repo_path, python_test_files))

    if errors:
        full_output = "\n\n".join(full_outputs) if full_outputs else "\n".join(errors)
        append_to_log(log_path, "verificacion fallida", full_output)
        return False, "; ".join(errors), full_output
    return True, "", ""


def mark_needs_review(repo_obj, issue_obj, reason: str) -> None:
    try:
        issue_obj.create_comment(f"🐦 Nightwing necesita ayuda en issue #{issue_obj.number}: {reason}")
    except GithubException as exc:
        log.error("No se pudo comentar en el issue #%s: %s", issue_obj.number, exc)
    try:
        issue_obj.add_to_labels("needs-review")
    except GithubException as exc:
        log.warning(
            "No se pudo aplicar el label 'needs-review' en #%s (creala manualmente en %s si no existe): %s",
            issue_obj.number, repo_obj.full_name, exc,
        )


def process_issue(config: dict, issue: dict) -> str:
    tag = f"{issue['repo']}#{issue['number']}"
    repo_short_name = issue["repo"].split("/")[-1]
    branch = config["github"]["work_branch"]
    aider_model = config["models"]["aider"]
    workspace_dir = get_workspace_dir(config)
    aider_timeout = config.get("development", {}).get("aider_timeout", DEFAULT_AIDER_TIMEOUT)
    aider_attempts = config.get("development", {}).get("aider_attempts", DEFAULT_AIDER_ATTEMPTS)

    repo_path = setup_workspace_repo(issue["repo"], workspace_dir, branch)
    if repo_path is None:
        msg = f"❌ {tag}: no se pudo preparar el workspace aislado para {issue['repo']}"
        log.error(msg)
        return msg

    if not repo_is_clean(repo_path):
        msg = f"⚠️ {tag}: el workspace tiene cambios sin commitear, se omite para no pisarlos"
        log.warning(msg)
        return msg

    body = relativize_paths_in_body(issue["body"], config, issue["repo"])
    allowed_files = extract_allowed_files(body)

    deadline = datetime.now() + timedelta(hours=MAX_HOURS_PER_ISSUE)
    written: list[str] = []
    previous_error: Optional[str] = None
    attempts = 0
    while attempts < aider_attempts:
        if datetime.now() >= deadline:
            msg = f"⏱️ {tag}: se alcanzo el limite de {MAX_HOURS_PER_ISSUE}h, se pasa al siguiente issue"
            log.warning(msg)
            return msg

        attempts += 1
        attempt_log_path = REPORTS_DIR / f"aider-{repo_short_name}-{issue['number']}-{attempts}.log"

        if len(allowed_files) > 1:
            aider_ok = True
            for rel_path in allowed_files:
                file_tag = re.sub(r"[^\w.-]", "_", rel_path)
                file_log_path = REPORTS_DIR / f"aider-{repo_short_name}-{issue['number']}-{attempts}-{file_tag}.log"
                file_message = build_aider_message(issue, body, only_file=rel_path, previous_error=previous_error)
                if not run_aider(repo_path, aider_model, file_message, [rel_path], PER_FILE_AIDER_TIMEOUT, file_log_path):
                    log.warning(
                        "%s: aider fallo con el archivo %s (intento %d/%d)", tag, rel_path, attempts, aider_attempts
                    )
                    aider_ok = False
        else:
            message = build_aider_message(issue, body, previous_error=previous_error)
            aider_ok = run_aider(repo_path, aider_model, message, allowed_files, aider_timeout, attempt_log_path)

        # Candado: se aplica SIEMPRE, incluso si aider termino por timeout o
        # error, para revertir rutas prohibidas/archivos fuera de la lista
        # permitida y borrar los archivos permitidos que hayan quedado vacios.
        reverted = revert_forbidden_changes(repo_path)
        if reverted:
            log.warning("%s: aider toco rutas prohibidas, revertidas: %s", tag, ", ".join(reverted))

        if allowed_files:
            unlisted = revert_unlisted_changes(repo_path, allowed_files, attempt_log_path)
            if unlisted:
                log.warning(
                    "%s: aider toco archivos fuera de la lista permitida, revertidos: %s",
                    tag, ", ".join(unlisted),
                )
            emptied = delete_empty_allowed_files(repo_path, allowed_files, attempt_log_path)
            if emptied:
                log.warning("%s: archivos permitidos vacios tras aider, eliminados: %s", tag, ", ".join(emptied))

        if not aider_ok:
            append_to_log(
                attempt_log_path,
                "intento sin resultado util",
                "aider termino por timeout o error; se aplico el candado y se descartan los cambios restantes",
            )
            discard_changes(repo_path)
            previous_error = None
            log.warning("%s: intento %d/%d con aider sin resultado util", tag, attempts, aider_attempts)
            continue

        written = changed_files(repo_path)
        if written:
            ok, error, full_output = verify_changes(repo_path, allowed_files, attempt_log_path)
            if ok:
                break
            log.warning("%s: intento %d/%d fallo la verificacion: %s", tag, attempts, aider_attempts, error)
            # No se descartan los cambios: el siguiente intento parte del
            # archivo ya generado para que aider lo corrija, en vez de
            # arrancar de cero.
            previous_error = last_lines(full_output, VERIFICATION_FEEDBACK_LINES)
            written = []
        log.warning("%s: intento %d/%d con aider sin resultado util", tag, attempts, aider_attempts)

    if not written:
        mark_needs_review(
            issue["repo_obj"],
            issue["issue_obj"],
            f"Aider ({aider_model}) no genero una solucion utilizable tras {aider_attempts} intentos.",
        )
        msg = f"🚫 {tag}: aider fallo {aider_attempts} veces, marcado 'needs-review'"
        log.error(msg)
        return msg

    commit_msg = f"feat(nightwing): resuelve #{issue['number']} - {issue['title']}"
    if commit_and_push(repo_path, branch, commit_msg):
        msg = (
            f"✅ {tag}: {len(written)} archivo(s) actualizados en `{branch}` "
            f"({', '.join(written)}): {issue['url']}"
        )
        log.info(msg)
        return msg

    return f"ℹ️ {tag}: aider respondio pero no genero cambios reales en el repo"


def write_development_report(started_at: datetime, results: list[str]) -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    date_str = started_at.strftime("%Y-%m-%d")
    report_path = REPORTS_DIR / f"development-{date_str}.md"
    lines = [f"# Reporte de Modo Desarrollo (Aider) - {date_str}", ""]
    if results:
        lines.extend(f"- {line}" for line in results)
    else:
        lines.append("No habia issues con label 'night-agent' pendientes.")
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_path


async def run_development_mode(config: dict, notifier) -> str:
    """Punto de entrada del modo Desarrollo: resuelve issues usando Aider (backend Ollama)."""
    load_env()
    started_at = datetime.now()

    gh = get_github_client()
    issues = get_open_issues(gh, config)

    if not issues:
        report_path = write_development_report(started_at, [])
        return f"Sin issues con label '{config['github']['issue_label']}' pendientes. Reporte: {report_path}"

    await notifier.send(
        f"🐦 *Nightwing — Modo Desarrollo (Aider)*\n{len(issues)} issue(s) por resolver "
        f"con label `{config['github']['issue_label']}`"
    )

    results: list[str] = []
    last_notified = datetime.now()
    for idx, issue in enumerate(issues, start=1):
        tag = f"{issue['repo']}#{issue['number']}"
        log.info("🐦 Nightwing trabajando en issue #%d (%d/%d): %s", issue["number"], idx, len(issues), tag)
        try:
            result = process_issue(config, issue)
        except Exception as exc:  # noqa: BLE001 - un issue no debe tumbar el resto de la corrida
            log.exception("Fallo inesperado procesando %s", tag)
            result = f"❌ {tag}: error inesperado: {exc}"
        results.append(result)

        if datetime.now() - last_notified >= PROGRESS_INTERVAL:
            await notifier.send(
                "🐦 *Nightwing — progreso*\n"
                f"{idx}/{len(issues)} issue(s) procesados hasta ahora:\n" + "\n".join(results)
            )
            last_notified = datetime.now()

    report_path = write_development_report(started_at, results)
    return "\n".join(results) + f"\n\nReporte guardado en {report_path}"
