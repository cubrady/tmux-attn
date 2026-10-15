import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import update

GIT_ENV = {**os.environ, 'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@example.com',
           'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@example.com'}

LAUNCHD_TEXT = '''launchd
gui/502/ai.tmux-attn.web = {
\targuments = {
\t\t/Users/dev/.local/bin/tmux-attn
\t\tweb
\t\t0.0.0.0
\t\t7788
\t}
}'''
SYSTEMD_TEXT = ('systemd\nActiveState=active\nExecStart={ path=/home/brad/.local/bin/tmux-attn ; '
                'argv[]=/home/brad/.local/bin/tmux-attn web 100.97.0.79 7788 ; ignore_errors=no ; status=0/0 }')
APP = '/home/brad/.local/share/tmux-attn/attention.py'


def sh(*args, cwd):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True, env=GIT_ENV)


class FakeServices:
    """Stands in for launchd/systemd, `ps` and the live web server."""

    def __init__(self, runtime, web=True, observers=(111,), healthy=True):
        self.runtime = Path(runtime)
        self.web = web
        self.healthy = healthy
        self.reported = None
        self.pids = list(observers)
        self.calls = []

    def has_web_service(self):
        return self.web

    def web_endpoint(self):
        return ('127.0.0.1', 7788) if self.web else None

    def restart_web(self):
        self.calls.append('restart_web')

    def fetch(self, host, port, path):
        if path == '/api/health':
            version = update.read_record(self.runtime).get('version', '')
            return json.dumps({'ok': self.healthy, 'version': version,
                               'started_version': self.reported or version}).encode()
        return (self.runtime / 'web/index.html').read_bytes()

    def observers(self):
        return list(self.pids)

    def restart_observers(self, pids):
        self.calls.append(('restart_observers', tuple(pids)))
        self.pids = [max(self.pids) + 1]

    def reinstall(self, src, sidebar):
        self.calls.append(('reinstall', sidebar))

    def uses_sidebar(self):
        return False


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.origin, self.author, self.client = root / 'origin.git', root / 'author', root / 'client'
        self.runtime, self.state = root / 'runtime', root / 'state'
        sh('git', 'init', '--bare', '-b', 'main', str(self.origin), cwd=root)
        sh('git', 'clone', '-q', str(self.origin), str(self.author), cwd=root)
        self.publish({'attention.py': 'v1 attention\n', 'codex_observer.py': 'v1 observer\n',
                      'install_web_service.sh': '#!/bin/sh\n', 'install.py': 'v1 install\n',
                      'web/index.html': '<p>v1</p>\n', 'web/icons/a.svg': '<svg a/>\n',
                      'web/icons/b.svg': '<svg b/>\n', 'update.py': 'v1 update\n'}, 'v1')
        sh('git', 'clone', '-q', str(self.origin), str(self.client), cwd=root)
        self.v1 = self.head(self.client)
        for rel, data in update.tree_files(self.client, self.v1).items():
            update.write_atomic(self.runtime / rel, data)
        update.record_install(self.runtime, self.client)
        self.services = FakeServices(self.runtime)

    def head(self, repo):
        return subprocess.run(['git', '-C', str(repo), 'rev-parse', 'HEAD'], capture_output=True, text=True,
                              check=True).stdout.strip()

    def publish(self, files, message, repo=None, push=True):
        repo = repo or self.author
        for rel, text in files.items():
            if text is None:
                (repo / rel).unlink()
            else:
                (repo / rel).parent.mkdir(parents=True, exist_ok=True)
                (repo / rel).write_text(text)
        sh('git', 'add', '-A', cwd=repo)
        sh('git', 'commit', '-q', '-m', message, cwd=repo)
        if push:
            sh('git', 'push', '-q', 'origin', 'main', cwd=repo)
        return self.head(repo)

    def run_update(self, *argv, services=None):
        options = update.build_parser().parse_args(['--src', str(self.client), *argv])
        lines = []
        code = update.update(options, services or self.services, self.runtime, self.state, lines.append)
        return code, '\n'.join(lines)

    def text(self, rel):
        return (self.runtime / rel).read_text()

    def backups(self):
        return sorted((self.state / 'backups').glob('update-*')) if (self.state / 'backups').exists() else []

    # --- the happy paths

    def test_up_to_date_does_nothing(self):
        code, out = self.run_update()
        self.assertEqual(code, 0)
        self.assertIn('already up to date', out)
        self.assertEqual(self.services.calls, [])
        self.assertEqual(self.backups(), [])

    def test_page_only_change_copies_the_file_and_restarts_nothing(self):
        new = self.publish({'web/index.html': '<p>v2</p>\n'}, 'page')
        code, out = self.run_update()
        self.assertEqual(code, 0)
        self.assertEqual(self.text('web/index.html'), '<p>v2</p>\n')
        self.assertEqual(self.services.calls, [])
        self.assertEqual(update.read_record(self.runtime)['commit'], new)
        self.assertEqual(update.read_record(self.runtime)['version'], new[:12])
        self.assertEqual(self.head(self.client), new, 'the checkout is fast-forwarded after the deploy')
        self.assertIn('serving the new page', out)
        self.assertEqual(len(self.backups()), 1)

    def test_attention_change_restarts_web_and_observers_after_the_files_are_written(self):
        new = self.publish({'attention.py': 'v2 attention\n'}, 'core')
        code, out = self.run_update()
        self.assertEqual(code, 0)
        self.assertEqual(self.text('attention.py'), 'v2 attention\n')
        self.assertEqual(self.services.calls, ['restart_web', ('restart_observers', (111,))])
        self.assertIn(f'healthy at 127.0.0.1:7788 (version {new[:12]})', out)

    def test_observer_file_alone_restarts_only_observers(self):
        self.publish({'codex_observer.py': 'v2 observer\n'}, 'observer')
        self.run_update()
        self.assertEqual(self.services.calls, [('restart_observers', (111,))])

    def test_no_running_observer_means_nothing_to_restart(self):
        self.services.pids = []
        self.publish({'attention.py': 'v2 attention\n'}, 'core')
        code, _ = self.run_update()
        self.assertEqual(code, 0)
        self.assertEqual(self.services.calls, ['restart_web'])

    def test_removed_icons_are_removed_and_shell_scripts_stay_executable(self):
        self.publish({'web/icons/b.svg': None, 'install_web_service.sh': '#!/bin/sh\necho v2\n'}, 'icons')
        self.run_update()
        self.assertFalse((self.runtime / 'web/icons/b.svg').exists())
        self.assertTrue((self.runtime / 'web/icons/a.svg').exists())
        self.assertTrue(os.access(self.runtime / 'install_web_service.sh', os.X_OK))

    def test_unrecorded_install_with_identical_files_only_gets_a_version_record(self):
        (self.runtime / update.RECORD).unlink()
        code, out = self.run_update()
        self.assertEqual(code, 0)
        self.assertEqual(self.services.calls, [])
        self.assertEqual(update.read_record(self.runtime)['commit'], self.v1)
        self.assertIn('unrecorded', out)

    def test_second_run_is_a_no_op(self):
        self.publish({'attention.py': 'v2 attention\n'}, 'core')
        self.run_update()
        self.services.calls.clear()
        _, out = self.run_update()
        self.assertIn('already up to date', out)
        self.assertEqual((self.services.calls, len(self.backups())), ([], 1))

    # --- dry run and guards

    def test_check_changes_nothing(self):
        self.publish({'attention.py': 'v2 attention\n'}, 'core')
        code, out = self.run_update('--check')
        self.assertEqual(code, 0)
        self.assertIn('update available', out)
        self.assertIn('write   attention.py', out)
        self.assertEqual(self.text('attention.py'), 'v1 attention\n')
        self.assertEqual((self.services.calls, self.backups(), self.head(self.client)), ([], [], self.v1))

    def test_uncommitted_changes_in_the_checkout_are_refused_and_nothing_moves(self):
        self.publish({'attention.py': 'v2 attention\n'}, 'core')
        (self.client / 'attention.py').write_text('work in progress\n')
        with self.assertRaisesRegex(update.UpdateError, 'uncommitted changes'):
            self.run_update()
        self.assertEqual(self.text('attention.py'), 'v1 attention\n')
        self.assertEqual(self.services.calls, [])

    def test_untracked_files_do_not_block(self):
        (self.client / 'notes.txt').write_text('scratch\n')
        code, _ = self.run_update()
        self.assertEqual(code, 0)

    def test_not_installed_is_refused(self):
        (self.runtime / 'attention.py').unlink()
        with self.assertRaisesRegex(update.UpdateError, 'not installed'):
            self.run_update()

    def test_diverged_checkout_is_refused(self):
        self.publish({'web/index.html': '<p>theirs</p>\n'}, 'remote')
        self.publish({'web/index.html': '<p>mine</p>\n'}, 'local only', repo=self.client, push=False)
        with self.assertRaisesRegex(update.UpdateError, 'diverged'):
            self.run_update()

    def test_checkout_ahead_of_upstream_deploys_its_own_head(self):
        mine = self.publish({'web/index.html': '<p>unpushed</p>\n'}, 'local only', repo=self.client, push=False)
        code, _ = self.run_update('--no-fetch')
        self.assertEqual(code, 0)
        self.assertEqual(self.text('web/index.html'), '<p>unpushed</p>\n')
        self.assertEqual(update.read_record(self.runtime)['commit'], mine)

    def test_explicit_ref_behind_the_checkout_is_refused(self):
        self.publish({'web/index.html': '<p>v2</p>\n'}, 'page')
        self.run_update()
        with self.assertRaisesRegex(update.UpdateError, 'only moves forward'):
            self.run_update('--ref', self.v1)

    def test_explicit_ref_moves_to_that_commit_only(self):
        middle = self.publish({'web/index.html': '<p>v2</p>\n'}, 'page')
        self.publish({'web/index.html': '<p>v3</p>\n'}, 'page again')
        self.run_update('--ref', middle)
        self.assertEqual(self.text('web/index.html'), '<p>v2</p>\n')
        self.assertEqual(self.head(self.client), middle)

    # --- failure and rollback

    def test_unhealthy_web_service_rolls_everything_back_and_leaves_the_checkout_alone(self):
        self.publish({'attention.py': 'v2 attention\n'}, 'core')
        self.services.healthy = False
        update.HEALTH_WAIT_S, saved = 0.1, update.HEALTH_WAIT_S
        self.addCleanup(setattr, update, 'HEALTH_WAIT_S', saved)
        with self.assertRaisesRegex(update.UpdateError, 'not healthy'):
            self.run_update()
        self.assertEqual(self.text('attention.py'), 'v1 attention\n')
        self.assertEqual(update.read_record(self.runtime)['commit'], self.v1)
        self.assertEqual(self.head(self.client), self.v1)
        self.assertEqual(self.services.calls.count('restart_web'), 2, 'restarted again on the restored files')

    def test_a_restarted_service_still_reporting_the_old_version_is_a_failure(self):
        self.publish({'attention.py': 'v2 attention\n'}, 'core')
        self.services.reported = self.v1[:12]
        update.HEALTH_WAIT_S, saved = 0.1, update.HEALTH_WAIT_S
        self.addCleanup(setattr, update, 'HEALTH_WAIT_S', saved)
        with self.assertRaisesRegex(update.UpdateError, 'not healthy'):
            self.run_update()
        self.assertEqual(self.text('attention.py'), 'v1 attention\n')

    def test_rollback_restores_files_record_and_removes_created_ones(self):
        self.publish({'attention.py': 'v2 attention\n', 'contrib/systemd/tmux-attn-web.service': 'unit\n'}, 'core')
        self.run_update()
        self.assertTrue((self.runtime / 'contrib/systemd/tmux-attn-web.service').exists())
        update.rollback(update.latest_backup(self.state), self.services, lambda line: None)
        self.assertEqual(self.text('attention.py'), 'v1 attention\n')
        self.assertFalse((self.runtime / 'contrib/systemd/tmux-attn-web.service').exists())
        self.assertEqual(update.read_record(self.runtime)['commit'], self.v1)

    def test_rollback_of_an_unrecorded_install_removes_the_record_it_created(self):
        (self.runtime / update.RECORD).unlink()
        self.run_update()
        update.rollback(update.latest_backup(self.state), self.services, lambda line: None)
        self.assertFalse((self.runtime / update.RECORD).exists())

    def test_rollback_rejects_a_directory_that_is_not_a_backup(self):
        with self.assertRaisesRegex(update.UpdateError, 'not an update backup'):
            update.rollback(self.state, self.services)

    # --- the installer is never run behind your back

    def test_installer_changes_are_flagged_but_not_applied(self):
        self.publish({'install.py': 'v2 install\n'}, 'hooks')
        code, out = self.run_update()
        self.assertEqual(code, 0)
        self.assertIn('--reinstall', out)
        self.assertNotIn(('reinstall', False), self.services.calls)

    def test_hook_definition_changes_in_attention_py_are_flagged_too(self):
        self.publish({'attention.py': 'VIEW_HOOKS = ()\n'}, 'hooks in core')
        self.run_update('--ref', 'origin/main')
        self.publish({'attention.py': 'VIEW_HOOKS = ("x",)\n'}, 'more hooks')
        _, out = self.run_update()
        self.assertIn('--reinstall', out)

    def test_reinstall_runs_the_installer_after_a_successful_update(self):
        self.publish({'web/index.html': '<p>v2</p>\n'}, 'page')
        self.run_update('--reinstall')
        self.assertIn(('reinstall', False), self.services.calls)

    def test_changed_updater_is_called_out(self):
        self.publish({'update.py': 'v2 update\n'}, 'updater')
        _, out = self.run_update()
        self.assertIn('update.py itself changed', out)

    def test_machine_without_a_web_service_is_updated_with_a_note(self):
        self.services.web = False
        self.publish({'attention.py': 'v2 attention\n'}, 'core')
        code, out = self.run_update()
        self.assertEqual(code, 0)
        self.assertIn('no web service found', out)

    # --- failures that are not UpdateError, and a checkout that cannot be moved

    def test_a_disk_error_halfway_through_the_writes_rolls_everything_back(self):
        self.publish({'attention.py': 'v2 attention\n', 'web/index.html': '<p>v2</p>\n'}, 'both')
        real, calls = update.write_atomic, []

        def flaky(path, data, executable=False):
            calls.append(path)
            if len(calls) == 2:
                raise OSError(28, 'No space left on device')
            return real(path, data, executable)

        with patch.object(update, 'write_atomic', flaky), self.assertRaisesRegex(update.UpdateError, 'No space'):
            self.run_update()
        self.assertEqual((self.text('attention.py'), self.text('web/index.html')), ('v1 attention\n', '<p>v1</p>\n'))
        self.assertEqual(update.read_record(self.runtime)['commit'], self.v1)

    def test_ctrl_c_during_a_restart_rolls_back_and_still_interrupts(self):
        self.publish({'attention.py': 'v2 attention\n'}, 'core')
        self.services.restart_web = lambda: (_ for _ in ()).throw(KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            self.run_update()
        self.assertEqual(self.text('attention.py'), 'v1 attention\n')

    def test_a_checkout_that_could_not_be_fast_forwarded_converges_on_the_next_run(self):
        new = self.publish({'web/index.html': '<p>v2</p>\n'}, 'page')
        real = update.git

        def refuse_merge(src, *args, **kwargs):
            if args[:1] == ('merge',):
                raise update.UpdateError('merge refused')
            return real(src, *args, **kwargs)

        with patch.object(update, 'git', refuse_merge), self.assertRaisesRegex(update.UpdateError, 'merge refused'):
            self.run_update()
        self.assertEqual((self.text('web/index.html'), self.head(self.client)), ('<p>v2</p>\n', self.v1))
        _, out = self.run_update()
        self.assertIn('fast-forwarding', out)
        self.assertEqual(self.head(self.client), new)

    def test_check_reports_a_checkout_that_is_behind_even_when_the_runtime_is_current(self):
        self.publish({'web/index.html': '<p>v2</p>\n'}, 'page')
        self.run_update()
        sh('git', 'reset', '-q', '--hard', self.v1, cwd=self.client)
        code, out = self.run_update('--check', '--no-fetch', '--ref', 'origin/main')
        self.assertIn('update available', out)

    def test_observers_that_cannot_be_restarted_only_warn(self):
        self.publish({'attention.py': 'v2 attention\n'}, 'core')

        def refuse(pids):
            raise update.UpdateError('SIGTERM ignored')

        self.services.restart_observers = refuse
        code, out = self.run_update()
        self.assertEqual(code, 0)
        self.assertIn('warning: observers not restarted (SIGTERM ignored)', out)
        self.assertEqual(self.text('attention.py'), 'v2 attention\n')

    def test_no_observer_after_the_restart_is_a_warning_too(self):
        self.publish({'attention.py': 'v2 attention\n'}, 'core')
        self.services.restart_observers = lambda pids: setattr(self.services, 'pids', [])
        _, out = self.run_update()
        self.assertIn('warning: no observer is running yet', out)

    # --- the installer hint does not evaporate

    def test_the_reinstall_hint_survives_later_updates_until_the_installer_is_rerun(self):
        self.publish({'install.py': 'v2 install\n'}, 'hooks')
        self.assertIn('--reinstall', self.run_update()[1])
        self.publish({'web/index.html': '<p>v2</p>\n'}, 'page')
        self.assertIn('--reinstall', self.run_update()[1], 'ignoring it once must not hide it for good')
        self.publish({'web/index.html': '<p>v3</p>\n'}, 'page again')
        self.run_update('--reinstall')
        self.assertEqual(update.read_record(self.runtime)['installer_commit'], self.head(self.client))
        self.publish({'web/index.html': '<p>v4</p>\n'}, 'and again')
        self.assertNotIn('--reinstall', self.run_update()[1])

    # --- rollback guards

    def test_an_older_backup_is_refused_once_newer_updates_exist(self):
        self.publish({'web/index.html': '<p>v2</p>\n'}, 'page')
        self.run_update()
        first = update.latest_backup(self.state)
        self.publish({'web/index.html': '<p>v3</p>\n'}, 'page again')
        self.run_update()
        with self.assertRaisesRegex(update.UpdateError, 'most recent backup'):
            update.rollback(first, self.services, lambda line: None, runtime=self.runtime, require_current=True)
        update.rollback(update.latest_backup(self.state), self.services, lambda line: None,
                        runtime=self.runtime, require_current=True)
        self.assertEqual(self.text('web/index.html'), '<p>v2</p>\n')

    def test_a_backup_for_another_runtime_is_refused(self):
        self.publish({'web/index.html': '<p>v2</p>\n'}, 'page')
        self.run_update()
        with self.assertRaisesRegex(update.UpdateError, 'was taken for'):
            update.rollback(update.latest_backup(self.state), self.services, runtime=Path(self.tmp.name) / 'other')

    def test_a_tampered_backup_cannot_delete_or_write_outside_the_runtime(self):
        self.publish({'web/index.html': '<p>v2</p>\n'}, 'page')
        self.run_update()
        backup = update.latest_backup(self.state)
        meta = json.loads((backup / 'meta.json').read_text())
        meta['created'] = ['../outside.txt']
        (backup / 'meta.json').write_text(json.dumps(meta))
        victim = self.runtime.parent / 'outside.txt'
        victim.write_text('keep me')
        with self.assertRaisesRegex(update.UpdateError, 'unsafe path'):
            update.rollback(backup, self.services)
        self.assertTrue(victim.exists())

    # --- hostile git trees

    def test_a_tree_that_names_paths_outside_the_managed_directories_is_ignored(self):
        def plumbing(*args, data=None):
            return subprocess.run(['git', '-C', str(self.client), *args], input=data, capture_output=True,
                                  check=True, env=GIT_ENV).stdout.decode().strip()

        def tree(*entries):
            raw = b''.join(mode + b' ' + name + b'\0' + bytes.fromhex(sha) for mode, name, sha in entries)
            return plumbing('hash-object', '-w', '--literally', '-t', 'tree', '--stdin', data=raw)

        blob = plumbing('hash-object', '-w', '--stdin', data=b'pwned')
        inner = tree((b'100644', b'escaped.txt', blob))
        icons = tree((b'40000', b'..', inner), (b'100644', b'ok.svg', blob), (b'120000', b'link.svg', blob))
        root = tree((b'100644', b'attention.py', blob), (b'40000', b'web', tree((b'40000', b'icons', icons))))
        commit = plumbing('commit-tree', root, '-m', 'hostile')
        self.assertEqual(sorted(update.tree_files(self.client, commit)), ['attention.py', 'web/icons/ok.svg'])

    def test_a_failed_write_leaves_no_temporary_file_behind(self):
        target = self.runtime / 'attention.py'
        with patch.object(update.os, 'replace', side_effect=OSError('boom')), self.assertRaises(OSError):
            update.write_atomic(target, b'new')
        self.assertEqual([p.name for p in self.runtime.iterdir() if p.name.startswith('.attention.py')], [])
        self.assertEqual(target.read_text(), 'v1 attention\n')

    def test_unsafe_relative_paths_are_recognised(self):
        for bad in ('../x', 'a/../../x', '/etc/passwd', '', '.', 'a//b/../..'):
            self.assertFalse(update.safe_rel(bad), bad)
        for good in ('attention.py', 'web/icons/a.svg'):
            self.assertTrue(update.safe_rel(good), good)

    def test_install_record_fields_are_validated_before_anyone_trusts_them(self):
        (self.runtime / update.RECORD).write_text(json.dumps({
            'commit': '--upload-pack=x', 'installer_commit': 7, 'source': 'relative/path', 'version': ['x'],
            'ref': 'origin/main'}))
        self.assertEqual(update.read_record(self.runtime), {'ref': 'origin/main'})
        (self.runtime / update.RECORD).write_text(json.dumps({'commit': self.v1, 'source': str(self.client)}))
        self.assertEqual(update.read_record(self.runtime), {'commit': self.v1, 'source': str(self.client)})

    # --- what install.py records

    def test_record_install_writes_the_commit_of_a_clean_checkout(self):
        (self.runtime / update.RECORD).unlink()
        self.assertTrue(update.record_install(self.runtime, self.client))
        self.assertEqual(update.read_record(self.runtime)['commit'], self.v1)
        self.assertEqual(update.read_record(self.runtime)['source'], str(self.client))

    def test_record_install_forgets_the_version_when_the_tree_is_dirty_or_not_git(self):
        (self.client / 'attention.py').write_text('edited\n')
        self.assertFalse(update.record_install(self.runtime, self.client))
        self.assertFalse((self.runtime / update.RECORD).exists(), 'a stale record would claim a version that is not installed')
        update.write_record(self.runtime, self.v1, self.client, 'x')
        self.assertFalse(update.record_install(self.runtime, Path(self.tmp.name)))
        self.assertFalse((self.runtime / update.RECORD).exists())


class ParsingTests(unittest.TestCase):
    def test_web_endpoint_from_launchd_and_systemd(self):
        self.assertEqual(update.parse_web_endpoint(LAUNCHD_TEXT), ('0.0.0.0', 7788))
        self.assertEqual(update.parse_web_endpoint(SYSTEMD_TEXT), ('100.97.0.79', 7788))
        self.assertIsNone(update.parse_web_endpoint('nothing useful here'))

    def test_observers_are_matched_on_the_exact_command_only(self):
        ps = '\n'.join([
            f'  101 /usr/bin/python3 {APP} observe',
            f'  102 /usr/bin/python3 {APP} status',
            f'  103 sh -c pgrep -f "{APP} observe"',
            f'  104 vim {APP} observe',
            '  105 python3 /other/attention.py observe',
            f'  106 /usr/bin/python3.14 {APP} observe',
            f'  107 /Library/Frameworks/Python.framework/Versions/3.14/Resources/Python.app/Contents/MacOS/Python {APP} observe',
            'garbage line',
        ])
        self.assertEqual(update.parse_observers(ps, APP), [101, 106, 107], 'macOS framework Python is `Python`')
        self.assertEqual(update.parse_observers(ps, APP, own_pid=106), [101, 107])


class SystemServicesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.calls = []
        self.outputs = {}
        self.svc = update.SystemServices(self.home / 'runtime', self.home)
        self.svc._run = self.fake_run

    def fake_run(self, args):
        self.calls.append(list(args))
        code, out = self.outputs.get(args[0], (0, ''))
        return SimpleNamespace(returncode=code, stdout=out, stderr='boom' if code else '')

    def linux(self, state='active'):
        self.outputs['systemctl'] = (0, f'ActiveState={state}\nExecStart={{ argv[]=/b/tmux-attn web 100.97.0.79 7788 ; }}')
        return patch.multiple(update, sys=SimpleNamespace(platform='linux'),
                              shutil=SimpleNamespace(which=lambda name: '/bin/' + name, copy2=update.shutil.copy2))

    def test_systemd_service_is_found_and_restarted_by_name(self):
        with self.linux():
            self.assertTrue(self.svc.has_web_service())
            self.assertEqual(self.svc.web_endpoint(), ('100.97.0.79', 7788))
            self.svc.restart_web()
        self.assertEqual(self.calls[-1], ['systemctl', '--user', 'restart', 'tmux-attn-web'])

    def test_an_inactive_systemd_unit_counts_as_no_service(self):
        with self.linux('inactive'):
            self.assertFalse(self.svc.has_web_service())

    def test_launchd_service_is_found_and_kickstarted(self):
        self.outputs['launchctl'] = (0, LAUNCHD_TEXT.split('\n', 1)[1])
        with patch.object(update, 'sys', SimpleNamespace(platform='darwin')):
            self.assertEqual(self.svc.web_endpoint(), ('0.0.0.0', 7788))
            self.svc.restart_web()
        self.assertEqual(self.calls[-1][:4], ['launchctl', 'kickstart', '-k', f'gui/{os.getuid()}/ai.tmux-attn.web'])

    def test_a_failing_restart_is_reported(self):
        with self.linux():
            self.svc.has_web_service()
            self.outputs['systemctl'] = (1, '')
            with self.assertRaisesRegex(update.UpdateError, 'boom'):
                self.svc.restart_web()

    def test_local_urls_bypass_the_wildcard_address_and_bracket_ipv6(self):
        self.assertEqual(update.local_url('0.0.0.0', 7788, '/api/health'), 'http://127.0.0.1:7788/api/health')
        self.assertEqual(update.local_url('100.97.0.79', 7788, '/'), 'http://100.97.0.79:7788/')
        self.assertEqual(update.local_url('fd7a::1', 7788, '/'), 'http://[fd7a::1]:7788/')

    def test_observers_come_from_ps_and_are_restarted_by_pid_then_respawned(self):
        runtime_app = str(self.home / 'runtime' / 'attention.py')
        self.outputs['ps'] = (0, f'  301 /usr/bin/python3 {runtime_app} observe\n  302 vim {runtime_app} observe\n')
        self.assertEqual(self.svc.observers(), [301])
        listings = [[301], [301], []]
        self.svc.observers = lambda: listings.pop(0) if len(listings) > 1 else []
        killed = []
        with patch.object(update.os, 'kill', lambda pid, sig: killed.append(pid)):
            self.svc.restart_observers([999])
        self.assertEqual(killed, [301], 'the list is re-read at kill time, never trusted from earlier')
        self.assertEqual(self.calls[-1], [str(self.home / '.local/bin/tmux-attn'), 'status'])

    def test_signal_errors_are_tolerated_or_reported(self):
        listings = [[5], []]
        self.svc.observers = lambda: listings.pop(0) if len(listings) > 1 else []
        with patch.object(update.os, 'kill', side_effect=ProcessLookupError):
            self.svc.restart_observers()  # it exited on its own between listing and signalling
        self.svc.observers = lambda: [5]
        with patch.object(update.os, 'kill', side_effect=PermissionError('nope')):
            with self.assertRaisesRegex(update.UpdateError, 'cannot signal'):
                self.svc.restart_observers()

    def test_sidebar_mode_is_read_from_the_tmux_config(self):
        conf = self.home / '.config/tmux-attn/sidebar.conf'
        self.assertFalse(self.svc.uses_sidebar())
        conf.parent.mkdir(parents=True)
        conf.write_text("bind-key a display-popup -E 'x'\n")
        self.assertFalse(self.svc.uses_sidebar())
        conf.write_text('bind-key a run-shell -b "x focus y"\n')
        self.assertTrue(self.svc.uses_sidebar())


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.runtime = self.root / 'runtime'
        self.runtime.mkdir()

    def checkout(self, name):
        path = self.root / name
        (path / '.git').mkdir(parents=True)
        (path / 'attention.py').write_text('x\n')
        return path

    def test_precedence_is_flag_then_env_then_own_directory_then_the_record(self):
        flag, env, own, recorded = (self.checkout(n) for n in ('flag', 'env', 'own', 'recorded'))
        (self.runtime / update.RECORD).write_text(json.dumps({'source': str(recorded)}))
        environ = {'TMUX_ATTN_SRC': str(env)}
        self.assertEqual(update.resolve_source(str(flag), self.runtime, environ, own), flag.resolve())
        self.assertEqual(update.resolve_source(None, self.runtime, environ, own), env.resolve())
        self.assertEqual(update.resolve_source(None, self.runtime, {}, own), own.resolve())
        self.assertEqual(update.resolve_source(None, self.runtime, {}, self.root / 'elsewhere'), recorded.resolve())

    def test_unknown_or_invalid_source_is_an_error(self):
        with self.assertRaisesRegex(update.UpdateError, 'cannot find'):
            update.resolve_source(None, self.runtime, {}, self.root / 'elsewhere')
        with self.assertRaisesRegex(update.UpdateError, 'not a tmux-attn git checkout'):
            update.resolve_source(str(self.root), self.runtime, {}, self.root)


if __name__ == '__main__':
    unittest.main()
