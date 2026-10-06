from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
JEV0 = ROOT / "jev0.py"


class SuperviseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name)
        self.env = dict(
            os.environ,
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
        )
        subprocess.run(["git", "init", "-q"], cwd=self.repo, env=self.env, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=self.repo,
            env=self.env,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=self.repo,
            env=self.env,
            check=True,
        )
        (self.repo / "base.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "add", "base.txt"], cwd=self.repo, env=self.env, check=True)
        subprocess.run(
            ["git", "commit", "-qm", "base"],
            cwd=self.repo,
            env=self.env,
            check=True,
        )

    def tearDown(self):
        self.temp.cleanup()

    def cli(self, *args, timeout=10):
        return subprocess.run(
            [sys.executable, str(JEV0), *args],
            cwd=self.repo,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )

    def test_supervise_allows_clean_command(self):
        result = self.cli(
            "supervise",
            "--timeout",
            "2",
            "--interval",
            "0.02",
            "--",
            sys.executable,
            "-c",
            "print('ok')",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "ok\n")

    def test_supervise_refuses_dirty_baseline_before_spawn(self):
        (self.repo / "base.txt").write_text("dirty\n", encoding="utf-8")
        marker = self.repo / "spawned.txt"
        result = self.cli(
            "supervise",
            "--timeout",
            "2",
            "--",
            sys.executable,
            "-c",
            "from pathlib import Path; Path('spawned.txt').write_text('x')",
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("requires a clean workspace", result.stderr)
        self.assertFalse(marker.exists())

    def test_supervise_kills_owned_group_on_workspace_violation(self):
        code = (
            "from pathlib import Path; import time; "
            "Path('base.txt').write_text('base\\n' + 'x\\n' * 20); "
            "time.sleep(5)"
        )
        result = self.cli(
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
            timeout=8,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("supervise workspace violation", result.stderr)
        self.assertIn("process group terminated", result.stderr)
        self.assertIn("exceed budget", result.stderr)

    def test_supervise_timeout_terminates_group(self):
        started = time.monotonic()
        result = self.cli(
            "supervise",
            "--timeout",
            "0.2",
            "--interval",
            "0.02",
            "--",
            sys.executable,
            "-c",
            "import time; time.sleep(5)",
        )
        elapsed = time.monotonic() - started
        self.assertEqual(result.returncode, 1)
        self.assertIn("command exceeded", result.stderr)
        self.assertIn("process group terminated", result.stderr)
        self.assertLess(elapsed, 3)

    def test_supervise_failure_capture_is_entropy_compatible_shape(self):
        code = (
            "from pathlib import Path; import time; "
            "Path('base.txt').write_text('base\\n' + 'x\\n' * 20); "
            "time.sleep(5)"
        )
        result = self.cli(
            "supervise",
            "--max-lines",
            "1",
            "--timeout",
            "5",
            "--interval",
            "0.02",
            "--capture-failure",
            "--",
            sys.executable,
            "-c",
            code,
            timeout=8,
        )
        self.assertEqual(result.returncode, 1)
        directory = self.repo / ".git" / "jev0" / "failures"
        records = sorted(directory.glob("*.json"))
        self.assertEqual(len(records), 1)
        record = json.loads(records[0].read_text(encoding="utf-8"))
        self.assertEqual(record["action"], "supervise")
        self.assertFalse(record["raw_diff_captured"])


    def test_supervise_allows_policy_valid_dirty_tracked_baseline(self):
        (self.repo / "base.txt").write_text("base\nchanged\n", encoding="utf-8")
        result = self.cli(
            "supervise",
            "--allow-dirty-baseline",
            "--max-lines",
            "10",
            "--timeout",
            "2",
            "--",
            sys.executable,
            "-c",
            "print('ok')",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "ok\n")

    def test_supervise_dirty_baseline_must_already_pass_policy(self):
        (self.repo / "base.txt").write_text("base\n" + "x\n" * 20, encoding="utf-8")
        marker = self.repo / "spawned.txt"
        result = self.cli(
            "supervise",
            "--allow-dirty-baseline",
            "--max-lines",
            "1",
            "--timeout",
            "2",
            "--",
            sys.executable,
            "-c",
            "from pathlib import Path; Path('spawned.txt').write_text('x')",
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("exceed budget", result.stderr)
        self.assertFalse(marker.exists())

    def test_supervise_dirty_baseline_still_rejects_untracked_files(self):
        (self.repo / "new.txt").write_text("new\n", encoding="utf-8")
        result = self.cli(
            "supervise",
            "--allow-dirty-baseline",
            "--timeout",
            "2",
            "--",
            sys.executable,
            "-c",
            "print('never')",
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("untracked file requires staging/review", result.stderr)
        self.assertEqual(result.stdout, "")



    def test_supervise_allows_bounded_untracked_text_when_opted_in(self):
        code = (
            "from pathlib import Path; import time; "
            "Path('src').mkdir(exist_ok=True); "
            "Path('src/new.py').write_text('one\\ntwo\\n'); "
            "time.sleep(0.15)"
        )
        result = self.cli(
            "supervise",
            "--allow-untracked",
            "--allow",
            "src",
            "--max-files",
            "1",
            "--max-lines",
            "2",
            "--timeout",
            "2",
            "--interval",
            "0.02",
            "--",
            sys.executable,
            "-c",
            code,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.repo / "src" / "new.py").exists())

    def test_supervise_untracked_text_counts_toward_line_budget(self):
        code = (
            "from pathlib import Path; import time; "
            "Path('new.txt').write_text('one\\ntwo\\n'); "
            "time.sleep(5)"
        )
        result = self.cli(
            "supervise",
            "--allow-untracked",
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
            timeout=8,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("2 added/deleted lines exceed budget 1", result.stderr)

    def test_supervise_untracked_files_count_toward_file_budget(self):
        code = (
            "from pathlib import Path; import time; "
            "Path('one.txt').write_text('1\\n'); "
            "Path('two.txt').write_text('2\\n'); "
            "time.sleep(5)"
        )
        result = self.cli(
            "supervise",
            "--allow-untracked",
            "--max-files",
            "1",
            "--timeout",
            "5",
            "--interval",
            "0.02",
            "--",
            sys.executable,
            "-c",
            code,
            timeout=8,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("2 workspace files exceed budget 1", result.stderr)

    def test_supervise_untracked_binary_still_fails_closed(self):
        code = (
            "from pathlib import Path; import time; "
            "Path('blob.dat').write_bytes(b'abc\\x00def'); "
            "time.sleep(5)"
        )
        result = self.cli(
            "supervise",
            "--allow-untracked",
            "--timeout",
            "5",
            "--interval",
            "0.02",
            "--",
            sys.executable,
            "-c",
            code,
            timeout=8,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("binary change requires separate review", result.stderr)

    def test_supervise_untracked_model_artifact_still_fails_closed(self):
        code = (
            "from pathlib import Path; import time; "
            "Path('weights.gguf').write_text('not really a model\\n'); "
            "time.sleep(5)"
        )
        result = self.cli(
            "supervise",
            "--allow-untracked",
            "--timeout",
            "5",
            "--interval",
            "0.02",
            "--",
            sys.executable,
            "-c",
            code,
            timeout=8,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("model artifact", result.stderr)



    def test_supervise_allows_committed_change_within_pinned_budget(self):
        code = (
            "from pathlib import Path; import subprocess; "
            "Path('base.txt').write_text('base\\nchanged\\n'); "
            "subprocess.run(['git','add','base.txt'], check=True); "
            "subprocess.run(['git','commit','-qm','agent change'], check=True)"
        )
        result = self.cli(
            "supervise",
            "--max-lines",
            "2",
            "--timeout",
            "3",
            "--interval",
            "0.02",
            "--",
            sys.executable,
            "-c",
            code,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_supervise_blocks_commit_laundering_against_pinned_baseline(self):
        code = (
            "from pathlib import Path; import subprocess, time; "
            "Path('base.txt').write_text('base\\n' + 'x\\n' * 20); "
            "subprocess.run(['git','add','base.txt'], check=True); "
            "subprocess.run(['git','commit','-qm','launder change'], check=True); "
            "time.sleep(0.2)"
        )
        result = self.cli(
            "supervise",
            "--max-lines",
            "1",
            "--timeout",
            "3",
            "--interval",
            "0.02",
            "--",
            sys.executable,
            "-c",
            code,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("supervise workspace violation", result.stderr)
        self.assertIn("exceed budget", result.stderr)



if __name__ == "__main__":
    unittest.main()
