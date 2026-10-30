"""Batwoman: cambios FIM en rutas criticas explicados por un sudo interactivo
del admin (eventos sinteticos de Wazuh).

Correr: venv/bin/python -m pytest tests/ -q  (o venv/bin/python -m unittest discover tests)
"""

import asyncio
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import batwoman  # noqa: E402

CONFIG = {
    **batwoman.DEFAULTS,
    "critical_paths": [
        "/etc/sudoers.d",
        "/etc/ssh",
        "/home/dsantacruz/.ssh/authorized_keys",
        "/etc/night-agent.env",
        "/etc/systemd/system",
    ],
}
PWD = "/home/dsantacruz/projects/night-agent"
# Fijo en el pasado: ninguna alerta de estos tests se aplaza por "reciente".
BASE = datetime(2026, 10, 30, 3, 1, 36, tzinfo=timezone.utc)


def wazuh_ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "+0000"


def sudo_alert(at: datetime, command: str, user: str = "dsantacruz", tty: str = "/dev/pts/0",
               alert_id: str = "1.1", logged_at: datetime | None = None) -> dict:
    """Alerta 5402 como la escribe Wazuh. `logged_at` = hora del syslog (la
    real del comando) si difiere de la hora de la alerta."""
    syslog = (logged_at or at).astimezone()  # el syslog va en hora local del host
    data = {"srcuser": user, "dstuser": "root", "pwd": PWD, "command": command}
    tty_part = ""
    if tty:
        data["tty"] = tty
        tty_part = f"TTY={tty} ; "
    return {
        "timestamp": wazuh_ts(at),
        "id": alert_id,
        "rule": {"id": "5402", "level": 3, "description": "Successful sudo to ROOT executed.", "groups": ["syslog", "sudo"]},
        "decoder": {"parent": "sudo", "name": "sudo"},
        "predecoder": {"program_name": "sudo", "timestamp": syslog.strftime("%b %d %H:%M:%S")},
        "full_log": f"{syslog.strftime('%b %d %H:%M:%S')} host sudo[1]: {user} : {tty_part}PWD={PWD} ; USER=root ; COMMAND={command}",
        "data": data,
    }


def fim_alert(at: datetime, path: str, event: str = "added") -> dict:
    return {
        "timestamp": wazuh_ts(at),
        "id": f"fim-{path}",
        "rule": {"id": "554", "level": 5, "description": "File added to the system.", "groups": ["ossec", "syscheck"]},
        "syscheck": {"path": path, "event": event, "mode": "realtime"},
        "full_log": f"File '{path}' {event}\nMode: realtime\n",
        "location": "syscheck",
    }


def buffer_of(*alerts: dict) -> list[dict]:
    return [ev for ev in map(batwoman.sudo_event, alerts) if ev]


def explain(fim: dict, *sudos: dict):
    return batwoman.find_sudo_explanation(batwoman.alert_fields(fim), buffer_of(*sudos), CONFIG)


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)


class FindSudoExplanationTest(unittest.TestCase):
    def test_cp_to_systemd_dir_explains_installed_unit(self) -> None:
        sudo = sudo_alert(BASE, "/usr/bin/cp spoiler-release.service spoiler-release.timer /etc/systemd/system/")
        fim = fim_alert(BASE + timedelta(seconds=1), "/etc/systemd/system/spoiler-release.timer")
        self.assertIsNotNone(explain(fim, sudo))

    def test_systemctl_enable_explains_wants_symlink(self) -> None:
        sudo = sudo_alert(BASE, "/usr/bin/systemctl enable --now spoiler-release.timer")
        fim = fim_alert(BASE + timedelta(seconds=1), "/etc/systemd/system/timers.target.wants/spoiler-release.timer")
        self.assertIsNotNone(explain(fim, sudo))

    def test_systemctl_disable_explains_deleted_symlink(self) -> None:
        sudo = sudo_alert(BASE, "/usr/bin/systemctl disable --now red-hood")
        fim = fim_alert(BASE + timedelta(seconds=1), "/etc/systemd/system/multi-user.target.wants/red-hood.service", "deleted")
        self.assertIsNotNone(explain(fim, sudo))

    def test_tee_and_sed_in_place_explain_destination(self) -> None:
        path = "/etc/systemd/system/batwoman.service"
        later = BASE + timedelta(seconds=5)
        self.assertIsNotNone(explain(fim_alert(later, path), sudo_alert(BASE, f"/usr/bin/tee {path}")))
        self.assertIsNotNone(explain(fim_alert(later, path), sudo_alert(BASE, f"/usr/bin/sed -i s/a/b/ {path}")))
        # sed sin -i solo lee el archivo.
        self.assertIsNone(explain(fim_alert(later, path), sudo_alert(BASE, f"/usr/bin/sed -n p {path}")))

    def test_cp_explains_only_destination_not_source(self) -> None:
        sudo = sudo_alert(BASE, "/usr/bin/cp /etc/systemd/system/batwoman.service /tmp/")
        fim = fim_alert(BASE + timedelta(seconds=1), "/etc/systemd/system/batwoman.service", "modified")
        self.assertIsNone(explain(fim, sudo))

    def test_cp_to_dir_does_not_explain_other_files_in_dir(self) -> None:
        sudo = sudo_alert(BASE, "/usr/bin/cp night-agent.service /etc/systemd/system/")
        fim = fim_alert(BASE + timedelta(seconds=1), "/etc/systemd/system/evil.service")
        self.assertIsNone(explain(fim, sudo))

    def test_not_explained_by_unrelated_command(self) -> None:
        sudo = sudo_alert(BASE, "/usr/bin/systemctl daemon-reload")
        fim = fim_alert(BASE + timedelta(seconds=1), "/etc/systemd/system/backdoor.service")
        self.assertIsNone(explain(fim, sudo))

    def test_wrong_user_is_not_explained(self) -> None:
        sudo = sudo_alert(BASE, "/usr/bin/cp x.service /etc/systemd/system/", user="intruso")
        fim = fim_alert(BASE + timedelta(seconds=1), "/etc/systemd/system/x.service")
        self.assertIsNone(explain(fim, sudo))

    def test_non_interactive_sudo_is_not_explained(self) -> None:
        # Sin TTY (agentes via sudoers.d, cron, un proceso comprometido).
        sudo = sudo_alert(BASE, "/usr/bin/cp x.service /etc/systemd/system/", tty="")
        fim = fim_alert(BASE + timedelta(seconds=1), "/etc/systemd/system/x.service")
        self.assertIsNone(explain(fim, sudo))

    def test_too_old_sudo_is_not_explained(self) -> None:
        sudo = sudo_alert(BASE, "/usr/bin/cp x.service /etc/systemd/system/")
        fim = fim_alert(BASE + timedelta(seconds=121), "/etc/systemd/system/x.service")
        self.assertIsNone(explain(fim, sudo))
        self.assertIsNotNone(explain(fim_alert(BASE + timedelta(seconds=119), "/etc/systemd/system/x.service"), sudo))

    def test_sudo_after_the_change_is_not_explained(self) -> None:
        sudo = sudo_alert(BASE + timedelta(seconds=30), "/usr/bin/cp x.service /etc/systemd/system/")
        fim = fim_alert(BASE, "/etc/systemd/system/x.service")
        self.assertIsNone(explain(fim, sudo))

    def test_syslog_time_wins_over_late_alert(self) -> None:
        # Caso real: journald entrega el sudo ~2s tarde, su alerta queda
        # DESPUES de la del FIM; la hora del syslog es la del comando.
        sudo = sudo_alert(BASE + timedelta(seconds=2), "/usr/bin/systemctl enable --now spoiler-release.timer",
                          logged_at=BASE)
        fim = fim_alert(BASE + timedelta(seconds=1), "/etc/systemd/system/timers.target.wants/spoiler-release.timer")
        self.assertIsNotNone(explain(fim, sudo))

    def test_sudoers_always_alerts(self) -> None:
        sudo = sudo_alert(BASE, "/usr/bin/cp batifamilia /etc/sudoers.d/")
        fim = fim_alert(BASE + timedelta(seconds=1), "/etc/sudoers.d/batifamilia")
        self.assertIsNone(explain(fim, sudo))

    def test_ssh_authorized_keys_and_env_always_alert(self) -> None:
        cases = [
            ("/usr/bin/tee -a /home/dsantacruz/.ssh/authorized_keys", "/home/dsantacruz/.ssh/authorized_keys"),
            ("/usr/bin/sed -i s/no/yes/ /etc/ssh/sshd_config", "/etc/ssh/sshd_config"),
            ("/usr/bin/tee /etc/night-agent.env", "/etc/night-agent.env"),
        ]
        for command, path in cases:
            with self.subTest(path=path):
                fim = fim_alert(BASE + timedelta(seconds=1), path, "modified")
                self.assertIsNone(explain(fim, sudo_alert(BASE, command)))


class SudoBufferTest(unittest.TestCase):
    def test_buffer_keeps_ten_minutes_and_skips_non_sudo(self) -> None:
        old = sudo_alert(BASE - timedelta(minutes=11), "/usr/bin/true", alert_id="old")
        recent = sudo_alert(BASE - timedelta(minutes=5), "/usr/bin/true", alert_id="recent")
        fim = fim_alert(BASE, "/etc/systemd/system/x.service")
        buffer = batwoman.update_sudo_buffer([], [old, recent, fim, recent], CONFIG, now=BASE)
        self.assertEqual([ev["id"] for ev in buffer], ["recent"])
        self.assertEqual(buffer[0]["tty"], "/dev/pts/0")
        self.assertEqual(buffer[0]["pwd"], PWD)


class ProcessAlertTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for name, value in (
            ("ALERT_STATE_PATH", Path(tmp.name) / "alert_state.json"),
            ("COMMS_PATH", Path(tmp.name) / "comms.json"),
        ):
            patcher = mock.patch.object(batwoman, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(batwoman, "update_memory")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.state = {"digest": batwoman.empty_digest()}
        self.notifier = FakeNotifier()

    def process(self, alert: dict, buffer: list[dict], allow_defer: bool = True) -> str:
        return asyncio.run(
            batwoman.process_alert(alert, self.state, CONFIG, [], None, self.notifier, buffer, allow_defer)
        )

    def test_explained_change_goes_to_digest_without_alert(self) -> None:
        buffer = buffer_of(sudo_alert(BASE, "/usr/bin/cp night-agent.service /etc/systemd/system/"))
        result = self.process(fim_alert(BASE + timedelta(seconds=1), "/etc/systemd/system/night-agent.service"), buffer)
        self.assertEqual(result, "explained")
        self.assertEqual(self.notifier.sent, [])
        self.assertEqual(self.state["digest"]["sudo_explained_count"], 1)
        self.assertIn("cp night-agent.service", self.state["digest"]["sudo_explained"][0]["command"])
        html_lines, md = batwoman.render_sudo_explained(self.state["digest"])
        self.assertIn("Cambios explicados por sudo", html_lines[0])
        self.assertIn("## Cambios explicados por sudo", md)

    def test_unexplained_change_alerts_immediately(self) -> None:
        buffer = buffer_of(sudo_alert(BASE, "/usr/bin/systemctl daemon-reload"))
        result = self.process(fim_alert(BASE + timedelta(seconds=1), "/etc/systemd/system/backdoor.service"), buffer)
        self.assertEqual(result, "critical")
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertIn("ruta critica", self.notifier.sent[0])
        self.assertNotIn("sudo_explained_count", self.state["digest"])

    def test_sudoers_alerts_immediately_even_with_sudo(self) -> None:
        buffer = buffer_of(sudo_alert(BASE, "/usr/bin/cp batifamilia /etc/sudoers.d/"))
        result = self.process(fim_alert(BASE + timedelta(seconds=1), "/etc/sudoers.d/batifamilia"), buffer)
        self.assertEqual(result, "critical")
        self.assertEqual(len(self.notifier.sent), 1)

    def test_fresh_unexplained_change_is_deferred_once(self) -> None:
        now = datetime.now(timezone.utc)
        fim = fim_alert(now, "/etc/systemd/system/x.service")
        self.assertEqual(self.process(fim, []), "deferred")
        self.assertEqual(self.state["fim_pending"], [fim])
        self.assertEqual(self.notifier.sent, [])
        # Proximo poll: ya llego el sudo -> explicado.
        late_sudo = sudo_alert(now - timedelta(seconds=1), "/usr/bin/cp x.service /etc/systemd/system/")
        self.assertEqual(self.process(fim, buffer_of(late_sudo), allow_defer=False), "explained")
        # Sin sudo y sin aplazar de nuevo -> alerta como siempre.
        self.assertEqual(self.process(fim, [], allow_defer=False), "critical")
        self.assertEqual(len(self.notifier.sent), 1)

    def test_sudoers_change_is_never_deferred(self) -> None:
        fim = fim_alert(datetime.now(timezone.utc), "/etc/sudoers.d/batifamilia")
        self.assertEqual(self.process(fim, []), "critical")
        self.assertEqual(len(self.notifier.sent), 1)


if __name__ == "__main__":
    unittest.main()
