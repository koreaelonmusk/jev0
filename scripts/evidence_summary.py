#!/usr/bin/env python3
"""Render bounded, injection-safe Markdown from jev0 range evidence."""
import argparse
import json
from pathlib import Path
import sys

MAX_EVIDENCE_BYTES = 1_048_576
MAX_FIELD_CHARS = 512

REQUIRED_FIELDS = (
    "decision",
    "base_sha",
    "head_sha",
    "merge_base_sha",
    "policy_sha256",
    "verifier_version",
    "verifier_sha256",
    "evidence_sha256",
    "files_changed",
    "lines_changed",
    "reason",
)


def load_json(path):
    candidate = Path(path)
    with candidate.open("rb") as stream:
        raw = stream.read(MAX_EVIDENCE_BYTES + 1)
    if len(raw) > MAX_EVIDENCE_BYTES:
        raise ValueError(f"evidence exceeds {MAX_EVIDENCE_BYTES} bytes")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("evidence must be valid UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise ValueError("evidence root must be a JSON object")
    missing = [key for key in REQUIRED_FIELDS if key not in value]
    if missing:
        raise ValueError("evidence missing fields: " + ",".join(missing))
    return value


def safe_inline(value):
    if value is None:
        return "none"
    text = str(value)
    text = text.replace("\\", "\\\\")
    text = text.replace("\r", "\\r").replace("\n", "\\n")
    text = text.replace("`", "\\`")
    if len(text) > MAX_FIELD_CHARS:
        text = text[: MAX_FIELD_CHARS - 1] + "…"
    return text


def render(data):
    lines = [
        "### jev0 policy evidence",
        "",
        f"- decision: `{safe_inline(data[\'decision\'])}`",
        f"- base: `{safe_inline(data[\'base_sha\'])}`",
        f"- head: `{safe_inline(data[\'head_sha\'])}`",
        f"- merge base: `{safe_inline(data[\'merge_base_sha\'])}`",
        f"- policy sha256: `{safe_inline(data[\'policy_sha256\'])}`",
        f"- verifier: `{safe_inline(data[\'verifier_version\'])}`",
        f"- verifier sha256: `{safe_inline(data[\'verifier_sha256\'])}`",
        f"- evidence sha256: `{safe_inline(data[\'evidence_sha256\'])}`",
        f"- changed files: {safe_inline(data[\'files_changed\'])}",
        f"- added + deleted lines: {safe_inline(data[\'lines_changed\'])}",
    ]
    if data["reason"]:
        lines.append(f"- reason: `{safe_inline(data[\'reason\'])}`")
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence")
    args = parser.parse_args()
    try:
        sys.stdout.write(render(load_json(args.evidence)))
        return 0
    except (OSError, ValueError) as error:
        print("jev0-evidence-summary: " + " ".join(str(error).splitlines()), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
