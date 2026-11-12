import datetime
import io
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import attention as a


class UsageSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for mock in (patch.object(a, 'STATE', self.root / 'state'),
                     patch.object(a, 'user_home', return_value=self.root / 'home'),
                     patch.object(a, '_USAGE_CACHE', {}),
                     patch.object(a, '_USAGE_REFRESHING', set()),
                     patch.object(a, '_USAGE_REFRESH_ATTEMPTS', {}),
                     patch.object(a, '_USAGE_SNAPSHOT_SOURCES', {}),
                     patch.object(a, '_USAGE_DISK_READ', {}),
                     patch.object(a, '_CODEXBAR_WIDGETS', a.CodexbarWidgetCache())):
            mock.start()
            self.addCleanup(mock.stop)
        self.now = float(int(time.time()))

    def wait_widgets(self, cache=None):
        cache = cache or a._CODEXBAR_WIDGETS
        deadline = time.monotonic() + 4
        while cache.active and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertFalse(cache.active)

    def row(self, provider='codex', observed=None):
        return {'provider': provider, 'account': provider, 'label': provider,
                'observed_at': self.now if observed is None else observed,
                'windows': [{'key': 'primary', 'label': 'Session', 'shortLabel': '5h',
                             'usedPercent': 25, 'resetsAt': ''}]}

    def test_saved_snapshot_survives_memory_restart_without_network(self):
        a.seed_usage_snapshot({'accounts': [self.row()], 'updated_at': self.now}, now=self.now)
        path = a.usage_snapshot_path()
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        a._USAGE_CACHE.clear()
        with patch.object(a, '_fetch_provider_usage') as fetch:
            result = a.usage_snapshot_payload(['codex'], now=self.now + 2)
        fetch.assert_not_called()
        self.assertEqual(result['accounts'][0]['observed_at'], self.now)
        self.assertEqual(result['provider_status']['codex']['source'], 'saved-cache')
        self.assertEqual(result['provider_status']['codex']['state'], 'ready')

    def test_nonblocking_refresh_is_singleflight_and_preserves_last_good_on_failure(self):
        started = threading.Event()
        release = threading.Event()
        completed = threading.Event()
        a.seed_usage_snapshot({'accounts': [self.row(observed=self.now - 600)]}, now=self.now)

        def slow(_provider):
            started.set()
            release.wait(2)
            completed.set()
            return []

        with patch.object(a, '_fetch_provider_usage', side_effect=slow) as fetch:
            at = time.monotonic()
            result = a.usage_snapshot_payload(['codex'], now=self.now)
            self.assertLess(time.monotonic() - at, .5)
            self.assertTrue(started.wait(1))
            self.assertEqual(result['provider_status']['codex']['state'], 'refreshing')
            self.assertTrue(result['accounts'][0]['stale'])
            again = a.usage_snapshot_payload(['codex'], now=self.now + 1)
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(again['accounts'][0]['observed_at'], self.now - 600)
            release.set()
            self.assertTrue(completed.wait(1))
            deadline = time.monotonic() + 1
            while a._USAGE_REFRESHING and time.monotonic() < deadline:
                time.sleep(.005)
            final = a.usage_snapshot_payload(['codex'], now=self.now + 2)
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(final['provider_status']['codex']['state'], 'unavailable')
            self.assertEqual(final['accounts'][0]['observed_at'], self.now - 600)

    def test_codexbar_exports_use_entry_times_not_widget_generation_time(self):
        folder = self.root / 'home/Library/Group Containers/example.com.steipete.codexbar'
        folder.mkdir(parents=True)
        iso = lambda at: datetime.datetime.fromtimestamp(at, datetime.timezone.utc).isoformat()
        (folder / 'widget-snapshot.json').write_text(json.dumps({
            'generatedAt': iso(self.now), 'entries': [
                {'provider': 'claude', 'updatedAt': iso(self.now - 86400),
                 'primary': {'usedPercent': 25, 'windowMinutes': 300}},
                {'provider': 'antigravity', 'updatedAt': iso(self.now - 60),
                 'primary': {'usedPercent': 10, 'windowMinutes': 10080}}]}))
        a._CODEXBAR_WIDGETS.get(self.root / 'home')
        self.wait_widgets()
        rows = a.codexbar_snapshot_accounts(now=self.now)
        self.assertNotIn('claude', rows)
        self.assertEqual(rows['antigravity'][0]['observed_at'], self.now - 60)
        with patch.object(a.threading, 'Thread') as thread:
            result = a.usage_snapshot_payload(['antigravity'], now=self.now)
        thread.assert_called_once()
        self.assertEqual(result['provider_status']['antigravity']['source'], 'codexbar-cache')
        self.assertEqual(result['provider_status']['antigravity']['state'], 'refreshing')

    def test_blocked_widget_open_cannot_hold_quota_lock_or_hide_codex_export(self):
        folder = self.root / 'home/Library/Group Containers/example.com.steipete.codexbar'
        folder.mkdir(parents=True)
        os.mkfifo(folder / 'widget-snapshot.json')
        codex = self.root / 'home/Library/Application Support/CodexBar'
        codex.mkdir(parents=True)
        (codex / 'codex-account-snapshots.json').write_text(json.dumps({'records': [
            {'snapshot': {'updatedAt': self.now - 978307200,
                          'primary': {'usedPercent': 20, 'windowMinutes': 300}}}]}))
        cache = a._CODEXBAR_WIDGETS
        cache.timeout = .5
        with patch.object(a, '_fetch_provider_usage', return_value=[]):
            started = time.monotonic()
            result = a.usage_snapshot_payload(['codex'], now=self.now)
            self.assertLess(time.monotonic() - started, .25)
            self.assertEqual(result['accounts'][0]['windows'][0]['usedPercent'], 20)
            self.wait_widgets()
            self.assertFalse(cache.cache)  # The blocked worker timed out without a result.
            with patch.object(cache, '_command') as launch:
                for _ in range(5):
                    result = a.usage_snapshot_payload(['codex'], now=self.now)
                launch.assert_not_called()  # Cooldown prevents a process per HTTP request.
            self.assertEqual(result['accounts'][0]['source'], 'codexbar-cache')

    def test_widget_failure_preserves_last_good_sample_and_worker_capacity(self):
        cache = a.CodexbarWidgetCache(timeout=.2, max_workers=1)
        home = str(self.root / 'home')
        sample = [{'entries': []}]
        cache.cache[home] = sample
        with patch.object(cache, '_command', return_value=[a.sys.executable, '-c', 'import time; time.sleep(30)']) as launch:
            self.assertEqual(cache.get(home), sample)
            for _ in range(10):
                self.assertEqual(cache.get(home), sample)
            self.wait_widgets(cache)
            self.assertEqual(cache.get(home), sample)
            self.assertEqual(launch.call_count, 1)

    def test_codex_swift_dates_and_expired_account_are_handled_independently(self):
        folder = self.root / 'home/Library/Application Support/CodexBar'
        folder.mkdir(parents=True)
        (folder / 'codex-account-snapshots.json').write_text(json.dumps({'records': [
            {'snapshot': {'updatedAt': self.now - 978307200 - 60,
                          'primary': {'usedPercent': 20, 'windowMinutes': 300,
                                      'resetsAt': self.now - 978307200 + 300}}},
            {'snapshot': {'updatedAt': self.now - 978307200 - 86400,
                          'primary': {'usedPercent': 99, 'windowMinutes': 300}}}]}))
        rows = a.codexbar_snapshot_accounts(now=self.now)['codex']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['observed_at'], self.now - 60)
        self.assertIn('T', rows[0]['windows'][0]['resetsAt'])

    def test_worker_preserves_actual_account_timestamps_and_ignores_expired_rows(self):
        old = self.row(observed=self.now - 1000)
        old.pop('observed_at')
        old['updatedAt'] = datetime.datetime.fromtimestamp(self.now - 1000, datetime.timezone.utc).isoformat()
        with patch.object(a, '_fetch_provider_usage', return_value=[old]):
            a._refresh_usage_snapshot('codex')
        self.assertAlmostEqual(a._USAGE_CACHE['codex'][1][0]['observed_at'], self.now - 1000, places=3)
        self.assertTrue(a._USAGE_CACHE['codex'][1][0]['stale'])

    def test_invalid_or_expired_saved_cache_is_not_displayed(self):
        path = a.usage_snapshot_path()
        path.parent.mkdir()
        path.write_text(json.dumps({'version': 1, 'providers': {'codex': {
            'observed_at': self.now - 90000, 'accounts': [self.row(observed=self.now - 90000)]}}}))
        with patch.object(a.threading, 'Thread'):
            result = a.usage_snapshot_payload(['codex'], now=self.now)
        self.assertEqual(result['accounts'], [])
        self.assertEqual(result['provider_status']['codex']['state'], 'refreshing')

    def test_force_refresh_is_forwarded_to_known_peer(self):
        response = io.BytesIO(b'{"ok":true,"providers":[],"accounts":[]}')
        with patch.object(a, 'local_host_name', return_value='local'), \
                patch.object(a, 'load_peers', return_value=[{'name': 'peer', 'url': 'http://known:7788'}]), \
                patch.object(a, 'peer_urlopen', return_value=response) as fetch:
            self.assertEqual(a.work_usage('peer', force_refresh=True)[0], 200)
        self.assertEqual(fetch.call_args.args[0].full_url, 'http://known:7788/api/work-usage?refresh=1')


    def test_nonfinite_export_dates_are_ignored_without_crashing(self):
        for value in (float('nan'), float('inf'), -float('inf')):
            self.assertIsNone(a.usage_timestamp(value))
        usage = {'primary': {'usedPercent': 20, 'windowMinutes': 300, 'resetsAt': float('inf')}}
        row = a._codexbar_snapshot_account('codex', usage, self.now)
        self.assertEqual(row['windows'][0]['resetsAt'], '')

    def test_seed_prefers_actual_source_date_over_old_endpoints_fetch_stamp(self):
        row = self.row(observed=self.now)
        row['updatedAt'] = datetime.datetime.fromtimestamp(self.now - 90000, datetime.timezone.utc).isoformat()
        a.seed_usage_snapshot({'accounts': [row]}, now=self.now)
        self.assertNotIn('codex', a._USAGE_CACHE)


    def test_late_cli_response_cannot_replace_newer_imported_snapshot(self):
        old = self.row(observed=self.now - 100)
        newer = self.row(observed=self.now - 10)
        newer['windows'][0]['usedPercent'] = 30
        a._USAGE_CACHE['codex'] = (self.now - 10, [newer])
        with patch.object(a, '_fetch_provider_usage', return_value=[old]):
            a._refresh_usage_snapshot('codex')
        self.assertEqual(a._USAGE_CACHE['codex'][0], self.now - 10)
        self.assertEqual(a._USAGE_CACHE['codex'][1][0]['windows'][0]['usedPercent'], 30)


    def test_partial_app_cache_retains_other_account_and_individual_sources(self):
        first, second = self.row(observed=self.now - 400), self.row(observed=self.now - 500)
        first.update(account='first', source='live')
        second.update(account='second', source='saved-cache')
        a._USAGE_CACHE['codex'] = (self.now - 400, [first, second])
        newer = self.row(observed=self.now - 10)
        newer['account'] = 'first'
        with patch.object(a, 'codexbar_snapshot_accounts', return_value={'codex': [newer]}), \
                patch.object(a.threading, 'Thread'):
            result = a.usage_snapshot_payload(['codex'], now=self.now)
        rows = {row['account']: row for row in result['accounts']}
        self.assertEqual(set(rows), {'first', 'second'})
        self.assertEqual(rows['first']['observed_at'], self.now - 10)
        self.assertEqual(rows['first']['source'], 'codexbar-cache')
        self.assertEqual(rows['second']['observed_at'], self.now - 500)
        self.assertEqual(rows['second']['source'], 'saved-cache')
        self.assertTrue(rows['second']['stale'])

    def test_live_partial_account_result_keeps_other_usable_account(self):
        first, second = self.row(observed=self.now - 400), self.row(observed=self.now - 500)
        first['account'], second['account'] = 'first', 'second'
        a._USAGE_CACHE['codex'] = (self.now - 400, [first, second])
        newer = self.row(observed=self.now - 10)
        newer['account'] = 'first'
        with patch.object(a, '_fetch_provider_usage', return_value=[newer]):
            a._refresh_usage_snapshot('codex')
        rows = {row['account']: row for row in a._USAGE_CACHE['codex'][1]}
        self.assertEqual(set(rows), {'first', 'second'})
        self.assertTrue(rows['second']['stale'])

    def test_anonymous_widget_account_does_not_duplicate_known_live_account(self):
        anonymous = self.row(observed=self.now - 100)
        named = self.row(observed=self.now - 10)
        named.update(account='first', label='Known account')
        merged = a._merge_usage_accounts('codex', [named], (self.now - 100, [anonymous]), self.now, 'live')
        self.assertEqual([row['account'] for row in merged], ['first'])
        newer_anonymous = self.row(observed=self.now)
        merged = a._merge_usage_accounts('codex', [newer_anonymous], (self.now - 10, merged), self.now, 'codexbar-cache')
        self.assertEqual([row['account'] for row in merged], ['first'])
        self.assertEqual(merged[0]['label'], 'Known account')
        second = {**named, 'account': 'second'}
        unchanged = a._merge_usage_accounts('codex', [newer_anonymous], (self.now - 10, [named, second]), self.now, 'codexbar-cache')
        self.assertEqual(len(unchanged), 2)
        self.assertNotIn('codex', [row['account'] for row in unchanged])

    def test_failure_cooldown_starts_after_slow_refresh_finishes(self):
        a._USAGE_REFRESH_ATTEMPTS['codex'] = self.now
        a._USAGE_REFRESHING.add('codex')
        with patch.object(a.time, 'time', return_value=self.now + 31), \
                patch.object(a, '_fetch_provider_usage', return_value=[]) as fetch:
            a._refresh_usage_snapshot('codex')
        self.assertEqual(a._USAGE_REFRESH_ATTEMPTS['codex'], self.now + 31)
        with patch.object(a.threading, 'Thread') as thread:
            result = a.usage_snapshot_payload(['codex'], now=self.now + 34)
        self.assertFalse(any(call.kwargs.get('target') is a._refresh_usage_snapshot
                             for call in thread.call_args_list))
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(result['provider_status']['codex']['state'], 'unavailable')
