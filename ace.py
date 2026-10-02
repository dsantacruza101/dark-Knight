#!/usr/bin/env python3
"""Ace — can fiel de Batman. Verifica la Batifamilia y reporta a Alfred."""
import html
import json
import os
import subprocess
import urllib.request

SERVICES = {
    "Alfred": "telegram-bot",
    "Batman": "night-agent.timer",
    "Lucius": "lucius-fox.timer",
    "Signal": "signal.timer",
}


def run(cmd: list[str]) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


def line(ok: bool, text: str) -> str:
    return f"{'✅' if ok else '❌'} {html.escape(text)}"


def main() -> None:
    lines = []
    for name, unit in SERVICES.items():
        r = run(["systemctl", "is-active", unit])
        state = r.stdout.strip() if r else "sin respuesta"
        lines.append(line(state == "active", f"{name} — {state}"))

    r = run(["docker", "ps", "--format", "{{.Names}}"])
    count = len(r.stdout.split()) if r and r.returncode == 0 else 0
    lines.append(line(count > 0, f"{count} containers corriendo"))

    r = run(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "localhost:11434"])
    ollama_ok = bool(r) and r.stdout == "200"
    lines.append(line(ollama_ok, "Ollama — " + ("ok" if ollama_ok else "sin respuesta")))

    r = run(["claude", "--version"])
    claude_ok = bool(r) and r.returncode == 0
    lines.append(line(claude_ok, "Claude — " + ("ok" if claude_ok else "no disponible")))

    send("🐕 <b>Ace reporta:</b>\n" + "\n".join(lines))


def send(text: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print(text)
        return
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "HTML"}).encode(),
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(req, timeout=15).read()


if __name__ == "__main__":
    main()
