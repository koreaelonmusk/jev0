import json
import os
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "jev0.py"


class CapabilityContractTests(unittest.TestCase):
    def cli(self, *args):
        return subprocess.run(
            [sys.executable, str(CLI), *args],
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_json_contract_is_versioned_and_strict(self):
        result = self.cli("capabilities", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)

        self.assertEqual(
            set(data),
            {
                "schema_version",
                "version",
                "platform",
                "process_backend",
                "runtime_ready",
                "contracts",
                "commands",
                "features",
                "installers",
            },
        )
        self.assertEqual(data["schema_version"], 1)
        self.assertEqual(
            set(data["contracts"]),
            {
                "policy_schema_version",
                "failure",
                "evidence_schema_version",
                "provenance_schema_version",
            },
        )
        self.assertEqual(data["contracts"]["failure"], "jev0-failure/v1")
        self.assertEqual(
            set(data["commands"]),
            {
                "staged",
                "workspace",
                "range",
                "run",
                "supervise",
                "doctor",
                "capabilities",
            },
        )
        self.assertEqual(
            set(data["features"]),
            {
                "external_process_evaluator",
                "managed_pre_commit_hook",
                "failure_capture",
                "supervise_pinned_baseline",
                "supervise_allow_dirty_baseline",
                "supervise_allow_untracked",
                "range_evidence",
                "evidence_verification",
                "provenance_verification",
            },
        )
        self.assertEqual(
            set(data["installers"]),
            {"posix_shell", "windows_powershell"},
        )

    def test_contract_reports_current_supported_process_backend(self):
        result = self.cli("capabilities", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        expected = "windows" if os.name == "nt" else "posix"
        self.assertEqual(data["process_backend"], expected)
        self.assertTrue(data["commands"]["run"])
        self.assertTrue(data["features"]["external_process_evaluator"])

    def test_human_output_is_stable_flat_key_value_lines(self):
        result = self.cli("capabilities")
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertTrue(any(line == "schema_version: 1" for line in lines))
        self.assertTrue(any(line.startswith("process_backend: ") for line in lines))
        self.assertTrue(any(line == "commands.capabilities: yes" for line in lines))
        self.assertTrue(
            any(line == "features.supervise_pinned_baseline: yes" for line in lines)
        )
        self.assertTrue(
            any(line == "features.supervise_allow_untracked: yes" for line in lines)
        )


if __name__ == "__main__":
    unittest.main()
