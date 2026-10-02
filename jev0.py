#!/usr/bin/env python3
"""Local, deterministic Git change budgets and bounded command execution."""
import argparse
import hashlib
import json
import math
import os
import platform
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
GIT_BOUNDED_READ_TIMEOUT = 30.0
BASE_POLICY_READ_TIMEOUT = 5.0
GIT_ERROR_BYTES = 8192
DOCTOR_SCHEMA_VERSION = 1
DOCTOR_MAX_FILE_BYTES = 10_000_000
DOCTOR_HOOK_PREFIX_BYTES = 16_384
POLICY_SCHEMA_VERSION = 1
POLICY_MAX_BYTES = 65_536
EVIDENCE_SCHEMA_VERSION = 1
EVIDENCE_MAX_BYTES = 1_048_576
PROVENANCE_SCHEMA_VERSION = 1
PROVENANCE_MAX_BYTES = 1_048_576
RULESET_MAX_BYTES = 1_048_576
RULESET_REPORT_SCHEMA_VERSION = 1
CHANGE_METADATA_MAX_BYTES = 8_388_608
EVIDENCE_MAX_PATHS = 1000
DEFAULT_MAX_FILES = 20
DEFAULT_MAX_LINES = 500
FAILURE_SCHEMA_VERSION = 1
FAILURE_REASON_MAX_CHARS = 2048
FAILURE_MAX_BYTES = 65_536



class Blocked(Exception):
    pass


def _failure_reason(error):
    """Return one bounded, single-line failure reason for local capture."""
    return " ".join(str(error).splitlines())[:FAILURE_REASON_MAX_CHARS]


def _failure_git_dir(root):
    raw = git("rev-parse", "--git-dir").rstrip(b"\n")
    path = Path(os.fsdecode(raw))
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def _failure_repository_fingerprint(root):
    return hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()


def _failure_head_sha():
    try:
        return resolve_commit("HEAD")
    except (Blocked, OSError, ValueError):
        return None


def _failure_policy_sha256(args, root):
    policy = getattr(args, "policy", None)
    if policy is None:
        return None
    try:
        _, resolved = load_policy(policy, root)
        return file_sha256(resolved, POLICY_MAX_BYTES)
    except (Blocked, OSError, ValueError):
        return None


def build_failure_record(args, error):
    """Build a dependency-free, raw-diff-free jev0-failure/v1 record."""
    root = repository_root()
    unsigned = {
        "schema_version": FAILURE_SCHEMA_VERSION,
        "kind": "jev0-failure",
        "action": args.action,
        "reason": _failure_reason(error),
        "repository_fingerprint": _failure_repository_fingerprint(root),
        "head_sha": _failure_head_sha(),
        "policy_sha256": _failure_policy_sha256(args, root),
        "raw_diff_captured": False,
    }
    canonical = json.dumps(
        unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return {**unsigned, "failure_id": hashlib.sha256(canonical).hexdigest()}


def capture_failure(args, error):
    """Atomically spool one content-addressed failure under .git/jev0/failures."""
    root = repository_root()
    record = build_failure_record(args, error)
    payload = (
        json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode("utf-8")
    if len(payload) > FAILURE_MAX_BYTES:
        raise Blocked(f"failure record exceeds {FAILURE_MAX_BYTES} bytes")

    directory = _failure_git_dir(root) / "jev0" / "failures"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    final = directory / (record["failure_id"] + ".json")
    if final.exists():
        return record["failure_id"]

    descriptor, temporary_name = tempfile.mkstemp(prefix=".capture-", dir=directory)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        # Content addressing makes concurrent writers equivalent. Replace is
        # atomic within the Git metadata filesystem and avoids platform-specific
        # hard-link restrictions.
        os.replace(temporary, final)
        try:
            final.chmod(0o600)
        except OSError:
            pass
        return record["failure_id"]
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _failure_files():
    root = repository_root()
    directory = _failure_git_dir(root) / "jev0" / "failures"
    if not directory.is_dir():
        return []
    return sorted(
        path for path in directory.iterdir()
        if path.is_file() and path.suffix == ".json" and len(path.stem) == 64
    )


def read_failure_record(path):
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise Blocked("cannot read failure record: " + str(error)) from None
    if len(raw) > FAILURE_MAX_BYTES:
        raise Blocked(f"failure record exceeds {FAILURE_MAX_BYTES} bytes")
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise Blocked("failure record must be valid UTF-8 JSON") from None
    required = {
        "schema_version", "kind", "action", "reason",
        "repository_fingerprint", "head_sha", "policy_sha256",
        "raw_diff_captured", "failure_id",
    }
    if not isinstance(document, dict) or set(document) != required:
        raise Blocked("failure record keys mismatch")
    if document["schema_version"] != FAILURE_SCHEMA_VERSION or document["kind"] != "jev0-failure":
        raise Blocked("failure record identity is invalid")
    supplied = document["failure_id"]
    unsigned = dict(document)
    del unsigned["failure_id"]
    canonical = json.dumps(
        unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    if supplied != hashlib.sha256(canonical).hexdigest():
        raise Blocked("failure record digest mismatch")
    return document


def failures_list(args):
    records = [read_failure_record(path) for path in _failure_files()]
    if args.json:
        print(json.dumps(records, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
    else:
        for record in records:
            print(f"{record['failure_id']} {record['action']} {record['reason']}")
    return 0


def failures_show(args):
    if (
        len(args.failure_id) != 64
        or any(ch not in "0123456789abcdef" for ch in args.failure_id)
    ):
        raise Blocked("failure id must be a lowercase SHA-256")
    root = repository_root()
    path = _failure_git_dir(root) / "jev0" / "failures" / (args.failure_id + ".json")
    if not path.is_file():
        raise Blocked("failure record not found")
    record = read_failure_record(path)
    print(json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
    return 0


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
        if os.name != "posix":
            raise Blocked("Layer 1 process evaluators currently require macOS or Linux")
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
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(process.args, timeout)
                # Re-check leader exit frequently. A detached descendant may keep
                # inherited pipe fds open without producing data.
                wait = min(remaining, 0.05)

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


def capture_bounded_stdout(process, timeout, max_bytes, label):
    """Read one subprocess stdout stream with byte and wall-clock ceilings."""

    if os.name != "posix":
        raise Blocked("bounded subprocess reads currently require macOS or Linux")

    output = bytearray()
    selector = selectors.DefaultSelector()
    os.set_blocking(process.stdout.fileno(), False)
    selector.register(process.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout

    try:
        while selector.get_map():
            returncode = process.poll()
            if returncode is not None:
                wait = 0
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    terminate_process_group(process)
                    raise Blocked(f"{label} exceeded {timeout:g}s")
                wait = min(remaining, 0.05)

            events = selector.select(wait)
            if not events:
                # Never wait for pipe EOF from descendants after the leader exits.
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
                output.extend(chunk)
                if len(output) > max_bytes:
                    terminate_process_group(process)
                    raise Blocked(f"{label} exceeds {max_bytes} bytes")

        if process.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                terminate_process_group(process)
                raise Blocked(f"{label} exceeded {timeout:g}s")
            try:
                process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                terminate_process_group(process)
                raise Blocked(f"{label} exceeded {timeout:g}s") from None

        return bytes(output), process.returncode
    except KeyboardInterrupt:
        terminate_process_group(process)
        raise Blocked("Git operation interrupted") from None
    finally:
        selector.close()
        if process.stdout is not None and not process.stdout.closed:
            process.stdout.close()


def git_output_limited(max_bytes, label, *args):
    """Capture Git stdout with hard byte and wall-clock ceilings."""

    with tempfile.TemporaryFile() as error_stream:
        process = subprocess.Popen(
            ["git", *args],
            stdout=subprocess.PIPE,
            stderr=error_stream,
            start_new_session=True,
        )
        output, returncode = capture_bounded_stdout(
            process,
            GIT_BOUNDED_READ_TIMEOUT,
            max_bytes,
            label,
        )
        if returncode:
            error_stream.seek(0)
            detail = error_stream.read(GIT_ERROR_BYTES).decode(errors="replace").strip()
            raise Blocked("Git operation failed: " + detail)
        return output


def git_limited(max_bytes, *args):
    """Read bounded Git diff bytes with a finite wall-clock deadline."""

    with tempfile.TemporaryFile() as error_stream:
        process = subprocess.Popen(
            ["git", *args],
            stdout=subprocess.PIPE,
            stderr=error_stream,
            start_new_session=True,
        )
        output, returncode = capture_bounded_stdout(
            process,
            GIT_BOUNDED_READ_TIMEOUT,
            max_bytes,
            "Layer 1 diff",
        )
        if returncode:
            error_stream.seek(0)
            detail = error_stream.read(GIT_ERROR_BYTES).decode(errors="replace").strip()
            raise Blocked("Git operation failed: " + detail)
        return output


def file_sha256(path, max_bytes=None):
    path = Path(path)
    if max_bytes is not None and path.stat().st_size > max_bytes:
        raise Blocked(f"diagnostic file exceeds {max_bytes} bytes")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def executable_sha256():
    """Return the SHA-256 of the exact jev0 source/executable being run."""

    return file_sha256(Path(__file__).resolve())


def git_version():
    try:
        result = subprocess.run(
            ["git", "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError:
        return None
    if result.returncode:
        return None
    return result.stdout.decode(errors="replace").strip() or None


def parse_hook_layer0(command):
    """Extract effective Layer 0 settings from one managed hook command."""

    if len(command) < 3 or command[2] != "staged":
        return None
    parser = argparse.ArgumentParser(add_help=False)
    add_guard_arguments(parser)
    try:
        parsed, unknown = parser.parse_known_args(command[3:])
    except SystemExit:
        return None
    if unknown or parsed.policy is not None:
        return None
    return {
        "max_files": DEFAULT_MAX_FILES if parsed.max_files is None else parsed.max_files,
        "max_lines": DEFAULT_MAX_LINES if parsed.max_lines is None else parsed.max_lines,
        "allow": [] if parsed.allow is None else parsed.allow,
    }


def doctor_repository_state():
    """Inspect repository/hook state without changing files or Git config."""

    try:
        root_result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        return {
            "repository": False,
            "repository_root": None,
            "hook_status": "git-unavailable",
            "hook_enforced": False,
            "hook_python": None,
            "hook_target": None,
            "hook_target_exists": False,
            "hook_matches_executable": False,
            "hook_policy_sha256": None,
            "hook_max_files": None,
            "hook_max_lines": None,
            "hook_allow": None,
        }

    if root_result.returncode:
        return {
            "repository": False,
            "repository_root": None,
            "hook_status": "not-a-repository",
            "hook_enforced": False,
            "hook_python": None,
            "hook_target": None,
            "hook_target_exists": False,
            "hook_matches_executable": False,
            "hook_policy_sha256": None,
            "hook_max_files": None,
            "hook_max_lines": None,
            "hook_allow": None,
        }

    root = os.fsdecode(root_result.stdout.rstrip(b"\n"))
    custom = subprocess.run(
        ["git", "config", "--get", "core.hooksPath"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if custom.returncode == 0:
        return {
            "repository": True,
            "repository_root": root,
            "hook_status": "custom-hooks-path",
            "hook_enforced": False,
            "hook_python": None,
            "hook_target": None,
            "hook_target_exists": False,
            "hook_matches_executable": False,
            "hook_policy_sha256": None,
            "hook_max_files": None,
            "hook_max_lines": None,
            "hook_allow": None,
        }
    if custom.returncode != 1:
        return {
            "repository": True,
            "repository_root": root,
            "hook_status": "git-config-error",
            "hook_enforced": False,
            "hook_python": None,
            "hook_target": None,
            "hook_target_exists": False,
            "hook_matches_executable": False,
            "hook_policy_sha256": None,
            "hook_max_files": None,
            "hook_max_lines": None,
            "hook_allow": None,
        }

    hook_result = subprocess.run(
        ["git", "rev-parse", "--git-path", "hooks/pre-commit"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if hook_result.returncode:
        return {
            "repository": True,
            "repository_root": root,
            "hook_status": "git-path-error",
            "hook_enforced": False,
            "hook_python": None,
            "hook_target": None,
            "hook_target_exists": False,
            "hook_matches_executable": False,
            "hook_policy_sha256": None,
            "hook_max_files": None,
            "hook_max_lines": None,
            "hook_allow": None,
        }

    hook = Path(os.fsdecode(hook_result.stdout.rstrip(b"\n")))
    if not hook.is_absolute():
        hook = Path.cwd() / hook
    hook = hook.absolute()

    if hook.is_symlink():
        return {
            "repository": True,
            "repository_root": root,
            "hook_status": "symlink",
            "hook_enforced": False,
            "hook_python": None,
            "hook_target": None,
            "hook_target_exists": False,
            "hook_matches_executable": False,
            "hook_policy_sha256": None,
            "hook_max_files": None,
            "hook_max_lines": None,
            "hook_allow": None,
        }
    if not hook.exists():
        return {
            "repository": True,
            "repository_root": root,
            "hook_status": "missing",
            "hook_enforced": False,
            "hook_python": None,
            "hook_target": None,
            "hook_target_exists": False,
            "hook_matches_executable": False,
            "hook_policy_sha256": None,
            "hook_max_files": None,
            "hook_max_lines": None,
            "hook_allow": None,
        }

    try:
        with hook.open("rb") as stream:
            hook_prefix = stream.read(DOCTOR_HOOK_PREFIX_BYTES + 1)
        if len(hook_prefix) > DOCTOR_HOOK_PREFIX_BYTES:
            return {
                "repository": True,
                "repository_root": root,
                "hook_status": "oversized",
                "hook_enforced": False,
                "hook_python": None,
                "hook_target": None,
                "hook_target_exists": False,
                "hook_matches_executable": False,
                "hook_policy_sha256": None,
            "hook_max_files": None,
            "hook_max_lines": None,
            "hook_allow": None,
            }
        hook_text = hook_prefix.decode(errors="replace")
        first_lines = hook_text.splitlines()[:2]
    except OSError:
        return {
            "repository": True,
            "repository_root": root,
            "hook_status": "unreadable",
            "hook_enforced": False,
            "hook_python": None,
            "hook_target": None,
            "hook_target_exists": False,
            "hook_matches_executable": False,
            "hook_policy_sha256": None,
            "hook_max_files": None,
            "hook_max_lines": None,
            "hook_allow": None,
        }

    managed = "# jev0 managed pre-commit hook" in first_lines
    policy_prefix = "# jev0 policy sha256: "
    policy_fingerprint = next(
        (
            line[len(policy_prefix):]
            for line in hook_text.splitlines()
            if line.startswith(policy_prefix)
        ),
        None,
    )
    if policy_fingerprint is not None:
        if (
            len(policy_fingerprint) != 64
            or any(ch not in "0123456789abcdef" for ch in policy_fingerprint)
        ):
            policy_fingerprint = None
    executable = os.access(hook, os.X_OK)
    state = {
        "repository": True,
        "repository_root": root,
        "hook_status": "foreign",
        "hook_enforced": False,
        "hook_python": None,
        "hook_target": None,
        "hook_target_exists": False,
        "hook_matches_executable": False,
        "hook_policy_sha256": None,
        "hook_max_files": None,
        "hook_max_lines": None,
        "hook_allow": None,
    }
    state["hook_policy_sha256"] = policy_fingerprint
    if not managed:
        return state
    if not executable:
        state["hook_status"] = "managed-not-executable"
        return state

    try:
        lines = hook_text.splitlines()
        command_line = next(
            line for line in lines if line.startswith("exec ")
        )
        command = shlex.split(command_line)[1:]
    except (OSError, StopIteration, ValueError):
        state["hook_status"] = "managed-invalid"
        return state

    layer0 = parse_hook_layer0(command)
    if layer0 is None:
        state["hook_status"] = "managed-invalid"
        return state
    state["hook_max_files"] = layer0["max_files"]
    state["hook_max_lines"] = layer0["max_lines"]
    state["hook_allow"] = layer0["allow"]

    hook_python = Path(command[0])
    hook_target = Path(command[1])
    target_exists = hook_target.is_file()
    python_exists = hook_python.is_file()
    state["hook_python"] = str(hook_python)
    state["hook_target"] = str(hook_target)
    state["hook_target_exists"] = target_exists
    if target_exists:
        try:
            state["hook_matches_executable"] = (
                file_sha256(hook_target, DOCTOR_MAX_FILE_BYTES) == executable_sha256()
            )
        except (OSError, Blocked):
            state["hook_matches_executable"] = False

    state["hook_enforced"] = python_exists and target_exists
    state["hook_status"] = "managed" if state["hook_enforced"] else "managed-stale"
    return state


def doctor(args):
    state = {
        "schema_version": DOCTOR_SCHEMA_VERSION,
        "version": VERSION,
        "executable": str(Path(__file__).resolve()),
        "executable_sha256": executable_sha256(),
        "python_executable": sys.executable,
        "python_version": platform.python_version(),
        "platform": sys.platform,
        "machine": platform.machine() or None,
        "git_version": git_version(),
        "posix_process_groups": os.name == "posix",
    }
    state.update(doctor_repository_state())
    state["runtime_ready"] = bool(
        state["git_version"]
        and sys.version_info >= (3, 9)
        and state["posix_process_groups"]
    )

    if args.json:
        print(json.dumps(state, sort_keys=True, separators=(",", ":")))
    else:
        ordered = (
            "schema_version",
            "version",
            "executable",
            "executable_sha256",
            "python_executable",
            "python_version",
            "platform",
            "machine",
            "git_version",
            "posix_process_groups",
            "runtime_ready",
            "repository",
            "repository_root",
            "hook_status",
            "hook_python",
            "hook_target",
            "hook_target_exists",
            "hook_matches_executable",
            "hook_policy_sha256",
            "hook_max_files",
            "hook_max_lines",
            "hook_allow",
            "hook_enforced",
        )
        for key in ordered:
            value = state[key]
            if value is None:
                value = "unavailable"
            elif isinstance(value, bool):
                value = "yes" if value else "no"
            print(f"{key}: {value}")
    return 0


def policy_scope(value):
    if not isinstance(value, str):
        raise Blocked("policy allow entries must be strings")
    try:
        return scope(value)
    except argparse.ArgumentTypeError as error:
        raise Blocked("invalid policy scope: " + str(error)) from None


def parse_policy_document(raw):
    """Parse one bounded Layer 0 policy document from trusted bytes."""

    def strict_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise Blocked(f"duplicate policy key: {key}")
            result[key] = value
        return result

    try:
        document = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=strict_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise Blocked("policy must be valid UTF-8 JSON") from None

    if not isinstance(document, dict):
        raise Blocked("policy root must be a JSON object")
    allowed_keys = {"schema_version", "max_files", "max_lines", "allow"}
    unknown = set(document) - allowed_keys
    if unknown:
        raise Blocked("unknown policy keys: " + ", ".join(sorted(unknown)))
    if type(document.get("schema_version")) is not int:
        raise Blocked("policy schema_version must be an integer")
    if document["schema_version"] != POLICY_SCHEMA_VERSION:
        raise Blocked(
            f"unsupported policy schema_version {document['schema_version']}"
        )

    max_files = document.get("max_files", DEFAULT_MAX_FILES)
    max_lines = document.get("max_lines", DEFAULT_MAX_LINES)
    if type(max_files) is not int or max_files <= 0:
        raise Blocked("policy max_files must be a positive integer")
    if type(max_lines) is not int or max_lines <= 0:
        raise Blocked("policy max_lines must be a positive integer")

    raw_allow = document.get("allow", [])
    if not isinstance(raw_allow, list):
        raise Blocked("policy allow must be an array")
    allow = [policy_scope(value) for value in raw_allow]

    return argparse.Namespace(
        max_files=max_files,
        max_lines=max_lines,
        allow=allow,
    )


def policy_tree_path(value):
    if (
        not isinstance(value, str)
        or not value
        or ":" in value
        or "\0" in value
        or "\n" in value
        or "\r" in value
    ):
        raise argparse.ArgumentTypeError(
            "base policy path must be a repository-relative file path without ':'"
        )
    return scope(value)


def load_policy_from_commit(commit_sha, path):
    """Load a bounded policy blob from one already-resolved commit."""

    tree_path = policy_tree_path(path)
    object_spec = f"{commit_sha}:{tree_path}"
    with tempfile.TemporaryFile() as error_stream:
        process = subprocess.Popen(
            ["git", "show", "--no-ext-diff", "--no-textconv", object_spec],
            stdout=subprocess.PIPE,
            stderr=error_stream,
            start_new_session=True,
        )
        try:
            output, returncode = capture_bounded_stdout(
                process,
                BASE_POLICY_READ_TIMEOUT,
                POLICY_MAX_BYTES,
                "base policy read",
            )
        except Blocked as error:
            if str(error) == f"base policy read exceeds {POLICY_MAX_BYTES} bytes":
                raise Blocked(f"policy exceeds {POLICY_MAX_BYTES} bytes") from None
            raise

        if returncode:
            error_stream.seek(0)
            detail = error_stream.read(GIT_ERROR_BYTES).decode(errors="replace").strip()
            suffix = ": " + detail if detail else ""
            raise Blocked(f"cannot read base policy {tree_path!r}{suffix}")

    return parse_policy_document(output), tree_path, hashlib.sha256(output).hexdigest()


def load_policy(path, root):
    """Load one explicit, bounded, repository-contained Layer 0 policy."""

    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root.resolve())
    except (OSError, ValueError):
        raise Blocked("policy must be an existing file inside the repository") from None
    if not resolved.is_file():
        raise Blocked("policy must be a regular file")

    try:
        with resolved.open("rb") as stream:
            raw = stream.read(POLICY_MAX_BYTES + 1)
    except OSError as error:
        raise Blocked("cannot read policy: " + str(error)) from None
    if len(raw) > POLICY_MAX_BYTES:
        raise Blocked(f"policy exceeds {POLICY_MAX_BYTES} bytes")

    return parse_policy_document(raw), resolved


def resolve_guard_settings(args):
    """Resolve CLI defaults or one explicit policy into Layer 0 settings."""

    root = repository_root()
    policy = getattr(args, "policy", None)
    if policy is not None:
        settings, policy_file = load_policy(policy, root)
        return settings, policy_file, root

    max_files = getattr(args, "max_files", None)
    max_lines = getattr(args, "max_lines", None)
    allow = getattr(args, "allow", None)
    return argparse.Namespace(
        max_files=DEFAULT_MAX_FILES if max_files is None else max_files,
        max_lines=DEFAULT_MAX_LINES if max_lines is None else max_lines,
        allow=[] if allow is None else allow,
    ), None, root


def inspect_policy(args):
    root = repository_root()
    settings, resolved = load_policy(args.path, root)
    state = {
        "schema_version": POLICY_SCHEMA_VERSION,
        "policy_path": str(resolved),
        "policy_sha256": file_sha256(resolved, POLICY_MAX_BYTES),
        "max_files": settings.max_files,
        "max_lines": settings.max_lines,
        "allow": settings.allow,
    }
    if args.json:
        print(json.dumps(state, sort_keys=True, separators=(",", ":")))
    else:
        print(f"schema_version: {state['schema_version']}")
        print(f"policy_path: {state['policy_path']}")
        print(f"policy_sha256: {state['policy_sha256']}")
        print(f"max_files: {state['max_files']}")
        print(f"max_lines: {state['max_lines']}")
        print("allow: " + (", ".join(state["allow"]) if state["allow"] else "(all paths)"))
    return 0


def policy_check(args):
    root = repository_root()
    settings, resolved = load_policy(args.path, root)
    policy_sha = file_sha256(resolved, POLICY_MAX_BYTES)
    hook = doctor_repository_state()

    reasons = []
    if not hook["hook_enforced"]:
        reasons.append("managed hook is not enforced")
    if hook["hook_policy_sha256"] is None:
        reasons.append("managed hook has no policy fingerprint")
    elif hook["hook_policy_sha256"] != policy_sha:
        reasons.append("policy fingerprint differs from managed hook snapshot")

    expected = {
        "max_files": settings.max_files,
        "max_lines": settings.max_lines,
        "allow": settings.allow,
    }
    actual = {
        "max_files": hook.get("hook_max_files"),
        "max_lines": hook.get("hook_max_lines"),
        "allow": hook.get("hook_allow"),
    }
    if hook["hook_enforced"] and actual != expected:
        reasons.append("managed hook Layer 0 settings differ from policy")

    state = {
        "schema_version": POLICY_SCHEMA_VERSION,
        "policy_path": str(resolved),
        "policy_sha256": policy_sha,
        "hook_policy_sha256": hook["hook_policy_sha256"],
        "expected": expected,
        "actual": actual,
        "in_sync": not reasons,
        "reasons": reasons,
    }
    if args.json:
        print(json.dumps(state, sort_keys=True, separators=(",", ":")))
    elif state["in_sync"]:
        print("policy_check: in-sync")
        print(f"policy_sha256: {policy_sha}")
    else:
        print("policy_check: drift")
        for reason in reasons:
            print(f"reason: {reason}")
    return 0 if state["in_sync"] else 1


def read_ruleset(path):
    candidate = Path(path)
    try:
        with candidate.open("rb") as stream:
            raw = stream.read(RULESET_MAX_BYTES + 1)
    except OSError as error:
        raise Blocked("cannot read ruleset: " + str(error)) from None
    if len(raw) > RULESET_MAX_BYTES:
        raise Blocked(f"ruleset exceeds {RULESET_MAX_BYTES} bytes")
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise Blocked("ruleset must be valid UTF-8 JSON") from None
    if not isinstance(document, dict):
        raise Blocked("ruleset root must be a JSON object")
    if not isinstance(document.get("rules"), list):
        raise Blocked("ruleset rules must be an array")
    return document


def repository_codeowners(root):
    candidates = (
        root / ".github" / "CODEOWNERS",
        root / "CODEOWNERS",
        root / "docs" / "CODEOWNERS",
    )
    return next((path for path in candidates if path.is_file()), None)


def repository_gate_checks(root):
    checks = []
    workflow_dir = root / ".github" / "workflows"
    if not workflow_dir.is_dir():
        return checks
    for path in sorted(workflow_dir.iterdir()):
        if path.suffix not in (".yml", ".yaml") or not path.is_file():
            continue
        try:
            if path.stat().st_size > RULESET_MAX_BYTES:
                continue
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for name in ("jev0 gate", "jev0 policy gate"):
            if ("name: " + name) in text and name not in checks:
                checks.append(name)
    return checks


def ruleset_check(args):
    ruleset = read_ruleset(args.path)
    try:
        root = repository_root()
    except Blocked:
        root = Path.cwd().resolve()

    by_type = {}
    for rule in ruleset["rules"]:
        if isinstance(rule, dict) and isinstance(rule.get("type"), str):
            by_type[rule["type"]] = rule

    findings = []

    def finding(severity, code, message, recommendation):
        findings.append(
            {
                "severity": severity,
                "code": code,
                "message": message,
                "recommendation": recommendation,
            }
        )

    enforcement = ruleset.get("enforcement")
    if enforcement != "active":
        finding(
            "warning",
            "ruleset_not_active",
            f"ruleset enforcement is {enforcement!r}, not 'active'",
            "Set enforcement status to Active before relying on the ruleset.",
        )

    conditions = ruleset.get("conditions")
    includes = []
    if isinstance(conditions, dict):
        ref_name = conditions.get("ref_name")
        if isinstance(ref_name, dict) and isinstance(ref_name.get("include"), list):
            includes = ref_name["include"]
    if "~DEFAULT_BRANCH" not in includes:
        finding(
            "warning",
            "default_branch_not_targeted",
            "ruleset does not explicitly target the default branch",
            "Target the default branch (or main explicitly) for repository protection.",
        )

    pull_rule = by_type.get("pull_request")
    pull = pull_rule.get("parameters", {}) if isinstance(pull_rule, dict) else {}
    if not isinstance(pull, dict):
        pull = {}
    approvals = pull.get("required_approving_review_count", 0)
    if type(approvals) is not int or approvals < 0:
        finding(
            "error",
            "invalid_required_approvals",
            "required approving review count is not a non-negative integer",
            "Use a valid non-negative approval count.",
        )
        approvals = 0

    approval_switches = {
        "require_code_owner_review": "Code Owner review",
        "require_last_push_approval": "approval from someone other than the last pusher",
        "require_extra_approval_for_unattributed_changes": "extra approval for unattributed changes",
    }
    enabled_switches = [
        label for key, label in approval_switches.items() if pull.get(key) is True
    ]
    if args.solo and (approvals > 0 or enabled_switches):
        details = []
        if approvals > 0:
            details.append(f"{approvals} required approval(s)")
        details.extend(enabled_switches)
        finding(
            "error",
            "solo_review_deadlock",
            "single-maintainer mode conflicts with review requirements: "
            + ", ".join(details),
            "Set required approvals to 0 and disable Code Owner, last-push, and unattributed-change extra approvals for solo operation.",
        )
    elif approvals == 0 and pull.get("require_last_push_approval") is True:
        finding(
            "warning",
            "last_push_approval_with_zero_approvals",
            "last-push approval is enabled even though required approvals is 0",
            "Disable last-push approval unless a second reviewer is intentionally required.",
        )

    codeowners = repository_codeowners(root)
    if pull.get("require_code_owner_review") is True and codeowners is None:
        finding(
            "warning",
            "code_owner_review_without_codeowners",
            "Code Owner review is enabled but no CODEOWNERS file exists in the repository",
            "Add CODEOWNERS intentionally or disable Code Owner review.",
        )

    status_rule = by_type.get("required_status_checks")
    status = status_rule.get("parameters", {}) if isinstance(status_rule, dict) else {}
    configured = []
    if isinstance(status, dict):
        values = status.get("required_status_checks", [])
        if isinstance(values, list):
            for item in values:
                if isinstance(item, dict) and isinstance(item.get("context"), str):
                    configured.append(item["context"])

    discovered = repository_gate_checks(root)
    if status_rule is not None and not configured:
        finding(
            "error",
            "required_status_checks_empty",
            "required status checks rule exists but contains no required checks",
            "Add the stable aggregate checks to the ruleset before treating CI as merge protection.",
        )
    missing_checks = [name for name in discovered if name not in configured]
    if missing_checks:
        finding(
            "error",
            "repository_gates_not_required",
            "repository gate checks are not required by the ruleset: "
            + ", ".join(missing_checks),
            "Add these check names to Required status checks.",
        )

    if "required_linear_history" in by_type:
        merge_methods = pull.get("allowed_merge_methods", [])
        if isinstance(merge_methods, list) and "merge" in merge_methods:
            finding(
                "warning",
                "merge_method_conflicts_with_linear_history",
                "merge commits are listed as allowed while linear history is required",
                "Prefer squash and/or rebase only to keep the UI aligned with the enforced history rule.",
            )

    if "deletion" not in by_type:
        finding(
            "warning",
            "branch_deletion_not_blocked",
            "branch deletion protection is not enabled",
            "Enable Restrict deletions for the protected branch.",
        )
    if "non_fast_forward" not in by_type:
        finding(
            "warning",
            "force_push_not_blocked",
            "force-push protection is not enabled",
            "Enable Block force pushes for the protected branch.",
        )
    if pull_rule is None:
        finding(
            "error",
            "pull_request_not_required",
            "pull requests are not required before updating the protected branch",
            "Enable Require a pull request before merging.",
        )

    errors = sum(item["severity"] == "error" for item in findings)
    warnings = sum(item["severity"] == "warning" for item in findings)
    report = {
        "schema_version": RULESET_REPORT_SCHEMA_VERSION,
        "ruleset_name": ruleset.get("name"),
        "enforcement": enforcement,
        "solo": args.solo,
        "default_branch_targeted": "~DEFAULT_BRANCH" in includes,
        "codeowners": None if codeowners is None else str(codeowners),
        "discovered_checks": discovered,
        "configured_required_checks": configured,
        "errors": errors,
        "warnings": warnings,
        "healthy": errors == 0,
        "findings": findings,
    }

    if args.json:
        print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    else:
        print("ruleset: " + str(report["ruleset_name"] or "unnamed"))
        print("healthy: " + ("yes" if report["healthy"] else "no"))
        print(f"errors: {errors}")
        print(f"warnings: {warnings}")
        for item in findings:
            print(
                f"{item['severity']}: {item['code']}: {item['message']} "
                f"[{item['recommendation']}]"
            )
    return 0 if errors == 0 else 1


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


def commit_ref_argument(value):
    if not value or value.startswith("-") or "\0" in value or "\n" in value or "\r" in value:
        raise argparse.ArgumentTypeError(
            "commit ref must be a non-empty single-line non-option string"
        )
    return value


def policy_argument(value):
    if not value:
        raise argparse.ArgumentTypeError("policy path must not be empty")
    return value


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


def resolve_commit(ref):
    """Resolve an explicit ref to a commit SHA without treating it as an option."""

    if not isinstance(ref, str) or not ref or "\0" in ref or "\n" in ref or "\r" in ref:
        raise Blocked("commit ref must be a non-empty single-line string")
    result = subprocess.run(
        [
            "git",
            "rev-parse",
            "--verify",
            "--quiet",
            "--end-of-options",
            ref + "^{commit}",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode:
        raise Blocked(f"cannot resolve commit ref: {ref!r}")
    sha = result.stdout.decode("ascii", errors="strict").strip()
    if not sha or any(ch not in "0123456789abcdefABCDEF" for ch in sha):
        raise Blocked(f"Git returned an invalid commit id for {ref!r}")
    return sha.lower()


def range_diff_prefix(base_sha, head_sha):
    return ["diff", f"{base_sha}...{head_sha}"]


def diff_prefix(mode):
    if mode == "staged":
        return ["diff", "--cached"]
    if mode == "workspace":
        return ["diff", "HEAD"] if has_head() else ["diff", "--cached"]
    raise ValueError(f"unknown diff mode: {mode}")


def mode_label(mode):
    return "staged" if mode == "staged" else "workspace"


def unified_diff_prefix(prefix, max_bytes=None):
    args = (
        *prefix,
        "--no-ext-diff",
        "--no-textconv",
        "--no-renames",
        "--no-color",
        "--patch",
        "--",
    )
    output = git(*args) if max_bytes is None else git_limited(max_bytes, *args)
    return output.decode("utf-8", errors="replace")


def unified_diff(mode, max_bytes=None):
    return unified_diff_prefix(diff_prefix(mode), max_bytes)


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
    settings, _, _ = resolve_guard_settings(args)
    heuristic_rules(settings, "staged")
    if evaluator is not None:
        run_evaluator(evaluator, evaluator_diff("staged", evaluator))


def workspace(args, evaluator: Optional[GuardEvaluator] = None):
    settings, _, _ = resolve_guard_settings(args)
    heuristic_rules(settings, "workspace")
    if evaluator is not None:
        run_evaluator(evaluator, evaluator_diff("workspace", evaluator))


def inspect_change_set(prefix):
    fields = git_output_limited(
        CHANGE_METADATA_MAX_BYTES,
        "change metadata",
        *prefix,
        "--no-ext-diff",
        "--no-textconv",
        "--no-renames",
        "--numstat",
        "-z",
        "--",
    ).split(b"\0")
    entries = []
    total = 0
    for field in fields:
        if not field:
            continue
        added, removed, raw_path = field.split(b"\t", 2)
        path = os.fsdecode(raw_path)
        if added != b"-" and removed != b"-":
            total += int(added) + int(removed)
        entries.append((added, removed, raw_path, path))

    active = set(
        git_output_limited(
            CHANGE_METADATA_MAX_BYTES,
            "active-path metadata",
            *prefix,
            "--no-ext-diff",
            "--no-renames",
            "--name-only",
            "--diff-filter=ACMRT",
            "-z",
            "--",
        ).split(b"\0")
    )
    paths = [entry[3] for entry in entries]
    return {
        "entries": entries,
        "active": active,
        "files_changed": len(entries),
        "lines_changed": total,
        "paths": paths[:EVIDENCE_MAX_PATHS],
        "paths_truncated": len(paths) > EVIDENCE_MAX_PATHS,
        "paths_total": len(paths),
    }


def enforce_change_set(args, label, analysis):
    entries = analysis["entries"]
    if len(entries) > args.max_files:
        raise Blocked(f"{len(entries)} {label} files exceed budget {args.max_files}")

    for added, removed, raw_path, path in entries:
        if args.allow and not any(
            path == p or path.startswith(p + "/") for p in args.allow
        ):
            raise Blocked(f"outside allowed scope: {path!r}")
        parts = path.lower().split("/")
        if raw_path in analysis["active"] and (
            path.lower().endswith((".gguf", ".bin")) or "models" in parts
        ):
            raise Blocked(f"model artifact: {path!r}")
        if added == b"-" or removed == b"-":
            if raw_path in analysis["active"]:
                raise Blocked(f"binary change requires separate review: {path!r}")

    if analysis["lines_changed"] > args.max_lines:
        raise Blocked(
            f"{analysis['lines_changed']} added/deleted lines exceed budget {args.max_lines}"
        )
    return {
        "files_changed": analysis["files_changed"],
        "lines_changed": analysis["lines_changed"],
        "paths": analysis["paths"],
        "paths_truncated": analysis["paths_truncated"],
        "paths_total": analysis["paths_total"],
    }


def heuristic_rules_for_prefix(args, label, prefix):
    analysis = inspect_change_set(prefix)
    return enforce_change_set(args, label, analysis)


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
    heuristic_rules_for_prefix(args, mode_label(mode), diff_prefix(mode))


def read_evidence(path):
    candidate = Path(path)
    try:
        with candidate.open("rb") as stream:
            raw = stream.read(EVIDENCE_MAX_BYTES + 1)
    except OSError as error:
        raise Blocked("cannot read evidence: " + str(error)) from None
    if len(raw) > EVIDENCE_MAX_BYTES:
        raise Blocked(f"evidence exceeds {EVIDENCE_MAX_BYTES} bytes")

    def strict_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise Blocked(f"duplicate evidence key: {key}")
            result[key] = value
        return result

    try:
        document = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=strict_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise Blocked("evidence must be valid UTF-8 JSON") from None
    if not isinstance(document, dict):
        raise Blocked("evidence root must be a JSON object")
    return document


def evidence_verify(args):
    evidence = read_evidence(args.path)
    required = {
        "schema_version",
        "decision",
        "reason",
        "base_sha",
        "head_sha",
        "merge_base_sha",
        "policy_source",
        "policy_path",
        "policy_sha256",
        "policy",
        "verifier_version",
        "verifier_sha256",
        "files_changed",
        "lines_changed",
        "paths",
        "paths_truncated",
        "paths_total",
        "evidence_sha256",
    }
    if set(evidence) != required:
        missing = sorted(required - set(evidence))
        extra = sorted(set(evidence) - required)
        detail = []
        if missing:
            detail.append("missing=" + ",".join(missing))
        if extra:
            detail.append("extra=" + ",".join(extra))
        raise Blocked("evidence keys mismatch" + (": " + " ".join(detail) if detail else ""))

    if type(evidence["schema_version"]) is not int:
        raise Blocked("evidence schema_version must be an integer")
    if evidence["schema_version"] != EVIDENCE_SCHEMA_VERSION:
        raise Blocked(
            f"unsupported evidence schema_version {evidence['schema_version']}"
        )
    if evidence["decision"] not in ("allow", "block"):
        raise Blocked("evidence decision must be allow or block")
    if evidence["reason"] is not None and not isinstance(evidence["reason"], str):
        raise Blocked("evidence reason must be null or a string")
    if evidence["decision"] == "allow" and evidence["reason"] is not None:
        raise Blocked("allow evidence must not contain a block reason")
    if evidence["decision"] == "block" and (
        not isinstance(evidence["reason"], str) or not evidence["reason"].strip()
    ):
        raise Blocked("block evidence must contain a non-empty reason")
    if evidence["policy_source"] not in ("flags", "worktree", "base"):
        raise Blocked("evidence policy_source is invalid")
    if evidence["policy_source"] == "flags":
        if evidence["policy_path"] is not None or evidence["policy_sha256"] is not None:
            raise Blocked("flag policy evidence must not contain policy file provenance")
    else:
        if not isinstance(evidence["policy_path"], str) or not evidence["policy_path"]:
            raise Blocked("file policy evidence must contain policy_path")
        try:
            policy_tree_path(evidence["policy_path"])
        except argparse.ArgumentTypeError as error:
            raise Blocked("evidence policy_path is invalid: " + str(error)) from None
        value = evidence["policy_sha256"]
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(ch not in "0123456789abcdef" for ch in value)
        ):
            raise Blocked("file policy evidence must contain policy_sha256")
    if not isinstance(evidence["verifier_version"], str) or not evidence["verifier_version"]:
        raise Blocked("evidence verifier_version must be a non-empty string")
    for key in ("base_sha", "head_sha", "merge_base_sha"):
        value = evidence[key]
        if (
            not isinstance(value, str)
            or len(value) not in (40, 64)
            or any(ch not in "0123456789abcdef" for ch in value)
        ):
            raise Blocked(f"evidence {key} must be a lowercase Git object id")
    for key in ("verifier_sha256", "evidence_sha256"):
        value = evidence[key]
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(ch not in "0123456789abcdef" for ch in value)
        ):
            raise Blocked(f"evidence {key} must be a lowercase SHA-256")

    policy = evidence["policy"]
    if not isinstance(policy, dict) or set(policy) != {"max_files", "max_lines", "allow"}:
        raise Blocked("evidence policy shape is invalid")
    if type(policy["max_files"]) is not int or policy["max_files"] <= 0:
        raise Blocked("evidence policy max_files must be a positive integer")
    if type(policy["max_lines"]) is not int or policy["max_lines"] <= 0:
        raise Blocked("evidence policy max_lines must be a positive integer")
    if not isinstance(policy["allow"], list) or any(not isinstance(x, str) for x in policy["allow"]):
        raise Blocked("evidence policy allow must be an array of strings")

    if type(evidence["files_changed"]) is not int or evidence["files_changed"] < 0:
        raise Blocked("evidence files_changed must be a non-negative integer")
    if type(evidence["lines_changed"]) is not int or evidence["lines_changed"] < 0:
        raise Blocked("evidence lines_changed must be a non-negative integer")
    if type(evidence["paths_total"]) is not int or evidence["paths_total"] < 0:
        raise Blocked("evidence paths_total must be a non-negative integer")
    if type(evidence["paths_truncated"]) is not bool:
        raise Blocked("evidence paths_truncated must be a boolean")
    if not isinstance(evidence["paths"], list) or any(not isinstance(x, str) for x in evidence["paths"]):
        raise Blocked("evidence paths must be an array of strings")
    if len(evidence["paths"]) > EVIDENCE_MAX_PATHS:
        raise Blocked(f"evidence paths exceed {EVIDENCE_MAX_PATHS} entries")
    if evidence["paths_total"] < len(evidence["paths"]):
        raise Blocked("evidence paths_total cannot be smaller than paths length")
    if evidence["files_changed"] != evidence["paths_total"]:
        raise Blocked("evidence files_changed must equal paths_total")
    if evidence["paths_truncated"] != (evidence["paths_total"] > len(evidence["paths"])):
        raise Blocked("evidence paths_truncated is inconsistent with paths_total")

    supplied = evidence["evidence_sha256"]
    unsigned = dict(evidence)
    del unsigned["evidence_sha256"]
    canonical = json.dumps(
        unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    computed = hashlib.sha256(canonical).hexdigest()
    if supplied != computed:
        raise Blocked("evidence digest mismatch")

    verifier_match = evidence["verifier_sha256"] == executable_sha256()
    if args.require_current_verifier and not verifier_match:
        raise Blocked("evidence verifier does not match current jev0")

    repository_match = None
    if args.repo_check:
        root = repository_root()
        actual_base = resolve_commit(evidence["base_sha"])
        actual_head = resolve_commit(evidence["head_sha"])
        if actual_base != evidence["base_sha"]:
            raise Blocked("evidence base_sha does not resolve exactly in repository")
        if actual_head != evidence["head_sha"]:
            raise Blocked("evidence head_sha does not resolve exactly in repository")

        actual_merge_base = git("merge-base", actual_base, actual_head).decode("ascii").strip()
        if actual_merge_base != evidence["merge_base_sha"]:
            raise Blocked("evidence merge_base_sha does not match repository")

        settings = argparse.Namespace(
            max_files=evidence["policy"]["max_files"],
            max_lines=evidence["policy"]["max_lines"],
            allow=evidence["policy"]["allow"],
        )
        analysis = inspect_change_set(range_diff_prefix(actual_base, actual_head))
        actual_stats = {
            "files_changed": analysis["files_changed"],
            "lines_changed": analysis["lines_changed"],
            "paths": analysis["paths"],
            "paths_truncated": analysis["paths_truncated"],
            "paths_total": analysis["paths_total"],
        }
        for key, value in actual_stats.items():
            if evidence[key] != value:
                raise Blocked(f"evidence {key} does not match repository diff")

        actual_decision = "allow"
        actual_reason = None
        try:
            enforce_change_set(settings, "range", analysis)
        except Blocked as error:
            actual_decision = "block"
            actual_reason = str(error)
        if evidence["decision"] != actual_decision:
            raise Blocked("evidence decision does not match repository diff")
        if evidence["reason"] != actual_reason:
            raise Blocked("evidence reason does not match repository diff")

        if evidence["policy_source"] == "base":
            actual_settings, tree_path, policy_sha = load_policy_from_commit(
                actual_base, evidence["policy_path"]
            )
            if policy_sha != evidence["policy_sha256"]:
                raise Blocked("evidence base policy SHA-256 does not match repository")
            if {
                "max_files": actual_settings.max_files,
                "max_lines": actual_settings.max_lines,
                "allow": actual_settings.allow,
            } != evidence["policy"]:
                raise Blocked("evidence base policy values do not match repository")
            if tree_path != evidence["policy_path"]:
                raise Blocked("evidence base policy path does not match repository")
        elif evidence["policy_source"] == "worktree":
            policy_path = root / evidence["policy_path"]
            try:
                resolved = policy_path.resolve(strict=True)
                resolved.relative_to(root.resolve())
            except (OSError, ValueError):
                raise Blocked(
                    "evidence worktree policy is not available inside current repository"
                ) from None
            if file_sha256(resolved, POLICY_MAX_BYTES) != evidence["policy_sha256"]:
                raise Blocked("evidence worktree policy SHA-256 does not match repository")
            current_settings, _ = load_policy(str(resolved), root)
            if {
                "max_files": current_settings.max_files,
                "max_lines": current_settings.max_lines,
                "allow": current_settings.allow,
            } != evidence["policy"]:
                raise Blocked("evidence worktree policy values do not match repository")

        repository_match = True

    result = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "valid": True,
        "evidence_sha256": supplied,
        "verifier_sha256": evidence["verifier_sha256"],
        "current_verifier_match": verifier_match,
        "repository_match": repository_match,
    }
    if not getattr(args, "quiet", False):
        if args.json:
            print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        else:
            print("evidence: valid")
            print(f"evidence_sha256: {supplied}")
            print(f"verifier_sha256: {evidence['verifier_sha256']}")
            print("current_verifier_match: " + ("yes" if verifier_match else "no"))
            if repository_match is not None:
                print("repository_match: " + ("yes" if repository_match else "no"))
    return 0



def positive_int_argument(value):
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a positive integer") from None
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def read_provenance(path):
    candidate = Path(path)
    try:
        with candidate.open("rb") as stream:
            raw = stream.read(PROVENANCE_MAX_BYTES + 1)
    except OSError as error:
        raise Blocked("cannot read provenance: " + str(error)) from None
    if len(raw) > PROVENANCE_MAX_BYTES:
        raise Blocked(f"provenance exceeds {PROVENANCE_MAX_BYTES} bytes")

    def strict_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise Blocked(f"duplicate provenance key: {key}")
            result[key] = value
        return result

    try:
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=strict_object)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise Blocked("provenance must be valid UTF-8 JSON") from None
    if not isinstance(document, dict):
        raise Blocked("provenance root must be a JSON object")
    return document


def provenance_create(args):
    verify_args = argparse.Namespace(
        path=args.evidence,
        json=False,
        require_current_verifier=args.require_current_verifier,
        repo_check=args.repo_check,
        quiet=True,
    )
    evidence_verify(verify_args)
    evidence = read_evidence(args.evidence)

    state = {
        "schema_version": PROVENANCE_SCHEMA_VERSION,
        "issuer": "github-actions",
        "repository": args.repository,
        "workflow_ref": args.workflow_ref,
        "run_id": args.run_id,
        "run_attempt": args.run_attempt,
        "event_name": args.event_name,
        "pr_number": args.pr_number,
        "base_sha": evidence["base_sha"],
        "head_sha": evidence["head_sha"],
        "evidence_sha256": evidence["evidence_sha256"],
        "verifier_sha256": evidence["verifier_sha256"],
    }
    canonical = json.dumps(
        state, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    state["provenance_sha256"] = hashlib.sha256(canonical).hexdigest()
    print(json.dumps(state, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
    return 0


def provenance_verify(args):
    provenance = read_provenance(args.provenance)
    required = {
        "schema_version",
        "issuer",
        "repository",
        "workflow_ref",
        "run_id",
        "run_attempt",
        "event_name",
        "pr_number",
        "base_sha",
        "head_sha",
        "evidence_sha256",
        "verifier_sha256",
        "provenance_sha256",
    }
    if set(provenance) != required:
        raise Blocked("provenance keys mismatch")
    if provenance["schema_version"] != PROVENANCE_SCHEMA_VERSION:
        raise Blocked(
            f"unsupported provenance schema_version {provenance['schema_version']}"
        )
    if provenance["issuer"] != "github-actions":
        raise Blocked("provenance issuer must be github-actions")
    for key in ("repository", "workflow_ref", "event_name"):
        if not isinstance(provenance[key], str) or not provenance[key]:
            raise Blocked(f"provenance {key} must be a non-empty string")
    for key in ("run_id", "run_attempt", "pr_number"):
        if type(provenance[key]) is not int or provenance[key] <= 0:
            raise Blocked(f"provenance {key} must be a positive integer")
    for key in ("base_sha", "head_sha"):
        value = provenance[key]
        if (
            not isinstance(value, str)
            or len(value) not in (40, 64)
            or any(ch not in "0123456789abcdef" for ch in value)
        ):
            raise Blocked(f"provenance {key} must be a lowercase Git object id")
    for key in ("evidence_sha256", "verifier_sha256", "provenance_sha256"):
        value = provenance[key]
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(ch not in "0123456789abcdef" for ch in value)
        ):
            raise Blocked(f"provenance {key} must be a lowercase SHA-256")

    supplied = provenance["provenance_sha256"]
    unsigned = dict(provenance)
    del unsigned["provenance_sha256"]
    canonical = json.dumps(
        unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    if supplied != hashlib.sha256(canonical).hexdigest():
        raise Blocked("provenance digest mismatch")

    verify_args = argparse.Namespace(
        path=args.evidence,
        json=False,
        require_current_verifier=args.require_current_verifier,
        repo_check=args.repo_check,
        quiet=True,
    )
    evidence_verify(verify_args)
    evidence = read_evidence(args.evidence)
    linked = {
        "base_sha": evidence["base_sha"],
        "head_sha": evidence["head_sha"],
        "evidence_sha256": evidence["evidence_sha256"],
        "verifier_sha256": evidence["verifier_sha256"],
    }
    for key, value in linked.items():
        if provenance[key] != value:
            raise Blocked(f"provenance {key} does not match evidence")

    expectations = {
        "repository": args.expect_repository,
        "workflow_ref": args.expect_workflow_ref,
        "run_id": args.expect_run_id,
        "pr_number": args.expect_pr_number,
    }
    for key, expected in expectations.items():
        if expected is not None and provenance[key] != expected:
            raise Blocked(f"provenance {key} does not match expected value")

    result = {
        "schema_version": PROVENANCE_SCHEMA_VERSION,
        "valid": True,
        "provenance_sha256": supplied,
        "evidence_sha256": evidence["evidence_sha256"],
        "repository": provenance["repository"],
        "workflow_ref": provenance["workflow_ref"],
        "run_id": provenance["run_id"],
        "run_attempt": provenance["run_attempt"],
        "pr_number": provenance["pr_number"],
    }
    if args.json:
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    else:
        print("provenance: valid")
        print(f"provenance_sha256: {supplied}")
        print(f"evidence_sha256: {evidence['evidence_sha256']}")
        print(f"repository: {provenance['repository']}")
        print(f"workflow_ref: {provenance['workflow_ref']}")
        print(f"run_id: {provenance['run_id']}")
        print(f"run_attempt: {provenance['run_attempt']}")
        print(f"pr_number: {provenance['pr_number']}")
    return 0


def evidence_object_id_argument(value):
    if (
        len(value) not in (40, 64)
        or any(ch not in "0123456789abcdef" for ch in value)
    ):
        raise argparse.ArgumentTypeError(
            "expected boundary must be a lowercase 40- or 64-character Git object id"
        )
    return value


def evidence_chain_verify(args):
    """Verify a contiguous sequence of already-produced range evidence."""

    documents = []
    seen = set()
    for path in args.paths:
        verify_args = argparse.Namespace(
            path=path,
            json=False,
            require_current_verifier=args.require_current_verifier,
            repo_check=args.repo_check,
            quiet=True,
        )
        evidence_verify(verify_args)
        document = read_evidence(path)
        digest = document["evidence_sha256"]
        if digest in seen:
            raise Blocked("evidence chain contains a duplicate evidence digest")
        seen.add(digest)
        documents.append(document)

    for index in range(1, len(documents)):
        previous = documents[index - 1]
        current = documents[index]
        if previous["head_sha"] != current["base_sha"]:
            raise Blocked(
                "evidence chain gap at item "
                f"{index + 1}: previous head_sha does not equal current base_sha"
            )

    first_base_sha = documents[0]["base_sha"]
    final_head_sha = documents[-1]["head_sha"]
    if (
        args.expect_first_base is not None
        and first_base_sha != args.expect_first_base
    ):
        raise Blocked("evidence chain first base does not match expected boundary")
    if (
        args.expect_final_head is not None
        and final_head_sha != args.expect_final_head
    ):
        raise Blocked("evidence chain final head does not match expected boundary")

    digests = [document["evidence_sha256"] for document in documents]
    canonical = json.dumps(
        digests, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    result = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "valid": True,
        "entries": len(documents),
        "first_base_sha": first_base_sha,
        "final_head_sha": final_head_sha,
        "expected_first_base_sha": args.expect_first_base,
        "expected_final_head_sha": args.expect_final_head,
        "evidence_sha256s": digests,
        "chain_sha256": hashlib.sha256(canonical).hexdigest(),
        "current_verifier_match": all(
            document["verifier_sha256"] == executable_sha256()
            for document in documents
        ),
        "repository_checked": bool(args.repo_check),
    }
    if args.json:
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    else:
        print("evidence_chain: valid")
        print(f"entries: {result['entries']}")
        print(f"first_base_sha: {result['first_base_sha']}")
        print(f"final_head_sha: {result['final_head_sha']}")
        print(f"chain_sha256: {result['chain_sha256']}")
        print(
            "current_verifier_match: "
            + ("yes" if result["current_verifier_match"] else "no")
        )
        print("repository_checked: " + ("yes" if args.repo_check else "no"))
    return 0


def range_report(args):
    repository_root()
    base_sha = resolve_commit(args.base)
    head_sha = resolve_commit(args.head)
    merge_base = git("merge-base", base_sha, head_sha).decode("ascii").strip()

    policy_source = "flags"
    policy_sha256 = None
    policy_path = None
    if args.base_policy is not None:
        settings, tree_path, policy_sha256 = load_policy_from_commit(
            base_sha, args.base_policy
        )
        policy_source = "base"
        policy_path = tree_path
    elif args.policy is not None:
        root = Path.cwd().resolve()
        settings, resolved = load_policy(args.policy, root)
        policy_source = "worktree"
        policy_path = resolved.relative_to(root).as_posix()
        policy_sha256 = file_sha256(resolved, POLICY_MAX_BYTES)
    else:
        settings = argparse.Namespace(
            max_files=DEFAULT_MAX_FILES if args.max_files is None else args.max_files,
            max_lines=DEFAULT_MAX_LINES if args.max_lines is None else args.max_lines,
            allow=[] if args.allow is None else args.allow,
        )

    prefix = range_diff_prefix(base_sha, head_sha)
    analysis = inspect_change_set(prefix)
    decision = "allow"
    reason = None
    try:
        stats = enforce_change_set(settings, "range", analysis)
    except Blocked as error:
        decision = "block"
        reason = str(error)
        stats = {
            "files_changed": analysis["files_changed"],
            "lines_changed": analysis["lines_changed"],
            "paths": analysis["paths"],
            "paths_truncated": analysis["paths_truncated"],
            "paths_total": analysis["paths_total"],
        }

    state = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "decision": decision,
        "reason": reason,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "merge_base_sha": merge_base,
        "policy_source": policy_source,
        "policy_path": policy_path,
        "policy_sha256": policy_sha256,
        "policy": {
            "max_files": settings.max_files,
            "max_lines": settings.max_lines,
            "allow": settings.allow,
        },
        "verifier_version": VERSION,
        "verifier_sha256": executable_sha256(),
        **stats,
    }
    canonical = json.dumps(
        state, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    state["evidence_sha256"] = hashlib.sha256(canonical).hexdigest()
    print(json.dumps(state, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
    return 0 if decision == "allow" else 1


def range_guard(args, evaluator: Optional[GuardEvaluator] = None):
    root = repository_root()
    base_sha = resolve_commit(args.base)
    head_sha = resolve_commit(args.head)
    if args.base_policy is not None:
        settings, _, _ = load_policy_from_commit(base_sha, args.base_policy)
    else:
        settings, _, _ = resolve_guard_settings(args)
    prefix = range_diff_prefix(base_sha, head_sha)
    heuristic_rules_for_prefix(settings, "range", prefix)
    if evaluator is not None:
        max_bytes = (
            evaluator.max_diff_bytes
            if isinstance(evaluator, ProcessEvaluator)
            else None
        )
        run_evaluator(evaluator, unified_diff_prefix(prefix, max_bytes))


def ensure_managed_hook(hook, content):
    """Install a managed hook atomically without overwriting user content."""

    if hook.is_symlink():
        raise Blocked("existing hook is a symlink; integrate manually")
    if hook.exists():
        if hook.read_text() == content:
            if not os.access(hook, os.X_OK):
                hook.chmod(0o755)
                return "repaired"
            return "unchanged"
        raise Blocked(
            "existing pre-commit hook preserved; integrate jev0 staged manually"
        )

    hook.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".jev0-pre-commit-", dir=hook.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o755)
        try:
            os.link(temporary, hook)
        except FileExistsError:
            # Another process won the create race. Accept only an identical
            # managed hook; never replace content we did not create.
            if hook.is_symlink():
                raise Blocked("existing hook is a symlink; integrate manually") from None
            if hook.exists() and hook.read_text() == content:
                if not os.access(hook, os.X_OK):
                    hook.chmod(0o755)
                    return "repaired"
                return "unchanged"
            raise Blocked(
                "existing pre-commit hook preserved; integrate jev0 staged manually"
            ) from None
        return "installed"
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def init(args):
    settings, policy_file, root = resolve_guard_settings(args)
    custom = subprocess.run(
        ["git", "config", "--get", "core.hooksPath"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if custom.returncode == 0:
        raise Blocked(
            "custom core.hooksPath: integrate jev0 staged into your existing hook manager"
        )
    if custom.returncode != 1:
        detail = custom.stderr.decode(errors="replace").strip()
        raise Blocked("Git operation failed: " + detail)
    hook = Path(
        os.fsdecode(git("rev-parse", "--git-path", "hooks/pre-commit").rstrip(b"\n"))
    ).absolute()
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "staged",
        "--max-files",
        str(settings.max_files),
        "--max-lines",
        str(settings.max_lines),
    ]
    for allowed in settings.allow:
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
    policy_comment = ""
    if policy_file is not None:
        policy_comment = (
            "# jev0 policy sha256: "
            + file_sha256(policy_file, POLICY_MAX_BYTES)
            + "\n"
        )
    content = (
        "#!/bin/sh\n# jev0 managed pre-commit hook\n"
        + policy_comment
        + "exec "
        + shlex.join(command)
        + "\n"
    )
    outcome = ensure_managed_hook(hook, content)
    if outcome == "installed":
        print(f"jev0: installed pre-commit hook for {root}", file=sys.stderr)
    elif outcome == "repaired":
        print(f"jev0: repaired pre-commit hook for {root}", file=sys.stderr)


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
    item.add_argument("--policy", type=policy_argument)
    item.add_argument("--max-files", type=positive)
    item.add_argument("--max-lines", type=positive)
    item.add_argument("--allow", type=scope, action="append")
    item.add_argument("--evaluator-command", type=evaluator_command)
    item.add_argument("--evaluator-timeout", type=duration, default=30.0)
    item.add_argument("--max-diff-bytes", type=positive, default=1_000_000)
    item.add_argument("--max-evaluator-output-bytes", type=positive, default=4096)
    item.add_argument("--capture-failure", action="store_true")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=f"jev0 {VERSION}")
    sub = parser.add_subparsers(dest="action", required=True)
    for name in ("staged", "workspace", "init"):
        add_guard_arguments(sub.add_parser(name))
    item = sub.add_parser("range")
    item.add_argument("base", type=commit_ref_argument)
    item.add_argument("head", type=commit_ref_argument)
    item.add_argument("--base-policy", type=policy_tree_path)
    add_guard_arguments(item)
    item = sub.add_parser("range-report")
    item.add_argument("base", type=commit_ref_argument)
    item.add_argument("head", type=commit_ref_argument)
    item.add_argument("--base-policy", type=policy_tree_path)
    item.add_argument("--policy", type=policy_argument)
    item.add_argument("--max-files", type=positive)
    item.add_argument("--max-lines", type=positive)
    item.add_argument("--allow", type=scope, action="append")
    item = sub.add_parser("run")
    item.add_argument("--timeout", type=duration, required=True)
    item.add_argument("command", nargs=argparse.REMAINDER)
    item = sub.add_parser("failures")
    failure_sub = item.add_subparsers(dest="failure_action", required=True)
    failure_item = failure_sub.add_parser("list")
    failure_item.add_argument("--json", action="store_true")
    failure_item = failure_sub.add_parser("show")
    failure_item.add_argument("failure_id")
    item = sub.add_parser("doctor")
    item.add_argument("--json", action="store_true")
    item = sub.add_parser("policy")
    item.add_argument("path", type=policy_argument)
    item.add_argument("--json", action="store_true")
    item = sub.add_parser("policy-check")
    item.add_argument("path", type=policy_argument)
    item.add_argument("--json", action="store_true")
    item = sub.add_parser("evidence-verify")
    item.add_argument("path")
    item.add_argument("--json", action="store_true")
    item.add_argument("--require-current-verifier", action="store_true")
    item.add_argument("--repo-check", action="store_true")
    item = sub.add_parser("evidence-chain-verify")
    item.add_argument("paths", nargs="+")
    item.add_argument("--json", action="store_true")
    item.add_argument("--require-current-verifier", action="store_true")
    item.add_argument("--repo-check", action="store_true")
    item.add_argument("--expect-first-base", type=evidence_object_id_argument)
    item.add_argument("--expect-final-head", type=evidence_object_id_argument)
    item = sub.add_parser("provenance-create")
    item.add_argument("evidence")
    item.add_argument("--repository", required=True)
    item.add_argument("--workflow-ref", required=True)
    item.add_argument("--run-id", type=positive_int_argument, required=True)
    item.add_argument("--run-attempt", type=positive_int_argument, required=True)
    item.add_argument("--event-name", required=True)
    item.add_argument("--pr-number", type=positive_int_argument, required=True)
    item.add_argument("--require-current-verifier", action="store_true")
    item.add_argument("--repo-check", action="store_true")
    item = sub.add_parser("provenance-verify")
    item.add_argument("provenance")
    item.add_argument("evidence")
    item.add_argument("--json", action="store_true")
    item.add_argument("--require-current-verifier", action="store_true")
    item.add_argument("--repo-check", action="store_true")
    item.add_argument("--expect-repository")
    item.add_argument("--expect-workflow-ref")
    item.add_argument("--expect-run-id", type=positive_int_argument)
    item.add_argument("--expect-pr-number", type=positive_int_argument)
    item = sub.add_parser("ruleset-check")
    item.add_argument("path")
    item.add_argument("--json", action="store_true")
    item.add_argument("--solo", action="store_true")
    args = parser.parse_args()
    if args.action in ("staged", "workspace", "init", "range", "range-report") and args.policy is not None:
        if args.max_files is not None or args.max_lines is not None or args.allow:
            parser.error("--policy cannot be combined with --max-files, --max-lines, or --allow")
    if args.action in ("range", "range-report") and args.base_policy is not None:
        if (
            args.policy is not None
            or args.max_files is not None
            or args.max_lines is not None
            or args.allow
        ):
            parser.error(
                "--base-policy cannot be combined with --policy, --max-files, --max-lines, or --allow"
            )
    try:
        if args.action in ("staged", "workspace", "range"):
            evaluator = build_evaluator(args)
            return {
                "staged": staged,
                "workspace": workspace,
                "range": range_guard,
            }[args.action](args, evaluator) or 0
        return {
            "init": init,
            "run": run,
            "doctor": doctor,
            "policy": inspect_policy,
            "policy-check": policy_check,
            "evidence-verify": evidence_verify,
            "evidence-chain-verify": evidence_chain_verify,
            "provenance-create": provenance_create,
            "provenance-verify": provenance_verify,
            "ruleset-check": ruleset_check,
            "range-report": range_report,
            "failures": (failures_list if getattr(args, "failure_action", None) == "list" else failures_show),
        }[args.action](args) or 0
    except (Blocked, OSError, ValueError) as error:
        if getattr(args, "capture_failure", False):
            try:
                capture_failure(args, error)
            except (Blocked, OSError, ValueError):
                # Failure capture is observational only and must never replace
                # or soften the original guard decision.
                pass
        print("jev0: " + " ".join(str(error).splitlines()), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
