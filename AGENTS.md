# Working on jev0

- Prefer deterministic checks over model calls for deterministic policies.
- Keep changes within the requested scope; do not add unrelated refactors or dependencies.
- Preserve user files, index entries, existing hooks, and shell profiles.
- Keep scope checks relative to the repository root and handle unusual filenames.
- Never describe an advisory hook or command wrapper as a sandbox.
- Separate measured behavior from performance targets and assumptions.
- Verify behavior changes with `python3 -m unittest discover -s tests -v`.
- Report exact verification commands and results. Do not claim CI passed until observed.
