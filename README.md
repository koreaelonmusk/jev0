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
