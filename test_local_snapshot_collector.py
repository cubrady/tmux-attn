"""Exercise collector isolation with real child processes and stdio pipes."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import attention as a


_REAL_POPEN = subprocess.Popen
_COUNTER_WORKER = '''import json, os, sys
for counter, request in enumerate(sys.stdin, 1):
    print(json.dumps({'ok': True, 'agents': [], 'counter': counter, 'pid': os.getpid()}), flush=True)
'''


class LocalSnapshotCollectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.scripts = []
        self.children = []
        self.collectors = []
        launcher = patch.object(a.subprocess, 'Popen', side_effect=self.launch)
        self.launch_mock = launcher.start()
        self.addCleanup(launcher.stop)
        self.addCleanup(self.close_children)

    def launch(self, _argv, **kwargs):
        script = self.scripts.pop(0) if self.scripts else _COUNTER_WORKER
        child = _REAL_POPEN([sys.executable, '-u', '-c', script], cwd=a.ROOT, **kwargs)
        self.children.append(child)
        return child

    def collector(self, timeout=1.5):
        collector = a.LocalSnapshotCollector(timeout=timeout)
        self.collectors.append(collector)
        return collector

    def close_children(self):
        for collector in self.collectors:
            collector.close()
        for child in self.children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=2)
            for stream in (child.stdin, child.stdout, child.stderr):
                if stream is not None and not stream.closed:
                    stream.close()

    def assert_reaped(self, child):
        self.assertIsNotNone(child.poll())
        self.assertTrue(child.stdin.closed)
        self.assertTrue(child.stdout.closed)
        with self.assertRaises(ProcessLookupError):
            os.kill(child.pid, 0)

    def test_reuses_one_child_and_keeps_worker_state_between_scans(self):
        collector = self.collector()
        first, second = collector(), collector()
        self.assertEqual((first['counter'], second['counter']), (1, 2))
        self.assertEqual(first['pid'], second['pid'])
        self.assertEqual(self.launch_mock.call_count, 1)
        self.assertEqual(collector.proc.pid, first['pid'])

    def test_partial_response_deadline_reaps_child_and_next_scan_recovers(self):
        marker = self.root / 'reached-stdin'
        self.scripts.append('''import json, os, sys, time
sys.stdin.readline()
with open(MARKER, 'w') as stream: stream.write(str(os.getpid()))
sys.stdout.write('{"ok": true, "agents":'); sys.stdout.flush()
time.sleep(30)
'''.replace('MARKER', repr(str(marker))))
        collector = self.collector(timeout=.5)
        with self.assertRaises(subprocess.TimeoutExpired):
            collector()
        self.assertTrue(marker.exists(), 'deadline must exercise a running blocked worker')
        self.assert_reaped(self.children[0])
        result = collector()
        self.assertEqual(result['counter'], 1)
        self.assertNotEqual(result['pid'], int(marker.read_text()))
        self.assertEqual(self.launch_mock.call_count, 2)

    def test_silent_response_deadline_reaps_child(self):
        marker = self.root / 'worker-silent'
        self.scripts.append('''import sys, time
sys.stdin.readline()
with open(MARKER, 'w') as stream: stream.write('started')
time.sleep(30)
'''.replace('MARKER', repr(str(marker))))
        collector = self.collector(timeout=.5)
        with self.assertRaises(subprocess.TimeoutExpired):
            collector()
        self.assertTrue(marker.exists())
        self.assert_reaped(self.children[0])

    def test_oversized_frame_is_rejected_before_json_parsing_and_reaped(self):
        self.scripts.append('''import sys, time
sys.stdin.readline()
sys.stdout.buffer.write(b'x' * (4 * 1024 * 1024 + 1)); sys.stdout.flush()
time.sleep(30)
''')
        collector = self.collector()
        with self.assertRaisesRegex(ValueError, '4 MiB'):
            collector()
        self.assert_reaped(self.children[0])
        self.assertTrue(collector()['ok'])

    def test_unexpected_exit_is_reaped_and_next_scan_starts_new_child(self):
        self.scripts.append('''import sys
sys.stdin.readline()
sys.exit(7)
''')
        collector = self.collector()
        with self.assertRaisesRegex(ValueError, 'exited'):
            collector()
        self.assert_reaped(self.children[0])
        self.assertTrue(collector()['ok'])
        self.assertEqual(self.launch_mock.call_count, 2)

    def test_child_exit_between_successful_scans_closes_old_streams_before_restart(self):
        self.scripts.append('''import json, os, sys
sys.stdin.readline()
print(json.dumps({'ok': True, 'agents': [], 'pid': os.getpid()}), flush=True)
''')
        collector = self.collector()
        first = collector()
        self.children[0].wait(timeout=2)
        second = collector()
        self.assertNotEqual(first['pid'], second['pid'])
        self.assert_reaped(self.children[0])

    def test_close_reaps_worker_closes_pipes_and_prevents_later_scans(self):
        collector = self.collector()
        collector()
        child = self.children[0]
        collector.close()
        collector.close()  # lifecycle shutdown is idempotent
        self.assert_reaped(child)
        with self.assertRaisesRegex(RuntimeError, 'stopped'):
            collector()
        self.assertEqual(self.launch_mock.call_count, 1)

    def test_close_while_spawn_is_in_progress_reaps_unpublished_child(self):
        collector = self.collector()
        spawned, release = threading.Event(), threading.Event()
        errors = []

        def delayed_launch(argv, **kwargs):
            child = self.launch(argv, **kwargs)
            spawned.set()
            release.wait(3)
            return child

        def collect():
            try:
                collector()
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=collect, daemon=True)
        try:
            with patch.object(a.subprocess, 'Popen', side_effect=delayed_launch):
                thread.start()
                self.assertTrue(spawned.wait(2))
                self.assertIsNone(collector.proc, 'Popen result must not be published yet')
                collector.close()
                release.set()
                thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], RuntimeError)
            self.assertIn('stopped', str(errors[0]))
            self.assert_reaped(self.children[0])
            self.assertTrue(collector.closed)
        finally:
            release.set()
            thread.join(2)


    def test_blocked_git_enrichment_does_not_hold_agent_snapshot(self):
        import time
        from feed_snapshots import SnapshotCache
        release, entered = threading.Event(), threading.Event()
        def slow_git(path):
            entered.set()
            release.wait(3)
            return {'repo':'project','branch':'main','dirty':True}
        self.scripts.append("import json,sys\nfor line in sys.stdin: print(json.dumps({'ok':True,'agents':[{'pane':'%2','state':'RUNNING','path':'/project'}]}),flush=True)")
        with patch.object(a, '_GIT_SNAPSHOTS', SnapshotCache(ttl=25,max_workers=2,capacity=256)), patch.object(a,'git_workdir_info',side_effect=slow_git), patch.object(a,'project_technology',return_value=''):
            collector=self.collector()
            try:
                start=time.monotonic(); first=collector()
                self.assertLess(time.monotonic()-start,1)
                self.assertEqual(first['agents'][0]['state'],'RUNNING')
                self.assertTrue(entered.wait(1))
                self.assertEqual(collector()['agents'][0]['branch'],'')
                self.assertEqual(a._GIT_SNAPSHOTS.status()['active'],1)
            finally:
                release.set()
            deadline=time.monotonic()+1
            while a._GIT_SNAPSHOTS.status()['active'] and time.monotonic()<deadline: time.sleep(.005)
            self.assertEqual(collector()['agents'][0]['branch'],'main')


class SnapshotWorkerCliTests(unittest.TestCase):
    def test_actual_worker_cli_handles_multiple_requests_and_exits_on_parent_eof(self):
        script = '''import attention as a, sys
counter = 0
def scan():
    global counter
    counter += 1
    return {'ok': True, 'agents': [], 'counter': counter, 'icon': a.project_technology('/unused')}
a.local_agents_payload = scan
a.install_version = lambda: 'test-version'
sys.argv = ['attention.py', 'snapshot-worker']
a.main()
'''
        with _REAL_POPEN([sys.executable, '-u', '-c', script], cwd=a.ROOT,
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as child:
            output, errors = child.communicate(input=b'{}\n{}\n', timeout=3)
        self.assertEqual(child.returncode, 0, errors.decode())
        rows = [json.loads(line) for line in output.splitlines()]
        self.assertEqual([row['counter'] for row in rows], [1, 2])
        self.assertEqual([row['icon'] for row in rows], ['', ''])
        self.assertTrue(all(row['version'] == 'test-version' for row in rows))

    def test_actual_worker_cli_installs_os_alarm_around_a_stalled_scan(self):
        script = '''import attention as a, sys, time
def shorter_alarm(seconds):
    assert seconds in (0, 12)
    a.signal.setitimer(a.signal.ITIMER_REAL, .2 if seconds else 0)
    return 0
a.signal.alarm = shorter_alarm
a.local_agents_payload = lambda: time.sleep(30)
sys.argv = ['attention.py', 'snapshot-worker']
a.main()
'''
        with _REAL_POPEN([sys.executable, '-u', '-c', script], cwd=a.ROOT,
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as child:
            _output, errors = child.communicate(input=b'{}\n', timeout=3)
        self.assertEqual(child.returncode, -signal.SIGALRM, errors.decode())




class SnapshotTraceTests(unittest.TestCase):
    def test_slow_scan_records_stack_and_successful_scans_do_not_grow_log(self):
        with tempfile.TemporaryDirectory() as directory:
            script = """import io,sys,time,pathlib
import attention as a
a.STATE=pathlib.Path(sys.argv[1])
a.install_version=lambda:'test'
def slow():
 time.sleep(.15)
 return {'ok':True,'agents':[]}
a.local_agents_payload=slow
sys.stdin=io.StringIO('{}\\n')
a.snapshot_worker(trace_after=.05)
p=a.STATE/'snapshot-timeout.log'
first=p.read_text()
assert 'Timeout' in first and 'slow' in first,first
a.local_agents_payload=lambda:{'ok':True,'agents':[]}
sys.stdin=io.StringIO('{}\\n{}\\n')
a.snapshot_worker(trace_after=1)
assert p.read_text()==first
"""
            result = subprocess.run([sys.executable, '-c', script, directory], cwd=a.ROOT, capture_output=True, text=True, timeout=4)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
