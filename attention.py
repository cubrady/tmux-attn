#!/usr/bin/env python3
"""Pane-scoped, event-driven tmux inbox. Python standard library only."""
from __future__ import annotations
import argparse
import curses
import hashlib
from contextlib import closing
import json
import os
from pathlib import Path
import shlex
import sqlite3
import stat
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
STATE = Path(os.environ.get('TMUX_ATTN_STATE_DIR', Path.home() / '.local/state/tmux-attn'))


def tmux(*args, check=True, sock=None):
    command = ['tmux'] + (['-S', sock] if sock else []) + list(args)
    p = subprocess.run(command, capture_output=True, text=True, timeout=None if args and args[0]=='display-popup' else 4)
    if check and p.returncode:
        raise RuntimeError(p.stderr.strip())
    return p.stdout.strip()


def socket():
    return os.environ.get('TMUX', '').split(',')[0] or tmux('display-message', '-p', '#{socket_path}')


def db():
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    con = sqlite3.connect(STATE / 'inbox.sqlite3', timeout=3)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('''CREATE TABLE IF NOT EXISTS panes (
        socket TEXT, pane TEXT, pid TEXT, agent TEXT, state TEXT, reason TEXT,
        updated REAL, revision INTEGER, acknowledged INTEGER DEFAULT 0,
        snoozed_revision INTEGER DEFAULT 0, snoozed_until REAL DEFAULT 0, needs_review INTEGER DEFAULT 0,
        PRIMARY KEY(socket,pane,pid))''')
    if 'needs_review' not in {r[1] for r in con.execute('PRAGMA table_info(panes)')}:
        try:
            con.execute('ALTER TABLE panes ADD COLUMN needs_review INTEGER DEFAULT 0')
        except sqlite3.OperationalError as exc:
            if 'duplicate column' not in str(exc):
                raise
    if 'event_key' not in {r[1] for r in con.execute('PRAGMA table_info(panes)')}:
        try:
            con.execute("ALTER TABLE panes ADD COLUMN event_key TEXT DEFAULT ''")
        except sqlite3.OperationalError as exc:
            if 'duplicate column' not in str(exc): raise
    if 'last_event_time' not in {r[1] for r in con.execute('PRAGMA table_info(panes)')}:
        try:
            con.execute('ALTER TABLE panes ADD COLUMN last_event_time REAL DEFAULT 0')
        except sqlite3.OperationalError as exc:
            if 'duplicate column' not in str(exc): raise
    con.execute('''CREATE TABLE IF NOT EXISTS codex_sessions (
        socket TEXT, pane TEXT, pid TEXT, cli_pid INTEGER DEFAULT 0,
        thread_id TEXT, rollout_path TEXT, origin TEXT,
        PRIMARY KEY(socket,pane,pid))''')
    return con


def clean(value, limit=240):
    return ''.join(c for c in str(value) if c.isprintable())[:limit]


def record(con, sock, pane, pid, agent, state, reason, needs_review=False, event_time=None, event_key=""):
    reason = clean(reason)
    with con:
        old = con.execute('SELECT * FROM panes WHERE socket=? AND pane=? AND pid=?', (sock, pane, pid)).fetchone()
        stamp = event_time if event_time is not None else time.time()
        if old and event_time is not None and max(old['updated'],old['last_event_time']) > event_time:
            return
        if old and not (needs_review and event_key and event_key != old['event_key']) and (old['state'], old['reason'], old['agent'], bool(old['needs_review'])) == (state, reason, agent, needs_review):
            con.execute("UPDATE panes SET last_event_time=?,event_key=CASE WHEN ?<>'' THEN ? ELSE event_key END WHERE socket=? AND pane=? AND pid=?",
                        (stamp,event_key,event_key,sock,pane,pid))
            return
        con.execute('''INSERT INTO panes(socket,pane,pid,agent,state,reason,updated,revision,needs_review,event_key,last_event_time)
            VALUES(?,?,?,?,?,?,?,1,?,?,?) ON CONFLICT(socket,pane,pid) DO UPDATE SET
            agent=excluded.agent,state=excluded.state,reason=excluded.reason,
            updated=excluded.updated,revision=panes.revision+1,needs_review=excluded.needs_review,event_key=excluded.event_key,last_event_time=excluded.last_event_time''',
            (sock, pane, pid, agent, state, reason,stamp,int(needs_review),event_key,stamp))


def classify(agent, event, data):
    if event in ('SessionStart', 'session_start'):
        return 'IDLE', 'Registered; no new work yet'
    if event in ('SessionEnd', 'session_shutdown'):
        return 'IDLE', 'Agent exited'
    if event == 'idle_no_review':
        return 'IDLE', 'Waiting for next task'
    if event in ('UserPromptSubmit', 'agent_start', 'ui_prompt_end', 'PostToolUse'):
        return 'RUNNING', 'Working'
    if event in ('Interrupt',):
        return 'IDLE', 'Interrupted; review current work', True
    if event == 'PermissionRequest':
        return 'WAIT', 'Approval needed: ' + (data.get('tool_input', {}).get('description') or 'Tool operation')
    if event == 'ui_prompt_start':
        return 'WAIT', data.get('title') or 'Answer or decision needed'
    if event == 'PreToolUse':
        if data.get('tool_name', '').split('__')[-1].rsplit('.',1)[-1] in ('AskUserQuestion', 'request_user_input', 'request_user_input_async', 'ask_question'):
            return 'WAIT', 'Answer or decision needed'
        return 'RUNNING', 'Working'
    if event == 'Notification':
        if data.get('notification_type') in ('permission_prompt', 'elicitation_dialog', 'agent_needs_input', 'worker_permission_prompt'):
            return 'WAIT', data.get('message') or 'Input needed'
        return None
    if event == 'StopFailure':
        return 'IDLE', 'Agent stopped with an error; review needed', True
    if event in ('Stop', 'agent_settled'):
        last = str(data.get('last_assistant_message') or '').rstrip()
        if last.endswith(('?', '？')):
            return 'WAIT', 'Response ends with a question; likely needs input'
        if agent == 'claude' and any(t.get('status') == 'running' for t in data.get('background_tasks', [])):
            return 'IDLE', 'Response finished; review needed; background tasks still running', True
        return 'IDLE', 'Response finished; review needed', True
    return None


def hook_target():
    # Codex may construct a hook environment from an older shell snapshot.
    # Resolve the actual parent process chain instead of trusting TMUX_PANE.
    process = subprocess.run(['ps','-axo','pid=,ppid='],capture_output=True,text=True,timeout=3)
    parents = {}
    for line in process.stdout.splitlines():
        fields = line.split()
        if len(fields) == 2:
            parents[int(fields[0])] = int(fields[1])
    ancestors = []
    pid = os.getpid()
    while pid > 1 and pid not in ancestors:
        ancestors.append(pid)
        pid = parents.get(pid, 0)
    candidates = set()
    env_socket = os.environ.get('TMUX','').split(',')[0]
    if env_socket:
        candidates.add(str(Path(env_socket).resolve()))
    for base in [Path('/tmp'),Path('/private/tmp')]:
        directory = base / f'tmux-{os.getuid()}'
        if directory.exists():
            for path in directory.iterdir():
                try:
                    if stat.S_ISSOCK(path.stat().st_mode):
                        candidates.add(str(path.resolve()))
                except OSError:
                    pass
    matches = []
    for candidate in candidates:
        output = tmux('list-panes','-a','-F','#{pane_id} #{pane_pid}',check=False,sock=candidate)
        for line in output.splitlines():
            fields = line.split()
            if len(fields) == 2 and fields[1].isdigit() and int(fields[1]) in ancestors:
                matches.append((ancestors.index(int(fields[1])),candidate,fields[0],fields[1]))
    if matches:
        _, sock, pane, pid = min(matches)
        return sock,pane,pid
    return None


def codex_transcript_meta(path):
    try:
        with Path(path).open() as source:
            header = json.loads(source.readline(1024*1024))
        return header.get('payload', {}) if header.get('type') == 'session_meta' else None
    except (OSError, ValueError, TypeError):
        return None


def codex_root_transcript(path, session_id):
    meta = codex_transcript_meta(path) if path else None
    source = meta.get('source') if meta else None
    return bool(meta and meta.get('id') == session_id and not (isinstance(source, dict) and 'subagent' in source))


def codex_child_hook(data):
    meta = codex_transcript_meta(data['transcript_path']) if data.get('transcript_path') else None
    source = meta.get('source') if meta else None
    return bool(meta and (meta.get('id') != data.get('session_id') or (isinstance(source, dict) and 'subagent' in source)))


def register_codex_session(con,sock,pane,pid,data):
    path = data.get('transcript_path') or ''
    if path and not codex_root_transcript(path, data['session_id']):
        path = ''
    if not path:
        database = Path.home()/'.codex/state_5.sqlite'
        if database.exists():
            try:
                with closing(sqlite3.connect(database.as_uri()+'?mode=ro',uri=True,timeout=1)) as source:
                    row = source.execute('SELECT rollout_path FROM threads WHERE id=?',(data['session_id'],)).fetchone()
                    if row and codex_root_transcript(row[0], data['session_id']): path = row[0]
            except sqlite3.Error:
                pass
    with con:
        con.execute("""INSERT INTO codex_sessions(socket,pane,pid,thread_id,rollout_path,origin)
            VALUES(?,?,?,?,?,'native') ON CONFLICT(socket,pane,pid) DO UPDATE SET
            cli_pid=CASE WHEN codex_sessions.thread_id=excluded.thread_id THEN codex_sessions.cli_pid ELSE 0 END,
            thread_id=excluded.thread_id,rollout_path=CASE
            WHEN excluded.rollout_path<>'' THEN excluded.rollout_path
            WHEN codex_sessions.thread_id=excluded.thread_id THEN codex_sessions.rollout_path
            ELSE '' END,origin='native' """,
            (sock,pane,pid,data['session_id'],path))


def event_command(agent, event):
    # Hooks must never approve, block or fail the agent's own work.
    try:
        data = json.load(sys.stdin)
        # Child transcripts share the parent's session_id but have their own id.
        # Their completion/tool hooks must never replace the visible parent's state.
        if agent == 'codex' and codex_child_hook(data):
            print('{}')
            return
        state = classify(agent, event, data)
        target = hook_target() if state else None
        if state and not target and agent == 'codex' and data.get('session_id'):
            with closing(db()) as con:
                mappings = con.execute('SELECT * FROM codex_sessions WHERE thread_id=?',(data['session_id'],)).fetchall()
                valid = []
                for mapping in mappings:
                    actual = tmux('display-message','-p','-t',mapping['pane'],'#{pane_id} #{pane_pid}',check=False,sock=mapping['socket'])
                    if actual == f"{mapping['pane']} {mapping['pid']}":
                        valid.append((mapping['socket'],mapping['pane'],mapping['pid']))
                if len(valid) == 1:
                    target = valid[0]
        if state and not target and os.environ.get('TMUX'):
            STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
            with (STATE / 'hook-errors.log').open('a') as f:
                f.write(f'{time.time():.0f} {agent}/{event}: no tmux pane in hook process ancestry; ignored stale environment\n')
        if target:
            sock,pane,pid = target
            with closing(db()) as con:
                if agent == 'codex' and data.get('session_id'):
                    register_codex_session(con,sock,pane,pid,data)
                # Reattaching/restarting must not discard an unread completion.
                existing = con.execute('SELECT * FROM panes WHERE socket=? AND pane=? AND pid=?', (sock,pane,pid)).fetchone()
                if event in ('SessionEnd','session_shutdown') and existing:
                    if existing['needs_review']:
                        # Keep an unread completed response after the agent exits.
                        state = None
                    if existing['state'] in ('RUNNING','WAIT'):
                        state = ('IDLE', 'Agent exited; review current work', True)
                if event not in ('SessionStart','session_start') or not existing:
                    if state:
                        record(con, sock, pane, pid, agent, *state,event_key=data.get('turn_id',''))
    except Exception as exc:
        STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (STATE / 'hook-errors.log').open('a') as f:
            f.write(f'{time.time():.0f} {agent}/{event}: {type(exc).__name__}: {exc}\n')
    # Codex Stop requires JSON output; {} has no decision effects.
    print('{}')


def live_panes():
    fmt = '#{pane_id}\t#{pane_pid}\t#{session_name}\t#{window_name}\t#{pane_current_path}\t#{?@tmux_attn_sidebar,1,0}\t#{window_id}'
    result = {}
    for line in tmux('list-panes', '-a', '-F', fmt, check=False).splitlines():
        parts = line.split('\t')
        if len(parts) == 7 and parts[5] != '1':
            result[(parts[0], parts[1])] = dict(pane=parts[0], pid=parts[1], session=parts[2], window=clean(parts[3]), path=parts[4], window_id=parts[6])
    return result


def items(con, sock, live, all_states=False, now=None):
    now = time.time() if now is None else now
    result, running = [], 0
    for row in con.execute('SELECT * FROM panes WHERE socket=?', (sock,)):
        meta = live.get((row['pane'], row['pid']))
        if not meta:
            continue
        item = dict(row) | meta
        running += row['state'] == 'RUNNING'
        pending = (row['state'] == 'WAIT' or bool(row['needs_review'])) and row['revision'] > row['acknowledged']
        snoozed = row['snoozed_revision'] == row['revision'] and row['snoozed_until'] > now
        # Allow immediate continuation hooks to settle before showing completion.
        stable = not row['needs_review'] or now - row['updated'] >= 2
        item['pending'] = pending and not snoozed and stable
        if item['pending'] or all_states:
            result.append(item)
    result.sort(key=lambda x: (0 if x['pending'] and x['state'] == 'WAIT' else 1 if x['pending'] else 2, x['updated'], x['pane']))
    return result, running


def acknowledge(con, item, seconds=0):
    with con:
        if seconds:
            con.execute('UPDATE panes SET snoozed_revision=?,snoozed_until=? WHERE socket=? AND pane=? AND pid=? AND revision=?',
                        (item['revision'], time.time()+seconds, item['socket'], item['pane'], item['pid'], item['revision']))
        else:
            con.execute('UPDATE panes SET acknowledged=MAX(acknowledged,?) WHERE socket=? AND pane=? AND pid=?',
                        (item['revision'], item['socket'], item['pane'], item['pid']))


def window_badge(rows):
    if any(x['state'] == 'WAIT' for x in rows):
        return '🙋'
    if any(x['state'] == 'IDLE' and x['pending'] for x in rows):
        return '👀'
    if any(x['state'] == 'RUNNING' for x in rows):
        return '🏃'
    return '💤' if rows else ''


def window_project_path(rows):
    badge = window_badge(rows)
    row = next((x for x in rows if window_badge([x]) == badge), None)
    if not row:
        return ''
    return row['path']


def load_project_aliases():
    path = Path.home()/'.config/tmux-attn/project-aliases.json'
    try:
        aliases = json.loads(path.read_text()).get('aliases', {})
        return {key: clean(value).strip() for key, value in aliases.items()
                if isinstance(key, str) and isinstance(value, str) and clean(value).strip()}
    except (OSError, ValueError, AttributeError):
        return {}


def project_labels(paths, aliases):
    labels = {}
    for path in sorted(set(paths)):
        name = clean(Path(path).name)
        labels[path] = aliases.get(path) or aliases.get(name) or (name if len(name) <= 12 else name[:11]+'…')
    duplicates = {label for label in labels.values() if list(labels.values()).count(label) > 1}
    for path, label in list(labels.items()):
        if label in duplicates:
            suffix = clean(Path(path).name).rsplit('-', 1)[-1]
            candidate = label[:max(1, 11-len(suffix))]+'-'+suffix
            # Prefer a readable suffix; use the full path when names are identical.
            if len(candidate) <= 12 and suffix not in label and candidate not in labels.values():
                labels[path] = candidate
            else:
                digest = hashlib.sha256(path.encode()).hexdigest()
                for length in range(4, 11):
                    candidate = label[:11-length]+'-'+digest[:length]
                    if candidate not in labels.values():
                        labels[path] = candidate
                        break
    return labels


def update_window_badges(rows, live=None):
    live = live_panes() if live is None else live
    groups = {}
    for row in rows:
        groups.setdefault(row['window_id'], []).append(row)
    paths = {window: window_project_path(group) for window, group in groups.items()}
    # Names are useful even before a fresh/resumed agent registers its lifecycle.
    # An untracked pane gets its cwd label, never a fabricated state badge.
    for meta in live.values():
        paths.setdefault(meta['window_id'], meta['path'])
    labels = project_labels([path for path in paths.values() if path], load_project_aliases())
    commands = []
    for line in tmux('list-windows', '-a', '-F', '#{window_id}\t#{@tmux_attn_badge}\t#{@tmux_attn_project}', check=False).splitlines():
        window, current, project = (line.split('\t') + ['', ''])[:3]
        group = groups.get(window, [])
        for key, previous, desired in [('@tmux_attn_badge', current, window_badge(group)), ('@tmux_attn_project', project, labels.get(paths.get(window), ''))]:
            if desired != previous:
                if commands:
                    commands.append(';')
                commands.extend(['set-option', '-w', '-t', window, key, desired])
    if commands:
        # One server command for changed windows; no process per window per refresh.
        tmux(*commands, check=False)


def jump(item, client=None):
    # Verify pane PID as well: an old row must never target a reused pane.
    pid = tmux('display-message', '-p', '-t', item['pane'], '#{pane_pid}', check=False)
    if pid != item['pid']:
        return
    tmux('select-pane', '-t', item['pane'])
    tmux('select-window', '-t', item['pane'])
    if client:
        tmux('switch-client', '-c', client, '-t', item['pane'])


def age(stamp):
    seconds = max(0, int(time.time()-stamp))
    return f'{seconds//3600}h' if seconds >= 3600 else f'{seconds//60}m' if seconds >= 60 else f'{seconds}s'


def grouped_rows(rows):
    return sorted(rows, key=lambda x: (('WAIT', 'IDLE', 'RUNNING').index(x['state']), not x['pending'], x['updated'], x['pane']))


def display_reason(item, reviewed):
    if item['state'] == 'IDLE':
        background = '（背景工作仍在運行）' if 'background tasks still running' in item['reason'] else ''
        if reviewed:
            return '已完成，已查看' + background
        if item['needs_review']:
            outcome = '已中斷' if 'Interrupted' in item['reason'] else '發生錯誤' if 'error' in item['reason'] else '已完成'
            return outcome + '，尚未查看' + background
        return '閒置，等待新任務'
    if item['state'] == 'RUNNING':
        return '背景工作仍在運行' if 'Background work' in item['reason'] else '正在工作'
    reason = item['reason']
    if 'Approval' in reason or 'approval' in reason:
        return '需要批准指令或操作 · ' + reason
    if 'question' in reason:
        return '回覆包含提問，推測需要回答'
    return '需要回答或下判斷 · ' + reason


def inbox_lines(rows):
    lines = []
    for state, title in [('WAIT', '🙋 等待回覆'), ('IDLE', '💤 閒置'), ('RUNNING', '🏃 工作進行中')]:
        group = [x for x in rows if x['state'] == state]
        lines.append((None, f'── {title} ({len(group)}) ──'))
        for item in group:
            identity = (item['pane'], item['pid'])
            reviewed = item['revision'] <= item['acknowledged'] and (state == 'WAIT' or item['needs_review'])
            label = window_badge([item])
            if reviewed:
                label += ' · 已查看'
            reason = display_reason(item, reviewed)
            lines.extend([(identity, f'{label} · {item["agent"]} · {age(item["updated"])}'),
                          (identity, f'{Path(item["path"]).name} / {item["window"]}'),
                          (identity, reason)])
    return lines


def draw(win, y, x, value, attr=0):
    h, w = win.getmaxyx()
    if 0 <= y < h and 0 <= x < w-1:
        try:
            win.addnstr(y, x, clean(value, 2000), w-x-1, attr)
        except curses.error:
            pass


def sidebar(win, popup=False, client=None):
    curses.curs_set(0)
    win.timeout(1000)
    con, sock = db(), socket()
    selected = None
    show_all = True
    preview = False
    message = ''
    message_until = 0
    while True:
        live = live_panes()
        rows, running = items(con, sock, live, show_all)
        rows = grouped_rows(rows)
        registered, _ = items(con, sock, live, True)
        idle = sum(x['state'] == 'IDLE' for x in registered)
        ids = [(x['pane'], x['pid']) for x in rows]
        if selected not in ids:
            selected = ids[0] if ids else None
        index = ids.index(selected) if selected else 0
        h, w = win.getmaxyx()
        win.erase()
        draw(win, 0, 1, '待處理清單', curses.A_BOLD)
        waiting = sum(x['state'] == 'WAIT' for x in registered)
        review = sum(x['pending'] and x['state'] == 'IDLE' for x in rows)
        draw(win, 1, 1, f'🙋 {waiting} · 👀 {review} · 🏃 {running} · 💤 {idle-review}', curses.A_BOLD)
        draw(win, 2, 1, '全部 agent · 👀 表示尚未查看' if show_all else '僅顯示待處理項目')
        lines = inbox_lines(rows)
        slots = max(1, (h//2 if preview else h-4)-4)
        selected_line = next((i for i,(identity,_) in enumerate(lines) if identity == selected), 0)
        start = max(0, selected_line+3-slots)
        for offset, (identity, text) in enumerate(lines[start:start+slots]):
            attr = curses.A_BOLD if identity is None else curses.A_REVERSE if identity == selected else 0
            draw(win, 4+offset, 1, text, attr)
        if not rows:
            draw(win, 5, 1, 'No pending items', curses.A_BOLD)
            draw(win, 7, 1, 'New events will appear here.')
            draw(win, 9, 1, 'a: inspect registered agents')
            if not registered:
                draw(win, 11, 1, 'Reload agents to enable tracking.')
        if preview and rows:
            text = tmux('capture-pane', '-p', '-J', '-t', rows[index]['pane'], '-S', '-20', check=False)
            y = h//2
            draw(win, y, 1, '── Selected pane preview ──', curses.A_BOLD)
            for line_no, line in enumerate(text.splitlines()[-max(1,h-y-6):]):
                draw(win, y+1+line_no, 1, line)
        draw(win, h-4, 1, message if time.monotonic() < message_until else f'{len(rows)} shown · {len(live)-len(registered)} panes without events')
        draw(win, h-3, 1, 'j/k 選擇  Enter 跳轉  v 預覽')
        draw(win, h-2, 1, 'd 已查看  1 暫緩 5 分鐘  a 全部／待處理')
        draw(win, h-1, 1, 'Esc/q 關閉' if popup else 'Esc 返回工作')
        win.refresh()
        key = win.getch()
        if key in (ord('j'), curses.KEY_DOWN) and rows:
            selected = ids[min(index+1,len(ids)-1)]
        elif key in (ord('k'), curses.KEY_UP) and rows:
            selected = ids[max(0,index-1)]
        elif key == ord('a'):
            show_all = not show_all
        elif key == ord('v'):
            preview = not preview
        elif key in (ord('d'), ord('1')) and rows:
            item = rows[index]
            if item['state'] == 'RUNNING':
                message = '工作中，尚無回覆可查看'
            elif item['revision'] <= item['acknowledged']:
                message = '已經標記為已查看'
            elif item['state'] == 'IDLE' and not item['needs_review']:
                message = '尚無回覆可查看'
            else:
                acknowledge(con, item, 300 if key == ord('1') else 0)
                message = ('暫緩 5 分鐘' if key == ord('1') else '已查看') + ' · ' + Path(item['path']).name
            message_until = time.monotonic() + 4
        elif key in (10,13,curses.KEY_ENTER) and rows:
            try:
                target_client = client or tmux('display-message', '-p', '-t', os.environ['TMUX_PANE'], '#{client_name}')
                jump(rows[index], target_client or None)
                if popup:
                    con.close()
                    return
            except RuntimeError as exc:
                message = clean(exc)
                message_until = time.monotonic() + 4
        elif key == 27 or (popup and key == ord('q')):
            if popup:
                con.close()
                return
            tmux('last-pane', check=False)


def prune_sidebars():
    # A sidebar must not keep an otherwise finished window alive forever.
    for window in tmux('list-windows','-a','-F','#{window_id}',check=False).splitlines():
        lines = tmux('list-panes','-t',window,'-F','#{pane_id} #{?@tmux_attn_sidebar,1,0}',check=False).splitlines()
        if lines and all(line.endswith(' 1') for line in lines):
            for line in lines:
                tmux('kill-pane','-t',line.split()[0],check=False)


def ensure_sidebars(target=None):
    windows = [target] if target else tmux('list-windows', '-a', '-F', '#{window_id}').splitlines()
    for window in windows:
        # Serialize creators using tmux's server-local wait channel.
        lock = 'tmux-attn-' + window
        tmux('wait-for', '-L', lock)
        try:
            panes = tmux('list-panes', '-t', window, '-F', '#{pane_id}\t#{@tmux_attn_sidebar}\t#{pane_active}', check=False).splitlines()
            if not panes:
                # Windows may close while an asynchronous creation hook is queued.
                continue
            if any('\t1\t' in p for p in panes):
                continue
            active = next((p.split('\t')[0] for p in panes if p.endswith('\t1')), panes[0].split('\t')[0])
            cmd = shlex.join([sys.executable, str(ROOT/'attention.py'), 'sidebar'])
            pane = tmux('split-window', '-e', 'TMUX_ATTN_STATE_DIR='+str(STATE), '-d', '-f', '-h', '-b', '-l', '38', '-t', active, '-P', '-F', '#{pane_id}', cmd, check=False)
            if not pane:
                continue
            tmux('set-option', '-p', '-t', pane, '@tmux_attn_sidebar', '1', check=False)
            tmux('select-pane', '-t', pane, '-T', 'attention-inbox', check=False)
        finally:
            tmux('wait-for', '-U', lock)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['event','sidebar','ensure','status','dump','next','focus','prune','observe','popup','inbox'])
    parser.add_argument('args', nargs='*')
    args = parser.parse_args()
    if args.command == 'event':
        event_command(*args.args)
    elif args.command == 'inbox':
        curses.wrapper(lambda win: sidebar(win,popup=True,client=args.args[0] if args.args else None))
    elif args.command == 'popup':
        client = args.args[0] if args.args else tmux('display-message','-p','#{client_name}')
        command = shlex.join([sys.executable,str(ROOT/'attention.py'),'inbox',client])
        tmux('display-popup','-E','-w','90%','-h','85%','-T','待處理清單','-t',client,command)
    elif args.command == 'sidebar':
        curses.wrapper(sidebar)
    elif args.command == 'ensure':
        ensure_sidebars(args.args[0] if args.args else None)
    elif args.command == 'observe':
        from codex_observer import observe
        observe()
    elif args.command == 'prune':
        prune_sidebars()
    elif args.command == 'focus':
        window = args.args[0] if args.args else tmux('display-message','-p','#{window_id}')
        ensure_sidebars(window)
        for line in tmux('list-panes','-t',window,'-F','#{pane_id}\t#{@tmux_attn_sidebar}').splitlines():
            if line.endswith('\t1'):
                tmux('select-pane','-t',line.split('\t')[0])
    else:
        with closing(db()) as con:
            live = live_panes()
            rows, running = items(con, socket(), live, args.command == 'dump')
            if args.command == 'status':
                registered, _ = items(con, socket(), live, True)
                update_window_badges(registered, live)
                review = sum(x['state']=='IDLE' for x in rows)
                idle = sum(x['state']=='IDLE' for x in registered) - review
                print(f'🙋 {sum(x["state"]=="WAIT" for x in registered)} · 👀 {review} · 🏃 {running} · 💤 {idle}')
            elif args.command == 'dump':
                print(json.dumps(rows, ensure_ascii=False, indent=2))
            elif rows:
                jump(rows[0], args.args[0] if args.args else None)


if __name__ == '__main__':
    main()
