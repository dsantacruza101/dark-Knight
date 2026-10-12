"""Modo de Desarrollo Nocturno: el equipo de especialistas resuelve issues 'night-agent' usando Aider.

Claude (Batman) descansa en este modo. Cada issue etiquetado se resuelve localmente
con Aider (`AIDER_BIN`), usando Ollama como backend (`models.aider` en config.yaml).

Especialistas (`batcave/specialists/*.yaml`, esquema en su README.md): cada
issue se enruta al especialista cuyo `label` de perfil este en el issue
(ademas de `night-agent`); sin label de especialidad va al especialista por
defecto (`specialists.default`, Nightwing). El perfil aporta:
  - un preambulo (rol, stack, convenciones; max `PREAMBLE_MAX_LINES` lineas)
    mas hasta `MAX_KNOWLEDGE_NOTES` notas de `batcave/knowledge/<nombre>/`
    relevantes al issue (max `MAX_KNOWLEDGE_NOTE_LINES` lineas en total),
    que se anteponen al mensaje de Aider (nunca el CLAUDE.md);
  - los comandos de verificacion por stack (`verification`), en vez de
    comandos hardcodeados.

Escalacion qwen -> Claude: si todos los intentos de Aider fallan la
verificacion, se escala UNA vez a Claude Code (`claude -p --permission-mode
acceptEdits`) en el mismo workspace, con el issue, el preambulo, la lista de
archivos permitidos, el contenido actual de esos archivos y las ultimas
`ESCALATION_ERROR_LINES` lineas del ultimo error de verificacion. Se aplica el
mismo candado y la misma verificacion. Maximo
`development.max_claude_escalations` por noche (default 2); si Claude no
esta disponible o se alcanzo el limite, se marca 'needs-review'. Quien
resolvio cada issue (`aider-qwen`, `claude-escalation`, `none`) queda en el
reporte, en el progreso de Telegram y en `batcave/memory/<especialista>.md`.

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
  4. Se arma un mensaje minimo (preambulo del especialista + notas de
     `batcave/knowledge/<especialista>/`, nunca ejemplos de knowledge/) y se pasa a
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
     tener al menos un `def`/`class`; `.service`
     debe tener `[Unit]`/`[Service]` y `ExecStart=`; `.timer` debe tener
     `[Timer]` y `OnCalendar=`/`OnUnitActiveSec=`; `.spec.ts`/`.test.ts`
     debe tener `describe(`/`it(`/`test(`. Ademas se corren los pasos de
     `verification` del perfil del especialista para cada stack detectado
     (p. ej. Node: npm ci si hace falta, build, lint, test; Python:
     py_compile + pytest); si hay archivos de test entre los permitidos, se
     confirma que jest/vitest o pytest los detecta (conteo de tests > 0).
     Si falla, el intento cuenta
     como fallido: la salida COMPLETA del comando que fallo queda en
     `reports/aider-<repo>-<issue>-<intento>.log`,
     y los cambios NO se descartan (el siguiente intento parte del archivo ya
     generado, no de cero). El siguiente intento incluye en el mensaje a
     Aider una seccion "El intento anterior fallo la verificacion con este
     error, corrigelo:" con las ultimas `VERIFICATION_FEEDBACK_LINES` (60)
     lineas de esa salida. Si en cambio Aider termino por timeout o error (no
     llego a generar un intento verificable), los cambios si se descartan con
     `discard_changes` y no hay retroalimentacion para el siguiente intento.
  7. Si Aider agoto sus intentos, escalacion a Claude (ver arriba).
  8. El especialista comitea con el mensaje 'feat(<especialista>): ...' y hace push
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
    intentos (default `DEFAULT_AIDER_ATTEMPTS`) y la escalacion a Claude no
    procede o tambien falla, se comenta 'needs-review' en
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
import shlex
import shutil
import subprocess
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv
from github import Github, GithubException

from batcave import memory_store
from batcave.secrets import mask_secrets

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = Path("/etc/night-agent.env")
REPORTS_DIR = BASE_DIR / "reports"

DEFAULT_SPECIALISTS_DIR = "batcave/specialists"
DEFAULT_KNOWLEDGE_DIR = "batcave/knowledge"
DEFAULT_SPECIALIST = "nightwing"
PREAMBLE_MAX_LINES = 25
MAX_KNOWLEDGE_NOTES = 3
MAX_KNOWLEDGE_NOTE_LINES = 60
DEFAULT_STEP_TIMEOUT = 600

DEFAULT_MAX_CLAUDE_ESCALATIONS = 2
DEFAULT_CLAUDE_TIMEOUT = 1800
ESCALATION_ERROR_LINES = 80
ESCALATION_MAX_FILE_LINES = 400
MIN_ESCALATION_SECONDS = 600

SOLVER_AIDER = "aider-qwen"
SOLVER_CLAUDE = "claude-escalation"
SOLVER_NONE = "none"

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
PYTEST_PASSED_RE = re.compile(r"(\d+)\s+passed")
ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
NOTE_KEYWORDS_RE = re.compile(r"^\s*keywords\s*:\s*(.+)$", re.IGNORECASE)
NOTE_STEM_SPLIT_RE = re.compile(r"[-_.\s]+")
NODE_SOURCE_SUFFIXES = (".js", ".jsx", ".ts", ".tsx")

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
                        "labels": [lbl.name for lbl in issue.labels],
                    }
                )
        except GithubException as exc:
            log.error("Error consultando issues en %s: %s", repo_name, exc)
    return found


def resolve_project_path(raw: str) -> Path:
    """Ruta de config.yaml: absoluta/`~` tal cual, relativa respecto a la raiz del proyecto."""
    path = Path(raw).expanduser()
    return path if path.is_absolute() else BASE_DIR / path


def load_specialists(config: dict) -> dict[str, dict]:
    """Carga los perfiles `*.yaml` de `specialists.dir` (orden alfabetico).

    Un perfil sin `name`/`role`/`label`/`verification` se omite con error en
    el log. Si no queda ninguno, el modo Desarrollo no puede enrutar issues."""
    specialists_dir = resolve_project_path(config.get("specialists", {}).get("dir", DEFAULT_SPECIALISTS_DIR))
    specialists: dict[str, dict] = {}
    for path in sorted(specialists_dir.glob("*.yaml")):
        try:
            profile = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            log.error("No se pudo cargar el perfil de especialista %s: %s", path, exc)
            continue
        missing = [key for key in ("name", "role", "label", "verification") if not profile.get(key)]
        if missing:
            log.error("Perfil %s incompleto (falta %s), se omite", path.name, ", ".join(missing))
            continue
        if profile["name"] != path.stem:
            log.warning("Perfil %s: name '%s' no coincide con el nombre del archivo", path.name, profile["name"])
        specialists[profile["name"]] = profile
    if not specialists:
        raise RuntimeError(f"No hay perfiles de especialista validos en {specialists_dir}")
    return specialists


def default_specialist_name(config: dict, specialists: dict[str, dict]) -> str:
    """Especialista para issues sin label de especialidad: `specialists.default`
    de config.yaml; si no existe, el perfil con `default: true`; si no, el primero."""
    configured = config.get("specialists", {}).get("default", DEFAULT_SPECIALIST)
    if configured in specialists:
        return configured
    for name, profile in specialists.items():
        if profile.get("default"):
            return name
    return next(iter(specialists))


def specialist_display(profile: dict) -> str:
    return f"{profile.get('emoji', '🦇')} {profile.get('display_name') or profile['name'].capitalize()}"


def route_issue(issue: dict, specialists: dict[str, dict], default_name: str, issue_label: str) -> tuple[str, str]:
    """Devuelve (especialista, motivo). Gana el perfil cuyo `label` este en el
    issue (ademas de `issue_label`); sin label de especialidad, el default."""
    labels = {lbl.lower() for lbl in issue.get("labels", []) if lbl.lower() != issue_label.lower()}
    matches = [name for name, profile in specialists.items() if str(profile["label"]).lower() in labels]
    if len(matches) > 1:
        log.warning(
            "%s#%s tiene labels de varios especialistas (%s), se asigna a %s",
            issue["repo"], issue["number"], ", ".join(matches), matches[0],
        )
    if matches:
        name = matches[0]
        reason = f"label '{specialists[name]['label']}'"
    else:
        name = default_name
        reason = "default, sin label de especialidad"
    repo_short_name = issue["repo"].split("/")[-1]
    owned = specialists[name].get("repos") or []
    if owned and repo_short_name not in owned:
        reason += f"; ojo: {repo_short_name} no esta en sus repos"
    return name, reason


def build_specialist_preamble(profile: dict) -> str:
    """Preambulo corto (max `PREAMBLE_MAX_LINES` lineas): rol, stack y convenciones."""
    lines = [
        f"Actuas como {profile.get('display_name') or profile['name']}, "
        f"especialista {profile['role']} del equipo de desarrollo."
    ]
    for key, title in (("languages", "Lenguajes"), ("frameworks", "Frameworks"), ("orms", "ORMs"), ("messaging", "Mensajeria")):
        if profile.get(key):
            lines.append(f"{title}: {', '.join(str(v) for v in profile[key])}")
    if profile.get("conventions"):
        lines.append("Convenciones:")
        lines.extend(f"- {conv}" for conv in profile["conventions"])
    return "\n".join(lines[:PREAMBLE_MAX_LINES])


def note_keywords(path: Path, content: str) -> set[str]:
    """Palabras clave de una nota: partes del nombre de archivo + linea `keywords:` opcional."""
    words = set(NOTE_STEM_SPLIT_RE.split(path.stem.lower()))
    first_line = content.splitlines()[0] if content else ""
    match = NOTE_KEYWORDS_RE.match(first_line)
    if match:
        words.update(w.strip().lower() for w in match.group(1).split(","))
    return {w for w in words if len(w) >= 3}


def select_knowledge_notes(config: dict, specialist: str, issue_text: str) -> list[tuple[str, str]]:
    """Hasta `MAX_KNOWLEDGE_NOTES` notas de `<knowledge_dir>/<especialista>/`
    cuyas palabras clave aparecen en el issue, ordenadas por coincidencias y
    recortadas a `MAX_KNOWLEDGE_NOTE_LINES` lineas en total (incluido el
    encabezado de cada nota). Nunca incluye CLAUDE.md."""
    knowledge_dir = resolve_project_path(config.get("specialists", {}).get("knowledge_dir", DEFAULT_KNOWLEDGE_DIR))
    notes_dir = knowledge_dir / specialist
    if not notes_dir.is_dir():
        return []
    text = issue_text.lower()
    scored: list[tuple[int, str, str]] = []
    for path in sorted(notes_dir.glob("*.md")):
        if path.name.lower() == "claude.md":
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            log.warning("No se pudo leer la nota %s: %s", path, exc)
            continue
        hits = sum(
            1 for kw in note_keywords(path, content) if re.search(rf"(?<!\w){re.escape(kw)}(?!\w)", text)
        )
        if not hits:
            continue
        body_lines = content.splitlines()
        if body_lines and NOTE_KEYWORDS_RE.match(body_lines[0]):
            body_lines = body_lines[1:]
        scored.append((hits, path.stem, "\n".join(body_lines).strip()))

    scored.sort(key=lambda item: (-item[0], item[1]))
    selected: list[tuple[str, str]] = []
    used = 0
    for _, stem, body in scored[:MAX_KNOWLEDGE_NOTES]:
        budget = MAX_KNOWLEDGE_NOTE_LINES - used - 1
        if budget <= 0:
            break
        lines = body.splitlines()[:budget]
        selected.append((stem, "\n".join(lines)))
        used += len(lines) + 1
    return selected


def build_specialist_context(config: dict, profile: dict, issue_text: str) -> str:
    """Preambulo del especialista + notas de conocimiento relevantes al issue."""
    parts = [build_specialist_preamble(profile)]
    notes = select_knowledge_notes(config, profile["name"], issue_text)
    if notes:
        parts.append("Notas de conocimiento:\n" + "\n".join(f"### {stem}\n{body}" for stem, body in notes))
    return "\n\n".join(parts)


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
    issue: dict,
    body: str,
    only_file: Optional[str] = None,
    previous_error: Optional[str] = None,
    specialist_context: str = "",
) -> str:
    focus = f"De la tarea descrita, crea o edita SOLO el archivo {only_file}.\n\n" if only_file else ""
    feedback = (
        f"El intento anterior fallo la verificacion con este error, corrigelo:\n{previous_error}\n\n"
        if previous_error
        else ""
    )
    preamble = f"{specialist_context}\n\n" if specialist_context else ""
    return (
        f"{preamble}"
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


def step_condition_met(repo_path: Path, when: Optional[str]) -> bool:
    """Condicion `when` de un paso de verificacion: `script:<n>`, `exists:<ruta>` o ninguna."""
    if not when:
        return True
    kind, _, arg = str(when).partition(":")
    if kind == "script":
        return node_has_script(repo_path, arg)
    if kind == "exists":
        return (repo_path / arg).exists()
    log.warning("Condicion 'when' desconocida en perfil de especialista: %s (se omite el paso)", when)
    return False


def split_env_prefix(command: str) -> tuple[dict, list[str]]:
    """'CI=true npm test -- x' -> ({'CI': 'true'}, ['npm', 'test', '--', 'x'])."""
    tokens = shlex.split(command)
    env: dict[str, str] = {}
    while tokens and ENV_ASSIGN_RE.match(tokens[0]):
        key, _, value = tokens.pop(0).partition("=")
        env[key] = value
    return env, tokens


def detected_stacks(repo_path: Path, profile: dict) -> list[tuple[str, dict]]:
    """Stacks de `verification` cuyo `detect` (nombre o glob) coincide en la raiz del repo."""
    return [
        (name, stack)
        for name, stack in (profile.get("verification") or {}).items()
        if any(any(repo_path.glob(pattern)) for pattern in stack.get("detect", []))
    ]


def run_verification_steps(
    repo_path: Path, profile: dict, allowed_files: list[str], test_files: list[str]
) -> tuple[list[str], list[str]]:
    """Corre los pasos de `verification` del perfil para cada stack detectado.

    Devuelve (errores cortos para log/needs-review, salida completa de cada
    paso fallido para el log del intento y la retroalimentacion)."""
    errors: list[str] = []
    full_outputs: list[str] = []
    for stack_name, stack in detected_stacks(repo_path, profile):
        for step in stack.get("steps", []):
            if not step_condition_met(repo_path, step.get("when")):
                continue
            builtin = step.get("builtin")
            if builtin == "npm_ci_if_needed":
                npm_ci_if_needed(repo_path)
                continue
            if builtin == "py_compile_changed":
                py_files = sorted(
                    {f for f in changed_files(repo_path) if f.endswith(".py")}
                    | {f for f in allowed_files if f.endswith(".py")}
                )
                py_errors, py_full = verify_python_files(repo_path, py_files)
                errors.extend(py_errors)
                full_outputs.extend(py_full)
                continue
            if builtin or not step.get("cmd"):
                log.warning("Paso de verificacion invalido en %s/%s: %s", profile["name"], stack_name, step)
                continue

            command = str(step["cmd"])
            env, cmd = split_env_prefix(command)
            proc = run_cli(cmd, cwd=repo_path, timeout=int(step.get("timeout", DEFAULT_STEP_TIMEOUT)), extra_env=env or None)
            output = mask_secrets(proc.stdout + proc.stderr)
            if proc.returncode != 0:
                errors.append(f"{command} fallo")
                full_outputs.append(f"=== {command} ===\n{output}")

            counts = step.get("counts_tests")
            if counts == "node":
                node_tests = [f for f in test_files if Path(f).suffix in NODE_SOURCE_SUFFIXES]
                if node_tests and count_node_tests_detected(output) == 0:
                    errors.append("Los archivos de test no fueron detectados por jest/vitest (0 tests encontrados)")
            elif counts == "pytest":
                py_tests = [f for f in test_files if f.endswith(".py")]
                match = PYTEST_PASSED_RE.search(output)
                if py_tests and not (match and int(match.group(1)) > 0):
                    errors.append("pytest no ejecuto ningun test (0 passed)")
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


def verify_changes(
    repo_path: Path, allowed_files: list[str], log_path: Path, profile: dict
) -> tuple[bool, str, str]:
    """Verificacion antes de comitear: cada archivo permitido debe existir,
    tener contenido real y pasar su validacion minima por tipo
    (`validate_allowed_file_content`); luego los pasos de `verification` del
    perfil del especialista (`run_verification_steps`); y para archivos de
    test Python permitidos en repos no-Node, `pytest --collect-only`.
    Si falla, el intento cuenta como fallido, la salida COMPLETA de los
    comandos fallidos queda en el log del intento, y se devuelve junto con
    un resumen corto (para needs-review)."""
    test_files = [f for f in allowed_files if TEST_FILE_NAME_RE.match(Path(f).name)]

    errors = validate_allowed_file_content(repo_path, allowed_files)
    full_outputs: list[str] = []

    step_errors, step_full = run_verification_steps(repo_path, profile, allowed_files, test_files)
    errors.extend(step_errors)
    full_outputs.extend(step_full)

    if not (repo_path / "package.json").exists():
        python_test_files = [f for f in test_files if f.endswith(".py")]
        errors.extend(verify_python_test_files(repo_path, python_test_files))

    if errors:
        full_output = "\n\n".join(full_outputs) if full_outputs else "\n".join(errors)
        append_to_log(log_path, "verificacion fallida", full_output)
        return False, "; ".join(errors), full_output
    return True, "", ""


def mark_needs_review(repo_obj, issue_obj, reason: str, profile: dict) -> None:
    try:
        issue_obj.create_comment(f"{specialist_display(profile)} necesita ayuda en issue #{issue_obj.number}: {reason}")
    except GithubException as exc:
        log.error("No se pudo comentar en el issue #%s: %s", issue_obj.number, exc)
    try:
        issue_obj.add_to_labels("needs-review")
    except GithubException as exc:
        log.warning(
            "No se pudo aplicar el label 'needs-review' en #%s (creala manualmente en %s si no existe): %s",
            issue_obj.number, repo_obj.full_name, exc,
        )


def apply_guard(repo_path: Path, allowed_files: list[str], log_path: Path, tag: str, who: str) -> None:
    """Candado comun a Aider y a la escalacion a Claude: revierte rutas
    prohibidas y archivos fuera de la lista permitida, y borra los archivos
    permitidos que hayan quedado vacios."""
    reverted = revert_forbidden_changes(repo_path)
    if reverted:
        log.warning("%s: %s toco rutas prohibidas, revertidas: %s", tag, who, ", ".join(reverted))

    if allowed_files:
        unlisted = revert_unlisted_changes(repo_path, allowed_files, log_path)
        if unlisted:
            log.warning(
                "%s: %s toco archivos fuera de la lista permitida, revertidos: %s", tag, who, ", ".join(unlisted)
            )
        emptied = delete_empty_allowed_files(repo_path, allowed_files, log_path)
        if emptied:
            log.warning("%s: archivos permitidos vacios tras %s, eliminados: %s", tag, who, ", ".join(emptied))


def read_allowed_files_content(repo_path: Path, allowed_files: list[str]) -> str:
    """Contenido actual de los archivos permitidos (lo que dejo Aider), para la escalacion."""
    chunks: list[str] = []
    for rel in allowed_files:
        full = repo_path / rel
        if not full.is_file():
            chunks.append(f"=== {rel} ===\n(no existe todavia)")
            continue
        try:
            lines = full.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            chunks.append(f"=== {rel} ===\n(no se pudo leer: {exc})")
            continue
        if len(lines) > ESCALATION_MAX_FILE_LINES:
            lines = lines[:ESCALATION_MAX_FILE_LINES] + [f"... (truncado, {len(lines)} lineas en total)"]
        chunks.append(f"=== {rel} ===\n" + "\n".join(lines))
    return "\n\n".join(chunks)


def describe_verification(repo_path: Path, profile: dict) -> list[str]:
    return [
        f"- [{stack_name}] {step.get('cmd') or step.get('builtin')}"
        for stack_name, stack in detected_stacks(repo_path, profile)
        for step in stack.get("steps", [])
        if step_condition_met(repo_path, step.get("when"))
    ]


def build_claude_escalation_prompt(
    issue: dict,
    body: str,
    specialist_context: str,
    allowed_files: list[str],
    files_content: str,
    last_error: str,
    verification: list[str],
) -> str:
    parts = [
        "Eres la escalacion del modo Desarrollo nocturno. Aider con un modelo local (qwen) intento "
        "resolver este issue y no paso la verificacion. Estas en el workspace aislado del repositorio "
        "(rama de trabajo, nunca main); edita los archivos directamente en disco.",
        specialist_context,
        f"## Issue #{issue['number']} de {issue['repo']}: {issue['title']}\n{body[:6000]}",
        "## Archivos permitidos\nSOLO puedes crear o editar estos archivos (cualquier otro cambio se "
        "revierte automaticamente):\n" + "\n".join(f"- {f}" for f in allowed_files),
        f"## Contenido actual de los archivos permitidos\n{files_content}",
        f"## Ultimo error de verificacion (ultimas {ESCALATION_ERROR_LINES} lineas)\n{last_error}",
        "## Verificacion que se correra despues de tus cambios\n"
        + ("\n".join(verification) or "- validacion de contenido por archivo"),
        "Reglas obligatorias:\n"
        "- No ejecutes git (ni commit, ni push, ni checkout): el commit lo hace el agente.\n"
        "- NUNCA edites ni crees archivos .env, docker-compose*, ni configuracion de nginx, fail2ban, "
        "cloudflared, ssh o systemd del servidor.\n"
        "- Deja los archivos permitidos completos y listos para pasar la verificacion.\n"
        "- Al terminar, responde con un resumen de una o dos lineas de lo que cambiaste.",
    ]
    return "\n\n".join(part for part in parts if part)


def run_claude_escalation(claude_bin: str, prompt: str, repo_path: Path, timeout: int, log_path: Path) -> bool:
    """`claude -p --permission-mode acceptEdits` en el workspace. El prompt va
    por stdin (puede superar el limite de tamano de un argumento) y la salida
    se escribe en vivo a `log_path`, con secretos enmascarados al terminar."""
    cmd = [claude_bin, "-p", "--permission-mode", "acceptEdits"]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    timed_out = False
    returncode: Optional[int] = None
    try:
        with open(log_path, "w", encoding="utf-8") as log_file:
            log_file.write(f"## prompt\n{prompt}\n\n## salida de claude\n")
            log_file.flush()
            proc = subprocess.Popen(
                cmd, cwd=repo_path, stdin=subprocess.PIPE, stdout=log_file, stderr=subprocess.STDOUT, text=True
            )
            try:
                proc.communicate(input=prompt, timeout=timeout)
                returncode = proc.returncode
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
                timed_out = True
    except OSError as exc:
        log.error("Fallo ejecutando claude: %s", exc)
        append_to_log(log_path, "error", str(exc))
        mask_log_file(log_path)
        return False

    mask_log_file(log_path)
    if timed_out:
        log.error("claude excedio el timeout de %ss", timeout)
        append_to_log(log_path, "timeout", f"claude excedio el timeout de {timeout}s")
        return False
    if returncode != 0:
        log.error("claude devolvio codigo %s (ver %s)", returncode, log_path)
        return False
    return True


def escalate_to_claude(
    config: dict,
    issue: dict,
    profile: dict,
    repo_path: Path,
    body: str,
    specialist_context: str,
    allowed_files: list[str],
    last_full_output: str,
    deadline: datetime,
    escalation_state: dict,
) -> tuple[bool, str]:
    """Escalacion unica a Claude tras agotar los intentos de Aider. Devuelve
    (resuelto, motivo si no se resolvio). Aplica el mismo candado y la misma
    verificacion que con Aider."""
    tag = f"{issue['repo']}#{issue['number']}"
    claude_bin = config.get("models", {}).get("claude", "claude")
    claude_timeout = int(config.get("development", {}).get("claude_timeout", DEFAULT_CLAUDE_TIMEOUT))

    if not allowed_files:
        return False, "sin lista de archivos permitidos no se escala a Claude"
    if escalation_state["remaining"] <= 0:
        return False, f"se alcanzo el limite de {escalation_state['max']} escalacion(es) a Claude por noche"
    if shutil.which(claude_bin) is None:
        return False, f"Claude no esta disponible ('{claude_bin}' no encontrado)"
    remaining_seconds = (deadline - datetime.now()).total_seconds()
    if remaining_seconds < MIN_ESCALATION_SECONDS:
        return False, f"no queda tiempo dentro del limite de {MAX_HOURS_PER_ISSUE}h para escalar a Claude"

    escalation_state["remaining"] -= 1
    escalation_state["used"] += 1
    log.info(
        "%s: escalando a Claude (%d/%d esta noche)", tag, escalation_state["used"], escalation_state["max"]
    )

    repo_short_name = issue["repo"].split("/")[-1]
    log_path = REPORTS_DIR / f"claude-{repo_short_name}-{issue['number']}.log"
    last_error = (
        last_lines(last_full_output, ESCALATION_ERROR_LINES)
        if last_full_output
        else "(Aider no llego a un intento verificable: termino por timeout o error)"
    )
    prompt = build_claude_escalation_prompt(
        issue,
        body,
        specialist_context,
        allowed_files,
        read_allowed_files_content(repo_path, allowed_files),
        last_error,
        describe_verification(repo_path, profile),
    )
    claude_ok = run_claude_escalation(claude_bin, prompt, repo_path, int(min(claude_timeout, remaining_seconds)), log_path)

    apply_guard(repo_path, allowed_files, log_path, tag, "claude")
    if not changed_files(repo_path):
        return False, "Claude no genero cambios utilizables" + ("" if claude_ok else " (timeout o error)")

    ok, error, _ = verify_changes(repo_path, allowed_files, log_path, profile)
    if ok:
        return True, ""
    return False, f"la solucion de Claude tampoco paso la verificacion: {error}"


def process_issue(config: dict, issue: dict, profile: dict, escalation_state: dict) -> tuple[str, str]:
    """Resuelve un issue con el especialista `profile`. Devuelve (mensaje,
    resolutor), con resolutor en `SOLVER_AIDER`/`SOLVER_CLAUDE`/`SOLVER_NONE`."""
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
        return msg, SOLVER_NONE

    if not repo_is_clean(repo_path):
        msg = f"⚠️ {tag}: el workspace tiene cambios sin commitear, se omite para no pisarlos"
        log.warning(msg)
        return msg, SOLVER_NONE

    body = relativize_paths_in_body(issue["body"], config, issue["repo"])
    allowed_files = extract_allowed_files(body)
    specialist_context = build_specialist_context(config, profile, f"{issue['title']}\n{body}")

    deadline = datetime.now() + timedelta(hours=MAX_HOURS_PER_ISSUE)
    written: list[str] = []
    previous_error: Optional[str] = None
    last_full_output = ""
    attempts = 0
    while attempts < aider_attempts:
        if datetime.now() >= deadline:
            msg = f"⏱️ {tag}: se alcanzo el limite de {MAX_HOURS_PER_ISSUE}h, se pasa al siguiente issue"
            log.warning(msg)
            return msg, SOLVER_NONE

        attempts += 1
        attempt_log_path = REPORTS_DIR / f"aider-{repo_short_name}-{issue['number']}-{attempts}.log"

        if len(allowed_files) > 1:
            aider_ok = True
            for rel_path in allowed_files:
                file_tag = re.sub(r"[^\w.-]", "_", rel_path)
                file_log_path = REPORTS_DIR / f"aider-{repo_short_name}-{issue['number']}-{attempts}-{file_tag}.log"
                file_message = build_aider_message(
                    issue, body, only_file=rel_path, previous_error=previous_error,
                    specialist_context=specialist_context,
                )
                if not run_aider(repo_path, aider_model, file_message, [rel_path], PER_FILE_AIDER_TIMEOUT, file_log_path):
                    log.warning(
                        "%s: aider fallo con el archivo %s (intento %d/%d)", tag, rel_path, attempts, aider_attempts
                    )
                    aider_ok = False
        else:
            message = build_aider_message(
                issue, body, previous_error=previous_error, specialist_context=specialist_context
            )
            aider_ok = run_aider(repo_path, aider_model, message, allowed_files, aider_timeout, attempt_log_path)

        # Candado: se aplica SIEMPRE, incluso si aider termino por timeout o
        # error, para revertir rutas prohibidas/archivos fuera de la lista
        # permitida y borrar los archivos permitidos que hayan quedado vacios.
        apply_guard(repo_path, allowed_files, attempt_log_path, tag, "aider")

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
            ok, error, full_output = verify_changes(repo_path, allowed_files, attempt_log_path, profile)
            if ok:
                break
            log.warning("%s: intento %d/%d fallo la verificacion: %s", tag, attempts, aider_attempts, error)
            # No se descartan los cambios: el siguiente intento parte del
            # archivo ya generado para que aider lo corrija, en vez de
            # arrancar de cero.
            previous_error = last_lines(full_output, VERIFICATION_FEEDBACK_LINES)
            last_full_output = full_output
            written = []
        log.warning("%s: intento %d/%d con aider sin resultado util", tag, attempts, aider_attempts)

    solver = SOLVER_AIDER
    if not written:
        solved, reason = escalate_to_claude(
            config, issue, profile, repo_path, body, specialist_context,
            allowed_files, last_full_output, deadline, escalation_state,
        )
        if not solved:
            mark_needs_review(
                issue["repo_obj"],
                issue["issue_obj"],
                f"Aider ({aider_model}) no genero una solucion utilizable tras {aider_attempts} intentos; {reason}.",
                profile,
            )
            msg = f"🚫 {tag}: aider fallo {aider_attempts} veces y {reason}; marcado 'needs-review'"
            log.error(msg)
            return msg, SOLVER_NONE
        solver = SOLVER_CLAUDE
        written = changed_files(repo_path)

    commit_msg = f"feat({profile['name']}): resuelve #{issue['number']} - {issue['title']}\n\nResuelto por: {solver}"
    if commit_and_push(repo_path, branch, commit_msg):
        msg = (
            f"✅ {tag}: {len(written)} archivo(s) actualizados en `{branch}` "
            f"({', '.join(written)}): {issue['url']}"
        )
        log.info(msg)
        return msg, solver

    return f"ℹ️ {tag}: {solver} respondio pero no genero cambios reales en el repo", SOLVER_NONE


def record_specialist_memory(profile: dict, issue: dict, message: str, solver: str, branch: str) -> None:
    """Registra en batcave/memory/<especialista>.md quien resolvio el issue."""
    name = profile["name"]
    problema = memory_store.table_cell(f"Issue {issue['repo']}#{issue['number']}: {issue['title']}")
    try:
        memory_store.ensure_memory_file(
            name,
            profile.get("display_name") or name.capitalize(),
            f"Especialista {profile['role']} del modo Desarrollo (batcave/specialists/{name}.yaml)",
        )
        memory_store.touch_last_activity(name)
        if solver != SOLVER_NONE:
            memory_store.append_success(
                name, problema=problema, como_se_resolvio=f"Resuelto por {solver}, pusheado a {branch}"
            )
        else:
            memory_store.append_error(
                name,
                error=problema,
                causa=memory_store.table_cell(message[:300]),
                solucion=f"resuelto por: {SOLVER_NONE}",
                resultado="sin resolver",
            )
    except (OSError, ValueError) as exc:
        log.warning("No se pudo actualizar la memoria de %s: %s", name, exc)


def format_result_line(message: str, profile: dict, solver: str) -> str:
    """Linea de resultado para reporte/Telegram. night_agent.py la parsea
    (`especialista: X, resuelto por: Y`) para la memoria de Batman."""
    display = profile.get("display_name") or profile["name"].capitalize()
    return f"{message} — especialista: {display}, resuelto por: {solver}"


def write_development_report(started_at: datetime, results: list[dict], escalation_state: Optional[dict] = None) -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    date_str = started_at.strftime("%Y-%m-%d")
    report_path = REPORTS_DIR / f"development-{date_str}.md"
    lines = [f"# Reporte de Modo Desarrollo - {date_str}", ""]
    if results:
        solvers = Counter(r["solver"] for r in results)
        lines.extend(["## Resumen", f"- Issues procesados: {len(results)}"])
        lines.extend(f"- Resueltos por {s}: {solvers.get(s, 0)}" for s in (SOLVER_AIDER, SOLVER_CLAUDE))
        lines.append(f"- Sin resolver ({SOLVER_NONE}): {solvers.get(SOLVER_NONE, 0)}")
        if escalation_state:
            lines.append(f"- Escalaciones a Claude usadas: {escalation_state['used']}/{escalation_state['max']}")
        lines.extend(["", "## Detalle", "| Issue | Especialista | Resuelto por | Resultado |", "|---|---|---|---|"])
        lines.extend(
            f"| {r['tag']} | {r['specialist']} | {r['solver']} | {memory_store.table_cell(r['message'])} |"
            for r in results
        )
    else:
        lines.append("No habia issues con label 'night-agent' pendientes.")
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_path


async def run_development_mode(config: dict, notifier) -> str:
    """Punto de entrada del modo Desarrollo: enruta cada issue a su
    especialista y lo resuelve con Aider (backend Ollama), escalando a Claude
    si Aider agota sus intentos."""
    load_env()
    started_at = datetime.now()

    gh = get_github_client()
    issues = get_open_issues(gh, config)

    if not issues:
        report_path = write_development_report(started_at, [])
        return f"Sin issues con label '{config['github']['issue_label']}' pendientes. Reporte: {report_path}"

    specialists = load_specialists(config)
    default_name = default_specialist_name(config, specialists)
    issue_label = config["github"]["issue_label"]
    max_escalations = int(config.get("development", {}).get("max_claude_escalations", DEFAULT_MAX_CLAUDE_ESCALATIONS))
    escalation_state = {"max": max_escalations, "remaining": max_escalations, "used": 0}

    routed = [(issue, *route_issue(issue, specialists, default_name, issue_label)) for issue in issues]
    per_specialist = Counter(specialist_display(specialists[name]) for _, name, _ in routed)
    await notifier.send(
        f"🦇 *Equipo de desarrollo — Modo Desarrollo (Aider)*\n{len(issues)} issue(s) por resolver "
        f"con label `{issue_label}`:\n" + "\n".join(f"{who}: {n}" for who, n in per_specialist.items())
    )

    results: list[dict] = []
    worked: set[str] = set()
    last_notified = datetime.now()
    for idx, (issue, name, reason) in enumerate(routed, start=1):
        profile = specialists[name]
        tag = f"{issue['repo']}#{issue['number']}"
        log.info(
            "%s trabajando en issue #%d (%d/%d): %s (%s)",
            specialist_display(profile), issue["number"], idx, len(issues), tag, reason,
        )
        try:
            message, solver = process_issue(config, issue, profile, escalation_state)
        except Exception as exc:  # noqa: BLE001 - un issue no debe tumbar el resto de la corrida
            log.exception("Fallo inesperado procesando %s", tag)
            message, solver = f"❌ {tag}: error inesperado: {exc}", SOLVER_NONE
        record_specialist_memory(profile, issue, message, solver, config["github"]["work_branch"])
        worked.add(name)
        results.append(
            {
                "tag": tag,
                "specialist": profile.get("display_name") or name,
                "solver": solver,
                "message": message,
                "line": format_result_line(message, profile, solver),
            }
        )

        if datetime.now() - last_notified >= PROGRESS_INTERVAL:
            await notifier.send(
                "🦇 *Equipo de desarrollo — progreso*\n"
                f"{idx}/{len(issues)} issue(s) procesados hasta ahora:\n" + "\n".join(r["line"] for r in results)
            )
            last_notified = datetime.now()

    for name in worked:
        try:
            memory_store.bump_stat(name, "ciclos")
        except OSError as exc:
            log.warning("No se pudo actualizar la memoria de %s: %s", name, exc)

    report_path = write_development_report(started_at, results, escalation_state)
    return (
        "\n".join(r["line"] for r in results)
        + f"\n\nEscalaciones a Claude: {escalation_state['used']}/{escalation_state['max']}"
        + f"\nReporte guardado en {report_path}"
    )
