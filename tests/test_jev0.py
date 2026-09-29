import hashlib
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


class PolicyTests(unittest.TestCase):
    setUp = GuardTests.setUp
    git = GuardTests.git
    cli = GuardTests.cli
    stage = GuardTests.stage
    blocked = GuardTests.blocked

    def write_policy(self, document, name='.jev0.json'):
        path = self.repo / name
        path.write_text(json.dumps(document))
        return path

    def test_policy_applies_line_budget(self):
        self.write_policy({'schema_version': 1, 'max_lines': 1})
        self.stage('a', b'a\nb\n')
        self.blocked(self.cli('staged', '--policy', '.jev0.json'), '2 added/deleted')

    def test_policy_inspector_reports_effective_values_and_fingerprint(self):
        policy = self.write_policy({
            'schema_version': 1,
            'max_files': 7,
            'allow': ['src', 'tests'],
        })
        result = self.cli('policy', '.jev0.json', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data['schema_version'], 1)
        self.assertEqual(data['max_files'], 7)
        self.assertEqual(data['max_lines'], 500)
        self.assertEqual(data['allow'], ['src', 'tests'])
        self.assertEqual(data['policy_path'], str(policy.resolve()))
        self.assertEqual(
            data['policy_sha256'],
            hashlib.sha256(policy.read_bytes()).hexdigest(),
        )

    def test_policy_is_never_auto_discovered(self):
        self.write_policy({'schema_version': 1, 'max_lines': 1})
        self.stage('a', b'a\nb\n')
        result = self.cli('staged')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_policy_path_is_repository_relative_from_subdirectory(self):
        self.write_policy({'schema_version': 1, 'max_lines': 1})
        self.stage('src/a', b'a\nb\n')
        result = self.cli(
            'staged', '--policy', '.jev0.json',
            cwd=self.repo / 'src'
        )
        self.blocked(result, '2 added/deleted')

    def test_policy_applies_scope_and_file_budget(self):
        self.write_policy({
            'schema_version': 1,
            'max_files': 1,
            'allow': ['src'],
        })
        self.stage('src/a')
        self.stage('src/b')
        self.blocked(self.cli('staged', '--policy', '.jev0.json'), '2 staged files')
        self.git('reset', '-q')
        self.stage('outside/a')
        self.blocked(self.cli('staged', '--policy', '.jev0.json'), 'outside allowed scope')

    def test_policy_rejects_unknown_or_executable_keys(self):
        cases = [
            ({'schema_version': 1, 'unknown': 1}, 'unknown policy keys'),
            ({'schema_version': 1, 'evaluator_command': ['sh']}, 'unknown policy keys'),
            ({'schema_version': 2}, 'unsupported policy schema_version'),
            ({'schema_version': True}, 'schema_version must be an integer'),
            ({'schema_version': 1, 'max_files': True}, 'max_files must be a positive integer'),
            ({'schema_version': 1, 'allow': 'src'}, 'allow must be an array'),
            ({'schema_version': 1, 'allow': ['../src']}, 'invalid policy scope'),
        ]
        for document, reason in cases:
            with self.subTest(document=document):
                self.write_policy(document)
                self.blocked(self.cli('staged', '--policy', '.jev0.json'), reason)

    def test_policy_cannot_escape_repository_through_symlink(self):
        with tempfile.TemporaryDirectory() as outside:
            target = Path(outside) / 'policy.json'
            target.write_text(json.dumps({'schema_version': 1}))
            (self.repo / '.jev0.json').symlink_to(target)
            self.blocked(
                self.cli('staged', '--policy', '.jev0.json'),
                'policy must be an existing file inside the repository',
            )

    def test_policy_size_is_bounded_before_json_parse(self):
        (self.repo / '.jev0.json').write_bytes(b' ' * 65537)
        self.blocked(
            self.cli('staged', '--policy', '.jev0.json'),
            'policy exceeds 65536 bytes',
        )

    def test_policy_rejects_duplicate_keys(self):
        (self.repo / '.jev0.json').write_text(
            '{"schema_version":1,"max_files":2,"max_files":3}'
        )
        self.blocked(
            self.cli('staged', '--policy', '.jev0.json'),
            'duplicate policy key: max_files',
        )

    def test_policy_rejects_invalid_utf8(self):
        (self.repo / '.jev0.json').write_bytes(b'{"schema_version":1}\xff')
        self.blocked(
            self.cli('staged', '--policy', '.jev0.json'),
            'policy must be valid UTF-8 JSON',
        )

    def test_empty_policy_path_is_invalid_cli(self):
        result = self.cli('staged', '--policy', '')
        self.assertEqual(result.returncode, 2)
        self.assertIn('policy path must not be empty', result.stderr)

    def test_policy_and_layer_zero_flags_cannot_be_mixed(self):
        self.write_policy({'schema_version': 1})
        cases = [
            ('--max-files', '1'),
            ('--max-lines', '1'),
            ('--allow', 'src'),
        ]
        for option in cases:
            with self.subTest(option=option):
                result = self.cli('staged', '--policy', '.jev0.json', *option)
                self.assertEqual(result.returncode, 2)
                self.assertIn('--policy cannot be combined', result.stderr)

    def test_init_snapshots_policy_into_hook(self):
        policy = self.write_policy({
            'schema_version': 1,
            'max_files': 1,
            'max_lines': 50,
            'allow': ['src'],
        })
        fingerprint = hashlib.sha256(policy.read_bytes()).hexdigest()
        result = self.cli('init', '--policy', '.jev0.json')
        self.assertEqual(result.returncode, 0, result.stderr)
        hook = (self.repo / '.git/hooks/pre-commit').read_text()
        self.assertIn('--max-files 1', hook)
        self.assertIn('--max-lines 50', hook)
        self.assertIn('--allow src', hook)
        self.assertNotIn('--policy', hook)
        self.assertIn(f'# jev0 policy sha256: {fingerprint}', hook)

        doctor = self.cli('doctor', '--json')
        self.assertEqual(doctor.returncode, 0, doctor.stderr)
        self.assertEqual(
            json.loads(doctor.stdout)['hook_policy_sha256'],
            fingerprint,
        )

        policy.write_text(json.dumps({
            'schema_version': 1,
            'max_files': 100,
            'max_lines': 5000,
        }))
        doctor_after = self.cli('doctor', '--json')
        self.assertEqual(
            json.loads(doctor_after.stdout)['hook_policy_sha256'],
            fingerprint,
        )
        self.stage('src/a')
        self.stage('src/b')
        commit = subprocess.run(
            ['git', 'commit', '-qm', 'blocked'],
            cwd=self.repo,
            env=self.env,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(commit.returncode, 0)
        self.assertIn('2 staged files exceed budget 1', commit.stderr)


class PolicyDriftTests(unittest.TestCase):
    setUp = GuardTests.setUp
    git = GuardTests.git
    cli = GuardTests.cli
    stage = GuardTests.stage

    def write_policy(self, document):
        path = self.repo / '.jev0.json'
        path.write_text(json.dumps(document))
        return path

    def init_policy(self, document=None):
        policy = self.write_policy(document or {
            'schema_version': 1,
            'max_files': 2,
            'max_lines': 50,
            'allow': ['src'],
        })
        result = self.cli('init', '--policy', '.jev0.json')
        self.assertEqual(result.returncode, 0, result.stderr)
        return policy

    def test_policy_check_passes_when_snapshot_matches(self):
        self.init_policy()
        result = self.cli('policy-check', '.jev0.json', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertTrue(data['in_sync'])
        self.assertEqual(data['reasons'], [])
        self.assertEqual(data['expected'], data['actual'])

    def test_policy_check_detects_manifest_drift(self):
        policy = self.init_policy()
        policy.write_text(json.dumps({
            'schema_version': 1,
            'max_files': 9,
            'max_lines': 900,
            'allow': ['src'],
        }))
        result = self.cli('policy-check', '.jev0.json', '--json')
        self.assertEqual(result.returncode, 1)
        data = json.loads(result.stdout)
        self.assertFalse(data['in_sync'])
        self.assertIn(
            'policy fingerprint differs from managed hook snapshot',
            data['reasons'],
        )
        self.assertIn(
            'managed hook Layer 0 settings differ from policy',
            data['reasons'],
        )

    def test_policy_check_detects_hook_argument_tampering(self):
        self.init_policy()
        hook = self.repo / '.git/hooks/pre-commit'
        text = hook.read_text().replace('--max-files 2', '--max-files 99')
        hook.write_text(text)
        hook.chmod(0o755)
        result = self.cli('policy-check', '.jev0.json', '--json')
        self.assertEqual(result.returncode, 1)
        data = json.loads(result.stdout)
        self.assertFalse(data['in_sync'])
        self.assertIn(
            'managed hook Layer 0 settings differ from policy',
            data['reasons'],
        )

    def test_policy_check_detects_missing_fingerprint(self):
        self.init_policy()
        hook = self.repo / '.git/hooks/pre-commit'
        lines = [
            line for line in hook.read_text().splitlines()
            if not line.startswith('# jev0 policy sha256: ')
        ]
        hook.write_text('\n'.join(lines) + '\n')
        hook.chmod(0o755)
        result = self.cli('policy-check', '.jev0.json', '--json')
        self.assertEqual(result.returncode, 1)
        data = json.loads(result.stdout)
        self.assertIn(
            'managed hook has no policy fingerprint',
            data['reasons'],
        )

    def test_policy_check_detects_missing_hook(self):
        self.write_policy({'schema_version': 1})
        result = self.cli('policy-check', '.jev0.json', '--json')
        self.assertEqual(result.returncode, 1)
        data = json.loads(result.stdout)
        self.assertFalse(data['in_sync'])
        self.assertIn('managed hook is not enforced', data['reasons'])

class RangeGuardTests(unittest.TestCase):
    setUp = GuardTests.setUp
    git = GuardTests.git
    cli = GuardTests.cli
    blocked = GuardTests.blocked

    def commit_file(self, name, data, message):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        self.git('add', '-f', '--', name)
        self.git('commit', '-qm', message)
        return self.git('rev-parse', 'HEAD').decode().strip()

    def seed_base(self):
        return self.commit_file('base.txt', b'base\n', 'base')

    def test_range_allows_small_change(self):
        base = self.seed_base()
        head = self.commit_file('src/a.py', b'print(1)\n', 'head')
        result = self.cli('range', base, head, '--max-files', '2', '--max-lines', '10')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_range_enforces_line_and_file_budgets(self):
        base = self.seed_base()
        self.commit_file('src/a.py', b'a\nb\n', 'a')
        head = self.commit_file('src/b.py', b'x\n', 'b')
        self.blocked(
            self.cli('range', base, head, '--max-lines', '2'),
            '3 added/deleted lines exceed budget 2',
        )
        self.blocked(
            self.cli('range', base, head, '--max-files', '1'),
            '2 range files exceed budget 1',
        )

    def test_range_enforces_scope(self):
        base = self.seed_base()
        head = self.commit_file('outside/a.py', b'x\n', 'outside')
        self.blocked(
            self.cli('range', base, head, '--allow', 'src'),
            'outside allowed scope',
        )

    def test_range_rejects_model_and_binary_artifacts(self):
        base = self.seed_base()
        head = self.commit_file('weights.gguf', b'model\n', 'model')
        self.blocked(self.cli('range', base, head), 'model artifact')

        self.git('reset', '--hard', base)
        head = self.commit_file('asset.dat', b'\x00\x01\x02', 'binary')
        self.blocked(self.cli('range', base, head), 'binary change requires separate review')

    def test_range_uses_merge_base_semantics(self):
        base = self.seed_base()
        self.git('checkout', '-qb', 'feature')
        self.commit_file('src/feature.py', b'f\n', 'feature')
        head = self.git('rev-parse', 'HEAD').decode().strip()

        self.git('checkout', '-q', '-')
        self.commit_file('unrelated.txt', b'u\n', 'base advance')
        advanced_base = self.git('rev-parse', 'HEAD').decode().strip()

        result = self.cli(
            'range', advanced_base, head,
            '--allow', 'src',
            '--max-files', '1',
            '--max-lines', '1',
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_range_resolves_refs_before_diff(self):
        self.seed_base()
        self.git('branch', 'base-ref')
        self.commit_file('src/a.py', b'x\n', 'head')
        self.git('branch', 'head-ref')
        result = self.cli('range', 'base-ref', 'head-ref', '--max-lines', '1')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_range_rejects_unresolvable_or_option_like_refs(self):
        self.seed_base()
        option = self.cli('range', '--', '--no-index', 'HEAD')
        self.assertEqual(option.returncode, 2)
        self.assertIn(
            'commit ref must be a non-empty single-line non-option string',
            option.stderr,
        )
        self.blocked(
            self.cli('range', 'definitely-missing-ref', 'HEAD'),
            'cannot resolve commit ref',
        )

    def test_range_applies_explicit_policy(self):
        base = self.seed_base()
        head = self.commit_file('src/a.py', b'a\nb\n', 'head')
        (self.repo / '.jev0.json').write_text(json.dumps({
            'schema_version': 1,
            'max_lines': 1,
            'allow': ['src'],
        }))
        self.blocked(
            self.cli('range', base, head, '--policy', '.jev0.json'),
            '2 added/deleted lines exceed budget 1',
        )

    def test_range_supports_layer1_process_evaluator(self):
        base = self.seed_base()
        head = self.commit_file('src/a.py', b'x\n', 'head')
        code = (
            'import json,sys; '
            'data=sys.stdin.read(); '
            'print(json.dumps({"passed": "src/a.py" in data, "reason":"missing"}))'
        )
        command = json.dumps([sys.executable, '-c', code])
        result = self.cli(
            'range', base, head,
            '--evaluator-command', command,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


    def test_range_base_policy_cannot_be_weakened_by_head(self):
        (self.repo / '.jev0.json').write_text(json.dumps({
            'schema_version': 1,
            'max_files': 10,
            'max_lines': 1,
            'allow': ['src', '.jev0.json'],
        }))
        self.git('add', '.jev0.json')
        self.git('commit', '-qm', 'trusted policy')
        base = self.git('rev-parse', 'HEAD').decode().strip()

        (self.repo / '.jev0.json').write_text(json.dumps({
            'schema_version': 1,
            'max_files': 100,
            'max_lines': 10000,
        }))
        (self.repo / 'src').mkdir(exist_ok=True)
        (self.repo / 'src/a.py').write_text('a\nb\n')
        self.git('add', '.jev0.json', 'src/a.py')
        self.git('commit', '-qm', 'weaken policy and add change')
        head = self.git('rev-parse', 'HEAD').decode().strip()

        self.blocked(
            self.cli('range', base, head, '--base-policy', '.jev0.json'),
            'added/deleted lines exceed budget 1',
        )

    def test_range_base_policy_missing_fails_closed(self):
        base = self.seed_base()
        head = self.commit_file('src/a.py', b'x\n', 'head')
        self.blocked(
            self.cli('range', base, head, '--base-policy', '.jev0.json'),
            'cannot read base policy',
        )

    def test_range_base_policy_size_is_bounded(self):
        (self.repo / '.jev0.json').write_bytes(b' ' * 65537)
        self.git('add', '.jev0.json')
        self.git('commit', '-qm', 'oversized policy')
        base = self.git('rev-parse', 'HEAD').decode().strip()
        head = self.commit_file('src/a.py', b'x\n', 'head')
        self.blocked(
            self.cli('range', base, head, '--base-policy', '.jev0.json'),
            'policy exceeds 65536 bytes',
        )

    def test_range_base_policy_path_is_strict(self):
        base = self.seed_base()
        head = self.commit_file('src/a.py', b'x\n', 'head')
        result = self.cli('range', base, head, '--base-policy', 'bad:path')
        self.assertEqual(result.returncode, 2)
        self.assertIn(
            "base policy path must be a repository-relative file path without ':'",
            result.stderr,
        )

    def test_range_base_policy_cannot_mix_with_other_layer0_options(self):
        (self.repo / '.jev0.json').write_text(json.dumps({'schema_version': 1}))
        self.git('add', '.jev0.json')
        self.git('commit', '-qm', 'policy')
        base = self.git('rev-parse', 'HEAD').decode().strip()
        head = self.commit_file('src/a.py', b'x\n', 'head')
        cases = [
            ('--policy', '.jev0.json'),
            ('--max-files', '1'),
            ('--max-lines', '1'),
            ('--allow', 'src'),
        ]
        for option in cases:
            with self.subTest(option=option):
                result = self.cli(
                    'range', base, head,
                    '--base-policy', '.jev0.json',
                    *option,
                )
                self.assertEqual(result.returncode, 2)
                self.assertIn('--base-policy cannot be combined', result.stderr)


class RangeEvidenceTests(unittest.TestCase):
    setUp = GuardTests.setUp
    git = GuardTests.git
    cli = GuardTests.cli

    def commit_file(self, name, data, message):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        self.git('add', '-f', '--', name)
        self.git('commit', '-qm', message)
        return self.git('rev-parse', 'HEAD').decode().strip()

    def test_range_report_allow_contains_deterministic_evidence(self):
        base = self.commit_file('base.txt', b'base\n', 'base')
        head = self.commit_file('src/a.py', b'a\nb\n', 'head')
        merge_base = self.git('merge-base', base, head).decode().strip()

        result = self.cli(
            'range-report', base, head,
            '--max-files', '2',
            '--max-lines', '10',
            '--allow', 'src',
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data['schema_version'], 1)
        self.assertEqual(data['decision'], 'allow')
        self.assertIsNone(data['reason'])
        self.assertEqual(data['base_sha'], base)
        self.assertEqual(data['head_sha'], head)
        self.assertEqual(data['merge_base_sha'], merge_base)
        self.assertEqual(data['policy_source'], 'flags')
        self.assertIsNone(data['policy_sha256'])
        self.assertEqual(data['files_changed'], 1)
        self.assertEqual(data['lines_changed'], 2)
        self.assertEqual(data['paths'], ['src/a.py'])
        self.assertEqual(
            data['policy'],
            {'max_files': 2, 'max_lines': 10, 'allow': ['src']},
        )

    def test_range_report_block_preserves_evidence(self):
        base = self.commit_file('base.txt', b'base\n', 'base')
        head = self.commit_file('src/a.py', b'a\nb\n', 'head')
        result = self.cli(
            'range-report', base, head,
            '--max-lines', '1',
        )
        self.assertEqual(result.returncode, 1)
        data = json.loads(result.stdout)
        self.assertEqual(data['decision'], 'block')
        self.assertEqual(data['files_changed'], 1)
        self.assertEqual(data['lines_changed'], 2)
        self.assertIn('exceed budget 1', data['reason'])

    def test_range_report_records_worktree_policy_provenance(self):
        base = self.commit_file('base.txt', b'base\n', 'base')
        head = self.commit_file('src/a.py', b'x\n', 'head')
        policy = self.repo / '.jev0.json'
        policy.write_text(json.dumps({
            'schema_version': 1,
            'max_files': 5,
            'max_lines': 5,
            'allow': ['src'],
        }))
        result = self.cli(
            'range-report', base, head,
            '--policy', '.jev0.json',
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data['policy_source'], 'worktree')
        self.assertEqual(data['policy_path'], str(policy.resolve()))
        self.assertEqual(
            data['policy_sha256'],
            hashlib.sha256(policy.read_bytes()).hexdigest(),
        )

    def test_range_report_records_trusted_base_policy_provenance(self):
        policy = self.repo / '.jev0.json'
        policy.write_text(json.dumps({
            'schema_version': 1,
            'max_files': 5,
            'max_lines': 5,
        }))
        self.git('add', '.jev0.json')
        self.git('commit', '-qm', 'policy')
        base = self.git('rev-parse', 'HEAD').decode().strip()
        raw = self.git('show', f'{base}:.jev0.json')
        head = self.commit_file('src/a.py', b'x\n', 'head')

        result = self.cli(
            'range-report', base, head,
            '--base-policy', '.jev0.json',
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data['policy_source'], 'base')
        self.assertEqual(data['policy_path'], '.jev0.json')
        self.assertEqual(
            data['policy_sha256'],
            hashlib.sha256(raw).hexdigest(),
        )

    def test_range_report_binds_verifier_identity_and_evidence_digest(self):
        base = self.commit_file('base.txt', b'base\n', 'base')
        head = self.commit_file('src/a.py', b'x\n', 'head')
        result = self.cli('range-report', base, head)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)

        self.assertEqual(data['verifier_version'].split('-')[0], '0.3.0')
        self.assertEqual(
            data['verifier_sha256'],
            hashlib.sha256(CLI.read_bytes()).hexdigest(),
        )

        digest = data.pop('evidence_sha256')
        canonical = json.dumps(
            data, sort_keys=True, separators=(',', ':'), ensure_ascii=False
        ).encode('utf-8')
        self.assertEqual(digest, hashlib.sha256(canonical).hexdigest())

    def test_range_report_and_range_share_decision(self):
        base = self.commit_file('base.txt', b'base\n', 'base')
        head = self.commit_file('src/a.py', b'a\nb\n', 'head')
        guarded = self.cli('range', base, head, '--max-lines', '1')
        reported = self.cli('range-report', base, head, '--max-lines', '1')
        self.assertEqual(guarded.returncode, reported.returncode)
        self.assertIn('exceed budget 1', guarded.stderr)
        self.assertIn('exceed budget 1', json.loads(reported.stdout)['reason'])


    def test_range_report_truncates_evidence_paths_without_losing_counts(self):
        import jev0
        from unittest.mock import patch

        base = self.commit_file('base.txt', b'base\n', 'base')
        self.commit_file('src/a.py', b'a\n', 'a')
        self.commit_file('src/b.py', b'b\n', 'b')
        head = self.commit_file('src/c.py', b'c\n', 'c')

        previous = os.getcwd()
        self.addCleanup(os.chdir, previous)
        os.chdir(self.repo)
        args = type('Args', (), {
            'base': base,
            'head': head,
            'base_policy': None,
            'policy': None,
            'max_files': 10,
            'max_lines': 10,
            'allow': None,
        })()
        with patch.object(jev0, 'EVIDENCE_MAX_PATHS', 2):
            from io import StringIO
            with patch('sys.stdout', new_callable=StringIO) as stdout:
                rc = jev0.range_report(args)
                data = json.loads(stdout.getvalue())

        self.assertEqual(rc, 0)
        self.assertEqual(data['files_changed'], 3)
        self.assertEqual(data['paths_total'], 3)
        self.assertTrue(data['paths_truncated'])
        self.assertEqual(len(data['paths']), 2)

    def test_range_metadata_read_is_bounded_before_full_buffering(self):
        import jev0
        from unittest.mock import patch

        base = self.commit_file('base.txt', b'base\n', 'base')
        head = self.commit_file('very-long-name-for-limit.py', b'x\n', 'head')

        previous = os.getcwd()
        self.addCleanup(os.chdir, previous)
        os.chdir(self.repo)
        with patch.object(jev0, 'CHANGE_METADATA_MAX_BYTES', 8):
            with self.assertRaisesRegex(
                jev0.Blocked,
                'change metadata exceeds 8 bytes',
            ):
                jev0.inspect_change_set(jev0.range_diff_prefix(base, head))


class EvidenceVerifyTests(unittest.TestCase):
    setUp = GuardTests.setUp
    git = GuardTests.git
    cli = GuardTests.cli

    def commit_file(self, name, data, message):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        self.git('add', '-f', '--', name)
        self.git('commit', '-qm', message)
        return self.git('rev-parse', 'HEAD').decode().strip()

    def make_evidence(self):
        base = self.commit_file('base.txt', b'base\n', 'base')
        head = self.commit_file('src/a.py', b'x\n', 'head')
        report = self.cli('range-report', base, head)
        self.assertEqual(report.returncode, 0, report.stderr)
        path = self.repo / 'evidence.json'
        path.write_text(report.stdout)
        return path, json.loads(report.stdout)

    def test_evidence_verify_accepts_valid_report(self):
        path, evidence = self.make_evidence()
        result = self.cli('evidence-verify', str(path), '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertTrue(data['valid'])
        self.assertEqual(data['evidence_sha256'], evidence['evidence_sha256'])
        self.assertTrue(data['current_verifier_match'])

    def test_evidence_verify_require_current_verifier(self):
        path, _ = self.make_evidence()
        result = self.cli(
            'evidence-verify',
            str(path),
            '--require-current-verifier',
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('current_verifier_match: yes', result.stdout)

    def test_evidence_verify_rejects_tampering(self):
        path, evidence = self.make_evidence()
        evidence['decision'] = 'block'
        evidence['reason'] = 'tampered'
        path.write_text(json.dumps(evidence))
        result = self.cli('evidence-verify', str(path))
        self.assertEqual(result.returncode, 1)
        self.assertIn('evidence digest mismatch', result.stderr)

    def test_evidence_verify_rejects_unknown_keys(self):
        path, evidence = self.make_evidence()
        evidence['unexpected'] = True
        path.write_text(json.dumps(evidence))
        result = self.cli('evidence-verify', str(path))
        self.assertEqual(result.returncode, 1)
        self.assertIn('evidence keys mismatch', result.stderr)

    def test_evidence_verify_bounds_input_size(self):
        path = self.repo / 'evidence.json'
        with path.open('wb') as stream:
            stream.seek(1_048_576)
            stream.write(b'x')
        result = self.cli('evidence-verify', str(path))
        self.assertEqual(result.returncode, 1)
        self.assertIn('evidence exceeds 1048576 bytes', result.stderr)

    def resign_evidence(self, evidence):
        unsigned = dict(evidence)
        unsigned.pop('evidence_sha256', None)
        canonical = json.dumps(
            unsigned, sort_keys=True, separators=(',', ':'), ensure_ascii=False
        ).encode('utf-8')
        evidence['evidence_sha256'] = hashlib.sha256(canonical).hexdigest()
        return evidence

    def test_evidence_verify_rejects_semantically_impossible_allow_reason(self):
        path, evidence = self.make_evidence()
        evidence['reason'] = 'should not exist'
        path.write_text(json.dumps(self.resign_evidence(evidence)))
        result = self.cli('evidence-verify', str(path))
        self.assertEqual(result.returncode, 1)
        self.assertIn(
            'allow evidence must not contain a block reason',
            result.stderr,
        )

    def test_evidence_verify_rejects_inconsistent_change_counts(self):
        path, evidence = self.make_evidence()
        evidence['files_changed'] = evidence['paths_total'] + 1
        path.write_text(json.dumps(self.resign_evidence(evidence)))
        result = self.cli('evidence-verify', str(path))
        self.assertEqual(result.returncode, 1)
        self.assertIn(
            'files_changed must equal paths_total',
            result.stderr,
        )

    def test_evidence_verify_rejects_invalid_policy_provenance(self):
        path, evidence = self.make_evidence()
        evidence['policy_source'] = 'flags'
        evidence['policy_path'] = '.jev0.json'
        evidence['policy_sha256'] = '1' * 64
        path.write_text(json.dumps(self.resign_evidence(evidence)))
        result = self.cli('evidence-verify', str(path))
        self.assertEqual(result.returncode, 1)
        self.assertIn(
            'flag policy evidence must not contain policy file provenance',
            result.stderr,
        )

    def test_evidence_verify_detects_different_but_valid_verifier(self):
        path, evidence = self.make_evidence()
        evidence['verifier_sha256'] = '0' * 64
        self.resign_evidence(evidence)
        path.write_text(json.dumps(evidence))

        relaxed = self.cli('evidence-verify', str(path), '--json')
        self.assertEqual(relaxed.returncode, 0, relaxed.stderr)
        self.assertFalse(json.loads(relaxed.stdout)['current_verifier_match'])

        strict = self.cli(
            'evidence-verify',
            str(path),
            '--require-current-verifier',
        )
        self.assertEqual(strict.returncode, 1)
        self.assertIn(
            'evidence verifier does not match current jev0',
            strict.stderr,
        )


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
