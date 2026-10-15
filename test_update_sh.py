import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / 'update.sh'
GIT_ENV = {'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@example.com',
           'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@example.com'}
STUB = "import sys\nprint('ARGS', *sys.argv[1:])\n"


class UpdateScriptTests(unittest.TestCase):
    """update.sh only locates a checkout and hands over to update.py; a stub update.py records what it received."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / 'home'
        self.share = self.home / '.local/share/tmux-attn'
        self.share.mkdir(parents=True)
        (self.share / 'attention.py').write_text('x\n')
        self.origin = self.root / 'origin'
        self.origin.mkdir()
        self.git(self.origin, 'init', '-q', '-b', 'main')
        self.first = self.commit('first')
        self.second = self.commit('second')

    def git(self, cwd, *args):
        return subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True, text=True,
                              env={**os.environ, **GIT_ENV}).stdout.strip()

    def commit(self, message):
        (self.origin / 'update.py').write_text(STUB + f'# {message}\n')
        self.git(self.origin, 'add', '-A')
        self.git(self.origin, 'commit', '-q', '-m', message)
        return self.git(self.origin, 'rev-parse', 'HEAD')

    def run_script(self, *args, **env):
        environment = {k: v for k, v in os.environ.items() if k not in ('TMUX_ATTN_SRC', 'TMUX_ATTN_REF')}
        environment.update({'HOME': str(self.home), 'TMUX_ATTN_REPO': str(self.origin), **env})
        return subprocess.run(['sh', str(SCRIPT), *args], capture_output=True, text=True, env=environment)

    def test_refuses_when_tmux_attn_is_not_installed(self):
        (self.share / 'attention.py').unlink()
        result = self.run_script()
        self.assertEqual(result.returncode, 1)
        self.assertIn('install', result.stderr)
        self.assertFalse((self.home / '.local/share/tmux-attn-src').exists(), 'nothing is cloned for nothing')

    def test_uses_the_recorded_checkout_and_passes_arguments_through(self):
        checkout = self.root / 'recorded'
        self.git(self.root, 'clone', '-q', str(self.origin), str(checkout))
        (self.share / 'INSTALL.json').write_text('{"source": "%s"}' % checkout)
        result = self.run_script('--check')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), f'ARGS --src {checkout} --check')

    def test_clones_when_no_checkout_is_recorded(self):
        result = self.run_script()
        src = self.home / '.local/share/tmux-attn-src'
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f'ARGS --src {src}', result.stdout)
        self.assertEqual(self.git(src, 'rev-parse', 'HEAD'), self.second)

    def test_a_pinned_ref_checks_out_exactly_that_commit_and_is_passed_on(self):
        result = self.run_script(TMUX_ATTN_REF=self.first)
        src = self.home / '.local/share/tmux-attn-src'
        self.assertEqual(self.git(src, 'rev-parse', 'HEAD'), self.first, 'a pin older than main is honoured')
        self.assertIn(f'--ref {self.first}', result.stdout)

    def test_env_source_wins_over_the_record_and_must_be_a_checkout(self):
        checkout = self.root / 'envsrc'
        self.git(self.root, 'clone', '-q', str(self.origin), str(checkout))
        (self.share / 'INSTALL.json').write_text('{"source": "/nowhere"}')
        self.assertIn(f'--src {checkout}', self.run_script(TMUX_ATTN_SRC=str(checkout)).stdout)
        bad = self.run_script(TMUX_ATTN_SRC=str(self.root))
        self.assertEqual(bad.returncode, 1)
        self.assertIn('not a tmux-attn checkout', bad.stderr)

    def test_a_recorded_checkout_that_vanished_is_reported_instead_of_re_cloned(self):
        (self.share / 'INSTALL.json').write_text('{"source": "/nowhere"}')
        result = self.run_script()
        self.assertEqual(result.returncode, 1)
        self.assertIn('/nowhere', result.stderr)


    def test_option_like_values_and_relative_checkouts_are_refused(self):
        for env in ({'TMUX_ATTN_REF': '--upload-pack=x'}, {'TMUX_ATTN_REPO': '--upload-pack=x'}):
            result = self.run_script(**env)
            self.assertEqual(result.returncode, 1)
            self.assertIn('option-like', result.stderr)
        relative = self.run_script(TMUX_ATTN_SRC='relative/dir')
        self.assertEqual(relative.returncode, 1)
        self.assertIn('absolute', relative.stderr)


    def test_an_old_checkout_without_update_py_borrows_the_one_from_the_target_commit(self):
        legacy = self.root / 'legacy_origin'
        legacy.mkdir()
        self.git(legacy, 'init', '-q', '-b', 'main')
        (legacy / 'attention.py').write_text('x\n')
        self.git(legacy, 'add', '-A')
        self.git(legacy, 'commit', '-q', '-m', 'before update.py')
        checkout = self.root / 'legacy_checkout'
        self.git(self.root, 'clone', '-q', str(legacy), str(checkout))
        (legacy / 'update.py').write_text(STUB + '# new\n')
        self.git(legacy, 'add', '-A')
        self.git(legacy, 'commit', '-q', '-m', 'add update.py')
        (self.share / 'INSTALL.json').write_text('{"source": "%s"}' % checkout)
        result = self.run_script('--check')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), f'ARGS --src {checkout} --check')
        self.assertFalse((checkout / 'update.py').exists(), 'the working tree is left alone until update.py has checked it')


if __name__ == '__main__':
    unittest.main()
