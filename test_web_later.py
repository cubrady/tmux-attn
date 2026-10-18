"""Web-only later grouping stays separate from tmux attention state."""
import json
import tempfile
import threading
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request, urlopen

import attention as a


def fleet(state='IDLE', revision=2, acknowledged=2, live=True, online=True):
    return {
        'ok': True,
        'hosts': [{'name': 'work', 'ok': online}],
        'agents': ([{'host': 'work', 'pane': '%1', 'pid': '123', 'tracked': True,
                    'state': state, 'category': 'idle' if state == 'IDLE' else 'running',
                    'needs_review': True, 'revision': revision, 'acknowledged': acknowledged,
                    'unread': revision > acknowledged, 'reviewed': revision <= acknowledged}]
                   if live else []),
        'summary': {},
    }


class WebLaterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = patch.object(a, 'STATE', Path(self.temp.name))
        self.state.start()
        self.addCleanup(self.state.stop)
        self.target = {'host': 'work', 'pane': '%1', 'pid': '123'}

    def add(self, current=None):
        with patch.object(a, 'fleet_payload', return_value=current or fleet()):
            return a.dispatch_web_later(self.target | {'action': 'add', 'revision': 2})

    def test_add_persists_across_reads_and_remove_restores_without_tmux_db(self):
        self.assertEqual(self.add()[0], 200)
        self.assertTrue(a.web_later_fleet(fleet())['agents'][0]['later'])
        self.assertFalse((a.STATE / 'inbox.sqlite3').exists())
        self.assertEqual(a.dispatch_web_later(self.target | {'action': 'remove'})[1]['later'], False)
        self.assertNotIn('later', a.web_later_fleet(fleet())['agents'][0])

    def test_only_viewed_completion_can_be_added(self):
        for current in (fleet(acknowledged=1), fleet(state='RUNNING'), fleet(live=False), fleet(online=False)):
            with self.subTest(current=current):
                self.assertEqual(self.add(current)[0], 409)
        with patch.object(a, 'fleet_payload', return_value=fleet(revision=3, acknowledged=3)):
            self.assertEqual(a.dispatch_web_later(self.target | {'action': 'add', 'revision': 2})[0], 409)

    def test_new_work_and_closed_panes_release_later_but_offline_host_does_not(self):
        for changed in (fleet(state='RUNNING'), fleet(state='WAIT'), fleet(revision=3, acknowledged=2), fleet(live=False)):
            with self.subTest(changed=changed):
                self.add()
                a.web_later_fleet(deepcopy(changed))
                self.assertNotIn('later', a.web_later_fleet(fleet())['agents'][0])
        self.add()
        a.web_later_fleet(fleet(live=False, online=False))
        self.assertTrue(a.web_later_fleet(fleet())['agents'][0]['later'])

    def test_http_clients_share_the_web_services_group(self):
        with patch.object(a, 'fleet_payload', side_effect=lambda **_: fleet()):
            server = a.ThreadingHTTPServer(('127.0.0.1', 0), a.make_web_handler())
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            base = f'http://127.0.0.1:{server.server_port}'
            body = json.dumps(self.target | {'action': 'add', 'revision': 2}).encode()
            request = Request(base + '/api/later', data=body,
                              headers={'Content-Type': 'application/json'}, method='POST')
            with urlopen(request) as response:
                self.assertTrue(json.load(response)['later'])
            with urlopen(base + '/api/fleet') as response:
                self.assertTrue(json.load(response)['agents'][0]['later'])


if __name__ == '__main__':
    unittest.main()
