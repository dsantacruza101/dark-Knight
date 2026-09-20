"""Modo de Desarrollo Nocturno: Nightwing resuelve issues 'night-agent' usando SOLO Ollama.

Claude (Batman) descansa en este modo. Cada issue etiquetado se resuelve localmente
con el modelo definido en config.yaml (`models.ollama`). Flujo por issue:
  1. Se lee el titulo y la descripcion del issue (y se identifica su repo,
     que ya viene dado por de donde se obtuvo el issue).
  2. Se lee el CLAUDE.md del repo local para dar contexto al modelo.
  3. Se buscan ejemplos relevantes en knowledge/ (coincidencia de palabras
     clave con el issue).
  4. Se arma un prompt detallado y se ejecuta `ollama run <modelo>`.
  5. La respuesta se parsea como bloques de archivo y se escribe en disco,
     siempre sobre la rama de trabajo (`github.work_branch` en config.yaml).
  6. Se comitea con el mensaje 'feat(nightwing): ...'.

Restricciones duras (no configurables):
  - Nunca se toca ni se hace checkout de main/master; si el branch de trabajo
    configurado fuera main/master, el modo se niega a correr.
  - Nunca se escriben archivos .env, docker-compose*, ni configuracion de
    nginx/fail2ban/cloudflared/ssh (ver `is_forbidden_path`).
  - Maximo `MAX_HOURS_PER_ISSUE` horas por issue; si se supera se aborta ese
    issue y se continua con el siguiente.
  - Si Ollama no genera una solucion utilizable tras `MAX_OLLAMA_RETRIES`
    intentos, se comenta 'needs-review' en el issue y se continua.

El progreso se reporta a Telegram cada `PROGRESS_INTERVAL` (30 min). Como las
llamadas a git/ollama son bloqueantes, la notificacion se emite entre issues,
no con un timer independiente.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from github import Github, GithubException

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = Path("/etc/night-agent.env")
PROJECTS_DIR = Path.home() / "projects"
KNOWLEDGE_DIR = BASE_DIR / "knowledge"
REPORTS_DIR = BASE_DIR / "reports"

MAX_HOURS_PER_ISSUE = 3
MAX_OLLAMA_RETRIES = 3
OLLAMA_TIMEOUT = 1800
PROGRESS_INTERVAL = timedelta(minutes=30)

FORBIDDEN_NAME_SUBSTRINGS = (".env", "docker-compose")
FORBIDDEN_PATH_SEGMENTS = ("nginx", "fail2ban", "cloudflared", ".cloudflared", ".ssh", "ufw")

FILE_BLOCK_RE = re.compile(
    r"###\s*FILE:\s*(?P<path>\S.*?)\s*\n(?P<content>.*?)\n###\s*END",
    re.DOTALL,
)

log = logging.getLogger("night_agent.development")


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


def repo_local_path(repo_full_name: str) -> Path:
    return PROJECTS_DIR / repo_full_name.split("/")[-1]


def read_repo_claude_md(repo_path: Path) -> str:
    claude_md = repo_path / "CLAUDE.md"
    if not claude_md.exists():
        return ""
    try:
        return claude_md.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("No se pudo leer CLAUDE.md en %s: %s", repo_path, exc)
        return ""


def _tokenize(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-zA-Z0-9_]{4,}", text.lower())}


def find_relevant_examples(issue: dict, limit: int = 3, max_chars: int = 1200) -> list[str]:
    """Busca en knowledge/ los ejemplos con mas palabras clave en comun con el issue."""
    if not KNOWLEDGE_DIR.exists():
        return []
    issue_tokens = _tokenize(f"{issue['title']} {issue['body']}")
    if not issue_tokens:
        return []

    scored: list[tuple[int, Path, str]] = []
    for path in KNOWLEDGE_DIR.rglob("*"):
        if not path.is_file() or path.suffix not in (".md", ".json", ".txt"):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        overlap = len(issue_tokens & _tokenize(text))
        if overlap:
            scored.append((overlap, path, text))

    scored.sort(key=lambda item: item[0], reverse=True)
    return [
        f"### {path.relative_to(KNOWLEDGE_DIR)}\n{text[:max_chars]}"
        for _, path, text in scored[:limit]
    ]


def build_prompt(issue: dict, claude_md: str, examples: list[str]) -> str:
    claude_context = claude_md[:4000] if claude_md else "(sin CLAUDE.md en el repositorio)"
    examples_context = "\n\n".join(examples) if examples else "(sin ejemplos relevantes en knowledge/)"
    return (
        "Eres un desarrollador de software resolviendo un issue de GitHub en el "
        f"repositorio {issue['repo']}. Trabajas SOLO sobre la rama de desarrollo, "
        "nunca sobre main.\n\n"
        f"## Contexto del repositorio (CLAUDE.md)\n{claude_context}\n\n"
        f"## Ejemplos de referencia\n{examples_context}\n\n"
        f"## Issue #{issue['number']}: {issue['title']}\n{issue['body'][:3000]}\n\n"
        "## Formato de salida (obligatorio)\n"
        "Responde UNICAMENTE con uno o mas bloques con este formato exacto, sin "
        "texto antes, entre medio ni despues de los bloques:\n\n"
        "### FILE: ruta/relativa/al/archivo.ext\n"
        "<contenido completo y final del archivo>\n"
        "### END\n\n"
        "Reglas obligatorias:\n"
        "- Usa rutas relativas dentro del repositorio.\n"
        "- NUNCA generes ni modifiques archivos .env, docker-compose*, ni "
        "configuracion de nginx, fail2ban, cloudflared o ssh.\n"
        "- No incluyas comandos de git ni menciones la rama main.\n"
        "- Si no puedes resolver el issue con la informacion disponible, "
        "responde solo con la palabra NO_SOLUTION."
    )


def run_ollama(model: str, prompt: str, timeout: int = OLLAMA_TIMEOUT) -> Optional[str]:
    try:
        proc = subprocess.run(
            ["ollama", "run", model],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.error("Fallo ejecutando ollama: %s", exc)
        return None
    if proc.returncode != 0:
        log.error("ollama devolvio codigo %s: %s", proc.returncode, proc.stderr[-500:])
        return None
    return proc.stdout.strip() or None


def parse_file_blocks(output: str) -> dict[str, str]:
    files: dict[str, str] = {}
    for match in FILE_BLOCK_RE.finditer(output):
        path = match.group("path").strip()
        if path:
            files[path] = match.group("content")
    return files


def is_forbidden_path(rel_path: str) -> bool:
    normalized = rel_path.strip().lstrip("./").lower()
    name = Path(normalized).name
    if any(sub in name for sub in FORBIDDEN_NAME_SUBSTRINGS):
        return True
    parts = Path(normalized).parts
    return any(seg in parts or seg in name for seg in FORBIDDEN_PATH_SEGMENTS)


def apply_file_changes(repo_path: Path, files: dict[str, str]) -> list[str]:
    repo_root = repo_path.resolve()
    written: list[str] = []
    for rel_path, content in files.items():
        if is_forbidden_path(rel_path):
            log.warning("Archivo omitido por politica de seguridad: %s", rel_path)
            continue
        target = (repo_root / rel_path).resolve()
        if repo_root != target and repo_root not in target.parents:
            log.warning("Ruta fuera del repositorio omitida: %s", rel_path)
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content.rstrip("\n") + "\n", encoding="utf-8")
            written.append(rel_path)
        except OSError as exc:
            log.error("No se pudo escribir %s: %s", rel_path, exc)
    return written


def run_cli(cmd: list[str], cwd: Optional[Path] = None, timeout: int = 600) -> subprocess.CompletedProcess:
    log.info("Ejecutando: %s (cwd=%s)", " ".join(cmd), cwd)
    try:
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.error("Fallo ejecutando %s: %s", " ".join(cmd), exc)
        return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr=str(exc))


def ensure_dev_branch(repo_path: Path, branch: str) -> bool:
    """Deja el repo posicionado en `branch`. Nunca hace checkout de main/master."""
    if branch in ("main", "master"):
        log.error("work_branch configurado como '%s': el modo desarrollo se niega a usarlo", branch)
        return False

    run_cli(["git", "fetch", "origin"], cwd=repo_path)
    if run_cli(["git", "checkout", branch], cwd=repo_path).returncode != 0:
        if run_cli(["git", "checkout", "-b", branch, f"origin/{branch}"], cwd=repo_path).returncode != 0:
            if run_cli(["git", "checkout", "-b", branch], cwd=repo_path).returncode != 0:
                return False
    run_cli(["git", "pull", "origin", branch], cwd=repo_path)

    current = run_cli(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_path).stdout.strip()
    if current in ("main", "master"):
        log.error("Seguridad: el repo quedo en '%s', se aborta para no tocar main", current)
        return False
    return True


def repo_is_clean(repo_path: Path) -> bool:
    status = run_cli(["git", "status", "--porcelain"], cwd=repo_path)
    return status.returncode == 0 and not status.stdout.strip()


def commit_and_push(repo_path: Path, branch: str, message: str) -> bool:
    status = run_cli(["git", "status", "--porcelain"], cwd=repo_path)
    if not status.stdout.strip():
        return False
    run_cli(["git", "add", "-A"], cwd=repo_path)
    run_cli(["git", "commit", "-m", message], cwd=repo_path)
    push = run_cli(["git", "push", "origin", branch], cwd=repo_path)
    if push.returncode != 0:
        log.error("Fallo el push a %s: %s", branch, push.stderr[-500:])
    return True


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
    repo_path = repo_local_path(issue["repo"])
    branch = config["github"]["work_branch"]
    ollama_model = config["models"]["ollama"]

    if not repo_path.exists():
        msg = f"⚠️ {tag}: repo no encontrado en {repo_path}"
        log.warning(msg)
        return msg

    if not ensure_dev_branch(repo_path, branch):
        msg = f"❌ {tag}: no se pudo posicionar el repo en la rama '{branch}'"
        log.error(msg)
        return msg

    if not repo_is_clean(repo_path):
        msg = f"⚠️ {tag}: el repo tiene cambios sin commitear, se omite para no pisarlos"
        log.warning(msg)
        return msg

    claude_md = read_repo_claude_md(repo_path)
    examples = find_relevant_examples(issue)
    prompt = build_prompt(issue, claude_md, examples)

    deadline = datetime.now() + timedelta(hours=MAX_HOURS_PER_ISSUE)
    files: dict[str, str] = {}
    attempts = 0
    while attempts < MAX_OLLAMA_RETRIES:
        if datetime.now() >= deadline:
            msg = f"⏱️ {tag}: se alcanzo el limite de {MAX_HOURS_PER_ISSUE}h, se pasa al siguiente issue"
            log.warning(msg)
            return msg

        attempts += 1
        output = run_ollama(ollama_model, prompt)
        files = parse_file_blocks(output) if output else {}
        if files:
            break
        log.warning("%s: intento %d/%d con ollama sin resultado util", tag, attempts, MAX_OLLAMA_RETRIES)

    if not files:
        mark_needs_review(
            issue["repo_obj"],
            issue["issue_obj"],
            f"Ollama ({ollama_model}) no genero una solucion utilizable tras {MAX_OLLAMA_RETRIES} intentos.",
        )
        msg = f"🚫 {tag}: ollama fallo {MAX_OLLAMA_RETRIES} veces, marcado 'needs-review'"
        log.error(msg)
        return msg

    written = apply_file_changes(repo_path, files)
    if not written:
        msg = f"⚠️ {tag}: ollama respondio pero ningun archivo era valido para escribir"
        log.warning(msg)
        return msg

    commit_msg = f"feat(nightwing): resuelve #{issue['number']} - {issue['title']}"
    if commit_and_push(repo_path, branch, commit_msg):
        msg = (
            f"✅ {tag}: {len(written)} archivo(s) actualizados en `{branch}` "
            f"({', '.join(written)}): {issue['url']}"
        )
        log.info(msg)
        return msg

    return f"ℹ️ {tag}: ollama respondio pero no genero cambios reales en el repo"


def write_development_report(started_at: datetime, results: list[str]) -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    date_str = started_at.strftime("%Y-%m-%d")
    report_path = REPORTS_DIR / f"development-{date_str}.md"
    lines = [f"# Reporte de Modo Desarrollo (Ollama) - {date_str}", ""]
    if results:
        lines.extend(f"- {line}" for line in results)
    else:
        lines.append("No habia issues con label 'night-agent' pendientes.")
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_path


async def run_development_mode(config: dict, notifier) -> str:
    """Punto de entrada del modo Desarrollo: resuelve issues usando solo Ollama."""
    load_env()
    started_at = datetime.now()

    gh = get_github_client()
    issues = get_open_issues(gh, config)

    if not issues:
        report_path = write_development_report(started_at, [])
        return f"Sin issues con label '{config['github']['issue_label']}' pendientes. Reporte: {report_path}"

    await notifier.send(
        f"🐦 *Nightwing — Modo Desarrollo (Ollama)*\n{len(issues)} issue(s) por resolver "
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
