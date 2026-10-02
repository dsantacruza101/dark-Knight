import os
import subprocess
import json
import urllib.request
from datetime import datetime

TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

def send_telegram_message(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {
        'chat_id': TELEGRAM_CHAT_ID,
        'text': message,
        'parse_mode': 'HTML'
    }
    response = urllib.request.urlopen(url, json.dumps(data).encode('utf-8'))
    response.read()

def check_service_status(service_name):
    try:
        result = subprocess.run(['systemctl', 'is-active', service_name], capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            return '✅ ' + service_name + ' — activo'
        else:
            return '❌ ' + service_name + ' — inactivo'
    except subprocess.TimeoutExpired:
        return '❌ ' + service_name + ' — timeout'

def main():
    services = {
        'Alfred': 'telegram-bot',
        'Batman': 'night-agent.timer',
        'Lucius Fox': 'lucius-fox.timer',
        'Signal': 'signal.timer',
        'Containers': 'docker ps --format "{{.Names}}"',
        'Ollama': 'curl -s localhost:11434',
        'Claude': 'claude --version'
    }

    status_messages = []
    for service_name, command in services.items():
        status = check_service_status(service_name)
        status_messages.append(status)

    container_count = len(subprocess.run(['docker', 'ps', '--format', '{{.Names}}'], capture_output=True, text=True).stdout.splitlines())

    ollama_status = 'ok' if subprocess.run(['curl', '-s', 'localhost:11434'], capture_output=True, text=True).returncode == 0 else '❌'
    claude_status = 'ok' if subprocess.run(['claude', '--version'], capture_output=True, text=True).returncode == 0 else '❌'

    message = f"🐕 Ace reporta:\n" + "\n".join(status_messages) + f"\n✅ {container_count} containers corriendo\n✅ Ollama — {ollama_status}\n✅ Claude — {claude_status}"

    send_telegram_message(message)

if __name__ == '__main__':
    main()
