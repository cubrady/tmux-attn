import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.request import urlopen

import attention as a


def completed(stdout, returncode=0):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr='')


class TailscaleAddressTests(unittest.TestCase):
    def setUp(self):
        a._TAILSCALE_CACHE.update(at=None, ip='')
        self.addCleanup(a._TAILSCALE_CACHE.update, at=None, ip='')

    def test_first_ipv4_line_wins_and_ipv6_is_ignored(self):
        with patch.object(a.subprocess, 'run', return_value=completed('100.64.0.10\nfd7a:115c::1\n')) as run:
            self.assertEqual(a.tailscale_ipv4(), '100.64.0.10')
        self.assertEqual(run.call_args.args[0], ['tailscale', 'ip', '-4'])

    def test_falls_back_to_the_app_bundle_when_path_has_no_cli(self):
        calls = []

        def fake_run(argv, **_kwargs):
            calls.append(argv[0])
            if argv[0] == 'tailscale':
                raise FileNotFoundError(argv[0])
            return completed('100.1.2.3\n')

        with patch.object(a.subprocess, 'run', side_effect=fake_run):
            self.assertEqual(a.tailscale_ipv4(), '100.1.2.3')
        self.assertEqual(calls[0], 'tailscale')
        self.assertIn('/Applications/Tailscale.app/Contents/MacOS/Tailscale', calls)

    def test_no_cli_or_not_logged_in_reports_nothing(self):
        with patch.object(a.subprocess, 'run', side_effect=FileNotFoundError('tailscale')):
            self.assertEqual(a.tailscale_ipv4(), '')
        a._TAILSCALE_CACHE.update(at=None, ip='')
        with patch.object(a.subprocess, 'run', return_value=completed('', returncode=1)):
            self.assertEqual(a.tailscale_ipv4(), '')

    def test_a_hung_cli_is_not_retried_at_every_location(self):
        calls = []

        def hung(argv, **_kwargs):
            calls.append(argv[0])
            raise subprocess.TimeoutExpired(argv, 2)

        with patch.object(a.subprocess, 'run', side_effect=hung):
            self.assertEqual(a.tailscale_ipv4(), '')
        self.assertEqual(calls, ['tailscale'])

    def test_result_is_cached_so_opening_the_popover_does_not_spawn_per_request(self):
        with patch.object(a.subprocess, 'run', return_value=completed('100.64.0.10\n')) as run:
            self.assertEqual(a.tailscale_ipv4(), '100.64.0.10')
            self.assertEqual(a.tailscale_ipv4(), '100.64.0.10')
        self.assertEqual(run.call_count, 1)


class ServerInfoApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.peers_file = Path(self.tmp.name) / 'peers.json'
        self.peers_file.write_text(json.dumps({'peers': [{'name': 'work-mac', 'url': 'http://100.1.2.3:7788'}]}))
        for target in (
            patch.object(a, 'STATE', Path(self.tmp.name)),
            patch.object(a, 'load_peers', return_value=[{'name': 'work-mac', 'url': 'http://100.1.2.3:7788'}]),
            patch.object(a, 'install_version', return_value='abc123def456'),
            patch.object(a, 'local_host_name', return_value='workstation'),
            patch.object(a, 'peers_config_path', return_value=self.peers_file),
            patch.object(a, 'tailscale_ipv4', return_value='100.64.0.10'),
        ):
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

    def test_info_reports_what_the_popover_needs(self):
        port = self.serve(bind='0.0.0.0', port=7788)
        info = json.loads(urlopen(f'http://127.0.0.1:{port}/api/info', timeout=2).read())
        self.assertEqual(info, {
            'ok': True,
            'host': 'workstation',
            'version': 'abc123def456',
            'started_version': a.STARTED_VERSION,
            'fleet_only': False,
            'bind': '0.0.0.0',
            'port': 7788,
            'peers': [{'name': 'work-mac', 'url': 'http://100.1.2.3:7788'}],
            'peers_path': str(self.peers_file),
            'tailscale_url': 'http://100.64.0.10:7788',
        })

    def test_bind_and_port_come_from_the_server_that_serves_the_page(self):
        port = self.serve(bind='192.168.1.5', port=7799, fleet_only=True)
        info = json.loads(urlopen(f'http://127.0.0.1:{port}/api/info', timeout=2).read())
        self.assertEqual((info['bind'], info['port'], info['fleet_only']), ('192.168.1.5', 7799, True))
        self.assertEqual(info['tailscale_url'], 'http://100.64.0.10:7799')

    def test_without_tailscale_the_url_is_empty_rather_than_guessed(self):
        with patch.object(a, 'tailscale_ipv4', return_value=''):
            info = a.server_info_payload(bind='0.0.0.0', port=7788)
        self.assertEqual(info['tailscale_url'], '')


if __name__ == '__main__':
    unittest.main()
