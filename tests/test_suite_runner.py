"""Suite completa de tests segun el runner JS detectado (`detect_suite_runner`).

`--watchAll` es solo de jest: en un repo vitest (bills#3) `npm test --
--passWithNoTests --watchAll=false` revento con `Unknown option
--watchAll` sin correr ningun test. La suite debe usar la misma deteccion
que el runner por archivo: vitest -> `CI=true npx --no-install vitest run
--passWithNoTests`; jest -> `CI=true npm test -- --passWithNoTests
--watchAll=false`. Aplica a todos los perfiles que verifican Node y a Red
Hood.

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
from modes.development import (  # noqa: E402
    describe_verification,
    detect_suite_runner,
    load_specialists,
    run_verification_steps,
    step_condition_met,
)
import red_hood  # noqa: E402

VITEST_SUITE = "CI=true npx --no-install vitest run --passWithNoTests"
JEST_SUITE = "CI=true npm test -- --passWithNoTests --watchAll=false"


def make_node_repo(tmp: str, dev_deps: dict, test_script: str, extra: dict | None = None) -> Path:
    repo = Path(tmp) / "repo"
    repo.mkdir()
    pkg = {"name": "x", "scripts": {"build": "tsc", "test": test_script}, "devDependencies": dev_deps}
    (repo / "package.json").write_text(json.dumps(pkg), encoding="utf-8")
    (repo / "package-lock.json").write_text("{}", encoding="utf-8")
    (repo / "node_modules").mkdir()
    for rel, content in (extra or {}).items():
        (repo / rel).write_text(content, encoding="utf-8")
    return repo


def vitest_repo(tmp: str) -> Path:
    # Como bills: vitest en devDependencies, `npm test` = `vitest run`.
    return make_node_repo(tmp, {"vitest": "^3.2.4", "typescript": "^5.0.0"}, "vitest run")


def jest_repo(tmp: str) -> Path:
    return make_node_repo(tmp, {"jest": "^29.7.0", "ts-jest": "^29.0.0"}, "jest")


def ok(cmd: list[str], stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(cmd, 0, stdout, "")


class DetectSuiteRunnerTests(unittest.TestCase):
    def test_vitest_in_dev_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(detect_suite_runner(vitest_repo(tmp)), "vitest")

    def test_jest_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(detect_suite_runner(jest_repo(tmp)), "jest")

    def test_vitest_config_without_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_node_repo(tmp, {}, "vitest run", {"vitest.config.ts": "export default {};\n"})
            self.assertEqual(detect_suite_runner(repo), "vitest")

    def test_profile_runner_wins_like_per_file_runner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = jest_repo(tmp)
            self.assertEqual(detect_suite_runner(repo, "vitest"), "vitest")
            self.assertEqual(detect_suite_runner(repo, "VITEST"), "vitest")
            # pytest/flutter no aplican a JS: deteccion automatica.
            self.assertEqual(detect_suite_runner(repo, "pytest"), "jest")
            self.assertEqual(detect_suite_runner(repo, None), "jest")

    def test_same_answer_as_per_file_runner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = vitest_repo(tmp)
            for configured in ("auto", "jest", "vitest"):
                self.assertEqual(
                    detect_suite_runner(repo, configured),
                    development.detect_test_runner(repo, "tests/a.test.ts", configured),
                )


class RunnerConditionTests(unittest.TestCase):
    def test_runner_condition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = vitest_repo(tmp)
            self.assertTrue(step_condition_met(repo, "runner:vitest"))
            self.assertFalse(step_condition_met(repo, "runner:jest"))
            self.assertTrue(step_condition_met(repo, ["script:test", "runner:vitest"]))
            self.assertFalse(step_condition_met(repo, ["script:lint", "runner:vitest"]))
            self.assertTrue(step_condition_met(repo, "runner:jest", test_runner="jest"))


class ProfilesSuiteCommandTests(unittest.TestCase):
    """Los perfiles reales con stack Node eligen la suite segun el runner."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.specialists = load_specialists({})

    def node_profiles(self) -> dict[str, dict]:
        profiles = {n: p for n, p in self.specialists.items() if "node" in (p.get("verification") or {})}
        self.assertTrue({"nightwing", "batgirl", "red_robin"} <= set(profiles), profiles.keys())
        return profiles

    def suite_lines(self, repo: Path, profile: dict) -> list[str]:
        return [line for line in describe_verification(repo, profile) if "vitest" in line or "npm test" in line]

    def test_vitest_repo_never_gets_watch_all(self) -> None:
        for name, profile in self.node_profiles().items():
            with self.subTest(profile=name), tempfile.TemporaryDirectory() as tmp:
                lines = self.suite_lines(vitest_repo(tmp), profile)
                self.assertEqual(len(lines), 1, lines)
                self.assertIn(VITEST_SUITE, lines[0])
                self.assertNotIn("--watchAll", lines[0])

    def test_jest_repo_keeps_npm_test(self) -> None:
        for name, profile in self.node_profiles().items():
            if profile.get("test_runner") == "vitest":
                continue  # Batgirl fija vitest: igual que su runner por archivo.
            with self.subTest(profile=name), tempfile.TemporaryDirectory() as tmp:
                lines = self.suite_lines(jest_repo(tmp), profile)
                self.assertEqual(lines, [f"- [node] {JEST_SUITE}"])

    def test_batgirl_forced_vitest_profile(self) -> None:
        profile = self.specialists["batgirl"]
        with tempfile.TemporaryDirectory() as tmp:
            repo = jest_repo(tmp)
            self.assertEqual(self.suite_lines(repo, profile), [f"- [node] {VITEST_SUITE}"])
            auto = {**profile, "test_runner": "auto"}
            self.assertEqual(self.suite_lines(repo, auto), [f"- [node] {JEST_SUITE}"])

    def test_nightwing_verification_runs_vitest_on_bills_like_repo(self) -> None:
        """Regresion bills#3: el comando que se ejecuta no lleva --watchAll."""
        profile = self.specialists["nightwing"]
        seen: list[list[str]] = []

        def fake_run_cli(cmd, cwd=None, timeout=600, **kwargs):
            seen.append(list(cmd))
            if "vitest" in cmd:
                return ok(cmd, " Test Files  1 passed (1)\n      Tests  7 passed (7)\n")
            return ok(cmd)

        with tempfile.TemporaryDirectory() as tmp:
            repo = vitest_repo(tmp)
            (repo / "tests").mkdir()
            (repo / "tests/payway.test.ts").write_text("it('x', () => {});\n", encoding="utf-8")
            with mock.patch.object(development, "run_cli", side_effect=fake_run_cli):
                errors, _ = run_verification_steps(repo, profile, ["tests/payway.test.ts"], ["tests/payway.test.ts"])
        self.assertEqual(errors, [])
        suites = [c for c in seen if "vitest" in c or c[:2] == ["npm", "test"]]
        self.assertEqual(suites, [["npx", "--no-install", "vitest", "run", "--passWithNoTests"]])
        self.assertFalse(any("--watchAll=false" in c for c in seen), seen)


class RedHoodSuiteCommandTests(unittest.TestCase):
    def run_suite(self, repo: Path, test_runner: str = "auto") -> list[str]:
        seen: dict = {}

        def fake_run(cmd, cwd, timeout, env):
            seen["cmd"] = cmd
            return ok(cmd, "Tests  3 passed (3)\n"), False

        with mock.patch.object(red_hood, "_run_with_timeout", side_effect=fake_run):
            result = red_hood.run_node_tests(repo, test_runner)
        self.assertEqual(result["status"], "ok")
        return seen["cmd"]

    def test_vitest_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cmd = self.run_suite(vitest_repo(tmp))
        self.assertEqual(cmd[:5], ["npx", "--no-install", "vitest", "run", "--passWithNoTests"])
        self.assertIn("--coverage", cmd)
        self.assertNotIn("--watchAll=false", cmd)

    def test_vitest_config_only_repo(self) -> None:
        # Antes Red Hood solo miraba package.json: un vitest.config.* sin la
        # dependencia caia a jest y le pasaba --watchAll.
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_node_repo(tmp, {}, "vitest run", {"vitest.config.ts": "export default {};\n"})
            self.assertNotIn("--watchAll=false", self.run_suite(repo))

    def test_jest_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cmd = self.run_suite(jest_repo(tmp))
        self.assertEqual(cmd, ["npm", "test", "--", "--passWithNoTests", "--coverage", "--watchAll=false"])

    def test_profile_runner_is_honored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cmd = self.run_suite(jest_repo(tmp), "vitest")
        self.assertEqual(cmd[2], "vitest")


if __name__ == "__main__":
    unittest.main()
