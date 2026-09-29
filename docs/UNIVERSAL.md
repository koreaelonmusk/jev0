# Universal agent integration

jev0 is intentionally tool-agnostic. The stable contract is a local CLI, not a
vendor plugin API.

## Recommended integration contract

Run this before reporting work complete:

```sh
jev0 workspace --max-files 20 --max-lines 500
```

Run this before commit:

```sh
jev0 staged --max-files 20 --max-lines 500
```

Run long or failure-prone commands through:

```sh
jev0 run --timeout 120 -- <command> <args...>
```

## Agent instruction snippet

Use the instruction surface your tool actually supports:

```text
Keep changes within the requested scope.
Before completion, run the relevant tests and then `jev0 workspace`.
Before commit, run `jev0 staged`.
Do not bypass a jev0 rejection. Report the exact reason instead.
Use `jev0 run --timeout <seconds> -- ...` for bounded commands.
```

This applies equally to terminal-capable coding agents such as Cursor, Claude
Code, Codex, Windsurf, and future tools. Exact automatic hook/instruction
injection differs by product and is therefore not claimed as a universal feature.

## Why both workspace and staged exist

`staged` protects the Git index and is suitable for a pre-commit hook.
`workspace` compares tracked working-tree state against HEAD, catching agents
that edit files without staging or committing them.

Untracked files are blocked by `workspace` until they are explicitly staged.
That makes their inclusion visible to Git policy instead of silently guessing
whether a newly created file should count toward the budget.

Neither command is a sandbox. An agent with local write access can remove or
bypass local tooling. Use repository protections and CI for an external boundary.
