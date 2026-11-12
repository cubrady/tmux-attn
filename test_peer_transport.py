"""Peer authentication over real isolated loopback connections."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request

from peer_transport import open_peer, validate_peer_url


class PeerTransportTests(unittest.TestCase):
    def setUp(self):
        self.token = 'p' * 43
        self.received = []
        self.redirect_received = []
        owner = self

        class Destination(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                owner.redirect_received.append(dict(self.headers))
                self.send_response(200)
                self.send_header('Content-Length', '0')
                self.end_headers()

        self.destination = ThreadingHTTPServer(('127.0.0.1', 0), Destination)
        self.redirect_url = f'http://127.0.0.1:{self.destination.server_port}/should-not-receive'

        class Peer(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                owner.received.append({'path': self.path, 'authorization': self.headers.get('Authorization')})
                if self.path == '/redirect':
                    self.send_response(302)
                    self.send_header('Location', owner.redirect_url)
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                ok = self.headers.get('Authorization') == 'Bearer ' + owner.token
                data = json.dumps({'ok': ok, 'agents': []}).encode()
                self.send_response(200 if ok else 401)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Peer)
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        for server in (self.destination, self.server):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(self.close)

    def close(self):
        for server in (self.server, self.destination):
            server.shutdown()
            server.server_close()

    def test_bearer_is_sent_as_a_header_and_ambient_proxies_are_ignored(self):
        request = Request(self.base + '/api/agents')
        with patch.dict(os.environ, {'HTTP_PROXY': 'http://127.0.0.1:1',
                                     'HTTPS_PROXY': 'http://127.0.0.1:1', 'NO_PROXY': ''}):
            with open_peer(request, self.token, timeout=1) as response:
                self.assertTrue(json.load(response)['ok'])
        self.assertEqual(self.received, [{'path': '/api/agents', 'authorization': 'Bearer ' + self.token}])
        self.assertNotIn(self.token, request.full_url)

    def test_redirects_are_not_followed_and_cannot_forward_credentials(self):
        with self.assertRaises(HTTPError) as captured:
            open_peer(Request(self.base + '/redirect'), self.token, timeout=1)
        self.assertEqual(captured.exception.code, 302)
        captured.exception.close()
        self.assertEqual(len(self.received), 1)
        self.assertEqual(self.redirect_received, [])

    def test_missing_or_malformed_credentials_fail_before_network_access(self):
        for token in (None, '', 'short', 'x' * 129, 'p' * 43 + '\n'):
            with self.subTest(token_type=type(token).__name__), self.assertRaises(ValueError):
                open_peer(Request(self.base + '/api/agents'), token, timeout=1)
        self.assertEqual(self.received, [])

    def test_peer_rejection_is_propagated_instead_of_silently_retrying_without_auth(self):
        with self.assertRaises(HTTPError) as captured:
            open_peer(Request(self.base + '/api/agents'), 'wrong' * 9, timeout=1)
        self.assertEqual(captured.exception.code, 401)
        captured.exception.close()
        self.assertEqual(len(self.received), 1)

    def test_registration_origin_validation_rejects_urls_with_routing_extras(self):
        for url in ('http://127.0.0.1:7788', 'http://100.64.0.2:7788', 'https://work.tailnet.ts.net'):
            self.assertEqual(validate_peer_url(url), url)
        for url in ('http://user:secret@127.0.0.1:7788', 'http://127.0.0.1:7788/path',
                    'http://127.0.0.1:7788?x=1', 'http://127.0.0.1:7788#fragment',
                    'http://8.8.8.8:7788', 'http://0.0.0.0:7788', 'http://224.0.0.1:7788',
                    'file:///etc/passwd', 'http://127.0.0.1:99999', 'http://127.0.0.1\\other'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_peer_url(url)


if __name__ == '__main__':
    unittest.main()
