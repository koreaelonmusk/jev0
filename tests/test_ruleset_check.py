import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "jev0.py"


class RulesetCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        self.env = dict(
            os.environ,
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
        )
        subprocess.run(["git", "init", "-q"], cwd=self.repo, env=self.env, check=True)
        workflows = self.repo / ".github" / "workflows"
        workflows.mkdir(parents=True)
        (workflows / "test.yml").write_text(
            "jobs:\n  gate:\n    name: jev0 gate\n"
        )
        (workflows / "policy-gate.yml").write_text(
            "jobs:\n  policy:\n    name: jev0 policy gate\n"
        )

    def cli(self, *args):
        return subprocess.run(
            [sys.executable, str(CLI), *args],
            cwd=self.repo,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def write_ruleset(self, document):
        path = self.repo / "ruleset.json"
        path.write_text(json.dumps(document))
        return path

    def healthy_ruleset(self):
        return {
            "name": "protect-main",
            "enforcement": "active",
            "conditions": {
                "ref_name": {
                    "include": ["~DEFAULT_BRANCH"],
                    "exclude": [],
                }
            },
            "rules": [
                {"type": "deletion"},
                {"type": "non_fast_forward"},
                {
                    "type": "pull_request",
                    "parameters": {
                        "required_approving_review_count": 0,
                        "require_code_owner_review": False,
                        "require_last_push_approval": False,
                        "require_extra_approval_for_unattributed_changes": False,
                        "required_review_thread_resolution": True,
                        "allowed_merge_methods": ["squash", "rebase"],
                    },
                },
                {"type": "required_linear_history"},
                {
                    "type": "required_status_checks",
                    "parameters": {
                        "strict_required_status_checks_policy": False,
                        "required_status_checks": [
                            {"context": "jev0 gate"},
                            {"context": "jev0 policy gate"},
                        ],
                    },
                },
            ],
        }

    def test_healthy_solo_ruleset_passes(self):
        path = self.write_ruleset(self.healthy_ruleset())
        result = self.cli("ruleset-check", str(path), "--solo", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertTrue(data["healthy"])
        self.assertEqual(data["errors"], 0)
        self.assertEqual(data["configured_required_checks"], [
            "jev0 gate",
            "jev0 policy gate",
        ])
        self.assertEqual(data["discovered_checks"], [
            "jev0 policy gate",
            "jev0 gate",
        ])

    def test_current_style_ruleset_reports_real_contradictions(self):
        document = self.healthy_ruleset()
        pull = next(
            rule["parameters"]
            for rule in document["rules"]
            if rule["type"] == "pull_request"
        )
        pull.update({
            "require_code_owner_review": True,
            "require_last_push_approval": True,
            "require_extra_approval_for_unattributed_changes": True,
            "allowed_merge_methods": ["merge", "squash", "rebase"],
        })
        status = next(
            rule["parameters"]
            for rule in document["rules"]
            if rule["type"] == "required_status_checks"
        )
        status["required_status_checks"] = []
        path = self.write_ruleset(document)

        result = self.cli("ruleset-check", str(path), "--solo", "--json")
        self.assertEqual(result.returncode, 1)
        data = json.loads(result.stdout)
        codes = {item["code"] for item in data["findings"]}
        self.assertIn("solo_review_deadlock", codes)
        self.assertIn("code_owner_review_without_codeowners", codes)
        self.assertIn("required_status_checks_empty", codes)
        self.assertIn("repository_gates_not_required", codes)
        self.assertIn("merge_method_conflicts_with_linear_history", codes)

    def test_general_mode_downgrades_solo_specific_deadlock(self):
        document = self.healthy_ruleset()
        pull = next(
            rule["parameters"]
            for rule in document["rules"]
            if rule["type"] == "pull_request"
        )
        pull["require_last_push_approval"] = True
        path = self.write_ruleset(document)
        result = self.cli("ruleset-check", str(path), "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertTrue(data["healthy"])
        codes = {item["code"] for item in data["findings"]}
        self.assertIn("last_push_approval_with_zero_approvals", codes)

    def test_codeowners_file_clears_missing_codeowners_warning(self):
        document = self.healthy_ruleset()
        pull = next(
            rule["parameters"]
            for rule in document["rules"]
            if rule["type"] == "pull_request"
        )
        pull["require_code_owner_review"] = True
        codeowners = self.repo / ".github" / "CODEOWNERS"
        codeowners.write_text("* @example-owner\n")
        path = self.write_ruleset(document)
        result = self.cli("ruleset-check", str(path), "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        codes = {item["code"] for item in data["findings"]}
        self.assertNotIn("code_owner_review_without_codeowners", codes)

    def test_ruleset_input_is_bounded(self):
        path = self.repo / "ruleset.json"
        with path.open("wb") as stream:
            stream.seek(1_048_576)
            stream.write(b"x")
        result = self.cli("ruleset-check", str(path))
        self.assertEqual(result.returncode, 1)
        self.assertIn("ruleset exceeds 1048576 bytes", result.stderr)

    def test_ruleset_requires_rules_array(self):
        path = self.write_ruleset({"name": "broken"})
        result = self.cli("ruleset-check", str(path))
        self.assertEqual(result.returncode, 1)
        self.assertIn("ruleset rules must be an array", result.stderr)


if __name__ == "__main__":
    unittest.main()
