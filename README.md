# jev0

**Keep AI-assisted changes small. Stop commands that run too long.**

A local CLI for macOS and Linux. No model downloads, API keys, Python packages,
network calls, or background service. Requires Python 3.9+ and Git.

## Start

From a reviewed checkout:

```sh
git clone https://github.com/koreaelonmusk/jev0.git
cd jev0
python3 -m unittest discover -s tests -v
sh install.sh
export PATH="$HOME/.local/bin:$PATH"

cd /path/to/your/project
jev0 init
```

`init` installs a repository-local pre-commit hook. Existing hooks and custom
`core.hooksPath` settings are preserved: integrate `jev0 staged` manually in
those cases. Run `init` again with the same arguments safely. No shell profiles,
agent instructions, or global Git settings are edited.

## Core commands

```sh
# Check all tracked changes against HEAD, including unstaged edits.
jev0 workspace

# Check the index, including forcibly staged ignored files.
jev0 staged

# Run Layer 0, then an explicit external Layer 1 evaluator.
jev0 staged --evaluator-command '["python3","./evaluate.py"]'

# Persist an explicit scope and budget in a project's new hook.
jev0 init --allow src --allow tests --max-files 10 --max-lines 300

# Bound one foreground command and its ordinary child processes.
jev0 run --timeout 30 -- python3 -m unittest discover -s tests

# Inspect runtime, executable hash, repository, and hook state without mutation.
jev0 doctor
jev0 doctor --json

# Validate and fingerprint one explicit Layer 0 policy manifest.
jev0 policy .jev0.json
jev0 policy .jev0.json --json
```

| Check | Default |
| --- | --- |
| Changed paths | At most 20 |
| Added + deleted lines | At most 500 |
| Allowed paths | All; repeat `--allow` to restrict to exact files or directory trees |
| Model artifacts | Reject added/modified `*.gguf`, `*.bin`, and files under `models/` (case insensitive) |
| Binary changes | Reject added/modified files Git reports as binary |

Paths are relative to the repository root, even from a subdirectory. Renames
count as deletion plus addition; both paths must be in scope. Removing an old
binary/model is allowed. Git's diff configuration and attributes affect text
counts and binary classification. Budgets are review policies, not semantic
proof that a change is necessary or correct.

`workspace` checks tracked working-tree state against HEAD and blocks if untracked
files exist until they are explicitly staged for review. `staged` checks only the
Git index. Both succeed silently with exit **0**, or block with exit **1** and one
stderr line. Files and index entries are never rolled back or discarded.
`run` forwards output and the command's exit status; timeout or launch failure
returns **1**. Invalid CLI arguments return **2**.

## Versioned Layer 0 policy

Use an explicit JSON manifest when multiple people or agents need the same
deterministic Layer 0 budgets:

```json
{
  "schema_version": 1,
  "max_files": 10,
  "max_lines": 300,
  "allow": ["src", "tests", "README.md"]
}
```

```sh
jev0 workspace --policy .jev0.json
jev0 staged --policy .jev0.json
jev0 init --policy .jev0.json
```

Policy loading is deliberately **explicit**. jev0 never auto-discovers
`.jev0.json` or another repository file. Relative policy paths are resolved from
the repository root, so agent working-directory changes cannot select a different
policy accidentally. The manifest is bounded to 64 KiB, must resolve to a file
inside the repository, rejects unknown or duplicate keys, and currently permits
only `schema_version`, `max_files`, `max_lines`, and `allow`.
Evaluator commands are never loaded from repository policy files.

`--policy` cannot be combined with `--max-files`, `--max-lines`, or
`--allow`; this avoids ambiguous precedence. Use `jev0 policy <path> --json`
to validate a manifest and retrieve its normalized effective values plus SHA-256.

`jev0 init --policy ...` validates the manifest, snapshots its effective Layer 0
values into the managed hook, and records the source policy SHA-256 in a comment.
Later edits to the policy file therefore cannot silently weaken an already-installed
hook. `jev0 doctor` exposes that snapshot fingerprint as `hook_policy_sha256`.
Recreate the managed hook deliberately when adopting a new policy.

See [examples/policy.json](examples/policy.json) for a copyable manifest.

### Detect policy drift

After installing a managed hook from a manifest, verify that the current manifest
still matches both the recorded fingerprint and the hook's actual Layer 0 arguments:

```sh
jev0 policy-check .jev0.json
jev0 policy-check .jev0.json --json
```

Exit **0** means the manifest, snapshot fingerprint, and enforced
`max_files/max_lines/allow` values are all in sync. Exit **1** reports drift,
including a changed manifest, a missing fingerprint, a missing/stale hook, or
manual hook argument tampering. This command is read-only and is suitable for CI.

## What this actually guarantees

The deterministic guard itself uses **zero LLM tokens**. It does not guarantee zero
agent token spend. A pre-commit hook runs after editing and staging; it cannot
prevent earlier API calls. `jev0 workspace` can be invoked before completion to
cover tracked edits that were never staged, but invocation is still advisory unless
an external system enforces it. Hooks can also be skipped with `--no-verify`.

`run` is an explicit timeout wrapper, **not an OS interceptor or sandbox**. It
executes the command you provide without shell expansion. It does not classify
commands as safe, prevent destructive actions, or stop retries by the calling
agent. On timeout it kills the command's POSIX process group. A process that
creates another session can escape that group. No Windows support yet.

Rules files are guidance, not enforcement. No file watcher, semantic classifier,
or universal tool integration is claimed. Cold-start latency is measured, not
promised to be 15ms. Local agents with write access can change or remove the guard.
Use a real sandbox and externally enforced CI policy for an adversarial boundary.

## Agent guidance

You can add this to the instruction file your agent actually reads:

> Change only the requested scope. Avoid unrelated refactors and new dependencies.
> Run the relevant tests and `jev0 staged`; report the exact commands and results.
> If a guard rejects an operation, stop and explain it instead of retrying or bypassing it.

Cursor, Claude Code, Codex, and Windsurf can use this CLI wherever they can run
local commands. Automatic interception in these tools has not been tested.
See [docs/UNIVERSAL.md](docs/UNIVERSAL.md) for the vendor-neutral integration contract.

## Doctor

`jev0 doctor` is read-only. Its JSON output includes `schema_version` for machine
consumers. It reports the exact executable path and SHA-256,
Python/Git/platform information, whether the current directory is in a Git
repository, and whether a jev0-managed pre-commit hook is actually executable.

Use `jev0 doctor --json` for scripts and support reports. `runtime_ready`
means the local runtime satisfies jev0's current Python/Git/POSIX requirements;
`hook_enforced` is separate and only reports whether the current repository has
an executable jev0-managed pre-commit hook. When a hook was created from a policy
manifest, `hook_policy_sha256` records the manifest fingerprint captured at
initialization time. A true enforcement value is not a sandbox claim.

## Server-side range guard

Use `range` in CI to validate the full pull-request change set instead of only a
developer's staged or working-tree state:

```sh
jev0 range <base-ref> <head-ref> --policy .jev0.json
```

jev0 resolves both refs to commit IDs first, then evaluates the merge-base diff
(`base...head`) with the same Layer 0 rules used locally. This matches pull-request
semantics: unrelated commits added to the base branch after the feature branch was
created are not charged to the feature.

The command accepts the same explicit policy and optional Layer 1 evaluator flags
as `staged` and `workspace`. It does not fetch missing refs. CI must check out or
fetch the base/head commits before calling it. Ref strings are resolved before
being used in a diff so option-like user input is not passed directly to
`git diff`.

Example GitHub Actions usage after fetching the base commit:

```sh
jev0 range "$BASE_SHA" "$HEAD_SHA" --base-policy .github/jev0-policy.json
```

For server enforcement, prefer `--base-policy` over `--policy`. The policy blob
is read from the already-resolved base commit, not from the pull-request head.
That prevents a PR from weakening its own limits and then using those weaker
limits to approve itself. Base-policy reads are bounded to 64 KiB and fail closed
when the file is missing, invalid, oversized, or outside the supported schema.

This repository's workflow also exposes one stable aggregate check named
`jev0 gate`. After the feature is merged, configure the main-branch ruleset to
require `jev0 gate`; the gate succeeds only when the full macOS/Linux × Python
matrix succeeds.

This is the server-enforced companion to the local hook: skipping `pre-commit`
does not bypass a required CI range check.

### Range evidence

Use `range-report` when CI, audit, or incident review needs a machine-readable
record of the exact decision inputs:

```sh
jev0 range-report "$BASE_SHA" "$HEAD_SHA" \
  --base-policy .github/jev0-policy.json
```

The JSON schema is versioned and includes:

- resolved base and head commit SHAs,
- merge-base SHA,
- policy source/path/SHA-256,
- verifier version and exact jev0 source SHA-256,
- canonical evidence SHA-256,
- effective `max_files`, `max_lines`, and `allow`,
- changed file count,
- added + deleted line count,
- changed paths,
- final `allow` / `block` decision, and
- the deterministic block reason when rejected.

`range-report` and `range` share the same Layer 0 change-set analysis and
enforcement function, so the report is evidence of the same decision rather than
a second implementation of the rules.

The evidence digest is a deterministic integrity identifier over the canonical
JSON fields before `evidence_sha256` is added. It is **not** a digital signature
and does not by itself authenticate who produced the report. Authenticity still
depends on the trusted CI boundary, protected base branch, and verifier provenance.


The trusted GitHub policy gate also preserves the canonical JSON as a direct
Actions artifact for 30 days. The workflow records GitHub's artifact ID, URL, and
artifact SHA-256 in the Step Summary. The artifact digest protects the uploaded
file object, while `evidence_sha256` protects the canonical evidence fields;
they are intentionally separate integrity layers.


The human-facing Step Summary is rendered by a tested helper rather than inline
workflow code. Repository-controlled text such as block reasons is forced onto a
single line, bounded in length, and HTML-escaped before rendering so filenames or
error text cannot inject headings, links, or raw HTML into the summary.

Range metadata is resource-bounded before parsing: jev0 reads at most 8 MiB
from each Git metadata stream and gives bounded Git stream reads a finite
wall-clock deadline. Trusted base-policy reads use a shorter dedicated deadline.
Evidence JSON includes at most 1000 changed paths while preserving `paths_total`
and `paths_truncated`, so extremely large or stalled change sets cannot force
unbounded memory growth or an indefinite wait.

### Verify persisted evidence

Persisted evidence can be checked later without recomputing the Git diff:

```sh
jev0 evidence-verify evidence.json
jev0 evidence-verify evidence.json --json
jev0 evidence-verify evidence.json --require-current-verifier
```

The verifier accepts at most 1 MiB of UTF-8 JSON, enforces the evidence schema,
recomputes the canonical `evidence_sha256`, validates path/count invariants, and
reports whether the recorded verifier SHA-256 matches the currently running
`jev0.py`. `--require-current-verifier` turns a verifier mismatch into exit 1.

This verifies deterministic integrity, not authorship. A valid digest proves only
that the evidence document is internally self-consistent; provenance still
depends on where the evidence was produced and how that CI boundary is protected.

Use `--repo-check` when the original repository objects are available:

```sh
jev0 evidence-verify evidence.json --repo-check
```

Repository checking resolves the recorded base/head commits, recomputes the
merge base and range statistics, re-applies the recorded Layer 0 policy, and
verifies base/worktree policy provenance when present. This distinguishes an
internally self-consistent JSON document from evidence that actually describes
the current repository object graph.

For portable evidence, file-backed `policy_path` values are stored as
repository-relative POSIX paths rather than absolute local paths. This avoids
leaking workstation usernames/directories and lets the same evidence be checked
after the repository is moved or cloned elsewhere. Evidence JSON parsing is
strict and rejects duplicate keys at any object depth.

### Verify an evidence chain

Multiple range reports can be verified as one contiguous history:

```sh
jev0 evidence-chain-verify evidence-1.json evidence-2.json evidence-3.json
jev0 evidence-chain-verify evidence-*.json --repo-check --json
```

Every member is first subjected to the full `evidence-verify` validation. The
chain then requires each prior `head_sha` to equal the next `base_sha`, rejects
duplicate evidence digests, and emits a deterministic `chain_sha256` over the
ordered member digests. This detects reordered, duplicated, missing, or tampered
segments when a continuous verification history is expected.

The chain digest is an integrity identifier, not a signature or timestamp.
Authenticity still depends on the trusted CI/artifact boundary that produced and
retained each member. Use `--repo-check` when the referenced Git objects are
available to revalidate every segment against repository state.

## GitHub ruleset audit

Export a repository ruleset with GitHub CLI or the REST API, then audit it
locally without giving jev0 network credentials:

```sh
gh api repos/OWNER/REPO/rulesets/RULESET_ID > ruleset.json
jev0 ruleset-check ruleset.json --json
```

For a single-maintainer repository, add `--solo`:

```sh
jev0 ruleset-check ruleset.json --solo
```

Solo mode treats approval requirements that need another actor as blocking
errors. The audit also cross-checks the current repository for CODEOWNERS and
stable local workflow checks such as `jev0 gate` and `jev0 policy gate`.

It detects configurations including:

- required approvals or last-push approval that deadlock solo maintenance,
- Code Owner review with no CODEOWNERS file,
- a Required status checks rule with an empty check list,
- repository gate checks that exist locally but are not required by the ruleset,
- merge commits offered while linear history is required,
- missing pull-request, deletion, or force-push protection.

The command is read-only. It does not update GitHub settings. See
[examples/github-ruleset-solo.json](examples/github-ruleset-solo.json) for a
known-good solo baseline.

## Universal workflow

A practical tool-agnostic loop is:

```sh
# after an agent edits files
jev0 workspace

# before commit
jev0 staged

# for bounded test/build commands
jev0 run --timeout 120 -- python3 -m unittest discover -s tests -v
```

This gives one stable CLI contract across terminal-capable coding agents without
pretending every vendor exposes the same plugin or hook API.

## Verify and measure

```sh
python3 -m unittest discover -s tests -v
python3 scripts/benchmark.py
```

Tests create disposable Git repositories and cover scope boundaries, budgets,
model/binary rejection, staged versus unstaged content, non-destructive failures,
hook preservation, literal argument passing, and timeout cleanup.
CI runs the same suite on macOS/Linux with Python 3.9/3.13. Benchmark output includes
platform, Python version, workload, sample count, and process-launch latency.
It measures the guard, not token savings or model inference.

Baseline measurement at commit `9279307`: **54.43ms median / 64.45ms p95** on
macOS 26.4.1, Apple Silicon arm64, Python 3.14.4; 50 fresh CLI processes checking
one staged text file with one added line, with OS caches retained. This is a
local Layer 0 measurement, not a Linux result or a latency guarantee. Linux CI
verifies correctness; Linux latency has not been measured here. Interpreter,
Git, and policy overhead were not timed separately.

## Optional Layer 1 evaluators

Layer 0 remains the default: deterministic checks on structured Git metadata.
Passing Layer 0 means the configured rules passed, not that code is bug-free.
The optional Python API defines one structural interface in `jev0.py`:

```python
class GuardEvaluator(Protocol):
    def evaluate(self, diff_text: str) -> tuple[bool, str]: ...
```

Call `staged(args, evaluator=your_evaluator)` to run a trusted evaluator **after**
Layer 0 passes. `args` supplies `max_files`, `max_lines`, and `allow`, just as the
CLI does. Any object implementing `evaluate` satisfies the interface; inheritance
is unnecessary. Without an evaluator, no patch is generated and no model is loaded.

The evaluator receives the staged unified text diff (UTF-8, undecodable bytes
replaced), never unstaged contents. It returns `(True, reason)` to allow or
`(False, reason)` to block. Exceptions and malformed results block; no fallback
can override Layer 0. Git metadata checks retain their own structured input
rather than reparsing the text patch. The index must remain unchanged throughout
the check; this API does not lock out concurrent staging.

This is an API slot, not a shipped GGUF integration. There is no model, CLI plugin
loader or automatic discovery. In-process evaluators execute trusted Python code
and must manage their own resources and inference limits.

The CLI can run a trusted evaluator process after Layer 0:

```sh
jev0 staged \
  --evaluator-command '["python3","./evaluate.py"]' \
  --evaluator-timeout 30 \
  --max-diff-bytes 1000000 \
  --max-evaluator-output-bytes 4096
```

`--evaluator-command` is a JSON array of literal arguments. No shell parses it.
The process receives the staged UTF-8 diff on stdin and must write exactly one
JSON value to stdout:

```json
{"passed": false, "reason": "change is unrelated to the requested scope"}
```

The object must contain only `passed` (boolean) and `reason` (string), and exit
zero. Rejection, timeout, launch failure, nonzero exit, malformed output, excess
diff size, or excess response size blocks the check. On timeout or interruption,
jev0 kills the evaluator's POSIX process group with bounded cleanup. As with
`jev0 run`, a process that starts another session can escape that group.

Evaluator stdout/stderr are drained incrementally with bounded in-memory
buffers. Stdout is rejected as soon as it exceeds the configured response limit;
stderr diagnostics retain only the configured prefix. Input diff bytes are also
bounded before evaluator launch. This is still a protocol/resource guard, not a
sandbox: a malicious evaluator can consume CPU or create detached processes until
the timeout or an external sandbox stops it.

Use the same options with `jev0 init` to persist the policy in a new pre-commit
hook. Commands using repository-relative paths run from the repository root.
The hook stores the literal command and absolute jev0/Python paths, so moving any
of them requires recreating the hook. Existing hooks remain preserved.

This protocol can wrap a future GGUF evaluator without replacing Layer 0, but no
model runtime, weights, prompt, or calibrated classifier ships here. The evaluator
command is trusted code with the user's permissions. Model quality, calibration,
latency, and resource consumption require separate measurement.

## Remove or update

`init` records absolute interpreter and executable paths. Keep those locations
available. To change hook settings or uninstall, inspect `.git/hooks/pre-commit`
(or the path reported by `git rev-parse --git-path hooks/pre-commit`) and remove
only the hook marked `# jev0 managed pre-commit hook`. Then rerun `init` if needed.
If the managed hook content is unchanged but its execute bit was lost, `jev0 init`
repairs that permission without replacing the file.

The installer creates a new executable atomically and never overwrites different
existing content or symlinks. Re-running it with identical jev0 content is safe and
repairs a missing execute bit. For an actual version update, review and remove the
installed `$HOME/.local/bin/jev0` before running `install.sh` again.
`JEV0_BIN_DIR` selects another installation directory.

`.gitignore` prevents accidental additions, not `git add -f` or previously tracked
files. The staged check is the additional gate. Broad `*.bin` exclusion is
intentional here; projects with legitimate binary assets need a separate review
path. Nothing removes existing data automatically.

## Contribute

[Report a reproducible bug](https://github.com/koreaelonmusk/jev0/issues) with the
command, expected result, actual result, and platform. Include a regression test
with behavior changes. Performance claims need a reproducible workload.
Do not include secrets in bug reports. License: [MIT](LICENSE).
