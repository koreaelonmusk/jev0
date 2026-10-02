from pathlib import Path
import json
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
JEV0 = ROOT / "jev0.py"


class FailureCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name)
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.repo, check=True)
        (self.repo / "base.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "add", "base.txt"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=self.repo, check=True)

    def tearDown(self):
        self.temp.cleanup()

    def cli(self, *args):
        return subprocess.run(
            [sys.executable, str(JEV0), *args],
            cwd=self.repo,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        )

    def records(self):
        directory = self.repo / ".git" / "jev0" / "failures"
        return sorted(directory.glob("*.json")) if directory.exists() else []

    def trigger_block(self):
        (self.repo / "base.txt").write_text("base\n" + "x\n" * 10, encoding="utf-8")
        subprocess.run(["git", "add", "base.txt"], cwd=self.repo, check=True)
        result = self.cli("staged", "--max-lines", "1", "--capture-failure")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(len(result.stderr.splitlines()), 1)
        return result

    def test_capture_is_raw_diff_free_and_content_addressed(self):
        self.trigger_block()
        records = self.records()
        self.assertEqual(len(records), 1)
        document = json.loads(records[0].read_text(encoding="utf-8"))
        self.assertEqual(document["schema_version"], 1)
        self.assertEqual(document["kind"], "jev0-failure")
        self.assertEqual(document["action"], "staged")
        self.assertFalse(document["raw_diff_captured"])
        self.assertNotIn("diff", document)
        self.assertRegex(document["failure_id"], r"^[0-9a-f]{64}$")
        self.assertEqual(records[0].stem, document["failure_id"])
        self.assertNotIn(str(self.repo), records[0].read_text(encoding="utf-8"))

    def test_repeated_identical_failure_is_idempotent(self):
        self.trigger_block()
        self.trigger_block()
        self.assertEqual(len(self.records()), 1)

    def test_capture_is_opt_in(self):
        (self.repo / "base.txt").write_text("base\n" + "x\n" * 10, encoding="utf-8")
        subprocess.run(["git", "add", "base.txt"], cwd=self.repo, check=True)
        result = self.cli("staged", "--max-lines", "1")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.records(), [])

    def test_failures_list_and_show_verify_record(self):
        self.trigger_block()
        record = json.loads(self.records()[0].read_text(encoding="utf-8"))
        listed = self.cli("failures", "list", "--json")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        payload = json.loads(listed.stdout)
        self.assertEqual([item["failure_id"] for item in payload], [record["failure_id"]])

        shown = self.cli("failures", "show", record["failure_id"])
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertEqual(json.loads(shown.stdout), record)

    def test_capture_failure_never_changes_original_block_exit(self):
        result = self.trigger_block()
        self.assertIn("line budget exceeded", result.stderr.lower())


if __name__ == "__main__":
    unittest.main()
