#!/usr/bin/env python3
"""Pane-scoped, event-driven tmux inbox. Python standard library only."""
from __future__ import annotations
import argparse
import curses
import fcntl
import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing, nullcontext
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
import os
from pathlib import Path
import shlex
import socket as socket_mod
import sqlite3
import stat
import subprocess
import sys
import time
from urllib.parse import urlparse
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
STATE = Path(os.environ.get('TMUX_ATTN_STATE_DIR', Path.home() / '.local/state/tmux-attn'))
VIEW_HOOKS = ('after-select-window', 'after-select-pane', 'client-session-changed')
VIEW_HOOK_INDEX = 91
VIEW_REVISION_OPTION = '@tmux_attn_view_revision'


def tmux(*args, check=True, sock=None):
    command = ['tmux'] + (['-S', sock] if sock else []) + list(args)
    p = subprocess.run(command, capture_output=True, text=True, timeout=None if args and args[0]=='display-popup' else 4)
    if check and p.returncode:
        raise RuntimeError(p.stderr.strip())
    return p.stdout.strip()


def socket():
    return os.environ.get('TMUX', '').split(',')[0] or tmux('display-message', '-p', '#{socket_path}')


def resolve_socket():
    """Socket for long-lived commands that may run outside a tmux client."""
    for key in ('TMUX', 'TMUX_ATTN_SOCKET'):
        value = os.environ.get(key, '')
        if key == 'TMUX':
            value = value.split(',')[0]
        if value:
            return value
    listed = tmux('list-sessions', '-F', '#{socket_path}', check=False)
    if listed:
        return listed.splitlines()[0]
    return socket()


def observer_lock_path(sock):
    return STATE / ('codex-observer-' + hashlib.sha256(sock.encode()).hexdigest()[:12] + '.lock')


def log_observer_error(exc):
    """Persist diagnostics without repeating the same failure on each status tick."""
    try:
        STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
        message = f'{type(exc).__name__}: {exc}'.replace('\n', ' ')
        with (STATE / 'observer-errors.log').open('a+b') as log:
            fcntl.flock(log, fcntl.LOCK_EX)
            log.seek(0, os.SEEK_END)
            size = log.tell()
            offset = max(0, size - 4096)
            if offset:
                log.seek(offset - 1)
                if log.read(1) != b'\n':
                    # A partial timestamp/message is not a diagnostic record.
                    log.readline()
            else:
                log.seek(0)
            lines = log.read().decode('utf-8', errors='replace').splitlines()
            now = time.time()
            for line in reversed(lines):
                stamp, _, previous = line.partition(' ')
                if previous == message and now - float(stamp) < 60:
                    return
            log.write(f'{now:.0f} {message}\n'.encode('utf-8'))
    except (OSError, ValueError):
        pass


def ensure_observer(sock):
    """Probe the kernel lock; PID text is diagnostic and may be stale."""
    try:
        STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
        with observer_lock_path(sock).open('a') as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
        # The observer acquires the same lock itself, so concurrent status calls
        # can launch contenders but only one child can become the observer.
        env = os.environ.copy()
        env.pop('TMUX', None)
        env['TMUX_ATTN_SOCKET'] = sock
        env['TMUX_ATTN_STATE_DIR'] = str(STATE)
        subprocess.Popen(
            [sys.executable, str(ROOT / 'attention.py'), 'observe'], env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True,
        )
    except Exception as exc:
        log_observer_error(exc)


def enable_wal(con, timeout=3):
    """Retry WAL activation contention within one total busy-wait budget."""
    deadline = time.monotonic() + timeout
    changed_timeout = False
    try:
        while True:
            try:
                con.execute('PRAGMA journal_mode=WAL')
                return
            except sqlite3.OperationalError as exc:
                code = getattr(exc, 'sqlite_errorcode', 0) & 0xff
                remaining = deadline - time.monotonic()
                if code not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) or remaining <= 0:
                    raise
                # SQLite may skip its busy handler during a journal transition.
                time.sleep(min(0.025, remaining))
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise
                con.execute(f'PRAGMA busy_timeout={int(remaining * 1000)}')
                changed_timeout = True
    finally:
        if changed_timeout:
            con.execute('PRAGMA busy_timeout=3000')


def db():
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    con = sqlite3.connect(STATE / 'inbox.sqlite3', timeout=3)
    try:
        con.row_factory = sqlite3.Row
        enable_wal(con)
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
        if 'source' not in {r[1] for r in con.execute('PRAGMA table_info(panes)')}:
            try:
                con.execute("ALTER TABLE panes ADD COLUMN source TEXT DEFAULT 'hook'")
            except sqlite3.OperationalError as exc:
                if 'duplicate column' not in str(exc):
                    raise
        for column, decl in [
            ('transcript_path', "TEXT DEFAULT ''"),
            ('preview_agent', "TEXT DEFAULT ''"),
            ('preview_users', "TEXT DEFAULT '[]'"),
            ('preview_mtime', 'REAL DEFAULT 0'),
            ('preview_revision', 'INTEGER DEFAULT 0'),
        ]:
            if column not in {r[1] for r in con.execute('PRAGMA table_info(panes)')}:
                try:
                    con.execute(f'ALTER TABLE panes ADD COLUMN {column} {decl}')
                except sqlite3.OperationalError as exc:
                    if 'duplicate column' not in str(exc):
                        raise
        con.execute('''CREATE TABLE IF NOT EXISTS codex_sessions (
            socket TEXT, pane TEXT, pid TEXT, cli_pid INTEGER DEFAULT 0,
            thread_id TEXT, rollout_path TEXT, origin TEXT,
            PRIMARY KEY(socket,pane,pid))''')
        con.execute('''CREATE TABLE IF NOT EXISTS cycle_cursors (
            socket TEXT, client TEXT, category TEXT, pane TEXT, pid TEXT,
            PRIMARY KEY(socket,client,category))''')
        return con
    except Exception:
        con.close()
        raise


def clean(value, limit=240):
    return ''.join(c for c in str(value) if c.isprintable())[:limit]


PREVIEW_CHARS = 160
PREVIEW_CHARS_FULL = 4000
PREVIEW_USERS = 3


def clip_preview(text, limit=PREVIEW_CHARS):
    """Trim preview text while keeping markdown structure (newlines / indents)."""
    text = str(text or '').replace('\r\n', '\n').replace('\r', '\n')
    text = ''.join(c for c in text if c in '\n\t' or c.isprintable())
    text = re.sub(r'\n{3,}', '\n\n', text).strip('\n')
    if len(text) <= limit:
        return text
    cut = text[:max(0, limit - 1)]
    nl = cut.rfind('\n')
    if nl >= max(24, limit // 3):
        cut = cut[:nl]
    return cut.rstrip() + '…'


def user_text_is_noise(text):
    t = (text or '').strip()
    if not t:
        return True
    head = t[:160].lower()
    markers = (
        '<environment_context>', '<local-command', '<command-', '<permissions',
        '<system-reminder>', '<task-notification>', 'local-command-caveat',
        'caveat: the messages below',
        'filesystem boundary:', 'this session is being continued from a previous conversation',
        'the messages below were generated by the user while running local commands',
        '# agents.md instructions for', '<instructions>',
    )
    return any(head.startswith(m) or m in head for m in markers)


def content_to_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                if part.get('type') in ('text', 'input_text', 'output_text'):
                    parts.append(part.get('text') or '')
                elif part.get('text'):
                    parts.append(str(part.get('text')))
        return '\n'.join(p for p in parts if p)
    return ''


def hook_user_text(data):
    if not isinstance(data, dict):
        return ''
    for key in ('prompt', 'user_prompt', 'last_user_message', 'message', 'text', 'input', 'query'):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, dict):
            text = content_to_text(value.get('content'))
            if text.strip():
                return text
    return ''


def claude_project_dir(cwd):
    if not cwd:
        return None
    return Path.home() / '.claude' / 'projects' / str(cwd).replace('/', '-')


def latest_claude_transcript(cwd):
    directory = claude_project_dir(cwd)
    if not directory or not directory.is_dir():
        return ''
    files = [p for p in directory.glob('*.jsonl') if p.is_file()]
    if not files:
        return ''
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return str(files[0])


def cursor_project_dir(cwd):
    if not cwd:
        return None
    slug = str(Path(cwd)).lstrip('/').replace('/', '-')
    return Path.home() / '.cursor' / 'projects' / slug / 'agent-transcripts'


def latest_cursor_transcript(cwd):
    directory = cursor_project_dir(cwd)
    if not directory or not directory.is_dir():
        return ''
    files = [p for p in directory.rglob('*.jsonl') if p.is_file()]
    if not files:
        return ''
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return str(files[0])


TRANSCRIPT_TAIL_BYTES = 262144


def _iter_transcript_tail_lines(path, tail_bytes=TRANSCRIPT_TAIL_BYTES):
    """Yield lines from the tail of a large transcript starting after a newline boundary."""
    path = Path(path)
    size = path.stat().st_size
    if size <= tail_bytes:
        with path.open(errors='replace') as source:
            yield from source
        return
    with path.open('rb') as raw:
        raw.seek(max(0, size - tail_bytes))
        chunk = raw.read()
    nl = chunk.find(b'\n')
    if nl != -1:
        chunk = chunk[nl + 1:]
    for line in chunk.decode('utf-8', errors='replace').split('\n'):
        if line:
            yield line


def _parse_transcript_lines(lines, parser):
    return parser(lines)


def _parse_with_tail_fallback(path, line_parser, tail_bytes=TRANSCRIPT_TAIL_BYTES):
    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError:
        return [], ''
    if size > tail_bytes:
        users, agent = line_parser(_iter_transcript_tail_lines(path, tail_bytes))
        if len(users) >= PREVIEW_USERS:
            return users, agent
    with path.open(errors='replace') as source:
        return line_parser(source)


def _parse_claude_lines(lines):
    users, agent = [], ''
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        kind = row.get('type')
        if kind == 'user' and not row.get('isMeta'):
            message = row.get('message') or {}
            text = content_to_text(message.get('content') if isinstance(message, dict) else message)
            if not user_text_is_noise(text):
                users.append(text.strip())
                agent = ''
        elif kind == 'assistant':
            message = row.get('message') or {}
            text = content_to_text(message.get('content') if isinstance(message, dict) else message).strip()
            if text:
                agent = text
    return users, agent


def parse_claude_style_transcript(path):
    return _parse_with_tail_fallback(path, _parse_claude_lines)


def _parse_codex_lines(lines):
    users, agent = [], ''
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        payload = row.get('payload') if isinstance(row.get('payload'), dict) else {}
        if row.get('type') == 'event_msg':
            if payload.get('type') == 'user_message':
                text = str(payload.get('message') or '').strip()
                if not user_text_is_noise(text):
                    users.append(text)
                    agent = ''
            elif payload.get('type') == 'agent_message':
                text = str(payload.get('message') or '').strip()
                if text:
                    agent = text
        elif row.get('type') == 'response_item' and payload.get('type') == 'message':
            role = payload.get('role')
            text = content_to_text(payload.get('content')).strip()
            if not text:
                continue
            if role == 'user' and not user_text_is_noise(text):
                users.append(text)
                agent = ''
            elif role == 'assistant':
                agent = text
    return users, agent


def parse_codex_transcript(path):
    return _parse_with_tail_fallback(path, _parse_codex_lines)


def clean_cursor_user_text(text):
    text = str(text or '').strip()
    if not text:
        return ''
    # IDE agent wraps prompts in <user_query>; prefer the inner task text.
    match = re.search(r'<user_query>\s*(.*?)\s*</user_query>', text, re.S | re.I)
    if match:
        text = match.group(1).strip()
    text = re.sub(r'<timestamp>[\s\S]*?</timestamp>', '', text, flags=re.I).strip()
    return text


def _parse_cursor_lines(lines):
    users, agent = [], ''
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        role = row.get('role') or ''
        message = row.get('message') if isinstance(row.get('message'), dict) else {}
        text = content_to_text(message.get('content')).strip() if message else ''
        if not text and isinstance(row.get('message'), str):
            text = row.get('message').strip()
        if not text:
            continue
        if role == 'user':
            text = clean_cursor_user_text(text)
            if text and not user_text_is_noise(text):
                users.append(text)
                agent = ''
        elif role == 'assistant':
            agent = text
    return users, agent


def parse_cursor_transcript(path):
    """Cursor IDE/agent-transcripts jsonl: {role, message:{content:[...]}}."""
    return _parse_with_tail_fallback(path, _parse_cursor_lines)


def conversation_preview(path, agent='', limit=PREVIEW_CHARS_FULL):
    """Last user intents + latest agent text from a transcript. Empty if unreadable."""
    path = Path(path) if path else None
    if not path or not path.is_file():
        return {'users': [], 'agent': ''}
    try:
        name = path.name
        parts = {p.lower() for p in path.parts}
        if 'rollout' in name or agent == 'codex':
            users, agent_text = parse_codex_transcript(path)
        elif agent == 'cursor' or 'agent-transcripts' in parts:
            users, agent_text = parse_cursor_transcript(path)
        else:
            users, agent_text = parse_claude_style_transcript(path)
    except OSError:
        return {'users': [], 'agent': ''}
    users = [clip_preview(text, limit) for text in users[-PREVIEW_USERS:]]
    return {'users': users, 'agent': clip_preview(agent_text, limit) if agent_text else ''}


def remember_preview(con, sock, pane, pid, *, user=None, agent=None, transcript_path=None, expected_revision=None):
    row = con.execute('SELECT * FROM panes WHERE socket=? AND pane=? AND pid=?', (sock, pane, pid)).fetchone()
    if not row or (expected_revision is not None and row['revision'] != expected_revision):
        return
    users = []
    try:
        users = list(json.loads(row['preview_users'] if 'preview_users' in row.keys() else '[]') or [])
    except (TypeError, ValueError):
        users = []
    if user and not user_text_is_noise(user):
        clipped = clip_preview(user, PREVIEW_CHARS_FULL)
        if not users or users[-1] != clipped:
            users = (users + [clipped])[-PREVIEW_USERS:]
    agent_text = (
        clip_preview(agent, PREVIEW_CHARS_FULL) if agent and str(agent).strip()
        else (row['preview_agent'] if 'preview_agent' in row.keys() else '')
    )
    path = clean(transcript_path or '', 500) if transcript_path else (row['transcript_path'] if 'transcript_path' in row.keys() else '')
    baseline_mtime = row['preview_mtime']
    if (user or agent) and path:
        try:
            baseline_mtime = Path(path).stat().st_mtime
        except OSError:
            pass
    with con:
        con.execute(
            '''UPDATE panes SET preview_users=?, preview_agent=?, preview_revision=?, preview_mtime=?, transcript_path=CASE
                WHEN ?<>'' THEN ? ELSE transcript_path END
               WHERE socket=? AND pane=? AND pid=? AND revision=?''',
            (json.dumps(users, ensure_ascii=False), agent_text or '',
             row['revision'] if agent and str(agent).strip() else row['preview_revision'],
             baseline_mtime, path, path, sock, pane, pid, row['revision']),
        )


def resolve_transcript_path(con, row, meta):
    path = ''
    if 'transcript_path' in row.keys():
        path = row['transcript_path'] or ''
    if path and Path(path).is_file():
        return path
    mapping = con.execute(
        'SELECT rollout_path FROM codex_sessions WHERE socket=? AND pane=? AND pid=?',
        (row['socket'], row['pane'], row['pid']),
    ).fetchone()
    if mapping and mapping['rollout_path'] and Path(mapping['rollout_path']).is_file():
        return mapping['rollout_path']
    cwd = (meta or {}).get('path') or ''
    agent = row.get('agent') or ''
    if agent == 'cursor':
        found = latest_cursor_transcript(cwd)
        if found:
            return found
    if agent in ('claude', 'cursor') or cwd:
        found = latest_claude_transcript(cwd)
        if found:
            return found
    return path if path and Path(path).is_file() else ''


def completed_transcript_matches_revision(path, row, displayed_agent):
    """Recognize an already cached final turn from its completion marker.

    Callers must separately verify transcript ownership. Text or file changes
    are unnecessary when the root turn marker identifies the stored completion.
    """
    from datetime import datetime

    agent = row.get('agent')
    if agent not in ('codex', 'claude') or not displayed_agent.strip():
        return False
    reason = str(row.get('reason') or '').lower()
    if any(outcome in reason for outcome in ('interrupted', 'error', 'exited')):
        return False
    if agent == 'codex' and not row.get('event_key'):
        return False
    final_text, final_time, matched = '', None, False

    def timestamp(entry):
        try:
            value = datetime.fromisoformat(entry['timestamp'].replace('Z', '+00:00'))
            return value.timestamp() if value.tzinfo is not None else None
        except (KeyError, ValueError, TypeError, AttributeError):
            return None

    try:
        with Path(path).open(errors='replace') as source:
            for line in source:
                if not line.endswith('\n'):
                    return False
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(entry, dict) or entry.get('isSidechain'):
                    continue
                kind = entry.get('type')
                if agent == 'codex':
                    payload = entry.get('payload')
                    if not isinstance(payload, dict):
                        continue
                    if kind == 'session_meta':
                        origin = payload.get('source')
                        if isinstance(origin, dict) and 'subagent' in origin:
                            return False
                    elif kind == 'turn_context':
                        matched = False
                    elif kind == 'event_msg':
                        event = payload.get('type')
                        if event in ('task_started', 'turn_aborted'):
                            final_text, matched = '', False
                        elif event == 'user_message':
                            if not user_text_is_noise(str(payload.get('message') or '')):
                                final_text, matched = '', False
                        elif event == 'agent_message':
                            final_text, matched = str(payload.get('message') or '').strip(), False
                        elif event == 'task_complete':
                            last_message = str(payload.get('last_agent_message') or '').strip()
                            matched = (payload.get('turn_id') == row['event_key'] and bool(final_text)
                                       and (not last_message or clip_preview(last_message, PREVIEW_CHARS_FULL)
                                            == clip_preview(final_text, PREVIEW_CHARS_FULL)))
                    elif kind == 'response_item' and payload.get('type') == 'message':
                        text = content_to_text(payload.get('content')).strip()
                        if payload.get('role') == 'user' and not user_text_is_noise(text):
                            final_text, matched = '', False
                        elif payload.get('role') == 'assistant' and text:
                            final_text, matched = text, False
                else:
                    message = entry.get('message')
                    message = message if isinstance(message, dict) else {}
                    text = content_to_text(message.get('content')).strip()
                    if kind == 'user' and not entry.get('isMeta') and not user_text_is_noise(text):
                        final_text, final_time, matched = '', None, False
                    elif kind == 'assistant' and message.get('content'):
                        final_text = text if message.get('stop_reason') == 'end_turn' else ''
                        final_time, matched = timestamp(entry), False
                    elif kind == 'system' and entry.get('subtype') == 'stop_hook_summary':
                        stopped = timestamp(entry)
                        matched = (bool(final_text) and final_time is not None and stopped is not None
                                   and stopped >= final_time and not entry.get('preventedContinuation')
                                   and 0 <= stopped - float(row['updated']) <= 2)
    except (OSError, TypeError, ValueError):
        return False
    return matched and clip_preview(final_text, PREVIEW_CHARS_FULL) == displayed_agent


def pane_conversation_preview(con, row, meta=None, limit=PREVIEW_CHARS, snapshot=False):
    """Cached preview, refreshed from transcript when the file changes.

    Store a long cache; callers choose card (short) vs sheet (full) clip length.
    """
    users = []
    try:
        users = list(json.loads(row['preview_users'] if 'preview_users' in row.keys() else '[]') or [])
    except (TypeError, ValueError):
        users = []
    agent_text = (row['preview_agent'] if 'preview_agent' in row.keys() else '') or ''
    preview_revision = row['preview_revision'] if 'preview_revision' in row.keys() else 0
    cached_mtime = float(row['preview_mtime'] if 'preview_mtime' in row.keys() else 0) or 0
    path = resolve_transcript_path(con, row, meta)
    # A hook's current result is authoritative; a lagging transcript must not replace it.
    if path and not (row['state'] == 'IDLE' and row['needs_review']
                     and preview_revision == row['revision'] and agent_text):
        try:
            mtime = Path(path).stat().st_mtime
        except OSError:
            mtime = 0
        # Sheet asks for a long limit; old installs may also have short clipped cache.
        force_full = limit > PREVIEW_CHARS
        stale_clip = any(
            str(text).endswith('…') and len(str(text)) <= PREVIEW_CHARS
            for text in list(users) + ([agent_text] if agent_text else [])
        )
        if mtime and (force_full or stale_clip or mtime != cached_mtime or not (users or agent_text)):
            parsed = conversation_preview(path, row.get('agent') or '', limit=PREVIEW_CHARS_FULL)
            if parsed['users'] or parsed['agent']:
                # Only forward progress in an identified transcript may confirm a turn.
                # Cwd-based discovery remains useful for display, but isn't proof of ownership.
                mapping = con.execute('SELECT rollout_path FROM codex_sessions WHERE socket=? AND pane=? AND pid=?',
                                      (row['socket'], row['pane'], row['pid'])).fetchone()
                bound_path = path == row.get('transcript_path') or bool(mapping and path == mapping['rollout_path'])
                forward_users = not users or parsed['users'] == users or any(
                    users[-overlap:] == parsed['users'][:overlap] and len(parsed['users']) > overlap
                    for overlap in range(1, len(users) + 1)
                )
                advanced = parsed['agent'] != agent_text or parsed['users'] != users
                verified = (row['state'] == 'IDLE' and bool(row['needs_review']) and bool(parsed['agent'])
                            and bound_path and ((mtime > cached_mtime and forward_users and advanced)
                                                or (forward_users and completed_transcript_matches_revision(path, row, parsed['agent']))))
                preview_revision = row['revision'] if verified else 0
                users, agent_text = parsed['users'], parsed['agent']
                try:
                    con.execute('PRAGMA busy_timeout = 50')
                    with con:
                        updated = con.execute(
                            '''UPDATE panes SET preview_users=?, preview_agent=?, preview_mtime=?, preview_revision=?
                               WHERE socket=? AND pane=? AND pid=? AND revision=?
                               AND preview_revision=?''',
                            (json.dumps(users, ensure_ascii=False), agent_text, mtime, preview_revision,
                             row['socket'], row['pane'], row['pid'], row['revision'], row['preview_revision']),
                        )
                        if not updated.rowcount:
                            preview_revision = 0
                except sqlite3.OperationalError:
                    # Do not fail or stall read-only status polls if a hook holds the write lock.
                    preview_revision = 0
                finally:
                    try:
                        con.execute('PRAGMA busy_timeout = 3000')
                    except sqlite3.OperationalError:
                        pass
    preview = {
        'users': [clip_preview(text, limit) for text in users[-PREVIEW_USERS:] if str(text).strip()],
        'agent': clip_preview(agent_text, limit) if agent_text else '',
    }
    return (preview, preview_revision) if snapshot else preview


def local_preview_for_pane(con, pane, pid, limit=PREVIEW_CHARS_FULL, snapshot=False):
    """Resolve a live tracked pane and return a (longer) conversation preview."""
    sock = resolve_socket()
    live = live_panes(sock)
    meta = live.get((pane, pid))
    if not meta:
        return None, 'pane is not live'
    row = con.execute(
        'SELECT * FROM panes WHERE socket=? AND pane=? AND pid=?',
        (sock, pane, pid),
    ).fetchone()
    if not row:
        return None, 'pane is not tracked'
    item = dict(row) | meta
    if snapshot:
        preview, preview_revision = pane_conversation_preview(con, item, meta, limit=limit, snapshot=True)
    else:
        preview = pane_conversation_preview(con, item, meta, limit=limit)
    if not snapshot:
        return preview, None
    current = con.execute('SELECT revision FROM panes WHERE socket=? AND pane=? AND pid=?',
                          (sock, pane, pid)).fetchone()
    if not current or current['revision'] != row['revision']:
        return None, 'content changed; refresh preview'
    return {'preview': preview, 'revision': row['revision'],
            'viewable': row['state'] == 'IDLE' and bool(row['needs_review'])
            and preview_revision == row['revision'] and bool(preview['agent'].strip())}, None


def record(con, sock, pane, pid, agent, state, reason, needs_review=False, event_time=None, event_key="", replace_identity=False, source='hook'):
    reason = clean(reason)
    source = clean(source or 'hook', 32) or 'hook'
    notify = False
    with con:
        old = con.execute('SELECT * FROM panes WHERE socket=? AND pane=? AND pid=?', (sock, pane, pid)).fetchone()
        stamp = event_time if event_time is not None else time.time()
        if old and not replace_identity and event_time is not None and max(old['updated'],old['last_event_time']) > event_time:
            return
        same_state = (old and not replace_identity
                      and not (needs_review and event_key and event_key != old['event_key'])
                      and (old['state'], old['reason'], old['agent'], bool(old['needs_review'])) == (state, reason, agent, needs_review))
        if same_state:
            old_source = old['source'] if 'source' in old.keys() else 'hook'
            # Source-only flips (hook→observer) must not bump revision or re-notify.
            if old_source != source:
                con.execute("UPDATE panes SET source=?,last_event_time=?,event_key=CASE WHEN ?<>'' THEN ? ELSE event_key END WHERE socket=? AND pane=? AND pid=?",
                            (source,stamp,event_key,event_key,sock,pane,pid))
            else:
                con.execute("UPDATE panes SET last_event_time=?,event_key=CASE WHEN ?<>'' THEN ? ELSE event_key END WHERE socket=? AND pane=? AND pid=?",
                            (stamp,event_key,event_key,sock,pane,pid))
            return old['revision']
        became_wait = state == 'WAIT' and (not old or old['state'] != 'WAIT' or old['revision'] <= old['acknowledged'])
        con.execute('''INSERT INTO panes(socket,pane,pid,agent,state,reason,updated,revision,needs_review,event_key,last_event_time,source)
            VALUES(?,?,?,?,?,?,?,1,?,?,?,?) ON CONFLICT(socket,pane,pid) DO UPDATE SET
            agent=excluded.agent,state=excluded.state,reason=excluded.reason,
            updated=excluded.updated,revision=panes.revision+1,needs_review=excluded.needs_review,event_key=excluded.event_key,last_event_time=excluded.last_event_time,source=excluded.source''',
            (sock, pane, pid, agent, state, reason,stamp,int(needs_review),event_key,stamp,source))
        notify = became_wait
        revision = con.execute('SELECT revision FROM panes WHERE socket=? AND pane=? AND pid=?',
                               (sock, pane, pid)).fetchone()['revision']
    if notify:
        maybe_notify_wait(agent, reason, pane)
    publish_view_revision(sock, pane, pid, revision if state == 'IDLE' and needs_review else 0)
    return revision


def classify(agent, event, data):
    if agent == 'cursor':
        event = {'sessionStart':'SessionStart', 'sessionEnd':'SessionEnd',
                 'postToolUse':'PostToolUse', 'beforeSubmitPrompt':'UserPromptSubmit', 'stop':'Stop'}.get(event,event)
        if event == 'Stop' and data.get('status') == 'aborted':
            return 'IDLE', 'Interrupted; review current work', True
        if event == 'Stop' and data.get('status') == 'error':
            return 'IDLE', 'Agent stopped with an error; review needed', True
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
        # Soft hint only: trailing ? is not proof of WAIT (Fleet-style). Explicit
        # Permission / AskUserQuestion / ui_prompt events remain hard WAIT.
        if last.endswith(('?', '？')):
            return 'IDLE', 'Response ends with a question; review suggested', True
        if agent == 'claude' and any(t.get('status') == 'running' for t in data.get('background_tasks', [])):
            return 'IDLE', 'Response finished; review needed; background tasks still running', True
        return 'IDLE', 'Response finished; review needed', True
    return None


def load_notify_config():
    path = Path(os.environ.get('TMUX_ATTN_NOTIFY', Path.home() / '.config/tmux-attn/notify.json'))
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def maybe_notify_wait(agent, reason, pane):
    """Optional ntfy push when a pane newly enters WAIT. No auth; Tailscale/LAN only."""
    cfg = load_notify_config()
    if not cfg.get('enabled'):
        return
    base = clean(cfg.get('server') or 'https://ntfy.sh').rstrip('/')
    topic = clean(cfg.get('topic') or '')
    if not topic:
        return
    title = clean(cfg.get('title') or 'tmux-attn')
    host = local_host_name()
    body = f'{host} {agent} {pane}: {reason}'
    try:
        request = Request(
            f'{base}/{topic}',
            data=body.encode(),
            headers={
                'Title': title,
                'Priority': clean(cfg.get('priority') or 'default'),
                'Tags': clean(cfg.get('tags') or 'raising_hand'),
            },
            method='POST',
        )
        urlopen(request, timeout=2).read()
    except Exception as exc:
        STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (STATE / 'notify-errors.log').open('a') as log:
            log.write(f'{time.time():.0f} ntfy: {type(exc).__name__}: {exc}\n')


def send_to_pane(pane, pid, text, submit=True, sock=None):
    """Paste text into a live pane after verifying pane_pid. Returns None on success."""
    text = str(text)
    if not text.strip():
        return 'empty message'
    if len(text) > 8000:
        return 'message too long'
    live = live_panes(sock)
    meta = next((row for row in live.values() if row['pane'] == pane and row['pid'] == str(pid)), None)
    if not meta:
        return 'pane not found or pid mismatch'
    # Verify again at paste time — pane ids are reused.
    actual = tmux('display-message', '-p', '-t', pane, '#{pane_pid}', check=False, sock=sock)
    if actual != str(pid):
        return 'pane pid changed'
    encoded = text.encode()
    buf = f'tmux-attn-{os.getpid()}-{time.time_ns()}'
    try:
        subprocess.run(
            ['tmux'] + (['-S', sock] if sock else []) + ['load-buffer', '-b', buf, '-'],
            input=encoded, capture_output=True, timeout=3, check=True,
        )
        tmux('paste-buffer', '-b', buf, '-t', pane, '-p', '-d', sock=sock)
        if submit and not text.endswith('\n'):
            tmux('send-keys', '-t', pane, 'Enter', sock=sock)
    except (RuntimeError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        return f'paste failed: {exc}'
    return None


def hook_target():
    # Codex may construct a hook environment from an older shell snapshot.
    # Prefer the live process tree over TMUX_PANE (which can be stale).
    process = subprocess.run(['ps','-axo','pid=,ppid='],capture_output=True,text=True,timeout=3)
    parents = {}
    children = {}
    for line in process.stdout.splitlines():
        fields = line.split()
        if len(fields) == 2:
            pid, ppid = int(fields[0]), int(fields[1])
            parents[pid] = ppid
            children.setdefault(ppid, []).append(pid)
    ancestors = []
    pid = os.getpid()
    while pid > 1 and pid not in ancestors:
        ancestors.append(pid)
        pid = parents.get(pid, 0)
    ancestor_set = set(ancestors)
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
            if len(fields) != 2 or not fields[1].isdigit():
                continue
            pane_id, pane_pid = fields[0], int(fields[1])
            # Hook is under the pane shell (typical CLI).
            if pane_pid in ancestor_set:
                matches.append((ancestors.index(pane_pid), candidate, pane_id, str(pane_pid)))
                continue
            # Or hook was reparented but still descends from the pane tree.
            stack = [pane_pid]
            seen = set()
            while stack:
                current = stack.pop()
                if current in seen:
                    continue
                seen.add(current)
                if current == os.getpid() or current in ancestor_set:
                    matches.append((len(seen), candidate, pane_id, str(pane_pid)))
                    break
                stack.extend(children.get(current, ()))
    if matches:
        _, sock, pane, pane_pid = min(matches)
        return sock, pane, pane_pid
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
        # Cursor imports Claude hooks; retain the identity of the actual caller.
        if agent == 'claude' and (os.environ.get('CURSOR_VERSION') or data.get('cursor_version')):
            agent = 'cursor'
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
                recorded_revision = None
                if event in ('SessionEnd','session_shutdown','sessionEnd') and existing:
                    if existing['needs_review']:
                        # Keep an unread completed response after the agent exits.
                        state = None
                    if existing['state'] in ('RUNNING','WAIT'):
                        state = ('IDLE', 'Agent exited; review current work', True)
                if event not in ('SessionStart','session_start','sessionStart') or not existing:
                    if state:
                        recorded_revision = record(con, sock, pane, pid, agent, *state,event_key=(data.get('generation_id','') if agent == 'cursor' else data.get('turn_id','')))
                user_text = hook_user_text(data) if event in (
                    'UserPromptSubmit', 'beforeSubmitPrompt', 'agent_start',
                ) else ''
                agent_text = str(data.get('last_assistant_message') or '').strip() if event in (
                    'Stop', 'stop', 'agent_settled', 'StopFailure',
                ) else ''
                transcript = data.get('transcript_path') or ''
                if user_text or agent_text or transcript:
                    if not con.execute('SELECT 1 FROM panes WHERE socket=? AND pane=? AND pid=?', (sock, pane, pid)).fetchone():
                        record(con, sock, pane, pid, agent, 'IDLE', 'Registered; no new work yet')
                    remember_preview(
                        con, sock, pane, pid,
                        user=user_text or None,
                        agent=agent_text or None,
                        transcript_path=transcript or None,
                        expected_revision=recorded_revision,
                    )
    except Exception as exc:
        STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (STATE / 'hook-errors.log').open('a') as f:
            f.write(f'{time.time():.0f} {agent}/{event}: {type(exc).__name__}: {exc}\n')
    # Codex Stop requires JSON output; {} has no decision effects.
    print('{}')


def live_panes(sock=None):
    fmt = '#{pane_id}\t#{pane_pid}\t#{session_name}\t#{window_name}\t#{pane_current_path}\t#{?@tmux_attn_sidebar,1,0}\t#{window_id}\t#{pane_current_command}\t#{@tmux_attn_view_revision}'
    result = {}
    for line in tmux('list-panes', '-a', '-F', fmt, check=False, sock=sock).splitlines():
        parts = line.split('\t')
        if len(parts) >= 7 and parts[5] != '1':
            result[(parts[0], parts[1])] = dict(
                pane=parts[0], pid=parts[1], session=parts[2], window=clean(parts[3]),
                path=parts[4], window_id=parts[6], command=clean(parts[7] if len(parts) > 7 else ''),
                view_revision=parts[8] if len(parts) > 8 else '',
            )
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
        reviewable = row['state'] == 'WAIT' or bool(row['needs_review'])
        item['unread'] = reviewable and row['revision'] > row['acknowledged']
        item['reviewed'] = reviewable and not item['unread']
        pending = item['unread']
        snoozed = row['snoozed_revision'] == row['revision'] and row['snoozed_until'] > now
        # Allow immediate continuation hooks to settle before showing completion.
        stable = not row['needs_review'] or now - row['updated'] >= 2
        item['pending'] = pending and not snoozed and stable
        if item['pending'] or all_states:
            result.append(item)
    result.sort(key=lambda x: (0 if x['pending'] and x['state'] == 'WAIT' else 1 if x['pending'] else 2, -x['updated'], x['pane']))
    return result, running


def acknowledge(con, item, seconds=0):
    # Navigation serializes cursor changes and confirmation in one transaction.
    with (nullcontext() if con.in_transaction else con):
        if seconds:
            con.execute('UPDATE panes SET snoozed_revision=?,snoozed_until=? WHERE socket=? AND pane=? AND pid=? AND revision=?',
                        (item['revision'], time.time()+seconds, item['socket'], item['pane'], item['pid'], item['revision']))
        else:
            con.execute('UPDATE panes SET acknowledged=MAX(acknowledged,?) WHERE socket=? AND pane=? AND pid=?',
                        (item['revision'], item['socket'], item['pane'], item['pid']))


STATUS_GROUPS = [('wait','🙋'),('review','👀'),('running','🏃'),('idle','💤')]


def status_groups(rows):
    groups = {key:[] for key,_ in STATUS_GROUPS}
    for row in rows:
        category = ('wait' if row['state'] == 'WAIT' else
                    'running' if row['state'] == 'RUNNING' else
                    'review' if row.get('needs_review') or row['pending'] else 'idle')
        groups[category].append(row)
    for group in groups.values():
        # Keep stable navigation for working/waiting panes; review follows recency below.
        group.sort(key=lambda row:(row['session'],int(row['window_id'][1:]),int(row['pane'][1:])))
    groups['review'].sort(key=lambda row: (not row.get('unread', row['pending']),
                                          -float(row.get('updated') or 0), row['pane']))
    return groups


def status_text(groups, clickable=False):
    labels = []
    for index,(key,emoji) in enumerate(STATUS_GROUPS):
        count = sum(row.get('unread', row['pending']) for row in groups[key]) if key == 'review' else len(groups[key])
        label = f'{emoji} {count}'
        if clickable:
            label = f'#[range=control|{index}]'+label+'#[norange]'
        labels.append(label)
    return ' · '.join(labels)


def cycle_category(con, sock, client, category):
    if category not in dict(STATUS_GROUPS) or not client:
        return
    # Serialize rapid clicks, keeping every client's four cursors separate.
    with con:
        con.execute('BEGIN IMMEDIATE')
        registered,_ = items(con,sock,live_panes(),True)
        group = status_groups(registered)[category]
        if not group:
            return
        cursor = con.execute('SELECT pane,pid FROM cycle_cursors WHERE socket=? AND client=? AND category=?',
                             (sock,client,category)).fetchone()
        identities = [(row['pane'],row['pid']) for row in group]
        previous = tuple(cursor) if cursor else None
        index = (identities.index(previous)+1)%len(group) if previous in identities else 0
        target = group[index]
        if jump_and_acknowledge(con,target,client):
            con.execute('''INSERT INTO cycle_cursors(socket,client,category,pane,pid) VALUES(?,?,?,?,?)
                ON CONFLICT(socket,client,category) DO UPDATE SET pane=excluded.pane,pid=excluded.pid''',
                (sock,client,category,target['pane'],target['pid']))


def window_badge(rows):
    if any(x['state'] == 'WAIT' for x in rows):
        return '🙋'
    if any(x['state'] == 'IDLE' and x.get('unread', x['pending']) for x in rows):
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


def update_window_badges(rows, live=None, sock=None):
    live = live_panes(sock=sock) if live is None else live
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
    for row in rows:
        token = f"{row['pid']}:{row['revision']}" if row['state'] == 'IDLE' and row.get('needs_review') else ''
        if live.get((row['pane'], row['pid']), {}).get('view_revision', '') != token:
            if commands:
                commands.append(';')
            commands.extend(['set-option', '-p', '-t', row['pane'], VIEW_REVISION_OPTION, token])
    for line in tmux('list-windows', '-a', '-F', '#{window_id}\t#{@tmux_attn_badge}\t#{@tmux_attn_project}', check=False, sock=sock).splitlines():
        window, current, project = (line.split('\t') + ['', ''])[:3]
        group = groups.get(window, [])
        for key, previous, desired in [('@tmux_attn_badge', current, window_badge(group)), ('@tmux_attn_project', project, labels.get(paths.get(window), ''))]:
            if desired != previous:
                if commands:
                    commands.append(';')
                commands.extend(['set-option', '-w', '-t', window, key, desired])
    if commands:
        # One server command for changed windows; no process per window per refresh.
        tmux(*commands, check=False, sock=sock)


def jump(item, client=None):
    # Verify pane PID as well: an old row must never target a reused pane.
    pid = tmux('display-message', '-p', '-t', item['pane'], '#{pane_pid}', check=False)
    if pid != item['pid']:
        return False
    tmux('select-pane', '-t', item['pane'])
    tmux('select-window', '-t', item['pane'])
    if client:
        tmux('switch-client', '-c', client, '-t', item['pane'])
    return True


def jump_and_acknowledge(con, item, client=None):
    """Confirm only the completion version selected before a successful jump."""
    if not jump(item, client):
        return False
    if item['state'] == 'IDLE' and item.get('needs_review'):
        acknowledge(con, item)
    return True


def publish_view_revision(sock, pane, pid, revision):
    """Mirror a version into tmux so the selection hook can snapshot it immediately."""
    try:
        actual = tmux('display-message', '-p', '-t', pane, '#{pane_pid}', check=False, sock=sock)
        if actual == str(pid):
            token = f'{pid}:{revision}' if revision else ''
            tmux('set-option', '-p', '-t', pane, VIEW_REVISION_OPTION, token, check=False, sock=sock)
    except (OSError, RuntimeError, subprocess.SubprocessError):
        pass  # A departed pane must never fail an agent hook.


def acknowledge_visible_pane(con, sock, client, pane, pid, token):
    """Confirm the pane/version captured by a human client's selection event."""
    match = re.fullmatch(r'([0-9]+):([1-9][0-9]*)', token or '')
    if not client or not match or match[1] != str(pid):
        return False
    # Background selections and control-mode clients must not clear human attention.
    visible = tmux('display-message', '-p', '-c', client,
                   '#{pane_id} #{pane_pid} #{client_control_mode}', check=False, sock=sock)
    if visible != f'{pane} {pid} 0':
        return False
    revision = int(match[2])
    with (nullcontext() if con.in_transaction else con):
        if not con.in_transaction:
            con.execute('BEGIN IMMEDIATE')
        row = con.execute('SELECT * FROM panes WHERE socket=? AND pane=? AND pid=?',
                          (sock, pane, pid)).fetchone()
        if not row or row['state'] != 'IDLE' or not row['needs_review'] or revision > row['revision']:
            return False
        acknowledge(con, dict(row) | {'revision': revision})
    return True


def view_hooks_config(launch):
    command = f'{launch} viewed "#{{client_name}}" "#{{pane_id}}" "#{{pane_pid}}" "#{{{VIEW_REVISION_OPTION}}}"'
    hook = 'run-shell -b ' + shlex.quote(command)
    return ''.join(f'set-hook -g {shlex.quote(name + "[" + str(VIEW_HOOK_INDEX) + "]")} {shlex.quote(hook)}\n'
                   for name in VIEW_HOOKS)


def install_view_hooks(sock=None):
    """Add viewing hooks to an existing installation without replacing its settings."""
    config = Path.home() / '.config/tmux-attn/sidebar.conf'
    start, end = '# Begin automatic viewing hooks\n', '# End automatic viewing hooks\n'
    original = config.read_text()
    prior = []
    for name in VIEW_HOOKS:
        value = tmux('show-hooks', '-g', f'{name}[{VIEW_HOOK_INDEX}]', check=False, sock=sock)
        command = value.partition(' ')[2].strip()
        if command and str(ROOT / 'attention.py') not in command:
            raise RuntimeError(f'Hook index {VIEW_HOOK_INDEX} already used: {name}')
        prior.append(value)
    backup = STATE / 'backups' / f'view-hooks-{time.time_ns()}'
    backup.mkdir(parents=True)
    (backup / 'sidebar.conf').write_text(original)
    (backup / 'runtime-hooks.conf').write_text('\n'.join(prior) + '\n')
    launch = shlex.join(['env', 'TMUX_ATTN_STATE_DIR=' + str(STATE), sys.executable, str(ROOT / 'attention.py')])
    block = start + view_hooks_config(launch) + end
    updated = re.sub(re.escape(start) + r'.*?' + re.escape(end), '', original, flags=re.S)
    # Existing installer versions may include unmarked copies of these same hooks.
    updated = '\n'.join(line for line in updated.splitlines()
                        if not any(line.startswith(f'set-hook -g {name}[{VIEW_HOOK_INDEX}] ') for name in VIEW_HOOKS))
    config.write_text(updated.rstrip() + '\n' + block)
    runtime = backup / 'apply.conf'
    runtime.write_text(block)
    tmux('source-file', str(runtime), sock=sock)
    with closing(db()) as con:
        live = live_panes(sock=sock)
        rows, _ = items(con, sock or resolve_socket(), live, True)
        update_window_badges(rows, live, sock=sock)
    return str(backup)


def age(stamp):
    seconds = max(0, int(time.time()-stamp))
    return f'{seconds//3600}h' if seconds >= 3600 else f'{seconds//60}m' if seconds >= 60 else f'{seconds}s'


def agent_emoji(row):
    if row['state'] == 'WAIT':
        return '🙋'
    if row['state'] == 'RUNNING':
        return '🏃'
    if row.get('pending'):
        return '👀'
    return '💤'


def agent_severity(row):
    reason = row.get('reason') or ''
    if 'error' in reason.lower():
        return 'error'
    if 'Interrupted' in reason:
        return 'interrupt'
    return ''


KNOWN_CLI_ICONS = ('claude', 'codex', 'agent', 'pi')


def cli_label(agent):
    """Human-facing CLI name for the web panel."""
    key = clean(agent).lower()
    return {
        'claude': 'claude',
        'codex': 'codex',
        'cursor': 'agent',
        'pi': 'pi',
    }.get(key, key or 'unknown')


def command_cli(command):
    """Map tmux pane_current_command to a display cli label when possible."""
    raw = clean(command).strip()
    if not raw:
        return 'shell'
    key = Path(raw).name.lower()
    if key in ('claude', 'claude-code'):
        return 'claude'
    if key in ('codex',):
        return 'codex'
    if key in ('agent', 'cursor'):
        return 'agent'
    if key in ('pi',):
        return 'pi'
    # Claude Code sometimes surfaces as a version-looking binary name.
    if key.replace('.', '').isdigit() and key.count('.') >= 2:
        return 'claude'
    return key[:24]


def agent_sort_key(row):
    """Band by attention need; within a band, newest activity first."""
    host = row.get('host') or ''
    if not row.get('tracked', True):
        return (4, host, row.get('session', ''), row.get('window_id', ''), row['pane'])
    # Negate updated so the most recently completed / active pane floats up.
    fresh = -float(row.get('updated') or 0)
    if row['state'] == 'WAIT':
        return (0, fresh, host, row['pane'])
    if row.get('needs_review') or row.get('pending'):
        return (1, not row.get('unread', row.get('pending', False)), fresh, host, row['pane'])
    if row['state'] == 'RUNNING':
        return (2, fresh, host, row['pane'])
    return (3, fresh, host, row['pane'])


def local_host_name():
    return clean(os.environ.get('TMUX_ATTN_HOST') or socket_mod.gethostname().split('.')[0]) or 'local'


def peers_config_path():
    return Path(os.environ.get('TMUX_ATTN_PEERS', Path.home() / '.config/tmux-attn/peers.json'))


def load_peers():
    path = peers_config_path()
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    peers = data.get('peers', []) if isinstance(data, dict) else []
    result = []
    for peer in peers:
        if not isinstance(peer, dict):
            continue
        name = clean(peer.get('name') or '')
        url = clean(peer.get('url') or '').rstrip('/')
        if name and url.startswith(('http://', 'https://')):
            result.append({'name': name, 'url': url})
    return result


def upsert_peer(name, url):
    """Add or update a peer in peers.json. Returns (ok, detail)."""
    name = clean(name or '')
    url = clean(url or '').rstrip('/')
    if not name or not url.startswith(('http://', 'https://')):
        return False, 'name and http(s) url required'
    path = peers_config_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = {'peers': []}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text() or '{}')
            if isinstance(loaded, dict):
                data = loaded
        except ValueError:
            return False, 'peers.json is not valid JSON'
    peers = list(data.get('peers') or [])
    action = 'added'
    for row in peers:
        if isinstance(row, dict) and clean(row.get('name') or '') == name:
            row['url'] = url
            action = 'updated'
            break
    else:
        peers.append({'name': name, 'url': url})
    data['peers'] = peers
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    return True, action


def kill_pane(pane, pid, sock=None):
    """Kill a live pane after verifying pane_pid. Returns None on success."""
    pane = clean(pane or '')
    pid = clean(str(pid or ''))
    if not pane or not pid:
        return 'pane and pid required'
    live = live_panes(sock)
    meta = next((row for row in live.values() if row['pane'] == pane and row['pid'] == str(pid)), None)
    if not meta:
        return 'pane not found or pid mismatch'
    actual = tmux('display-message', '-p', '-t', pane, '#{pane_pid}', check=False, sock=sock)
    if actual != str(pid):
        return 'pane pid changed'
    try:
        tmux('kill-pane', '-t', pane, sock=sock)
    except RuntimeError as exc:
        return f'kill failed: {exc}'
    return None


PEER_FETCH_TIMEOUT = 3.5
_FLEET_HOST_CACHE = {}
_FLEET_STALE_TTL = 35.0


def fetch_peer_agents(url, timeout=PEER_FETCH_TIMEOUT):
    """Pull a machine-local /api/agents document. Never call /api/fleet here."""
    request = Request(url.rstrip('/') + '/api/agents', headers={'Accept': 'application/json'})
    with urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read().decode())
    if not isinstance(data, dict) or not data.get('ok'):
        raise RuntimeError(data.get('error') if isinstance(data, dict) else 'invalid peer payload')
    return data


def empty_summary():
    return {'wait': 0, 'review': 0, 'review_seen': 0, 'running': 0, 'idle': 0, 'tracked': 0, 'untracked': 0}


def merge_summaries(parts):
    summary = empty_summary()
    for part in parts:
        for key in summary:
            summary[key] += int(part.get(key) or 0)
    return summary


def tag_host(payload, host):
    agents = []
    for row in payload.get('agents') or []:
        item = dict(row)
        item['host'] = host
        agents.append(item)
    return agents, payload.get('summary') or empty_summary()


def _remember_fleet_host(cache_key, now, tagged, summary):
    _FLEET_HOST_CACHE[cache_key] = (
        float(now),
        [dict(item) for item in tagged],
        dict(summary),
    )


def _cached_fleet_host(cache_key, now, stale_ttl):
    entry = _FLEET_HOST_CACHE.get(cache_key)
    if not entry or stale_ttl <= 0:
        return None
    cached_at, tagged, summary = entry
    age_s = float(now) - cached_at
    if age_s < 0 or age_s > stale_ttl:
        return None
    return [dict(item) for item in tagged], dict(summary), int(age_s)


def fleet_payload(include_local=True, peer_timeout=PEER_FETCH_TIMEOUT, now=None, stale_ttl=_FLEET_STALE_TTL):
    """Aggregate local and/or Tailscale/LAN peer /api/agents feeds."""
    now = time.time() if now is None else float(now)
    agents = []
    summaries = []
    hosts = []
    if include_local:
        host = local_host_name()
        cache_key = ('local', host)
        try:
            with closing(db()) as con:
                local = agents_payload(con)
            tagged, summary = tag_host(local, host)
            _remember_fleet_host(cache_key, now, tagged, summary)
            agents.extend(tagged)
            summaries.append(summary)
            hosts.append({'name': host, 'ok': True, 'source': 'local'})
        except Exception as exc:
            cached = _cached_fleet_host(cache_key, now, stale_ttl)
            host_row = {'name': host, 'ok': False, 'source': 'local', 'error': f'{type(exc).__name__}'}
            if cached:
                tagged, summary, stale_age = cached
                agents.extend(tagged)
                summaries.append(summary)
                host_row['stale'] = True
                host_row['stale_age'] = stale_age
            hosts.append(host_row)
    peers = load_peers()
    if peers:
        with ThreadPoolExecutor(max_workers=min(8, len(peers))) as pool:
            futures = {pool.submit(fetch_peer_agents, peer['url'], peer_timeout): peer for peer in peers}
            for future in as_completed(futures):
                peer = futures[future]
                cache_key = ('peer', peer['name'], peer['url'])
                try:
                    payload = future.result()
                    tagged, summary = tag_host(payload, peer['name'])
                    _remember_fleet_host(cache_key, now, tagged, summary)
                    agents.extend(tagged)
                    summaries.append(summary)
                    hosts.append({'name': peer['name'], 'ok': True, 'source': 'peer', 'url': peer['url']})
                except Exception as exc:
                    cached = _cached_fleet_host(cache_key, now, stale_ttl)
                    host_row = {
                        'name': peer['name'], 'ok': False, 'source': 'peer', 'url': peer['url'],
                        'error': f'{type(exc).__name__}',
                    }
                    if cached:
                        tagged, summary, stale_age = cached
                        agents.extend(tagged)
                        summaries.append(summary)
                        host_row['stale'] = True
                        host_row['stale_age'] = stale_age
                    hosts.append(host_row)
    agents.sort(key=agent_sort_key)
    hosts.sort(key=lambda row: (0 if row.get('ok') else 1, row.get('name') or ''))
    return {
        'ok': True,
        'summary': merge_summaries(summaries) if summaries else empty_summary(),
        'agents': agents,
        'hosts': hosts,
    }


_GIT_ENV_CACHE = {}
_GIT_ENV_TTL = 25.0


def repo_name(path):
    return Path(path).name if path else ''


def git_workdir_info(path):
    """Best-effort repo basename, branch, and dirty flag for a pane cwd."""
    path = str(path or '').strip()
    repo = repo_name(path)
    empty = {'repo': repo, 'branch': '', 'dirty': False}
    if not path:
        return empty
    now = time.time()
    cached = _GIT_ENV_CACHE.get(path)
    if cached and now - cached[0] < _GIT_ENV_TTL:
        info = dict(cached[1])
        info['repo'] = repo
        return info
    info = dict(empty)
    try:
        result = subprocess.run(
            ['git', '-C', path, 'status', '-sb', '--porcelain'],
            capture_output=True, text=True, timeout=0.8,
        )
        if result.returncode != 0:
            _GIT_ENV_CACHE[path] = (now, info)
            return info
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        if not lines:
            _GIT_ENV_CACHE[path] = (now, info)
            return info
        head = lines[0]
        if head.startswith('## '):
            ref = head[3:].split('...')[0].strip()
            for prefix in ('No commits yet on ', 'Initial commit on '):
                if ref.startswith(prefix):
                    ref = ref[len(prefix):]
                    break
            if ref.startswith('HEAD ') or ref == 'HEAD (no branch)':
                short = subprocess.run(
                    ['git', '-C', path, 'rev-parse', '--short', 'HEAD'],
                    capture_output=True, text=True, timeout=0.5,
                )
                info['branch'] = (short.stdout.strip() if short.returncode == 0 else '') or 'detached'
            else:
                info['branch'] = ref
            info['dirty'] = any(not line.startswith('## ') for line in lines)
        else:
            # Unexpected format; treat remaining lines as dirty markers only.
            info['dirty'] = True
    except (OSError, subprocess.TimeoutExpired):
        if cached:
            stale_info = dict(cached[1])
            stale_info['repo'] = repo
            _GIT_ENV_CACHE[path] = (now, stale_info)
            return stale_info
    _GIT_ENV_CACHE[path] = (now, info)
    return info


def agents_payload(con, sock=None, live=None, now=None):
    """JSON-ready status for the mobile web panel and future clients."""
    now = time.time() if now is None else now
    sock = resolve_socket() if sock is None else sock
    if live is None:
        live = live_panes(sock)
        if not live and con.execute('SELECT 1 FROM panes WHERE socket=? LIMIT 1', (sock,)).fetchone():
            time.sleep(0.04)
            live = live_panes(sock)
            if not live and sock and Path(sock).exists():
                raise RuntimeError('transient empty live_panes while socket exists')
    rows, _ = items(con, sock, live, True, now=now)
    groups = status_groups(rows)
    tracked_keys = {(row['pane'], row['pid']) for row in rows}
    paths = [row['path'] for row in rows if row.get('path')]
    paths += [meta['path'] for key, meta in live.items() if key not in tracked_keys and meta.get('path')]
    labels = project_labels(paths, load_project_aliases())
    agents = []
    for row in rows:
        reviewed = row['revision'] <= row['acknowledged'] and (row['state'] == 'WAIT' or row['needs_review'])
        command = row.get('command') or ''
        preview = pane_conversation_preview(con, row, row)
        env = git_workdir_info(row.get('path', ''))
        agents.append({
            'pane': row['pane'],
            'pid': row['pid'],
            'session': row['session'],
            'window': row['window'],
            'window_id': row.get('window_id', ''),
            'agent': row['agent'],
            'cli': cli_label(row['agent']),
            'command': command,
            'tracked': True,
            'project': labels.get(row['path'], Path(row['path']).name if row.get('path') else ''),
            'path': row.get('path', ''),
            'repo': env['repo'],
            'branch': env['branch'],
            'dirty': bool(env['dirty']),
            'state': row['state'],
            'pending': bool(row['pending']),
            'needs_review': bool(row['needs_review']),
            'unread': bool(row['unread']),
            'reviewed': bool(row['reviewed']),
            'revision': row['revision'],
            'acknowledged': row['acknowledged'],
            'emoji': agent_emoji(row),
            'severity': agent_severity(row),
            'source': (row['source'] if 'source' in row.keys() else 'hook') or 'hook',
            'reason': display_reason(row, reviewed),
            'preview': preview,
            'updated': row['updated'],
            'age': age(row['updated']),
        })
    for key, meta in live.items():
        if key in tracked_keys:
            continue
        command = meta.get('command') or ''
        cli = command_cli(command)
        env = git_workdir_info(meta.get('path', ''))
        agents.append({
            'pane': meta['pane'],
            'pid': meta['pid'],
            'session': meta['session'],
            'window': meta['window'],
            'window_id': meta.get('window_id', ''),
            'agent': '',
            'cli': cli,
            'command': command,
            'tracked': False,
            'project': labels.get(meta['path'], Path(meta['path']).name if meta.get('path') else ''),
            'path': meta.get('path', ''),
            'repo': env['repo'],
            'branch': env['branch'],
            'dirty': bool(env['dirty']),
            'state': 'SHELL',
            'pending': False,
            'emoji': '💻',
            'severity': '',
            'source': 'shell',
            'reason': f'未追蹤 · 前景 {command or "shell"}',
            'preview': {'users': [], 'agent': ''},
            'updated': 0,
            'age': '',
        })
    agents.sort(key=agent_sort_key)
    return {
        'ok': True,
        'summary': {
            'wait': len(groups['wait']),
            'review': sum(row['unread'] for row in groups['review']),
            'review_seen': sum(not row['unread'] for row in groups['review']),
            'running': len(groups['running']),
            'idle': len(groups['idle']),
            'tracked': len(rows),
            'untracked': max(0, len(live) - len(rows)),
        },
        'agents': agents,
    }


def grouped_rows(rows):
    return sorted(rows, key=lambda x: (('WAIT', 'IDLE', 'RUNNING').index(x['state']),
                                      not x.get('needs_review', False) if x['state'] == 'IDLE' else False,
                                      not x.get('unread', x['pending']), -x['updated'], x['pane']))


def display_reason(item, reviewed):
    if item['state'] == 'WAIT' and item['reason'].startswith('Queued follow-up questions: '):
        count = item['reason'].split(': ',1)[1]
        return f'有 {count} 個問題等你回答（agent 仍可繼續工作）'
    if item['state'] == 'IDLE':
        background = '（背景工作仍在運行）' if 'background tasks still running' in item['reason'] else ''
        if reviewed:
            return '已完成，已查看' + background
        if item['needs_review']:
            if 'question' in item['reason'].lower():
                return '回覆以問號結尾，建議查看（非確定等待）' + background
            outcome = '已中斷' if 'Interrupted' in item['reason'] else '發生錯誤' if 'error' in item['reason'] else '已完成'
            return outcome + '，尚未查看' + background
        return '閒置，等待新任務'
    if item['state'] == 'RUNNING':
        return '背景工作仍在運行' if 'Background work' in item['reason'] else '正在工作'
    reason = item['reason']
    if 'Approval' in reason or 'approval' in reason:
        return '需要批准指令或操作 · ' + reason
    return '需要回答或下判斷 · ' + reason


def inbox_lines(rows):
    lines = []
    groups = [
        ('🙋 等待回覆', [x for x in rows if x['state'] == 'WAIT']),
        ('👀 待看 · 未看', [x for x in rows if x['state'] == 'IDLE' and x.get('needs_review') and x['unread']]),
        ('👀 待看 · 已看', [x for x in rows if x['state'] == 'IDLE' and x.get('needs_review') and not x['unread']]),
        ('💤 閒置', [x for x in rows if x['state'] == 'IDLE' and not x.get('needs_review')]),
        ('🏃 工作進行中', [x for x in rows if x['state'] == 'RUNNING']),
    ]
    for title, group in groups:
        lines.append((None, f'── {title} ({len(group)}) ──'))
        for item in group:
            identity = (item['pane'], item['pid'])
            reviewed = item['reviewed']
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
        idle = sum(x['state'] == 'IDLE' and not x['needs_review'] for x in registered)
        ids = [(x['pane'], x['pid']) for x in rows]
        if selected not in ids:
            selected = ids[0] if ids else None
        index = ids.index(selected) if selected else 0
        h, w = win.getmaxyx()
        win.erase()
        draw(win, 0, 1, '待處理清單', curses.A_BOLD)
        waiting = sum(x['state'] == 'WAIT' for x in registered)
        review = sum(x['unread'] and x['state'] == 'IDLE' for x in registered)
        draw(win, 1, 1, f'🙋 {waiting} · 👀 {review} · 🏃 {running} · 💤 {idle}', curses.A_BOLD)
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
                jumped = jump_and_acknowledge(con, rows[index], target_client or None)
                if popup and jumped:
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


def prune_stale_panes(con, sock, live):
    """Delete inbox rows whose pane+pid is no longer live (status already ignores them).

    Skip when live is empty: list-panes failure also yields {}, and pruning then
    would wipe every row on the socket.
    """
    if not live:
        return 0
    live_keys = set(live)
    removed = 0
    with con:
        for table in ('panes', 'codex_sessions'):
            for row in con.execute(f'SELECT pane, pid FROM {table} WHERE socket=?', (sock,)).fetchall():
                if (row['pane'], row['pid']) in live_keys:
                    continue
                con.execute(f'DELETE FROM {table} WHERE socket=? AND pane=? AND pid=?',
                            (sock, row['pane'], row['pid']))
                removed += 1
        for row in con.execute(
            'SELECT client, category, pane, pid FROM cycle_cursors WHERE socket=?', (sock,)
        ).fetchall():
            if (row['pane'], row['pid']) in live_keys:
                continue
            con.execute(
                'DELETE FROM cycle_cursors WHERE socket=? AND client=? AND category=?',
                (sock, row['client'], row['category']),
            )
            removed += 1
    return removed


def web_index_path():
    return ROOT / 'web' / 'index.html'


def web_icon_path(name):
    safe = Path(name).name
    if not safe.endswith('.svg') or safe != name:
        return None
    aliases = {'cursor.svg': 'agent.svg'}
    safe = aliases.get(safe, safe)
    path = ROOT / 'web' / 'icons' / safe
    return path if path.is_file() else None


def dispatch_send(payload):
    """Send text to a local pane or forward to a named peer. Returns (code, body dict)."""
    if not isinstance(payload, dict):
        return 400, {'ok': False, 'error': 'invalid json'}
    pane = clean(payload.get('pane') or '')
    pid = clean(str(payload.get('pid') or ''))
    text = payload.get('text')
    host = clean(payload.get('host') or '')
    submit = payload.get('submit', True)
    if not pane or not pid or text is None:
        return 400, {'ok': False, 'error': 'pane, pid, and text are required'}
    local = local_host_name()
    if host and host != local and not payload.get('_local_only'):
        peer = next((row for row in load_peers() if row['name'] == host), None)
        if not peer:
            return 404, {'ok': False, 'error': f'unknown host {host}'}
        try:
            body = json.dumps({
                'pane': pane, 'pid': pid, 'text': text, 'submit': submit, '_local_only': True,
            }).encode()
            request = Request(
                peer['url'].rstrip('/') + '/api/send',
                data=body,
                headers={'Content-Type': 'application/json', 'Accept': 'application/json'},
                method='POST',
            )
            with urlopen(request, timeout=3) as response:
                data = json.loads(response.read().decode())
            return (200 if data.get('ok') else 502), data
        except Exception as exc:
            return 502, {'ok': False, 'error': f'peer send failed: {type(exc).__name__}'}
    error = send_to_pane(pane, pid, text, submit=bool(submit))
    if error:
        return 409, {'ok': False, 'error': error}
    return 200, {'ok': True, 'pane': pane, 'pid': pid, 'host': local}


def dispatch_kill(payload):
    """Kill a local pane or forward kill to a named peer."""
    if not isinstance(payload, dict):
        return 400, {'ok': False, 'error': 'invalid json'}
    pane = clean(payload.get('pane') or '')
    pid = clean(str(payload.get('pid') or ''))
    host = clean(payload.get('host') or '')
    if not pane or not pid:
        return 400, {'ok': False, 'error': 'pane and pid are required'}
    local = local_host_name()
    if host and host != local and not payload.get('_local_only'):
        peer = next((row for row in load_peers() if row['name'] == host), None)
        if not peer:
            return 404, {'ok': False, 'error': f'unknown host {host}'}
        try:
            body = json.dumps({'pane': pane, 'pid': pid, '_local_only': True}).encode()
            request = Request(
                peer['url'].rstrip('/') + '/api/kill',
                data=body,
                headers={'Content-Type': 'application/json', 'Accept': 'application/json'},
                method='POST',
            )
            with urlopen(request, timeout=3) as response:
                data = json.loads(response.read().decode())
            return (200 if data.get('ok') else 502), data
        except Exception as exc:
            return 502, {'ok': False, 'error': f'peer kill failed: {type(exc).__name__}'}
    error = kill_pane(pane, pid)
    if error:
        return 409, {'ok': False, 'error': error}
    return 200, {'ok': True, 'pane': pane, 'pid': pid, 'host': local, 'killed': True}


def dispatch_preview(payload):
    """Return a longer conversation preview for the sheet, local or via peer."""
    if not isinstance(payload, dict):
        return 400, {'ok': False, 'error': 'invalid json'}
    pane = clean(payload.get('pane') or '')
    pid = clean(str(payload.get('pid') or ''))
    host = clean(payload.get('host') or '')
    if not pane or not pid:
        return 400, {'ok': False, 'error': 'pane and pid are required'}
    local = local_host_name()
    if host and host != local and not payload.get('_local_only'):
        peer = next((row for row in load_peers() if row['name'] == host), None)
        if not peer:
            return 404, {'ok': False, 'error': f'unknown host {host}'}
        try:
            body = json.dumps({'pane': pane, 'pid': pid, '_local_only': True}).encode()
            request = Request(
                peer['url'].rstrip('/') + '/api/preview',
                data=body,
                headers={'Content-Type': 'application/json', 'Accept': 'application/json'},
                method='POST',
            )
            with urlopen(request, timeout=3) as response:
                data = json.loads(response.read().decode())
            return (200 if data.get('ok') else 502), data
        except Exception as exc:
            return 502, {'ok': False, 'error': f'peer preview failed: {type(exc).__name__}'}
    try:
        with closing(db()) as con:
            snapshot, error = local_preview_for_pane(con, pane, pid, snapshot=True)
    except Exception as exc:
        return 500, {'ok': False, 'error': f'{type(exc).__name__}'}
    if error:
        return 409, {'ok': False, 'error': error}
    return 200, {'ok': True, 'pane': pane, 'pid': pid, 'host': local, **snapshot}


def dispatch_ack(payload):
    """Persist the displayed version on its owning host, never a newer version."""
    if not isinstance(payload, dict):
        return 400, {'ok': False, 'error': 'invalid json'}
    pane = clean(payload.get('pane') or '')
    pid = clean(str(payload.get('pid') or ''))
    host = clean(payload.get('host') or '')
    revision = payload.get('revision')
    if not pane or not pid or type(revision) is not int or revision < 1:
        return 400, {'ok': False, 'error': 'pane, pid, and positive integer revision are required'}
    local = local_host_name()
    if host and host != local and not payload.get('_local_only'):
        peer = next((row for row in load_peers() if row['name'] == host), None)
        if not peer:
            return 404, {'ok': False, 'error': f'unknown host {host}'}
        try:
            request = Request(peer['url'].rstrip('/') + '/api/ack',
                              data=json.dumps({'pane': pane, 'pid': pid, 'revision': revision,
                                               '_local_only': True}).encode(),
                              headers={'Content-Type': 'application/json', 'Accept': 'application/json'},
                              method='POST')
            with urlopen(request, timeout=3) as response:
                data = json.loads(response.read().decode())
            return (200 if data.get('ok') else 502), data
        except HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read().decode())
            except (ValueError, UnicodeError):
                return 502, {'ok': False, 'error': f'peer acknowledgement failed: HTTP {exc.code}'}
        except Exception as exc:
            return 502, {'ok': False, 'error': f'peer acknowledgement failed: {type(exc).__name__}'}
    try:
        sock = resolve_socket()
        if (pane, pid) not in live_panes(sock):
            return 409, {'ok': False, 'error': 'pane is not live'}
        with closing(db()) as con, con:
            con.execute('BEGIN IMMEDIATE')
            row = con.execute('SELECT * FROM panes WHERE socket=? AND pane=? AND pid=?',
                              (sock, pane, pid)).fetchone()
            if not row:
                return 409, {'ok': False, 'error': 'pane is not tracked'}
            if revision > row['revision']:
                return 409, {'ok': False, 'error': 'revision is newer than current content'}
            if row['state'] != 'WAIT' and not row['needs_review']:
                return 409, {'ok': False, 'error': 'pane has no reviewable result'}
            acknowledge(con, dict(row) | {'revision': revision})
            saved = max(row['acknowledged'], revision)
            return 200, {'ok': True, 'pane': pane, 'pid': pid, 'host': local,
                         'revision': row['revision'], 'acknowledged': saved,
                         'unread': saved < row['revision'], 'reviewed': saved >= row['revision']}
    except Exception as exc:
        return 500, {'ok': False, 'error': type(exc).__name__}


def dispatch_peer_register(payload):
    """Upsert a client feed into this server's peers.json."""
    if not isinstance(payload, dict):
        return 400, {'ok': False, 'error': 'invalid json'}
    name = payload.get('name')
    url = payload.get('url')
    ok, detail = upsert_peer(name, url)
    if not ok:
        return 400, {'ok': False, 'error': detail}
    return 200, {'ok': True, 'action': detail, 'peer': {'name': clean(name), 'url': clean(url).rstrip('/')}}


def make_web_handler(fleet_only=False):
    index = web_index_path()
    if not index.is_file():
        raise SystemExit(f'Missing web UI: {index}')

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            sys.stderr.write("%s - %s\n" % (self.address_string(), format % args))

        def _send(self, code, body, content_type):
            payload = body if isinstance(body, bytes) else body.encode()
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            path = urlparse(self.path).path
            if path == '/api/health':
                self._send(200, json.dumps({
                    'ok': True,
                    'fleet_only': fleet_only,
                    'peers': len(load_peers()),
                }), 'application/json; charset=utf-8')
                return
            if path == '/api/agents':
                # Machine-local feed only — peer scrapers must use this path.
                try:
                    with closing(db()) as con:
                        payload = agents_payload(con)
                    self._send(200, json.dumps(payload, ensure_ascii=False), 'application/json; charset=utf-8')
                except Exception as exc:
                    sys.stderr.write(f'web /api/agents: {type(exc).__name__}: {exc}\n')
                    self._send(500, json.dumps({'ok': False, 'error': 'failed to load agents'}),
                               'application/json; charset=utf-8')
                return
            if path == '/api/fleet':
                try:
                    payload = fleet_payload(include_local=not fleet_only)
                    self._send(200, json.dumps(payload, ensure_ascii=False), 'application/json; charset=utf-8')
                except Exception as exc:
                    sys.stderr.write(f'web /api/fleet: {type(exc).__name__}: {exc}\n')
                    self._send(500, json.dumps({'ok': False, 'error': 'failed to load fleet'}),
                               'application/json; charset=utf-8')
                return
            if path.startswith('/icons/'):
                icon = web_icon_path(path[len('/icons/'):])
                if icon:
                    self._send(200, icon.read_bytes(), 'image/svg+xml')
                    return
                self._send(404, json.dumps({'ok': False, 'error': 'not found'}), 'application/json; charset=utf-8')
                return
            if path in ('/', '/index.html'):
                self._send(200, index.read_bytes(), 'text/html; charset=utf-8')
                return
            self._send(404, json.dumps({'ok': False, 'error': 'not found'}), 'application/json; charset=utf-8')

        def do_POST(self):
            path = urlparse(self.path).path
            length = int(self.headers.get('Content-Length') or 0)
            raw = self.rfile.read(length) if length > 0 else b'{}'
            try:
                payload = json.loads(raw.decode() or '{}')
            except ValueError:
                self._send(400, json.dumps({'ok': False, 'error': 'invalid json'}), 'application/json; charset=utf-8')
                return
            if not isinstance(payload, dict):
                self._send(400, json.dumps({'ok': False, 'error': 'invalid json'}), 'application/json; charset=utf-8')
                return
            # Only peer-forwarded server requests may set _local_only.
            payload.pop('_local_only', None)
            if path == '/api/peers':
                code, body = dispatch_peer_register(payload)
                self._send(code, json.dumps(body, ensure_ascii=False), 'application/json; charset=utf-8')
                return
            if path == '/api/kill':
                if fleet_only and not payload.get('host'):
                    self._send(400, json.dumps({'ok': False, 'error': 'host required in fleet mode'}),
                               'application/json; charset=utf-8')
                    return
                code, body = dispatch_kill(payload)
                self._send(code, json.dumps(body, ensure_ascii=False), 'application/json; charset=utf-8')
                return
            if path == '/api/send':
                if fleet_only and not payload.get('host'):
                    self._send(400, json.dumps({'ok': False, 'error': 'host required in fleet mode'}),
                               'application/json; charset=utf-8')
                    return
                code, body = dispatch_send(payload)
                self._send(code, json.dumps(body, ensure_ascii=False), 'application/json; charset=utf-8')
                return
            if path == '/api/preview':
                if fleet_only and not payload.get('host'):
                    self._send(400, json.dumps({'ok': False, 'error': 'host required in fleet mode'}),
                               'application/json; charset=utf-8')
                    return
                code, body = dispatch_preview(payload)
                self._send(code, json.dumps(body, ensure_ascii=False), 'application/json; charset=utf-8')
                return
            if path == '/api/ack':
                if fleet_only and not payload.get('host'):
                    self._send(400, json.dumps({'ok': False, 'error': 'host required in fleet mode'}),
                               'application/json; charset=utf-8')
                    return
                code, body = dispatch_ack(payload)
                self._send(code, json.dumps(body, ensure_ascii=False), 'application/json; charset=utf-8')
                return
            self._send(404, json.dumps({'ok': False, 'error': 'not found'}), 'application/json; charset=utf-8')

    return Handler


def serve_web(host='0.0.0.0', port=7788, fleet_only=False):
    server = ThreadingHTTPServer((host, port), make_web_handler(fleet_only=fleet_only))
    print(json.dumps({
        'web': f'http://{host}:{port}/',
        'bind': host,
        'port': port,
        'fleet_only': fleet_only,
        'peers': len(load_peers()),
    }, ensure_ascii=False), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


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
    parser.add_argument('command', choices=['event','sidebar','ensure','status','dump','next','focus','prune','observe','popup','inbox','status-clickable','cycle','web','viewed','install-view-hooks'])
    parser.add_argument('args', nargs='*')
    args = parser.parse_args()
    if args.command == 'event':
        event_command(*args.args)
    elif args.command == 'viewed':
        if len(args.args) == 4:
            try:
                with closing(db()) as con:
                    acknowledge_visible_pane(con, resolve_socket(), *args.args)
            except (OSError, RuntimeError, sqlite3.Error, subprocess.SubprocessError):
                pass  # Selection may have changed again while the hook was queued.
    elif args.command == 'install-view-hooks':
        print(json.dumps({'backup': install_view_hooks(resolve_socket())}))
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
    elif args.command == 'cycle':
        if len(args.args) == 2:
            with closing(db()) as con:
                cycle_category(con,socket(),args.args[1],args.args[0])
    elif args.command == 'observe':
        from codex_observer import observe
        try:
            observe()
        except Exception as exc:
            log_observer_error(exc)
            raise
    elif args.command == 'prune':
        prune_sidebars()
    elif args.command == 'web':
        host = '0.0.0.0'
        port = 7788
        fleet_only = False
        rest = list(args.args)
        if rest and rest[0] == '--fleet':
            fleet_only = True
            rest = rest[1:]
        if rest:
            if len(rest) == 1 and rest[0].isdigit():
                port = int(rest[0])
            else:
                host = rest[0]
                if len(rest) >= 2:
                    port = int(rest[1])
        serve_web(host, port, fleet_only=fleet_only)
    elif args.command == 'focus':
        window = args.args[0] if args.args else tmux('display-message','-p','#{window_id}')
        ensure_sidebars(window)
        for line in tmux('list-panes','-t',window,'-F','#{pane_id}\t#{@tmux_attn_sidebar}').splitlines():
            if line.endswith('\t1'):
                tmux('select-pane','-t',line.split('\t')[0])
    else:
        sock = resolve_socket()
        if args.command in ('status', 'status-clickable'):
            ensure_observer(sock)
        with closing(db()) as con:
            live = live_panes(sock=sock)
            rows, running = items(con, sock, live, args.command == 'dump')
            if args.command in ('status','status-clickable'):
                registered, _ = items(con, sock, live, True)
                update_window_badges(registered, live, sock=sock)
                print(status_text(status_groups(registered),args.command == 'status-clickable'))
            elif args.command == 'dump':
                print(json.dumps(rows, ensure_ascii=False, indent=2))
            elif rows:
                jump_and_acknowledge(con, rows[0], args.args[0] if args.args else None)


if __name__ == '__main__':
    main()
