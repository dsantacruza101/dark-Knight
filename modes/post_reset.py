"""Modo Post-Reset: corre el lunes, justo despues del reset semanal de tokens
(domingo a las hora de `schedule.weekly_reset` en config.yaml). Los tokens de
Claude estan frescos, asi que este modo lo usa intensivamente para auditar la
semana que dejo el modo Desarrollo (que solo usa Ollama):

  1. Por cada repo de config.yaml, se listan los commits de night-agent de los
     ultimos 7 dias en la rama de trabajo (`github.work_branch`):
     `git log --author='night-agent' --since='7 days ago'`.
  2. Cada commit se pasa a `claude -p` para un code review de solo lectura
     (`git show <hash>` como diff de entrada).
  3. Si el review encuentra algo accionable, se le pide a claude que lo
     implemente (--permission-mode acceptEdits) y se commitea/pushea a dev.
  4. Se comparan los pares Claude/Ollama guardados en knowledge/ durante la
     semana: claude puntua cada respuesta de Ollama contra la de Claude
     (0-1) y se promedia, ademas de sintetizar mejoras y vacios de ejemplos.
  5. Se genera reports/weekly-YYYY-MM-DD.md con el reporte ejecutivo completo.
  6. El reporte completo se envia a Alfred como documento y un resumen
     ejecutivo como mensaje de texto.

Respeta las mismas restricciones de seguridad que el modo Desarrollo: nunca se
toca main/master y nunca se dejan cambios en .env/docker-compose/nginx/
fail2ban/cloudflared/ssh, aunque vengan sugeridos por claude
(ver `modes.development.is_forbidden_path`).
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from telegram.error import TelegramError

from modes.development import (
    commit_and_push,
    ensure_dev_branch,
    is_forbidden_path,
    load_env,
    repo_is_clean,
    repo_local_path,
    run_cli,
)

BASE_DIR = Path(__file__).resolve().parent.parent
KNOWLEDGE_DIR = BASE_DIR / "knowledge"
REPORTS_DIR = BASE_DIR / "reports"

SINCE = "7 days ago"
NIGHT_AGENT_AUTHOR = "night-agent"
MAX_DIFF_CHARS = 8000
CLAUDE_TIMEOUT = 900
CLAUDE_FIX_TIMEOUT = 3600

SCORE_RE = re.compile(r"SCORE:\s*([01](?:\.\d+)?)", re.IGNORECASE)
NOTES_RE = re.compile(r"NOTES:\s*(.+)", re.IGNORECASE | re.DOTALL)

log = logging.getLogger("night_agent.post_reset")


def run_claude(
    claude_bin: str,
    prompt: str,
    cwd: Optional[Path] = None,
    accept_edits: bool = False,
    timeout: int = CLAUDE_TIMEOUT,
) -> Optional[str]:
    cmd = [claude_bin, "-p", prompt]
    if accept_edits:
        cmd += ["--permission-mode", "acceptEdits"]
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.error("Fallo ejecutando claude: %s", exc)
        return None
    if proc.returncode != 0:
        log.error("claude devolvio codigo %s: %s", proc.returncode, proc.stderr[-500:])
        return None
    return proc.stdout.strip() or None


def get_week_commits(repo_path: Path) -> list[dict]:
    """Commits de night-agent en los ultimos 7 dias, en orden cronologico."""
    log_out = run_cli(
        ["git", "log", f"--author={NIGHT_AGENT_AUTHOR}", f"--since={SINCE}", "--reverse", "--pretty=format:%H%x1f%s"],
        cwd=repo_path,
    )
    if log_out.returncode != 0 or not log_out.stdout.strip():
        return []
    commits = []
    for line in log_out.stdout.strip().splitlines():
        commit_hash, _, subject = line.partition("\x1f")
        if commit_hash.strip():
            commits.append({"hash": commit_hash.strip(), "subject": subject.strip()})
    return commits


def get_commit_diff(repo_path: Path, commit_hash: str) -> str:
    proc = run_cli(["git", "show", commit_hash], cwd=repo_path, timeout=60)
    diff = proc.stdout
    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS] + "\n... (diff truncado)"
    return diff


def get_commit_line_stats(repo_path: Path, commit_hash: str) -> tuple[int, int]:
    proc = run_cli(["git", "show", "--shortstat", "--format=", commit_hash], cwd=repo_path, timeout=60)
    ins_match = re.search(r"(\d+) insertion", proc.stdout)
    del_match = re.search(r"(\d+) deletion", proc.stdout)
    return (int(ins_match.group(1)) if ins_match else 0, int(del_match.group(1)) if del_match else 0)


def _revert_forbidden_changes(repo_path: Path, status_lines: list[str]) -> list[str]:
    reverted = []
    for line in status_lines:
        path = line[3:].strip()
        if path and is_forbidden_path(path):
            run_cli(["git", "checkout", "--", path], cwd=repo_path)
            reverted.append(path)
    return reverted


def review_and_fix_commit(claude_bin: str, branch: str, repo_full_name: str, repo_path: Path, commit: dict) -> dict:
    tag = f"{repo_full_name}@{commit['hash'][:7]}"
    log.info("👮 Commissioner Gordon revisando... %s", tag)
    diff = get_commit_diff(repo_path, commit["hash"])
    review = run_claude(
        claude_bin,
        "Haz code review de este diff:\n\n" + diff +
        "\n\nIdentifica bugs, mejoras y code smells. Se especifico y conciso.",
    )
    base = {**commit, "repo": repo_full_name, "review": review, "applied": False, "files_changed": []}
    if not review:
        log.warning("%s: claude no genero code review", tag)
        return base

    if not repo_is_clean(repo_path):
        log.warning("%s: el repo tiene cambios sin commitear, se omite la implementacion de correcciones", tag)
        return base

    fix_prompt = (
        f"Este es un code review de un commit reciente en este repositorio "
        f"(commit {commit['hash']}, rama {branch}):\n\n{review}\n\n"
        "Si el review identifica bugs o mejoras accionables, implementalas "
        "directamente en el codigo. Si el review no encontro nada que corregir, "
        "no modifiques ningun archivo."
    )
    run_claude(claude_bin, fix_prompt, cwd=repo_path, accept_edits=True, timeout=CLAUDE_FIX_TIMEOUT)

    status = run_cli(["git", "status", "--porcelain"], cwd=repo_path)
    status_lines = [line for line in status.stdout.splitlines() if line.strip()]
    reverted = _revert_forbidden_changes(repo_path, status_lines)
    if reverted:
        log.warning("%s: se revirtieron archivos protegidos: %s", tag, ", ".join(reverted))

    status = run_cli(["git", "status", "--porcelain"], cwd=repo_path)
    if not status.stdout.strip():
        return base

    files_changed = [line[3:].strip() for line in status.stdout.splitlines() if line.strip()]
    commit_and_push(repo_path, branch, f"fix(night-agent): correcciones de code review para {commit['hash'][:7]}")
    return {**base, "applied": True, "files_changed": files_changed}


def get_week_knowledge_pairs(since_days: int = 7) -> list[dict]:
    if not KNOWLEDGE_DIR.exists():
        return []
    cutoff = datetime.now() - timedelta(days=since_days)
    pairs = []
    for path in sorted(KNOWLEDGE_DIR.glob("pair_*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("No se pudo leer %s: %s", path, exc)
            continue
        try:
            timestamp = datetime.fromisoformat(data["timestamp"])
        except (KeyError, ValueError):
            continue
        if timestamp >= cutoff and data.get("claude_output") and data.get("ollama_output"):
            pairs.append(data)
    return pairs


def judge_ollama_output(claude_bin: str, pair: dict) -> Optional[tuple[float, str]]:
    prompt = (
        "Compara estas dos respuestas al mismo analisis de logs de Nginx. "
        "La primera es de Claude (referencia de calidad alta), la segunda de "
        "un modelo local Ollama que estamos entrenando con ejemplos de Claude.\n\n"
        f"## Respuesta de Claude\n{pair['claude_output'][:3000]}\n\n"
        f"## Respuesta de Ollama\n{pair['ollama_output'][:3000]}\n\n"
        "Responde EXACTAMENTE con este formato:\n"
        "SCORE: <numero entre 0 y 1, con hasta 2 decimales>\n"
        "NOTES: <una o dos frases: que hizo bien Ollama y que le falta>"
    )
    output = run_claude(claude_bin, prompt)
    if not output:
        return None
    score_match = SCORE_RE.search(output)
    if not score_match:
        return None
    score = max(0.0, min(1.0, float(score_match.group(1))))
    notes_match = NOTES_RE.search(output)
    return score, (notes_match.group(1).strip() if notes_match else "")


def evaluate_ollama_quality(claude_bin: str, pairs: list[dict]) -> dict:
    scores: list[float] = []
    notes: list[str] = []
    for pair in pairs:
        result = judge_ollama_output(claude_bin, pair)
        if result:
            score, note = result
            scores.append(score)
            if note:
                notes.append(note)

    avg_score = sum(scores) / len(scores) if scores else 0.0
    synthesis = "Sin suficientes datos para generar un resumen."
    if notes:
        synthesis_prompt = (
            "Estas son observaciones semanales comparando las respuestas de un "
            "modelo Ollama contra las de Claude en la misma tarea (analisis de "
            "logs de Nginx):\n\n" + "\n".join(f"- {note}" for note in notes) +
            "\n\nResume en dos listas cortas en markdown: 'Mejoras de Ollama esta "
            "semana' y 'Areas que necesitan mas ejemplos de entrenamiento'."
        )
        synthesis = run_claude(claude_bin, synthesis_prompt) or synthesis

    return {
        "avg_score": avg_score,
        "evaluated": len(scores),
        "total_pairs": len(pairs),
        "synthesis": synthesis,
    }


def get_week_ollama_completions(since_days: int = 7) -> list[str]:
    cutoff = datetime.now() - timedelta(days=since_days)
    completions: list[str] = []
    for path in sorted(REPORTS_DIR.glob("development-*.md")):
        try:
            report_date = datetime.strptime(path.stem.removeprefix("development-"), "%Y-%m-%d")
        except ValueError:
            continue
        if report_date < cutoff:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        completions.extend(line.strip() for line in text.splitlines() if line.strip().startswith("- ✅"))
    return completions


def build_recommendations(claude_bin: str, review_results: list[dict], quality: dict, completions: list[str]) -> str:
    bug_count = sum(1 for r in review_results if r["applied"])
    prompt = (
        "Eres un tech lead resumiendo la semana de un agente autonomo que "
        "programa de noche. Datos de la semana:\n"
        f"- Issues completados por Ollama: {len(completions)}\n"
        f"- Commits de night-agent revisados: {len(review_results)}\n"
        f"- Bugs/mejoras corregidos automaticamente por Claude: {bug_count}\n"
        f"- Score promedio de calidad de Ollama vs Claude: {quality['avg_score']:.2f}\n"
        f"- Observaciones de calidad:\n{quality['synthesis']}\n\n"
        "Da 3-5 recomendaciones concretas y accionables para la semana "
        "siguiente, en una lista markdown."
    )
    return run_claude(claude_bin, prompt) or "- Sin recomendaciones generadas."


def write_weekly_report(
    started_at: datetime,
    review_results: list[dict],
    quality: dict,
    completions: list[str],
    recommendations: str,
    total_insertions: int,
    total_deletions: int,
) -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    date_str = started_at.strftime("%Y-%m-%d")
    report_path = REPORTS_DIR / f"weekly-{date_str}.md"

    lines = [f"👮 Commissioner Gordon — Reporte Semanal - {date_str}", "", "## Issues completados por Ollama"]
    lines.extend(completions if completions else ["- Ninguno registrado esta semana."])

    lines += [
        "",
        "## Lineas de codigo generadas",
        f"- +{total_insertions} / -{total_deletions} en {len(review_results)} commit(s) de night-agent",
        "",
        "## Bugs encontrados y corregidos por Claude",
    ]
    if review_results:
        for result in review_results:
            tag = f"{result['repo']}@{result['hash'][:7]}"
            estado = "✅ corregido" if result["applied"] else "ℹ️ sin cambios necesarios"
            lines.append(f"### {tag} - {result['subject']} ({estado})")
            lines.append(result["review"] or "_Claude no genero code review para este commit._")
            if result["applied"]:
                lines.append(f"\nArchivos modificados: {', '.join(result['files_changed'])}")
            lines.append("")
    else:
        lines += ["- No hubo commits de night-agent esta semana.", ""]

    lines += [
        "## Score de Nightwing esta semana:",
        f"- Score promedio: {quality['avg_score']:.2f} (0-1)",
        f"- Pares evaluados: {quality['evaluated']}/{quality['total_pairs']}",
        "",
        quality["synthesis"],
        "",
        "## Recomendaciones para la semana siguiente",
        recommendations,
    ]

    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_path


async def send_weekly_report(notifier, report_path: Path) -> None:
    try:
        with report_path.open("rb") as fh:
            await notifier.bot.send_document(chat_id=notifier.chat_id, document=fh, filename=report_path.name)
    except TelegramError as exc:
        log.error("No se pudo enviar el reporte semanal como documento: %s", exc)


async def run_post_reset_mode(config: dict, notifier) -> str:
    """Punto de entrada del modo Post-Reset: audita la semana usando Claude a fondo."""
    load_env()
    started_at = datetime.now()
    branch = config["github"]["work_branch"]
    claude_bin = config["models"]["claude"]

    await notifier.send(
        "👮 *Commissioner Gordon revisando...*\nCommits de night-agent de la ultima semana, "
        "comparando Ollama vs Claude. Tokens frescos, Claude a fondo."
    )

    review_results: list[dict] = []
    total_insertions, total_deletions = 0, 0

    for repo_full_name in config["github"]["repos"]:
        repo_path = repo_local_path(repo_full_name)
        if not repo_path.exists():
            log.warning("Repo no encontrado en %s, se omite", repo_path)
            continue
        if not ensure_dev_branch(repo_path, branch):
            log.warning("No se pudo posicionar %s en '%s', se omite", repo_full_name, branch)
            continue

        commits = get_week_commits(repo_path)
        log.info("%s: %d commit(s) de night-agent en los ultimos 7 dias", repo_full_name, len(commits))
        for commit in commits:
            ins, dele = get_commit_line_stats(repo_path, commit["hash"])
            total_insertions += ins
            total_deletions += dele
            result = review_and_fix_commit(claude_bin, branch, repo_full_name, repo_path, commit)
            review_results.append(result)

    pairs = get_week_knowledge_pairs()
    quality = evaluate_ollama_quality(claude_bin, pairs)
    completions = get_week_ollama_completions()
    recommendations = build_recommendations(claude_bin, review_results, quality, completions)

    report_path = write_weekly_report(
        started_at, review_results, quality, completions, recommendations, total_insertions, total_deletions
    )
    await send_weekly_report(notifier, report_path)

    fixed = sum(1 for r in review_results if r["applied"])
    return (
        "📊 *Resumen ejecutivo Post-Reset*\n"
        f"- Commits revisados: {len(review_results)}\n"
        f"- Correcciones aplicadas por Claude: {fixed}\n"
        f"- Lineas generadas esta semana: +{total_insertions}/-{total_deletions}\n"
        f"- Issues completados por Ollama: {len(completions)}\n"
        f"- Score promedio de calidad Ollama: {quality['avg_score']:.2f} "
        f"({quality['evaluated']}/{quality['total_pairs']} pares evaluados)\n"
        f"- Reporte completo enviado: {report_path.name}"
    )
