"""Release manager de Spoiler: sincroniza ramas de trabajo y prepara los PRs a la rama base.

    python3 -m batcave.release [--dry-run]

Por cada repo de los perfiles de `batcave/specialists/*.yaml` (union sin
duplicados; rama de trabajo = `resolve_work_branch`, base = rama por defecto
del repo en GitHub, o `origin/HEAD` del clone si no hay API), sobre una copia
aislada en `release.workspace_dir/<repo>` (default
`~/projects/.release-workspace`; se niega a correr si se cruza con un
directorio de produccion de `repo_paths` / `red_hood.local_repos`):

  1. SYNC: si la base tiene commits que no estan en la rama de trabajo,
     `git merge --no-edit origin/<base>` sobre la rama de trabajo y `git push
     origin <rama>` (nunca reset del remoto ni force-push; el `reset --hard`
     es solo de la copia local del workspace contra `origin/<rama>`). Si hay
     conflicto: `git merge --abort`, no se pushea nada, se avisa a Alfred
     (una vez por par de SHAs base/trabajo) y queda en el estado.
  2. PR: si la rama de trabajo tiene commits (sin contar merges) que no estan
     en la base, se crea el PR `<rama> -> <base>` si no hay uno abierto, o se
     actualiza su cuerpo: commits agrupados en parches de seguridad (radar),
     features, fixes y otros; `Closes #N` por cada issue `in-dev` cuyo commit
     de arreglo esta en el PR (SHA del comentario `Aplicado en ...` o
     `Resuelve #N` en el mensaje); estado de CI del commit HEAD. Nunca se
     mergea un PR.
  3. NOTIFY: un solo mensaje a Alfred con los repos con PR listo (link,
     commits, parches de seguridad), solo si algo cambio desde la corrida
     anterior (`batcave/memory/release_state.json`).

Auth de git por entorno (`github_auth_env`, nunca en la URL ni en
`.git/config`) y secretos enmascarados en el log (`release.log`).
`--dry-run`: sincroniza la copia local e imprime el plan (merge pendiente,
PR a crear/actualizar y su cuerpo); sin merge, push, PRs, Telegram ni estado.
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import logging
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

from batcave.radar import MaskingFilter, Notifier, production_paths, repo_full_name
from batcave.secrets import mask_secrets
from modes.development import (
    IN_DEV_LABEL,
    PROTECTED_BRANCHES,
    build_clone_url,
    get_github_client,
    git_cmd,
    github_auth_env,
    load_env,
    load_specialists,
    profile_repos,
    resolve_work_branch,
    run_cli,
)

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config.yaml"
STATE_PATH = BASE_DIR / "batcave" / "memory" / "release_state.json"
LOG_PATH = BASE_DIR / "release.log"

DEFAULT_WORKSPACE_DIR = "~/projects/.release-workspace"
DEFAULT_MAX_COMMITS_LISTED = 50
CLONE_TIMEOUT = 300
GIT_TIMEOUT = 120
PR_MARKER = "<!-- spoiler-release -->"
FIELD_SEP, RECORD_SEP = "\x1f", "\x1e"

SECURITY, FEATURES, FIXES, OTHER = "security", "features", "fixes", "other"
GROUP_TITLES = {
    SECURITY: "🛡️ Parches de seguridad (radar)",
    FEATURES: "✨ Features",
    FIXES: "🐛 Fixes",
    OTHER: "🧹 Otros",
}
RADAR_COMMIT_RE = re.compile(r"^fix\(deps\):.*\(radar\)\s*$")
FEAT_RE = re.compile(r"^feat(?:\([^)]*\))?!?:", re.IGNORECASE)
FIX_RE = re.compile(r"^fix(?:\([^)]*\))?!?:", re.IGNORECASE)
APPLIED_COMMENT_RE = re.compile(r"Aplicado en `?[^`:\s]+`?:\s*([0-9a-f]{7,40})")
CI_ICONS = {"success": "✅", "failure": "❌", "pending": "⏳", "none": "➖", "unknown": "❔"}

log = logging.getLogger("release")


def setup_logging(quiet_stdout: bool) -> None:
    file_handler = logging.FileHandler(LOG_PATH)
    stream_handler = logging.StreamHandler(sys.stderr if quiet_stdout else sys.stdout)
    stream_handler.setLevel(logging.WARNING if quiet_stdout else logging.INFO)
    for handler in (file_handler, stream_handler):
        handler.addFilter(MaskingFilter())
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[file_handler, stream_handler])
    for noisy in ("httpx", "httpx2", "httpcore", "urllib3", "github"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# --------------------------------------------------------------------------
# Modelo
# --------------------------------------------------------------------------


@dataclass
class Commit:
    sha: str
    subject: str
    body: str

    @property
    def group(self) -> str:
        if RADAR_COMMIT_RE.match(self.subject) or "Resuelto por: deterministic-patch" in self.body:
            return SECURITY
        if FEAT_RE.match(self.subject):
            return FEATURES
        if FIX_RE.match(self.subject):
            return FIXES
        return OTHER


@dataclass
class RepoRelease:
    repo: str
    full_name: str
    work: str
    base: str = ""
    base_sha: str = ""
    head_sha: str = ""
    behind: int = 0
    synced: bool = False          # se mergeo (y pusheo) la base en la rama de trabajo
    conflict: list[str] = field(default_factory=list)
    conflict_key: str = ""
    commits: list[Commit] = field(default_factory=list)
    all_shas: set[str] = field(default_factory=set)
    closes: list[int] = field(default_factory=list)
    ci: str = "unknown"
    ci_detail: str = ""
    pr_number: Optional[int] = None
    pr_url: str = ""
    pr_action: str = ""           # created / updated / unchanged / would-create / would-update
    body: str = ""
    error: str = ""

    @property
    def security_count(self) -> int:
        return sum(1 for c in self.commits if c.group == SECURITY)


# --------------------------------------------------------------------------
# Configuracion y workspace
# --------------------------------------------------------------------------


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def release_config(config: dict) -> dict:
    return config.get("release", {}) or {}


def get_workspace_dir(config: dict) -> Path:
    """Workspace del release manager. Se niega a usar una ruta que sea (o
    contenga, o este dentro de) un directorio de produccion."""
    workspace = Path(release_config(config).get("workspace_dir", DEFAULT_WORKSPACE_DIR)).expanduser().resolve()
    for prod in production_paths(config):
        if workspace == prod or prod.is_relative_to(workspace) or workspace.is_relative_to(prod):
            raise RuntimeError(f"release.workspace_dir ({workspace}) se cruza con produccion ({prod}); se aborta")
    return workspace


def release_targets(config: dict, specialists: dict[str, dict]) -> list[tuple[str, str]]:
    """(repo corto, rama de trabajo) de todos los perfiles, sin duplicados."""
    targets: dict[str, tuple[str, str]] = {}
    for profile in specialists.values():
        for repo in profile_repos(profile):
            targets.setdefault(repo.lower(), (repo, resolve_work_branch(config, repo, specialists)))
    return list(targets.values())


def git(path: Path, *args: str, timeout: int = GIT_TIMEOUT):
    return run_cli(["git", *args], cwd=path, timeout=timeout)


def rev(path: Path, ref: str) -> str:
    result = git(path, "rev-parse", "--verify", "--quiet", ref)
    return result.stdout.strip() if result.returncode == 0 else ""


def sync_workspace(full_name: str, workspace_dir: Path) -> Path:
    """Clone (o fetch --prune) de la copia aislada; devuelve su ruta."""
    path = workspace_dir / full_name.split("/")[-1]
    auth = github_auth_env()
    if not (path / ".git").exists():
        workspace_dir.mkdir(parents=True, exist_ok=True)
        clone = run_cli(git_cmd(["clone", build_clone_url(full_name), str(path)]), timeout=CLONE_TIMEOUT, extra_env=auth)
        if clone.returncode != 0:
            raise RuntimeError(f"no se pudo clonar: {mask_secrets(clone.stderr[-400:])}")
    else:
        fetch = run_cli(git_cmd(["fetch", "origin", "--prune"]), cwd=path, timeout=GIT_TIMEOUT, extra_env=auth)
        if fetch.returncode != 0:
            raise RuntimeError(f"fetch fallo: {mask_secrets(fetch.stderr[-400:])}")
    return path


def default_branch_from_git(path: Path) -> str:
    result = git(path, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    return result.stdout.strip().removeprefix("origin/") if result.returncode == 0 else ""


def checkout_work_branch(path: Path, work: str) -> None:
    """Deja la copia LOCAL identica a origin/<work> (nunca toca el remoto)."""
    if git(path, "checkout", "-f", "-B", work, f"origin/{work}").returncode != 0:
        raise RuntimeError(f"no se pudo hacer checkout de {work}")
    git(path, "reset", "--hard", f"origin/{work}")
    git(path, "clean", "-fdq")


def merge_base_into_work(path: Path, work: str, base: str) -> tuple[bool, list[str], str]:
    """`git merge --no-edit origin/<base>` sobre `work` (ya en checkout).
    Devuelve (ok, archivos en conflicto, error). Si falla se aborta el merge
    y la copia local vuelve a origin/<work>; nunca se pushea nada aqui."""
    current = git(path, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if current != work or current in PROTECTED_BRANCHES or current == base:
        return False, [], f"seguridad: HEAD en '{current}', se esperaba '{work}'"
    merge = git(path, "merge", "--no-edit", f"origin/{base}", timeout=300)
    if merge.returncode == 0:
        return True, [], ""
    conflicts = [
        line.strip() for line in git(path, "diff", "--name-only", "--diff-filter=U").stdout.splitlines() if line.strip()
    ]
    if git(path, "merge", "--abort").returncode != 0:
        git(path, "reset", "--hard", f"origin/{work}")
    error = "" if conflicts else mask_secrets((merge.stdout + merge.stderr).strip()[-400:])
    return False, conflicts, error


def commits_between(path: Path, base_ref: str, head_ref: str) -> list[Commit]:
    fmt = f"%H{FIELD_SEP}%s{FIELD_SEP}%b{RECORD_SEP}"
    out = git(path, "log", "--no-merges", f"--format={fmt}", f"{base_ref}..{head_ref}").stdout
    commits = []
    for record in out.split(RECORD_SEP):
        parts = record.strip("\n").split(FIELD_SEP)
        if len(parts) == 3 and parts[0]:
            commits.append(Commit(parts[0], parts[1].strip(), parts[2].strip()))
    return commits


# --------------------------------------------------------------------------
# GitHub: issues in-dev, CI y PR
# --------------------------------------------------------------------------


def in_dev_closes(gh_repo, commits: list[Commit], all_shas: set[str]) -> list[int]:
    """Issues abiertos con `in-dev` cuyo commit de arreglo esta en el PR: el
    SHA del comentario `Aplicado en <rama>: <sha>` (parches del radar) o un
    commit del PR con `Resuelve #N` en el mensaje (modo Desarrollo)."""
    closes = []
    for issue in gh_repo.get_issues(state="open", labels=[IN_DEV_LABEL]):
        if issue.pull_request is not None:
            continue
        ref_re = re.compile(rf"(?i)\bresuelve #{issue.number}\b")
        found = any(ref_re.search(f"{c.subject}\n{c.body}") for c in commits)
        if not found:
            for comment in issue.get_comments():
                for sha in APPLIED_COMMENT_RE.findall(comment.body or ""):
                    if any(full.startswith(sha) for full in all_shas):
                        found = True
                        break
                if found:
                    break
        if found:
            closes.append(issue.number)
    return sorted(closes)


def ci_status(gh_repo, sha: str) -> tuple[str, str]:
    """Estado combinado del commit (statuses + check runs): success / failure
    / pending / none, y un detalle `N ok, N fallidos, N en curso`."""
    commit = gh_repo.get_commit(sha)
    states = [s.state for s in commit.get_combined_status().statuses]
    for check in commit.get_check_runs():
        if check.status != "completed":
            states.append("pending")
        elif check.conclusion in ("success", "neutral", "skipped"):
            states.append("success")
        else:
            states.append("failure")
    if not states:
        return "none", "sin CI"
    failed = sum(1 for s in states if s in ("failure", "error"))
    pending = sum(1 for s in states if s == "pending")
    ok = len(states) - failed - pending
    overall = "failure" if failed else "pending" if pending else "success"
    return overall, f"{ok} ok, {failed} fallidos, {pending} en curso"


def pr_title(rel: RepoRelease) -> str:
    return f"chore(release): {rel.work} -> {rel.base}"


def build_pr_body(rel: RepoRelease, max_listed: int) -> str:
    lines = [
        PR_MARKER,
        f"## 🚀 Release `{rel.work}` → `{rel.base}`",
        "",
        f"Preparado por Spoiler (release manager). **{len(rel.commits)} commits** "
        f"({rel.security_count} parches de seguridad). Este PR nunca se mergea automaticamente.",
        "",
        f"**CI del HEAD** (`{rel.head_sha[:12]}`): {CI_ICONS.get(rel.ci, '❔')} {rel.ci} — {rel.ci_detail or 'sin datos'}",
    ]
    if rel.conflict:
        lines += ["", f"⚠️ **Conflicto al sincronizar `{rel.base}` en `{rel.work}`**: " + ", ".join(f"`{f}`" for f in rel.conflict)]
    for group in (SECURITY, FEATURES, FIXES, OTHER):
        items = [c for c in rel.commits if c.group == group]
        if not items:
            continue
        lines += ["", f"### {GROUP_TITLES[group]} ({len(items)})"]
        lines += [f"- {mask_secrets(c.subject)} ({c.sha[:7]})" for c in items[:max_listed]]
        if len(items) > max_listed:
            lines.append(f"- ... y {len(items) - max_listed} mas")
    if rel.closes:
        lines += ["", "### Issues"] + [f"Closes #{n}" for n in rel.closes]
    return "\n".join(lines) + "\n"


def upsert_pr(gh_repo, rel: RepoRelease, dry_run: bool) -> None:
    owner = rel.full_name.split("/")[0]
    open_prs = list(gh_repo.get_pulls(state="open", head=f"{owner}:{rel.work}", base=rel.base))
    if open_prs:
        pr = open_prs[0]
        rel.pr_number, rel.pr_url = pr.number, pr.html_url
        if (pr.body or "").strip() == rel.body.strip():
            rel.pr_action = "unchanged"
        elif dry_run:
            rel.pr_action = "would-update"
        else:
            pr.edit(body=rel.body)
            rel.pr_action = "updated"
        return
    if dry_run:
        rel.pr_action = "would-create"
        return
    pr = gh_repo.create_pull(base=rel.base, head=rel.work, title=pr_title(rel), body=rel.body)
    rel.pr_number, rel.pr_url, rel.pr_action = pr.number, pr.html_url, "created"


# --------------------------------------------------------------------------
# Flujo por repo
# --------------------------------------------------------------------------


def process_repo(config: dict, gh, rel: RepoRelease, workspace_dir: Path, dry_run: bool) -> None:
    if rel.work in PROTECTED_BRANCHES:
        raise RuntimeError(f"rama de trabajo '{rel.work}' prohibida")
    path = sync_workspace(rel.full_name, workspace_dir)
    gh_repo = gh.get_repo(rel.full_name) if gh else None
    rel.base = gh_repo.default_branch if gh_repo else default_branch_from_git(path)
    if not rel.base:
        raise RuntimeError("no se pudo determinar la rama base")
    if rel.base == rel.work:
        raise RuntimeError(f"la rama de trabajo '{rel.work}' es la rama base")
    if not rev(path, f"origin/{rel.work}"):
        raise RuntimeError(f"origin no tiene la rama de trabajo '{rel.work}'")
    rel.base_sha = rev(path, f"origin/{rel.base}")
    if not rel.base_sha:
        raise RuntimeError(f"origin no tiene la rama base '{rel.base}'")

    checkout_work_branch(path, rel.work)
    work_sha = rev(path, "HEAD")
    rel.behind = int(git(path, "rev-list", "--count", f"HEAD..origin/{rel.base}").stdout.strip() or 0)

    # 1. SYNC
    if rel.behind and not dry_run:
        ok, conflicts, error = merge_base_into_work(path, rel.work, rel.base)
        if ok:
            push = run_cli(git_cmd(["push", "origin", rel.work]), cwd=path, timeout=GIT_TIMEOUT, extra_env=github_auth_env())
            if push.returncode != 0:
                git(path, "reset", "--hard", f"origin/{rel.work}")
                raise RuntimeError(f"push del merge fallo: {mask_secrets(push.stderr[-400:])}")
            rel.synced = True
            log.info("%s: %d commits de %s mergeados en %s y pusheados", rel.repo, rel.behind, rel.base, rel.work)
        elif conflicts:
            rel.conflict = conflicts
            rel.conflict_key = f"{rel.base_sha}:{work_sha}"
            log.warning("%s: conflicto mergeando %s en %s (%s); merge abortado, nada pusheado",
                        rel.repo, rel.base, rel.work, ", ".join(conflicts))
        else:
            raise RuntimeError(f"merge fallo sin conflictos: {error}")

    # 2. PR
    rel.head_sha = rev(path, "HEAD")
    rel.commits = commits_between(path, f"origin/{rel.base}", "HEAD")
    if not rel.commits:
        return
    rel.all_shas = set(git(path, "rev-list", f"origin/{rel.base}..HEAD").stdout.split())
    if gh_repo is None:
        rel.pr_action = "sin GITHUB_TOKEN"
        rel.body = build_pr_body(rel, int(release_config(config).get("max_commits_listed", DEFAULT_MAX_COMMITS_LISTED)))
        return
    try:
        rel.closes = in_dev_closes(gh_repo, rel.commits, rel.all_shas)
    except Exception as exc:
        log.warning("%s: no se pudieron revisar los issues '%s': %s", rel.repo, IN_DEV_LABEL, exc)
    try:
        rel.ci, rel.ci_detail = ci_status(gh_repo, rel.head_sha)
    except Exception as exc:
        log.warning("%s: no se pudo leer el CI de %s: %s", rel.repo, rel.head_sha[:12], exc)
    rel.body = build_pr_body(rel, int(release_config(config).get("max_commits_listed", DEFAULT_MAX_COMMITS_LISTED)))
    upsert_pr(gh_repo, rel, dry_run)


# --------------------------------------------------------------------------
# Estado y notificaciones
# --------------------------------------------------------------------------


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"repos": {}, "ready_signature": []}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(STATE_PATH)


def ready_releases(results: list[RepoRelease]) -> list[RepoRelease]:
    return [r for r in results if r.pr_number and not r.conflict and not r.error]


def ready_signature(results: list[RepoRelease]) -> list[list]:
    return sorted([r.repo, r.pr_number, r.head_sha, len(r.commits), r.security_count] for r in ready_releases(results))


def format_ready(results: list[RepoRelease]) -> str:
    lines = ["🚀 <b>Spoiler — releases listos para revisar</b>"]
    for r in ready_releases(results):
        lines.append(
            f"• <a href=\"{html.escape(r.pr_url)}\">{html.escape(r.repo)} #{r.pr_number}</a> "
            f"<code>{html.escape(r.work)}</code> → <code>{html.escape(r.base)}</code>: "
            f"{len(r.commits)} commits, {r.security_count} parches de seguridad, CI {CI_ICONS.get(r.ci, '❔')}"
        )
    return "\n".join(lines)


def format_conflict(rel: RepoRelease) -> str:
    files = ", ".join(f"<code>{html.escape(f)}</code>" for f in rel.conflict[:10])
    more = f" (+{len(rel.conflict) - 10})" if len(rel.conflict) > 10 else ""
    return (
        f"⚠️ <b>Spoiler — conflicto en {html.escape(rel.repo)}</b>\n"
        f"Merge de <code>{html.escape(rel.base)}</code> en <code>{html.escape(rel.work)}</code> abortado, "
        f"no se pusheo nada.\nArchivos: {files}{more}"
    )


def render_text(results: list[RepoRelease], dry_run: bool) -> str:
    out = [f"Release manager de Spoiler ({'dry-run' if dry_run else 'real'})"]
    for r in results:
        out.append(f"\n== {r.repo} ({r.work} -> {r.base or '?'})")
        if r.error:
            out.append(f"  ERROR: {r.error}")
            continue
        if r.behind:
            if r.synced:
                state = "mergeado y pusheado"
            elif r.conflict:
                state = "CONFLICTO: " + ", ".join(r.conflict)
            else:
                state = "se mergearia (dry-run)"
            out.append(f"  sync: {r.base} tiene {r.behind} commits que no estan en {r.work} -> {state}")
        else:
            out.append(f"  sync: {r.work} al dia con {r.base}")
        if not r.commits:
            out.append(f"  PR: {r.work} no tiene commits nuevos respecto a {r.base}, no hace falta PR")
            continue
        pr = f"#{r.pr_number} {r.pr_url}" if r.pr_number else "(nuevo)"
        out.append(f"  PR {pr}: {r.pr_action}; {len(r.commits)} commits, {r.security_count} de seguridad, "
                   f"CI {r.ci} ({r.ci_detail or 'sin datos'}), closes {r.closes or '-'}")
        if dry_run and r.pr_action in ("would-create", "would-update", "sin GITHUB_TOKEN"):
            out.append("  --- cuerpo ---")
            out.extend("  " + line for line in r.body.splitlines())
    return "\n".join(out)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


async def run(dry_run: bool) -> int:
    load_env()
    config = load_config()
    specialists = load_specialists(config)
    workspace_dir = get_workspace_dir(config)
    try:
        gh = get_github_client()
    except Exception as exc:
        if not dry_run:
            log.error("Sin cliente de GitHub no se pueden gestionar PRs: %s", exc)
            return 2
        log.warning("Sin cliente de GitHub (%s): dry-run solo con git", exc)
        gh = None

    results: list[RepoRelease] = []
    for repo, work in release_targets(config, specialists):
        rel = RepoRelease(repo=repo, full_name=repo_full_name(config, repo), work=work)
        try:
            process_repo(config, gh, rel, workspace_dir, dry_run)
        except Exception as exc:
            rel.error = mask_secrets(str(exc))
            log.error("%s: %s", repo, rel.error)
        results.append(rel)

    if dry_run:
        print(render_text(results, dry_run=True))
        return 0

    state = load_state()
    notifier = Notifier()
    now = datetime.now().isoformat(timespec="seconds")
    for rel in results:
        previous = state.setdefault("repos", {}).get(rel.repo, {})
        if rel.conflict and previous.get("conflict") != rel.conflict_key:
            await notifier.send(format_conflict(rel))
        state["repos"][rel.repo] = {
            "work": rel.work, "base": rel.base, "head": rel.head_sha, "pr": rel.pr_number, "url": rel.pr_url,
            "commits": len(rel.commits), "security": rel.security_count, "closes": rel.closes,
            "conflict": rel.conflict_key or None, "conflict_files": rel.conflict, "error": rel.error or None,
            "updated": now,
        }
    signature = ready_signature(results)
    if signature and signature != state.get("ready_signature"):
        await notifier.send(format_ready(results))
        state["last_notified"] = now
    state["ready_signature"] = signature
    state["last_run"] = now
    save_state(state)
    print(render_text(results, dry_run=False))
    return 1 if any(r.error for r in results) else 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Release manager de Spoiler: sync rama de trabajo <- base y PRs")
    parser.add_argument("--dry-run", action="store_true", help="solo el plan: sin merge, push, PRs, Telegram ni estado")
    args = parser.parse_args()
    setup_logging(quiet_stdout=args.dry_run)
    sys.exit(asyncio.run(run(args.dry_run)))


if __name__ == "__main__":
    main()
