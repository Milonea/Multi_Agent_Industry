from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import subprocess
import tempfile
import unittest

from portfolio_control_plane.reality import LocalGitRealityScanner


class LocalRealityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.repo = self.root / "project"
        self.repo.mkdir()
        self.remote = self.root / "remote.git"
        self._git("init", "-b", "main", cwd=self.repo)
        self._git("config", "user.email", "test@example.invalid", cwd=self.repo)
        self._git("config", "user.name", "Local Reality Test", cwd=self.repo)
        (self.repo / "docs" / "project").mkdir(parents=True)
        (self.repo / "README.md").write_text("fixture\n", encoding="utf-8")
        self._git("add", ".", cwd=self.repo)
        self._git("commit", "-m", "fixture", cwd=self.repo)
        self._git("init", "--bare", str(self.remote), cwd=self.root)
        self._git("remote", "add", "origin", str(self.remote), cwd=self.repo)
        self._git("push", "-u", "origin", "main", cwd=self.repo)
        self.head = self._git("rev-parse", "HEAD", cwd=self.repo)
        today = datetime.now(timezone.utc).date().isoformat()
        (self.repo / "docs" / "project" / "status.md").write_text(
            f"# Current snapshot — {today}\n\n"
            f"- local checkout: `main`, HEAD: `{self.head}`\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    @staticmethod
    def _git(*args: str, cwd: Path) -> str:
        result = subprocess.run(
            ["git", "-C", str(cwd), *args],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        return result.stdout.strip()

    def test_clean_upstream_and_worktree_are_collected_as_fresh_observation(self) -> None:
        snapshot = LocalGitRealityScanner(self.root).scan(
            "fixture/project",
            self.repo,
            status_document="docs/project/status.md",
        )

        self.assertEqual(snapshot.status, "fresh")
        self.assertEqual(snapshot.branch, "main")
        self.assertEqual(snapshot.head_sha, self.head)
        self.assertTrue(snapshot.dirty)
        self.assertEqual(snapshot.upstream, "origin/main")
        self.assertEqual((snapshot.ahead, snapshot.behind), (0, 0))
        self.assertFalse(snapshot.detached)
        self.assertTrue(snapshot.worktree)
        self.assertEqual(snapshot.document_declared_branch, "main")
        self.assertEqual(snapshot.document_declared_head, self.head)
        self.assertIn("project", snapshot.worktree_paths)

    def test_ahead_and_document_head_mismatch_are_reported_as_stale(self) -> None:
        (self.repo / "new.txt").write_text("ahead\n", encoding="utf-8")
        self._git("add", "new.txt", cwd=self.repo)
        self._git("commit", "-m", "ahead", cwd=self.repo)

        snapshot = LocalGitRealityScanner(self.root).scan(
            "fixture/project",
            self.repo,
            status_document="docs/project/status.md",
        )

        self.assertEqual(snapshot.status, "stale")
        self.assertEqual((snapshot.ahead, snapshot.behind), (1, 0))
        self.assertIn("DOCUMENT_HEAD_MISMATCH", snapshot.errors)

    def test_no_upstream_is_partial_and_detached_head_is_explicit(self) -> None:
        self._git("checkout", "--detach", self.head, cwd=self.repo)

        snapshot = LocalGitRealityScanner(self.root).scan(
            "fixture/project",
            self.repo,
            status_document="docs/project/status.md",
        )

        self.assertEqual(snapshot.status, "partial")
        self.assertTrue(snapshot.detached)
        self.assertIsNone(snapshot.upstream)
        self.assertIn("UPSTREAM_UNAVAILABLE", snapshot.errors)

    def test_missing_repository_is_unavailable_without_guessing(self) -> None:
        snapshot = LocalGitRealityScanner(self.root).scan(
            "missing/project",
            self.root / "missing",
        )

        self.assertEqual(snapshot.status, "unavailable")
        self.assertEqual(snapshot.errors, ("REPOSITORY_PATH_MISSING",))
        self.assertIsNone(snapshot.head_sha)

    def test_collection_time_uses_injected_clock(self) -> None:
        fixed = datetime(2040, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        snapshot = LocalGitRealityScanner(
            self.root,
            now=lambda: fixed,
        ).scan(
            "fixture/project",
            self.repo,
            status_document=None,
        )

        self.assertEqual(snapshot.collected_at, "2040-01-02T03:04:05+00:00")

    def test_status_document_cannot_escape_repository_boundary(self) -> None:
        outside = self.root / "outside-status.md"
        outside.write_text("# secret unrelated document\n", encoding="utf-8")
        snapshot = LocalGitRealityScanner(self.root).scan(
            "fixture/project",
            self.repo,
            status_document=outside,
        )

        self.assertIsNone(snapshot.status_document)
        self.assertIn("STATUS_DOCUMENT_OUTSIDE_REPOSITORY", snapshot.errors)

    def test_git_timeout_is_classified_as_unavailable(self) -> None:
        def timeout_runner(repository: Path, args: tuple[str, ...]) -> str:
            raise subprocess.TimeoutExpired(["git", *args], 0.01)

        snapshot = LocalGitRealityScanner(
            self.root,
            git_runner=timeout_runner,
        ).scan("fixture/project", self.repo, status_document=None)

        self.assertEqual(snapshot.status, "unavailable")
        self.assertEqual(snapshot.errors, ("GIT_STATUS_UNAVAILABLE",))

if __name__ == "__main__":
    unittest.main()
