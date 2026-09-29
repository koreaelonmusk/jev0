# Contributing to jev0

jev0 favors small, deterministic, auditable changes.

## Before opening a pull request

Run:

```sh
python3 -m compileall -q jev0.py tests scripts
python3 -m unittest discover -s tests -v
```

For performance changes, also run:

```sh
python3 scripts/benchmark.py
```

## Design rules

- Prefer deterministic checks when deterministic metadata is available.
- Never discard user work, reset the index, or rewrite files as part of a block.
- Treat shell commands and evaluator commands as literal argv, not shell text.
- Preserve existing hooks and user configuration.
- Add regression tests for every behavior change.
- Separate measured results from targets and marketing claims.
- Do not describe local hooks or wrappers as a security sandbox.
- Keep dependencies at zero unless a dependency has a clear, measured benefit.

## Pull request scope

One behavior change per PR is preferred. Explain:

1. what failure mode is being addressed,
2. why the change belongs in jev0,
3. what new guarantee is actually enforced,
4. what remains advisory or bypassable,
5. exact test and benchmark results.
