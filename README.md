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

## Three commands

```sh
# Check the index, including forcibly staged ignored files.
jev0 staged

# Run Layer 0, then an explicit external Layer 1 evaluator.
jev0 staged --evaluator-command '["python3","./evaluate.py"]'

# Persist an explicit scope and budget in a project's new hook.
jev0 init --allow src --allow tests --max-files 10 --max-lines 300

# Bound one foreground command and its ordinary child processes.
jev0 run --timeout 30 -- python3 -m unittest discover -s tests
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

`staged` succeeds silently with exit **0**, or blocks with exit **1** and one
stderr line. Files and index entries are never rolled back or discarded.
`run` forwards output and the command's exit status; timeout or launch failure
returns **1**. Invalid CLI arguments return **2**.

## What this actually guarantees

The guard itself uses **zero LLM tokens**. It does not guarantee zero agent token
spend. A pre-commit hook runs after editing and staging; it cannot prevent earlier
API calls. Agents need not commit, and hooks can be skipped with `--no-verify`.

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
diff size, or excess response size blocks the check. On timeout, jev0 kills the
evaluator's POSIX process group. As with `jev0 run`, a process that starts another
session can escape that group. The response-size check occurs after the process
exits; it is a protocol limit, not a memory sandbox for malicious evaluators.
Nonzero-exit stderr diagnostics are truncated to the configured response limit.

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
For updates, review and remove the installed `$HOME/.local/bin/jev0` before
running `install.sh` again. `JEV0_BIN_DIR` selects another installation directory.

`.gitignore` prevents accidental additions, not `git add -f` or previously tracked
files. The staged check is the additional gate. Broad `*.bin` exclusion is
intentional here; projects with legitimate binary assets need a separate review
path. Nothing removes existing data automatically.

## Contribute

[Report a reproducible bug](https://github.com/koreaelonmusk/jev0/issues) with the
command, expected result, actual result, and platform. Include a regression test
with behavior changes. Performance claims need a reproducible workload.
Do not include secrets in bug reports. License: [MIT](LICENSE).
