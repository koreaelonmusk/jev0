import os
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


if __name__ == '__main__':
    unittest.main()
