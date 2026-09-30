import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/evidence_summary.py"


class EvidenceSummaryTests(unittest.TestCase):
    def run_script(self, document):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            return subprocess.run(
                [sys.executable, str(SCRIPT), str(path)],
                capture_output=True, text=True, timeout=10,
            )

    def base_document(self):
        return {
            "decision": "allow",
            "base_sha": "a" * 40,
            "head_sha": "b" * 40,
            "merge_base_sha": "c" * 40,
            "policy_sha256": "d" * 64,
            "verifier_version": "0.3.0-dev",
            "verifier_sha256": "e" * 64,
            "evidence_sha256": "f" * 64,
            "files_changed": 1,
            "lines_changed": 2,
            "reason": None,
        }

    def test_render_is_single_line_safe_for_untrusted_reason(self):
        doc = self.base_document()
        doc["decision"] = "block"
        doc["reason"] = "bad\n### injected `<script>alert(1)</script>` & more"
        result = self.run_script(doc)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("\n### injected", result.stdout)
        self.assertIn("\\n### injected", result.stdout)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", result.stdout)
        self.assertNotIn("<script>", result.stdout)

    def test_render_bounds_long_fields(self):
        doc = self.base_document()
        doc["decision"] = "block"
        doc["reason"] = "x" * 5000
        result = self.run_script(doc)
        self.assertEqual(result.returncode, 0, result.stderr)
        reason_line = next(line for line in result.stdout.splitlines() if line.startswith("- reason:"))
        self.assertLess(len(reason_line), 600)
        self.assertIn("…", reason_line)

    def test_missing_required_field_fails_closed(self):
        doc = self.base_document()
        del doc["head_sha"]
        result = self.run_script(doc)
        self.assertEqual(result.returncode, 1)
        self.assertIn("evidence missing fields: head_sha", result.stderr)

    def test_oversized_input_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.json"
            with path.open("wb") as stream:
                stream.seek(1_048_576)
                stream.write(b"x")
            result = subprocess.run(
                [sys.executable, str(SCRIPT), str(path)],
                capture_output=True, text=True, timeout=10,
            )
        self.assertEqual(result.returncode, 1)
        self.assertIn("evidence exceeds 1048576 bytes", result.stderr)


if __name__ == "__main__":
    unittest.main()
