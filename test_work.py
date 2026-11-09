import io
import json
import os
import shlex
import shutil
import time
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

import attention as a


class WorkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.project = self.root / 'project with spaces; $(touch injected)'
        self.project.mkdir()
        self.discovery = {'agents': [{'id': 'codex', 'label': 'Codex', 'available': True,
                                      'executable': '/fake/bin/codex'}], 'path': '/fake/bin:/usr/bin'}
        for mock in (patch.object(a, 'STATE', self.root / 'state'),
                     patch.object(a, 'local_host_name', return_value='local'),
                     patch.object(a, 'work_discovery', return_value=self.discovery)):
            mock.start()
            self.addCleanup(mock.stop)

    def payload(self, **overrides):
        return {'path': str(self.project), 'agent': 'codex', 'request_id': 'request_123456', **overrides}

    def test_start_direct_argv_and_idempotence(self):
        with patch.object(a, 'tmux', side_effect=['', '%42\t987']) as tmux:
            code, result = a.dispatch_work_start(self.payload())
            self.assertEqual(code, 200)
            args = tmux.call_args.args
            self.assertEqual(args[0], 'new-session')
            self.assertIn(str(self.project), args)
            self.assertEqual(args[-3:], ('env', 'PATH=/fake/bin:/usr/bin', '/fake/bin/codex'))
            self.assertEqual(a.dispatch_work_start(self.payload()), (code, result))
            self.assertEqual(tmux.call_count, 2)
            self.assertEqual(a.dispatch_work_start(self.payload(agent='claude'))[0], 409)
        self.assertFalse((self.root / 'injected').exists())
        with a.closing(a.work_db()) as con:
            self.assertEqual(con.execute('SELECT agent FROM projects').fetchone()['agent'], 'codex')

    def test_completed_retry_survives_removed_directory_and_agent(self):
        with patch.object(a, 'tmux', side_effect=['', '%1\t123']):
            first = a.dispatch_work_start(self.payload())
        self.project.rmdir()
        self.discovery['agents'][0]['executable'] = ''
        with patch.object(a, 'tmux') as tmux:
            self.assertEqual(a.dispatch_work_start(self.payload()), first)
            tmux.assert_not_called()

    def test_source_usage_exposes_real_cache_age(self):
        accounts = [{'provider': 'codex', 'windows': []}, {'provider': 'claude', 'windows': []}]
        with patch.object(a, 'usage_payload', return_value={'ok': True, 'accounts': accounts}), \
             patch.object(a, '_USAGE_CACHE', {'codex': (100., accounts[:1])}), \
             patch.object(a.time, 'time', return_value=1000.):
            result = a.work_usage()[1]
        self.assertEqual(result['accounts'][0]['observed_at'], 100.)
        self.assertTrue(result['accounts'][0]['stale'])
        self.assertIsNone(result['accounts'][1]['observed_at'])
        self.assertTrue(result['accounts'][1]['stale'])

    def test_timeout_recovery_uses_same_session(self):
        with patch.object(a, 'tmux', side_effect=['', subprocess.TimeoutExpired('tmux', 4)]) as tmux:
            self.assertEqual(a.dispatch_work_start(self.payload())[0], 409)
            attempted_session = tmux.call_args.args[tmux.call_args.args.index('-s') + 1]
        with patch.object(a, 'tmux', return_value='%1\t123') as tmux:
            code, result = a.dispatch_work_start(self.payload())
            self.assertEqual(code, 200)
            self.assertEqual(result['session'], attempted_session)
            self.assertEqual(tmux.call_count, 1)

    def test_rejects_arbitrary_commands_bad_paths_and_unavailable_agents(self):
        with patch.object(a, 'tmux') as tmux:
            for payload in (self.payload(agent='codex; touch /tmp/attack'), self.payload(path='/missing/project'),
                            self.payload(path='relative/path'), self.payload(request_id='bad'),
                            self.payload(path='\x00bad')):
                self.assertEqual(a.dispatch_work_start(payload)[0], 400)
            self.discovery['agents'][0]['executable'] = ''
            self.assertEqual(a.dispatch_work_start(self.payload())[0], 409)
            tmux.assert_not_called()

    def test_observation_keeps_newest_agent_and_pin(self):
        a.remember_work_projects([{'path': str(self.project), 'agent': 'codex', 'updated': 30}])
        self.assertEqual(a.dispatch_work_pin({'path': str(self.project), 'pinned': True})[0], 200)
        a.remember_work_projects([{'path': str(self.project), 'agent': 'claude', 'updated': 20},
                                  {'path': '/fake/shell', 'agent': 'zsh', 'updated': 50}])
        with patch.object(a, 'db', return_value=io.StringIO()), \
             patch.object(a, 'agents_payload', return_value={'agents': []}):
            code, result = a.work_options()
        self.assertEqual(code, 200)
        self.assertEqual(result['default_agent'], 'codex')
        self.assertEqual(result['projects'], [{'path': str(self.project), 'agent': 'codex', 'last_used': 30., 'pinned': True}])
        a.remember_work_projects([{'path': str(self.project), 'agent': 'claude', 'updated': 40}])
        with a.closing(a.work_db()) as con:
            self.assertEqual(con.execute('SELECT agent,pinned FROM projects').fetchone()['agent'], 'claude')
        self.assertEqual(a.dispatch_work_pin({'path': str(self.project), 'pinned': 'true'})[0], 400)

    def test_peer_forwarding_and_fleet_mode(self):
        response = io.BytesIO(json.dumps({'ok': True, 'host': 'peer', 'agents': []}).encode())
        with patch.object(a, 'load_peers', return_value=[{'name': 'peer', 'url': 'http://known:7788'}]), \
             patch.object(a, 'urlopen', return_value=response) as fetch:
            self.assertEqual(a.work_options('peer', fleet_only=True)[0], 200)
            self.assertEqual(fetch.call_args.args[0].full_url, 'http://known:7788/api/work-options')
            self.assertEqual(parse_qs(urlparse(fetch.call_args.args[0].full_url).query), {})
        response = io.BytesIO(b'{"ok":true,"pane":"%2"}')
        with patch.object(a, 'load_peers', return_value=[{'name': 'peer', 'url': 'http://known:7788'}]), \
             patch.object(a, 'urlopen', return_value=response) as fetch:
            self.assertEqual(a.dispatch_work_start(self.payload(host='peer'))[0], 200)
            self.assertNotIn('host', json.loads(fetch.call_args.args[0].data))
        self.assertEqual(a.work_options('', fleet_only=True)[0], 400)
        self.assertEqual(a.work_usage('local', fleet_only=True)[0], 400)
        with patch.object(a, 'load_peers', return_value=[]):
            self.assertEqual(a.work_options('unknown')[0], 404)

    @unittest.skipUnless(shutil.which('tmux'), 'tmux required for integration')
    def test_real_tmux_launch_preserves_cwd_and_literal_path(self):
        socket_path = str(self.root / 'test.sock')
        output = self.root / 'observed-cwd'
        executable = self.root / 'fake-agent'
        executable.write_text('#!/bin/sh\npwd > ' + shlex.quote(str(output)) + '\nsleep 30\n')
        executable.chmod(0o700)
        self.discovery['agents'][0]['executable'] = str(executable)
        self.discovery['path'] = os.defpath

        def isolated_tmux(*args, check=True, sock=None):
            command = [shutil.which('tmux'), '-f', os.devnull, '-S', socket_path, *args]
            result = subprocess.run(command, capture_output=True, text=True, timeout=4)
            if check and result.returncode:
                raise RuntimeError(result.stderr)
            return result.stdout.strip()

        self.addCleanup(lambda: isolated_tmux('kill-server', check=False))
        with patch.object(a, 'tmux', side_effect=isolated_tmux):
            code, result = a.dispatch_work_start(self.payload())
            self.assertEqual(code, 200, result)
            deadline = time.monotonic() + 3
            while not output.exists() and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertEqual(output.read_text().strip(), str(self.project))
            self.assertEqual(a.dispatch_work_start(self.payload())[1]['pane'], result['pane'])
            self.assertEqual(isolated_tmux('list-sessions', '-F', '#{session_name}').splitlines(), [result['session']])
        self.assertFalse((self.project / 'injected').exists())

    def test_http_host_usage_and_start_routes(self):
        class Quiet(a.make_web_handler(fleet_only=True)):
            def log_message(self, *_args):
                pass
        server = a.ThreadingHTTPServer(('127.0.0.1', 0), Quiet)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f'http://127.0.0.1:{server.server_port}'
        with patch.object(a, 'work_peer_request', return_value=(200, {'ok': True, 'accounts': []})) as peer:
            with urlopen(base + '/api/work-usage?host=peer') as response:
                self.assertEqual(json.load(response), {'ok': True, 'accounts': []})
            peer.assert_called_once_with('peer', '/api/work-usage')
        with patch.object(a, 'work_peer_request', return_value=(200, {'ok': True, 'pane': '%1'})):
            with urlopen(Request(base + '/api/work/start', data=json.dumps(self.payload(host='peer')).encode(),
                                 headers={'Content-Type': 'application/json'})) as response:
                self.assertEqual(json.load(response)['pane'], '%1')


if __name__ == '__main__':
    unittest.main()
