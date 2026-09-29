#!/usr/bin/env python3
"""Local, deterministic Git change budgets and bounded command execution."""
import argparse
import json
import math
import os
from pathlib import Path
import selectors
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from typing import Optional, Protocol

VERSION = "0.3.0-dev"
PROCESS_CLEANUP_TIMEOUT = 1.0
GIT_ERROR_BYTES = 8192


class Blocked(Exception):
    pass


class GuardEvaluator(Protocol):
    """Optional Layer 1 contract; True allows, False blocks with a reason."""

    def evaluate(self, diff_text: str) -> tuple[bool, str]:
        ...


class ProcessEvaluator:
    """Run a trusted Layer 1 evaluator using a strict stdin/stdout protocol."""

    def __init__(self, command, timeout, max_diff_bytes, max_output_bytes):
        self.command = command
        self.timeout = timeout
        self.max_diff_bytes = max_diff_bytes
        self.max_output_bytes = max_output_bytes

    def evaluate(self, diff_text: str) -> tuple[bool, str]:
        payload = diff_text.encode("utf-8")
        if len(payload) > self.max_diff_bytes:
            raise Blocked(f"Layer 1 diff exceeds {self.max_diff_bytes} bytes")
        with tempfile.TemporaryFile() as input_stream:
            input_stream.write(payload)
            input_stream.seek(0)
            process = subprocess.Popen(
                self.command,
                stdin=input_stream,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            try:
                output, error = capture_process_output(
                    process, self.timeout, self.max_output_bytes
                )
            except (subprocess.TimeoutExpired, KeyboardInterrupt) as error:
                terminate_process_group(process)
                if isinstance(error, KeyboardInterrupt):
                    raise Blocked("Layer 1 evaluator interrupted") from None
                raise Blocked(f"Layer 1 evaluator exceeded {self.timeout:g}s") from None

            if process.returncode:
                detail = error.decode(errors="replace").strip()
                suffix = ": " + detail if detail else ""
                raise Blocked(f"Layer 1 evaluator exited {process.returncode}{suffix}")

        try:
            result = json.loads(output)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise Blocked("Layer 1 evaluator returned invalid JSON") from None
        if (
            not isinstance(result, dict)
            or set(result) != {"passed", "reason"}
            or type(result["passed"]) is not bool
            or not isinstance(result["reason"], str)
        ):
            raise Blocked("Layer 1 JSON must contain only passed (bool) and reason (str)")
        return result["passed"], result["reason"]


def capture_process_output(process, timeout, max_output_bytes):
    """Drain evaluator pipes with bounded memory and a finite deadline."""

    if os.name != "posix":
        raise Blocked("Layer 1 process evaluators currently require macOS or Linux")

    output = bytearray()
    error = bytearray()
    stdout_bytes = 0
    selector = selectors.DefaultSelector()
    streams = ((process.stdout, "stdout"), (process.stderr, "stderr"))
    for stream, name in streams:
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ, name)

    deadline = time.monotonic() + timeout
    try:
        while selector.get_map():
            returncode = process.poll()
            if returncode is not None:
                wait = 0
            else:
                wait = deadline - time.monotonic()
                if wait <= 0:
                    raise subprocess.TimeoutExpired(process.args, timeout)

            events = selector.select(wait)
            if not events:
                if returncode is not None:
                    break
                continue

            for key, _ in events:
                try:
                    chunk = os.read(key.fileobj.fileno(), 65536)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                    continue

                if key.data == "stdout":
                    stdout_bytes += len(chunk)
                    if stdout_bytes > max_output_bytes:
                        terminate_process_group(process)
                        raise Blocked(
                            f"Layer 1 output exceeds {max_output_bytes} bytes"
                        )
                    output.extend(chunk)
                elif len(error) < max_output_bytes:
                    remaining = max_output_bytes - len(error)
                    error.extend(chunk[:remaining])

        if process.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(process.args, timeout)
            process.wait(timeout=remaining)
        return bytes(output), bytes(error)
    finally:
        selector.close()
        close_process_pipes(process)


def close_process_pipes(process):
    """Close parent-side pipes so escaped descendants cannot keep us waiting for EOF."""

    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is None:
            continue
        try:
            stream.close()
        except (OSError, ValueError):
            pass


def terminate_process_group(process, cleanup_timeout=PROCESS_CLEANUP_TIMEOUT):
    """Best-effort process-group kill with bounded local cleanup."""

    try:
        if os.name == "posix":
            # The leader may already have exited while descendants still keep the
            # process group (and inherited pipes) alive.
            os.killpg(process.pid, signal.SIGKILL)
        elif process.poll() is None:
            process.kill()
    except (ProcessLookupError, PermissionError):
        pass
    close_process_pipes(process)
    try:
        process.wait(timeout=cleanup_timeout)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except (ProcessLookupError, PermissionError):
            pass
        try:
            process.wait(timeout=cleanup_timeout)
        except subprocess.TimeoutExpired:
            pass


def git(*args):
    result = subprocess.run(
        ["git", *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    if result.returncode:
        raise Blocked(
            "Git operation failed: " + result.stderr.decode(errors="replace").strip()
        )
    return result.stdout


def git_limited(max_bytes, *args):
    """Read at most max_bytes + 1 bytes from Git before rejecting oversized output."""

    with tempfile.TemporaryFile() as error_stream:
        process = subprocess.Popen(
            ["git", *args],
            stdout=subprocess.PIPE,
            stderr=error_stream,
            start_new_session=True,
        )
        try:
            output = process.stdout.read(max_bytes + 1)
            if len(output) > max_bytes:
                terminate_process_group(process)
                raise Blocked(f"Layer 1 diff exceeds {max_bytes} bytes")
            returncode = process.wait()
        except KeyboardInterrupt:
            terminate_process_group(process)
            raise Blocked("Git operation interrupted") from None
        finally:
            if process.stdout is not None and not process.stdout.closed:
                process.stdout.close()
        if returncode:
            error_stream.seek(0)
            detail = error_stream.read(GIT_ERROR_BYTES).decode(errors="replace").strip()
            raise Blocked("Git operation failed: " + detail)
        return output


def positive(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def duration(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return number


def scope(value):
    path = value.rstrip("/")
    if (
        not path
        or path.startswith("/")
        or any(p in ("", ".", "..") for p in path.split("/"))
    ):
        raise argparse.ArgumentTypeError(
            "use a repository-relative file or directory without . or .."
        )
    return path


def evaluator_command(value):
    try:
        command = json.loads(value)
    except json.JSONDecodeError as error:
        raise argparse.ArgumentTypeError(
            "must be a JSON array of command arguments"
        ) from error
    if (
        not isinstance(command, list)
        or not command
        or any(not isinstance(part, str) or not part for part in command)
    ):
        raise argparse.ArgumentTypeError(
            "must be a non-empty JSON array of non-empty strings"
        )
    return command


def repository_root():
    root = os.fsdecode(git("rev-parse", "--show-toplevel").rstrip(b"\n"))
    os.chdir(root)
    return Path(root)


def has_head():
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def diff_prefix(mode):
    if mode == "staged":
        return ["diff", "--cached"]
    if mode == "workspace":
        return ["diff", "HEAD"] if has_head() else ["diff", "--cached"]
    raise ValueError(f"unknown diff mode: {mode}")


def mode_label(mode):
    return "staged" if mode == "staged" else "workspace"


def unified_diff(mode, max_bytes=None):
    args = (
        *diff_prefix(mode),
        "--no-ext-diff",
        "--no-textconv",
        "--no-renames",
        "--no-color",
        "--patch",
        "--",
    )
    output = git(*args) if max_bytes is None else git_limited(max_bytes, *args)
    return output.decode("utf-8", errors="replace")


def evaluator_diff(mode, evaluator):
    if isinstance(evaluator, ProcessEvaluator):
        return unified_diff(mode, evaluator.max_diff_bytes)
    return unified_diff(mode)


def run_evaluator(evaluator, diff_text):
    try:
        result = evaluator.evaluate(diff_text)
    except Blocked:
        raise
    except Exception as error:
        raise Blocked("Layer 1 evaluator failed: " + str(error)) from None
    if (
        not isinstance(result, tuple)
        or len(result) != 2
        or type(result[0]) is not bool
        or not isinstance(result[1], str)
    ):
        raise Blocked("Layer 1 evaluator must return (bool, str)")
    passed, reason = result
    if not passed:
        raise Blocked("Layer 1: " + (reason.strip() or "rejected without a reason"))


def staged(args, evaluator: Optional[GuardEvaluator] = None):
    heuristic_rules(args, "staged")
    if evaluator is not None:
        run_evaluator(evaluator, evaluator_diff("staged", evaluator))


def workspace(args, evaluator: Optional[GuardEvaluator] = None):
    heuristic_rules(args, "workspace")
    if evaluator is not None:
        run_evaluator(evaluator, evaluator_diff("workspace", evaluator))


def heuristic_rules(args, mode):
    repository_root()
    if mode == "workspace":
        untracked = [
            field
            for field in git("ls-files", "--others", "--exclude-standard", "-z", "--").split(
                b"\0"
            )
            if field
        ]
        if untracked:
            path = os.fsdecode(untracked[0])
            raise Blocked(f"untracked file requires staging/review: {path!r}")

    prefix = diff_prefix(mode)
    fields = git(
        *prefix,
        "--no-ext-diff",
        "--no-textconv",
        "--no-renames",
        "--numstat",
        "-z",
        "--",
    ).split(b"\0")
    entries = [field.split(b"\t", 2) for field in fields if field]
    label = mode_label(mode)
    if len(entries) > args.max_files:
        raise Blocked(f"{len(entries)} {label} files exceed budget {args.max_files}")

    active = set(
        git(
            *prefix,
            "--no-ext-diff",
            "--no-renames",
            "--name-only",
            "--diff-filter=ACMRT",
            "-z",
            "--",
        ).split(b"\0")
    )
    total = 0
    for added, removed, raw_path in entries:
        path = os.fsdecode(raw_path)
        if args.allow and not any(
            path == p or path.startswith(p + "/") for p in args.allow
        ):
            raise Blocked(f"outside allowed scope: {path!r}")
        parts = path.lower().split("/")
        if raw_path in active and (
            path.lower().endswith((".gguf", ".bin")) or "models" in parts
        ):
            raise Blocked(f"model artifact: {path!r}")
        if added == b"-" or removed == b"-":
            if raw_path in active:
                raise Blocked(f"binary change requires separate review: {path!r}")
        else:
            total += int(added) + int(removed)
    if total > args.max_lines:
        raise Blocked(
            f"{total} added/deleted lines exceed budget {args.max_lines}"
        )


def init(args):
    root = Path(os.fsdecode(git("rev-parse", "--show-toplevel").rstrip(b"\n")))
    custom = subprocess.run(
        ["git", "config", "--get", "core.hooksPath"], stdout=subprocess.PIPE
    )
    if custom.returncode != 1:
        raise Blocked(
            "custom core.hooksPath: integrate jev0 staged into your existing hook manager"
        )
    hook = Path(
        os.fsdecode(git("rev-parse", "--git-path", "hooks/pre-commit").rstrip(b"\n"))
    ).absolute()
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "staged",
        "--max-files",
        str(args.max_files),
        "--max-lines",
        str(args.max_lines),
    ]
    for allowed in args.allow:
        command.extend(["--allow", allowed])
    if args.evaluator_command:
        command.extend(
            [
                "--evaluator-command",
                json.dumps(args.evaluator_command, separators=(",", ":")),
                "--evaluator-timeout",
                str(args.evaluator_timeout),
                "--max-diff-bytes",
                str(args.max_diff_bytes),
                "--max-evaluator-output-bytes",
                str(args.max_evaluator_output_bytes),
            ]
        )
    content = (
        "#!/bin/sh\n# jev0 managed pre-commit hook\nexec "
        + shlex.join(command)
        + "\n"
    )
    if hook.is_symlink():
        raise Blocked("existing hook is a symlink; integrate manually")
    if hook.exists():
        if hook.read_text() == content and os.access(hook, os.X_OK):
            return
        raise Blocked(
            "existing pre-commit hook preserved; integrate jev0 staged manually"
        )
    hook.parent.mkdir(parents=True, exist_ok=True)
    with hook.open("x") as stream:
        stream.write(content)
    hook.chmod(0o755)
    print(f"jev0: installed pre-commit hook for {root}", file=sys.stderr)


def run(args):
    command = args.command
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        raise Blocked("run requires a command after --")
    if os.name != "posix":
        raise Blocked("run currently requires macOS or Linux process groups")
    process = subprocess.Popen(command, start_new_session=True)
    try:
        returncode = process.wait(timeout=args.timeout)
    except (subprocess.TimeoutExpired, KeyboardInterrupt) as error:
        terminate_process_group(process)
        if isinstance(error, KeyboardInterrupt):
            raise Blocked("command interrupted") from None
        raise Blocked(
            f"command exceeded {args.timeout:g}s; process group terminated"
        ) from None
    return returncode if returncode >= 0 else 128 - returncode


def build_evaluator(args):
    if not args.evaluator_command:
        return None
    return ProcessEvaluator(
        args.evaluator_command,
        args.evaluator_timeout,
        args.max_diff_bytes,
        args.max_evaluator_output_bytes,
    )


def add_guard_arguments(item):
    item.add_argument("--max-files", type=positive, default=20)
    item.add_argument("--max-lines", type=positive, default=500)
    item.add_argument("--allow", type=scope, action="append", default=[])
    item.add_argument("--evaluator-command", type=evaluator_command)
    item.add_argument("--evaluator-timeout", type=duration, default=30.0)
    item.add_argument("--max-diff-bytes", type=positive, default=1_000_000)
    item.add_argument("--max-evaluator-output-bytes", type=positive, default=4096)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=f"jev0 {VERSION}")
    sub = parser.add_subparsers(dest="action", required=True)
    for name in ("staged", "workspace", "init"):
        add_guard_arguments(sub.add_parser(name))
    item = sub.add_parser("run")
    item.add_argument("--timeout", type=duration, required=True)
    item.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        if args.action in ("staged", "workspace"):
            evaluator = build_evaluator(args)
            return {"staged": staged, "workspace": workspace}[args.action](
                args, evaluator
            ) or 0
        return {"init": init, "run": run}[args.action](args) or 0
    except (Blocked, OSError, ValueError) as error:
        print("jev0: " + " ".join(str(error).splitlines()), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
