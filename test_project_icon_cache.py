import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from urllib.request import urlopen

import attention as a


class ProjectIconCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cache = a.ProjectIconCache(timeout=.5)
        self.addCleanup(self.wait_idle)

    def wait_idle(self, cache=None):
        cache = self.cache if cache is None else cache
        deadline = time.monotonic() + 4
        while cache.status()['active'] and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(cache.status()['active'], 0)

    def test_cold_get_does_no_filesystem_io_and_worker_populates_cache(self):
        (self.root / 'package.json').write_text('{"bin":"cli.js"}')
        with patch.object(a.Path, 'is_dir', side_effect=AssertionError('HTTP stat')), \
                patch.object(a, '_project_read', side_effect=AssertionError('HTTP read')):
            self.assertEqual(self.cache.get(str(self.root)), '')
            self.wait_idle()
            self.assertEqual(self.cache.get(str(self.root)), 'cli')
        self.assertEqual(self.cache.status()['failures'], 0)

    def test_disabled_and_invalid_paths_never_launch(self):
        with patch.object(a.subprocess, 'Popen') as launch:
            for path in ('', 'relative', None):
                self.assertEqual(self.cache.get(path), '')
            with patch.dict(os.environ, {'TMUX_ATTN_PROJECT_ICONS': '0'}):
                self.assertEqual(self.cache.get(str(self.root)), '')
                self.assertFalse(self.cache.status()['enabled'])
            launch.assert_not_called()

    def test_start_failure_cooldown_and_cache_capacity(self):
        cache = a.ProjectIconCache(capacity=3)
        with patch.object(a.subprocess, 'Popen', side_effect=OSError('launch failed')) as launch:
            for index in range(5):
                path = str(self.root / str(index))
                cache.get(path)
                self.wait_idle(cache)
                cache.get(path)
                self.assertEqual(launch.call_count, index + 1)
        self.assertEqual(cache.status()['cached'], 3)
        self.assertEqual(cache.status()['failures'], 5)

    def test_thread_start_failure_releases_reservation(self):
        with patch.object(a.threading.Thread, 'start', side_effect=RuntimeError('no threads')):
            self.cache.get(str(self.root))
        self.assertEqual(self.cache.status()['active'], 0)
        self.assertEqual(self.cache.status()['last_failure']['error'], 'thread_start_failed')

    def test_timeout_keeps_last_good_value_and_unreaped_children_keep_capacity(self):
        release, waiting = threading.Event(), threading.Event()

        class StuckChild:
            pid = 123
            returncode = None

            def __init__(self):
                self.stdout = io.BytesIO()

            def communicate(self, timeout):
                raise subprocess.TimeoutExpired('detector', timeout)

            def poll(self):
                return None

            def kill(self):
                pass

            def wait(self):
                waiting.set()
                release.wait(4)
                self.returncode = -9
                return -9

        path = str(self.root)
        self.cache.cache[path] = {'value': 'cli', 'observed': time.monotonic(), 'retry_at': 0}
        try:
            with patch.object(a.subprocess, 'Popen', side_effect=lambda *args, **kw: StuckChild()) as launch:
                self.assertEqual(self.cache.get(path), 'cli')
                self.assertTrue(waiting.wait(1))
                with ThreadPoolExecutor(max_workers=12) as pool:
                    list(pool.map(self.cache.get, [path] * 30 + [str(self.root / str(i)) for i in range(30)]))
                self.assertEqual(launch.call_count, 2)
                self.assertEqual(self.cache.status()['active'], 2)
                self.assertEqual(self.cache.status()['last_failure']['error'], 'timeout')
                self.assertEqual(self.cache.get(path), 'cli')
                self.cache.cache[path]['observed'] = time.monotonic() - self.cache.max_age - 1
                self.assertEqual(self.cache.get(path), '')
                release.set()
                self.wait_idle()
                self.cache.get(path)
                self.assertEqual(launch.call_count, 2)  # failed path remains in cooldown
        finally:
            release.set()

    def test_invalid_worker_result_does_not_replace_last_good_value(self):
        path = str(self.root)
        self.cache.cache[path] = {'value': 'cli', 'observed': time.monotonic(), 'retry_at': 0}
        with patch.object(self.cache, '_command', return_value=[sys.executable, '-c', 'print("123")']):
            self.assertEqual(self.cache.get(path), 'cli')
            self.wait_idle()
        self.assertEqual(self.cache.get(path), 'cli')
        self.assertEqual(self.cache.status()['last_failure']['error'], 'ValueError')

    def test_worker_has_own_deadline_if_parent_disappears(self):
        script = '''import attention as a, sys, time
sys.argv = ['attention.py', 'project-technology', '/blocked']
a.detect_project_technology = lambda path: time.sleep(30)
a.main()
'''
        result = subprocess.run([sys.executable, '-c', script], cwd=a.ROOT, timeout=4, capture_output=True)
        self.assertEqual(result.returncode, -signal.SIGALRM)

    def test_failed_path_recovers_after_cooldown_and_success_refreshes_after_ttl(self):
        path = str(self.root)
        manifest = self.root / 'package.json'
        manifest.write_text('{"bin":"cli.js"}')
        with patch.object(self.cache, '_command', return_value=[sys.executable, '-c', 'print("123")']):
            self.cache.get(path)
            self.wait_idle()
        with patch.object(a.threading.Thread, 'start') as start:
            self.assertEqual(self.cache.get(path), '')
            start.assert_not_called()
        # Advance this entry beyond its cooldown without changing the worker clock.
        self.cache.cache[path]['retry_at'] = time.monotonic() - 1
        self.assertEqual(self.cache.get(path), '')
        self.wait_idle()
        self.assertEqual(self.cache.get(path), 'cli')
        manifest.write_text('{}')
        self.assertEqual(self.cache.get(path), 'cli')
        self.cache.cache[path]['retry_at'] = time.monotonic() - 1
        self.assertEqual(self.cache.get(path), 'cli')
        self.wait_idle()
        self.assertEqual(self.cache.get(path), '')

    def test_hung_open_does_not_block_http_feed_or_health_and_child_is_reaped(self):
        path = str(self.root)
        marker = self.root / 'started'
        script = '''import attention as a, os, sys, time, json
root, marker = sys.argv[1:]
original = os.open
def blocked_open(path, *args, **kwargs):
    if str(path) == root + '/package.json':
        with open(marker, 'w') as stream: stream.write(str(os.getpid()))
        time.sleep(30)
    return original(path, *args, **kwargs)
a.os.open = blocked_open
print(json.dumps(a.detect_project_technology(root)))
'''
        live = {('%1', '123'): {'pane': '%1', 'pid': '123', 'session': 'work', 'window': 'shell',
                                'path': path, 'command': 'zsh'}}
        patches = (
            patch.object(a, '_PROJECT_ICONS', self.cache),
            patch.object(self.cache, '_command', return_value=[sys.executable, '-c', script, path, str(marker)]),
            patch.object(a, 'STATE', self.root / 'state'),
            patch.object(a, 'resolve_socket', return_value='test'),
            patch.object(a, 'live_panes', return_value=live),
            patch.object(a, 'git_workdir_info', return_value={'repo': 'test', 'branch': '', 'dirty': False}),
            patch.object(a, 'shell_pane_preview', return_value={}),
            patch.object(a, 'load_project_aliases', return_value={}),
        )
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        server = a.ThreadingHTTPServer(('127.0.0.1', 0), a.make_web_handler())
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f'http://127.0.0.1:{server.server_port}'

        def get(endpoint):
            started = time.monotonic()
            with urlopen(base + endpoint, timeout=2) as response:
                result = json.load(response)
            self.assertLess(time.monotonic() - started, 2)
            return result

        first = get('/api/agents')
        self.assertEqual(first['agents'][0]['project_technology'], '')
        deadline = time.monotonic() + 2
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(marker.exists(), 'fault injection must actually reach os.open')
        with ThreadPoolExecutor(max_workers=4) as pool:
            responses = list(pool.map(get, ['/api/agents', '/api/fleet/host?name=' + a.local_host_name(),
                                            '/api/health', '/api/agents']))
        self.assertTrue(all(row['ok'] for row in responses))
        self.assertEqual(len(responses[1]['agents']), 1)
        self.wait_idle()
        health = get('/api/health')
        self.assertEqual(health['project_icons']['failures'], 1)
        self.assertEqual(health['project_icons']['last_failure']['error'], 'timeout')
        self.assertEqual(health['project_icons']['active'], 0)
        self.assertIsNotNone(health['agent_feed']['last_success_at'])
        pid = int(marker.read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        self.assertEqual(get('/api/agents')['agents'][0]['project_technology'], '')
        self.assertEqual(self.cache.status()['failures'], 1)
