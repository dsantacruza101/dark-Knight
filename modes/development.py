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
resolvio cada issue (`deterministic-patch`, `aider-qwen`, `claude-escalation`,
`none`) queda en el reporte, en el progreso de Telegram y en
`batcave/memory/<especialista>.md`.

Parches de dependencias del radar (label `dependencies` + marcador
`<!-- radar:`): NUNCA pasan por Aider ni Claude (no consumen escalaciones).
`process_dependency_patch` lee el bloque ```radar-patch``` (o, en issues
viejos, las lineas Paquete/Version del cuerpo) y aplica: npm directa ->
`npm install [-D] <pkg>@<to>` (-D segun package.json), y si el paquete es de
una familia de `development.package_families` (nestjs, angular, react...)
sube con el a todos los miembros presentes en package.json (un
`npm install` para los de produccion y uno `-D` para los de desarrollo);
npm transitiva -> `npm update <pkg>` y, si OSV aun ve una version
vulnerable en el lock, `overrides: {<pkg>: <to>}` en package.json +
`npm install`. En npm, `npm ls --depth=0` despues de instalar: peers
invalidos o faltantes fallan antes de los tests. PyPI -> fija
`<pkg>==<to>` en requirements.txt. Luego corre la verificacion del perfil,
re-chequea OSV (los advisories del issue ya no deben afectar a ninguna
version del lock), stagea solo package.json/package-lock.json (o
requirements.txt) y comitea `fix(deps): <pkg> <from> -> <to> (radar)`.
Exito -> comentario `Aplicado en <rama>: <sha>, verificado` + label `in-dev`
(los issues `in-dev` se omiten); ya corregido segun OSV -> se cierra como
completado; fallo -> `needs-review` con las ultimas 30 lineas del paso que
fallo. Log en `reports/deps-<repo>-<issue>.log` (termina con un resumen de
comandos y resultado).

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
  - La rama de trabajo es por repo (`resolve_work_branch`): el `work_branch`
    de la entrada del repo en `repos` de un perfil (p. ej.
    `my_Portfolio_React_v2` -> `development`), o `github.work_branch`
    (`dev`). Nunca main/master.
  - Si `origin` no tiene la rama de trabajo, se crea
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
  5. Candado: primero se borran los artefactos de Aider (`.aider*`, ver
     `remove_aider_artifacts`; Aider ademas los agrega al `.gitignore`, y al
     revertir ese cambio quedarian visibles para git). Los cambios en rutas
     prohibidas se revierten
     (`revert_forbidden_changes`), y cualquier archivo modificado o creado
     que no este en la lista de archivos permitidos tambien se revierte
     (`revert_unlisted_changes`); los archivos permitidos que queden vacios
     o solo con espacios/comentarios se borran (`delete_empty_allowed_files`).
     Este candado se aplica SIEMPRE, incluso si aider termino por timeout o
     error, antes de verificar o comitear.
  6. Verificacion antes de comitear (`verify_changes`): sin lista de
     archivos permitidos no hay intento posible (needs-review directo). Cada
     archivo permitido debe existir, no estar vacio, diferir de su version
     en `origin/<work_branch>` (o ser nuevo) y pasar una validacion
     minima segun su tipo (`validate_allowed_file_content`): `.py` debe
     tener al menos un `def`/`class`; `.service`
     debe tener `[Unit]`/`[Service]` y `ExecStart=`; `.timer` debe tener
     `[Timer]` y `OnCalendar=`/`OnUnitActiveSec=`; `.spec.ts`/`.test.ts`
     debe tener `describe(`/`it(`/`test(`. Ademas se corren los pasos de
     `verification` del perfil del especialista para cada stack detectado
     (p. ej. Node: npm ci si hace falta, build, lint, test; Python:
     py_compile + pytest). Cada archivo de test permitido (`*.spec.ts`,
     `*.test.ts`, `test_*.py`) se corre aparte con su runner apuntando solo a
     ese archivo (`run_test_file`, runner segun `test_runner` del perfil o
     detectado: `npx vitest run <archivo> --reporter=json`, jest
     `--runTestsByPath` con `--json`, o `pytest <archivo>`): debe ejecutar
     mas de 0 tests y 0 fallidos.
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
     Nunca `git add -A`: `commit_allowed_and_push` borra los `.aider*`,
     stagea solo `git add -- <archivo permitido>` y exige que
     `git diff --cached --name-only` sea un subconjunto no vacio de la lista
     permitida; si no, aborta el commit y el intento cuenta como fallido.
     El reporte incluye una autoverificacion: archivos exactos del commit y
     tests ejecutados por archivo de test.

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
import tempfile
from collections import Counter
from datetime import datetime, timedelta
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import requests
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
DEFAULT_WORK_BRANCH = "dev"
PROTECTED_BRANCHES = ("main", "master")
TEST_RUNNERS = ("auto", "vitest", "jest", "pytest")
VITEST_CONFIG_GLOBS = ("vitest.config.*", "vitest.workspace.*")

DEFAULT_MAX_CLAUDE_ESCALATIONS = 2
DEFAULT_CLAUDE_TIMEOUT = 1800
ESCALATION_ERROR_LINES = 80
ESCALATION_MAX_FILE_LINES = 400
MIN_ESCALATION_SECONDS = 600

SOLVER_AIDER = "aider-qwen"
SOLVER_CLAUDE = "claude-escalation"
SOLVER_DETERMINISTIC = "deterministic-patch"
SOLVER_NONE = "none"

# Parches de dependencias del radar (`batcave/radar.py`): se aplican sin Aider ni Claude.
DEPENDENCIES_LABEL = "dependencies"
RADAR_MARKER = "<!-- radar:"
RADAR_PATCH_BLOCK_RE = re.compile(r"```radar-patch[ \t]*\n(.*?)\n[ \t]*```", re.DOTALL)
LEGACY_PACKAGE_RE = re.compile(
    r"\*\*Paquete:\*\*\s*`([^`]+)`\s*\((\w+), dependencia (directa|transitiva) de (desarrollo|produccion), `([^`]+)`\)"
)
LEGACY_CURRENT_RE = re.compile(r"\*\*Version actual:\*\*\s*`([^`]+)`")
LEGACY_TARGET_RE = re.compile(r"\*\*Version objetivo:\*\*\s*`([^`]+)`")
LEGACY_ADVISORY_RE = re.compile(r"^- \[([A-Za-z0-9-]+)\]\(https://osv\.dev/", re.MULTILINE)
NPM_PACKAGE_NAME_RE = re.compile(r"^(?:@[a-z0-9][\w.-]*/)?[a-z0-9][\w.-]*$", re.IGNORECASE)
PYPI_PACKAGE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
PATCH_VERSION_RE = re.compile(r"^\d+(?:\.\d+)*(?:[-+.]?[A-Za-z0-9][\w.-]*)?$")
REQ_PIN_LINE_RE = re.compile(
    r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*"
    r"((?:(?:===|==|~=|>=|>|<=|<|!=)\s*[^;#\s,]+\s*,?\s*)*)(.*)$"
)
PATCH_FILES = {"npm": ["package.json", "package-lock.json"], "PyPI": ["requirements.txt"]}
NPM_QUIET_FLAGS = ["--no-audit", "--no-fund"]
OSV_QUERYBATCH_URL = "https://api.osv.dev/v1/querybatch"
OSV_TIMEOUT = 30
IN_DEV_LABEL = "in-dev"   # parche aplicado y verificado en la rama de trabajo: no se reprocesa
NPM_REGISTRY_URL = "https://registry.npmjs.org"
NPM_ABBREVIATED_ACCEPT = "application/vnd.npm.install-v1+json"
NPM_REGISTRY_TIMEOUT = 30
# Problemas de `npm ls` (npm 7+): "x@1 invalid: ...", "npm error missing: ...", "UNMET DEPENDENCY x".
# Los peers opcionales ("UNMET OPTIONAL DEPENDENCY") no cuentan.
NPM_LS_PROBLEM_RE = re.compile(r"\b(?:invalid|missing):|UNMET (?:PEER )?DEPENDENCY|peer dep missing")
FAILURE_COMMENT_LINES = 30

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

FILE_MENTION_RE = re.compile(r"(?<![\w/])[\w./\-]+\.(?:py|ts|tsx|jsx|css|scss|html|service|timer|md|yaml)(?![\w])")
FILES_SECTION_HEADER_RE = re.compile(r"^\s*archivos\s*:\s*$", re.IGNORECASE)
SERVICE_CHECK_LINE_RE = re.compile(r"systemctl|is-active|docker", re.IGNORECASE)
TEST_FILE_NAME_RE = re.compile(r"^test_.+\.py$|.+\.(?:spec|test)\.[jt]sx?$")
NODE_TEST_COUNT_RE = re.compile(r"Tests:\s*(?:(\d+) failed,\s*)?(?:(\d+) skipped,\s*)?(\d+) passed")
PYTEST_PASSED_RE = re.compile(r"(\d+)\s+passed")
PYTEST_FAILED_RE = re.compile(r"(\d+)\s+failed")
PYTEST_ERRORS_RE = re.compile(r"(\d+)\s+errors?\b")
PYTEST_SUMMARY_LINE_RE = re.compile(r"^.*\bin [\d.]+s\b.*$", re.MULTILINE)
JEST_TESTS_LINE_RE =re.compile(r"^Tests:\s+(.+)$", re.MULTILINE)
# Resumen de vitest: " Tests  1 failed | 103 passed (104)" (sin dos puntos).
VITEST_TESTS_LINE_RE = re.compile(r"^\s*Tests\s+(\d+ (?:failed|passed|skipped|todo).*)$", re.MULTILINE)
# vitest/picocolors colorean la salida si `CI` esta en el entorno, aun sin TTY.
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
AIDER_ARTIFACT_PREFIX = ".aider"
ARTIFACT_SCAN_SKIP_DIRS = {".git", "node_modules"}
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
    ".tsx": ("//", "/*", "*"),
    ".js": ("//", "/*", "*"),
    ".jsx": ("//", "/*", "*"),
    ".css": ("/*", "*"),
    ".scss": ("//", "/*", "*"),
    ".html": ("<!--",),
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
                labels = [lbl.name for lbl in issue.labels]
                if IN_DEV_LABEL in labels:
                    log.info("%s#%d tiene '%s' (ya aplicado en la rama de trabajo), se omite", repo_name, issue.number, IN_DEV_LABEL)
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
                        "labels": labels,
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
        if not validate_profile_repos(path.name, profile):
            continue
        runner = str(profile.get("test_runner", "auto")).lower()
        if runner not in TEST_RUNNERS:
            log.warning("Perfil %s: test_runner '%s' desconocido, se usa 'auto'", path.name, runner)
            runner = "auto"
        profile["test_runner"] = runner
        specialists[profile["name"]] = profile
    if not specialists:
        raise RuntimeError(f"No hay perfiles de especialista validos en {specialists_dir}")
    return specialists


def validate_profile_repos(file_name: str, profile: dict) -> bool:
    """Cada entrada de `repos` es un nombre (str) o `{name, work_branch}`.
    Un `work_branch` main/master invalida el perfil (regla dura)."""
    for entry in profile.get("repos") or []:
        if isinstance(entry, str):
            continue
        if not isinstance(entry, dict) or not entry.get("name"):
            log.error("Perfil %s: entrada de repos invalida (%r), se omite el perfil", file_name, entry)
            return False
        branch = entry.get("work_branch")
        if branch is not None and str(branch) in PROTECTED_BRANCHES:
            log.error(
                "Perfil %s: work_branch '%s' para %s esta prohibido (main/master), se omite el perfil",
                file_name, branch, entry["name"],
            )
            return False
    return True


def profile_repos(profile: dict) -> list[str]:
    """Nombres de repo (sin owner) del perfil, sea la entrada str o dict."""
    return [
        str(entry["name"] if isinstance(entry, dict) else entry)
        for entry in profile.get("repos") or []
        if isinstance(entry, str) or (isinstance(entry, dict) and entry.get("name"))
    ]


def resolve_work_branch(
    config: dict, repo: str, specialists: Optional[dict[str, dict]] = None, preferred: Optional[dict] = None
) -> str:
    """Rama de integracion de `repo` (`owner/repo` o nombre corto): el
    `work_branch` de su entrada en `repos` de un perfil (primero `preferred`,
    luego el resto), o `github.work_branch` (default `dev`). La rama es del
    repo, no del especialista: un issue de Nightwing en un repo de Batgirl
    usa la rama de ese repo."""
    short = repo.split("/")[-1].lower()
    profiles = ([preferred] if preferred else []) + list((specialists or {}).values())
    for profile in profiles:
        for entry in profile.get("repos") or []:
            if isinstance(entry, dict) and str(entry.get("name", "")).lower() == short and entry.get("work_branch"):
                return str(entry["work_branch"])
    return str(config.get("github", {}).get("work_branch") or DEFAULT_WORK_BRANCH)


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
    owned = profile_repos(specialists[name])
    if owned and repo_short_name not in owned:
        reason += f"; ojo: {repo_short_name} no esta en sus repos"
    return name, reason


def build_specialist_preamble(profile: dict) -> str:
    """Preambulo corto (max `PREAMBLE_MAX_LINES` lineas): rol, stack y convenciones."""
    lines = [
        f"Actuas como {profile.get('display_name') or profile['name']}, "
        f"especialista {profile['role']} del equipo de desarrollo."
    ]
    for key, title in (
        ("languages", "Lenguajes"), ("frameworks", "Frameworks"), ("ui", "UI"), ("testing", "Testing"),
        ("orms", "ORMs"), ("messaging", "Mensajeria"),
    ):
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


def normalize_rel_path(rel_path: str) -> str:
    """Quita espacios y prefijos `./` (no `lstrip("./")`, que tambien le
    quitaria el punto inicial a `.env`, `.ssh` o `.aider*`)."""
    normalized = rel_path.strip()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.rstrip("/")


def is_forbidden_path(rel_path: str) -> bool:
    normalized = normalize_rel_path(rel_path).lower()
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
    allowed = {normalize_rel_path(f) for f in allowed_files}
    reverted: list[str] = []
    for rel_path in changed_files(repo_path):
        if normalize_rel_path(rel_path) in allowed:
            continue
        run_cli(["git", "checkout", "--", rel_path], cwd=repo_path)
        run_cli(["git", "clean", "-fd", "--", rel_path], cwd=repo_path)
        reverted.append(rel_path)
    if reverted:
        append_to_log(log_path, "candado (fuera de la lista permitida, revertidos)", "\n".join(reverted))
    return reverted


def remove_aider_artifacts(repo_path: Path) -> list[str]:
    """Borra todo archivo/directorio `.aider*` del workspace (historial de
    chat, input, cache de tags). Recorre el disco en vez de `git status`
    porque Aider los agrega al `.gitignore`: mientras ese cambio exista git
    no los ve, y al revertirlo el candado los dejaria listos para comitear."""
    removed: list[str] = []
    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d not in ARTIFACT_SCAN_SKIP_DIRS]
        for name in [d for d in dirs if d.startswith(AIDER_ARTIFACT_PREFIX)]:
            shutil.rmtree(Path(root) / name, ignore_errors=True)
            dirs.remove(name)
            removed.append(str((Path(root) / name).relative_to(repo_path)) + "/")
        for name in files:
            if name.startswith(AIDER_ARTIFACT_PREFIX):
                (Path(root) / name).unlink(missing_ok=True)
                removed.append(str((Path(root) / name).relative_to(repo_path)))
    if removed:
        log.info("%s: artefactos de Aider eliminados: %s", repo_path.name, ", ".join(removed))
    return removed


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
        elif TEST_FILE_NAME_RE.match(name) and not name.endswith(".py"):
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
    if branch in PROTECTED_BRANCHES:
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


def commit_allowed_and_push(
    repo_path: Path, branch: str, message: str, allowed_files: list[str]
) -> tuple[list[str], str, bool]:
    """Commit del modo Desarrollo: nunca `git add -A`. Borra los `.aider*`,
    stagea solo `git add -- <archivo>` por cada archivo permitido y exige que
    `git diff --cached --name-only` sea un subconjunto NO vacio de la lista
    permitida antes de comitear; si no, vacia el index y aborta.

    Devuelve (archivos del commit segun `git show HEAD`, error si se aborto,
    push ok). Lista vacia = no hubo commit (el intento cuenta como fallido)."""
    remove_aider_artifacts(repo_path)
    allowed = [normalize_rel_path(f) for f in allowed_files]
    if not allowed:
        return [], "sin lista de archivos permitidos no se comitea", False

    run_cli(["git", "reset", "-q"], cwd=repo_path)
    for rel in allowed:
        add = run_cli(["git", "add", "--", rel], cwd=repo_path)
        if add.returncode != 0:
            run_cli(["git", "reset", "-q"], cwd=repo_path)
            return [], f"git add fallo en {rel}: {mask_secrets(add.stderr.strip()[-300:])}", False

    staged = [
        normalize_rel_path(line)
        for line in run_cli(["git", "diff", "--cached", "--name-only"], cwd=repo_path).stdout.splitlines()
        if line.strip()
    ]
    outside = sorted(set(staged) - set(allowed))
    if not staged or outside:
        run_cli(["git", "reset", "-q"], cwd=repo_path)
        reason = (
            "no hay cambios staged en los archivos permitidos"
            if not staged
            else f"el index contiene archivos fuera de la lista permitida: {', '.join(outside)}"
        )
        log.error("%s: commit abortado, %s", repo_path.name, reason)
        return [], f"commit abortado: {reason}", False

    commit = run_cli(["git", "commit", "-m", message], cwd=repo_path)
    if commit.returncode != 0:
        run_cli(["git", "reset", "-q"], cwd=repo_path)
        return [], f"git commit fallo: {mask_secrets((commit.stdout + commit.stderr).strip()[-300:])}", False

    committed = [
        line.strip()
        for line in run_cli(["git", "show", "--name-only", "--pretty=format:", "HEAD"], cwd=repo_path).stdout.splitlines()
        if line.strip()
    ]
    push = run_cli(git_cmd(["push", "origin", branch]), cwd=repo_path, extra_env=github_auth_env())
    if push.returncode != 0:
        log.error("Fallo el push a %s: %s", branch, mask_secrets(push.stderr[-500:]))
    return committed, "", push.returncode == 0


def strip_ansi(text: str) -> str:
    return ANSI_ESCAPE_RE.sub("", text)


def vitest_summary_counts(output: str) -> Optional[tuple[int, int]]:
    """(passed, failed) de la linea `Tests  ...` de vitest, o None si no esta."""
    matches = VITEST_TESTS_LINE_RE.findall(strip_ansi(output))
    if not matches:
        return None
    summary = matches[-1]
    passed = sum(int(n) for n in re.findall(r"(\d+) passed", summary))
    failed = sum(int(n) for n in re.findall(r"(\d+) failed", summary))
    return passed, failed


def count_node_tests_detected(output: str) -> int:
    """Tests ejecutados (passed + failed) segun el resumen de jest o de vitest."""
    clean = strip_ansi(output)
    match = NODE_TEST_COUNT_RE.search(clean)
    if match:
        return int(match.group(1) or 0) + int(match.group(3) or 0)
    vitest = vitest_summary_counts(clean)
    return sum(vitest) if vitest else 0


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


def npm_ci_if_needed(repo_path: Path) -> Optional[subprocess.CompletedProcess]:
    """Antes de verificar en repos Node, instala dependencias con `npm ci` si
    `node_modules` no existe o si `package-lock.json` cambio desde la ultima
    instalacion (hash guardado dentro de `node_modules`, que sobrevive al
    `git clean -fd` de `setup_workspace_repo` por estar gitignoreado).
    Devuelve el resultado de `npm ci`, o None si no hizo falta."""
    lockfile = repo_path / "package-lock.json"
    node_modules = repo_path / "node_modules"
    hash_marker = node_modules / ".night-agent-lockfile-hash"

    current_hash = hashlib.sha256(lockfile.read_bytes()).hexdigest() if lockfile.exists() else None
    previous_hash = hash_marker.read_text(encoding="utf-8").strip() if hash_marker.exists() else None

    if node_modules.exists() and current_hash == previous_hash:
        return None

    log.info("%s: instalando dependencias (npm ci)", repo_path.name)
    proc = run_cli(["npm", "ci"], cwd=repo_path, timeout=NPM_CI_TIMEOUT)
    if current_hash is not None and node_modules.exists():
        hash_marker.write_text(current_hash, encoding="utf-8")
    return proc


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
    repo_path: Path,
    profile: dict,
    allowed_files: list[str],
    test_files: list[str],
    step_log: Optional[list[tuple[str, bool, str, float]]] = None,
) -> tuple[list[str], list[str]]:
    """Corre los pasos de `verification` del perfil para cada stack detectado.

    Si se pasa `step_log`, agrega (comando, ok, detalle, segundos) por cada
    paso ejecutado (resumen del log de parches de dependencias).
    Devuelve (errores cortos para log/needs-review, salida completa de cada
    paso fallido para el log del intento y la retroalimentacion)."""
    errors: list[str] = []
    full_outputs: list[str] = []

    def record(label: str, ok: bool, detail: str, started: datetime) -> None:
        if step_log is not None:
            step_log.append((label, ok, detail, (datetime.now() - started).total_seconds()))

    for stack_name, stack in detected_stacks(repo_path, profile):
        for step in stack.get("steps", []):
            if not step_condition_met(repo_path, step.get("when")):
                continue
            builtin = step.get("builtin")
            started = datetime.now()
            if builtin == "npm_ci_if_needed":
                ci = npm_ci_if_needed(repo_path)
                if ci is not None:
                    record("npm ci", ci.returncode == 0, f"codigo {ci.returncode}", started)
                continue
            if builtin == "py_compile_changed":
                py_files = sorted(
                    {f for f in changed_files(repo_path) if f.endswith(".py")}
                    | {f for f in allowed_files if f.endswith(".py")}
                )
                py_errors, py_full = verify_python_files(repo_path, py_files)
                errors.extend(py_errors)
                full_outputs.extend(py_full)
                record(f"py_compile ({len(py_files)} archivo(s))", not py_errors, f"{len(py_errors)} error(es)", started)
                continue
            if builtin or not step.get("cmd"):
                log.warning("Paso de verificacion invalido en %s/%s: %s", profile["name"], stack_name, step)
                continue

            command = str(step["cmd"])
            env, cmd = split_env_prefix(command)
            proc = run_cli(cmd, cwd=repo_path, timeout=int(step.get("timeout", DEFAULT_STEP_TIMEOUT)), extra_env=env or None)
            output = mask_secrets(proc.stdout + proc.stderr)
            record(command, proc.returncode == 0, f"codigo {proc.returncode}", started)
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


def unchanged_vs_remote(repo_path: Path, branch: str, allowed_files: list[str]) -> list[str]:
    """Archivos permitidos identicos a su version en `origin/<branch>` (los
    que no existen en el remoto cuentan como nuevos, o sea cambiados)."""
    unchanged: list[str] = []
    for rel in allowed_files:
        full = repo_path / rel
        if not full.is_file():
            continue
        remote = run_cli(["git", "rev-parse", "--verify", "-q", f"origin/{branch}:{normalize_rel_path(rel)}"], cwd=repo_path)
        if remote.returncode != 0:
            continue
        local = run_cli(["git", "hash-object", "--", rel], cwd=repo_path)
        if local.returncode == 0 and local.stdout.strip() == remote.stdout.strip():
            unchanged.append(rel)
    return unchanged


def parse_jest_json(report_path: Path, test_path: Path) -> Optional[tuple[int, int]]:
    """(ejecutados, fallidos) de `test_path` en el reporte `--json` de jest,
    o None si el reporte no existe o no menciona ese archivo."""
    try:
        data = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    for suite in data.get("testResults", []):
        if Path(suite.get("name", "")).resolve() != test_path.resolve():
            continue
        statuses = [a.get("status") for a in suite.get("assertionResults", [])]
        failed = statuses.count("failed")
        if suite.get("status") == "failed" and failed == 0:
            failed = 1  # la suite no compilo / reventó antes de correr tests
        return statuses.count("passed") + statuses.count("failed"), failed
    return None


def detect_test_runner(repo_path: Path, rel: str, configured: str = "auto") -> str:
    """Runner para el archivo de test `rel`. `.py` siempre pytest. Para tests
    JS/TS: `vitest`/`jest` del perfil (`test_runner`) si se fijo uno; con
    `auto` (o `pytest`, que no aplica a JS), vitest si esta en las
    dependencias de package.json o hay `vitest.config.*`, si no jest."""
    if rel.endswith(".py"):
        return "pytest"
    if configured in ("vitest", "jest"):
        return configured
    package_json = repo_path / "package.json"
    try:
        pkg = json.loads(package_json.read_text(encoding="utf-8")) if package_json.exists() else {}
    except (OSError, json.JSONDecodeError):
        pkg = {}
    deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
    if "vitest" in deps or any(any(repo_path.glob(pattern)) for pattern in VITEST_CONFIG_GLOBS):
        return "vitest"
    return "jest"


def run_vitest_file(repo_path: Path, rel: str) -> tuple[int, int, str]:
    """`npx vitest run <rel> --reporter=json --outputFile=<tmp>`. El reporte
    JSON de vitest es compatible con el de jest (`parse_jest_json`); fallback:
    linea `Tests  ...` del texto, exigiendo que el archivo aparezca en la
    salida. Ojo: el posicional de vitest es un filtro por substring, por eso
    el reporte se filtra por la ruta exacta."""
    with tempfile.TemporaryDirectory(prefix="night-agent-vitest-") as tmp:
        report_path = Path(tmp) / "vitest.json"
        proc = run_cli(
            ["npx", "vitest", "run", rel, "--reporter=json", f"--outputFile={report_path}"],
            cwd=repo_path,
            timeout=DEFAULT_STEP_TIMEOUT,
            extra_env={"CI": "true"},
        )
        output = mask_secrets(proc.stdout + proc.stderr)
        parsed = parse_jest_json(report_path, repo_path / rel)
    if parsed is not None:
        executed, failed = parsed
    else:
        counts = vitest_summary_counts(output)
        if counts is None or Path(rel).name not in strip_ansi(output):
            return 0, 0, output
        passed, failed = counts
        executed = passed + failed
    if proc.returncode != 0 and failed == 0:
        failed = 1
    return executed, failed, output


def run_test_file(repo_path: Path, rel: str, runner: str = "auto") -> tuple[int, int, str]:
    """Corre el runner apuntando SOLO a `rel`. Devuelve (tests ejecutados,
    fallidos, salida). El runner sale de `detect_test_runner` (`test_runner`
    del perfil: auto|vitest|jest|pytest). Vitest: `run_vitest_file`. Jest:
    `npm test -- --runTestsByPath <rel>` con `--json` a un archivo temporal
    fuera del repo (fallback: linea `Tests:` del texto, exigiendo que el
    archivo aparezca en la salida). Python: `pytest <rel>`; ejecutados =
    passed + failed + errors."""
    runner = detect_test_runner(repo_path, rel, runner)
    if runner == "vitest":
        return run_vitest_file(repo_path, rel)
    if runner == "pytest":
        proc = run_cli(
            ["python3", "-m", "pytest", rel, "-q", "-p", "no:cacheprovider"], cwd=repo_path, timeout=DEFAULT_STEP_TIMEOUT
        )
        output = mask_secrets(proc.stdout + proc.stderr)
        summaries = PYTEST_SUMMARY_LINE_RE.findall(output)
        summary = summaries[-1] if summaries else ""
        counts = {
            name: sum(int(n) for n in regex.findall(summary))
            for name, regex in (("passed", PYTEST_PASSED_RE), ("failed", PYTEST_FAILED_RE), ("errors", PYTEST_ERRORS_RE))
        }
        failed = counts["failed"] + counts["errors"]
        if proc.returncode not in (0, 5) and failed == 0:
            failed = 1
        return counts["passed"] + failed, failed, output

    with tempfile.TemporaryDirectory(prefix="nightwing-jest-") as tmp:
        report_path = Path(tmp) / "jest.json"
        proc = run_cli(
            ["npm", "test", "--", "--runTestsByPath", rel, "--watchAll=false", "--ci",
             "--json", f"--outputFile={report_path}"],
            cwd=repo_path,
            timeout=DEFAULT_STEP_TIMEOUT,
            extra_env={"CI": "true"},
        )
        output = mask_secrets(proc.stdout + proc.stderr)
        parsed = parse_jest_json(report_path, repo_path / rel)
    if parsed is not None:
        executed, failed = parsed
    else:
        match = JEST_TESTS_LINE_RE.search(output)
        if not match or Path(rel).name not in output:
            return 0, 0, output
        summary = match.group(1)
        passed = sum(int(n) for n in re.findall(r"(\d+) passed", summary))
        failed = sum(int(n) for n in re.findall(r"(\d+) failed", summary))
        executed = passed + failed
    if proc.returncode != 0 and failed == 0:
        failed = 1
    return executed, failed, output


def verify_test_files(
    repo_path: Path, test_files: list[str], runner: str = "auto"
) -> tuple[list[str], list[str], dict[str, dict]]:
    """Cada archivo de test permitido debe ejecutar > 0 tests y 0 fallidos
    corriendo su runner apuntado a ese archivo. Devuelve (errores, salidas
    completas de los fallidos, {archivo: {"executed", "failed", "runner"}})."""
    errors: list[str] = []
    full_outputs: list[str] = []
    counts: dict[str, dict] = {}
    for rel in test_files:
        if not (repo_path / rel).is_file():
            continue
        executed, failed, output = run_test_file(repo_path, rel, runner)
        counts[rel] = {"executed": executed, "failed": failed, "runner": detect_test_runner(repo_path, rel, runner)}
        if executed == 0 or failed > 0:
            errors.append(f"{rel}: {executed} test(s) ejecutados, {failed} fallido(s) (se exige > 0 y 0)")
            full_outputs.append(f"=== tests de {rel} ===\n{output}")
    return errors, full_outputs, counts


def last_lines(text: str, n: int) -> str:
    return "\n".join(text.splitlines()[-n:])


def verify_changes(
    repo_path: Path, allowed_files: list[str], branch: str, log_path: Path, profile: dict
) -> tuple[bool, str, str, dict[str, dict]]:
    """Criterio de exito de un intento (Aider o Claude), antes de comitear:
      - hay lista de archivos permitidos;
      - CADA archivo permitido existe, no esta vacio, pasa su validacion
        minima por tipo (`validate_allowed_file_content`) y difiere de su
        version en `origin/<branch>` (o es nuevo);
      - pasan los pasos de `verification` del perfil (`run_verification_steps`);
      - cada archivo de test permitido, corrido aparte con su runner, ejecuta
        > 0 tests y 0 fallidos (`verify_test_files`).
    Si ningun archivo permitido existe o cambio respecto al remoto no se
    corren los pasos (no hay nada que verificar). Si falla, el intento cuenta como
    fallido, la salida COMPLETA de los comandos fallidos queda en el log del
    intento y se devuelve junto con un resumen corto (para needs-review).
    Devuelve (ok, resumen, salida completa, tests por archivo)."""
    if not allowed_files:
        error = "el issue no tiene lista de archivos permitidos"
        append_to_log(log_path, "verificacion fallida", error)
        return False, error, error, {}

    test_files = [f for f in allowed_files if TEST_FILE_NAME_RE.match(Path(f).name)]
    errors = validate_allowed_file_content(repo_path, allowed_files)
    unchanged = unchanged_vs_remote(repo_path, branch, allowed_files)
    errors.extend(f"{rel}: identico a origin/{branch} (no se cambio nada)" for rel in unchanged)
    present = [f for f in allowed_files if (repo_path / f).is_file()]
    if len(unchanged) == len(present):
        full_output = "\n".join(errors)
        append_to_log(log_path, "verificacion fallida", full_output)
        return False, "; ".join(errors), full_output, {}

    full_outputs: list[str] = []
    step_errors, step_full = run_verification_steps(repo_path, profile, allowed_files, test_files)
    errors.extend(step_errors)
    full_outputs.extend(step_full)

    test_errors, test_full, test_counts = verify_test_files(repo_path, test_files, profile.get("test_runner", "auto"))
    errors.extend(test_errors)
    full_outputs.extend(test_full)

    if errors:
        full_output = "\n\n".join(full_outputs + ["\n".join(errors)])
        append_to_log(log_path, "verificacion fallida", full_output)
        return False, "; ".join(errors), full_output, test_counts
    return True, "", "", test_counts


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
    """Candado comun a Aider y a la escalacion a Claude: borra los artefactos
    `.aider*` (antes de revertir el `.gitignore` que Aider modifica), revierte
    rutas prohibidas y archivos fuera de la lista permitida, y borra los
    archivos permitidos que hayan quedado vacios."""
    artifacts = remove_aider_artifacts(repo_path)
    if artifacts:
        append_to_log(log_path, "candado (artefactos de aider, eliminados)", "\n".join(artifacts))
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
    branch: str,
) -> tuple[bool, str, dict[str, dict]]:
    """Escalacion unica a Claude tras agotar los intentos de Aider. Devuelve
    (resuelto, motivo si no se resolvio, tests por archivo). Aplica el mismo
    candado y la misma verificacion que con Aider."""
    tag = f"{issue['repo']}#{issue['number']}"
    claude_bin = config.get("models", {}).get("claude", "claude")
    claude_timeout = int(config.get("development", {}).get("claude_timeout", DEFAULT_CLAUDE_TIMEOUT))

    if not allowed_files:
        return False, "sin lista de archivos permitidos no se escala a Claude", {}
    if escalation_state["remaining"] <= 0:
        return False, f"se alcanzo el limite de {escalation_state['max']} escalacion(es) a Claude por noche", {}
    if shutil.which(claude_bin) is None:
        return False, f"Claude no esta disponible ('{claude_bin}' no encontrado)", {}
    remaining_seconds = (deadline - datetime.now()).total_seconds()
    if remaining_seconds < MIN_ESCALATION_SECONDS:
        return False, f"no queda tiempo dentro del limite de {MAX_HOURS_PER_ISSUE}h para escalar a Claude", {}

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
        return False, "Claude no genero cambios utilizables" + ("" if claude_ok else " (timeout o error)"), {}

    ok, error, _, test_counts = verify_changes(repo_path, allowed_files, branch, log_path, profile)
    if ok:
        return True, "", test_counts
    return False, f"la solucion de Claude tampoco paso la verificacion: {error}", test_counts


def issue_result(message: str, solver: str, committed: Optional[list[str]] = None, tests: Optional[dict] = None) -> dict:
    return {"message": message, "solver": solver, "committed": committed or [], "tests": tests or {}}


# --------------------------------------------------------------------------
# Parches deterministas de dependencias (issues del radar)
# --------------------------------------------------------------------------


def is_dependency_patch_issue(issue: dict) -> bool:
    """Issue de parche abierto por el radar: label `dependencies` + marcador oculto del radar."""
    return DEPENDENCIES_LABEL in issue.get("labels", []) and RADAR_MARKER in issue.get("body", "")


def parse_radar_patch(body: str) -> tuple[Optional[dict], str]:
    """Lee el bloque ```radar-patch``` (JSON) del issue; si no existe (issues
    creados antes del bloque) cae a las lineas legibles del cuerpo. Devuelve
    (parche, error). El parche se valida aqui porque sus campos terminan como
    argumentos de npm o en requirements.txt."""
    match = RADAR_PATCH_BLOCK_RE.search(body)
    if match:
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError as exc:
            return None, f"bloque radar-patch con JSON invalido: {exc}"
        if not isinstance(data, dict):
            return None, "bloque radar-patch no es un objeto JSON"
    else:
        package = LEGACY_PACKAGE_RE.search(body)
        current = LEGACY_CURRENT_RE.search(body)
        target = LEGACY_TARGET_RE.search(body)
        if not (package and current and target):
            return None, "el issue no tiene bloque radar-patch ni las lineas Paquete/Version del radar"
        data = {
            "package": package.group(1),
            "ecosystem": package.group(2),
            "direct": package.group(3) == "directa",
            "dev": package.group(4) == "desarrollo",
            "manifest": package.group(5),
            "from": current.group(1),
            "to": target.group(1),
            "advisories": LEGACY_ADVISORY_RE.findall(body),
        }

    ecosystem = str(data.get("ecosystem") or "")
    package = str(data.get("package") or "")
    if ecosystem not in PATCH_FILES:
        return None, f"ecosistema '{ecosystem}' sin parche determinista (solo npm y PyPI)"
    name_re = NPM_PACKAGE_NAME_RE if ecosystem == "npm" else PYPI_PACKAGE_NAME_RE
    if not name_re.match(package):
        return None, f"nombre de paquete invalido: {package!r}"
    for key in ("from", "to"):
        if not PATCH_VERSION_RE.match(str(data.get(key) or "")):
            return None, f"version '{key}' invalida: {data.get(key)!r}"
    advisories = data.get("advisories") or []
    return {
        "package": package,
        "ecosystem": ecosystem,
        "from": str(data["from"]),
        "to": str(data["to"]),
        "direct": bool(data.get("direct", True)),
        "dev": bool(data.get("dev", False)),
        "manifest": str(data.get("manifest") or ""),
        "advisories": [str(a) for a in advisories if isinstance(a, str)],
    }, ""


def describe_dependency_patch(patch: dict) -> str:
    """Plan del parche en una linea (dry-run, logs)."""
    pkg, to = patch["package"], patch["to"]
    if patch["ecosystem"] == "npm":
        if patch["direct"]:
            return f"npm install {'-D ' if patch['dev'] else ''}{pkg}@{to}"
        return f"npm update {pkg} (+ overrides {{\"{pkg}\": \"{to}\"}} y npm install si el lock sigue vulnerable)"
    return f"fijar {pkg}=={to} en requirements.txt"


def py_key(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def installed_versions(repo_path: Path, patch: dict) -> list[str]:
    """Versiones del paquete que quedan en el lock (npm, anidadas incluidas)
    o el pin de requirements.txt (PyPI)."""
    package = patch["package"]
    if patch["ecosystem"] == "npm":
        # Import tardio: batcave.radar importa este modulo.
        from batcave.radar import lock_all_packages

        lock_path = repo_path / "package-lock.json"
        try:
            lock = json.loads(lock_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("%s: no se pudo leer package-lock.json: %s", repo_path.name, exc)
            return []
        return sorted({version for name, version, _ in lock_all_packages(lock) if name == package})
    req = repo_path / "requirements.txt"
    if not req.exists():
        return []
    versions: set[str] = set()
    for line in req.read_text(encoding="utf-8", errors="replace").splitlines():
        match = REQ_PIN_LINE_RE.match(line.split("#", 1)[0])
        if match and py_key(match.group(1)) == py_key(package):
            pin = re.search(r"===?\s*([^\s,;]+)", match.group(3))
            if pin:
                versions.add(pin.group(1))
    return sorted(versions)


def osv_vulnerable_versions(patch: dict, versions: list[str]) -> tuple[Optional[list[str]], str]:
    """Re-chequeo OSV del paquete: cuales de `versions` siguen afectadas por
    los advisories del issue (por cualquiera si el issue no los lista).
    Devuelve (vulnerables, error); None si OSV no respondio (no se puede
    confirmar, cuenta como fallo)."""
    if not versions:
        return [], ""
    name = py_key(patch["package"]) if patch["ecosystem"] == "PyPI" else patch["package"]
    queries = [{"package": {"name": name, "ecosystem": patch["ecosystem"]}, "version": v} for v in versions]
    try:
        resp = requests.post(OSV_QUERYBATCH_URL, json={"queries": queries}, timeout=OSV_TIMEOUT)
        resp.raise_for_status()
        results = resp.json().get("results") or []
    except (requests.RequestException, ValueError) as exc:
        return None, f"OSV no respondio: {exc}"
    if len(results) != len(versions):
        return None, f"OSV devolvio {len(results)} resultados para {len(versions)} versiones"
    wanted = set(patch["advisories"])
    vulnerable: list[str] = []
    for version, result in zip(versions, results):
        ids = {v.get("id") for v in result.get("vulns") or []}
        hit = bool(ids & wanted) if wanted else bool(ids)
        # Con mas paginas no se ve la lista completa: se asume vulnerable.
        if hit or result.get("next_page_token"):
            vulnerable.append(version)
    return vulnerable, ""


class PatchLog:
    """Log de un parche de dependencias (`reports/deps-<repo>-<issue>.log`):
    la salida de cada comando y, al cerrar (`finish`), un resumen con cada
    comando, su resultado y el resultado final, para que `tail -n 40` baste."""

    def __init__(self, path: Path, header: str) -> None:
        self.path = path
        self.steps: list[tuple[str, bool, str, float]] = []
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"## {header}\n", encoding="utf-8")

    def note(self, title: str, body: str) -> None:
        append_to_log(self.path, title, strip_ansi(body))

    def step(self, label: str, ok: bool, detail: str = "", seconds: float = 0.0) -> None:
        self.steps.append((label, ok, detail, seconds))

    def run(self, cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
        started = datetime.now()
        proc = run_cli(cmd, cwd=cwd, timeout=DEFAULT_STEP_TIMEOUT)
        command = " ".join(cmd)
        self.note(f"{command} (codigo {proc.returncode})", (proc.stdout + proc.stderr).strip()[-6000:])
        self.step(command, proc.returncode == 0, f"codigo {proc.returncode}", (datetime.now() - started).total_seconds())
        return proc

    def finish(self, outcome: str) -> None:
        lines = [
            f"- [{'ok' if ok else 'FALLO'}] {label} ({detail + ', ' if detail else ''}{seconds:.0f}s)"
            for label, ok, detail, seconds in self.steps
        ]
        self.note("resumen: comandos y resultado", "\n".join(lines or ["- (ningun comando)"]) + f"\nresultado: {outcome}")


def package_families(config: dict) -> dict[str, dict]:
    """Familias de `development.package_families`: {nombre: {include, exclude}} (patrones fnmatch)."""
    families: dict[str, dict] = {}
    for name, spec in ((config.get("development") or {}).get("package_families") or {}).items():
        if isinstance(spec, list):
            spec = {"include": spec}
        if not isinstance(spec, dict) or not spec.get("include"):
            log.warning("Familia de paquetes '%s' invalida en config.yaml (falta include), se ignora", name)
            continue
        families[str(name)] = {
            "include": [str(p) for p in spec["include"]],
            "exclude": [str(p) for p in spec.get("exclude") or []],
        }
    return families


def in_family(family: dict, package: str) -> bool:
    return any(fnmatchcase(package, p) for p in family["include"]) and not any(
        fnmatchcase(package, p) for p in family["exclude"]
    )


def npm_registry_versions(package: str) -> tuple[Optional[list[str]], str]:
    """Versiones publicadas de `package` (metadata abreviada del registry de npm)."""
    url = f"{NPM_REGISTRY_URL}/{quote(package, safe='@')}"
    try:
        resp = requests.get(url, headers={"Accept": NPM_ABBREVIATED_ACCEPT}, timeout=NPM_REGISTRY_TIMEOUT)
        resp.raise_for_status()
        return list((resp.json().get("versions") or {}).keys()), ""
    except (requests.RequestException, ValueError, AttributeError) as exc:
        return None, f"el registry de npm no respondio para {package}: {exc}"


def plan_npm_install(config: dict, patch: dict, manifest: dict, lock: dict) -> tuple[Optional[dict], str]:
    """Plan de `npm install` para un parche de dependencia directa.

    Si el paquete cae en una familia de `development.package_families`, cada
    miembro presente en package.json sube con el: a la version objetivo si
    existe en el registry para ese miembro; si no, a su maxima estable del
    mismo major >= la actual. Los miembros que ya estan en esa version o
    mas arriba se dejan igual. `dev` sale de package.json (no del radar).
    Devuelve ({family, installs: [{package, from, to, dev}], notes}, error)."""
    # Import tardio: batcave.radar importa este modulo.
    from batcave.radar import is_stable, lock_versions, major_of, npm_version_from_spec, vgt, vmax

    prod = manifest.get("dependencies") or {}
    dev = manifest.get("devDependencies") or {}
    installed = lock_versions(lock)

    def entry(name: str, to: str) -> dict:
        current = installed.get(name) or npm_version_from_spec(str(prod.get(name) or dev.get(name) or ""))
        return {"package": name, "from": current or "?", "to": to, "dev": name in dev and name not in prod}

    package, target = patch["package"], patch["to"]
    plan = {"family": None, "installs": [entry(package, target)], "notes": []}
    families = package_families(config)
    family = next((name for name, fam in families.items() if in_family(fam, package)), None)
    if family is None:
        return plan, ""
    plan["family"] = family

    for name in sorted(n for n in {**prod, **dev} if n != package and in_family(families[family], n)):
        item = entry(name, target)
        current = None if item["from"] == "?" else item["from"]
        versions, error = npm_registry_versions(name)
        if versions is None:
            return None, f"familia {family}: {error}"
        fallback = target not in versions
        if fallback:
            same_major = [
                v for v in versions
                if current and is_stable("npm", v) and major_of("npm", v) == major_of("npm", current)
                and not vgt("npm", current, v)
            ]
            item["to"] = vmax("npm", same_major)
            if not item["to"]:
                plan["notes"].append(f"{name}: no existe {target} ni otra version del major de {item['from']}; se deja igual")
                continue
        if current and not vgt("npm", item["to"], current):
            plan["notes"].append(f"{name}: ya esta en {current} (>= {item['to']}), se deja igual")
            continue
        if fallback:
            plan["notes"].append(f"{name}: no existe {target}, se usa {item['to']} (maxima del mismo major)")
        plan["installs"].append(item)
    return plan, ""


def npm_install_commands(plan: dict) -> list[list[str]]:
    """Un `npm install` para los miembros de produccion y uno `-D` para los de desarrollo."""
    commands = []
    for dev in (False, True):
        specs = [f"{i['package']}@{i['to']}" for i in plan["installs"] if i["dev"] == dev]
        if specs:
            commands.append(["npm", "install", *(["-D"] if dev else []), *specs, *NPM_QUIET_FLAGS])
    return commands


def describe_npm_plan(plan: dict) -> list[str]:
    """Lineas del plan npm (log, dry-run): familia, comandos y chequeo de peers."""
    lines = []
    if plan["family"]:
        lines.append(
            f"familia {plan['family']}: "
            + ", ".join(f"{i['package']} {i['from']} -> {i['to']}{' (dev)' if i['dev'] else ''}" for i in plan["installs"])
        )
    lines.extend(plan["notes"])
    lines.extend(" ".join(a for a in cmd if a not in NPM_QUIET_FLAGS) for cmd in npm_install_commands(plan))
    lines.append("npm ls --depth=0 (peers invalidos/faltantes -> falla antes de los tests)")
    return lines


def github_json(repo_obj, path: str, ref: str) -> Optional[dict]:
    """JSON de `path` en `ref` via la API de GitHub (sin clonar); None si no se pudo leer."""
    try:
        content = repo_obj.get_contents(path, ref=ref)
        if content.encoding == "base64":
            raw = content.decoded_content
        else:  # > 1 MB: la API de contenidos no trae el cuerpo, se pide el blob
            raw = base64.b64decode(repo_obj.get_git_blob(content.sha).content)
        data = json.loads(raw)
    except (GithubException, ValueError, AttributeError) as exc:
        log.warning("No se pudo leer %s@%s de %s: %s", path, ref, repo_obj.full_name, exc)
        return None
    return data if isinstance(data, dict) else None


def dependency_patch_plan_lines(config: dict, issue: dict, patch: dict, branch: str) -> list[str]:
    """Plan del parche para `--dry-run`, sin clonar: lee package.json y
    package-lock.json de la rama de trabajo via la API de GitHub y consulta
    el registry de npm (solo lectura)."""
    if patch["ecosystem"] != "npm":
        return [describe_dependency_patch(patch)]
    manifest = github_json(issue["repo_obj"], "package.json", branch)
    if manifest is None:
        return [f"{describe_dependency_patch(patch)} (no se pudo leer package.json de `{branch}`)"]
    if patch["package"] not in {**(manifest.get("dependencies") or {}), **(manifest.get("devDependencies") or {})}:
        return [describe_dependency_patch(patch)]
    plan, error = plan_npm_install(config, patch, manifest, github_json(issue["repo_obj"], "package-lock.json", branch) or {})
    if plan is None:
        return [f"NO aplicable ({error}) -> needs-review"]
    return describe_npm_plan(plan)


def check_npm_peers(repo_path: Path, plog: PatchLog) -> tuple[str, str]:
    """`npm ls --depth=0` tras instalar: si reporta dependencias invalidas o
    faltantes (p. ej. un peer que quedo en otra version), falla antes de
    correr build/tests. Devuelve (error, salida)."""
    proc = plog.run(["npm", "ls", "--depth=0"], repo_path)
    output = strip_ansi(proc.stdout + proc.stderr)
    problems = [line.strip(" │├└┬─") for line in output.splitlines() if NPM_LS_PROBLEM_RE.search(line)]
    plog.step("chequeo de peers", not problems, f"{len(problems)} problema(s)" if problems else "sin invalidos ni faltantes")
    if problems:
        return "`npm ls --depth=0` reporta dependencias invalidas o faltantes: " + "; ".join(problems[:5]), output
    return "", ""


def add_npm_override(repo_path: Path, package: str, version: str) -> None:
    """Agrega `"overrides": {package: version}` a package.json conservando el orden de claves."""
    manifest_path = repo_path / "package.json"
    raw = manifest_path.read_text(encoding="utf-8")
    manifest = json.loads(raw)
    manifest.setdefault("overrides", {})[package] = version
    indent = re.match(r"\{\s*\n([ \t]+)", raw)
    manifest_path.write_text(
        json.dumps(manifest, indent=indent.group(1) if indent else 2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def apply_npm_patch(config: dict, repo_path: Path, patch: dict, plog: PatchLog) -> tuple[str, str, list[str]]:
    """Aplica el parche npm en el workspace: directa -> `plan_npm_install`
    (familia incluida); transitiva -> `npm update` y, si no alcanza,
    override. Siempre termina con `check_npm_peers`. Devuelve (error, salida
    del paso que fallo, cambios legibles `pkg from -> to`); error '' si se aplico."""
    package, target = patch["package"], patch["to"]
    changes = [f"{package} {patch['from']} -> {target}"]
    try:
        manifest = json.loads((repo_path / "package.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return f"package.json ilegible: {exc}", "", []

    if package in {**(manifest.get("dependencies") or {}), **(manifest.get("devDependencies") or {})}:
        try:
            lock = json.loads((repo_path / "package-lock.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            lock = {}
        plan, error = plan_npm_install(config, patch, manifest, lock)
        if plan is None:
            return error, "", []
        plog.note("plan npm", "\n".join(describe_npm_plan(plan)))
        changes = [f"{i['package']} {i['from']} -> {i['to']}" for i in plan["installs"]]
        for cmd in npm_install_commands(plan):
            proc = plog.run(cmd, repo_path)
            if proc.returncode != 0:
                return f"`{' '.join(cmd)}` fallo", proc.stdout + proc.stderr, changes
        error, output = check_npm_peers(repo_path, plog)
        return error, output, changes
    if patch["direct"]:
        return f"{package} figura como directa pero no esta en dependencies/devDependencies de package.json", "", []

    proc = plog.run(["npm", "update", package, *NPM_QUIET_FLAGS], repo_path)
    if proc.returncode != 0:
        return f"`npm update {package}` fallo", proc.stdout + proc.stderr, changes
    vulnerable, error = osv_vulnerable_versions(patch, installed_versions(repo_path, patch))
    if vulnerable is None:
        return error, "", changes
    if vulnerable:
        plog.note("npm update no alcanzo", f"siguen vulnerables: {', '.join(vulnerable)}; se agrega override")
        try:
            add_npm_override(repo_path, package, target)
        except (OSError, json.JSONDecodeError) as exc:
            return f"no se pudo escribir el override en package.json: {exc}", "", changes
        plog.step(f"overrides {{{package}: {target}}} en package.json", True)
        proc = plog.run(["npm", "install", *NPM_QUIET_FLAGS], repo_path)
        if proc.returncode != 0:
            return f"`npm install` con overrides {{{package}: {target}}} fallo", proc.stdout + proc.stderr, changes
    error, output = check_npm_peers(repo_path, plog)
    return error, output, changes


def apply_pypi_patch(config: dict, repo_path: Path, patch: dict, plog: PatchLog) -> tuple[str, str, list[str]]:
    """Fija `paquete==to` en requirements.txt (conserva extras, marcadores y
    comentario de la linea; si no estaba, lo agrega). Misma firma y retorno
    que `apply_npm_patch`."""
    error = pin_requirement(repo_path, patch, plog)
    plog.step(f"fijar {patch['package']}=={patch['to']} en requirements.txt", not error, error)
    return error, "", [] if error else [f"{patch['package']} {patch['from']} -> {patch['to']}"]


def pin_requirement(repo_path: Path, patch: dict, plog: PatchLog) -> str:
    if patch["manifest"] not in ("", "requirements.txt", "requirements.in"):
        return f"el paquete viene de {patch['manifest']}: solo se parchea requirements.txt de forma determinista"
    req = repo_path / "requirements.txt"
    if not req.exists():
        return "el repo no tiene requirements.txt"
    package, target = patch["package"], patch["to"]
    lines = req.read_text(encoding="utf-8").splitlines()
    replaced = False
    for idx, line in enumerate(lines):
        if line.lstrip().startswith(("#", "-")):
            continue
        match = REQ_PIN_LINE_RE.match(line)
        if not match or py_key(match.group(1)) != py_key(package):
            continue
        rest = match.group(4).strip()
        if rest.startswith("@"):
            return f"{package} se instala desde una URL en requirements.txt; no se fija version"
        lines[idx] = f"{match.group(1)}{match.group(2) or ''}=={target}" + (f" {rest}" if rest else "")
        replaced = True
    if not replaced:
        lines.append(f"{package}=={target}")
    req.write_text("\n".join(lines) + "\n", encoding="utf-8")
    plog.note("requirements.txt", f"{'actualizado' if replaced else 'agregado'}: {package}=={target}")
    return ""


def process_dependency_patch(config: dict, issue: dict, profile: dict, repo_path: Path, branch: str) -> dict:
    """Issue `dependencies` del radar: parche mecanico, sin Aider ni Claude
    (no consume escalaciones). Aplica el comando, corre la verificacion del
    especialista, re-chequea OSV, stagea solo los archivos del manifiesto y
    comitea `fix(deps): ...` en la rama de trabajo.

    Feedback en el issue: exito -> comentario `Aplicado en <rama>: <sha>,
    verificado` + label `in-dev` (las corridas siguientes lo omiten); OSV ya
    no lo ve vulnerable -> comentario y se cierra como completado; fallo ->
    `needs-review` con las ultimas `FAILURE_COMMENT_LINES` lineas del paso
    que fallo. El log termina con un resumen de comandos y resultado."""
    tag = f"{issue['repo']}#{issue['number']}"
    repo_short_name = issue["repo"].split("/")[-1]
    issue_obj = issue["issue_obj"]
    plog = PatchLog(REPORTS_DIR / f"deps-{repo_short_name}-{issue['number']}.log", f"{tag}: {issue['title']}")

    def fail(reason: str, output: str = "") -> dict:
        discard_changes(repo_path)
        detail = reason
        tail = last_lines(strip_ansi(mask_secrets(output)).strip(), FAILURE_COMMENT_LINES)
        if tail:
            detail += (
                f"\n\nUltimas {FAILURE_COMMENT_LINES} lineas del paso que fallo:\n"
                f"```\n{tail.replace('```', chr(39) * 3)}\n```"
            )
        mark_needs_review(issue["repo_obj"], issue_obj, f"parche determinista de dependencias: {detail}", profile)
        plog.finish(f"FALLO -> needs-review: {reason}")
        msg = f"🚫 {tag}: parche determinista fallo ({reason}); marcado 'needs-review'"
        log.error(msg)
        return issue_result(msg, SOLVER_NONE)

    patch, error = parse_radar_patch(issue["body"])
    if patch is None:
        return fail(error)
    package, target = patch["package"], patch["to"]
    files = PATCH_FILES[patch["ecosystem"]]
    plog.note("parche", f"{json.dumps(patch, ensure_ascii=False)}\nplan base: {describe_dependency_patch(patch)}")
    log.info("%s: parche determinista %s", tag, describe_dependency_patch(patch))

    current_versions = installed_versions(repo_path, patch)
    before, _ = osv_vulnerable_versions(patch, current_versions)
    if current_versions and before == []:
        installed = ", ".join(current_versions)
        plog.step("OSV antes de aplicar", True, f"{package} {installed} ya sin los advisories del issue")
        try:
            issue_obj.create_comment(
                f"OSV ya no ve `{package}` vulnerable en `{branch}` (instalado: {installed}); "
                f"nada que aplicar, se cierra como completado."
            )
            issue_obj.edit(state="closed", state_reason="completed")
            outcome = "ya corregido; issue cerrado como completado"
        except GithubException as exc:
            log.error("No se pudo cerrar el issue %s: %s", tag, exc)
            outcome = f"ya corregido, pero no se pudo cerrar el issue: {exc}"
        plog.finish(outcome)
        msg = f"ℹ️ {tag}: OSV ya no ve {package} vulnerable en `{branch}`; {outcome}"
        log.info(msg)
        return issue_result(msg, SOLVER_NONE)

    apply = apply_npm_patch if patch["ecosystem"] == "npm" else apply_pypi_patch
    error, output, changes = apply(config, repo_path, patch, plog)
    if error:
        return fail(error, output)

    apply_guard(repo_path, files, plog.path, tag, SOLVER_DETERMINISTIC)
    if not changed_files(repo_path):
        return fail("el comando no cambio ningun archivo del manifiesto")

    step_errors, step_full = run_verification_steps(repo_path, profile, files, [], plog.steps)
    if step_errors:
        plog.note("verificacion fallida", "\n\n".join(step_full + step_errors))
        return fail("; ".join(step_errors), step_full[0] if step_full else "")

    remaining = installed_versions(repo_path, patch)
    vulnerable, error = osv_vulnerable_versions(patch, remaining)
    if vulnerable is None:
        return fail(f"no se pudo confirmar con OSV: {error}")
    plog.step("re-chequeo OSV", not vulnerable, f"{package} instalado: {', '.join(remaining) or '-'}")
    if vulnerable:
        return fail(f"el lock sigue con {package} vulnerable: {', '.join(vulnerable)}")

    message = f"fix(deps): {package} {patch['from']} -> {target} (radar)\n\n"
    if len(changes) > 1:
        message += "Paquetes actualizados juntos:\n" + "\n".join(f"- {c}" for c in changes) + "\n\n"
    message += f"Resuelve #{issue['number']}\nResuelto por: {SOLVER_DETERMINISTIC}"
    committed, commit_error, pushed = commit_allowed_and_push(repo_path, branch, message, files)
    if not committed:
        return fail(commit_error)
    sha = run_cli(["git", "rev-parse", "HEAD"], cwd=repo_path).stdout.strip()
    plog.step(f"commit {sha[:12]} + push a {branch}", pushed, "push ok" if pushed else "push fallo")
    if not pushed:
        plog.finish(f"commit local {sha} sin push; el issue queda igual")
        msg = f"⚠️ {tag}: commit local `fix(deps)` en `{branch}` ({', '.join(committed)}) pero el push fallo; no cuenta como resuelto"
        log.error(msg)
        return issue_result(msg, SOLVER_NONE, committed)

    try:
        issue_obj.create_comment(
            f"Aplicado en `{branch}`: {sha}, verificado\n\n" + "\n".join(f"- {c}" for c in changes)
        )
        issue_obj.add_to_labels(IN_DEV_LABEL)
        if "needs-review" in issue.get("labels", []):
            issue_obj.remove_from_labels("needs-review")
        outcome = f"OK: {sha} en {branch}, verificado; label '{IN_DEV_LABEL}'"
    except GithubException as exc:
        log.error("No se pudo comentar/etiquetar el issue %s: %s", tag, exc)
        outcome = f"OK: {sha} en {branch}, verificado; pero fallo el feedback en el issue: {exc}"
    plog.finish(outcome)
    msg = f"✅ {tag}: {', '.join(changes)} en `{branch}` ({', '.join(committed)}): {issue['url']}"
    log.info(msg)
    return issue_result(msg, SOLVER_DETERMINISTIC, committed)


def process_issue(config: dict, issue: dict, profile: dict, escalation_state: dict, branch: str) -> dict:
    """Resuelve un issue con el especialista `profile` en la rama de trabajo
    del repo (`resolve_work_branch`). Devuelve `issue_result`: mensaje,
    resolutor (`SOLVER_AIDER`/`SOLVER_CLAUDE`/`SOLVER_NONE`), archivos
    exactos del commit y tests por archivo de test (para la
    autoverificacion del reporte)."""
    tag = f"{issue['repo']}#{issue['number']}"
    repo_short_name = issue["repo"].split("/")[-1]
    aider_model = config["models"]["aider"]
    workspace_dir = get_workspace_dir(config)
    aider_timeout = config.get("development", {}).get("aider_timeout", DEFAULT_AIDER_TIMEOUT)
    aider_attempts = config.get("development", {}).get("aider_attempts", DEFAULT_AIDER_ATTEMPTS)

    repo_path = setup_workspace_repo(issue["repo"], workspace_dir, branch)
    if repo_path is None:
        msg = f"❌ {tag}: no se pudo preparar el workspace aislado para {issue['repo']}"
        log.error(msg)
        return issue_result(msg, SOLVER_NONE)

    if not repo_is_clean(repo_path):
        msg = f"⚠️ {tag}: el workspace tiene cambios sin commitear, se omite para no pisarlos"
        log.warning(msg)
        return issue_result(msg, SOLVER_NONE)

    if is_dependency_patch_issue(issue):
        return process_dependency_patch(config, issue, profile, repo_path, branch)

    body = relativize_paths_in_body(issue["body"], config, issue["repo"])
    allowed_files = [normalize_rel_path(f) for f in extract_allowed_files(body)]
    specialist_context = build_specialist_context(config, profile, f"{issue['title']}\n{body}")

    if not allowed_files:
        reason = "el issue no declara archivos (seccion `Archivos:` o rutas mencionadas); sin lista permitida no se puede comitear"
        mark_needs_review(issue["repo_obj"], issue["issue_obj"], reason, profile)
        msg = f"🚫 {tag}: {reason}; marcado 'needs-review'"
        log.error(msg)
        return issue_result(msg, SOLVER_NONE)

    def commit_message(solver: str) -> str:
        return f"feat({profile['name']}): resuelve #{issue['number']} - {issue['title']}\n\nResuelto por: {solver}"

    deadline = datetime.now() + timedelta(hours=MAX_HOURS_PER_ISSUE)
    committed: list[str] = []
    pushed = False
    test_counts: dict[str, dict] = {}
    previous_error: Optional[str] = None
    last_full_output = ""
    attempts = 0
    while attempts < aider_attempts:
        if datetime.now() >= deadline:
            msg = f"⏱️ {tag}: se alcanzo el limite de {MAX_HOURS_PER_ISSUE}h, se pasa al siguiente issue"
            log.warning(msg)
            return issue_result(msg, SOLVER_NONE, tests=test_counts)

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
        # error, para borrar los .aider*, revertir rutas prohibidas/archivos
        # fuera de la lista permitida y borrar los permitidos que hayan
        # quedado vacios.
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

        ok, error, full_output, test_counts = verify_changes(
            repo_path, allowed_files, branch, attempt_log_path, profile
        )
        if ok:
            committed, commit_error, pushed = commit_allowed_and_push(
                repo_path, branch, commit_message(SOLVER_AIDER), allowed_files
            )
            if committed:
                break
            error = full_output = commit_error
            append_to_log(attempt_log_path, "commit abortado", commit_error)
        log.warning("%s: intento %d/%d fallo: %s", tag, attempts, aider_attempts, error)
        # No se descartan los cambios: el siguiente intento parte del
        # archivo ya generado para que aider lo corrija, en vez de
        # arrancar de cero.
        previous_error = last_lines(full_output, VERIFICATION_FEEDBACK_LINES)
        last_full_output = full_output

    solver = SOLVER_AIDER
    if not committed:
        solved, reason, test_counts = escalate_to_claude(
            config, issue, profile, repo_path, body, specialist_context,
            allowed_files, last_full_output, deadline, escalation_state, branch,
        )
        if solved:
            committed, commit_error, pushed = commit_allowed_and_push(
                repo_path, branch, commit_message(SOLVER_CLAUDE), allowed_files
            )
            if not committed:
                reason = f"la solucion de Claude paso la verificacion pero {commit_error}"
        if not committed:
            mark_needs_review(
                issue["repo_obj"],
                issue["issue_obj"],
                f"Aider ({aider_model}) no genero una solucion utilizable tras {aider_attempts} intentos; {reason}.",
                profile,
            )
            msg = f"🚫 {tag}: aider fallo {aider_attempts} veces y {reason}; marcado 'needs-review'"
            log.error(msg)
            return issue_result(msg, SOLVER_NONE, tests=test_counts)
        solver = SOLVER_CLAUDE

    if not pushed:
        msg = (
            f"⚠️ {tag}: {solver} genero un commit local en `{branch}` ({', '.join(committed)}) "
            f"pero el push fallo; no cuenta como resuelto"
        )
        log.error(msg)
        return issue_result(msg, SOLVER_NONE, committed, test_counts)

    msg = (
        f"✅ {tag}: {len(committed)} archivo(s) actualizados en `{branch}` "
        f"({', '.join(committed)}): {issue['url']}"
    )
    log.info(msg)
    return issue_result(msg, solver, committed, test_counts)


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
        lines.extend(
            f"- Resueltos por {s}: {solvers.get(s, 0)}" for s in (SOLVER_DETERMINISTIC, SOLVER_AIDER, SOLVER_CLAUDE)
        )
        lines.append(f"- Sin resolver ({SOLVER_NONE}): {solvers.get(SOLVER_NONE, 0)}")
        if escalation_state:
            lines.append(f"- Escalaciones a Claude usadas: {escalation_state['used']}/{escalation_state['max']}")
        lines.extend(["", "## Detalle", "| Issue | Especialista | Resuelto por | Resultado |", "|---|---|---|---|"])
        lines.extend(
            f"| {r['tag']} | {r['specialist']} | {r['solver']} | {memory_store.table_cell(r['message'])} |"
            for r in results
        )
        lines.extend(["", "## Autoverificacion"])
        for r in results:
            committed = r.get("committed") or []
            lines.append(f"### {r['tag']} ({r['solver']})")
            lines.append(
                "- Archivos comiteados: " + (", ".join(f"`{f}`" for f in committed) if committed else "ninguno")
            )
            if any(Path(f).name.startswith(AIDER_ARTIFACT_PREFIX) for f in committed):
                lines.append("- ❌ El commit contiene artefactos de Aider")
            tests = r.get("tests") or {}
            if tests:
                lines.extend(
                    f"- Tests `{f}` ({c.get('runner', '?')}): {c['executed']} ejecutado(s), {c['failed']} fallido(s)"
                    + ("" if c["executed"] > 0 and c["failed"] == 0 else " ❌")
                    for f, c in tests.items()
                )
            else:
                lines.append("- Tests por archivo: sin archivos de test en la lista permitida")
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
        branch = resolve_work_branch(config, issue["repo"], specialists, profile)
        log.info(
            "%s trabajando en issue #%d (%d/%d): %s (%s, rama %s)",
            specialist_display(profile), issue["number"], idx, len(issues), tag, reason, branch,
        )
        try:
            outcome = process_issue(config, issue, profile, escalation_state, branch)
        except Exception as exc:  # noqa: BLE001 - un issue no debe tumbar el resto de la corrida
            log.exception("Fallo inesperado procesando %s", tag)
            outcome = issue_result(f"❌ {tag}: error inesperado: {exc}", SOLVER_NONE)
        message, solver = outcome["message"], outcome["solver"]
        record_specialist_memory(profile, issue, message, solver, branch)
        worked.add(name)
        results.append(
            {
                "tag": tag,
                "specialist": profile.get("display_name") or name,
                "solver": solver,
                "message": message,
                "line": format_result_line(message, profile, solver),
                "committed": outcome["committed"],
                "tests": outcome["tests"],
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
