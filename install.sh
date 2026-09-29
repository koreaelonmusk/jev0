#!/bin/sh
set -eu
# Run from a reviewed checkout. No network requests or shell profile edits.
source_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
command -v python3 >/dev/null 2>&1 || { echo 'jev0: Python 3.9+ is required' >&2; exit 1; }
python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))'
target=${JEV0_BIN_DIR:-"$HOME/.local/bin"}
mkdir -p "$target"
if [ -e "$target/jev0" ] || [ -L "$target/jev0" ]; then
    if cmp -s "$source_dir/jev0.py" "$target/jev0" && [ -x "$target/jev0" ]; then exit 0; fi
    echo 'jev0: existing executable preserved; review and remove it before reinstalling' >&2
    exit 1
fi
( set -C; cat "$source_dir/jev0.py" > "$target/jev0" )
chmod 755 "$target/jev0"
printf 'Installed %s/jev0; add this directory to PATH if needed.\n' "$target"
