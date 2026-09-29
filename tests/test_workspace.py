import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "jev0.py"


class WorkspaceGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        self.env = dict(
            os.environ,
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
        )
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Test")
        (self.repo / "base.txt").write_text("base\n")
        self.git("add", "base.txt")
        self.git("commit", "-qm", "base")

    def git(self, *args):
        return subprocess.run(
            ["git", *args],
            cwd=self.repo,
            env=self.env,
            check=True,
            capture_output=True,
        ).stdout

    def cli(self, *args):
        return subprocess.run(
            [sys.executable, str(CLI), *args],
            cwd=self.repo,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def blocked(self, result, reason):
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(reason, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_workspace_allows_clean_repository(self):
        result = self.cli("workspace")
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))

    def test_workspace_sees_unstaged_tracked_changes(self):
        (self.repo / "base.txt").write_text("base\nextra\n")
        self.blocked(
            self.cli("workspace", "--max-lines", "1"),
            "2 added/deleted lines",
        )
        self.assertEqual(self.cli("staged", "--max-lines", "1").returncode, 0)

    def test_workspace_blocks_untracked_files(self):
        (self.repo / "new.txt").write_text("new\n")
        self.blocked(
            self.cli("workspace"),
            "untracked file requires staging/review",
        )

    def test_workspace_blocks_unstaged_model_artifact(self):
        self.git("checkout", "-qb", "model-test")
        (self.repo / "weights.gguf").write_text("model\n")
        self.git("add", "-f", "weights.gguf")
        self.git("commit", "-qm", "track model fixture")
        (self.repo / "weights.gguf").write_text("changed model\n")
        self.blocked(self.cli("workspace"), "model artifact")

    def test_workspace_scope_applies_to_unstaged_changes(self):
        (self.repo / "base.txt").write_text("changed\n")
        self.blocked(
            self.cli("workspace", "--allow", "src"),
            "outside allowed scope",
        )

    def test_version_is_stable_cli_surface(self):
        result = subprocess.run(
            [sys.executable, str(CLI), "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0)
        self.assertRegex(result.stdout.strip(), r"^jev0 \d+\.\d+\.\d+")


if __name__ == "__main__":
    unittest.main()
