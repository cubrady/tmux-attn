import fcntl
import io
import os
from contextlib import closing
from pathlib import Path
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import attention as a
import codex_observer as observer


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state = Path(self.directory.name)
        state_patch = patch.object(a, 'STATE', self.state)
        state_patch.start()
        self.addCleanup(state_patch.stop)

    def test_healthy_observer_never_spawns(self):
        with a.observer_lock_path('sock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with patch.object(a.subprocess, 'Popen') as spawn:
                a.ensure_observer('sock')
                spawn.assert_not_called()

    def test_stale_pid_does_not_prevent_detached_start(self):
        a.observer_lock_path('sock').write_text(str(os.getpid()))
        with patch.dict(os.environ, {'TMUX': 'wrong,123,0'}), \
                patch.object(a.subprocess, 'Popen') as spawn:
            a.ensure_observer('sock')
        args, kwargs = spawn.call_args
        self.assertEqual(args[0][-2:], [str(a.ROOT / 'attention.py'), 'observe'])
        self.assertTrue(kwargs['start_new_session'])
        self.assertTrue(kwargs['close_fds'])
        for stream in ('stdin', 'stdout', 'stderr'):
            self.assertEqual(kwargs[stream], subprocess.DEVNULL)
        self.assertNotIn('TMUX', kwargs['env'])
        self.assertEqual(kwargs['env']['TMUX_ATTN_SOCKET'], 'sock')
        self.assertEqual(kwargs['env']['TMUX_ATTN_STATE_DIR'], str(self.state))

    def test_start_failure_keeps_status_and_throttles_diagnostics(self):
        with a.db() as con:
            a.record(con, 'sock', '%1', '123', 'codex', 'RUNNING', 'Working')
        live = {('%1', '123'): dict(pane='%1', pid='123', session='test',
                                  window='test', path='/project', window_id='@1')}
        output = io.StringIO()
        with patch.object(sys, 'argv', ['attention.py', 'status']), \
                patch.object(a, 'resolve_socket', return_value='sock'), \
                patch.object(a, 'live_panes', return_value=live), \
                patch.object(a, 'update_window_badges'), \
                patch.object(a.subprocess, 'Popen', side_effect=OSError('spawn failed')), \
                patch('sys.stdout', output):
            a.main()
            a.main()
        self.assertIn('🏃', output.getvalue())
        self.assertEqual(len((self.state / 'observer-errors.log').read_text().splitlines()), 1)

    def test_observer_startup_failure_is_logged(self):
        with patch.object(sys, 'argv', ['attention.py', 'observe']), \
                patch.object(observer, 'observe', side_effect=OSError('database unavailable')):
            with self.assertRaises(OSError):
                a.main()
        self.assertIn('database unavailable', (self.state / 'observer-errors.log').read_text())

    def test_unicode_diagnostic_tail_is_safe_and_throttled(self):
        a.log_observer_error(ValueError('錯誤' * 2000))
        a.log_observer_error(OSError('missing socket'))
        a.log_observer_error(OSError('missing socket'))
        self.assertEqual(len((self.state / 'observer-errors.log').read_text().splitlines()), 2)

    def test_partial_diagnostic_line_cannot_prevent_logging(self):
        line = b'bad OSError: missing socket\n'
        tail = line + b'x' * (4096 - len(line) - 1) + b'\n'
        original = b'1000 ' + tail
        path = self.state / 'observer-errors.log'
        path.write_bytes(original)
        with patch.object(a.time, 'time', return_value=2000):
            a.log_observer_error(OSError('missing socket'))
        self.assertEqual(path.read_bytes(), original + b'2000 OSError: missing socket\n')

    def test_diagnostic_tail_preserves_complete_line_at_exact_boundary(self):
        line = b'2000 OSError: missing socket\n'
        tail = line + b'x' * (4096 - len(line) - 1) + b'\n'
        original = b'1000 older diagnostic\n' + tail
        path = self.state / 'observer-errors.log'
        path.write_bytes(original)
        with patch.object(a.time, 'time', return_value=2000):
            a.log_observer_error(OSError('missing socket'))
        self.assertEqual(path.read_bytes(), original)

    def test_wal_activation_retries_real_contention_until_lock_released(self):
        path = self.state / 'contended.sqlite3'
        release = threading.Event()
        attempts = []

        class ContendedConnection(sqlite3.Connection):
            def execute(self, sql, *args):
                if sql == 'PRAGMA journal_mode=WAL':
                    attempts.append(sql)
                try:
                    return super().execute(sql, *args)
                except sqlite3.OperationalError:
                    release.set()
                    raise

        with closing(sqlite3.connect(path, check_same_thread=False)) as holder:
            holder.execute('CREATE TABLE test(value)')
            holder.execute('BEGIN EXCLUSIVE')

            def unlock():
                if release.wait(2):
                    holder.rollback()

            thread = threading.Thread(target=unlock)
            thread.start()
            try:
                with closing(sqlite3.connect(path, timeout=0, factory=ContendedConnection)) as con:
                    a.enable_wal(con, timeout=1)
                    self.assertEqual(con.execute('PRAGMA journal_mode').fetchone()[0], 'wal')
                    self.assertEqual(con.execute('PRAGMA busy_timeout').fetchone()[0], 3000)
                self.assertGreaterEqual(len(attempts), 2)
            finally:
                release.set()
                thread.join(timeout=2)

    def test_wal_activation_permanent_lock_respects_total_deadline(self):
        path = self.state / 'locked.sqlite3'
        with closing(sqlite3.connect(path)) as holder:
            holder.execute('CREATE TABLE test(value)')
            holder.execute('BEGIN EXCLUSIVE')
            with closing(sqlite3.connect(path, timeout=0.05)) as con:
                started = time.monotonic()
                with self.assertRaises(sqlite3.OperationalError) as raised:
                    a.enable_wal(con, timeout=0.2)
                elapsed = time.monotonic() - started
                self.assertEqual(raised.exception.sqlite_errorcode & 0xff, sqlite3.SQLITE_BUSY)
                self.assertGreaterEqual(elapsed, 0.18)
                self.assertLess(elapsed, 0.5)

    def test_wal_unrelated_error_propagates_and_db_closes_connection(self):
        path = self.state / 'inbox.sqlite3'
        with closing(sqlite3.connect(path)) as source:
            source.execute('CREATE TABLE test(value)')
        con = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
        self.addCleanup(con.close)
        with patch.object(a.sqlite3, 'connect', return_value=con), \
                patch.object(a.time, 'sleep') as sleep:
            with self.assertRaises(sqlite3.OperationalError) as raised:
                a.db()
            self.assertEqual(raised.exception.sqlite_errorcode, sqlite3.SQLITE_READONLY)
            sleep.assert_not_called()
        with self.assertRaises(sqlite3.ProgrammingError):
            con.execute('SELECT 1')

    def test_wal_activation_handles_extended_contention_codes(self):
        for code in (sqlite3.SQLITE_BUSY_RECOVERY, sqlite3.SQLITE_LOCKED_SHAREDCACHE):
            with self.subTest(code=code):
                exc = sqlite3.OperationalError('contention')
                exc.sqlite_errorcode = code
                con = Mock()
                wal_attempts = []

                def execute(sql):
                    if sql == 'PRAGMA journal_mode=WAL':
                        wal_attempts.append(sql)
                        if len(wal_attempts) == 1:
                            raise exc

                con.execute.side_effect = execute
                with patch.object(a.time, 'sleep'):
                    a.enable_wal(con)
                self.assertEqual(len(wal_attempts), 2)

    def test_wal_activation_without_contention_uses_one_pragma(self):
        with closing(sqlite3.connect(self.state / 'healthy.sqlite3')) as con:
            statements = []
            con.set_trace_callback(statements.append)
            a.enable_wal(con)
            self.assertEqual(statements, ['PRAGMA journal_mode=WAL'])


@unittest.skipUnless(shutil.which('tmux') and Path('/proc').exists(), 'requires tmux and Linux process inspection')
class RecoveryIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='attn-recovery-')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.sock = str(self.root / 'tmux.sock')
        self.state = self.root / 'state'
        self.home = self.root / 'home'
        self.home.mkdir()
        self.env = os.environ.copy()
        self.env.pop('TMUX', None)
        self.env.update(HOME=str(self.home), TMUX_ATTN_STATE_DIR=str(self.state),
                        TMUX_ATTN_SOCKET=self.sock)
        self.children = []
        self.addCleanup(self.stop_observers)
        self.addCleanup(lambda: self.tmux('kill-server', check=False))
        # A harmless native executable named codex lets the observer exercise
        # real process ancestry and screen recovery without an agent account.
        executable = self.root / 'codex'
        shutil.copyfile(sys.executable, executable)
        executable.chmod(0o700)
        command = "printf 'Ask Codex to do anything\\n← for agents\\n'; exec " + shlex.quote(str(executable)) + ' -c ' + shlex.quote('import time; time.sleep(120)')
        self.tmux('-f', '/dev/null', 'new-session', '-d', '-s', 'recovery', command)

    def tmux(self, *args, check=True):
        return subprocess.run(['tmux', '-S', self.sock, *args], capture_output=True,
                              text=True, timeout=5, check=check)

    def stop_observers(self):
        for child in self.children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
            for stream in (child.stdout, child.stderr):
                if stream is not None:
                    stream.close()
        if self.state.exists():
            for lock in self.state.glob('codex-observer-*.lock'):
                try:
                    pid = int(lock.read_text())
                    # Check the actual lock to avoid killing a reused stale PID.
                    with lock.open('a') as handle:
                        try:
                            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError:
                            os.kill(pid, signal.SIGKILL)
                except (OSError, ValueError):
                    pass

    def wait_for(self, predicate):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            result = predicate()
            if result:
                return result
            time.sleep(0.05)
        self.fail('observer did not recover within 10 seconds')

    def lock_pid(self):
        files = list(self.state.glob('codex-observer-*.lock')) if self.state.exists() else []
        for path in files:
            try:
                with path.open('a') as handle:
                    try:
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        return int(path.read_text())
            except (OSError, ValueError):
                pass
        return None

    def registered(self):
        database = self.state / 'inbox.sqlite3'
        if not database.exists():
            return False
        try:
            with sqlite3.connect(database) as con:
                return con.execute('SELECT state FROM panes WHERE socket=?', (self.sock,)).fetchone()
        except sqlite3.OperationalError:
            return False

    def status(self, command='status'):
        return subprocess.run([sys.executable, str(a.ROOT / 'attention.py'), command],
                              env=self.env, capture_output=True, text=True, timeout=5, check=True)

    def test_status_recovers_killed_observer_on_nondefault_socket(self):
        # capture_output must complete promptly even with the observer alive.
        self.status()
        first = self.wait_for(self.lock_pid)
        self.assertEqual(self.wait_for(self.registered)[0], 'IDLE')
        self.assertIn('💤', self.status().stdout)
        os.kill(first, signal.SIGKILL)
        self.wait_for(lambda: not self.lock_pid())
        with sqlite3.connect(self.state / 'inbox.sqlite3') as con:
            con.execute('DELETE FROM panes')
        self.status('status-clickable')
        def restarted_pid():
            pid = self.lock_pid()
            return pid if pid is not None and pid != first else None

        second = self.wait_for(restarted_pid)
        self.assertNotEqual(first, second)
        self.assertEqual(self.wait_for(self.registered)[0], 'IDLE')
        self.assertIn('💤', self.status('status-clickable').stdout)

    def test_concurrent_observers_have_one_lock_holder(self):
        for _ in range(4):
            self.children.append(subprocess.Popen(
                [sys.executable, str(a.ROOT / 'attention.py'), 'observe'],
                env=self.env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True))
        pid = self.wait_for(self.lock_pid)
        self.wait_for(lambda: sum(child.poll() is None for child in self.children) == 1)
        self.assertEqual([child.pid for child in self.children if child.poll() is None], [pid])
        self.assertEqual(self.wait_for(self.registered)[0], 'IDLE')

    def test_simultaneous_status_calls_return_and_leave_one_observer(self):
        callers = [subprocess.Popen(
            [sys.executable, str(a.ROOT / 'attention.py'), 'status'],
            env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True) for _ in range(4)]
        self.children.extend(callers)
        for caller in callers:
            _, stderr = caller.communicate(timeout=5)
            self.assertEqual(caller.returncode, 0, stderr)
        pid = self.wait_for(self.lock_pid)
        self.assertEqual(self.wait_for(self.registered)[0], 'IDLE')
        def observer_pids():
            observers = []
            for entry in Path('/proc').iterdir():
                if not entry.name.isdigit():
                    continue
                try:
                    environment = (entry / 'environ').read_bytes().split(b'\0')
                    command = (entry / 'cmdline').read_bytes().split(b'\0')
                    if (f'TMUX_ATTN_STATE_DIR={self.state}'.encode() in environment
                            and command[-3:-1] == [str(a.ROOT / 'attention.py').encode(), b'observe']):
                        observers.append(int(entry.name))
                except (OSError, PermissionError):
                    continue
            return observers

        # Status callers return before detached contenders finish starting.
        # Wait for those children to reject the held lock and exit.
        self.wait_for(lambda: observer_pids() == [pid])
        self.assertEqual(observer_pids(), [pid])


if __name__ == '__main__':
    unittest.main()
