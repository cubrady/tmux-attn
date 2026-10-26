"""Verify ordinary tmux window/pane clicks through a real attached PTY client."""
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import socket
import struct
import subprocess
import sys
import tempfile
import termios
import time
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import attention as a


with tempfile.TemporaryDirectory() as directory:
    home = Path(directory)
    command = ['tmux', '-L', 'attn-window-click-' + str(os.getpid())]

    def tmux(*args):
        return subprocess.check_output(command + list(args), text=True).strip()

    subprocess.run(command + ['-f', '/dev/null', 'new-session', '-d', '-s', 'fixture',
                              '-n', 'alpha', '-x', '120', '-y', '35', '/bin/sh'], check=True)
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 35, 120, 0, 0))
    client = subprocess.Popen(command + ['attach-session', '-t', 'fixture'],
                              stdin=slave, stdout=slave, stderr=slave,
                              env=os.environ | {'TERM': 'xterm-256color'})
    web = None
    result = None
    rolled_back = False
    try:
        def drain(seconds=.1):
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                if select.select([master], [], [], .02)[0]:
                    os.read(master, 65536)

        def wait_for(predicate, label, timeout=5):
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                if predicate():
                    return
                drain(.05)
            raise AssertionError(label)

        drain(.4)
        name = tmux('list-clients', '-F', '#{client_name}')
        sock = tmux('display-message', '-p', '#{socket_path}')
        alpha = tmux('display-message', '-p', '#{pane_id}')
        beta = tmux('new-window', '-d', '-t', 'fixture:', '-n', 'beta', '-P', '-F', '#{pane_id}', '/bin/sh')
        other = tmux('split-window', '-d', '-v', '-t', beta, '-P', '-F', '#{pane_id}', '/bin/sh')
        pids = {pane: tmux('display-message', '-p', '-t', pane, '#{pane_pid}') for pane in [alpha, beta, other]}
        original_status_binding = next(line for line in tmux('list-keys', '-T', 'root').splitlines()
                                       if ' MouseDown1Status ' in line)
        # Index 7 belongs to an unrelated integration; index 91 simulates prior attention hooks.
        tmux('set-hook', '-g', 'after-select-window[7]', 'set-option -g @window_click_unrelated preserved')
        before_hooks = {}
        for event in a.VIEW_HOOKS:
            tmux('set-hook', '-g', f'{event}[91]',
                 f'set-option -g @window_click_prior "tmux-attn original {event}"')
            before_hooks[event] = tmux('show-hooks', '-g', f'{event}[91]')
        unrelated_hook = tmux('show-hooks', '-g', 'after-select-window[7]')
        env = os.environ | {'HOME': directory, 'CODEX_HOME': str(home / '.codex'),
                            'TMUX': f'{sock},{tmux("display-message", "-p", "#{pid}")},0',
                            'TMUX_PANE': alpha, 'TMUX_ATTN_SOCKET': sock,
                            'TMUX_ATTN_NOTIFY': str(home / 'disabled-notifications.json')}
        env.pop('TMUX_ATTN_STATE_DIR', None)
        result = json.loads(subprocess.check_output([sys.executable, str(ROOT / 'install.py'),
                                                     '--enable-codex-hooks'], env=env, text=True))
        app = Path(result['installed'])
        config = Path(result['config']).read_text()
        for event in a.VIEW_HOOKS:
            assert f'{event}[91]' in config, (event, 'missing installed viewing hook')
            hook = tmux('show-hooks', '-g', f'{event}[91]')
            assert str(app) in hook and ' viewed ' in hook, (event, hook)
        assert tmux('show-hooks', '-g', 'after-select-window[7]') == unrelated_hook
        assert original_status_binding in tmux('list-keys', '-T', 'root')
        a.STATE = home / '.local/state/tmux-attn'
        os.environ['TMUX_ATTN_NOTIFY'] = env['TMUX_ATTN_NOTIFY']

        def record(pane, state='IDLE', turn='first'):
            with a.db() as con:
                revision = a.record(con, sock, pane, pids[pane], 'fixture', state,
                                    'Fixture ' + state, state == 'IDLE', event_key=turn)
            return revision

        def row(pane):
            with a.db() as con:
                return dict(con.execute('SELECT * FROM panes WHERE socket=? AND pane=? AND pid=?',
                                        (sock, pane, pids[pane])).fetchone())

        def viewed(pane):
            value = row(pane)
            return value['acknowledged'] == value['revision']

        record(alpha, 'WAIT')
        beta_first = record(beta)
        record(other)
        assert tmux('show-option', '-pv', '-t', beta, a.VIEW_REVISION_OPTION) == f'{pids[beta]}:{beta_first}'
        # Plain native window labels are left aligned: alpha is columns 1..5, beta 7..10.
        tmux('set-option', '-g', 'status-left', '')
        tmux('set-option', '-g', 'status-right', '')
        tmux('set-option', '-g', 'status-justify', 'left')
        tmux('set-option', '-gw', 'window-status-format', '#W')
        tmux('set-option', '-gw', 'window-status-current-format', '#W')
        tmux('set-option', '-gw', 'window-status-separator', ' ')
        tmux('set-option', '-gw', 'automatic-rename', 'off')
        tmux('refresh-client', '-S', '-t', name)
        drain(.5)

        def active():
            return tmux('display-message', '-p', '-c', name, '#{pane_id}')

        def click(x, y=35):
            os.write(master, f'\x1b[<0;{x};{y}M\x1b[<0;{x};{y}m'.encode())
            # Keep native clicks distinct from tmux's default double/triple-click gestures.
            drain(.4)

        click(9)
        wait_for(lambda: active() == beta, 'native beta window label did not select beta')
        wait_for(lambda: viewed(beta), 'native window click did not acknowledge beta completion')
        assert row(beta)['acknowledged'] == beta_first
        assert not viewed(other), 'window click acknowledged an unrelated pane'
        assert row(alpha)['acknowledged'] == 0, 'WAIT pane was acknowledged by selection'

        # Serve the installed app, exercising the same endpoint used by web fleet consumers.
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        web = subprocess.Popen([sys.executable, str(app), 'web', '127.0.0.1', str(port)],
                               env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        ready, _, _ = select.select([web.stdout], [], [], 5)
        assert ready, 'installed web server did not start'
        assert json.loads(web.stdout.readline())['port'] == port

        def agent(pane):
            with urlopen(f'http://127.0.0.1:{port}/api/agents', timeout=3) as response:
                payload = json.load(response)
            return next(item for item in payload['agents'] if item['pane'] == pane)

        assert agent(beta)['reviewed'] and not agent(beta)['unread']
        assert agent(other)['unread'] and agent(alpha)['pending']
        # Remaining focused while a new result arrives must not read it automatically.
        beta_second = record(beta, turn='second')
        drain(.5)
        assert beta_second > beta_first and not viewed(beta)
        assert agent(beta)['unread']
        click(9)  # Clicking the already selected native window runs after-select-window too.
        wait_for(lambda: viewed(beta), 'repeat selected-window click did not acknowledge new result')
        assert row(beta)['acknowledged'] == beta_second

        beta_third = record(beta, turn='third')
        drain(.3)
        assert not viewed(beta)
        click(3)
        wait_for(lambda: active() == alpha, 'native alpha label did not select alpha')
        assert not viewed(beta) and row(alpha)['acknowledged'] == 0
        click(9)
        wait_for(lambda: active() == beta and viewed(beta), 'switch away/back did not acknowledge new result')
        assert row(beta)['acknowledged'] == beta_third and agent(beta)['reviewed']

        # Default pane mouse selection acknowledges only the pane actually opened.
        beta_fourth = record(beta, turn='fourth')
        top = int(tmux('display-message', '-p', '-t', other, '#{pane_top}'))
        click(2, top + 2)
        wait_for(lambda: active() == other and viewed(other), 'native pane click did not acknowledge target pane')
        assert row(beta)['revision'] == beta_fourth and not viewed(beta)
        assert agent(beta)['unread'] and agent(other)['reviewed']
        assert original_status_binding in tmux('list-keys', '-T', 'root')

        subprocess.run([sys.executable, str(ROOT / 'rollback.py'), result['backup']],
                       env=env, check=True, capture_output=True, text=True)
        rolled_back = True
        for event in a.VIEW_HOOKS:
            assert tmux('show-hooks', '-g', f'{event}[91]') == before_hooks[event], event
        assert tmux('show-hooks', '-g', 'after-select-window[7]') == unrelated_hook
        assert original_status_binding in tmux('list-keys', '-T', 'root')
        print('PASS: installed native window/pane mouse hooks, exact-version DB/API confirmation, '
              'same-window repeat, focused new-result unread, switch away/back, WAIT isolation, '
              'unrelated pane/hook isolation, default mouse binding and hook rollback')
    finally:
        if web:
            web.terminate()
            try:
                web.wait(timeout=3)
            except subprocess.TimeoutExpired:
                web.kill()
                web.wait()
        if result and not rolled_back:
            # Stop the installed observer and restore hooks even after a failing assertion.
            subprocess.run([sys.executable, str(ROOT / 'rollback.py'), result['backup']],
                           env=env, capture_output=True)
        client.terminate()
        try:
            client.wait(timeout=3)
        except subprocess.TimeoutExpired:
            client.kill()
            client.wait()
        os.close(master)
        os.close(slave)
        subprocess.run(command + ['kill-server'], capture_output=True)
