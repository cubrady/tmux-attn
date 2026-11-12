import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import urlopen

import attention as a


class HttpAdmissionTests(unittest.TestCase):
    def test_busy_request_budget_returns_503_then_recovers_after_completion(self):
        entered, release = threading.Event(), threading.Event()

        class Handler(a.BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                entered.set()
                release.wait(3)
                self.send_response(200)
                self.send_header('Content-Length', '2')
                self.end_headers()
                self.wfile.write(b'ok')

        server = a.BoundedHTTPServer(('127.0.0.1', 0), Handler, max_workers=1)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f'http://127.0.0.1:{server.server_port}/'
        result = []

        def first():
            with urlopen(url, timeout=4) as response:
                result.append(response.read())

        reader = threading.Thread(target=first)
        reader.start()
        try:
            self.assertTrue(entered.wait(1))
            with self.assertRaises(HTTPError) as caught:
                urlopen(url, timeout=1)
            self.assertEqual(caught.exception.code, 503)
            self.assertEqual(caught.exception.headers['Retry-After'], '1')
            caught.exception.close()
            release.set()
            reader.join(2)
            self.assertEqual(result, [b'ok'])
            with urlopen(url, timeout=1) as response:
                self.assertEqual(response.read(), b'ok')
        finally:
            release.set()
            reader.join(2)
            server.shutdown()
            server.server_close()


class PeerRegistryLimitTests(unittest.TestCase):
    def test_roster_and_enrollment_agree_on_capacity_without_overwriting_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'peers.json'
            peers = [{'name': f'peer{i}', 'url': f'http://100.64.0.{i + 1}:7788'} for i in range(65)]
            path.write_text(json.dumps({'peers': peers}))
            before = path.read_bytes()
            with patch.object(a, 'peers_config_path', return_value=path):
                self.assertEqual(len(a.load_peers()), 64)
                self.assertEqual(len(a.fleet_plan(include_local=False)['hosts']), 64)
                ok, error = a.upsert_peer('extra', 'http://100.64.1.1:7788', 't' * 43)
            self.assertFalse(ok)
            self.assertIn('64', error)
            self.assertEqual(path.read_bytes(), before)

    def test_missing_peer_key_never_triggers_a_network_request(self):
        request = a.Request('http://100.64.0.2:7788/api/send', data=b'{}')
        with patch.object(a, 'load_peers', return_value=[{'name': 'peer', 'url': 'http://100.64.0.2:7788'}]), \
                patch('peer_transport.build_opener') as network:
            with self.assertRaises(ValueError):
                a.peer_urlopen(request)
        network.assert_not_called()
