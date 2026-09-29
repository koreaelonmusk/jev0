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
