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


def git(repo, *args, check=True):
    return subprocess.run(['git', '-C', str(repo), *args], check=check, capture_output=True, text=True)


class FleetUpdateTests(unittest.TestCase):
    def setUp(self):
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
        self.assertEqual(command[-2:], ['--ref', second])
        self.assertTrue(popen.call_args.kwargs['start_new_session'])
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

    def test_http_routes_reject_a_short_ref_and_serve_the_log(self):
        root, first, second = self.repo()
        handler = a.make_web_handler()

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


if __name__ == '__main__':
    unittest.main()
