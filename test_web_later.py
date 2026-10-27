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

    def test_usage_payload_normalizes_multi_account_falls_back_and_caches(self):
        a._USAGE_CACHE.clear()
        calls = []

        def fake_runner(cmd):
            calls.append(cmd)
            provider = cmd[cmd.index('--provider') + 1]
            if provider == 'codex' and '--all-accounts' in cmd:
                return [
                    {
                        'provider': 'codex',
                        'account': 'alice@example.com',
                        'rateWindowLabels': {'secondary': 'Weekly'},
                        'pace': {'secondary': {'stage': 'farAhead', 'deltaPercent': 55, 'etaSeconds': 34588,
                                               'expectedUsedPercent': 12, 'willLastToReset': False,
                                               'summary': '55% in deficit'}},
                        'usage': {
                            'loginMethod': 'prolite',
                            'primary': None,
                            'secondary': {'usedPercent': 67, 'windowMinutes': 10080,
                                          'resetsAt': '2026-11-02T07:46:55Z', 'resetDescription': 'Nov 2'},
                            'updatedAt': '2026-10-27T03:17:17Z',
                        },
                    },
                    {
                        'provider': 'codex',
                        'account': 'bob@example.com',
                        'rateWindowLabels': {'primary': 'Session', 'secondary': 'Weekly'},
                        'usage': {
                            'loginMethod': 'plus',
                            'primary': {'usedPercent': 100, 'windowMinutes': 300,
                                        'resetsAt': '2026-10-27T05:06:48Z', 'resetDescription': '1:06 PM'},
                            'secondary': {'usedPercent': 30, 'windowMinutes': 10080,
                                          'resetsAt': '2026-11-02T10:41:57Z', 'resetDescription': 'Nov 2'},
                            'updatedAt': '2026-10-27T03:17:20Z',
                        },
                    },
                ]
            if provider == 'claude':
                if '--all-accounts' in cmd:
                    return [{'provider': 'claude', 'error': {'code': 1, 'message': 'No token accounts'}}]
                return [{
                    'provider': 'claude',
                    'rateWindowLabels': {'primary': 'Session', 'secondary': 'Weekly'},
                    'usage': {
                        'accountEmail': 'carol@example.com',
                        'loginMethod': 'Team',
                        'primary': {'usedPercent': 0, 'windowMinutes': 300,
                                    'resetsAt': '2026-10-27T08:20:00Z', 'resetDescription': '4:20pm'},
                        'secondary': {'usedPercent': 100, 'windowMinutes': 10080,
                                      'resetsAt': '2026-10-28T21:00:00Z', 'resetDescription': 'Oct 29'},
                    },
                }]
            return []

        res = a.usage_payload('codex,claude,pi', now=1000.0, binary='/usr/bin/codexbar', runner=fake_runner)
        self.assertTrue(res['ok'])
        self.assertEqual(res['providers'], ['codex', 'claude'])
        self.assertEqual([acc['label'] for acc in res['accounts']], ['alice', 'bob', 'carol'])
        self.assertEqual(res['accounts'][0]['windows'][0]['shortLabel'], '週')
        self.assertEqual(res['accounts'][0]['windows'][0]['pace']['deltaPercent'], 55)
        self.assertEqual(res['accounts'][1]['windows'][0]['shortLabel'], '5h')
        self.assertEqual(res['accounts'][1]['windows'][0]['usedPercent'], 100)

        # Cached within TTL does not invoke runner again
        before_calls = len(calls)
        cached_res = a.usage_payload('codex,claude', now=1050.0, binary='/usr/bin/codexbar', runner=fake_runner)
        self.assertEqual(len(calls), before_calls)
        self.assertEqual(len(cached_res['accounts']), 3)

        # Force refresh bypasses TTL
        a.usage_payload('codex', force_refresh=True, now=1060.0, binary='/usr/bin/codexbar', runner=fake_runner)
        self.assertEqual(len(calls), before_calls + 1)

    def test_cursor_direct_usage_builds_monthly_included_api_and_auto_windows(self):
        def fake_rpc(token, method):
            self.assertEqual(token, 'valid-jwt-token')
            if method == 'GetCurrentPeriodUsage':
                return {
                    'billingCycleStart': '1791940711000',
                    'billingCycleEnd': '1794619111000',
                    'planUsage': {
                        'totalPercentUsed': 62.6,
                        'apiPercentUsed': 88.2,
                        'autoPercentUsed': 60.1,
                    },
                }
            if method == 'GetPlanInfo':
                return {'planInfo': {'planName': 'Pro', 'billingCycleEnd': '1794619111000'}}
            if method == 'GetMe':
                return {'email': 'alice@example.com'}
            return {}

        accounts = a._fetch_cursor_direct_usage(rpc=fake_rpc, tokens=['valid-jwt-token'], now=1793075000.0)
        self.assertEqual(len(accounts), 1)
        acc = accounts[0]
        self.assertEqual(acc['provider'], 'cursor')
        self.assertEqual(acc['label'], 'alice')
        self.assertEqual(acc['plan'], 'Pro')
        self.assertEqual([w['shortLabel'] for w in acc['windows']], ['月', 'API', 'Auto'])
        self.assertEqual([w['usedPercent'] for w in acc['windows']], [63, 88, 60])
        self.assertFalse(acc['windows'][0]['pace']['willLastToReset'])
        self.assertTrue(acc['windows'][1]['pace']['willLastToReset'])


if __name__ == '__main__':
    unittest.main()
