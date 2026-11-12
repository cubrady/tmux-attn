"""Real HTTP/security integration, with isolated in-memory collection sources."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

import attention as a
from web_security import WebSecurity


class IntegrationServer(a.BoundedHTTPServer):
    # Admit the simultaneous loopback clients independently of OS backlog defaults.
    request_queue_size = 64


class ArchitectureHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.security = WebSecurity(self.root / 'config/auth.key')
        self.source = self.payload()
        self.local_calls = []
        self.local_loader = lambda: deepcopy(self.source)
        self.peer_loader = lambda _url: self.payload(pane='%2')
        self.peers = []
        self.releases = []
        for mocked in (
            patch.object(a, 'STATE', self.root / 'state'),
            patch.object(a, 'local_host_name', return_value='local'),
            patch.object(a, 'load_peers', side_effect=lambda: deepcopy(self.peers)),
        ):
            mocked.start()
            self.addCleanup(mocked.stop)
        self.feeds = a.FeedSnapshots(local_loader=self.collect_local,
                                     peer_loader=lambda url: self.peer_loader(url))
        self.server = None
        self.opener = build_opener(ProxyHandler({}))
        self.addCleanup(self.close)

    @staticmethod
    def payload(pane='%1'):
        return {'ok': True, 'version': 'test-version', 'summary': a.empty_summary(), 'agents': [{
            'pane': pane, 'pid': '123', 'agent': 'codex', 'tracked': True,
            'session': 'work', 'window': 'agent', 'path': '/project',
            'state': 'IDLE', 'needs_review': True, 'unread': True, 'reviewed': False,
            'revision': 1, 'acknowledged': 0, 'updated': 1,
            'preview': {'users': [], 'agent': 'Completed'},
        }]}

    def collect_local(self):
        self.local_calls.append(threading.get_ident())
        return self.local_loader()

    def start(self):
        base = a.make_web_handler(security=self.security, snapshots=self.feeds)

        class Quiet(base):
            def log_message(self, *_args):
                pass

        self.server = IntegrationServer(('127.0.0.1', 0), Quiet, max_workers=32)
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.feeds.start()

    def close(self):
        for release in self.releases:
            release.set()
        self.feeds.stop()
        deadline = time.monotonic() + 2
        while (self.feeds.local.status()['active'] or self.feeds.peers.status()['active']) and time.monotonic() < deadline:
            time.sleep(.005)
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()

    def request(self, path, *, body=None, headers=None, authenticated=True):
        merged = {'Authorization': 'Bearer ' + self.security.token} if authenticated else {}
        if body is not None:
            merged['Content-Type'] = 'application/json'
        merged.update(headers or {})
        request = Request(self.base + path, data=json.dumps(body).encode() if body is not None else None,
                          headers=merged, method='POST' if body is not None else 'GET')
        at = time.monotonic()
        try:
            response = self.opener.open(request, timeout=3)
        except HTTPError as error:
            response = error
        with response:
            data = response.read()
            content_type = response.headers.get('Content-Type', '')
            result = json.loads(data) if 'application/json' in content_type else data.decode()
            return response.status, result, response.headers, time.monotonic() - at

    def wait_for(self, predicate):
        deadline = time.monotonic() + 2
        while not predicate() and time.monotonic() < deadline:
            time.sleep(.005)
        self.assertTrue(predicate())

    def wait_local(self):
        self.wait_for(lambda: self.feeds.local.status()['active'] == 0)

    def browser_headers(self, cookie=None):
        return {'Origin': self.base, 'X-Tmux-Attn-Request': '1',
                **({'Cookie': cookie} if cookie else {})}

    def test_browser_login_cookie_authorizes_control_and_logout_revokes_it(self):
        with patch.object(a, 'dispatch_send', return_value=(200, {'ok': True})) as send:
            self.start()
            status, page, _, _ = self.request('/', authenticated=False)
            self.assertEqual(status, 200)
            self.assertIn('存取金鑰', page)
            self.assertNotIn(self.security.token, page)
            status, _, _, _ = self.request('/api/auth/session', body={'token': 'wrong'},
                                            headers=self.browser_headers(), authenticated=False)
            self.assertEqual(status, 401)
            status, body, headers, _ = self.request('/api/auth/session', body={'token': self.security.token},
                                                  headers=self.browser_headers(), authenticated=False)
            self.assertEqual(status, 200)
            self.assertTrue(body['authenticated'])
            cookie_header = headers['Set-Cookie']
            self.assertIn('HttpOnly', cookie_header)
            self.assertIn('SameSite=Strict', cookie_header)
            cookie = cookie_header.split(';', 1)[0]
            status, body, _, _ = self.request('/api/auth', headers={'Cookie': cookie}, authenticated=False)
            self.assertEqual(status, 200)
            self.assertTrue(body['authenticated'])
            status, _, _, _ = self.request('/api/agents', headers={'Cookie': cookie}, authenticated=False)
            self.assertEqual(status, 200)
            command = {'pane': '%1', 'pid': '123', 'text': 'Continue'}
            status, _, _, _ = self.request('/api/send', body=command,
                                            headers=self.browser_headers(cookie), authenticated=False)
            self.assertEqual(status, 200)
            send.assert_called_once_with(command)
            status, _, headers, _ = self.request('/api/auth/logout', body={},
                                                 headers=self.browser_headers(cookie), authenticated=False)
            self.assertEqual(status, 200)
            self.assertIn('Max-Age=0', headers['Set-Cookie'])
            self.assertEqual(self.request('/api/agents', headers={'Cookie': cookie}, authenticated=False)[0], 401)

    def test_unauthenticated_or_untrusted_requests_cannot_reach_control(self):
        with patch.object(a, 'dispatch_send', return_value=(200, {'ok': True})) as send:
            self.start()
            self.assertEqual(self.request('/api/agents', authenticated=False)[0], 401)
            command = {'pane': '%1', 'pid': '123', 'text': 'Continue'}
            self.assertEqual(self.request('/api/send', body=command,
                                          headers=self.browser_headers(), authenticated=False)[0], 401)
            self.assertEqual(self.request('/api/agents', headers={'Authorization': 'Bearer wrong'})[0], 401)
            self.assertEqual(self.request('/api/send', body=command, headers={'Origin': 'https://other.example'})[0], 403)
            self.assertEqual(self.request('/api/send', body=command, headers={'Origin': 'null'})[0], 403)
            self.assertEqual(self.request('/api/send', body=command, headers={'Host': 'other.example'})[0], 403)
            self.assertEqual(self.request('/api/send', body=command, headers={'Content-Type': 'text/plain'})[0], 415)
            send.assert_not_called()

    def test_machine_bearer_can_control_without_a_browser_origin(self):
        with patch.object(a, 'dispatch_send', return_value=(200, {'ok': True})) as send:
            self.start()
            command = {'pane': '%1', 'pid': '123', 'text': 'Continue'}
            status, body, _, _ = self.request('/api/send', body=command)
            self.assertEqual(status, 200)
            self.assertTrue(body['ok'])
            send.assert_called_once_with(command)

    def test_blocked_collection_and_twenty_clients_share_one_scan(self):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)

        def blocked():
            entered.set()
            release.wait(30)
            return self.source

        self.local_loader = blocked
        self.start()
        self.assertTrue(entered.wait(1))
        status, cold, _, elapsed = self.request('/api/agents')
        self.assertEqual(status, 200)
        self.assertLess(elapsed, .5)
        self.assertEqual(cold['snapshot']['state'], 'cold')
        self.assertFalse(cold['snapshot']['available'])
        with ThreadPoolExecutor(max_workers=20) as pool:
            results = list(pool.map(lambda _: self.request('/api/agents'), range(20)))
        self.assertTrue(all(row[0] == 200 and row[3] < .5 for row in results))
        self.assertEqual(len(self.local_calls), 1)
        self.assertEqual(self.feeds.local.status()['active'], 1)
        release.set()
        self.wait_local()
        status, ready, _, _ = self.request('/api/agents')
        self.assertEqual(status, 200)
        self.assertEqual(len(ready['agents']), 1)
        self.assertTrue(ready['snapshot']['available'])
        for endpoint in ('/api/fleet/host?name=local', '/api/fleet'):
            status, fleet, _, elapsed = self.request(endpoint)
            self.assertEqual(status, 200)
            self.assertLess(elapsed, .5)
            self.assertEqual(len(fleet['agents']), 1)
        self.assertEqual(len(self.local_calls), 1)

    def test_registered_source_refreshes_periodically_without_http_reads(self):
        condition = threading.Condition()

        def collect():
            with condition:
                value = len(self.local_calls)
                condition.notify_all()
            payload = self.payload()
            payload['agents'][0]['revision'] = value
            return payload

        self.local_loader = collect
        self.feeds.local.ttl = .03
        self.feeds.local.scheduler_interval = .005
        self.start()
        with condition:
            self.assertTrue(condition.wait_for(lambda: len(self.local_calls) >= 3, timeout=1))
        status, payload, _, _ = self.request('/api/agents')
        self.assertEqual(status, 200)
        self.assertGreaterEqual(payload['snapshot']['revision'], 2)
        self.assertGreaterEqual(payload['agents'][0]['revision'], 2)

    def test_successful_ack_invalidates_and_refreshes_the_shared_snapshot(self):
        def acknowledge(payload):
            self.source['agents'][0].update(acknowledged=payload['revision'], reviewed=True, unread=False)
            return 200, {'ok': True, 'revision': payload['revision']}

        with patch.object(a, 'dispatch_ack', side_effect=acknowledge) as ack:
            self.start()
            self.wait_local()
            self.assertTrue(self.request('/api/agents')[1]['agents'][0]['unread'])
            command = {'host': 'local', 'pane': '%1', 'pid': '123', 'revision': 1}
            status, _, _, _ = self.request('/api/ack', body=command)
            self.assertEqual(status, 200)
            ack.assert_called_once_with(command)
            self.wait_for(lambda: len(self.local_calls) >= 2)
            self.wait_local()
            updated = self.request('/api/agents')[1]
            self.assertFalse(updated['agents'][0]['unread'])
            self.assertEqual(updated['snapshot']['revision'], 2)

    def test_blocked_peer_does_not_starve_local_or_other_peers(self):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        self.peers = [
            {'name': 'slow', 'url': 'http://127.0.0.2:7788', 'token': 's' * 43},
            {'name': 'good', 'url': 'http://127.0.0.3:7788', 'token': 'g' * 43},
        ]

        def peer_loader(url):
            if '127.0.0.2' in url:
                entered.set()
                release.wait(30)
                raise TimeoutError('peer unavailable')
            return self.payload(pane='%2')

        self.peer_loader = peer_loader
        self.start()
        self.assertTrue(entered.wait(1))
        self.wait_local()
        good_key = self.feeds._peer_key(self.peers[1])
        self.wait_for(lambda: self.feeds.peers.get(good_key)['payload'] is not None)
        status, fleet, _, elapsed = self.request('/api/fleet')
        self.assertEqual(status, 200)
        self.assertLess(elapsed, .5)
        hosts = {row['name']: row for row in fleet['hosts']}
        self.assertTrue(hosts['local']['ok'])
        self.assertTrue(hosts['good']['ok'])
        self.assertFalse(hosts['slow']['ok'])
        self.assertTrue(hosts['slow']['pending'])
        self.assertEqual(len(fleet['agents']), 2)
        release.set()
        self.wait_for(lambda: self.feeds.peers.status()['active'] == 0)
        unavailable = self.request('/api/fleet/host?name=slow')[1]
        self.assertFalse(unavailable['hosts'][0]['ok'])
        self.assertEqual(unavailable['hosts'][0]['error'], 'TimeoutError')
        self.assertEqual(len(self.request('/api/agents')[1]['agents']), 1)

    def test_expired_snapshot_is_offline_and_preserves_saved_later_metadata(self):
        now = [0.0]
        self.feeds.local.clock = lambda: now[0]
        self.source['agents'][0].update(unread=False, reviewed=True)
        self.start()
        self.wait_local()
        with a.closing(a.web_later_db()) as con, con:
            con.execute('INSERT INTO later(host,pane,pid,revision) VALUES(?,?,?,?)', ('local', '%1', '123', 1))
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)

        def blocked():
            entered.set()
            release.wait(30)
            return self.source

        self.local_loader = blocked
        now[0] = self.feeds.local.max_age + 1
        status, expired, _, elapsed = self.request('/api/fleet/host?name=local')
        self.assertEqual(status, 200)
        self.assertLess(elapsed, .5)
        self.assertFalse(expired['hosts'][0]['ok'])
        self.assertFalse(expired['snapshot']['available'])
        self.assertTrue(expired['snapshot']['stale'])
        self.assertEqual(expired['agents'], [])
        self.assertTrue(entered.wait(1))
        with a.closing(a.web_later_db()) as con:
            self.assertEqual(con.execute('SELECT host,pane,pid FROM later').fetchall(), [('local', '%1', '123')])

    def test_unknown_host_is_a_404_and_public_health_hides_private_details(self):
        self.start()
        status, payload, _, _ = self.request('/api/fleet/host?name=not-registered')
        self.assertEqual(status, 404)
        self.assertFalse(payload['ok'])
        status, health, _, _ = self.request('/api/health', authenticated=False)
        self.assertEqual(status, 200)
        self.assertTrue(health['ok'])
        self.assertNotIn('agent_feed', health)
        self.assertNotIn('project_icons', health)
        self.assertNotIn('snapshots', health)
        private = self.request('/api/health')[1]
        self.assertIn('snapshots', private)


if __name__ == '__main__':
    unittest.main()
