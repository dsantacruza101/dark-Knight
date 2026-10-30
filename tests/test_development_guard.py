"""Regresion del candado del modo Desarrollo (bills#1): un archivo nuevo
dentro de un directorio nuevo que esta en la lista permitida sobrevive; uno
fuera de la lista se borra (archivo por archivo, sin borrar directorios con
contenido) y los directorios que quedan vacios se podan.

Correr: venv/bin/python -m pytest tests/ -q  (o venv/bin/python -m unittest discover tests)
"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modes.development import (  # noqa: E402
    apply_guard,
    changed_files,
    installed_versions,
    pin_requirement,
    requirement_files_for_patch,
)


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


class GuardNewDirectoriesTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name) / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "dev")
        git(self.repo, "config", "user.email", "test@example.com")
        git(self.repo, "config", "user.name", "test")
        (self.repo / "README.md").write_text("# bills\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-q", "-m", "init")
        self.log_path = Path(self._tmp.name) / "guard.log"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, rel: str, content: str) -> None:
        path = self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def test_changed_files_lists_files_inside_new_directories(self) -> None:
        self.write("src/services/bills.ts", "export const x = 1;\n")
        self.write("tests/bills.test.ts", "test('x', () => {});\n")
        self.assertEqual(sorted(changed_files(self.repo)), ["src/services/bills.ts", "tests/bills.test.ts"])

    def test_allowed_file_in_new_dir_survives_and_unlisted_is_removed(self) -> None:
        allowed = ["src/services/bills.ts", "tests/bills.test.ts"]
        self.write("src/services/bills.ts", "export const total = (a: number) => a;\n")
        self.write("tests/bills.test.ts", "test('total', () => { expect(1).toBe(1); });\n")
        # Fuera de la lista: dentro de un directorio permitido, en un directorio
        # nuevo propio y un archivo modificado ya trackeado.
        self.write("src/services/extra.ts", "export const extra = 2;\n")
        self.write("scripts/deploy/run.sh", "echo hola\n")
        self.write("README.md", "# cambiado\n")

        apply_guard(self.repo, allowed, self.log_path, "bills#1", "claude")

        self.assertTrue((self.repo / "src/services/bills.ts").is_file())
        self.assertTrue((self.repo / "tests/bills.test.ts").is_file())
        self.assertFalse((self.repo / "src/services/extra.ts").exists())
        self.assertFalse((self.repo / "scripts").exists(), "el directorio vacio debe podarse")
        self.assertEqual((self.repo / "README.md").read_text(encoding="utf-8"), "# bills\n")
        self.assertEqual(sorted(changed_files(self.repo)), sorted(allowed))


class PypiRequirementsLockTest(unittest.TestCase):
    def test_pins_package_in_requirements_dev_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "requirements.txt").write_text("requests==2.32.0\n", encoding="utf-8")
            (repo / "requirements-dev.lock").write_text("pytest==8.0.0\nJinja2==3.1.2  # via x\n", encoding="utf-8")
            patch = {"package": "jinja2", "ecosystem": "PyPI", "from": "3.1.2", "to": "3.1.6", "manifest": ""}

            self.assertEqual(requirement_files_for_patch(repo, patch), ["requirements-dev.lock"])

            class Log:
                def note(self, *_args) -> None:
                    pass

            self.assertEqual(pin_requirement(repo, patch, Log()), "")
            self.assertIn("Jinja2==3.1.6 # via x", (repo / "requirements-dev.lock").read_text(encoding="utf-8"))
            self.assertEqual((repo / "requirements.txt").read_text(encoding="utf-8"), "requests==2.32.0\n")
            self.assertEqual(installed_versions(repo, patch), ["3.1.6"])


if __name__ == "__main__":
    unittest.main()
