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
