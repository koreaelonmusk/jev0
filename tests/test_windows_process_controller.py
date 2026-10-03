import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "jev0.py"


class WindowsProcessControllerTests(unittest.TestCase):
    def cli(self, *args, cwd):
        return subprocess.run(
            [sys.executable, str(CLI), *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_run_preserves_literal_argv_cross_platform(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.cli(
                "run",
                "--timeout",
                "2",
                "--",
                sys.executable,
                "-c",
                "import sys; print(sys.argv[1]); sys.exit(7)",
                "$(touch injected)",
                cwd=directory,
            )
            self.assertEqual(result.returncode, 7, result.stderr)
            self.assertEqual(result.stdout.strip(), "$(touch injected)")
            self.assertFalse((Path(directory) / "injected").exists())

    def test_run_timeout_kills_owned_child_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "escaped"
            child = (
                "import time,pathlib; "
                "time.sleep(1); "
                "pathlib.Path('escaped').touch()"
            )
            code = (
                "import subprocess,sys,time; "
                f"subprocess.Popen([sys.executable,'-c',{child!r}]); "
                "time.sleep(20)"
            )
            result = self.cli(
                "run",
                "--timeout",
                "0.3",
                "--",
                sys.executable,
                "-c",
                code,
                cwd=directory,
            )
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("process group terminated", result.stderr)
            time.sleep(1.1)
            self.assertFalse(marker.exists())

    def _init_repo(self, directory):
        env = dict(
            os.environ,
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
        )
        subprocess.run(["git", "init", "-q"], cwd=directory, env=env, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.invalid"],
            cwd=directory,
            env=env,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=directory,
            env=env,
            check=True,
        )
        path = Path(directory) / "base.txt"
        path.write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "add", "base.txt"], cwd=directory, env=env, check=True)
        subprocess.run(
            ["git", "commit", "-qm", "base"],
            cwd=directory,
            env=env,
            check=True,
        )
        return env

    def test_workspace_staged_and_range_use_portable_bounded_git_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            env = self._init_repo(directory)
            path = Path(directory) / "base.txt"
            path.write_text("base\nchanged\nextra\n", encoding="utf-8")

            workspace = subprocess.run(
                [sys.executable, str(CLI), "workspace", "--max-lines", "1"],
                cwd=directory,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(workspace.returncode, 1, workspace.stderr)
            self.assertIn("added/deleted lines exceed budget", workspace.stderr)

            subprocess.run(["git", "add", "base.txt"], cwd=directory, env=env, check=True)
            staged = subprocess.run(
                [sys.executable, str(CLI), "staged", "--max-lines", "1"],
                cwd=directory,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(staged.returncode, 1, staged.stderr)

            same_range = subprocess.run(
                [sys.executable, str(CLI), "range", "HEAD", "HEAD"],
                cwd=directory,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(same_range.returncode, 0, same_range.stderr)

    def test_external_evaluator_uses_portable_bounded_pipe_reader(self):
        with tempfile.TemporaryDirectory() as directory:
            env = self._init_repo(directory)
            path = Path(directory) / "base.txt"
            path.write_text("base\nchanged\n", encoding="utf-8")
            subprocess.run(["git", "add", "base.txt"], cwd=directory, env=env, check=True)

            evaluator = json.dumps([
                sys.executable,
                "-c",
                (
                    "import json,sys; "
                    "payload=sys.stdin.read(); "
                    "print(json.dumps({'passed': bool(payload), 'reason': ''}))"
                ),
            ])
            result = subprocess.run(
                [
                    sys.executable,
                    str(CLI),
                    "staged",
                    "--evaluator-command",
                    evaluator,
                ],
                cwd=directory,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_supervise_detects_mutation_and_terminates_owned_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            env = self._init_repo(directory)
            code = (
                "from pathlib import Path; import time; "
                "Path('base.txt').write_text('base\\n' + 'x\\n' * 20); "
                "time.sleep(20)"
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(CLI),
                    "supervise",
                    "--max-lines",
                    "1",
                    "--timeout",
                    "5",
                    "--interval",
                    "0.02",
                    "--",
                    sys.executable,
                    "-c",
                    code,
                ],
                cwd=directory,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("supervise workspace violation", result.stderr)
            self.assertIn("process group terminated", result.stderr)

    @unittest.skipUnless(os.name == "nt", "Windows-specific process contract")
    def test_windows_init_hook_blocks_invalid_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            env = self._init_repo(directory)

            init = subprocess.run(
                [
                    sys.executable,
                    str(CLI),
                    "init",
                    "--max-files",
                    "5",
                    "--max-lines",
                    "50",
                ],
                cwd=directory,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(init.returncode, 0, init.stderr)

            hook = Path(directory) / ".git" / "hooks" / "pre-commit"
            self.assertTrue(hook.is_file())
            hook_text = hook.read_text(encoding="utf-8")
            self.assertIn("# jev0 managed pre-commit hook", hook_text)
            self.assertIn(" staged ", hook_text)

            model = Path(directory) / "weights.gguf"
            model.write_text("model\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", "-f", "weights.gguf"],
                cwd=directory,
                env=env,
                check=True,
            )
            commit = subprocess.run(
                ["git", "commit", "-qm", "blocked"],
                cwd=directory,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertNotEqual(commit.returncode, 0)
            self.assertIn("model artifact", commit.stderr)

    @unittest.skipUnless(os.name == "nt", "Windows-specific process contract")
    def test_windows_doctor_reports_runtime_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            env = self._init_repo(directory)
            result = subprocess.run(
                [sys.executable, str(CLI), "doctor", "--json"],
                cwd=directory,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["runtime_ready"])
            self.assertFalse(payload["posix_process_groups"])

    @unittest.skipUnless(os.name == "nt", "Windows-specific process contract")
    def test_windows_controller_is_selected(self):
        sys.path.insert(0, str(ROOT))
        import jev0  # type: ignore

        self.assertEqual(
            type(jev0.process_controller()).__name__,
            "WindowsProcessController",
        )
        self.assertTrue(hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"))


if __name__ == "__main__":
    unittest.main()
