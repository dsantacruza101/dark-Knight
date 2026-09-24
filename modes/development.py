"""Modo de Desarrollo Nocturno: Nightwing resuelve issues 'night-agent' usando Aider.

Claude (Batman) descansa en este modo. Cada issue etiquetado se resuelve localmente
con Aider (`AIDER_BIN`), usando Ollama como backend (`models.aider` en config.yaml).

Workspace aislado (`development.workspace_dir`, por defecto
`~/projects/.nightwing-workspace`): Nightwing NUNCA hace checkout, pull,
commits ni ediciones sobre los directorios de produccion listados en
`config.yaml` (`repo_paths`). Todo el trabajo ocurre sobre una copia clonada
en `workspace_dir/<nombre-repo>`:

  - Si la copia no existe: `git clone` (autenticado con `GITHUB_TOKEN`) desde
    `https://github.com/<repo_full_name>.git`.
  - Si ya existe: `git fetch origin` + `checkout <work_branch>` +
    `reset --hard origin/<work_branch>`.
  - Si `origin` no tiene la rama de trabajo (`github.work_branch`), se crea
    en el workspace desde `origin/main` y se pushea con `-u origin <rama>`.

Flujo por issue:
  1. Se lee el titulo y la descripcion del issue (y se identifica su repo,
     que ya viene dado por de donde se obtuvo el issue).
  2. Se prepara el workspace aislado del repo (ver arriba).
  3. Se lee el CLAUDE.md del repo, pero SOLO desde el directorio de
     produccion mapeado en `config.yaml` (`repo_paths`) -- nunca desde el
     workspace -- para dar contexto real y actualizado al modelo.
  4. Se buscan ejemplos relevantes en knowledge/ (coincidencia de palabras
     clave con el issue).
  5. Se arma un mensaje detallado y se pasa a Aider (`--message`), que edita
     los archivos directamente en disco sobre el workspace aislado.
  6. Los cambios en rutas prohibidas quedan revertidos antes de comitear.
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
    (solo se leen, y solo para obtener el CLAUDE.md).
  - Maximo `MAX_HOURS_PER_ISSUE` horas por issue; si se supera se aborta ese
    issue y se continua con el siguiente.
  - Si Aider no genera una solucion utilizable tras `MAX_AIDER_RETRIES`
    intentos, se comenta 'needs-review' en el issue y se continua.

El progreso se reporta a Telegram cada `PROGRESS_INTERVAL` (30 min). Como las
llamadas a git/aider son bloqueantes, la notificacion se emite entre issues,
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
KNOWLEDGE_DIR = BASE_DIR / "knowledge"
REPORTS_DIR = BASE_DIR / "reports"

AIDER_BIN = os.path.expanduser("~/.aider-venv/bin/aider")
OLLAMA_API_BASE = "http://localhost:11434"

DEFAULT_WORKSPACE_DIR = "~/projects/.nightwing-workspace"

MAX_HOURS_PER_ISSUE = 3
MAX_AIDER_RETRIES = 3
AIDER_TIMEOUT = 1800
CLONE_TIMEOUT = 300
PROGRESS_INTERVAL = timedelta(minutes=30)

FORBIDDEN_NAME_SUBSTRINGS = (".env", "docker-compose")
FORBIDDEN_PATH_SEGMENTS = ("nginx", "fail2ban", "cloudflared", ".cloudflared", ".ssh", "ufw")

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


def get_workspace_dir(config: dict) -> Path:
    return Path(config.get("development", {}).get("workspace_dir", DEFAULT_WORKSPACE_DIR)).expanduser()


def get_repo_paths(config: dict) -> dict[str, Path]:
    """Mapeo repo remoto -> ruta local de produccion (`config.yaml`, `repo_paths`).

    Se usa UNICAMENTE para leer el CLAUDE.md de contexto; nunca para checkout,
    pull, commits ni ninguna otra escritura (ver `read_claude_md_for_repo`).
    """
    return {name: Path(path).expanduser() for name, path in config.get("repo_paths", {}).items()}


def build_clone_url(repo_full_name: str) -> str:
    """URL de clone autenticada con GITHUB_TOKEN; nunca se loguea (contiene el token)."""
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        return f"https://{token}@github.com/{repo_full_name}.git"
    return f"https://github.com/{repo_full_name}.git"


def read_repo_claude_md(repo_path: Path) -> str:
    claude_md = repo_path / "CLAUDE.md"
    if not claude_md.exists():
        return ""
    try:
        return claude_md.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("No se pudo leer CLAUDE.md en %s: %s", repo_path, exc)
        return ""


def read_claude_md_for_repo(config: dict, repo_full_name: str) -> str:
    """Lee el CLAUDE.md desde el directorio de produccion mapeado en `repo_paths`.

    Es la UNICA operacion que toca el directorio de produccion, y es de solo
    lectura. Si el repo no esta mapeado (o la ruta no existe), se sigue sin
    contexto en vez de fallar el issue completo.
    """
    repo_short_name = repo_full_name.split("/")[-1]
    prod_path = get_repo_paths(config).get(repo_short_name)
    if not prod_path or not prod_path.exists():
        log.warning(
            "Sin ruta de produccion mapeada (repo_paths) para %s, se continua sin CLAUDE.md de contexto",
            repo_full_name,
        )
        return ""
    return read_repo_claude_md(prod_path)


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


def build_aider_message(issue: dict, claude_md: str, examples: list[str]) -> str:
    claude_context = claude_md[:4000] if claude_md else "(sin CLAUDE.md en el repositorio)"
    examples_context = "\n\n".join(examples) if examples else "(sin ejemplos relevantes en knowledge/)"
    return (
        f"Resuelve el issue #{issue['number']} del repositorio {issue['repo']}: "
        f"{issue['title']}\n\n"
        f"{issue['body'][:3000]}\n\n"
        f"## Contexto del repositorio (CLAUDE.md)\n{claude_context}\n\n"
        f"## Ejemplos de referencia\n{examples_context}\n\n"
        "Reglas obligatorias:\n"
        "- Usa rutas relativas dentro del repositorio.\n"
        "- NUNCA edites ni crees archivos .env, docker-compose*, ni "
        "configuracion de nginx, fail2ban, cloudflared o ssh.\n"
        "- No ejecutes comandos de git ni toques la rama main."
    )


def run_aider(repo_path: Path, model: str, message: str, timeout: int = AIDER_TIMEOUT) -> bool:
    """Ejecuta Aider sobre el repo; Aider edita los archivos directamente en disco."""
    cmd = [
        AIDER_BIN,
        "--model", model,
        "--message", message,
        "--yes",
        "--no-auto-commits",
    ]
    env = {**os.environ, "OLLAMA_API_BASE": OLLAMA_API_BASE}
    try:
        proc = subprocess.run(
            cmd,
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.error("Fallo ejecutando aider: %s", exc)
        return False
    if proc.returncode != 0:
        log.error("aider devolvio codigo %s: %s", proc.returncode, proc.stderr[-500:])
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


def run_cli(cmd: list[str], cwd: Optional[Path] = None, timeout: int = 600) -> subprocess.CompletedProcess:
    log.info("Ejecutando: %s (cwd=%s)", " ".join(cmd), cwd)
    try:
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.error("Fallo ejecutando %s: %s", " ".join(cmd), exc)
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
        clone = run_cli(["git", "clone", build_clone_url(repo_full_name), str(workspace_repo_path)], timeout=CLONE_TIMEOUT)
        if clone.returncode != 0:
            log.error("Fallo el clone de %s: %s", repo_full_name, clone.stderr[-500:])
            return None
    else:
        run_cli(["git", "fetch", "origin"], cwd=workspace_repo_path)

    if run_cli(["git", "rev-parse", "--verify", f"origin/{branch}"], cwd=workspace_repo_path).returncode == 0:
        if run_cli(["git", "checkout", branch], cwd=workspace_repo_path).returncode != 0:
            run_cli(["git", "checkout", "-b", branch, f"origin/{branch}"], cwd=workspace_repo_path)
        run_cli(["git", "reset", "--hard", f"origin/{branch}"], cwd=workspace_repo_path)
    else:
        log.warning("%s: origin no tiene la rama '%s', se crea desde origin/main", repo_full_name, branch)
        if run_cli(["git", "checkout", "main"], cwd=workspace_repo_path).returncode != 0:
            run_cli(["git", "checkout", "-b", "main", "origin/main"], cwd=workspace_repo_path)
        run_cli(["git", "reset", "--hard", "origin/main"], cwd=workspace_repo_path)
        if run_cli(["git", "checkout", "-b", branch], cwd=workspace_repo_path).returncode != 0:
            log.error("%s: no se pudo crear la rama '%s' en el workspace", repo_full_name, branch)
            return None
        push = run_cli(["git", "push", "-u", "origin", branch], cwd=workspace_repo_path)
        if push.returncode != 0:
            log.error("%s: fallo pusheando la nueva rama '%s': %s", repo_full_name, branch, push.stderr[-500:])
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
    branch = config["github"]["work_branch"]
    aider_model = config["models"]["aider"]
    workspace_dir = get_workspace_dir(config)

    repo_path = setup_workspace_repo(issue["repo"], workspace_dir, branch)
    if repo_path is None:
        msg = f"❌ {tag}: no se pudo preparar el workspace aislado para {issue['repo']}"
        log.error(msg)
        return msg

    if not repo_is_clean(repo_path):
        msg = f"⚠️ {tag}: el workspace tiene cambios sin commitear, se omite para no pisarlos"
        log.warning(msg)
        return msg

    claude_md = read_claude_md_for_repo(config, issue["repo"])
    examples = find_relevant_examples(issue)
    message = build_aider_message(issue, claude_md, examples)

    deadline = datetime.now() + timedelta(hours=MAX_HOURS_PER_ISSUE)
    written: list[str] = []
    attempts = 0
    while attempts < MAX_AIDER_RETRIES:
        if datetime.now() >= deadline:
            msg = f"⏱️ {tag}: se alcanzo el limite de {MAX_HOURS_PER_ISSUE}h, se pasa al siguiente issue"
            log.warning(msg)
            return msg

        attempts += 1
        if run_aider(repo_path, aider_model, message):
            reverted = revert_forbidden_changes(repo_path)
            if reverted:
                log.warning("%s: aider toco rutas prohibidas, revertidas: %s", tag, ", ".join(reverted))
            written = changed_files(repo_path)
            if written:
                break
        log.warning("%s: intento %d/%d con aider sin resultado util", tag, attempts, MAX_AIDER_RETRIES)

    if not written:
        mark_needs_review(
            issue["repo_obj"],
            issue["issue_obj"],
            f"Aider ({aider_model}) no genero una solucion utilizable tras {MAX_AIDER_RETRIES} intentos.",
        )
        msg = f"🚫 {tag}: aider fallo {MAX_AIDER_RETRIES} veces, marcado 'needs-review'"
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
