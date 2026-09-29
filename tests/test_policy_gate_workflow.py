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

    def test_policy_gate_uses_pinned_actions(self):
        text = WORKFLOW.read_text()
        self.assertIn(
            'actions/checkout@11d5960a326750d5838078e36cf38b85af677262',
            text,
        )
        self.assertIn(
            'actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065',
            text,
        )


if __name__ == '__main__':
    unittest.main()
