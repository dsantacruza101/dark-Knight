"""Criterio de exito del modo Desarrollo (`verify_changes`):

1. un archivo permitido identico a `origin/<rama>` no hace fallar el intento
   por si solo: basta con que AL MENOS UNO cambie (y todos existan y no
   esten vacios);
2. `npm run lint` solo corre si el script `lint` ya existia en
   `origin/<rama>` (`when: base_script:lint`); uno agregado por el modelo
   no es requisito.

Correr: python3 -m unittest discover -s tests -q
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modes import development  # noqa: E402
from modes.development import node_has_script, step_condition_met, verify_changes  # noqa: E402

LINT_STEP = {"cmd": "npm run lint", "when": "base_script:lint"}
PROFILE = {
    "name": "nightwing",
    "test_runner": "auto",
    "verification": {"node": {"detect": ["package.json"], "steps": [LINT_STEP]}},
}


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


class RepoWithOrigin(unittest.TestCase):
    """Repo `work` clonado de un bare `origin` con la rama `dev`."""

    base_scripts: dict = {"build": "tsc"}

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        origin = root / "origin.git"
        seed = root / "seed"
        git(root, "init", "-q", "--bare", "-b", "dev", str(origin))
        git(root, "init", "-q", "-b", "dev", str(seed))
        git(seed, "config", "user.email", "test@example.com")
        git(seed, "config", "user.name", "test")
        (seed / "package.json").write_text(json.dumps({"name": "x", "scripts": self.base_scripts}), encoding="utf-8")
        (seed / "src").mkdir()
        (seed / "src/a.ts").write_text("export const a = 1;\n", encoding="utf-8")
        (seed / "src/b.ts").write_text("export const b = 2;\n", encoding="utf-8")
        git(seed, "add", ".")
        git(seed, "commit", "-q", "-m", "init")
        git(seed, "remote", "add", "origin", str(origin))
        git(seed, "push", "-q", "origin", "dev")
        self.repo = root / "work"
        git(root, "clone", "-q", "-b", "dev", str(origin), str(self.repo))
        self.log_path = root / "attempt.log"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, rel: str, content: str) -> None:
        path = self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def set_scripts(self, scripts: dict) -> None:
        self.write("package.json", json.dumps({"name": "x", "scripts": scripts}))


class AtLeastOneChangedTest(RepoWithOrigin):
    def verify(self, allowed: list[str]):
        # Solo interesa el criterio de archivos: sin pasos ni tests por archivo.
        with mock.patch.object(development, "run_verification_steps", return_value=([], [])) as steps:
            result = verify_changes(self.repo, allowed, "dev", self.log_path, PROFILE)
        return result, steps

    def test_one_identical_file_does_not_fail_if_another_changed(self) -> None:
        self.write("src/a.ts", "export const a = 42;\n")
        (ok, error, _, _), steps = self.verify(["src/a.ts", "src/b.ts"])
        self.assertTrue(ok, error)
        steps.assert_called_once()
        self.assertEqual(steps.call_args.kwargs.get("base_branch"), "dev")

    def test_new_file_counts_as_changed(self) -> None:
        self.write("src/c.ts", "export const c = 3;\n")
        (ok, error, _, _), _ = self.verify(["src/b.ts", "src/c.ts"])
        self.assertTrue(ok, error)

    def test_all_identical_fails_without_running_steps(self) -> None:
        (ok, error, _, _), steps = self.verify(["src/a.ts", "src/b.ts"])
        self.assertFalse(ok)
        self.assertIn("ningun archivo permitido cambio", error)
        steps.assert_not_called()

    def test_missing_allowed_file_still_fails(self) -> None:
        self.write("src/a.ts", "export const a = 42;\n")
        (ok, error, _, _), _ = self.verify(["src/a.ts", "src/missing.ts"])
        self.assertFalse(ok)
        self.assertIn("src/missing.ts: no existe", error)

    def test_empty_allowed_file_still_fails(self) -> None:
        self.write("src/a.ts", "export const a = 42;\n")
        self.write("src/b.ts", "   \n// solo comentario\n")
        (ok, error, _, _), _ = self.verify(["src/a.ts", "src/b.ts"])
        self.assertFalse(ok)
        self.assertIn("src/b.ts: vacio", error)

    def test_identical_file_is_not_reported_as_error(self) -> None:
        self.write("src/a.ts", "export const a = 42;\n")
        with mock.patch.object(development, "run_verification_steps", return_value=(["paso X fallo"], [])):
            ok, error, _, _ = verify_changes(self.repo, ["src/a.ts", "src/b.ts"], "dev", self.log_path, PROFILE)
        self.assertFalse(ok)
        self.assertIn("paso X fallo", error)
        self.assertNotIn("identico", error)


class LintOnlyIfInBaseTest(RepoWithOrigin):
    base_scripts = {"build": "tsc"}

    def ran_commands(self) -> list[list[str]]:
        ran: list[list[str]] = []

        def fake_run_cli(cmd, *args, **kwargs):
            if cmd[:2] == ["npm", "run"]:
                ran.append(list(cmd))
                return subprocess.CompletedProcess(cmd, 0, "", "")
            return real_run_cli(cmd, *args, **kwargs)

        real_run_cli = development.run_cli
        with mock.patch.object(development, "run_cli", side_effect=fake_run_cli):
            errors, _ = development.run_verification_steps(
                self.repo, PROFILE, ["package.json"], [], base_branch="dev"
            )
        self.assertEqual(errors, [])
        return ran

    def test_lint_added_by_model_is_not_run(self) -> None:
        self.set_scripts({"build": "tsc", "lint": "eslint ."})
        self.assertTrue(node_has_script(self.repo, "lint"))
        self.assertFalse(node_has_script(self.repo, "lint", "origin/dev"))
        self.assertFalse(step_condition_met(self.repo, "base_script:lint", "dev"))
        self.assertEqual(self.ran_commands(), [])

    def test_base_script_without_branch_uses_disk(self) -> None:
        self.set_scripts({"lint": "eslint ."})
        self.assertTrue(step_condition_met(self.repo, "base_script:lint"))

    def test_base_script_in_list_condition(self) -> None:
        self.assertTrue(step_condition_met(self.repo, ["base_script:build", "exists:package.json"], "dev"))
        self.assertFalse(step_condition_met(self.repo, ["base_script:lint", "exists:package.json"], "dev"))


class LintPresentInBaseTest(RepoWithOrigin):
    base_scripts = {"build": "tsc", "lint": "eslint ."}

    def test_lint_from_base_runs_even_if_model_removed_it(self) -> None:
        self.set_scripts({"build": "tsc"})
        self.assertTrue(step_condition_met(self.repo, "base_script:lint", "dev"))
        ran: list[list[str]] = []

        def fake_run_cli(cmd, *args, **kwargs):
            if cmd[:2] == ["npm", "run"]:
                ran.append(list(cmd))
                return subprocess.CompletedProcess(cmd, 0, "", "")
            return real_run_cli(cmd, *args, **kwargs)

        real_run_cli = development.run_cli
        with mock.patch.object(development, "run_cli", side_effect=fake_run_cli):
            development.run_verification_steps(self.repo, PROFILE, ["package.json"], [], base_branch="dev")
        self.assertEqual(ran, [["npm", "run", "lint"]])


class ProfilesUseBaseScriptForLintTest(unittest.TestCase):
    def test_node_profiles_gate_lint_on_base(self) -> None:
        import yaml

        specialists = Path(__file__).resolve().parent.parent / "batcave" / "specialists"
        for name in ("nightwing", "batgirl", "red_robin"):
            profile = yaml.safe_load((specialists / f"{name}.yaml").read_text(encoding="utf-8"))
            steps = [s for stack in profile["verification"].values() for s in stack.get("steps", [])]
            lint = [s for s in steps if "lint" in str(s.get("cmd", "")) and "npm" in str(s.get("cmd", ""))]
            self.assertTrue(lint, name)
            for step in lint:
                self.assertEqual(step.get("when"), "base_script:lint", name)


if __name__ == "__main__":
    unittest.main()
