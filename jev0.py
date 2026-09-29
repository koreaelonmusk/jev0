#!/usr/bin/env python3
"""Local, deterministic Git change budgets and bounded command execution."""
import argparse
import math
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys


class Blocked(Exception):
    pass


def git(*args):
    result = subprocess.run(['git', *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise Blocked('Git operation failed: ' + result.stderr.decode(errors='replace').strip())
    return result.stdout


def positive(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError('must be a positive integer')
    return number


def duration(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError('must be a finite positive number')
    return number


def scope(value):
    path = value.rstrip('/')
    if not path or path.startswith('/') or any(p in ('', '.', '..') for p in path.split('/')):
        raise argparse.ArgumentTypeError('use a repository-relative file or directory without . or ..')
    return path


def staged(args):
    git('rev-parse', '--show-toplevel')
    # Run at the root so scope paths have one meaning even from a subdirectory.
    root = os.fsdecode(git('rev-parse', '--show-toplevel').rstrip(b'\n'))
    os.chdir(root)
    fields = git('diff', '--cached', '--no-ext-diff', '--no-textconv', '--no-renames',
                 '--numstat', '-z', '--').split(b'\0')
    entries = [field.split(b'\t', 2) for field in fields if field]
    if len(entries) > args.max_files:
        raise Blocked(f'{len(entries)} staged files exceed budget {args.max_files}')
    # Only additions/modifications need artifact checks; deleting a model is allowed.
    active = set(git('diff', '--cached', '--no-ext-diff', '--no-renames',
                     '--name-only', '--diff-filter=ACMRT', '-z', '--').split(b'\0'))
    total = 0
    for added, removed, raw_path in entries:
        path = os.fsdecode(raw_path)
        if args.allow and not any(path == p or path.startswith(p + '/') for p in args.allow):
            raise Blocked(f'outside allowed scope: {path!r}')
        parts = path.lower().split('/')
        if raw_path in active and (path.lower().endswith(('.gguf', '.bin')) or 'models' in parts):
            raise Blocked(f'model artifact: {path!r}')
        if added == b'-' or removed == b'-':
            if raw_path in active:
                raise Blocked(f'binary change requires separate review: {path!r}')
        else:
            total += int(added) + int(removed)
    if total > args.max_lines:
        raise Blocked(f'{total} added/deleted lines exceed budget {args.max_lines}')


def init(args):
    root = Path(os.fsdecode(git('rev-parse', '--show-toplevel').rstrip(b'\n')))
    custom = subprocess.run(['git', 'config', '--get', 'core.hooksPath'], stdout=subprocess.PIPE)
    if custom.returncode != 1:
        raise Blocked('custom core.hooksPath: integrate jev0 staged into your existing hook manager')
    hook = Path(os.fsdecode(git('rev-parse', '--git-path', 'hooks/pre-commit').rstrip(b'\n'))).absolute()
    command = [sys.executable, str(Path(__file__).resolve()), 'staged',
               '--max-files', str(args.max_files), '--max-lines', str(args.max_lines)]
    for allowed in args.allow:
        command.extend(['--allow', allowed])
    content = '#!/bin/sh\n# jev0 managed pre-commit hook\nexec ' + shlex.join(command) + '\n'
    if hook.is_symlink():
        raise Blocked('existing hook is a symlink; integrate manually')
    if hook.exists():
        if hook.read_text() == content and os.access(hook, os.X_OK):
            return
        raise Blocked('existing pre-commit hook preserved; integrate jev0 staged manually')
    hook.parent.mkdir(parents=True, exist_ok=True)
    with hook.open('x') as stream:
        stream.write(content)
    hook.chmod(0o755)
    print(f'jev0: installed pre-commit hook for {root}', file=sys.stderr)


def run(args):
    command = args.command
    if command[:1] == ['--']:
        command = command[1:]
    if not command:
        raise Blocked('run requires a command after --')
    if os.name != 'posix':
        raise Blocked('run currently requires macOS or Linux process groups')
    process = subprocess.Popen(command, start_new_session=True)
    try:
        returncode = process.wait(timeout=args.timeout)
    except (subprocess.TimeoutExpired, KeyboardInterrupt) as error:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        if isinstance(error, KeyboardInterrupt):
            raise Blocked('command interrupted') from None
        raise Blocked(f'command exceeded {args.timeout:g}s; process group terminated') from None
    return returncode if returncode >= 0 else 128 - returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    for name in ('staged', 'init'):
        item = sub.add_parser(name)
        item.add_argument('--max-files', type=positive, default=20)
        item.add_argument('--max-lines', type=positive, default=500)
        item.add_argument('--allow', type=scope, action='append', default=[])
    item = sub.add_parser('run')
    item.add_argument('--timeout', type=duration, required=True)
    item.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        return {'staged': staged, 'init': init, 'run': run}[args.action](args) or 0
    except (Blocked, OSError, ValueError) as error:
        print('jev0: ' + ' '.join(str(error).splitlines()), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
