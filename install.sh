#!/bin/sh
set -eu

# Run from a reviewed checkout. No network requests or shell profile edits.
source_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
command -v python3 >/dev/null 2>&1 || {
    echo 'jev0: Python 3.9+ is required' >&2
    exit 1
}
python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))'

target=${JEV0_BIN_DIR:-"$HOME/.local/bin"}
destination="$target/jev0"
mkdir -p "$target"

if [ -e "$destination" ] || [ -L "$destination" ]; then
    if [ ! -L "$destination" ] && cmp -s "$source_dir/jev0.py" "$destination"; then
        chmod 755 "$destination"
        exit 0
    fi
    echo 'jev0: existing executable preserved; review and remove it before reinstalling' >&2
    exit 1
fi

tmp=$(mktemp "$target/.jev0-install.XXXXXX")
cleanup() {
    rm -f "$tmp"
}
trap cleanup EXIT HUP INT TERM

cat "$source_dir/jev0.py" > "$tmp"
chmod 755 "$tmp"

# Hard-link creation is atomic and refuses to overwrite a destination created
# concurrently by another installer or process.
if ! ln "$tmp" "$destination" 2>/dev/null; then
    if [ ! -L "$destination" ] && [ -e "$destination" ] &&
       cmp -s "$source_dir/jev0.py" "$destination"; then
        chmod 755 "$destination"
        exit 0
    fi
    echo 'jev0: install target appeared concurrently; existing file preserved' >&2
    exit 1
fi

printf 'Installed %s; add this directory to PATH if needed.\n' "$destination"
