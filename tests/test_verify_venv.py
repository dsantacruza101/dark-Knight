"""Regresion: pytest en repos Python SIN uv.lock nunca usa el python3 del
PATH (la noche del 2026-11-11 resolvio a ~/.aider-venv/bin/python3, sin
pytest). Se usa un venv aislado y cacheado `.verify-venv` dentro del
workspace, que el candado ignora y `git clean` conserva.

Correr: python3 -m unittest discover -s tests -q
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import red_hood  # noqa: E402
from modes import development  # noqa: E402
from modes.development import (  # noqa: E402
    VERIFY_VENV_DIR,
    changed_entries,
    ensure_verify_venv,
    exclude_verify_venv,
    is_forbidden_path,
    is_python_pytest_cmd,
    run_pytest_file,
    run_verification_steps,
)

HASHED = "certifi==2026.7.22 \\\n    --hash=sha256:" + "a" * 64 + "\n"
PYTEST_OK = "1 passed in 0.01s\n"


def ok(cmd, out=""):
    return subprocess.CompletedProcess(cmd, 0, out, "")


class FakeUv:
    """`run_cli` simulado: `uv venv` crea el python del venv; registra todo."""

    def __init__(self, fail_on: str = "") -> None:
        self.calls: list[tuple[list[str], dict]] = []
        self.fail_on = fail_on

    def __call__(self, cmd, cwd=None, timeout=600, extra_env=None, drop_env=None):
        self.calls.append((list(cmd), {"cwd": cwd, "extra_env": extra_env, "drop_env": drop_env}))
        if self.fail_on and self.fail_on in " ".join(cmd):
            return subprocess.CompletedProcess(cmd, 2, "", "boom")
        if cmd[:2] == ["uv", "venv"]:
            python = Path(cwd) / cmd[2] / "bin" / "python"
            python.parent.mkdir(parents=True, exist_ok=True)
            python.write_text("", encoding="utf-8")
        if "pytest" in cmd and cmd[:2] != ["uv", "pip"]:
            return ok(cmd, PYTEST_OK)
        return ok(cmd)

    def commands(self) -> list[list[str]]:
        return [c for c, _ in self.calls]


def make_repo(tmp: str, files: dict[str, str]) -> Path:
    repo = Path(tmp) / "repo"
    repo.mkdir()
    for rel, content in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(content, encoding="utf-8")
    return repo


class EnsureVerifyVenvTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.object(development, "tool_available", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_installs_requirements_hashed_separately_then_pytest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp, {
                "requirements.txt": "httpx==0.27.0\n",
                "requirements-dev.txt": "ruff==0.6.0\n",
                "requirements-dev.lock": HASHED,
            })
            fake = FakeUv()
            with mock.patch.object(development, "run_cli", side_effect=fake):
                python, runs = ensure_verify_venv(repo)
            self.assertEqual(python, repo / VERIFY_VENV_DIR / "bin" / "python")
            cmds = fake.commands()
            self.assertEqual(cmds[0], ["uv", "venv", VERIFY_VENV_DIR])
            install = ["uv", "pip", "install", "--python", str(python)]
            self.assertEqual(cmds[1], install + ["-r", "requirements.txt", "-r", "requirements-dev.txt"])
            self.assertEqual(cmds[2], install + ["--require-hashes", "-r", "requirements-dev.lock"])
            self.assertEqual(cmds[3], install + ["pytest"])
            self.assertEqual(len(runs), 4)
            # Ningun comando hereda secretos ni el VIRTUAL_ENV del servicio.
            drop = fake.calls[1][1]["drop_env"]
            self.assertTrue(drop("GITHUB_TOKEN") and drop("TYPESAFE_API_KEY") and drop("VIRTUAL_ENV"))
            self.assertFalse(drop("PATH"))

    def test_cached_until_requirements_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp, {"requirements.txt": "httpx==0.27.0\n"})
            fake = FakeUv()
            with mock.patch.object(development, "run_cli", side_effect=fake):
                ensure_verify_venv(repo)
                first = len(fake.calls)
                python, runs = ensure_verify_venv(repo)
                self.assertEqual((len(fake.calls), runs), (first, []))
                self.assertIsNotNone(python)
                (repo / "requirements.txt").write_text("httpx==0.28.0\n", encoding="utf-8")
                ensure_verify_venv(repo)
            self.assertEqual(fake.calls[first][0], ["uv", "venv", VERIFY_VENV_DIR])

    def test_failed_install_is_not_cached(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp, {"requirements.txt": "httpx==0.27.0\n"})
            with mock.patch.object(development, "run_cli", side_effect=FakeUv(fail_on="-r requirements.txt")):
                python, runs = ensure_verify_venv(repo)
            self.assertIsNone(python)
            self.assertEqual(runs[-1][1].returncode, 2)
            fake = FakeUv()
            with mock.patch.object(development, "run_cli", side_effect=fake):
                self.assertIsNotNone(ensure_verify_venv(repo)[0])
            self.assertEqual(fake.commands()[0][:2], ["uv", "venv"])

    def test_without_uv_fails_instead_of_using_path_python(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp, {"requirements.txt": "", "tests/test_a.py": "def test_a(): pass\n"})
            with mock.patch.object(development, "tool_available", return_value=False), \
                    mock.patch.object(development, "run_cli") as run:
                executed, failed, output = run_pytest_file(repo, "tests/test_a.py")
            run.assert_not_called()
            self.assertEqual((executed, failed), (0, 1))
            self.assertIn("uv no esta en el PATH", output)

    def test_pyproject_only_installs_its_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp, {"pyproject.toml": "[project]\nname='x'\nversion='0'\n"})
            fake = FakeUv()
            with mock.patch.object(development, "run_cli", side_effect=fake):
                ensure_verify_venv(repo)
            self.assertIn(["-r", "pyproject.toml", "--all-extras"], [c[-3:] for c in fake.commands()])


class PytestCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.object(development, "tool_available", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_detects_pytest_commands(self) -> None:
        self.assertTrue(is_python_pytest_cmd(["python3", "-m", "pytest", "-q"]))
        self.assertTrue(is_python_pytest_cmd(["pytest", "-q"]))
        self.assertFalse(is_python_pytest_cmd(["uv", "run", "--locked", "pytest"]))
        self.assertFalse(is_python_pytest_cmd(["python3", "-m", "unittest"]))

    def test_suite_step_runs_with_verify_venv_python(self) -> None:
        profile = {"name": "nightwing", "verification": {"python": {
            "detect": ["requirements.txt"],
            "steps": [{"cmd": "FOO=1 python3 -m pytest -q", "when": "exists:tests"}],
        }}}
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp, {"requirements.txt": "", "tests/test_a.py": "def test_a(): pass\n"})
            fake = FakeUv()
            with mock.patch.object(development, "run_cli", side_effect=fake):
                errors, _ = run_verification_steps(repo, profile, [], [])
            self.assertEqual(errors, [])
            last_cmd, kwargs = fake.calls[-1]
            self.assertEqual(last_cmd, [str(repo / VERIFY_VENV_DIR / "bin" / "python"), "-m", "pytest", "-q"])
            self.assertEqual(kwargs["extra_env"], {"FOO": "1"})
            self.assertTrue(kwargs["drop_env"]("TELEGRAM_BOT_TOKEN"))
            self.assertNotIn("python3", [c[0] for c in fake.commands()])

    def test_suite_step_skipped_without_tests_dir(self) -> None:
        profile = {"name": "nightwing", "verification": {"python": {
            "detect": ["requirements.txt"], "steps": [{"cmd": "pytest -q"}],
        }}}
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp, {"requirements.txt": ""})
            with mock.patch.object(development, "run_cli") as run:
                errors, _ = run_verification_steps(repo, profile, [], [])
            run.assert_not_called()
            self.assertEqual(errors, [])

    def test_single_file_runner_uses_verify_venv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp, {"requirements.txt": "", "tests/test_a.py": "def test_a(): pass\n"})
            fake = FakeUv()
            with mock.patch.object(development, "run_cli", side_effect=fake):
                executed, failed, _ = run_pytest_file(repo, "tests/test_a.py", {"X": "1"})
            self.assertEqual((executed, failed), (1, 0))
            self.assertEqual(fake.commands()[-1][:4], [str(repo / VERIFY_VENV_DIR / "bin" / "python"), "-m", "pytest", "tests/test_a.py"])

    def test_red_hood_suite_uses_verify_venv_without_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp, {"requirements.txt": "", "tests/test_a.py": "def test_a(): pass\n"})
            seen: dict = {}

            def fake_run(cmd, cwd, timeout, env):
                seen.update(cmd=cmd, env=env)
                return ok(cmd, PYTEST_OK), False

            with mock.patch.object(development, "run_cli", side_effect=FakeUv()), \
                    mock.patch.object(red_hood, "_run_with_timeout", side_effect=fake_run), \
                    mock.patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_x"}):
                result = red_hood.run_python_tests(repo, {"PENGUIN_ENV_FILE": "/nonexistent"})
            self.assertEqual(result["status"], "ok")
            self.assertEqual(seen["cmd"][0], str(repo / VERIFY_VENV_DIR / "bin" / "python"))
            self.assertNotIn("GITHUB_TOKEN", seen["env"])
            self.assertEqual(seen["env"]["PENGUIN_ENV_FILE"], "/nonexistent")


class GuardIgnoresVerifyVenvTests(unittest.TestCase):
    def git(self, repo: Path, *args: str) -> None:
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

    def test_never_listed_reverted_or_cleaned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(tmp, {"app.py": "x = 1\n"})
            self.git(repo, "init", "-q")
            self.git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "add", "app.py")
            self.git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
            site = repo / VERIFY_VENV_DIR / "lib" / "site.py"
            site.parent.mkdir(parents=True)
            site.write_text("", encoding="utf-8")
            (repo / "new.py").write_text("", encoding="utf-8")
            # Aun sin el exclude, el candado no lo lista.
            self.assertEqual([rel for _, rel in changed_entries(repo)], ["new.py"])
            exclude_verify_venv(repo)
            exclude_verify_venv(repo)  # idempotente
            exclude = (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8")
            self.assertEqual(exclude.count(f"/{VERIFY_VENV_DIR}/"), 1)
            self.git(repo, "clean", "-fd", "-e", VERIFY_VENV_DIR)
            self.assertTrue(site.exists())
            self.assertFalse((repo / "new.py").exists())
            self.assertTrue(is_forbidden_path(f"{VERIFY_VENV_DIR}/lib/site.py"))
            self.assertFalse(is_forbidden_path("src/verify-venv.py"))


if __name__ == "__main__":
    unittest.main()
