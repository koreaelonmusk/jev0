import json
import os
import shlex
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / 'jev0.py'


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        self.env = dict(os.environ, GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull)
        self.git('init', '-q')
        self.git('config', 'user.email', 'test@example.invalid')
        self.git('config', 'user.name', 'Test')
        self.git('config', 'core.autocrlf', 'false')

    def git(self, *args):
        return subprocess.run(['git', *args], cwd=self.repo, env=self.env,
                              check=True, capture_output=True).stdout

    def cli(self, *args, cwd=None):
        return subprocess.run([sys.executable, str(CLI), *args], cwd=cwd or self.repo,
                              env=self.env, capture_output=True, text=True, timeout=10)

    def stage(self, name, data=b'x\n'):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        self.git('add', '-f', '--', name)

    def blocked(self, result, reason):
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(reason, result.stderr)
        self.assertEqual(len(result.stderr.splitlines()), 1)
        self.assertEqual(result.stdout, '')

    def test_empty_index(self):
        result = self.cli('staged')
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, '', ''))

    def test_staged_only(self):
        self.stage('small.py')
        (self.repo / 'small.py').write_text('large\n' * 1000)
        self.assertEqual(self.cli('staged', '--max-lines', '1').returncode, 0)

    def test_line_budget(self):
        self.stage('a', b'a\nb\n')
        self.blocked(self.cli('staged', '--max-lines', '1'), '2 added/deleted')

    def test_file_budget(self):
        self.stage('a')
        self.stage('b')
        self.blocked(self.cli('staged', '--max-files', '1'), '2 staged files')

    def test_artifacts(self):
        for name in ('weights.gguf', 'weights.BIN', 'nested/models/readme'):
            with self.subTest(name=name):
                self.stage(name)
                self.blocked(self.cli('staged'), 'model artifact')
                self.git('rm', '--cached', '--', name)

    def test_binary(self):
        self.stage('blob', b'\0\1')
        self.blocked(self.cli('staged'), 'binary change')

    def test_scope_boundary(self):
        self.stage('src-other/a')
        self.blocked(self.cli('staged', '--allow', 'src'), 'outside allowed scope')

    def test_scope_from_subdirectory(self):
        self.stage('src/a')
        self.assertEqual(self.cli('staged', '--allow', 'src', cwd=self.repo / 'src').returncode, 0)

    def test_exact_file_and_multiple_scopes(self):
        self.stage('src/a')
        self.stage('README.md')
        self.assertEqual(self.cli('staged', '--allow', 'src/', '--allow', 'README.md').returncode, 0)

    def test_unusual_filename(self):
        self.stage('odd\tline\nname')
        self.blocked(self.cli('staged', '--allow', 'src'), 'outside allowed scope')

    def test_deletions_count(self):
        self.stage('a', b'a\nb\n')
        self.git('commit', '-qm', 'base')
        self.git('rm', 'a')
        self.blocked(self.cli('staged', '--max-lines', '1'), '2 added/deleted')

    def test_model_deletion_allowed(self):
        self.stage('old.gguf', b'\0model')
        self.git('commit', '-qm', 'base')
        self.git('rm', 'old.gguf')
        self.assertEqual(self.cli('staged').returncode, 0)

    def test_rename_checks_old_scope(self):
        self.stage('outside/a')
        self.git('commit', '-qm', 'base')
        (self.repo / 'src').mkdir()
        self.git('mv', 'outside/a', 'src/a')
        self.blocked(self.cli('staged', '--allow', 'src'), 'outside allowed scope')

    def test_block_preserves_index_and_worktree(self):
        self.stage('weights.gguf')
        before = self.git('diff', '--cached', '--binary')
        self.blocked(self.cli('staged'), 'model artifact')
        self.assertEqual(before, self.git('diff', '--cached', '--binary'))
        self.assertEqual((self.repo / 'weights.gguf').read_bytes(), b'x\n')

    def test_hook_idempotent_and_commit_blocked(self):
        self.assertEqual(self.cli('init').returncode, 0)
        hook = self.repo / '.git/hooks/pre-commit'
        before = hook.read_bytes()
        self.assertEqual(self.cli('init').returncode, 0)
        self.assertEqual(hook.read_bytes(), before)
        self.stage('weights.gguf')
        result = subprocess.run(['git', 'commit', '-qm', 'blocked'], cwd=self.repo,
                                env=self.env, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'model artifact', result.stderr)
        self.assertEqual((self.repo / 'weights.gguf').read_bytes(), b'x\n')

    def test_init_repairs_managed_hook_execute_bit(self):
        self.assertEqual(self.cli('init').returncode, 0)
        hook = self.repo / '.git/hooks/pre-commit'
        hook.chmod(0o644)
        result = self.cli('init')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(os.access(hook, os.X_OK))
        self.assertIn('repaired pre-commit hook', result.stderr)

    def test_init_leaves_no_temporary_hook_files(self):
        self.assertEqual(self.cli('init').returncode, 0)
        hooks = self.repo / '.git/hooks'
        self.assertEqual(list(hooks.glob('.jev0-pre-commit-*')), [])

    def test_existing_hook_preserved(self):
        hook = self.repo / '.git/hooks/pre-commit'
        hook.write_text('#!/bin/sh\nexit 0\n')
        self.blocked(self.cli('init'), 'existing pre-commit')
        self.assertEqual(hook.read_text(), '#!/bin/sh\nexit 0\n')

    def test_custom_hooks_path_preserved(self):
        self.git('config', 'core.hooksPath', '.hooks')
        self.blocked(self.cli('init'), 'custom core.hooksPath')
        self.assertFalse((self.repo / '.hooks').exists())

    def test_symlink_hook_preserved(self):
        target = self.repo / 'target'
        target.write_text('keep')
        (self.repo / '.git/hooks/pre-commit').symlink_to(target)
        self.blocked(self.cli('init'), 'symlink')
        self.assertEqual(target.read_text(), 'keep')

    def test_not_a_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            self.blocked(self.cli('staged', cwd=directory), 'Git operation failed')

    def test_invalid_arguments(self):
        for args in [('staged', '--max-lines', '0'), ('staged', '--allow', '../src'),
                     ('run', '--timeout', 'nan'), ('run', '--timeout', '-1')]:
            with self.subTest(args=args):
                self.assertEqual(self.cli(*args).returncode, 2)

    def test_run_exit_status_and_literal_argv(self):
        result = self.cli('run', '--timeout', '2', '--', sys.executable, '-c',
                          'import sys; print(sys.argv[1]); sys.exit(7)', '$(touch injected)')
        self.assertEqual(result.returncode, 7)
        self.assertEqual(result.stdout.strip(), '$(touch injected)')
        self.assertFalse((self.repo / 'injected').exists())

    def test_timeout_kills_child_group(self):
        code = ('import subprocess,sys,time; '
                'subprocess.Popen([sys.executable,"-c",'
                '"import time,pathlib; time.sleep(1); pathlib.Path(\'escaped\').touch()"]); '
                'time.sleep(20)')
        self.blocked(self.cli('run', '--timeout', '0.3', '--', sys.executable, '-c', code),
                     'process group terminated')
        time.sleep(1.1)
        self.assertFalse((self.repo / 'escaped').exists())

    def test_missing_command(self):
        self.blocked(self.cli('run', '--timeout', '1'), 'requires a command')
        self.blocked(self.cli('run', '--timeout', '1', '--', '/no/such/jev0-command'), 'No such file')

    def test_gitignore(self):
        (self.repo / '.gitignore').write_bytes((ROOT / '.gitignore').read_bytes())
        for name in ('x.gguf', 'x.bin', 'models/x', '.cache/x', '.DS_Store', 'x.log'):
            self.assertEqual(self.git('check-ignore', '--', name).decode().strip(), name)

    def test_installer_idempotent_and_preserves_existing(self):
        target = self.repo / 'bin with spaces'
        env = dict(self.env, JEV0_BIN_DIR=str(target))
        def install():
            return subprocess.run(['sh', str(ROOT / 'install.sh')], env=env, capture_output=True)
        self.assertEqual(install().returncode, 0)
        self.assertEqual(install().returncode, 0)
        self.assertTrue(os.access(target / 'jev0', os.X_OK))
        (target / 'jev0').write_text('keep')
        self.assertEqual(install().returncode, 1)
        self.assertEqual((target / 'jev0').read_text(), 'keep')

    def test_installer_repairs_execute_bit_for_identical_binary(self):
        target = self.repo / 'bin'
        env = dict(self.env, JEV0_BIN_DIR=str(target))
        install = lambda: subprocess.run(
            ['sh', str(ROOT / 'install.sh')], env=env, capture_output=True
        )
        self.assertEqual(install().returncode, 0)
        binary = target / 'jev0'
        binary.chmod(0o644)
        self.assertEqual(install().returncode, 0)
        self.assertTrue(os.access(binary, os.X_OK))

    def test_installer_leaves_no_temporary_files(self):
        target = self.repo / 'bin'
        env = dict(self.env, JEV0_BIN_DIR=str(target))
        result = subprocess.run(['sh', str(ROOT / 'install.sh')], env=env, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(list(target.glob('.jev0-install.*')), [])

    def test_installer_preserves_symlink_destination(self):
        target = self.repo / 'bin'
        target.mkdir()
        existing = self.repo / 'existing'
        existing.write_text('keep')
        (target / 'jev0').symlink_to(existing)
        env = dict(self.env, JEV0_BIN_DIR=str(target))
        result = subprocess.run(['sh', str(ROOT / 'install.sh')], env=env, capture_output=True)
        self.assertEqual(result.returncode, 1)
        self.assertTrue((target / 'jev0').is_symlink())
        self.assertEqual(existing.read_text(), 'keep')


class DoctorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        self.env = dict(os.environ, GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull)
        subprocess.run(['git', 'init', '-q'], cwd=self.repo, env=self.env, check=True)
        subprocess.run(['git', 'config', 'user.email', 'test@example.invalid'],
                       cwd=self.repo, env=self.env, check=True)
        subprocess.run(['git', 'config', 'user.name', 'Test'],
                       cwd=self.repo, env=self.env, check=True)

    def cli(self, *args, cwd=None):
        return subprocess.run(
            [sys.executable, str(CLI), *args],
            cwd=cwd or self.repo,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_doctor_json_reports_runtime_and_repo_state(self):
        result = self.cli('doctor', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data['schema_version'], 1)
        self.assertEqual(data['version'].split('-')[0], '0.3.0')
        self.assertTrue(data['repository'])
        self.assertEqual(data['hook_status'], 'missing')
        self.assertFalse(data['hook_enforced'])
        self.assertTrue(data['runtime_ready'])
        self.assertEqual(len(data['executable_sha256']), 64)
        self.assertEqual(data['executable'], str(CLI.resolve()))

    def test_doctor_reports_managed_hook(self):
        self.assertEqual(self.cli('init').returncode, 0)
        result = self.cli('doctor', '--json')
        data = json.loads(result.stdout)
        self.assertEqual(data['hook_status'], 'managed')
        self.assertTrue(data['hook_enforced'])
        self.assertTrue(data['hook_target_exists'])
        self.assertTrue(data['hook_matches_executable'])
        self.assertEqual(data['hook_target'], str(CLI.resolve()))
        self.assertEqual(data['hook_python'], sys.executable)

    def test_doctor_detects_stale_managed_hook_target(self):
        self.assertEqual(self.cli('init').returncode, 0)
        hook = self.repo / '.git/hooks/pre-commit'
        lines = hook.read_text().splitlines()
        command = shlex.split(next(line for line in lines if line.startswith('exec ')))
        command[2] = str(self.repo / 'missing-jev0')
        hook.write_text('\n'.join(lines[:2] + ['exec ' + shlex.join(command[1:])]) + '\n')
        hook.chmod(0o755)
        result = self.cli('doctor', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data['hook_status'], 'managed-stale')
        self.assertFalse(data['hook_enforced'])
        self.assertFalse(data['hook_target_exists'])

    def test_doctor_rejects_oversized_hook_without_reading_it_all(self):
        hook = self.repo / '.git/hooks/pre-commit'
        hook.write_bytes(
            b'#!/bin/sh\n# jev0 managed pre-commit hook\n' +
            b'x' * 20000
        )
        hook.chmod(0o755)
        result = self.cli('doctor', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data['hook_status'], 'oversized')
        self.assertFalse(data['hook_enforced'])

    def test_doctor_survives_oversized_hook_target(self):
        self.assertEqual(self.cli('init').returncode, 0)
        hook = self.repo / '.git/hooks/pre-commit'
        target = self.repo / 'huge-jev0'
        with target.open('wb') as stream:
            stream.seek(10_000_000)
            stream.write(b'x')
        lines = hook.read_text().splitlines()
        command = shlex.split(next(line for line in lines if line.startswith('exec ')))
        command[2] = str(target)
        hook.write_text('\n'.join(lines[:2] + ['exec ' + shlex.join(command[1:])]) + '\n')
        hook.chmod(0o755)
        result = self.cli('doctor', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertTrue(data['hook_target_exists'])
        self.assertFalse(data['hook_matches_executable'])
        self.assertTrue(data['hook_enforced'])


    def test_doctor_reports_custom_hooks_path_without_mutation(self):
        subprocess.run(['git', 'config', 'core.hooksPath', '.hooks'],
                       cwd=self.repo, env=self.env, check=True)
        before = subprocess.run(['git', 'status', '--porcelain=v1', '-z'],
                                cwd=self.repo, env=self.env, check=True,
                                capture_output=True).stdout
        result = self.cli('doctor', '--json')
        after = subprocess.run(['git', 'status', '--porcelain=v1', '-z'],
                               cwd=self.repo, env=self.env, check=True,
                               capture_output=True).stdout
        data = json.loads(result.stdout)
        self.assertEqual(data['hook_status'], 'custom-hooks-path')
        self.assertFalse(data['hook_enforced'])
        self.assertEqual(before, after)

    def test_doctor_works_outside_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.cli('doctor', '--json', cwd=directory)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertFalse(data['repository'])
        self.assertEqual(data['hook_status'], 'not-a-repository')
        self.assertFalse(data['hook_enforced'])

    def test_doctor_human_output_is_stable_key_value_lines(self):
        result = self.cli('doctor')
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertTrue(any(line.startswith('version: ') for line in lines))
        self.assertTrue(any(line.startswith('runtime_ready: ') for line in lines))
        self.assertTrue(any(line.startswith('hook_status: ') for line in lines))



class EvaluatorTests(unittest.TestCase):
    """Exercise the optional Python API against real staged content."""

    setUp = GuardTests.setUp
    git = GuardTests.git
    stage = GuardTests.stage

    def evaluate(self, evaluator):
        import argparse
        import jev0
        from unittest.mock import patch
        previous = os.getcwd()
        self.addCleanup(os.chdir, previous)
        os.chdir(self.repo)
        with patch.dict(os.environ, self.env):
            jev0.staged(argparse.Namespace(max_files=20, max_lines=500, allow=[]), evaluator)

    def test_layer_zero_blocks_before_evaluator(self):
        import jev0
        from unittest.mock import Mock
        self.stage('weights.gguf')
        evaluator = Mock()
        with self.assertRaisesRegex(jev0.Blocked, 'model artifact'):
            self.evaluate(evaluator)
        evaluator.evaluate.assert_not_called()

    def test_default_does_not_generate_patch(self):
        import jev0
        from unittest.mock import patch
        self.stage('src/a')
        with patch.object(jev0, 'git', wraps=jev0.git) as calls:
            self.evaluate(None)
        self.assertFalse(any('--patch' in call.args for call in calls.call_args_list))

    def test_evaluator_sees_only_staged_diff(self):
        from unittest.mock import Mock
        self.stage('src/a', b'staged value\n')
        (self.repo / 'src/a').write_text('unstaged value\n')
        evaluator = Mock()
        evaluator.evaluate.return_value = (True, '')
        self.evaluate(evaluator)
        evaluator.evaluate.assert_called_once()
        diff = evaluator.evaluate.call_args.args[0]
        self.assertIn('+staged value', diff)
        self.assertNotIn('unstaged value', diff)

    def test_rejection_preserves_index(self):
        import jev0
        from unittest.mock import Mock
        self.stage('src/a')
        before = self.git('diff', '--cached')
        evaluator = Mock()
        evaluator.evaluate.return_value = (False, 'unrelated change')
        with self.assertRaisesRegex(jev0.Blocked, 'Layer 1: unrelated change'):
            self.evaluate(evaluator)
        self.assertEqual(before, self.git('diff', '--cached'))

    def test_evaluator_exception_blocks(self):
        import jev0
        from unittest.mock import Mock
        self.stage('src/a')
        evaluator = Mock()
        evaluator.evaluate.side_effect = RuntimeError('model unavailable')
        with self.assertRaisesRegex(jev0.Blocked, 'evaluator failed: model unavailable'):
            self.evaluate(evaluator)

    def test_invalid_results_block(self):
        import jev0
        from unittest.mock import Mock
        self.stage('src/a')
        evaluator = Mock()
        for result in (None, True, (1, ''), ('false', ''), (True, None), [True, ''], (True,)):
            with self.subTest(result=result):
                evaluator.evaluate.return_value = result
                with self.assertRaisesRegex(jev0.Blocked, 'must return'):
                    self.evaluate(evaluator)

    def test_empty_rejection_has_reason(self):
        import jev0
        from unittest.mock import Mock
        evaluator = Mock()
        evaluator.evaluate.return_value = (False, ' ')
        with self.assertRaisesRegex(jev0.Blocked, 'rejected without a reason'):
            self.evaluate(evaluator)


class ProcessEvaluatorTests(unittest.TestCase):
    setUp = GuardTests.setUp
    git = GuardTests.git
    cli = GuardTests.cli
    stage = GuardTests.stage
    blocked = GuardTests.blocked

    def command(self, code):
        return json.dumps([sys.executable, '-c', code])

    def test_cli_allows_and_receives_staged_diff(self):
        self.stage('src/a', b'expected staged value\n')
        code = ('import json,sys; data=sys.stdin.read(); '
                'print(json.dumps({"passed":"+expected staged value" in data,"reason":"missing diff"}))')
        result = self.cli('staged', '--evaluator-command', self.command(code))
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, '', ''))

    def test_cli_rejects_and_preserves_index(self):
        self.stage('src/a')
        before = self.git('diff', '--cached')
        code = 'print(\'{"passed":false,"reason":"semantic rejection"}\')'
        self.blocked(self.cli('staged', '--evaluator-command', self.command(code)),
                     'Layer 1: semantic rejection')
        self.assertEqual(before, self.git('diff', '--cached'))

    def test_layer_zero_runs_before_process(self):
        self.stage('weights.gguf')
        code = 'from pathlib import Path; Path("evaluator-ran").touch()'
        self.blocked(self.cli('staged', '--evaluator-command', self.command(code)), 'model artifact')
        self.assertFalse((self.repo / 'evaluator-ran').exists())

    def test_command_arguments_are_literal(self):
        self.stage('src/a')
        marker = '$(touch evaluator-injected)'
        code = ('import json,sys; print(json.dumps({"passed":sys.argv[1].startswith("$("),'
                '"reason":"argument changed"}))')
        command = json.dumps([sys.executable, '-c', code, marker])
        result = self.cli('staged', '--evaluator-command', command)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.repo / 'evaluator-injected').exists())

    def test_timeout_kills_evaluator_group(self):
        self.stage('src/a')
        code = ('import subprocess,sys,time; '
                'subprocess.Popen([sys.executable,"-c",'
                '"import time,pathlib; time.sleep(1); pathlib.Path(\'escaped-evaluator\').touch()"]); '
                'time.sleep(20)')
        self.blocked(self.cli('staged', '--evaluator-command', self.command(code),
                              '--evaluator-timeout', '0.3'), 'evaluator exceeded 0.3s')
        time.sleep(1.1)
        self.assertFalse((self.repo / 'escaped-evaluator').exists())

    def test_timeout_does_not_wait_for_escaped_pipe_holder(self):
        self.stage('src/a')
        code = (
            'import subprocess,sys,time; '
            'subprocess.Popen([sys.executable,"-c","import time; time.sleep(2)"], '
            'start_new_session=True); '
            'time.sleep(20)'
        )
        started = time.monotonic()
        result = self.cli('staged', '--evaluator-command', self.command(code),
                          '--evaluator-timeout', '0.2')
        elapsed = time.monotonic() - started
        self.blocked(result, 'evaluator exceeded 0.2s')
        self.assertLess(elapsed, 1.5, f'evaluator cleanup took {elapsed:.2f}s')

    def test_success_does_not_wait_for_detached_child_holding_output_fd(self):
        self.stage('src/a')
        code = (
            'import json,subprocess,sys,time; '
            'subprocess.Popen([sys.executable,"-c","import time; time.sleep(2)"], '
            'start_new_session=True); '
            'print(json.dumps({"passed":True,"reason":""}))'
        )
        started = time.monotonic()
        result = self.cli('staged', '--evaluator-command', self.command(code),
                          '--evaluator-timeout', '1')
        elapsed = time.monotonic() - started
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(elapsed, 1.5, f'evaluator completion took {elapsed:.2f}s')

    def test_large_stdout_is_rejected_without_returning_it(self):
        self.stage('src/a')
        code = 'import sys; sys.stdout.write("x"*1000000)'
        result = self.cli('staged', '--evaluator-command', self.command(code),
                          '--max-evaluator-output-bytes', '128')
        self.blocked(result, 'output exceeds 128 bytes')
        self.assertEqual(result.stdout, '')

    def test_large_stderr_is_truncated_on_nonzero_exit(self):
        self.stage('src/a')
        code = 'import sys; sys.stderr.write("e"*1000000); sys.exit(7)'
        result = self.cli('staged', '--evaluator-command', self.command(code),
                          '--max-evaluator-output-bytes', '128')
        self.blocked(result, 'evaluator exited 7')
        self.assertLess(len(result.stderr), 300)

    def test_unsupported_platform_blocks_before_evaluator_launch(self):
        import jev0
        from unittest.mock import patch

        evaluator = jev0.ProcessEvaluator(['never-runs'], 1, 100, 100)
        with patch.object(jev0.os, 'name', 'nt'), \
             patch.object(jev0.subprocess, 'Popen') as popen:
            with self.assertRaisesRegex(jev0.Blocked, 'require macOS or Linux'):
                evaluator.evaluate('diff')
        popen.assert_not_called()

    def test_keyboard_interrupt_cleans_evaluator(self):
        import jev0
        from unittest.mock import Mock, patch

        process = Mock()
        process.pid = 424242
        evaluator = jev0.ProcessEvaluator(['fake'], 1, 100, 100)
        with patch.object(jev0.subprocess, 'Popen', return_value=process), \
             patch.object(jev0, 'capture_process_output', side_effect=KeyboardInterrupt()), \
             patch.object(jev0, 'terminate_process_group') as cleanup:
            with self.assertRaisesRegex(jev0.Blocked, 'evaluator interrupted'):
                evaluator.evaluate('diff')
        cleanup.assert_called_once_with(process)

    def test_normal_evaluator_exit_does_not_kill_process_group(self):
        import jev0
        from unittest.mock import patch

        command = [sys.executable, '-c',
                   'print(\'{"passed":true,"reason":""}\')']
        evaluator = jev0.ProcessEvaluator(command, 2, 1000, 1000)
        with patch.object(jev0, 'terminate_process_group') as cleanup:
            self.assertEqual(evaluator.evaluate('diff'), (True, ''))
        cleanup.assert_not_called()

    def test_protocol_failures_block(self):
        self.stage('src/a')
        cases = [
            ('print("not json")', 'invalid JSON'),
            ('print(\'{"passed":1,"reason":"bad"}\')', 'must contain only'),
            ('import sys; sys.stderr.write("failed\\nsecond line"); sys.exit(7)', 'exited 7'),
            ('print("x"*20)', 'output exceeds 10 bytes'),
        ]
        for code, reason in cases:
            with self.subTest(reason=reason):
                args = ['staged', '--evaluator-command', self.command(code)]
                if reason.startswith('output'):
                    args.extend(['--max-evaluator-output-bytes', '10'])
                self.blocked(self.cli(*args), reason)

    def test_diff_limit_blocks_before_process(self):
        self.stage('src/a', b'large staged value\n')
        code = 'from pathlib import Path; Path("evaluator-ran").touch()'
        self.blocked(self.cli('staged', '--evaluator-command', self.command(code),
                              '--max-diff-bytes', '1'), 'diff exceeds 1 bytes')
        self.assertFalse((self.repo / 'evaluator-ran').exists())

    def test_process_evaluator_uses_bounded_git_diff_reader(self):
        import jev0
        from unittest.mock import Mock, patch

        self.stage('src/a', b'x' * 10000 + b'\n')
        evaluator = jev0.ProcessEvaluator(['never-runs'], 1, 64, 100)
        with patch.object(evaluator, 'evaluate') as evaluate, \
             patch.object(jev0, 'git_limited', wraps=jev0.git_limited) as limited:
            with self.assertRaisesRegex(jev0.Blocked, 'diff exceeds 64 bytes'):
                self.evaluate_process_evaluator_direct(evaluator)
        self.assertTrue(limited.called)
        evaluate.assert_not_called()

    def evaluate_process_evaluator_direct(self, evaluator):
        import argparse
        import jev0
        from unittest.mock import patch

        previous = os.getcwd()
        self.addCleanup(os.chdir, previous)
        os.chdir(self.repo)
        args = argparse.Namespace(max_files=20, max_lines=500, allow=[])
        with patch.dict(os.environ, self.env):
            jev0.staged(args, evaluator)

    def test_invalid_command_arguments(self):
        for value in ('not-json', '{}', '[]', '[""]', '[1]'):
            with self.subTest(value=value):
                self.assertEqual(self.cli('staged', '--evaluator-command', value).returncode, 2)

    def test_init_persists_evaluator_policy(self):
        code = 'print(\'{"passed":false,"reason":"hook rejection"}\')'
        command = self.command(code)
        result = self.cli('init', '--evaluator-command', command, '--evaluator-timeout', '2',
                          '--max-diff-bytes', '2000', '--max-evaluator-output-bytes', '200')
        self.assertEqual(result.returncode, 0, result.stderr)
        hook = (self.repo / '.git/hooks/pre-commit').read_text()
        self.assertIn('--evaluator-command', hook)
        self.stage('src/a')
        commit = subprocess.run(['git', 'commit', '-qm', 'blocked'], cwd=self.repo,
                                env=self.env, capture_output=True, text=True)
        self.assertNotEqual(commit.returncode, 0)
        self.assertIn('hook rejection', commit.stderr)


if __name__ == '__main__':
    unittest.main()
