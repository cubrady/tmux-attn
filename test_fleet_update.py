"""Hub-wide peer update: one commit list, then every outdated client updates itself."""
import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import attention as a
from test_http_support import domain_web_handler


def git(repo, *args, check=True):
    return subprocess.run(['git', '-C', str(repo), *args], check=check, capture_output=True, text=True)


class FleetUpdateTests(unittest.TestCase):
    def setUp(self):
        publication = patch.object(a, 'fleet_publication_error', return_value='')
        self.publication = publication.start()
        self.addCleanup(publication.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.previous_state = a.STATE
        self.previous_proc = a._UPDATE_PROC
        a.STATE = Path(self.tmp.name)
        a._UPDATE_PROC = None
        self.addCleanup(self._restore)

    def _restore(self):
        a.STATE = self.previous_state
        a._UPDATE_PROC = self.previous_proc

    def repo(self):
        root = Path(self.tmp.name) / 'checkout'
        root.mkdir()
        (root / 'update.py').write_text('# test\n')
        git(root, 'init', '-b', 'main')
        (root / 'note.txt').write_text('one\n')
        git(root, 'add', 'note.txt')
        git(root, '-c', 'user.email=test@example.com', '-c', 'user.name=test', 'commit', '-m', 'first commit')
        first = git(root, 'rev-parse', 'HEAD').stdout.strip()
        (root / 'note.txt').write_text('two\n')
        git(root, 'add', 'note.txt')
        git(root, '-c', 'user.email=test@example.com', '-c', 'user.name=test', 'commit', '-m', 'second commit')
        second = git(root, 'rev-parse', 'HEAD').stdout.strip()
        return root, first, second

    def test_update_log_lists_subjects_and_rejects_a_short_or_dirty_ref(self):
        root, first, second = self.repo()
        with patch.object(a, 'install_source', return_value=root), patch.object(a, 'install_commit', return_value=second):
            code, body = a.update_log(first[:12])
            self.assertEqual(code, 200)
            self.assertEqual(body['from'], first)
            self.assertEqual(body['to'], second)
            self.assertEqual([(row['sha'], row['subject']) for row in body['commits']], [(second, 'second commit')])
            self.assertEqual(a.update_log('not a ref')[0], 400)
            self.assertEqual(a.update_log('../attention.py', second)[0], 400)
            self.assertEqual(a.update_log('abc123', second)[0], 400)

    def test_local_update_accepts_only_a_full_commit_and_ignores_a_second_start(self):
        root, _first, second = self.repo()
        running = Mock()
        running.poll.return_value = None
        with patch.object(a, 'install_source', return_value=root), \
                patch.object(a, 'install_commit', return_value='a' * 40), \
                patch.object(a.subprocess, 'Popen', return_value=running) as popen:
            self.assertEqual(a.start_local_update('abcd')[0], 400)
            code, body = a.start_local_update(second)
            self.assertEqual((code, body['started'], body['ref']), (200, True, second))
            again = a.start_local_update(second)[1]
            self.assertEqual((again['started'], again['running'], popen.call_count), (False, True, 1))
        command = popen.call_args.args[0]
        self.assertEqual(command[-2:], [str(root / 'update.py'), second])
        self.assertEqual(command[1:3], ['-c', a._UPDATE_SUPERVISOR])
        self.assertTrue(popen.call_args.kwargs['start_new_session'])
        self.assertEqual(a.local_update_status(), {'ref': second, 'running': True})
        self.assertEqual(popen.call_args.kwargs['cwd'], str(root))

    def test_fleet_update_starts_only_outdated_peers_with_the_hub_commit(self):
        target = 'c' * 40
        peers = [
            {'name': 'cicd-1', 'url': 'http://cicd-1:7788'},
            {'name': 'brad-cicd-2', 'url': 'http://brad:7788'},
            {'name': 'same', 'url': 'http://same:7788'},
            {'name': 'old', 'url': 'http://old:7788'},
            {'name': 'down', 'url': 'http://down:7788'},
        ]
        health = {
            'http://cicd-1:7788': {'ok': True, 'version': 'aaaaaaa'},
            'http://brad:7788': {'ok': True, 'version': 'bbbbbbb'},
            'http://same:7788': {'ok': True, 'version': target[:12]},
            'http://old:7788': {'ok': True, 'version': ''},
        }
        posted = []

        def fetch(url, timeout=3.5):
            if url not in health:
                raise OSError('offline')
            return health[url]

        def post(url, ref, timeout=8):
            posted.append((url, ref))
            if url.startswith('http://cicd-1'):
                return {'ok': False, 'error': 'HTTP 404'}
            return {'ok': True, 'started': True}

        with patch.object(a, 'install_commit', return_value=target), patch.object(a, 'load_peers', return_value=peers):
            code, body = a.fleet_update(fetch=fetch, post=post)
        self.assertEqual(code, 200)
        self.assertEqual(body['ref'], target)
        by_name = {row['name']: row for row in body['hosts']}
        self.assertEqual(posted, [
            ('http://cicd-1:7788', target),
            ('http://brad:7788', target),
        ])
        self.assertEqual(by_name['cicd-1']['error'], 'HTTP 404')
        self.assertTrue(by_name['brad-cicd-2']['started'])
        self.assertTrue(by_name['same']['skipped'])
        self.assertTrue(by_name['old']['manual'])
        self.assertEqual(by_name['down']['error'], 'OSError')

    def test_unpublished_hub_is_refused_before_contacting_peers(self):
        self.publication.return_value = 'push hub first'
        fetch, post = Mock(), Mock()
        with patch.object(a, 'install_commit', return_value='a' * 40), \
                patch.object(a, 'load_peers', return_value=[{'name': 'peer', 'url': 'peer'}]):
            code, body = a.fleet_update(fetch=fetch, post=post)
        self.assertEqual((code, body['error']), (409, 'push hub first'))
        fetch.assert_not_called()
        post.assert_not_called()

    def test_status_waits_for_exit_and_reports_failure_after_rollback(self):
        target = 'c' * 40
        peers = [{'name': name, 'url': name} for name in ('running', 'success', 'failed', 'down', 'legacy', 'stale')]
        health = {
            'running': {'version': target[:12], 'update': {'ref': target, 'running': True}},
            'success': {'version': target[:12], 'started_version': 'old', 'code_current': True,
                        'update': {'ref': target, 'running': False, 'exit_code': 0}},
            'failed': {'version': 'a' * 12, 'update': {'ref': target, 'exit_code': 1}},
            'legacy': {'version': target[:12], 'started_version': target[:12]},
            'stale': {'version': target[:12], 'started_version': 'a' * 12, 'code_current': False,
                      'update': {'ref': target, 'running': False, 'exit_code': 0}},
        }
        with patch.object(a, 'load_peers', return_value=peers):
            self.assertEqual(a.fleet_update_status('bad')[0], 400)
            code, body = a.fleet_update_status(target, fetch=lambda url: health[url])
        self.assertEqual(code, 200)
        self.assertEqual({h['name']: h['state'] for h in body['hosts']}, {
            'running': 'running', 'success': 'success', 'failed': 'failed',
            'down': 'running', 'legacy': 'success', 'stale': 'running',
        })

    def test_supervisor_persists_exit_after_updater_finishes(self):
        target = 'd' * 40
        status = a.STATE / 'remote-update-status.json'
        updater = a.STATE / 'fake-update.py'
        for code in (0, 1):
            updater.write_text(f'import sys\nsys.exit({code})\n')
            result = subprocess.run([a.sys.executable, '-c', a._UPDATE_SUPERVISOR,
                                     str(status), str(updater), target], capture_output=True)
            self.assertEqual(result.returncode, code, result.stderr)
            self.assertEqual(a.local_update_status(), {'ref': target, 'running': False, 'exit_code': code})

    def test_retry_only_selected_hosts_and_rejects_changed_hub(self):
        target = 'c' * 40
        peers = [{'name': name, 'url': name} for name in ('failed', 'success')]
        fetch = Mock(return_value={'ok': True, 'version': 'a' * 12})
        post = Mock(return_value={'ok': True, 'started': True})
        with patch.object(a, 'install_commit', return_value=target), patch.object(a, 'load_peers', return_value=peers):
            code, body = a.fleet_update(fetch=fetch, post=post, names=['failed'], ref=target[:12])
            self.assertEqual(code, 200)
            self.assertEqual([h['name'] for h in body['hosts']], ['failed'])
            post.assert_called_once_with('failed', target)
            self.assertEqual(a.fleet_update(names='bad')[0], 400)
            self.assertEqual(a.fleet_update(ref='b' * 40)[0], 409)

    def test_lost_peer_start_response_is_polled_instead_of_reported_failed(self):
        target = 'c' * 40
        with patch.object(a, 'install_commit', return_value=target), \
                patch.object(a, 'load_peers', return_value=[{'name': 'lab', 'url': 'lab'}]):
            code, body = a.fleet_update(fetch=lambda url: {'ok': True, 'version': 'a' * 12},
                                        post=Mock(side_effect=TimeoutError))
        self.assertEqual(code, 200)
        self.assertTrue(body['hosts'][0]['ok'])
        self.assertTrue(body['hosts'][0]['running'])
        self.assertTrue(body['hosts'][0]['uncertain'])

    def test_http_routes_reject_a_short_ref_and_serve_the_log(self):
        root, first, second = self.repo()
        handler = domain_web_handler(app=a)

        class Quiet(handler):
            def log_message(self, format, *args):
                pass

        server = a.ThreadingHTTPServer(('127.0.0.1', 0), Quiet)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        port = server.server_address[1]

        def post(payload):
            request = Request(f'http://127.0.0.1:{port}/api/update', data=json.dumps(payload).encode(),
                              headers={'Content-Type': 'application/json'}, method='POST')
            with urlopen(request, timeout=2) as response:
                return response.status, json.loads(response.read().decode())

        def get(query):
            with urlopen(f'http://127.0.0.1:{port}/api/update-log?{query}', timeout=2) as response:
                return response.status, json.loads(response.read().decode())

        with patch.object(a, 'install_source', return_value=root), patch.object(a, 'install_commit', return_value=second):
            with self.assertRaises(HTTPError) as caught:
                post({'ref': first[:12]})
            self.assertEqual(caught.exception.code, 400)
            caught.exception.close()
            status, body = get(f'from={first[:12]}')
            self.assertEqual(status, 200)
            self.assertEqual(body['commits'][0]['subject'], 'second commit')


class PublicationTests(unittest.TestCase):
    def test_real_upstream_requires_published_target_and_accepts_ancestors(self):
        with tempfile.TemporaryDirectory() as tmp:
            origin, repo = Path(tmp) / 'origin.git', Path(tmp) / 'repo'
            git(Path(tmp), 'init', '--bare', '-b', 'main', str(origin))
            git(Path(tmp), 'clone', str(origin), str(repo))
            (repo / 'note').write_text('first')
            git(repo, 'add', '.')
            git(repo, '-c', 'user.name=test', '-c', 'user.email=test@example.com', 'commit', '-m', 'first')
            first = git(repo, 'rev-parse', 'HEAD').stdout.strip()
            git(repo, 'push', '-u', 'origin', 'main')
            (repo / 'note').write_text('second')
            git(repo, 'add', '.')
            git(repo, '-c', 'user.name=test', '-c', 'user.email=test@example.com', 'commit', '-m', 'second')
            second = git(repo, 'rev-parse', 'HEAD').stdout.strip()
            with patch.object(a, 'install_source', return_value=repo):
                self.assertEqual(a.fleet_publication_error(first), '')
                self.assertIn('push', a.fleet_publication_error(second))
                git(repo, 'push', 'origin', 'main')
                self.assertEqual(a.fleet_publication_error(second), '')
                self.assertEqual(a.fleet_publication_error(first), '')
                with patch.object(a.subprocess, 'run', side_effect=subprocess.TimeoutExpired('git', 8)):
                    self.assertTrue(a.fleet_publication_error(second))


if __name__ == '__main__':
    unittest.main()
