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
GIT_ERROR_BYTES = 8192
DOCTOR_SCHEMA_VERSION = 1
DOCTOR_MAX_FILE_BYTES = 10_000_000
DOCTOR_HOOK_PREFIX_BYTES = 16_384
POLICY_SCHEMA_VERSION = 1
POLICY_MAX_BYTES = 65_536
DEFAULT_MAX_FILES = 20
DEFAULT_MAX_LINES = 500


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
        }

    managed = "# jev0 managed pre-commit hook" in first_lines
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
    }
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

    if len(command) < 3 or command[2] != "staged":
        state["hook_status"] = "managed-invalid"
        return state

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
        size = resolved.stat().st_size
    except OSError as error:
        raise Blocked("cannot stat policy: " + str(error)) from None
    if size > POLICY_MAX_BYTES:
        raise Blocked(f"policy exceeds {POLICY_MAX_BYTES} bytes")

    try:
        raw = resolved.read_bytes()
        document = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
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
    ), resolved


def resolve_guard_settings(args):
    """Resolve CLI defaults or one explicit policy into Layer 0 settings."""

    root = repository_root()
    policy = getattr(args, "policy", None)
    if policy:
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
    settings, _, _ = resolve_guard_settings(args)
    heuristic_rules(settings, "staged")
    if evaluator is not None:
        run_evaluator(evaluator, evaluator_diff("staged", evaluator))


def workspace(args, evaluator: Optional[GuardEvaluator] = None):
    settings, _, _ = resolve_guard_settings(args)
    heuristic_rules(settings, "workspace")
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
    ]
    if policy_file is not None:
        command.extend(["--policy", str(policy_file)])
    else:
        command.extend(
            [
                "--max-files",
                str(settings.max_files),
                "--max-lines",
                str(settings.max_lines),
            ]
        )
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
    content = (
        "#!/bin/sh\n# jev0 managed pre-commit hook\nexec "
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
    item.add_argument("--policy")
    item.add_argument("--max-files", type=positive)
    item.add_argument("--max-lines", type=positive)
    item.add_argument("--allow", type=scope, action="append")
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
    item = sub.add_parser("doctor")
    item.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if args.action in ("staged", "workspace", "init") and args.policy:
        if args.max_files is not None or args.max_lines is not None or args.allow:
            parser.error("--policy cannot be combined with --max-files, --max-lines, or --allow")
    try:
        if args.action in ("staged", "workspace"):
            evaluator = build_evaluator(args)
            return {"staged": staged, "workspace": workspace}[args.action](
                args, evaluator
            ) or 0
        return {"init": init, "run": run, "doctor": doctor}[args.action](args) or 0
    except (Blocked, OSError, ValueError) as error:
        print("jev0: " + " ".join(str(error).splitlines()), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
