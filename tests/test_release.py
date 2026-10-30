"""Release manager de Spoiler: merge base -> rama de trabajo (limpio y con
conflicto, que se aborta sin pushear) y agrupacion de commits del PR.

Correr: venv/bin/python -m pytest tests/ -q  (o venv/bin/python -m unittest discover tests)
"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from batcave.release import (  # noqa: E402
    FEATURES,
    FIXES,
    OTHER,
    SECURITY,
    Commit,
    RepoRelease,
    build_pr_body,
    checkout_work_branch,
    commits_between,
    merge_base_into_work,
)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def commit_file(repo: Path, name: str, content: str, message: str) -> None:
    (repo / name).write_text(content, encoding="utf-8")
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", message)


class MergeBaseIntoWorkTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.origin = root / "origin.git"
        git(root, "init", "-q", "--bare", "-b", "main", str(self.origin))
        self.author = root / "author"
        git(root, "clone", "-q", str(self.origin), str(self.author))
        for repo_cfg in (("user.email", "test@example.com"), ("user.name", "test")):
            git(self.author, "config", *repo_cfg)
        commit_file(self.author, "app.txt", "v1\n", "chore: init")
        git(self.author, "push", "-q", "origin", "main")
        git(self.author, "checkout", "-q", "-b", "dev")
        commit_file(self.author, "feature.txt", "x\n", "feat(nightwing): resuelve #3 - algo")
        git(self.author, "push", "-q", "origin", "dev")
        self.ws = root / "ws"
        git(root, "clone", "-q", str(self.origin), str(self.ws))
        for repo_cfg in (("user.email", "test@example.com"), ("user.name", "test")):
            git(self.ws, "config", *repo_cfg)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def push_to_main(self, name: str, content: str, message: str) -> None:
        git(self.author, "checkout", "-q", "main")
        commit_file(self.author, name, content, message)
        git(self.author, "push", "-q", "origin", "main")
        git(self.ws, "fetch", "-q", "origin")

    def test_clean_merge(self) -> None:
        self.push_to_main("hotfix.txt", "y\n", "fix: hotfix en main")
        checkout_work_branch(self.ws, "dev")
        ok, conflicts, error = merge_base_into_work(self.ws, "dev", "main")
        self.assertTrue(ok, error)
        self.assertEqual(conflicts, [])
        self.assertTrue((self.ws / "hotfix.txt").exists())
        # Solo el commit propio de dev queda como "no esta en la base" (sin merges).
        subjects = [c.subject for c in commits_between(self.ws, "origin/main", "HEAD")]
        self.assertEqual(subjects, ["feat(nightwing): resuelve #3 - algo"])

    def test_conflict_aborts_and_restores(self) -> None:
        git(self.author, "checkout", "-q", "dev")
        commit_file(self.author, "app.txt", "dev\n", "feat: cambio en dev")
        git(self.author, "push", "-q", "origin", "dev")
        self.push_to_main("app.txt", "main\n", "fix: cambio en main")
        checkout_work_branch(self.ws, "dev")
        before = git(self.ws, "rev-parse", "HEAD")
        ok, conflicts, _ = merge_base_into_work(self.ws, "dev", "main")
        self.assertFalse(ok)
        self.assertEqual(conflicts, ["app.txt"])
        self.assertEqual(git(self.ws, "rev-parse", "HEAD"), before)
        self.assertEqual(git(self.ws, "status", "--porcelain"), "")
        self.assertEqual(git(self.ws, "rev-parse", "origin/dev"), before)

    def test_refuses_wrong_branch(self) -> None:
        git(self.ws, "checkout", "-q", "main")
        ok, _, error = merge_base_into_work(self.ws, "dev", "main")
        self.assertFalse(ok)
        self.assertIn("seguridad", error)


class PrBodyTest(unittest.TestCase):
    def test_groups_closes_and_ci(self) -> None:
        commits = [
            Commit("a" * 40, "fix(deps): axios 1.6.0 -> 1.7.4 (radar)", "Resuelve #7\nResuelto por: deterministic-patch"),
            Commit("b" * 40, "feat(batgirl): resuelve #2 - navbar", ""),
            Commit("c" * 40, "fix: typo", ""),
            Commit("d" * 40, "Merge-less chore", ""),
        ]
        self.assertEqual([c.group for c in commits], [SECURITY, FEATURES, FIXES, OTHER])
        rel = RepoRelease(repo="r", full_name="o/r", work="dev", base="main", head_sha="e" * 40,
                          commits=commits, closes=[2, 7], ci="success", ci_detail="3 ok, 0 fallidos, 0 en curso")
        body = build_pr_body(rel, max_listed=50)
        self.assertIn("Parches de seguridad (radar) (1)", body)
        self.assertIn("Closes #2\nCloses #7", body)
        self.assertIn("✅ success", body)
        self.assertIn("(aaaaaaa)", body)


if __name__ == "__main__":
    unittest.main()
