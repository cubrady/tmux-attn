import io
import json
import tempfile
import threading
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch
from urllib.request import urlopen

import attention as a


class InstallInfoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        patcher = patch.object(a, 'ROOT', self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_version_comes_from_the_install_record(self):
        (self.root / 'INSTALL.json').write_text(json.dumps({'version': 'abc123def456', 'source': '/src'}))
        self.assertEqual(a.install_version(), 'abc123def456')
        self.assertEqual(a.install_info()['source'], '/src')

    def test_installs_without_a_usable_record_report_no_version(self):
        self.assertEqual((a.install_info(), a.install_version()), ({}, ''))
        (self.root / 'INSTALL.json').write_text('not json')
        self.assertEqual(a.install_version(), '')
        (self.root / 'INSTALL.json').write_text('[1, 2]')
        self.assertEqual(a.install_version(), '')


class VersionApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for target in (patch.object(a, 'STATE', Path(self.tmp.name)), patch.object(a, 'load_peers', return_value=[]),
                       patch.object(a, 'install_version', return_value='abc123def456')):
            target.start()
            self.addCleanup(target.stop)

    def serve(self, **kwargs):
        class Quiet(a.make_web_handler(**kwargs)):
            def log_message(self, format, *args):
                pass

        server = a.ThreadingHTTPServer(('127.0.0.1', 0), Quiet)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        return server.server_address[1]

    def get(self, port, path):
        return json.loads(urlopen(f'http://127.0.0.1:{port}{path}', timeout=2).read())

    def test_health_and_the_local_feed_report_the_version(self):
        port = self.serve()
        with patch.object(a, 'resolve_socket', return_value='socket'), patch.object(a, 'live_panes', return_value={}):
            health = self.get(port, '/api/health')
            self.assertEqual(health['version'], 'abc123def456')
            self.assertEqual(health['started_version'], a.STARTED_VERSION, 'what the process loaded, not the file on disk')
            self.assertEqual(self.get(port, '/api/agents')['version'], 'abc123def456')

    def test_fleet_reports_the_hub_version_and_each_host_version(self):
        peers = [{'name': 'lab', 'url': 'http://lab:7788'}, {'name': 'old', 'url': 'http://old:7788'}]
        feeds = {'lab': {'ok': True, 'agents': [], 'version': 'ffffffffffff'}, 'old': {'ok': True, 'agents': []}}
        a._FLEET_HOST_CACHE.clear()
        with patch.object(a, 'load_peers', return_value=peers), \
             patch.object(a, 'local_host_name', return_value='home'), \
             patch.object(a, 'resolve_socket', return_value='socket'), patch.object(a, 'live_panes', return_value={}), \
             patch.object(a, 'fetch_peer_agents', side_effect=lambda url, timeout=1.5: feeds[url.split('//')[1].split(':')[0]]):
            fleet = a.fleet_payload(include_local=True)
        versions = {row['name']: row.get('version') for row in fleet['hosts']}
        self.assertEqual(versions, {'home': 'abc123def456', 'lab': 'ffffffffffff', 'old': ''})
        self.assertEqual(fleet['version'], 'abc123def456')

    def test_fleet_only_server_still_reports_its_own_version(self):
        with patch.object(a, 'load_peers', return_value=[]):
            self.assertEqual(a.fleet_payload(include_local=False)['version'], 'abc123def456')


class UpdateCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name in ('recorded', 'envdir', 'flagdir'):
            (self.root / name).mkdir()
            (self.root / name / 'update.py').write_text('# updater\n')
        self.runtime = self.root / 'runtime'
        self.runtime.mkdir()
        (self.runtime / 'INSTALL.json').write_text(json.dumps({'source': str(self.root / 'recorded')}))
        patcher = patch.object(a, 'ROOT', self.runtime)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_update(self, argv, environ=None):
        calls = []

        def fake_execv(exe, args):
            calls.append(args)
            raise SystemExit(0)

        with patch.object(a.os, 'execv', side_effect=fake_execv), \
             patch.dict(a.os.environ, {'TMUX_ATTN_SRC': '', **(environ or {})}):
            with self.assertRaises(SystemExit):
                a.run_update(argv)
        return calls[0]

    def test_uses_the_recorded_checkout_and_passes_the_arguments_through(self):
        args = self.run_update(['--check'])
        self.assertEqual(args[1:], [str(self.root / 'recorded' / 'update.py'), '--check'])

    def test_env_beats_the_record_and_the_flag_beats_env(self):
        env = {'TMUX_ATTN_SRC': str(self.root / 'envdir')}
        self.assertEqual(self.run_update([], env)[1], str(self.root / 'envdir' / 'update.py'))
        flag = str(self.root / 'flagdir')
        for argv in (['--src', flag], [f'--src={flag}']):
            self.assertEqual(self.run_update(argv, env)[1], str(self.root / 'flagdir' / 'update.py'))

    def test_without_any_known_checkout_it_says_how_to_bootstrap(self):
        (self.runtime / 'INSTALL.json').unlink()
        err = io.StringIO()
        with redirect_stderr(err), patch.dict(a.os.environ, {'TMUX_ATTN_SRC': ''}):
            self.assertEqual(a.run_update([]), 1)
        self.assertIn('update.sh', err.getvalue())

    def test_a_recorded_checkout_that_vanished_is_reported(self):
        (self.root / 'recorded' / 'update.py').unlink()
        err = io.StringIO()
        with redirect_stderr(err), patch.dict(a.os.environ, {'TMUX_ATTN_SRC': ''}):
            self.assertEqual(a.run_update([]), 1)
        self.assertIn('update.py', err.getvalue())


if __name__ == '__main__':
    unittest.main()
