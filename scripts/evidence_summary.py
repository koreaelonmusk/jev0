#!/usr/bin/env python3
"""Render bounded, injection-safe Markdown from jev0 range evidence."""
import argparse
import html
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
    text = str(value).replace("\r", "\\r").replace("\n", "\\n")
    if len(text) > MAX_FIELD_CHARS:
        text = text[: MAX_FIELD_CHARS - 1] + "…"
    return html.escape(text, quote=True)


def code(value):
    return "<code>" + safe_inline(value) + "</code>"


def render(data):
    lines = [
        "### jev0 policy evidence",
        "",
        "- decision: " + code(data["decision"]),
        "- base: " + code(data["base_sha"]),
        "- head: " + code(data["head_sha"]),
        "- merge base: " + code(data["merge_base_sha"]),
        "- policy sha256: " + code(data["policy_sha256"]),
        "- verifier: " + code(data["verifier_version"]),
        "- verifier sha256: " + code(data["verifier_sha256"]),
        "- evidence sha256: " + code(data["evidence_sha256"]),
        "- changed files: " + safe_inline(data["files_changed"]),
        "- added + deleted lines: " + safe_inline(data["lines_changed"]),
    ]
    if data["reason"]:
        lines.append("- reason: " + code(data["reason"]))
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
