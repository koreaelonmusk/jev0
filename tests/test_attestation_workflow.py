from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / '.github/workflows/attest-evidence.yml'


class AttestationWorkflowTests(unittest.TestCase):
    def test_signer_is_isolated_from_pr_head(self):
        text = WORKFLOW.read_text()
        self.assertIn('workflow_run:', text)
        self.assertIn('workflows: [policy-gate]', text)
        self.assertIn("github.event.workflow_run.conclusion == 'success'", text)
        self.assertIn("github.event.workflow_run.event == 'pull_request_target'", text)
        self.assertIn('ref: ${{ github.event.repository.default_branch }}', text)
        self.assertNotIn('github.event.pull_request.head.sha', text)
        self.assertNotIn('refs/pull/', text)
        self.assertNotIn('secrets.', text)

    def test_signer_has_minimal_required_privileges(self):
        text = WORKFLOW.read_text()
        self.assertIn('actions: read', text)
        self.assertIn('contents: read', text)
        self.assertIn('id-token: write', text)
        self.assertIn('attestations: write', text)
        self.assertNotIn('contents: write', text)
        self.assertNotIn('pull-requests: write', text)
        self.assertNotIn('packages: write', text)

    def test_source_artifacts_are_reverified_before_signing(self):
        text = WORKFLOW.read_text()
        self.assertIn('digest-mismatch: error', text)
        self.assertIn('provenance-verify "$PROVENANCE" "$EVIDENCE"', text)
        self.assertIn('--require-current-verifier', text)
        self.assertIn('--expect-repository "$REPOSITORY"', text)
        self.assertIn('--expect-workflow-ref "$EXPECTED_WORKFLOW_REF"', text)
        self.assertIn('--expect-run-id "$SOURCE_RUN_ID"', text)

    def test_signer_actions_are_immutable_pins(self):
        text = WORKFLOW.read_text()
        self.assertIn(
            'actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1',
            text,
        )
        self.assertIn(
            'actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c',
            text,
        )
        self.assertIn(
            'actions/attest@508db95dd578ae2727ebd6217d5ba78e4fbda05d',
            text,
        )
        self.assertIn(
            'actions/upload-artifact@bbbca2ddaa5d8feaa63e36b76fdaad77386f024f',
            text,
        )

    def test_attestation_binds_subject_and_custom_predicate(self):
        text = WORKFLOW.read_text()
        self.assertIn(
            'subject-path: ${{ runner.temp }}/jev0-attestation-input/jev0-range-evidence.json',
            text,
        )
        self.assertIn(
            'predicate-type: https://github.com/koreaelonmusk/jev0/attestation/provenance/v1',
            text,
        )
        self.assertIn(
            'predicate-path: ${{ runner.temp }}/jev0-attestation-input/jev0-ci-provenance.json',
            text,
        )
        self.assertIn('steps.attest.outputs.bundle-path', text)


if __name__ == '__main__':
    unittest.main()
