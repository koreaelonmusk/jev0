# Security policy

## Supported versions

jev0 is pre-1.0 software. Security fixes are made on the latest `main` branch.

## Reporting a vulnerability

Please do not open a public issue for a vulnerability that could enable command
execution, policy bypass, hook replacement, unsafe path handling, or destructive
behavior.

Instead, use GitHub's private vulnerability reporting for this repository when
available. Include:

- affected commit or version,
- operating system and Python version,
- exact command or minimal reproducer,
- expected behavior,
- observed behavior,
- impact,
- whether the issue requires a malicious repository or local write access.

Do not include secrets, private source code, model weights, or customer data.

## Security model

jev0 is a local policy guard, not a sandbox or privilege boundary. A process with
the same user permissions can modify files, remove hooks, invoke Git with
`--no-verify`, or replace the jev0 executable.

Use CI, protected branches, code review, and an OS/container sandbox when the
agent itself is not trusted.

## CI policy gate

The optional `policy-gate.yml` workflow uses `pull_request_target` so its
workflow definition, verifier, and policy come from the trusted base commit.
Because this event can carry elevated trust, the workflow deliberately:

- grants only `contents: read`,
- checks out only the pull request base SHA,
- never checks out or executes pull request head files,
- fetches the pull request head only as Git object data,
- verifies the fetched head SHA against the event payload, and
- runs the base commit's `jev0.py` with the base commit's policy.

Do not add build, test, package-install, shell-source, or other execution of
pull-request head content to this workflow. Untrusted code belongs in the normal
`pull_request` workflow with its restricted token.

## Evidence digests

Range evidence records the running jev0 source SHA-256 and a deterministic
SHA-256 over the canonical evidence fields. These hashes support reproducibility,
change detection, and audit correlation. They are not signatures, certificates,
or proof of authorship. Treat evidence as authenticated only when it was produced
inside a trusted CI boundary whose workflow, verifier, and policy come from the
protected base commit.

## Evidence artifact retention

The trusted policy gate uploads the canonical evidence JSON as a GitHub Actions
artifact using an immutable commit pin for the official upload action. The upload
runs even when the policy decision blocks the pull request, when an evidence file
was produced. No secrets or write permissions are granted to the workflow.

The GitHub artifact digest and jev0 `evidence_sha256` have different purposes:
the artifact digest identifies the uploaded file object, while
`evidence_sha256` covers the canonical evidence fields. Neither is a signature
or independent proof of authorship.

## Summary rendering

GitHub Step Summary output is a separate presentation boundary. Evidence fields
can contain repository-controlled filenames or diagnostic text, so summaries are
rendered by `scripts/evidence_summary.py` with bounded field lengths,
single-line normalization, and HTML escaping. Do not replace this with direct
interpolation of evidence strings into Markdown.

## Ruleset diagnostics

`jev0 ruleset-check` consumes an exported Ruleset JSON file and local repository
metadata. It does not authenticate to GitHub or mutate repository settings.
Recommendations are configuration diagnostics, not proof that GitHub has already
applied the suggested state. Re-fetch the Ruleset after changes and re-run the
audit to verify the server-side configuration.
