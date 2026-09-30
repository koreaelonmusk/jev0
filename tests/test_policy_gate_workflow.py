from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / '.github/workflows/policy-gate.yml'


class PolicyGateWorkflowTests(unittest.TestCase):
    def test_policy_gate_keeps_pr_head_as_data_only(self):
        text = WORKFLOW.read_text()
        self.assertIn('pull_request_target:', text)
        self.assertIn('contents: read', text)
        self.assertNotIn('contents: write', text)
        self.assertNotIn('${{ secrets.', text)
        self.assertIn('ref: ${{ github.event.pull_request.base.sha }}', text)
        self.assertNotIn('ref: ${{ github.event.pull_request.head.sha }}', text)
        self.assertIn('persist-credentials: false', text)
        self.assertIn('refs/pull/${PR_NUMBER}/head:refs/remotes/jev0/pr-head', text)
        self.assertIn('EXPECTED_HEAD_SHA: ${{ github.event.pull_request.head.sha }}', text)
        self.assertIn('--base-policy .github/jev0-policy.json', text)
        self.assertIn('python3 jev0.py range-report', text)
        self.assertIn('cat "$EVIDENCE"', text)
        self.assertIn('GITHUB_STEP_SUMMARY', text)
        self.assertIn("data['verifier_version']", text)
        self.assertIn("data['verifier_sha256']", text)
        self.assertIn("data['evidence_sha256']", text)
        self.assertIn('exit "$STATUS"', text)

    def test_policy_gate_uses_pinned_actions(self):
        text = WORKFLOW.read_text()
        self.assertIn(
            'actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1',
            text,
        )
        self.assertIn(
            'actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97',
            text,
        )


if __name__ == '__main__':
    unittest.main()
