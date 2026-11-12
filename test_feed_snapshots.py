from concurrent.futures import ThreadPoolExecutor
import threading
import time
import unittest
from unittest.mock import patch

from feed_snapshots import SnapshotCache


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class SnapshotCacheTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.cache = SnapshotCache(clock=self.clock, wall_clock=lambda: 1000 + self.clock.now)
        self.addCleanup(self.cache.stop)

    def wait_idle(self, cache=None):
        cache = cache or self.cache
        deadline = time.monotonic() + 2
        while cache.status()['active'] and time.monotonic() < deadline:
            time.sleep(.002)
        self.assertEqual(cache.status()['active'], 0)

    def test_twenty_readers_share_one_blocked_loader_and_do_not_wait(self):
        entered, release = threading.Event(), threading.Event()
        calls = []

        def blocked():
            calls.append(threading.get_ident())
            entered.set()
            release.wait(30)
            return {'agents': [{'pane': '%1'}]}

        try:
            started = time.monotonic()
            first = self.cache.get('local', blocked)
            self.assertLess(time.monotonic() - started, .2)
            self.assertIsNone(first['payload'])
            self.assertEqual(first['snapshot']['state'], 'cold')
            self.assertTrue(entered.wait(1))
            with ThreadPoolExecutor(max_workers=20) as pool:
                responses = list(pool.map(lambda _: self.cache.get('local', blocked), range(20)))
            self.assertEqual(len(calls), 1)
            self.assertNotEqual(calls[0], threading.get_ident())
            self.assertTrue(all(response['snapshot']['refreshing'] for response in responses))
            self.assertEqual(self.cache.status()['active'], 1)
        finally:
            release.set()
            self.wait_idle()
        ready = self.cache.get('local')
        self.assertEqual(ready['snapshot']['revision'], 1)
        self.assertEqual(ready['snapshot']['generated_at'], 1000)
        self.assertEqual(ready['snapshot']['state'], 'ready')

    def test_publication_and_reads_are_independent_copies(self):
        source = {'agents': [{'state': 'WAIT'}]}
        self.cache.get('local', lambda: source)
        self.wait_idle()
        source['agents'][0]['state'] = 'RUNNING'
        response = self.cache.get('local')
        self.assertEqual(response['payload']['agents'][0]['state'], 'WAIT')
        response['payload']['agents'][0]['state'] = 'IDLE'
        self.assertEqual(self.cache.get('local')['payload']['agents'][0]['state'], 'WAIT')

    def test_failed_refresh_keeps_last_good_value_and_recovers_after_cooldown(self):
        self.cache.get('local', lambda: {'value': 1})
        self.wait_idle()

        def fail():
            raise OSError('unavailable disk')

        self.clock.now = 3
        self.cache.get('local', fail)
        self.wait_idle()
        failed = self.cache.get('local')
        self.assertEqual(failed['payload'], {'value': 1})
        self.assertEqual(failed['snapshot']['state'], 'stale')
        self.assertEqual(failed['snapshot']['last_error'], 'OSError')
        self.assertEqual(failed['snapshot']['revision'], 1)
        self.cache.get('local', lambda: {'value': 2})
        self.assertEqual(self.cache.status()['active'], 0)
        self.clock.now = 9
        self.cache.get('local')
        self.wait_idle()
        recovered = self.cache.get('local')
        self.assertEqual(recovered['payload'], {'value': 2})
        self.assertEqual(recovered['snapshot']['revision'], 2)
        self.assertEqual(recovered['snapshot']['state'], 'ready')
        self.assertIsNone(recovered['snapshot']['last_error'])

    def test_blocked_refresh_serves_stale_snapshot_but_withholds_expired_payload(self):
        entered, release = threading.Event(), threading.Event()
        self.cache.get('local', lambda: {'value': 1})
        self.wait_idle()

        def blocked():
            entered.set()
            release.wait(30)
            return {'value': 2}

        try:
            self.clock.now = 3
            self.cache.get('local', blocked)
            self.assertTrue(entered.wait(1))
            stale = self.cache.get('local')
            self.assertEqual(stale['payload'], {'value': 1})
            self.assertTrue(stale['snapshot']['stale'])
            self.assertEqual(stale['snapshot']['state'], 'refreshing')
            self.clock.now = 3601
            expired = self.cache.get('local')
            self.assertIsNone(expired['payload'])
            self.assertEqual(expired['snapshot']['state'], 'stale')
            self.assertEqual(expired['snapshot']['revision'], 1)
            self.assertEqual(expired['snapshot']['age_s'], 3601)
        finally:
            release.set()
            self.wait_idle()

    def test_failure_before_first_success_is_an_error_not_an_empty_ready_feed(self):
        def fail():
            raise RuntimeError('no feed')

        self.cache.get('local', fail)
        self.wait_idle()
        response = self.cache.get('local')
        self.assertIsNone(response['payload'])
        self.assertEqual(response['snapshot']['state'], 'error')
        self.assertEqual(response['snapshot']['last_error'], 'RuntimeError')
        self.assertIsNone(response['snapshot']['generated_at'])

    def test_workers_and_registration_are_bounded_and_removed_workers_keep_slots(self):
        cache = SnapshotCache(max_workers=2, capacity=2)
        self.addCleanup(cache.stop)
        release = threading.Event()

        def blocked():
            release.wait(30)
            return {'value': 1}

        try:
            cache.get('first', blocked)
            cache.get('second', blocked)
            rejected = cache.get('third', blocked)
            self.assertEqual(rejected['snapshot']['last_error'], 'capacity_exhausted')
            self.assertEqual(cache.status()['active'], 2)
            self.assertEqual(cache.status()['registered'], 2)
            cache.retain([])
            queued = cache.get('new', blocked)
            self.assertIsNone(queued['payload'])
            self.assertFalse(queued['snapshot']['refreshing'])
            self.assertEqual(cache.status()['active'], 2)
            self.assertEqual(cache.status()['registered'], 1)
        finally:
            release.set()
            self.wait_idle(cache)
        cache.get('new')
        self.wait_idle(cache)
        self.assertEqual(cache.get('new')['payload'], {'value': 1})

    def test_idle_entry_eviction_and_repeated_topology_changes_stay_bounded(self):
        cache = SnapshotCache(capacity=2)
        self.addCleanup(cache.stop)
        for index in range(10):
            cache.get(index, lambda index=index: {'value': index})
            self.wait_idle(cache)
            self.assertLessEqual(cache.status()['registered'], 2)
        cache.retain([9])
        self.assertEqual(cache.status()['registered'], 1)
        self.assertEqual(cache.get(9)['payload'], {'value': 9})
        self.assertIsNone(cache.get(0)['payload'])

    def test_removed_source_cannot_publish_into_a_replacement_with_the_same_key(self):
        release, entered = threading.Event(), threading.Event()

        def old_source():
            entered.set()
            release.wait(30)
            return {'source': 'old'}

        try:
            self.cache.get('peer', old_source)
            self.assertTrue(entered.wait(1))
            self.cache.retain([])
            replacement = self.cache.get('peer', lambda: {'source': 'new'})
            self.assertIsNone(replacement['payload'])
            self.assertEqual(self.cache.status()['active'], 1)
        finally:
            release.set()
            self.wait_idle()
        result = self.cache.get('peer')
        self.assertIsNone(result['payload'])
        self.wait_idle()
        self.assertEqual(self.cache.get('peer')['payload'], {'source': 'new'})

    def test_scheduler_refreshes_registered_sources_without_readers_and_stop_is_terminal(self):
        cache = SnapshotCache(ttl=.02, scheduler_interval=.005)
        self.addCleanup(cache.stop)
        condition = threading.Condition()
        calls = []

        def collect():
            with condition:
                calls.append(len(calls) + 1)
                condition.notify_all()
                return {'value': calls[-1]}

        self.assertTrue(cache.register('local', collect))
        self.assertTrue(cache.start())
        self.assertTrue(cache.start())
        with condition:
            self.assertTrue(condition.wait_for(lambda: len(calls) >= 3, timeout=1))
        cache.stop()
        self.wait_idle(cache)
        count = len(calls)
        self.assertFalse(cache.start())
        self.assertFalse(cache.register('new', collect))
        cache.get('local')
        threading.Event().wait(.05)
        self.assertEqual(len(calls), count)

    def test_stop_does_not_wait_for_blocked_loader_and_prevents_further_work(self):
        release, entered = threading.Event(), threading.Event()

        def blocked():
            entered.set()
            release.wait(30)
            return {}

        try:
            self.cache.get('local', blocked)
            self.assertTrue(entered.wait(1))
            at = time.monotonic()
            self.cache.stop()
            self.assertLess(time.monotonic() - at, .2)
            self.cache.get('other', blocked)
            self.assertEqual(self.cache.status()['active'], 1)
            self.assertEqual(self.cache.status()['registered'], 1)
        finally:
            release.set()
            self.wait_idle()

    def test_invalidation_during_collection_requires_a_followup_refresh(self):
        release, entered = threading.Event(), threading.Event()
        calls = []

        def collect():
            calls.append(1)
            if len(calls) == 1:
                entered.set()
                release.wait(30)
            return {'value': len(calls)}

        try:
            self.cache.get('local', collect)
            self.assertTrue(entered.wait(1))
            self.assertTrue(self.cache.invalidate('local'))
            self.assertFalse(self.cache.invalidate('unknown'))
        finally:
            release.set()
            self.wait_idle()
        response = self.cache.get('local')
        self.assertTrue(response['snapshot']['stale'])
        self.wait_idle()
        final = self.cache.get('local')
        self.assertEqual(final['payload'], {'value': 2})
        self.assertEqual(final['snapshot']['revision'], 2)
        self.assertFalse(final['snapshot']['stale'])

    def test_thread_start_failure_cools_down_and_releases_capacity(self):
        with patch.object(threading.Thread, 'start', side_effect=RuntimeError('no threads')):
            response = self.cache.get('local', lambda: {})
        self.assertEqual(response['snapshot']['state'], 'error')
        self.assertEqual(response['snapshot']['last_error'], 'thread_start_failed')
        self.assertEqual(self.cache.status()['active'], 0)
        self.cache.get('local')
        self.assertEqual(self.cache.status()['active'], 0)
        self.clock.now = 6
        self.cache.get('local')
        self.wait_idle()
        self.assertEqual(self.cache.get('local')['payload'], {})


if __name__ == '__main__':
    unittest.main()
