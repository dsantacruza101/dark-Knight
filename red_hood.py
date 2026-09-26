#!/usr/bin/env python3
"""Red Hood (Jason Todd): QA brutal de la Batifamilia.

Corre una vez al dia como servicio systemd (red-hood.service +
red-hood.timer, dispara a las 02:00 America/El_Salvador) y audita la
calidad de los repos de `config.yaml` (`red_hood.local_repos`): tests,
cobertura, lint, vulnerabilidades de dependencias y secretos hardcodeados.
No tiene filtro: reporta todo lo que encuentra, sin piedad.

Workspace aislado (`red_hood.workspace_dir`, por defecto
`~/projects/.red-hood-workspace`): Red Hood NUNCA hace checkout, pull,
`npm ci` ni ninguna escritura sobre los directorios de produccion
(`red_hood.local_repos`). Todo el trabajo destructivo ocurre sobre una
copia clonada en `workspace_dir/<nombre-repo>`:

  - Si la copia no existe: `git clone` desde la URL que devuelve
    `git -C <repo_prod> remote get-url origin`.
  - Si ya existe: `git fetch origin`, `checkout dev` (o `main` en modo
    solo-lectura si no hay `dev`), `git reset --hard origin/<rama>`.
  - `npm ci`, tests, lint, auditoria, Aider, commits y push ocurren solo
    en esa copia; el directorio de produccion nunca se modifica.

Por repo (fuera de `--dry-run`):

  1. Reconocimiento: prepara el workspace aislado (ver arriba). Compara el
     HEAD del workspace contra el ultimo hash auditado
     (`batcave/memory/red_hood_state.json`); sin commits nuevos, se omite
     el repo.
  2. Tests: detecta el stack por archivos (`package.json` -> Node,
     `requirements.txt`/`*.py` -> Python) y corre la suite siempre con
     `CI=true` y sin watch mode (`npx vitest run --coverage` si el repo usa
     Vitest, `npm test -- --passWithNoTests --coverage --watchAll=false`
     si usa Jest; `pytest` si existe `tests/`), con timeout de 10 minutos.
     Un timeout no cuenta como test fallido: se clasifica aparte como
     categoria `test_timeout` (severidad base `low`).
  3. Analisis estatico: lint (`npm run lint --if-present`), auditoria de
     dependencias (`npm audit --json` / `pip-audit`), `py_compile` en
     Python, y un escaneo de secretos hardcodeados (regex) que dispara una
     alerta critica inmediata por Telegram si encuentra algo.
  4. Cada hallazgo se evalua con TypeSafe (Jev): `severity` (Score),
     `category` (Choice), `should_generate_tests` (Noul) y `who_to_notify`
     (Choice: alfred/nightwing/batman/none). Sin `TYPESAFE_API_KEY`, cae a
     un fallback deterministico (igual que Lucius Fox y Signal).
  5. Si `should_generate_tests` > 0.7 y la cobertura esta por debajo del
     umbral (`red_hood.coverage_threshold`), genera tests para hasta
     `red_hood.max_tests_per_repo` archivos usando Aider (backend Ollama),
     sobre el workspace aislado. Solo se tocan archivos de test
     (`*.spec.ts`, `*.test.ts`, `test_*.py`); si Aider toca algo mas, o el
     test generado falla al correrlo, se descarta con `git checkout` y se
     registra en memoria. Si pasa, se comitea y se pushea a `dev` (contra
     el remoto de GitHub, no contra el directorio de produccion).
  6. Reporta: issues en GitHub (labels `red-hood` + `bug`, buscando
     duplicados primero; `night-agent` si Nightwing podria resolverlo),
     mensajes en `batcave/comms.json` (`type: qa_finding`) y notificaciones
     por Telegram via Alfred. Reporte consolidado en
     `reports/red-hood-YYYY-MM-DD.md`.

Restricciones duras (no configurables):
  - Nunca comitea ni pushea a main/master.
  - Nunca toca `.env`, docker-compose, ni configuracion de
    nginx/fail2ban/cloudflared/ssh (ver `modes.development.is_forbidden_path`).
  - Nunca modifica codigo fuente, solo archivos de test.
  - Los secretos encontrados nunca se muestran completos: solo los
    primeros 6 caracteres + `***`.
  - Nunca escribe sobre los directorios de produccion listados en
    `red_hood.local_repos`.

Con `--dry-run` la auditoria es 100% de solo lectura sobre los directorios
de produccion: no clona, no hace checkout, no instala nada, no comitea, no
crea issues y no envia Telegram. Solo corre `git log`, lee
`package.json`/`requirements.txt`, hace `git grep` de secretos y
`npm audit --json` (no modifica nada); no corre tests ni lint porque
requerirían instalar dependencias. Cada paso se imprime/loguea con el
prefijo `[dry-run]`.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import html
import json
import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv
from github import Github, GithubException
from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import TelegramError
from typesafe_sdk import AsyncTypeSafeClient, Choice, Noul, Score

from batcave import memory_store
from batcave.secrets import mask_secrets
from modes.development import (
    AIDER_BIN,
    OLLAMA_API_BASE,
    changed_files,
    commit_and_push,
    git_cmd,
    is_forbidden_path,
    read_repo_claude_md,
    repo_is_clean,
    run_cli,
)

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = Path("/etc/night-agent.env")
CONFIG_PATH = BASE_DIR / "config.yaml"
LOG_PATH = BASE_DIR / "red_hood.log"
COMMS_PATH = BASE_DIR / "batcave" / "comms.json"
REPORTS_DIR = BASE_DIR / "reports"
STATE_PATH = BASE_DIR / "batcave" / "memory" / "red_hood_state.json"

TEST_TIMEOUT_SECONDS = 600  # 10 minutos maximo por repo (FASE 2)
CLONE_TIMEOUT = 300
NPM_CI_TIMEOUT = 300
LINT_TIMEOUT = 180
NPM_AUDIT_TIMEOUT = 120
PIP_AUDIT_TIMEOUT = 180
PY_COMPILE_TIMEOUT = 30
SINGLE_TEST_TIMEOUT = 180
AIDER_TEST_TIMEOUT = 600

DEFAULT_COVERAGE_THRESHOLD = 60
DEFAULT_MAX_TESTS_PER_REPO = 3
DEFAULT_WORKSPACE_DIR = "~/projects/.red-hood-workspace"

SEVERITY_LEVELS = ["info", "low", "medium", "high", "critical"]
SHOULD_GENERATE_THRESHOLD = 0.7
FINDING_REPEAT_THRESHOLD = 3

# Secretos hardcodeados: sk-ant-* (Claude), ghp_* (GitHub PAT), apikey_*
# (generico), bot<digitos>:AA* (token de bot de Telegram) y passwords en
# texto plano. Encontrar cualquiera de estos es CRITICO inmediato (FASE 3).
SECRET_PATTERNS: dict[str, re.Pattern] = {
    "anthropic_api_key": re.compile(r"sk-ant-[A-Za-z0-9_-]{10,}"),
    "github_token": re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    "generic_api_key": re.compile(r"apikey_[A-Za-z0-9]{10,}", re.IGNORECASE),
    "telegram_bot_token": re.compile(r"bot\d+:AA[A-Za-z0-9_-]{20,}"),
    "hardcoded_password": re.compile(r"password\s*[=:]\s*[\"'][^\"'\s]{4,}[\"']", re.IGNORECASE),
}

COVERAGE_SUMMARY_RE = re.compile(r"All files\s*\|\s*([\d.]+)")
COVERAGE_FILE_ROW_RE = re.compile(r"^\s*([\w][\w./\-]*\.\w+)\s*\|\s*([\d.]+)")

CATEGORY_CRITERIA = {
    "failing_test": "Tests existentes que estan fallando actualmente en el repositorio.",
    "missing_tests": "El repositorio no tiene tests configurados en absoluto.",
    "vulnerability": "Vulnerabilidad de dependencias reportada por npm audit o pip-audit.",
    "hardcoded_secret": "Secreto, token o password hardcodeado encontrado en el codigo fuente.",
    "lint_error": "Errores de lint, estilo o sintaxis detectados por herramientas estaticas.",
    "low_coverage": "Cobertura de tests por debajo del umbral aceptable del proyecto.",
    "test_timeout": "La suite de tests excedio el tiempo limite de ejecucion sin fallar explicitamente.",
}
WHO_TO_NOTIFY_CRITERIA = {
    "alfred": "Alfred debe enterarse de inmediato: hallazgo critico o de alto impacto.",
    "nightwing": "Nightwing (modo Desarrollo con Aider) podria resolver esto en un proximo ciclo.",
    "batman": "Batman deberia revisarlo con mas profundidad en su ronda nocturna.",
    "none": "Basta con dejarlo en el reporte, no amerita interrumpir a nadie.",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.FileHandler(LOG_PATH), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("red_hood")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpx2").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


@dataclass(frozen=True)
class Finding:
    category: str
    summary: str
    detail: str
    baseline_severity: str
    repo: str


def load_env() -> None:
    if "TELEGRAM_BOT_TOKEN" in os.environ:
        return
    if ENV_PATH.exists():
        load_dotenv(ENV_PATH)
    else:
        log.warning("No se encontro %s, se usan las variables de entorno actuales", ENV_PATH)


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
                chat_id=self.chat_id, text=text, parse_mode=ParseMode.HTML
            )
        except TelegramError as exc:
            log.error("No se pudo enviar mensaje a Telegram: %s", exc)


class NullNotifier:
    """Notifier de --dry-run: solo loguea, nunca llama a Telegram."""

    async def send(self, text: str) -> None:
        log.info("[dry-run] Telegram: %s", text)


def _run(cmd: list[str], timeout: int = 30, cwd: Optional[Path] = None) -> str:
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return proc.stdout.strip() or proc.stderr.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("Fallo ejecutando %s: %s", mask_secrets(" ".join(cmd)), exc)
        return ""


def _run_with_timeout(
    cmd: list[str], cwd: Path, timeout: int, env: Optional[dict] = None
) -> tuple[subprocess.CompletedProcess, bool]:
    """Como run_cli, pero distingue explicitamente un timeout de otros fallos.

    Necesario para clasificar timeouts como categoria propia (`test_timeout`,
    severidad baja) en vez de como `failing_test` (FASE 2).
    """
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)
        return proc, False
    except subprocess.TimeoutExpired as exc:
        log.warning("Timeout ejecutando %s (cwd=%s)", " ".join(cmd), cwd)
        stdout = exc.stdout.decode(errors="ignore") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode(errors="ignore") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return subprocess.CompletedProcess(cmd, returncode=124, stdout=stdout, stderr=stderr), True
    except OSError as exc:
        log.warning("Fallo ejecutando %s: %s", " ".join(cmd), exc)
        return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr=str(exc)), False


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
    """Actualiza batcave/memory/red_hood.md segun el resultado de la auditoria."""
    try:
        if event == "audit_complete":
            memory_store.touch_last_activity("red_hood")
            memory_store.bump_stat("red_hood", "ciclos")
        elif event == "finding":
            memory_store.append_error(
                "red_hood",
                error=kwargs["summary"],
                causa=kwargs["category"],
                solucion="reportado (issue/telegram/comms segun severidad)",
                resultado=f"severidad {kwargs['severity']}",
            )
            if kwargs.get("escalated_to_alfred"):
                memory_store.bump_stat("red_hood", "escalaciones")
        elif event == "test_generated_success":
            memory_store.append_success(
                "red_hood",
                problema=f"Sin tests o cobertura baja en {kwargs['file']}",
                como_se_resolvio=f"Tests generados con Aider ({kwargs['framework']}) y commiteados en dev",
            )
            memory_store.bump_stat("red_hood", "reparaciones")
        elif event == "test_generated_failed":
            memory_store.append_error(
                "red_hood",
                error=f"Test generado para {kwargs['file']} no paso",
                causa="Aider genero un test que falla (o toco archivos fuera de lo permitido)",
                solucion="descartado con git checkout",
                resultado="revertido",
            )
    except OSError as exc:
        log.warning("No se pudo actualizar la memoria de Red Hood: %s", exc)


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def track_finding_repetition(state_entry: dict, category: str) -> bool:
    """True la primera vez que una categoria de hallazgo llega a 3+ repeticiones."""
    counts = state_entry.setdefault("finding_counts", {})
    learned = state_entry.setdefault("learned_categories", [])
    counts[category] = counts.get(category, 0) + 1
    if counts[category] >= FINDING_REPEAT_THRESHOLD and category not in learned:
        learned.append(category)
        return True
    return False


def display_repo_name(repo_path: Path) -> str:
    if repo_path.parent.name == "portfolioServiceLauncher":
        return f"{repo_path.parent.name}/{repo_path.name}"
    return repo_path.name


GITHUB_REMOTE_RE = re.compile(r"github\.com[:/](?P<full_name>[\w.\-]+/[\w.\-]+?)(?:\.git)?$")


def get_github_repo_full_name(repo_path: Path) -> Optional[str]:
    url = _run(["git", "config", "--get", "remote.origin.url"], cwd=repo_path)
    match = GITHUB_REMOTE_RE.search(url)
    return match.group("full_name") if match else None


def get_github_client() -> Optional[Github]:
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        log.warning("GITHUB_TOKEN no esta definido, se omite la creacion de issues en GitHub")
        return None
    return Github(token)


def get_workspace_dir(config: dict) -> Path:
    return Path(config.get("red_hood", {}).get("workspace_dir", DEFAULT_WORKSPACE_DIR)).expanduser()


def get_origin_url(prod_repo_path: Path) -> Optional[str]:
    url = _run(["git", "-C", str(prod_repo_path), "remote", "get-url", "origin"], timeout=30)
    return url or None


def setup_workspace_repo(prod_repo_path: Path, workspace_dir: Path) -> tuple[Optional[Path], str, bool]:
    """Prepara una copia aislada de prod_repo_path en workspace_dir.

    Red Hood NUNCA hace checkout/pull/npm ci sobre prod_repo_path (regla
    dura): todo el trabajo ocurre sobre `workspace_dir/<nombre-repo>`. Si la
    copia no existe se clona desde el 'origin' del repo de produccion; si ya
    existe se hace `fetch` + `reset --hard` para dejarla identica al remoto.

    Se posiciona en 'dev' si existe; si no, audita 'main' en modo
    solo-lectura (nunca crea una rama 'dev' nueva, nunca comitea en main).

    Devuelve (workspace_repo_path, branch, read_only). workspace_repo_path
    es None si no se pudo preparar el workspace (repo se omite).
    """
    workspace_repo_path = workspace_dir / prod_repo_path.name

    if not workspace_repo_path.exists():
        origin_url = get_origin_url(prod_repo_path)
        if not origin_url:
            log.error("No se pudo obtener 'origin' de %s, se omite el workspace aislado", prod_repo_path)
            return None, "main", True
        workspace_dir.mkdir(parents=True, exist_ok=True)
        log.info("Clonando %s -> %s (workspace aislado)", origin_url, workspace_repo_path)
        clone = run_cli(git_cmd(["clone", origin_url, str(workspace_repo_path)]), timeout=CLONE_TIMEOUT)
        if clone.returncode != 0:
            log.error("Fallo el clone de %s: %s", origin_url, mask_secrets(clone.stderr[-500:]))
            return None, "main", True
    else:
        run_cli(git_cmd(["fetch", "origin"]), cwd=workspace_repo_path)

    if run_cli(["git", "checkout", "dev"], cwd=workspace_repo_path).returncode == 0:
        run_cli(["git", "reset", "--hard", "origin/dev"], cwd=workspace_repo_path)
        return workspace_repo_path, "dev", False
    if run_cli(["git", "checkout", "-b", "dev", "origin/dev"], cwd=workspace_repo_path).returncode == 0:
        run_cli(["git", "reset", "--hard", "origin/dev"], cwd=workspace_repo_path)
        return workspace_repo_path, "dev", False

    run_cli(["git", "checkout", "main"], cwd=workspace_repo_path)
    run_cli(["git", "reset", "--hard", "origin/main"], cwd=workspace_repo_path)
    return workspace_repo_path, "main", True


def detect_stack(repo_path: Path) -> str:
    if (repo_path / "package.json").exists():
        return "node"
    if (repo_path / "requirements.txt").exists() or any(repo_path.glob("*.py")):
        return "python"
    return "unknown"


# --- Node: tests, lint, auditoria ------------------------------------------------


def read_package_json(repo_path: Path) -> dict:
    try:
        return json.loads((repo_path / "package.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("No se pudo leer package.json en %s: %s", repo_path, exc)
        return {}


def sha256_file(path: Path) -> Optional[str]:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def npm_ci_if_needed(repo_path: Path, state_entry: dict) -> None:
    lockfile = repo_path / "package-lock.json"
    lockfile_hash = sha256_file(lockfile) if lockfile.exists() else None
    node_modules_missing = not (repo_path / "node_modules").exists()
    if node_modules_missing or lockfile_hash != state_entry.get("lockfile_hash"):
        log.info("%s: instalando dependencias (npm ci)", repo_path.name)
        run_cli(["npm", "ci"], cwd=repo_path, timeout=NPM_CI_TIMEOUT)
    state_entry["lockfile_hash"] = lockfile_hash


def parse_node_test_counts(output: str) -> tuple[int, int]:
    match = re.search(r"Tests:\s*(?:(\d+) failed,\s*)?(?:(\d+) skipped,\s*)?(\d+) passed", output)
    if match:
        return int(match.group(3) or 0), int(match.group(1) or 0)
    match = re.search(r"Tests\s+(\d+) failed\s*\|\s*(\d+) passed", output)
    if match:
        return int(match.group(2)), int(match.group(1))
    match = re.search(r"Tests\s+(\d+) passed", output)
    if match:
        return int(match.group(1)), 0
    return 0, 0


def run_node_tests(repo_path: Path) -> dict:
    pkg = read_package_json(repo_path)
    if "test" not in pkg.get("scripts", {}):
        return {"status": "sin_tests", "passed": 0, "failed": 0, "coverage_pct": None, "output": ""}

    # CI=true siempre: evita que jest/vitest queden esperando input interactivo (FASE 2).
    env = {**os.environ, "CI": "true"}
    if node_test_runner(repo_path) == "vitest":
        cmd = ["npx", "vitest", "run", "--coverage"]
    else:
        cmd = ["npm", "test", "--", "--passWithNoTests", "--coverage", "--watchAll=false"]

    proc, timed_out = _run_with_timeout(cmd, cwd=repo_path, timeout=TEST_TIMEOUT_SECONDS, env=env)
    output = (proc.stdout + "\n" + proc.stderr)[-20000:]
    if timed_out:
        return {"status": "timeout", "passed": 0, "failed": 0, "coverage_pct": None, "output": output}
    passed, failed = parse_node_test_counts(output)
    coverage_match = COVERAGE_SUMMARY_RE.search(output)
    coverage_pct = float(coverage_match.group(1)) if coverage_match else None
    status = "ok" if proc.returncode == 0 else "failing"
    return {"status": status, "passed": passed, "failed": failed, "coverage_pct": coverage_pct, "output": output}


def find_low_coverage_files(output: str, limit: int) -> list[str]:
    rows: list[tuple[str, float]] = []
    for line in output.splitlines():
        if "All files" in line or set(line.strip()) <= {"-", "|", " "}:
            continue
        match = COVERAGE_FILE_ROW_RE.match(line)
        if match:
            rows.append((match.group(1), float(match.group(2))))
    rows.sort(key=lambda row: row[1])

    seen: set[str] = set()
    result: list[str] = []
    for path, _pct in rows:
        if path not in seen:
            seen.add(path)
            result.append(path)
        if len(result) >= limit:
            break
    return result


def node_test_runner(repo_path: Path) -> str:
    pkg = read_package_json(repo_path)
    deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
    return "vitest" if "vitest" in deps else "jest"


def run_node_lint(repo_path: Path) -> str:
    proc = run_cli(["npm", "run", "lint", "--if-present"], cwd=repo_path, timeout=LINT_TIMEOUT)
    return (proc.stdout + proc.stderr).strip()


def run_node_audit(repo_path: Path) -> dict:
    proc = run_cli(["npm", "audit", "--json"], cwd=repo_path, timeout=NPM_AUDIT_TIMEOUT)
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return {}
    return data.get("metadata", {}).get("vulnerabilities", {})


# --- Python: tests, py_compile, pip-audit -----------------------------------------


def venv_python(repo_path: Path) -> Optional[str]:
    for candidate in ("venv", ".venv"):
        py = repo_path / candidate / "bin" / "python3"
        if py.exists():
            return str(py)
    return None


def parse_pytest_counts(output: str) -> tuple[int, int]:
    passed = failed = 0
    match = re.search(r"(\d+) passed", output)
    if match:
        passed = int(match.group(1))
    match = re.search(r"(\d+) failed", output)
    if match:
        failed = int(match.group(1))
    return passed, failed


def run_python_tests(repo_path: Path) -> dict:
    if not (repo_path / "tests").exists():
        return {"status": "sin_tests", "passed": 0, "failed": 0, "coverage_pct": None, "output": ""}

    python_bin = venv_python(repo_path) or "python3"
    env = {**os.environ, "CI": "true"}
    proc, timed_out = _run_with_timeout(
        [python_bin, "-m", "pytest", "--tb=short", "-q"], cwd=repo_path, timeout=TEST_TIMEOUT_SECONDS, env=env
    )
    output = (proc.stdout + "\n" + proc.stderr)[-20000:]
    if timed_out:
        return {"status": "timeout", "passed": 0, "failed": 0, "coverage_pct": None, "output": output}
    passed, failed = parse_pytest_counts(output)
    status = "ok" if proc.returncode == 0 else "failing"
    return {"status": status, "passed": passed, "failed": failed, "coverage_pct": None, "output": output}


def run_py_compile(repo_path: Path) -> list[str]:
    errors: list[str] = []
    skip_dirs = {"venv", ".venv", "__pycache__", "node_modules"}
    for py_file in repo_path.rglob("*.py"):
        if skip_dirs & set(py_file.parts):
            continue
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "py_compile", str(py_file)],
                capture_output=True,
                text=True,
                timeout=PY_COMPILE_TIMEOUT,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.warning("Fallo compilando %s: %s", py_file, exc)
            continue
        if proc.returncode != 0:
            errors.append(f"{py_file.relative_to(repo_path)}: {proc.stderr.strip()[-300:]}")
    return errors


def run_pip_audit(repo_path: Path, allow_install: bool = True) -> dict:
    requirements = repo_path / "requirements.txt"
    if not requirements.exists():
        return {}
    python_bin = venv_python(repo_path)
    if not python_bin:
        log.info("%s: sin venv detectado, se omite pip-audit", repo_path.name)
        return {}

    check = _run([python_bin, "-m", "pip_audit", "--version"], timeout=30)
    if not check:
        if not allow_install:
            log.info("%s: pip-audit no instalado, se omite (no se instala nada)", repo_path.name)
            return {}
        install = subprocess.run(
            [python_bin, "-m", "pip", "install", "pip-audit"], capture_output=True, text=True, timeout=180
        )
        if install.returncode != 0:
            log.warning("%s: no se pudo instalar pip-audit: %s", repo_path.name, install.stderr[-300:])
            return {}

    proc = run_cli(
        [python_bin, "-m", "pip_audit", "-r", "requirements.txt", "--format", "json"],
        cwd=repo_path,
        timeout=PIP_AUDIT_TIMEOUT,
    )
    try:
        return json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return {}


def summarize_vulnerabilities(data, stack: str) -> str:
    if not data:
        return ""
    if stack == "node":
        parts = [f"{k}:{v}" for k, v in data.items() if k in ("low", "moderate", "high", "critical") and v]
        return ", ".join(parts)
    if stack == "python":
        deps = data.get("dependencies", []) if isinstance(data, dict) else data
        vulnerable = [d for d in deps if isinstance(d, dict) and d.get("vulns")]
        return f"{len(vulnerable)} paquete(s) vulnerable(s)" if vulnerable else ""
    return ""


# --- Secretos hardcodeados -----------------------------------------------------


def redact_secret(value: str) -> str:
    return f"{value[:6]}***" if len(value) > 6 else "***"


def scan_for_secrets(repo_path: Path) -> list[dict]:
    combined = "|".join(f"(?:{pattern.pattern})" for pattern in SECRET_PATTERNS.values())
    proc = run_cli(["git", "grep", "-InP", combined], cwd=repo_path, timeout=60)
    if proc.returncode not in (0, 1):
        return []

    findings: list[dict] = []
    for line in proc.stdout.splitlines():
        file_part, _, rest = line.partition(":")
        for name, pattern in SECRET_PATTERNS.items():
            match = pattern.search(rest)
            if match:
                findings.append({"file": file_part, "kind": name, "sample": redact_secret(match.group(0))})
    return findings


# --- TypeSafe (Jev) -------------------------------------------------------------


async def evaluate_finding_with_typesafe(client: AsyncTypeSafeClient, finding: Finding) -> dict:
    try:
        response = await client.system_one(
            state={
                "repo": finding.repo,
                "category": finding.category,
                "summary": finding.summary,
                "detail": finding.detail[:1500],
            },
            questions={
                "severity": Score(
                    instructions="Que tan grave es este hallazgo de calidad/seguridad para el repositorio.",
                    criteria=SEVERITY_LEVELS,
                ),
                "category": Choice(
                    instructions="A que categoria de hallazgo de QA corresponde.", criteria=CATEGORY_CRITERIA
                ),
                "should_generate_tests": Noul(
                    instructions="Vale la pena generar tests automaticamente con Aider para resolver este hallazgo."
                ),
                "who_to_notify": Choice(
                    instructions="A quien de la Batifamilia hay que avisar sobre este hallazgo.",
                    criteria=WHO_TO_NOTIFY_CRITERIA,
                ),
            },
        )
    except Exception as exc:  # noqa: BLE001 - un fallo de TypeSafe no debe tumbar la auditoria
        log.error("Fallo la evaluacion TypeSafe de '%s' en %s: %s", finding.category, finding.repo, exc)
        return fallback_decision(finding)

    severity_raw = response.scores["severity"].score
    severity_idx = max(0, min(len(SEVERITY_LEVELS) - 1, round(severity_raw)))
    return {
        "severity_label": SEVERITY_LEVELS[severity_idx],
        "category": response.choices["category"].choice,
        "should_generate_tests": response.nouls["should_generate_tests"].noul,
        "who_to_notify": response.choices["who_to_notify"].choice,
    }


def fallback_decision(finding: Finding) -> dict:
    if finding.baseline_severity in ("critical", "high"):
        who = "alfred"
    elif finding.category in ("missing_tests", "low_coverage", "lint_error"):
        who = "nightwing"
    else:
        who = "none"
    return {
        "severity_label": finding.baseline_severity,
        "category": finding.category,
        "should_generate_tests": 1.0 if finding.category in ("missing_tests", "low_coverage") else 0.0,
        "who_to_notify": who,
    }


# --- GitHub: issues de QA -------------------------------------------------------


def find_duplicate_issue(repo_obj, marker: str):
    try:
        for issue in repo_obj.get_issues(state="open", labels=["red-hood"]):
            if marker in (issue.title or ""):
                return issue
    except GithubException as exc:
        log.error("Error buscando issues duplicados en %s: %s", repo_obj.full_name, exc)
    return None


def create_qa_issue(gh: Github, github_repo: str, title: str, body: str, extra_labels: list[str]) -> Optional[str]:
    try:
        repo_obj = gh.get_repo(github_repo)
    except GithubException as exc:
        log.error("No se pudo abrir el repo de GitHub %s: %s", github_repo, exc)
        return None

    existing = find_duplicate_issue(repo_obj, title)
    if existing:
        log.info("Issue de QA duplicado ya existe en %s: #%s", github_repo, existing.number)
        return existing.html_url

    labels = sorted({"red-hood", "bug", *extra_labels})
    try:
        issue = repo_obj.create_issue(title=title, body=body[:4000], labels=labels)
        return issue.html_url
    except GithubException as exc:
        log.error("No se pudo crear issue de QA en %s: %s", github_repo, exc)
        return None


# --- FASE 5: generacion de tests con Aider ------------------------------------


def guess_node_test_path(repo_path: Path, source_rel: str) -> str:
    path = Path(source_rel)
    if (repo_path / path.parent / "__tests__").exists():
        return str(path.parent / "__tests__" / f"{path.stem}.test{path.suffix}")
    if path.suffix in (".tsx", ".jsx"):
        return str(path.parent / f"{path.stem}.test{path.suffix}")
    return str(path.parent / f"{path.stem}.spec{path.suffix}")


def guess_python_test_path(source_rel: str) -> str:
    return str(Path("tests") / f"test_{Path(source_rel).stem}.py")


def find_untested_python_files(repo_path: Path, limit: int) -> list[str]:
    tests_dir = repo_path / "tests"
    existing = {p.stem.removeprefix("test_") for p in tests_dir.glob("test_*.py")} if tests_dir.exists() else set()

    candidates: list[str] = []
    for py_file in sorted(repo_path.glob("*.py")) + sorted(repo_path.glob("**/*.py")):
        if "venv" in py_file.parts or ".venv" in py_file.parts or "__pycache__" in py_file.parts:
            continue
        if py_file.name.startswith("test_") or py_file.name == "__init__.py":
            continue
        rel = str(py_file.relative_to(repo_path))
        if py_file.stem in existing or rel in candidates:
            continue
        candidates.append(rel)
        if len(candidates) >= limit:
            break
    return candidates


def build_aider_test_message(source_rel: str, test_rel: str, framework: str, claude_md: str) -> str:
    context = claude_md[:3000] if claude_md else "(sin CLAUDE.md en el repositorio)"
    return (
        f"Escribe tests para `{source_rel}` en el archivo `{test_rel}`, usando {framework}.\n\n"
        f"## Contexto del repositorio (CLAUDE.md)\n{context}\n\n"
        "Reglas obligatorias:\n"
        f"- SOLO crea o edita `{test_rel}`. No modifiques `{source_rel}` ni ningun otro archivo.\n"
        "- No ejecutes comandos de git.\n"
        "- Cubre los casos principales y al menos un caso limite o de error."
    )


def run_aider_for_test(repo_path: Path, model: str, message: str, source_rel: str, test_rel: str) -> bool:
    cmd = [
        AIDER_BIN,
        "--model", model,
        "--message", message,
        "--yes",
        "--no-auto-commits",
        "--env", f"OLLAMA_API_BASE={OLLAMA_API_BASE}",
        source_rel,
        test_rel,
    ]
    try:
        proc = subprocess.run(cmd, cwd=repo_path, capture_output=True, text=True, timeout=AIDER_TEST_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.error("Fallo ejecutando aider generando %s: %s", test_rel, exc)
        return False
    if proc.returncode != 0:
        log.error("aider devolvio codigo %s generando %s: %s", proc.returncode, test_rel, proc.stderr[-500:])
        return False
    return True


TEST_FILE_NAME_RE = re.compile(r"^test_.+\.py$|.+\.(spec|test)\.[jt]sx?$")


def only_test_files_changed(repo_path: Path) -> bool:
    files = changed_files(repo_path)
    if not files:
        return False
    for rel_path in files:
        if is_forbidden_path(rel_path):
            return False
        if not TEST_FILE_NAME_RE.match(Path(rel_path).name):
            return False
    return True


def discard_changes(repo_path: Path) -> None:
    for rel_path in changed_files(repo_path):
        run_cli(["git", "checkout", "--", rel_path], cwd=repo_path)
        run_cli(["git", "clean", "-f", "--", rel_path], cwd=repo_path)


def run_single_node_test(repo_path: Path, test_rel: str) -> bool:
    runner = node_test_runner(repo_path)
    cmd = ["npx", "vitest", "run", test_rel] if runner == "vitest" else ["npx", "jest", test_rel]
    return run_cli(cmd, cwd=repo_path, timeout=SINGLE_TEST_TIMEOUT).returncode == 0


def run_single_python_test(repo_path: Path, test_rel: str) -> bool:
    python_bin = venv_python(repo_path) or "python3"
    proc = run_cli([python_bin, "-m", "pytest", test_rel, "--tb=short", "-q"], cwd=repo_path, timeout=SINGLE_TEST_TIMEOUT)
    return proc.returncode == 0


def generate_tests_for_repo(
    repo_path: Path, stack: str, branch: str, candidates: list[str], model: str, max_files: int
) -> list[dict]:
    claude_md = read_repo_claude_md(repo_path)
    if stack == "node":
        framework = "Vitest" if node_test_runner(repo_path) == "vitest" else "Jest"
    else:
        framework = "pytest"

    results: list[dict] = []
    for source_rel in candidates[:max_files]:
        if not repo_is_clean(repo_path):
            log.warning("%s: repo con cambios sin commitear, se detiene la generacion de tests", repo_path.name)
            break

        test_rel = guess_node_test_path(repo_path, source_rel) if stack == "node" else guess_python_test_path(source_rel)
        message = build_aider_test_message(source_rel, test_rel, framework, claude_md)
        ok = run_aider_for_test(repo_path, model, message, source_rel, test_rel)

        if not ok or not only_test_files_changed(repo_path):
            discard_changes(repo_path)
            results.append({"file": source_rel, "test_file": test_rel, "status": "aider_failed"})
            continue

        passed = (
            run_single_node_test(repo_path, test_rel) if stack == "node" else run_single_python_test(repo_path, test_rel)
        )
        if passed:
            commit_and_push(repo_path, branch, f"test(red-hood): add tests for {source_rel}")
            results.append({"file": source_rel, "test_file": test_rel, "status": "committed"})
        else:
            discard_changes(repo_path)
            results.append({"file": source_rel, "test_file": test_rel, "status": "discarded"})
    return results


# --- Hallazgos: reporte y escalado ---------------------------------------------


def build_findings(
    repo_name: str,
    test_result: dict,
    lint_output: str,
    py_compile_errors: list[str],
    audit_vulns: dict,
    stack: str,
    coverage_threshold: int,
) -> list[Finding]:
    findings: list[Finding] = []

    if test_result["status"] == "failing":
        findings.append(
            Finding(
                category="failing_test",
                summary=f"{test_result['failed']} test(s) fallando en {repo_name}",
                detail=test_result["output"][-1500:],
                baseline_severity="high",
                repo=repo_name,
            )
        )
    elif test_result["status"] == "timeout":
        findings.append(
            Finding(
                category="test_timeout",
                summary=f"Los tests de {repo_name} excedieron el tiempo limite ({TEST_TIMEOUT_SECONDS}s)",
                detail=test_result["output"][-1500:],
                baseline_severity="low",
                repo=repo_name,
            )
        )
    elif test_result["status"] == "sin_tests":
        findings.append(
            Finding(
                category="missing_tests",
                summary=f"{repo_name} no tiene tests configurados",
                detail="Sin script de test en package.json, o sin directorio tests/.",
                baseline_severity="medium",
                repo=repo_name,
            )
        )

    coverage_pct = test_result.get("coverage_pct")
    if coverage_pct is not None and coverage_pct < coverage_threshold:
        findings.append(
            Finding(
                category="low_coverage",
                summary=f"Cobertura baja en {repo_name}: {coverage_pct:.1f}% (umbral {coverage_threshold}%)",
                detail=test_result["output"][-1500:],
                baseline_severity="medium",
                repo=repo_name,
            )
        )

    if lint_output.strip():
        findings.append(
            Finding(
                category="lint_error",
                summary=f"Errores de lint en {repo_name}",
                detail=lint_output[-1500:],
                baseline_severity="low",
                repo=repo_name,
            )
        )

    if py_compile_errors:
        findings.append(
            Finding(
                category="lint_error",
                summary=f"{len(py_compile_errors)} archivo(s) con errores de sintaxis en {repo_name}",
                detail="\n".join(py_compile_errors)[-1500:],
                baseline_severity="high",
                repo=repo_name,
            )
        )

    vuln_summary = summarize_vulnerabilities(audit_vulns, stack)
    if vuln_summary:
        findings.append(
            Finding(
                category="vulnerability",
                summary=f"Vulnerabilidades de dependencias en {repo_name}: {vuln_summary}",
                detail=json.dumps(audit_vulns)[:1500],
                baseline_severity="high" if any(level in vuln_summary for level in ("high", "critical")) else "medium",
                repo=repo_name,
            )
        )

    return findings


async def handle_finding(
    finding: Finding,
    decision: dict,
    gh: Optional[Github],
    github_repo: Optional[str],
    state_entry: dict,
    notifier,
    dry_run: bool,
) -> None:
    severity = decision["severity_label"]
    who = decision["who_to_notify"]

    repeated = track_finding_repetition(state_entry, finding.category)
    if not dry_run:
        update_memory(
            "finding",
            summary=finding.summary,
            category=finding.category,
            severity=severity,
            escalated_to_alfred=(who == "alfred" or severity == "critical"),
        )
        if repeated:
            memory_store.add_learning(
                "red_hood",
                f"'{finding.category}' se repite 3+ veces en {finding.repo}: revisar patron recurrente.",
            )

    if severity == "critical":
        await notifier.send(
            f"🔴🚨 Red Hood encontró: {html.escape(finding.summary)} en "
            f"<code>{html.escape(finding.repo)}</code>"
        )
    elif who == "alfred":
        await notifier.send(
            f"🔴 Red Hood: {html.escape(finding.summary)} en "
            f"<code>{html.escape(finding.repo)}</code> (severidad {severity})"
        )

    if dry_run:
        log.info(
            "[dry-run] %s: hallazgo '%s' (severidad %s, notificar %s)", finding.repo, finding.category, severity, who
        )
        return

    targets: set[str] = set()
    if who in ("batman", "nightwing"):
        targets.add(who)
    if finding.category == "hardcoded_secret":
        targets.add("lucius")

    for target in targets:
        append_comm(
            {
                "timestamp": datetime.now().isoformat(),
                "from": "red_hood",
                "to": target,
                "type": "qa_finding",
                "repo": finding.repo,
                "category": finding.category,
                "severity": severity,
                "message": finding.summary,
                "read": False,
            }
        )

    if severity in ("high", "critical") and gh is not None and github_repo:
        arreglable = finding.category in ("missing_tests", "low_coverage", "lint_error")
        extra_labels = ["night-agent"] if who == "nightwing" and arreglable else []
        title = f"[Red Hood] {finding.category}: {finding.summary}"[:250]
        url = create_qa_issue(gh, github_repo, title, finding.detail, extra_labels)
        if url:
            log.info("Issue de QA para %s: %s", finding.repo, url)


# --- Orquestacion por repo -------------------------------------------------------


def format_repo_summary(summary: dict) -> str:
    if summary.get("skipped"):
        return f"🔴 <code>{html.escape(summary['repo'])}</code>: sin commits nuevos, omitido"

    test_result = summary["test_result"]
    tests_icon = {"ok": "✅", "failing": "❌", "timeout": "⏱️"}.get(test_result["status"], "➖")
    coverage = f"{test_result['coverage_pct']:.1f}%" if test_result.get("coverage_pct") is not None else "N/D"
    vulns = summarize_vulnerabilities(summary["vulnerabilities"], summary["stack"]) or "ninguna"
    generated = summary["generated"]
    generated_line = (
        ", ".join(f"<code>{html.escape(g['file'])}</code> ({html.escape(g['status'])})" for g in generated)
        if generated
        else "ninguno"
    )
    read_only_tag = " (solo lectura, sin rama dev)" if summary["read_only"] else ""

    return (
        f"🔴 <b>Red Hood</b> — <code>{html.escape(summary['repo'])}</code>{read_only_tag}\n"
        f"Tests: {tests_icon} ({test_result['passed']} ok / {test_result['failed']} fallando)\n"
        f"Cobertura: {coverage}\n"
        f"Vulnerabilidades: {html.escape(vulns)}\n"
        f"Tests generados: {generated_line}"
    )


async def audit_repo_dry_run(prod_repo_path: Path, repo_name: str, state_entry: dict) -> dict:
    """Auditoria 100% de solo lectura para --dry-run: nunca toca prod_repo_path.

    Solo corre `git log`, lee package.json/requirements.txt, `git grep` de
    secretos y `npm audit --json` (no modifica nada). No clona, no hace
    checkout, no instala nada, no corre tests/lint (requerirían instalar
    dependencias), no comitea, no crea issues y no envia Telegram.
    """
    log.info(
        "[dry-run] %s: auditoria de solo lectura sobre %s (sin clonar, sin checkout, sin instalar nada)",
        repo_name, prod_repo_path,
    )

    head_hash = _run(["git", "-C", str(prod_repo_path), "rev-parse", "HEAD"], timeout=30)
    log.info("[dry-run] %s: git log -> HEAD=%s", repo_name, head_hash[:8] if head_hash else "desconocido")
    if head_hash and head_hash == state_entry.get("last_hash"):
        log.info("[dry-run] %s: sin commits nuevos desde la ultima auditoria, se omitiria", repo_name)
        return {"repo": repo_name, "skipped": True}

    stack = detect_stack(prod_repo_path)
    log.info("[dry-run] %s: stack detectado (package.json/requirements.txt) -> %s", repo_name, stack)

    audit_vulns: dict = {}
    if stack == "node":
        log.info("[dry-run] %s: ejecutaria npm audit --json (solo lectura, no modifica nada)", repo_name)
        audit_vulns = run_node_audit(prod_repo_path)
    elif stack == "python":
        python_bin = venv_python(prod_repo_path)
        if python_bin:
            log.info("[dry-run] %s: pip-audit ya instalado en el venv, se consulta sin instalar nada", repo_name)
            audit_vulns = run_pip_audit(prod_repo_path, allow_install=False)
        else:
            log.info("[dry-run] %s: sin venv detectado, se omite pip-audit (dry-run no instala nada)", repo_name)

    log.info("[dry-run] %s: escaneando secretos con git grep (solo lectura)", repo_name)
    secrets = scan_for_secrets(prod_repo_path)
    if secrets:
        redacted = ", ".join(f"{s['file']} ({s['kind']}: {s['sample']})" for s in secrets[:5])
        log.warning(
            "[dry-run] %s: [CRITICO] secretos hardcodeados encontrados: %s (no se envia Telegram ni se crea issue)",
            repo_name, redacted,
        )

    vuln_summary = summarize_vulnerabilities(audit_vulns, stack)
    log.info("[dry-run] %s: vulnerabilidades -> %s", repo_name, vuln_summary or "ninguna")
    log.info(
        "[dry-run] %s: no se ejecutan tests/lint (requerirían instalar dependencias), "
        "no se comitea, no se crean issues, no se envia Telegram", repo_name,
    )

    return {
        "repo": repo_name,
        "dry_run": True,
        "read_only": True,
        "stack": stack,
        "vulnerabilities": audit_vulns,
        "secrets_found": len(secrets),
        "findings": [],
        "generated": [],
    }


async def audit_repo(
    repo_path: Path,
    config: dict,
    gh: Optional[Github],
    client: Optional[AsyncTypeSafeClient],
    notifier,
    state: dict,
    dry_run: bool,
) -> Optional[dict]:
    if not repo_path.exists():
        log.warning("Repo no encontrado en %s, se omite", repo_path)
        return None

    repo_name = display_repo_name(repo_path)
    state_entry = state.setdefault(str(repo_path), {})

    if dry_run:
        return await audit_repo_dry_run(repo_path, repo_name, state_entry)

    workspace_dir = get_workspace_dir(config)
    repo_path, branch, read_only = setup_workspace_repo(repo_path, workspace_dir)
    if repo_path is None:
        log.warning("%s: no se pudo preparar el workspace aislado, se omite", repo_name)
        return {"repo": repo_name, "skipped": True}

    head_hash = run_cli(["git", "rev-parse", "HEAD"], cwd=repo_path).stdout.strip()

    if head_hash and head_hash == state_entry.get("last_hash"):
        log.info("%s: sin commits nuevos desde la ultima auditoria, se omite", repo_name)
        return {"repo": repo_name, "skipped": True}

    log.info(
        "🔴 Red Hood auditando %s en workspace aislado %s (rama %s%s)",
        repo_name, repo_path, branch, ", solo lectura" if read_only else "",
    )

    stack = detect_stack(repo_path)
    test_result = {"status": "sin_tests", "passed": 0, "failed": 0, "coverage_pct": None, "output": ""}
    audit_vulns: dict = {}
    lint_output = ""
    py_compile_errors: list[str] = []

    if stack == "node":
        npm_ci_if_needed(repo_path, state_entry)
        test_result = run_node_tests(repo_path)
        lint_output = run_node_lint(repo_path)
        audit_vulns = run_node_audit(repo_path)
    elif stack == "python":
        test_result = run_python_tests(repo_path)
        py_compile_errors = run_py_compile(repo_path)
        audit_vulns = run_pip_audit(repo_path)
    else:
        log.warning("%s: stack no reconocido (sin package.json, requirements.txt ni *.py)", repo_name)

    coverage_threshold = config.get("red_hood", {}).get("coverage_threshold", DEFAULT_COVERAGE_THRESHOLD)
    findings = build_findings(repo_name, test_result, lint_output, py_compile_errors, audit_vulns, stack, coverage_threshold)

    secrets = scan_for_secrets(repo_path)
    if secrets:
        redacted = ", ".join(
            f"<code>{html.escape(s['file'])}</code> ({html.escape(s['kind'])}: {html.escape(s['sample'])})"
            for s in secrets[:5]
        )
        await notifier.send(
            f"🔴🚨 Red Hood encontró: secretos hardcodeados en <code>{html.escape(repo_name)}</code>\n"
            f"{redacted}"
        )
        findings.insert(
            0,
            Finding(
                category="hardcoded_secret",
                summary=f"{len(secrets)} secreto(s) hardcodeado(s) en {repo_name}",
                detail=redacted,
                baseline_severity="critical",
                repo=repo_name,
            ),
        )

    github_repo = get_github_repo_full_name(repo_path)
    evaluated: list[tuple[Finding, dict]] = []
    for finding in findings:
        if finding.category == "hardcoded_secret":
            decision = {
                "severity_label": "critical",
                "category": "hardcoded_secret",
                "should_generate_tests": 0.0,
                "who_to_notify": "alfred",
            }
        elif client is not None:
            decision = await evaluate_finding_with_typesafe(client, finding)
        else:
            decision = fallback_decision(finding)
        evaluated.append((finding, decision))
        await handle_finding(finding, decision, gh, github_repo, state_entry, notifier, dry_run)

    generated: list[dict] = []
    if not read_only:
        max_files = config.get("red_hood", {}).get("max_tests_per_repo", DEFAULT_MAX_TESTS_PER_REPO)
        should_generate = any(
            decision["should_generate_tests"] > SHOULD_GENERATE_THRESHOLD and finding.category in ("missing_tests", "low_coverage")
            for finding, decision in evaluated
        )
        coverage_pct = test_result.get("coverage_pct")
        effective_coverage = coverage_pct if coverage_pct is not None else (0 if test_result["status"] == "sin_tests" else 100)

        if should_generate and effective_coverage < coverage_threshold:
            if stack == "node":
                candidates = find_low_coverage_files(test_result["output"], max_files)
            elif stack == "python":
                candidates = find_untested_python_files(repo_path, max_files)
            else:
                candidates = []

            if candidates:
                model = config["models"]["aider"]
                generated = generate_tests_for_repo(repo_path, stack, branch, candidates, model, max_files)
                for item in generated:
                    if item["status"] == "committed":
                        update_memory("test_generated_success", file=item["file"], framework=stack)
                    else:
                        update_memory("test_generated_failed", file=item["file"])

    state_entry["last_hash"] = head_hash
    save_state(state)

    summary = {
        "repo": repo_name,
        "branch": branch,
        "read_only": read_only,
        "stack": stack,
        "test_result": test_result,
        "vulnerabilities": audit_vulns,
        "findings": [
            {"category": finding.category, "summary": finding.summary, "severity": decision["severity_label"]}
            for finding, decision in evaluated
        ],
        "generated": generated,
    }
    await notifier.send(format_repo_summary(summary))
    return summary


def build_final_summary(summaries: list[dict], report_path: Path) -> str:
    audited = [s for s in summaries if not s.get("skipped")]
    skipped = [s for s in summaries if s.get("skipped")]
    total_findings = sum(len(s.get("findings", [])) for s in audited)
    total_generated = sum(len(s.get("generated", [])) for s in audited)
    return (
        "🔴 <b>Red Hood — auditoria completada</b>\n"
        f"Repos auditados: {len(audited)} | omitidos (sin cambios): {len(skipped)}\n"
        f"Hallazgos totales: {total_findings}\n"
        f"Tests generados: {total_generated}\n"
        f"Reporte: <code>{html.escape(report_path.name)}</code>"
    )


def write_report(started_at: datetime, summaries: list[dict]) -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    date_str = started_at.strftime("%Y-%m-%d")
    report_path = REPORTS_DIR / f"red-hood-{date_str}.md"

    lines = [f"# Reporte de QA (Red Hood) - {date_str}", ""]
    for summary in summaries:
        lines.append(f"## {summary['repo']}")
        if summary.get("skipped"):
            lines.append("Sin commits nuevos desde la ultima auditoria, omitido.\n")
            continue

        if summary.get("dry_run"):
            lines.append("- [dry-run] Solo lectura: sin clonar, checkout ni instalar nada")
            lines.append(f"- [dry-run] Stack: {summary['stack']}")
            lines.append(
                f"- [dry-run] Vulnerabilidades: {summarize_vulnerabilities(summary['vulnerabilities'], summary['stack']) or 'ninguna'}"
            )
            lines.append(f"- [dry-run] Secretos hardcodeados encontrados: {summary.get('secrets_found', 0)}")
            lines.append("- [dry-run] Tests/lint no ejecutados (requerirían instalar dependencias)")
            lines.append("")
            continue

        test_result = summary["test_result"]
        lines.append(f"- Rama: {summary['branch']}{' (solo lectura)' if summary['read_only'] else ''}")
        lines.append(f"- Tests: {test_result['status']} ({test_result['passed']} ok / {test_result['failed']} fallando)")
        coverage_pct = test_result.get("coverage_pct")
        lines.append(f"- Cobertura: {coverage_pct:.1f}%" if coverage_pct is not None else "- Cobertura: N/D")
        lines.append(f"- Vulnerabilidades: {summarize_vulnerabilities(summary['vulnerabilities'], summary['stack']) or 'ninguna'}")

        if summary["findings"]:
            lines.append("- Hallazgos:")
            lines.extend(f"  - [{f['severity']}] {f['category']}: {f['summary']}" for f in summary["findings"])

        if summary["generated"]:
            lines.append("- Tests generados:")
            lines.extend(f"  - {g['file']} -> {g['test_file']} ({g['status']})" for g in summary["generated"])

        lines.append("")

    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Red Hood: QA brutal de la Batifamilia")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Solo audita: no genera tests, no crea issues, no comitea/pushea, no envia Telegram",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    load_env()
    notifier = NullNotifier() if args.dry_run else TelegramNotifier()

    try:
        config = load_config()
    except (OSError, yaml.YAMLError) as exc:
        log.exception("No se pudo cargar config.yaml")
        await notifier.send(
            f"🔴 <b>Red Hood encontró un problema</b>: no pudo iniciar (error leyendo config.yaml)\n"
            f"<code>{html.escape(str(exc))}</code>"
        )
        sys.exit(1)

    repo_paths = config.get("red_hood", {}).get("local_repos", [])
    if not repo_paths:
        log.warning("config.yaml no tiene 'red_hood.local_repos' configurado, nada que auditar")
        return

    started_at = datetime.now()
    await notifier.send(f"🔴 Red Hood entrando en acción — auditando {len(repo_paths)} repos")

    gh = None if args.dry_run else get_github_client()
    state = load_state()
    summaries: list[dict] = []

    async def run_all(client: Optional[AsyncTypeSafeClient]) -> None:
        for raw_path in repo_paths:
            try:
                summary = await audit_repo(Path(raw_path).expanduser(), config, gh, client, notifier, state, args.dry_run)
            except Exception as exc:  # noqa: BLE001 - un repo no debe tumbar el resto de la auditoria
                log.exception("Fallo inesperado auditando %s", raw_path)
                summary = {"repo": raw_path, "skipped": True, "error": str(exc)}
            if summary:
                summaries.append(summary)

    typesafe_available = bool(os.environ.get("TYPESAFE_API_KEY"))
    if typesafe_available:
        async with AsyncTypeSafeClient() as client:
            await run_all(client)
    else:
        log.warning("TYPESAFE_API_KEY no esta definida, Red Hood opera en modo fallback (sin Jev)")
        await run_all(None)

    report_path = write_report(started_at, summaries)
    await notifier.send(build_final_summary(summaries, report_path))
    if not args.dry_run:
        update_memory("audit_complete")

    log.info("Red Hood: auditoria completada (%d repo(s) procesados)", len(summaries))


if __name__ == "__main__":
    asyncio.run(main())
