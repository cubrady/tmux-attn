#!/usr/bin/env python3
"""Pane-scoped, event-driven tmux inbox. Python standard library only."""
from __future__ import annotations
import argparse
import base64
import binascii
import curses
import fcntl
import hashlib
import io
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing, nullcontext
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
import os
from pathlib import Path
import secrets
import shlex
import socket as socket_mod
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import unicodedata
import zipfile
import zlib
import xml.etree.ElementTree as ET
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


def normalize_preview(text):
    """Printable text with tidy blank lines and no length limit."""
    text = str(text or '').replace('\r\n', '\n').replace('\r', '\n')
    text = ''.join(c for c in text if c in '\n\t' or c.isprintable())
    return re.sub(r'\n{3,}', '\n\n', text).strip('\n')


def clip_preview(text, limit=PREVIEW_CHARS):
    """Trim preview text while keeping markdown structure (newlines / indents)."""
    text = normalize_preview(text)
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


def read_transcript(path, agent=''):
    """Raw (user messages, latest agent message) from a transcript. ([], '') if unreadable."""
    path = Path(path) if path else None
    if not path or not path.is_file():
        return [], ''
    try:
        parts = {p.lower() for p in path.parts}
        if 'rollout' in path.name or agent == 'codex':
            return parse_codex_transcript(path)
        if agent == 'cursor' or 'agent-transcripts' in parts:
            return parse_cursor_transcript(path)
        return parse_claude_style_transcript(path)
    except OSError:
        return [], ''


def conversation_preview(path, agent='', limit=PREVIEW_CHARS_FULL):
    """Last user intents + latest agent text from a transcript. Empty if unreadable."""
    users, agent_text = read_transcript(path, agent)
    users = [clip_preview(text, limit) for text in users[-PREVIEW_USERS:]]
    return {'users': users, 'agent': clip_preview(agent_text, limit) if agent_text else ''}


_MODEL_CACHE = {}
_MODEL_CACHE_LIMIT = 256
_CLAUDE_MODEL = re.compile(r'claude-(?:(?P<fam>opus|sonnet|haiku|fable)-(?P<ver>\d+(?:-\d{1,2})?)'
                           r'|(?P<ver2>\d+(?:-\d{1,2})?)-(?P<fam2>opus|sonnet|haiku))(?:-\d{8})?')


def _scan_model(lines, agent):
    model, effort = '', ''
    for line in lines:
        if agent == 'codex':
            if '"turn_context"' not in line:
                continue
        elif '"assistant"' not in line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if agent == 'codex':
            payload = row.get('payload') if isinstance(row.get('payload'), dict) else {}
            if row.get('type') == 'turn_context' and payload.get('model'):
                model, effort = str(payload['model']), str(payload.get('effort') or '')
        elif row.get('type') == 'assistant':
            message = row.get('message') if isinstance(row.get('message'), dict) else {}
            found = str(message.get('model') or '')
            if found and not found.startswith('<'):
                model = found
    return model, effort


def transcript_model(path, agent=''):
    """(model id, reasoning effort) last seen in a transcript; ('', '') when unknown.

    Only Claude and Codex transcripts record the model. The tail scan can miss it during a
    long turn, so the last value seen for a file is kept rather than blanking the badge.
    """
    key = str(path or '')
    try:
        stat_result = Path(key).stat()
    except OSError:
        return '', ''
    rollout = 'rollout' in Path(key).name or agent == 'codex'
    kind = 'codex' if rollout else agent
    if kind not in ('codex', 'claude'):
        return '', ''
    signature = (stat_result.st_mtime_ns, stat_result.st_size)
    cached = _MODEL_CACHE.get(key)
    if cached and cached[0] == signature:
        return cached[1], cached[2]
    try:
        model, effort = _scan_model(_iter_transcript_tail_lines(key), kind)
        if not model and not cached and stat_result.st_size > TRANSCRIPT_TAIL_BYTES:
            # Codex writes turn_context once per turn, so a long turn can push it past the tail.
            with open(key, errors='replace') as source:
                model, effort = _scan_model(source, kind)
    except OSError:
        return (cached[1], cached[2]) if cached else ('', '')
    if not model and cached:
        model, effort = cached[1], cached[2]
    if len(_MODEL_CACHE) >= _MODEL_CACHE_LIMIT:
        _MODEL_CACHE.clear()
    _MODEL_CACHE[key] = (signature, model, effort)
    return model, effort


def pane_model(con, row):
    """(model id, effort) for a tracked pane's transcript."""
    return transcript_model(resolve_transcript_path(con, row, row), row.get('agent') or '')


def model_label(model):
    """Short card label: 'claude-sonnet-5-5' -> 'sonnet 5.5'; unknown ids pass through."""
    model = clean(model, 80)
    match = _CLAUDE_MODEL.fullmatch(model)
    if not match:
        return model
    family = match.group('fam') or match.group('fam2')
    version = match.group('ver') or match.group('ver2')
    return f'{family} {version.replace("-", ".")}'


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
    # A new Claude session can bind its transcript before the file is created.
    if path and (row.get('agent') == 'claude' or Path(path).is_file()):
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
            and preview_revision == row['revision'] and bool(preview['agent'].strip()),
            'questions': [public_question(q) for q in pane_questions(con, item, meta, sock)]}, None


MAX_MESSAGE_CHARS = 200000


def full_message_for_pane(con, pane, pid, role, index, sig):
    """Unclipped text of one message the sheet shows. Returns (message dict, error).

    `index` counts within the last PREVIEW_USERS user messages; `sig` is the start of the text the
    sheet displayed, so a conversation that moved on is reported instead of showing a different message.
    """
    if role not in ('user', 'agent'):
        return None, 'role must be user or agent'
    sock = resolve_socket()
    meta = live_panes(sock).get((pane, pid))
    if not meta:
        return None, 'pane is not live'
    row = con.execute('SELECT * FROM panes WHERE socket=? AND pane=? AND pid=?', (sock, pane, pid)).fetchone()
    if not row:
        return None, 'pane is not tracked'
    item = dict(row) | meta
    try:
        cached_users = list(json.loads(row['preview_users'] if 'preview_users' in row.keys() else '[]') or [])
    except (TypeError, ValueError):
        cached_users = []
    cached_agent = (row['preview_agent'] if 'preview_agent' in row.keys() else '') or ''
    # A hook's final result can land before the transcript flushes, so the (clipped) cache is a second source.
    sources = [read_transcript(resolve_transcript_path(con, item, meta), item.get('agent') or ''),
               (cached_users, cached_agent)]
    for number, (users, agent_text) in enumerate(sources):
        # Same list the sheet builds: last messages, normalised, empties dropped.
        shown = [text for text in (normalize_preview(t) for t in users[-PREVIEW_USERS:] if str(t).strip()) if text]
        text = normalize_preview(agent_text) if role == 'agent' else (shown[index] if index < len(shown) else '')
        if text and text.startswith(sig):
            # Cached text was clipped for the sheet; say so, so the reader can ask the user to retry later.
            partial = number == 1 and text.endswith('…') and len(text) > PREVIEW_CHARS_FULL // 3
            return {'role': role, 'text': text[:MAX_MESSAGE_CHARS], 'chars': len(text),
                    'truncated': len(text) > MAX_MESSAGE_CHARS or partial, 'partial': partial}, None
    return None, 'content changed; refresh preview'


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


# Reserved for a visible, explicit wait on background work. A Stop hook's
# background_tasks only says those tasks exist; it does not mean the turn waits.
BACKGROUND_WAIT = 'Background work: waiting on '


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


MAX_BODY_BYTES = 256 * 1024
MAX_UPLOAD_BODY_BYTES = 12 * 1024 * 1024
MAX_UPLOAD_BYTES = 8 * 1024 * 1024
MAX_IMAGES = 4
UPLOAD_TTL_SECONDS = 7 * 24 * 3600
UPLOAD_MAX_FILES = 200
UPLOAD_MAX_TOTAL_BYTES = 256 * 1024 * 1024
TEXT_EXTENSIONS = ('txt', 'md', 'csv', 'tsv', 'json', 'log', 'xml', 'yaml', 'yml', 'ini', 'cfg', 'toml')
OFFICE_EXTENSIONS = ('doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx')
IMAGE_ID_RE = re.compile(r'^[0-9a-f]{32}\.(?:png|jpg|gif|webp)\Z')
FILE_ID_RE = re.compile(r'^[0-9a-f]{32}\.(?:' + '|'.join(TEXT_EXTENSIONS + OFFICE_EXTENSIONS) + r')\Z')
UPLOAD_ID_RE = re.compile(r'^[0-9a-f]{32}\.(?:png|jpg|gif|webp|' + '|'.join(TEXT_EXTENSIONS + OFFICE_EXTENSIONS) + r')\Z')
PEER_UPLOAD_TIMEOUT = 20
# A peer's 404 on a new route means that host has not been updated; say so instead of passing 'not found' on.
PEER_OUTDATED = 'peer is running an older tmux-attn without this feature'
# Claude Code reads a pasted image path asynchronously; give it a beat before Enter.
IMAGE_SETTLE_SECONDS = 0.4


def image_extension(data):
    """File extension for the image formats we accept, judged by magic bytes only."""
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'png'
    if data.startswith(b'\xff\xd8\xff'):
        return 'jpg'
    if data[:6] in (b'GIF87a', b'GIF89a'):
        return 'gif'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'webp'
    return ''


def upload_dir():
    return STATE / 'uploads'


def upload_path(image_id):
    """Resolve a server-issued attachment id to its file. Anything else is None."""
    if not isinstance(image_id, str) or not UPLOAD_ID_RE.match(image_id):
        return None
    path = upload_dir() / image_id
    return path if path.is_file() else None


def prune_uploads(now=None):
    """Drop uploads past their TTL, then the oldest ones beyond the file-count and total-byte caps."""
    now = time.time() if now is None else now
    entries = []
    try:
        for entry in os.scandir(upload_dir()):
            if entry.is_file():
                info = entry.stat()
                entries.append((info.st_mtime, info.st_size, entry.path))
    except OSError:
        return
    entries.sort()
    keep = [row for row in entries if now - row[0] <= UPLOAD_TTL_SECONDS]
    keep = keep[max(0, len(keep) - UPLOAD_MAX_FILES):]
    total = sum(row[1] for row in keep)
    while keep and total > UPLOAD_MAX_TOTAL_BYTES:
        total -= keep.pop(0)[1]
    survivors = {row[2] for row in keep}
    for _, _, path in entries:
        if path not in survivors:
            try:
                os.unlink(path)
            except OSError:
                pass


def document_extension(data, filename):
    """Allowlisted extension plus text encoding or Office container structure."""
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
    if ext in TEXT_EXTENSIONS:
        try:
            text = data.decode('utf-16' if data.startswith((b'\xff\xfe', b'\xfe\xff')) else 'utf-8-sig')
        except UnicodeError:
            return ''
        return ext if all(ch >= ' ' or ch in '\t\r\n\f' for ch in text) else ''
    if ext in ('docx', 'xlsx', 'pptx'):
        part, content_type = {
            'docx': ('word/document.xml', 'wordprocessingml.document'),
            'xlsx': ('xl/workbook.xml', 'spreadsheetml.sheet'),
            'pptx': ('ppt/presentation.xml', 'presentationml.presentation'),
        }[ext]
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                info = archive.getinfo('[Content_Types].xml')
                if (info.file_size > 128 * 1024 or part not in archive.namelist()
                        or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)):
                    return ''
                root = ET.fromstring(archive.read(info))
                expected = f'application/vnd.openxmlformats-officedocument.{content_type}.main+xml'
                for node in root.findall('{http://schemas.openxmlformats.org/package/2006/content-types}Override'):
                    if node.get('PartName') == '/' + part and node.get('ContentType') == expected:
                        return ext
        except (KeyError, ValueError, OSError, RuntimeError, EOFError, zlib.error, zipfile.BadZipFile, ET.ParseError):
            pass
    if ext in ('doc', 'xls', 'ppt'):
        names = {'doc': ('WordDocument',), 'xls': ('Workbook', 'Book'), 'ppt': ('PowerPoint Document',)}[ext]
        return ext if compound_has_stream(data, names) else ''
    return ''


def compound_has_stream(data, names):
    """Follow the compound file's FAT and directory, rather than searching file contents."""
    if len(data) < 512 or not data.startswith(b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1') or data[28:30] != b'\xfe\xff':
        return False
    major = int.from_bytes(data[26:28], 'little')
    shift = int.from_bytes(data[30:32], 'little')
    if (major, shift) not in ((3, 9), (4, 12)):
        return False
    size = 1 << shift
    count = len(data) // size - 1
    if len(data) % size or count < 2:
        return False

    def number(offset):
        return int.from_bytes(data[offset:offset + 4], 'little')

    def sector(index):
        if index >= count:
            raise ValueError('invalid sector')
        return data[(index + 1) * size:(index + 2) * size]

    try:
        fat_count = number(44)
        difat_count = number(72)
        if not 0 < fat_count <= count or difat_count > count:
            return False
        fat_ids = [number(pos) for pos in range(76, 512, 4)]
        next_difat = number(68)
        seen = set()
        for _ in range(difat_count):
            if next_difat in seen:
                return False
            seen.add(next_difat)
            block = sector(next_difat)
            fat_ids.extend(int.from_bytes(block[pos:pos + 4], 'little') for pos in range(0, size - 4, 4))
            next_difat = int.from_bytes(block[-4:], 'little')
        fat_ids = [index for index in fat_ids if index != 0xffffffff]
        if len(fat_ids) != fat_count or len(set(fat_ids)) != fat_count:
            return False
        fat = b''.join(sector(index) for index in fat_ids)
        directory = number(48)
        seen = set()
        found = False
        has_root = False
        while directory != 0xfffffffe:
            if directory in seen or directory * 4 + 4 > len(fat):
                return False
            seen.add(directory)
            block = sector(directory)
            for offset in range(0, size, 128):
                entry = block[offset:offset + 128]
                has_root |= entry[66] == 5
                for name in names:
                    encoded = (name + '\0').encode('utf-16le')
                    if entry[66] == 2 and int.from_bytes(entry[64:66], 'little') == len(encoded) and entry.startswith(encoded):
                        found = True
            directory = int.from_bytes(fat[directory * 4:directory * 4 + 4], 'little')
        return has_root and found
    except ValueError:
        return False


def store_upload(data, filename=''):
    """Persist validated attachment bytes under a server-chosen name."""
    kind = 'file' if filename else 'image'
    if not data:
        return None, f'empty {kind}'
    if len(data) > MAX_UPLOAD_BYTES:
        return None, f'{kind} too large'
    ext = document_extension(data, filename) if filename else image_extension(data)
    if not ext:
        return None, f'unsupported {kind} type'
    directory = upload_dir()
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    prune_uploads()
    image_id = f'{secrets.token_hex(16)}.{ext}'
    path = directory / image_id
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return image_id, None


def live_pane_meta(pane, pid, sock=None):
    return next((row for row in live_panes(sock).values() if row['pane'] == pane and row['pid'] == str(pid)), None)


def paste_to_pane(pane, content, sock=None):
    """Bracketed-paste content into a pane through a throwaway tmux buffer."""
    buf = f'tmux-attn-{os.getpid()}-{time.time_ns()}'
    subprocess.run(
        ['tmux'] + (['-S', sock] if sock else []) + ['load-buffer', '-b', buf, '-'],
        input=content.encode(), capture_output=True, timeout=3, check=True,
    )
    tmux('paste-buffer', '-b', buf, '-t', pane, '-p', '-d', sock=sock)


def has_control_chars(text, allowed=''):
    """ESC would end a bracketed paste early and reach the TUI as keys; a lone surrogate cannot be pasted at all."""
    return any(unicodedata.category(c) in ('Cc', 'Cs') and c not in allowed for c in text)


def send_to_pane(pane, pid, text, submit=True, sock=None, images=(), files=()):
    """Paste text and uploaded attachment paths after verifying pane_pid. Returns None on success.

    Each image goes in as its own paste of its bare absolute path, separated by a space:
    Claude Code and Codex turn that into an attachment, Pi keeps the path as text.
    Document paths remain text for the agent's file-reading tools.
    """
    text = str(text)
    if not text.strip() and not images and not files:
        return 'empty message'
    if len(text) > 8000:
        return 'message too long'
    if has_control_chars(text, '\n\r\t'):
        return 'message contains control characters'
    paths = []
    for image_id in images:
        path = upload_path(image_id)
        if not path:
            return 'image not found'
        paths.append(str(path))
    for file_id in files:
        path = upload_path(file_id)
        if not path:
            return 'file not found'
        paths.append(shlex.quote(str(path)))
    if not live_pane_meta(pane, pid, sock):
        return 'pane not found or pid mismatch'
    # Verify again at paste time — pane ids are reused.
    actual = tmux('display-message', '-p', '-t', pane, '#{pane_pid}', check=False, sock=sock)
    if actual != str(pid):
        return 'pane pid changed'
    try:
        pasted = False
        if text.strip():
            paste_to_pane(pane, text, sock)
            pasted = True
        for path in paths:
            if pasted:
                tmux('send-keys', '-t', pane, 'Space', sock=sock)
            paste_to_pane(pane, path, sock)
            pasted = True
        if submit and (paths or not text.endswith('\n')):
            if paths:
                time.sleep(IMAGE_SETTLE_SECONDS)
            tmux('send-keys', '-t', pane, 'Enter', sock=sock)
    except (RuntimeError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        return f'paste failed: {exc}'
    return None


# Questions an agent is waiting on (Codex request_user_input_async, Claude AskUserQuestion).
# The transcript supplies the wording; the pane's screen decides what is still answerable.

QUESTION_SCAN_BYTES = 64 * 1024 * 1024
QUESTION_MATCH_CHARS = 24
QUESTION_POLL_SECONDS = 0.1
QUESTION_WAIT_SECONDS = 2.5
CODEX_QUESTION_MARKERS = (b'request_user_input_async', b'send_user_message_question_reply',
                          b'"task_complete"', b'"turn_aborted"')
CODEX_REPLY_RE = re.compile(r'<send_user_message_question_reply>\s*(.*?)\s*</send_user_message_question_reply>', re.S)
# One writer at a time: a reply must not interleave with the keys an answer is sending.
PANE_INPUT_LOCK = threading.Lock()


def transcript_rows(path, wanted, limit=QUESTION_SCAN_BYTES):
    """JSON rows from the transcript tail whose raw line passes wanted(line); skips parsing the rest."""
    path = Path(path)
    size = path.stat().st_size
    with path.open('rb') as raw:
        if size > limit:
            raw.seek(size - limit)
            raw.readline()
        for line in raw:
            if wanted(line):
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    yield row


def codex_question_id(call_id, index):
    """Codex's own question identity, as echoed back in the answer envelope."""
    return json.dumps(['request_user_input_async', call_id, index], separators=(',', ':'))


def codex_pending_questions(path):
    """request_user_input_async questions in the current turn that have no delivered answer."""
    pending = {}
    for row in transcript_rows(path, lambda line: any(m in line for m in CODEX_QUESTION_MARKERS)):
        payload = row.get('payload') if isinstance(row.get('payload'), dict) else {}
        kind = payload.get('type')
        if kind in ('task_complete', 'turn_aborted'):
            # Codex drops unanswered async questions when the turn ends.
            pending.clear()
        elif kind == 'function_call' and payload.get('name') == 'request_user_input_async':
            try:
                questions = json.loads(payload.get('arguments') or '{}').get('questions') or []
            except (ValueError, AttributeError):
                continue
            call_id = str(payload.get('call_id') or '')
            for index, question in enumerate(questions if isinstance(questions, list) else []):
                title = str(question.get('title') or '').strip() if isinstance(question, dict) else ''
                if not call_id or not title:
                    continue
                options = question.get('options') if isinstance(question.get('options'), list) else []
                qid = codex_question_id(call_id, index)
                pending[qid] = {
                    'id': qid, 'group': qid, 'title': clip_preview(title, PREVIEW_CHARS_FULL), 'header': '',
                    'options': [{'label': clip_preview(str(o), 512), 'description': ''} for o in options[:32]],
                    'multi': False, 'free_text': True, '_title': title,
                }
        elif kind == 'message' and payload.get('role') == 'user':
            for match in CODEX_REPLY_RE.finditer(content_to_text(payload.get('content'))):
                try:
                    replies = json.loads(match.group(1))
                except ValueError:
                    continue
                for reply in replies if isinstance(replies, list) else []:
                    if isinstance(reply, dict):
                        pending.pop(str(reply.get('questionItemId') or ''), None)
    return list(pending.values())


def claude_pending_questions(path):
    """AskUserQuestion calls in a Claude transcript that have no tool_result yet, one entry per question."""
    pending = {}

    def wanted(line):
        return b'"AskUserQuestion"' in line or (b'"tool_result"' in line and any(key.encode() in line for key in pending))

    for row in transcript_rows(path, wanted):
        message = row.get('message') if isinstance(row.get('message'), dict) else {}
        for block in message.get('content') if isinstance(message.get('content'), list) else []:
            if not isinstance(block, dict):
                continue
            if block.get('type') == 'tool_result':
                pending.pop(str(block.get('tool_use_id') or ''), None)
            elif block.get('type') == 'tool_use' and block.get('name') == 'AskUserQuestion' and block.get('id'):
                questions = (block.get('input') or {}).get('questions') if isinstance(block.get('input'), dict) else None
                group = str(block['id'])
                entries = []
                for index, question in enumerate(questions if isinstance(questions, list) else []):
                    title = str(question.get('question') or '').strip() if isinstance(question, dict) else ''
                    options = [o for o in question.get('options') or [] if isinstance(o, dict)] if title else []
                    if not title or not options:
                        entries = []
                        break
                    multi = bool(question.get('multiSelect'))
                    # Options with a preview change the layout: digits only move the cursor, no free-text row.
                    previews = any(o.get('preview') for o in options)
                    entries.append({
                        'id': f'{group}:{index}', 'group': group, 'title': clip_preview(title, PREVIEW_CHARS_FULL),
                        'header': clean(question.get('header') or '', 40),
                        'options': [{'label': clip_preview(str(o.get('label') or ''), 512),
                                     'description': clip_preview(str(o.get('description') or ''), 512)} for o in options],
                        'multi': multi, 'free_text': not multi and not previews, '_title': title, '_previews': previews,
                    })
                if entries:
                    pending[group] = entries
    return [question for entries in pending.values() for question in entries]


def squash(text):
    # Drop whitespace and markdown marks so transcript text matches how a TUI wrapped and styled it.
    return re.sub(r'[\s`*]+', '', str(text or ''))


def keyed_questions(questions):
    """Give each question the shortest title prefix (at least QUESTION_MATCH_CHARS) that no other one starts with.

    Screens are matched on these keys; a question that cannot be told apart gets '' and is not answered from the web.
    """
    titles = [squash(q['_title']) for q in questions]
    keyed = []
    for i, (question, title) in enumerate(zip(questions, titles)):
        shared = max((len(os.path.commonprefix([title, other])) for j, other in enumerate(titles) if j != i), default=0)
        keyed.append(question | {'_key': title[:max(QUESTION_MATCH_CHARS, shared + 1)] if shared < len(title) else ''})
    return keyed


def pasted_into(before, after, text):
    """True once the pasted text shows up more often than before the paste (it may already be an option or title)."""
    text = squash(text)
    return any(after.count(part) > before.count(part) for part in (text[:QUESTION_MATCH_CHARS], text[-QUESTION_MATCH_CHARS:]))


def codex_queued_answers(screen):
    """Answers the TUI accepted but has not yet handed to the model ("↳ > question" rows)."""
    return [squash(line.split('↳ >', 1)[1]) for line in screen.splitlines() if line.lstrip().startswith('↳ >')]


def codex_visible_questions(questions, screen):
    # A queued row shows the title, cut with "…" when the terminal is narrow.
    queued = [text.removesuffix('…') for text in codex_queued_answers(screen)]
    return [q for q in questions if not any(text and squash(q['_title']).startswith(text) for text in queued)]


def codex_panel(screen):
    """(position, total, text) of Codex's expanded question panel, or None when it is not open."""
    lines = screen.rstrip('\n').split('\n')
    start = max((i + 1 for i, line in enumerate(lines) if line[:1] not in ('', ' ')), default=0)
    lines = lines[start:]
    if not any('enter submit' in line for line in [l for l in lines if l.strip()][-3:]):
        return None
    text = '\n'.join(lines)
    # The text returned starts at the question title.
    position = re.search(r'^\s*(\d+) of (\d+)\s*$', text, re.M)
    return (int(position[1]), int(position[2]), text[position.end():]) if position else (1, 1, text)


def codex_collapsed(screen):
    return bool(re.search(r'^\s*\?\s+\d+ questions?\s*$', screen, re.M)) and bool(re.search(r'^\s*⇧← to answer\s*$', screen, re.M))


class PaneKeys:
    """The few tmux operations an answer needs, kept apart so tests can drive a fake TUI."""

    def __init__(self, pane, sock=None):
        self.pane, self.sock = pane, sock

    def screen(self):
        return tmux('capture-pane', '-p', '-J', '-t', self.pane, check=False, sock=self.sock)

    def keys(self, *keys):
        tmux('send-keys', '-t', self.pane, *keys, sock=self.sock)

    def paste(self, text):
        paste_to_pane(self.pane, text, self.sock)

    def wait(self, ready, seconds=None):
        """Poll the screen until ready(screen) is truthy; returns the last screen and the result."""
        deadline = time.monotonic() + (QUESTION_WAIT_SECONDS if seconds is None else seconds)
        while True:
            screen = self.screen()
            result = ready(screen)
            if result or time.monotonic() >= deadline:
                return screen, result
            time.sleep(QUESTION_POLL_SECONDS)


def codex_close_panel(driver, screen):
    """Collapse the panel with ⇧→ (Esc would interrupt the turn); best effort, stops once a press changes nothing."""
    def state(s):
        # The narrow-terminal notice replaces the panel footer while it shows.
        return codex_panel(s), 'Expand terminal' in s

    for _ in range(40):
        before = state(screen)
        if before == (None, False):
            return
        driver.keys('S-Right')
        screen, moved = driver.wait(lambda s: state(s) != before)
        if not moved:
            return


def answer_codex_question(driver, question, option=None, text=None):
    """Open Codex's queued question panel, find the question, answer it, and confirm it left the panel."""
    def shows(screen):
        panel = codex_panel(screen)
        return bool(panel) and squash(panel[2]).startswith(question['_key'])

    def move(screen, key, step):
        # Wait for the new position itself: pressing again on a stale screen could overshoot the last question.
        target = codex_panel(screen)[0] + step
        driver.keys(key)
        return driver.wait(lambda s: (codex_panel(s) or (0,))[0] == target)

    if option is not None and option >= 9:
        return '選項超過 9 個，請在終端機回答'
    screen = driver.screen()
    if not codex_panel(screen):
        if not codex_collapsed(screen):
            return 'Codex 畫面上沒有待回答的問題（可能已回答或該輪已結束）'
        driver.keys('S-Left')
        screen, opened = driver.wait(codex_panel)
        if not opened:
            return '無法開啟 Codex 的問題面板'
    # Walk back to the first question, then forward. ⇧← past the last one would open a queued message instead.
    while codex_panel(screen)[0] > 1:
        screen, moved = move(screen, 'S-Right', -1)
        if not moved:
            codex_close_panel(driver, screen)
            return 'Codex 的問題面板沒有回應，已停止'
    while not shows(screen):
        panel = codex_panel(screen)
        if panel[0] >= panel[1]:
            codex_close_panel(driver, screen)
            return '找不到這個問題（可能已回答、略過或已失效）'
        screen, moved = move(screen, 'S-Left', 1)
        if not moved:
            codex_close_panel(driver, screen)
            return 'Codex 的問題面板沒有回應，已停止'
    if option is not None:
        driver.keys(str(option + 1))
    else:
        before = squash(codex_panel(screen)[2])
        driver.paste(text)
        screen, pasted = driver.wait(lambda s: shows(s) and pasted_into(before, squash(codex_panel(s)[2]), text))
        if not pasted:
            # Enter now would submit the preselected option instead of the text.
            codex_close_panel(driver, screen)
            return 'Codex 沒有顯示貼上的回答，已取消送出'
        driver.keys('Enter')
    screen, gone = driver.wait(lambda s: not shows(s) and 'Expand terminal' not in s)
    if not gone:
        flash = 'Expand terminal' in screen
        codex_close_panel(driver, screen)
        return ('終端機太小，Codex 未完整顯示選項而拒絕送出；請放大該 pane 後再試' if flash
                else 'Codex 沒有接受這個回答')
    codex_close_panel(driver, screen)
    return None


CLAUDE_TABS_RE = re.compile(r'^\s*(?:←\s+)?[☐☒]\s+\S')
CLAUDE_OPTION_RE = re.compile(r'^\s*(?:❯\s*)?(\d+)\.\s')


def claude_dialog(screen):
    """Claude's AskUserQuestion dialog at the bottom of the screen: a question tab or the review step."""
    lines = screen.rstrip('\n').split('\n')
    header = max((i for i, line in enumerate(lines) if CLAUDE_TABS_RE.match(line)), default=None)
    if header is None:
        return None
    body = lines[header + 1:]
    shown = [line.strip() for line in body if line.strip()]
    if not shown:
        return None
    footer = shown[-1]
    if footer.startswith('Enter to select') and footer.endswith('Esc to cancel'):
        question = []
        for line in body:
            if CLAUDE_OPTION_RE.match(line):
                break
            question.append(line)
        checked = {int(m[1]) - 1: m[2] != ' ' for m in
                   (re.match(r'^\s*(?:❯\s*)?(\d+)\.\s+\[(.)\]', line) for line in body) if m}
        cursor = next((int(m[1]) - 1 for m in (re.match(r'^\s*❯\s*(\d+)\.\s', line) for line in body) if m), None)
        return {'kind': 'question', 'title': squash('\n'.join(question)), 'body': '\n'.join(body),
                'footer': footer, 'checked': checked, 'cursor': cursor}
    if 'Ready to submit your answers?' in shown and any(re.match(r'^❯\s*1\.\s*Submit answers$', line) for line in shown):
        return {'kind': 'review', 'title': '', 'body': '\n'.join(body), 'footer': footer, 'checked': {}, 'cursor': None}
    return None


def claude_review(body):
    """[(question, answer)], squashed, from Claude's "Review your answers" step."""
    text = squash(body.split('Ready to submit your answers?')[0])
    return [tuple(item.partition('→')[::2]) for item in text.split('●')[1:]]


def answer_claude_questions(driver, questions, answers):
    """Answer one AskUserQuestion call tab by tab, confirming each step on screen; never sends Esc."""
    def on(screen, question):
        dialog = claude_dialog(screen)
        return bool(dialog) and dialog['kind'] == 'question' and dialog['title'].startswith(question['_key'])

    # Digits pick options; a two-digit number would arrive as two keys.
    if any(len(question['options']) > 8 for question in questions):
        return '選項太多，請在終端機回答'
    screen = driver.screen()
    if not claude_dialog(screen):
        return 'Claude 畫面上沒有開啟中的問題'
    # The dialog may sit on a later tab or the review step; ← moves back without losing answers.
    for _ in range(len(questions) + 1):
        before = claude_dialog(screen)
        if on(screen, questions[0]) or not before or (before['kind'] == 'question' and len(questions) == 1):
            break
        driver.keys('Left')
        screen, _ = driver.wait(lambda s: claude_dialog(s) not in (None, before))
    if not on(screen, questions[0]):
        return 'Claude 對話框顯示的不是這組問題'
    for question, answer in zip(questions, answers):
        if not on(screen, question):
            return 'Claude 對話框沒有切到預期的問題，已停止'
        before = claude_dialog(screen)
        if question['multi']:
            for index in range(len(question['options'])):
                if not on(screen, question):
                    return 'Claude 對話框沒有切到預期的問題，已停止'
                if claude_dialog(screen)['checked'].get(index, False) != (index in answer['options']):
                    driver.keys(str(index + 1))
                    screen, flipped = driver.wait(
                        lambda s, i=index: on(s, question) and claude_dialog(s)['checked'].get(i, False) == (i in answer['options']))
                    if not flipped:
                        return 'Claude 沒有切換這個選項，已停止'
            driver.keys('Tab')
        elif question['_previews']:
            driver.keys(str(answer['option'] + 1))
            screen, moved = driver.wait(lambda s: on(s, question) and claude_dialog(s)['cursor'] == answer['option'])
            if not moved:
                return 'Claude 沒有選到這個選項，已停止'
            driver.keys('Enter')
        elif answer.get('text') is None:
            driver.keys(str(answer['option'] + 1))
        else:
            driver.keys(str(len(question['options']) + 1))
            screen, focused = driver.wait(lambda s: on(s, question) and 'ctrl+g to edit in nano' in claude_dialog(s)['footer'])
            if not focused:
                return 'Claude 沒有進入自訂回答欄位，已停止'
            shown = squash(claude_dialog(screen)['body'])
            driver.paste(answer['text'])
            screen, typed = driver.wait(
                lambda s: on(s, question) and pasted_into(shown, squash(claude_dialog(s)['body']), answer['text']))
            if not typed:
                return 'Claude 沒有顯示貼上的回答，已停止'
            driver.keys('Enter')
        screen, _ = driver.wait(lambda s: claude_dialog(s) != before and not on(s, question))
    dialog = claude_dialog(screen)
    if dialog and dialog['kind'] == 'review':
        # "2" here would cancel the whole call, so confirm the summary before choosing "1. Submit answers".
        review = claude_review(dialog['body'])
        for question, answer in zip(questions, answers):
            labels = [question['options'][i]['label'] for i in answer.get('options') or [answer.get('option')] if i is not None]
            expected = squash(answer['text'] if answer.get('text') is not None else ', '.join(labels))
            if not any(title.startswith(question['_key']) and given == expected for title, given in review):
                return 'Claude 確認頁的答案與選擇不符，未送出'
        driver.keys('1')
        screen, _ = driver.wait(lambda s: claude_dialog(s) is None)
        dialog = claude_dialog(screen)
    if dialog:
        return 'Claude 沒有接受這組回答，對話框仍開著'
    return None


QUESTION_PARSERS = {'codex': codex_pending_questions, 'claude': claude_pending_questions}
MAX_ANSWER_CHARS = 2000


def pane_questions(con, row, meta, sock=None):
    """Answerable questions for a waiting Codex/Claude pane, with their internal matching fields."""
    parser = QUESTION_PARSERS.get(row.get('agent'))
    if row.get('state') != 'WAIT' or not parser:
        return []
    path = resolve_transcript_path(con, row, meta)
    if not path:
        return []
    try:
        questions = parser(path)
    except (OSError, ValueError):
        return []
    if questions and row['agent'] == 'codex':
        questions = codex_visible_questions(questions, PaneKeys(row['pane'], sock).screen())
    return keyed_questions(questions)


def public_question(question):
    return {key: value for key, value in question.items() if not key.startswith('_')}


def normalize_answer(question, answer):
    """One validated answer: exactly one of option, options (multi-select) or text. Returns (answer, error)."""
    if not isinstance(answer, dict) or answer.get('id') != question['id']:
        return None, 'answers do not match the questions'
    given = [key for key in ('option', 'options', 'text') if answer.get(key) is not None]
    if len(given) != 1:
        return None, 'each answer needs exactly one of option, options or text'
    count = len(question['options'])
    if given == ['option']:
        option = answer['option']
        if question['multi'] or type(option) is not int or not 0 <= option < count:
            return None, 'invalid option'
        return {'option': option}, None
    if given == ['options']:
        options = answer['options']
        if (not question['multi'] or not isinstance(options, list) or not options
                or not all(type(i) is int and 0 <= i < count for i in options) or len(set(options)) != len(options)):
            return None, 'invalid options'
        return {'options': sorted(options)}, None
    text = answer['text']
    if not question['free_text'] or not isinstance(text, str):
        return None, 'this question does not take a typed answer'
    # One line: a pasted newline reaches the TUI as Enter.
    text = ' '.join(text.split())
    if not text or len(text) > MAX_ANSWER_CHARS:
        return None, 'answer is empty or too long'
    if has_control_chars(text):
        return None, 'answer contains control characters'
    # The screen checks look for the answer with whitespace and markdown marks removed.
    if not squash(text):
        return None, 'answer is empty or too long'
    return {'text': text}, None


def answer_pane_questions(pane, pid, group, answers, sock=None):
    """Validate answers against the pane's pending questions, then drive its TUI. Returns None on success."""
    sock = sock or resolve_socket()
    meta = live_pane_meta(pane, pid, sock)
    if not meta:
        return 'pane not found or pid mismatch'
    with closing(db()) as con:
        row = con.execute('SELECT * FROM panes WHERE socket=? AND pane=? AND pid=?', (sock, pane, pid)).fetchone()
        if not row:
            return 'pane is not tracked'
        row = dict(row) | meta
        questions = [q for q in pane_questions(con, row, meta, sock) if q['group'] == group]
    if not questions:
        return '找不到這個問題（可能已回答或已失效）'
    if not isinstance(answers, list) or len(answers) != len(questions):
        return 'answers do not match the questions'
    if not all(q['_key'] for q in questions):
        return '有其他問題的開頭與這題相同，請在終端機回答'
    normalized = []
    for question, answer in zip(questions, answers):
        value, error = normalize_answer(question, answer)
        if error:
            return error
        normalized.append(value)
    if not PANE_INPUT_LOCK.acquire(timeout=QUESTION_WAIT_SECONDS * 4):
        return 'another answer is still being sent'
    try:
        # Verify again at key time — pane ids are reused.
        if tmux('display-message', '-p', '-t', pane, '#{pane_pid}', check=False, sock=sock) != str(pid):
            return 'pane pid changed'
        driver = PaneKeys(pane, sock)
        if row['agent'] == 'codex':
            return answer_codex_question(driver, questions[0], option=normalized[0].get('option'),
                                         text=normalized[0].get('text'))
        return answer_claude_questions(driver, questions, normalized)
    except (RuntimeError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        return f'tmux failed: {exc}'
    finally:
        PANE_INPUT_LOCK.release()


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
                clear_session = (agent == 'claude' and event in ('SessionStart', 'session_start')
                                 and data.get('source') == 'clear')
                if event in ('SessionEnd','session_shutdown','sessionEnd') and existing:
                    if existing['needs_review']:
                        # Keep an unread completed response after the agent exits.
                        state = None
                    if existing['state'] in ('RUNNING','WAIT'):
                        state = ('IDLE', 'Agent exited; review current work', True)
                if clear_session or event not in ('SessionStart','session_start','sessionStart') or not existing:
                    if state:
                        recorded_revision = record(con, sock, pane, pid, agent, *state,event_key=(data.get('generation_id','') if agent == 'cursor' else data.get('turn_id','')))
                user_text = hook_user_text(data) if event in (
                    'UserPromptSubmit', 'beforeSubmitPrompt', 'agent_start',
                ) else ''
                agent_text = str(data.get('last_assistant_message') or '').strip() if event in (
                    'Stop', 'stop', 'agent_settled', 'StopFailure',
                ) else ''
                transcript = data.get('transcript_path') or ''
                if clear_session and recorded_revision is not None:
                    # Clearing starts a new conversation, even if its transcript is still empty.
                    with con:
                        # record() leaves the revision alone when the state did not change, so bump it here:
                        # a poll that read the row before the reset must not be able to write the old cache back.
                        con.execute('''UPDATE panes SET preview_users='[]',preview_agent='',preview_mtime=0,
                            preview_revision=0,transcript_path=?,revision=revision+1
                            WHERE socket=? AND pane=? AND pid=? AND revision=?''',
                            (clean(transcript, 500), sock, pane, pid, recorded_revision))
                elif user_text or agent_text or transcript:
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
        item['unread'] = is_unread(item)
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
STATUS_EMOJI = dict(STATUS_GROUPS) | {'shell': '💻'}
# Running by proxy: counts and navigation stay in 🏃, only the icon tells them apart.
BACKGROUND_WAIT_EMOJI = '⏳'
STATUS_ORDER = {key: index for index, (key, _) in enumerate(STATUS_GROUPS)}


def is_unread(row):
    reviewable = row['state'] == 'WAIT' or row.get('needs_review', row.get('unread', row.get('pending', False)))
    if not reviewable:
        return False
    revision = row.get('revision')
    if isinstance(revision, int) and revision > 0:
        return revision > (row.get('acknowledged') or 0)
    return bool(row.get('unread', row.get('pending', not row.get('reviewed', False))))


def attention_category(row):
    """One policy for counts, icons, lists and navigation; viewing keeps history."""
    if not row.get('tracked', True) or row['state'] == 'SHELL':
        return 'shell'
    if row['state'] == 'WAIT':
        return 'wait'
    if row['state'] == 'RUNNING':
        return 'running'
    return 'review' if is_unread(row) else 'idle'


def status_groups(rows):
    groups = {key:[] for key,_ in STATUS_GROUPS}
    for row in rows:
        category = attention_category(row)
        if category in groups:
            groups[category].append(row)
    for group in groups.values():
        # Keep stable navigation for working/waiting panes; review follows recency below.
        group.sort(key=lambda row:(row['session'],int(row['window_id'][1:]),int(row['pane'][1:])))
    groups['review'].sort(key=lambda row: (-float(row.get('updated') or 0), row['pane']))
    return groups


def status_text(groups, clickable=False):
    labels = []
    for index,(key,emoji) in enumerate(STATUS_GROUPS):
        count = len(groups[key])
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
    categories = {attention_category(row) for row in rows}
    badge = next((emoji for key, emoji in STATUS_GROUPS if key in categories), '')
    # A window reads ⏳ only when every running agent in it is parked; real work keeps 🏃.
    if badge == STATUS_EMOJI['running'] and all(background_wait(row) for row in rows if attention_category(row) == 'running'):
        return BACKGROUND_WAIT_EMOJI
    return badge


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


# tmux window labels stay compact; a web card title has room for much more.
WEB_LABEL_CHARS = 28


def project_labels(paths, aliases, limit=12):
    labels = {}
    for path in sorted(set(paths)):
        name = clean(Path(path).name)
        labels[path] = aliases.get(path) or aliases.get(name) or (name if len(name) <= limit else name[:limit-1]+'…')
    duplicates = {label for label in labels.values() if list(labels.values()).count(label) > 1}
    for path, label in list(labels.items()):
        if label in duplicates:
            suffix = clean(Path(path).name).rsplit('-', 1)[-1]
            candidate = label[:max(1, limit-1-len(suffix))]+'-'+suffix
            # Prefer a readable suffix; use the full path when names are identical.
            if len(candidate) <= limit and suffix not in label and candidate not in labels.values():
                labels[path] = candidate
            else:
                digest = hashlib.sha256(path.encode()).hexdigest()
                for length in range(4, limit-1):
                    candidate = label[:limit-1-length]+'-'+digest[:length]
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


def background_wait(row):
    """Running only by proxy: parked on background work. Peers relay this as a flag, not a reason."""
    keys = row.keys()
    return bool('background_wait' in keys and row['background_wait']) or (
        row['state'] == 'RUNNING' and str(row['reason'] or '').startswith(BACKGROUND_WAIT))


def agent_emoji(row):
    category = attention_category(row)
    return BACKGROUND_WAIT_EMOJI if category == 'running' and background_wait(row) else STATUS_EMOJI[category]


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
    # Negate updated so the most recently completed / active pane floats up.
    fresh = -float(row.get('updated') or 0)
    return (STATUS_ORDER.get(attention_category(row), len(STATUS_GROUPS)), fresh, host, row['pane'])


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


def attention_summary(rows):
    summary = empty_summary()
    for row in rows:
        category = attention_category(row)
        if category == 'shell':
            summary['untracked'] += 1
        else:
            summary[category] += 1
            summary['tracked'] += 1
    return summary


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
        item['category'] = attention_category(item)
        item['emoji'] = agent_emoji(item)
        agents.append(item)
    # Reclassify older peers too; their summary may still count viewed results as review.
    return agents, attention_summary(agents)


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
    peers = load_peers()
    # Start peer I/O before the local tmux/DB scan so the two waits overlap.
    with ThreadPoolExecutor(max_workers=max(1, min(8, len(peers)))) as pool:
        futures = {pool.submit(fetch_peer_agents, peer['url'], peer_timeout): peer for peer in peers}
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
                hosts.append({'name': host, 'ok': True, 'source': 'local', 'version': install_version()})
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
        if futures:
            for future in as_completed(futures):
                peer = futures[future]
                cache_key = ('peer', peer['name'], peer['url'])
                try:
                    payload = future.result()
                    tagged, summary = tag_host(payload, peer['name'])
                    _remember_fleet_host(cache_key, now, tagged, summary)
                    agents.extend(tagged)
                    summaries.append(summary)
                    hosts.append({'name': peer['name'], 'ok': True, 'source': 'peer', 'url': peer['url'],
                                  'version': str(payload.get('version') or '')})
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
        'version': install_version(),
    }


def web_later_db():
    """Web-only grouping; no pane event, tmux status, or hook reads this database."""
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    con = sqlite3.connect(STATE / 'web-later.sqlite3', timeout=3)
    try:
        con.execute('''CREATE TABLE IF NOT EXISTS later (
            host TEXT NOT NULL, pane TEXT NOT NULL, pid TEXT NOT NULL,
            revision INTEGER NOT NULL, PRIMARY KEY(host,pane,pid))''')
        return con
    except Exception:
        con.close()
        raise


def web_later_key(agent):
    return (str(agent.get('host') or ''), str(agent.get('pane') or ''), str(agent.get('pid') or ''))


def web_later_fleet(fleet):
    """Mark live cards and release them when their agent changes or their pane closes."""
    agents = {web_later_key(agent): agent for agent in fleet['agents'] if agent.get('tracked')}
    online = {host['name'] for host in fleet['hosts'] if host.get('ok')}
    with closing(web_later_db()) as con, con:
        saved = con.execute('SELECT host,pane,pid,revision FROM later').fetchall()
        for host, pane, pid, revision in saved:
            agent = agents.get((host, pane, pid))
            current_revision = agent.get('revision') if agent else 0
            if host in online and (agent is None or agent.get('state') != 'IDLE'
                                   or agent.get('unread')
                                   or (isinstance(current_revision, int) and current_revision > revision)):
                con.execute('DELETE FROM later WHERE host=? AND pane=? AND pid=?', (host, pane, pid))
            elif agent is not None:
                agent['later'] = True
    return fleet


def dispatch_web_later(payload, include_local=True):
    """Move a viewed completion into/out of the Web-only later group."""
    if not isinstance(payload, dict):
        return 400, {'ok': False, 'error': 'invalid json'}
    host, pane, pid = (payload.get(key) for key in ('host', 'pane', 'pid'))
    action = payload.get('action')
    if any(not isinstance(value, str) or not value or len(value) > 240 for value in (host, pane, pid)):
        return 400, {'ok': False, 'error': 'host, pane, and pid are required'}
    if action not in ('add', 'remove'):
        return 400, {'ok': False, 'error': 'action must be add or remove'}
    if action == 'remove':
        with closing(web_later_db()) as con, con:
            con.execute('DELETE FROM later WHERE host=? AND pane=? AND pid=?', (host, pane, pid))
        return 200, {'ok': True, 'later': False}
    revision = payload.get('revision')
    if type(revision) is not int or revision < 1:
        return 400, {'ok': False, 'error': 'positive integer revision required'}
    fleet = fleet_payload(include_local=include_local)
    if host not in {row['name'] for row in fleet['hosts'] if row.get('ok')}:
        return 409, {'ok': False, 'error': 'host is offline'}
    agent = next((row for row in fleet['agents'] if web_later_key(row) == (host, pane, pid)), None)
    if (not agent or not agent.get('tracked') or agent.get('state') != 'IDLE'
            or not agent.get('needs_review') or not agent.get('reviewed')
            or agent.get('unread') or agent.get('revision') != revision):
        return 409, {'ok': False, 'error': 'session is no longer a viewed completion'}
    with closing(web_later_db()) as con, con:
        con.execute('INSERT OR REPLACE INTO later(host,pane,pid,revision) VALUES(?,?,?,?)',
                    (host, pane, pid, revision))
    return 200, {'ok': True, 'later': True}


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
    tracked_keys = {(row['pane'], row['pid']) for row in rows}
    paths = [row['path'] for row in rows if row.get('path')]
    paths += [meta['path'] for key, meta in live.items() if key not in tracked_keys and meta.get('path')]
    labels = project_labels(paths, load_project_aliases(), limit=WEB_LABEL_CHARS)
    agents = []
    for row in rows:
        reviewed = row['revision'] <= row['acknowledged'] and (row['state'] == 'WAIT' or row['needs_review'])
        command = row.get('command') or ''
        preview = pane_conversation_preview(con, row, row)
        model, effort = pane_model(con, row)
        env = git_workdir_info(row.get('path', ''))
        agents.append({
            'pane': row['pane'],
            'pid': row['pid'],
            'session': row['session'],
            'window': row['window'],
            'window_id': row.get('window_id', ''),
            'agent': row['agent'],
            'cli': cli_label(row['agent']),
            'model': model,
            'model_label': model_label(model),
            'effort': effort,
            'command': command,
            'tracked': True,
            'project': labels.get(row['path'], Path(row['path']).name if row.get('path') else ''),
            'path': row.get('path', ''),
            'repo': env['repo'],
            'branch': env['branch'],
            'dirty': bool(env['dirty']),
            'state': row['state'],
            'category': attention_category(row),
            'pending': bool(row['pending']),
            'needs_review': bool(row['needs_review']),
            'unread': bool(row['unread']),
            'reviewed': bool(row['reviewed']),
            'revision': row['revision'],
            'acknowledged': row['acknowledged'],
            'emoji': agent_emoji(row),
            'background_wait': background_wait(row),
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
            'model': '',
            'model_label': '',
            'effort': '',
            'command': command,
            'tracked': False,
            'project': labels.get(meta['path'], Path(meta['path']).name if meta.get('path') else ''),
            'path': meta.get('path', ''),
            'repo': env['repo'],
            'branch': env['branch'],
            'dirty': bool(env['dirty']),
            'state': 'SHELL',
            'category': 'shell',
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
        'summary': attention_summary(agents),
        'agents': agents,
    }


def grouped_rows(rows):
    return sorted(rows, key=agent_sort_key)


def display_reason(item, reviewed):
    if item['state'] == 'WAIT' and item['reason'].startswith('Queued follow-up questions: '):
        count = item['reason'].split(': ',1)[1]
        return f'有 {count} 個問題等你回答（agent 仍可繼續工作）'
    if item['state'] == 'IDLE':
        if reviewed:
            return '已完成，已查看'
        if item['needs_review']:
            if 'question' in item['reason'].lower():
                return '回覆以問號結尾，建議查看（非確定等待）'
            outcome = '已中斷' if 'Interrupted' in item['reason'] else '發生錯誤' if 'error' in item['reason'] else '已完成'
            return outcome + '，尚未查看'
        return '閒置，等待新任務'
    if item['state'] == 'RUNNING':
        if item['reason'].startswith(BACKGROUND_WAIT):
            return f'等待背景工作（{item["reason"][len(BACKGROUND_WAIT):]}）· 可先看進度'
        return '正在工作'
    reason = item['reason']
    if 'Approval' in reason or 'approval' in reason:
        return '需要批准指令或操作 · ' + reason
    return '需要回答或下判斷 · ' + reason


def inbox_lines(rows):
    lines = []
    groups = status_groups(rows)
    titles = {'wait': '等待回覆', 'review': '待看', 'running': '工作進行中', 'idle': '閒置'}
    for key, emoji in STATUS_GROUPS:
        title, group = f'{emoji} {titles[key]}', sorted(groups[key], key=agent_sort_key)
        lines.append((None, f'── {title} ({len(group)}) ──'))
        for item in group:
            identity = (item['pane'], item['pid'])
            reviewed = item['reviewed']
            label = agent_emoji(item)
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
        rows, _ = items(con, sock, live, show_all)
        rows = grouped_rows(rows)
        registered, _ = items(con, sock, live, True)
        ids = [(x['pane'], x['pid']) for x in rows]
        if selected not in ids:
            selected = ids[0] if ids else None
        index = ids.index(selected) if selected else 0
        h, w = win.getmaxyx()
        win.erase()
        draw(win, 0, 1, '待處理清單', curses.A_BOLD)
        draw(win, 1, 1, status_text(status_groups(registered)), curses.A_BOLD)
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


def install_info():
    """What update.py recorded when it last deployed here; {} for installs that predate it."""
    try:
        data = json.loads((ROOT / 'INSTALL.json').read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def install_version():
    return str(install_info().get('version') or '')


# What this process loaded. install_version() re-reads the file, so only this proves a restart picked up new code.
STARTED_VERSION = install_version()


def run_update(argv):
    """`tmux-attn update ...` hands over to update.py in the checkout this install came from."""
    source = os.environ.get('TMUX_ATTN_SRC') or str(install_info().get('source') or '')
    for i, arg in enumerate(argv):
        if arg == '--src' and i + 1 < len(argv):
            source = argv[i + 1]
        elif arg.startswith('--src='):
            source = arg[len('--src='):]
    updater = Path(source) / 'update.py' if source else None
    if not updater or not updater.is_file():
        where = f'{updater} is missing' if updater else 'this install has no recorded checkout'
        sys.stderr.write(
            f'tmux-attn update: {where}.\nRun it from a checkout: python3 <checkout>/update.py, '
            'or bootstrap with: curl -fsSL https://raw.githubusercontent.com/cubrady/tmux-attn/main/update.sh | sh\n')
        return 1
    os.execv(sys.executable, [sys.executable, str(updater), *argv])


def web_icon_path(name):
    safe = Path(name).name
    if not safe.endswith('.svg') or safe != name:
        return None
    aliases = {'cursor.svg': 'agent.svg'}
    safe = aliases.get(safe, safe)
    path = ROOT / 'web' / 'icons' / safe
    return path if path.is_file() else None


def peer_upload_reply(data):
    """The part of a peer's /api/upload reply we relay, or None if it is malformed."""
    if (isinstance(data, dict) and data.get('ok') is True and isinstance(data.get('id'), str)
            and UPLOAD_ID_RE.match(data['id'])):
        return {'ok': True, 'id': data['id']}
    return None


def peer_message_reply(data):
    """The part of a peer's /api/message reply we relay, or None if it is malformed."""
    if (not isinstance(data, dict) or data.get('ok') is not True or data.get('role') not in ('user', 'agent')
            or not isinstance(data.get('text'), str) or type(data.get('chars')) is not int):
        return None
    return {'ok': True, 'role': data['role'], 'text': data['text'], 'chars': data['chars'],
            'truncated': data.get('truncated') is True, 'partial': data.get('partial') is True}


def dispatch_send(payload):
    """Send text to a local pane or forward to a named peer. Returns (code, body dict)."""
    if not isinstance(payload, dict):
        return 400, {'ok': False, 'error': 'invalid json'}
    pane = clean(payload.get('pane') or '')
    pid = clean(str(payload.get('pid') or ''))
    text = payload.get('text')
    host = clean(payload.get('host') or '')
    submit = payload.get('submit', True)
    images = payload.get('images') or []
    files = payload.get('files', [])
    if not pane or not pid or text is None:
        return 400, {'ok': False, 'error': 'pane, pid, and text are required'}
    if (not isinstance(images, list) or len(images) > MAX_IMAGES
            or not all(isinstance(i, str) and IMAGE_ID_RE.match(i) for i in images)):
        return 400, {'ok': False, 'error': 'invalid images'}
    if (not isinstance(files, list) or len(images) + len(files) > MAX_IMAGES
            or not all(isinstance(i, str) and FILE_ID_RE.match(i) for i in files)):
        return 400, {'ok': False, 'error': 'invalid files'}
    local = local_host_name()
    if host and host != local and not payload.get('_local_only'):
        peer = next((row for row in load_peers() if row['name'] == host), None)
        if not peer:
            return 404, {'ok': False, 'error': f'unknown host {host}'}
        try:
            body = json.dumps({
                'pane': pane, 'pid': pid, 'text': text, 'submit': submit, 'images': images,
                '_local_only': True,
                **({'files': files} if files else {}),
            }).encode()
            request = Request(
                peer['url'].rstrip('/') + '/api/send',
                data=body,
                headers={'Content-Type': 'application/json', 'Accept': 'application/json'},
                method='POST',
            )
            # Each image costs the peer several tmux calls; a timeout after delivery would invite a duplicate send.
            with urlopen(request, timeout=PEER_UPLOAD_TIMEOUT if images or files else 3) as response:
                data = json.loads(response.read().decode())
            return (200 if data.get('ok') else 502), data
        except HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read().decode())
            except (ValueError, UnicodeError):
                return 502, {'ok': False, 'error': f'peer send failed: HTTP {exc.code}'}
        except Exception as exc:
            return 502, {'ok': False, 'error': f'peer send failed: {type(exc).__name__}'}
    with PANE_INPUT_LOCK:
        error = send_to_pane(pane, pid, text, submit=bool(submit), images=images, **({'files': files} if files else {}))
    if error:
        return 409, {'ok': False, 'error': error}
    return 200, {'ok': True, 'pane': pane, 'pid': pid, 'host': local}


def dispatch_upload(payload):
    """Store an attachment on the pane's own host (local or via peer)."""
    if not isinstance(payload, dict):
        return 400, {'ok': False, 'error': 'invalid json'}
    pane = clean(payload.get('pane') or '')
    pid = clean(str(payload.get('pid') or ''))
    host = clean(payload.get('host') or '')
    data = payload.get('data')
    filename = payload.get('filename', '')
    if not isinstance(filename, str) or len(filename) > 255:
        return 400, {'ok': False, 'error': 'invalid filename'}
    if not pane or not pid or not isinstance(data, str) or not data:
        return 400, {'ok': False, 'error': 'pane, pid, and data are required'}
    local = local_host_name()
    if host and host != local and not payload.get('_local_only'):
        peer = next((row for row in load_peers() if row['name'] == host), None)
        if not peer:
            return 404, {'ok': False, 'error': f'unknown host {host}'}
        try:
            body = json.dumps({'pane': pane, 'pid': pid, 'data': data, '_local_only': True,
                               **({'filename': filename} if filename else {})}).encode()
            request = Request(
                peer['url'].rstrip('/') + '/api/upload',
                data=body,
                headers={'Content-Type': 'application/json', 'Accept': 'application/json'},
                method='POST',
            )
            with urlopen(request, timeout=PEER_UPLOAD_TIMEOUT) as response:
                result = json.loads(response.read().decode())
            reply = peer_upload_reply(result)
            return (200, reply) if reply else (502, {'ok': False, 'error': 'peer upload failed: bad reply'})
        except HTTPError as exc:
            if exc.code == 404:
                return 502, {'ok': False, 'error': PEER_OUTDATED}
            try:
                return exc.code, json.loads(exc.read().decode())
            except (ValueError, UnicodeError):
                return 502, {'ok': False, 'error': f'peer upload failed: HTTP {exc.code}'}
        except Exception as exc:
            return 502, {'ok': False, 'error': f'peer upload failed: {type(exc).__name__}'}
    if not live_pane_meta(pane, pid):
        return 409, {'ok': False, 'error': 'pane not found or pid mismatch'}
    try:
        raw = base64.b64decode(data, validate=True)
    except (ValueError, binascii.Error):
        return 400, {'ok': False, 'error': 'invalid base64'}
    try:
        image_id, error = store_upload(raw, filename)
    except OSError as exc:
        return 500, {'ok': False, 'error': f'upload failed: {type(exc).__name__}'}
    if error:
        return (413 if error in ('image too large', 'file too large') else 400), {'ok': False, 'error': error}
    return 200, {'ok': True, 'id': image_id, 'bytes': len(raw), 'pane': pane, 'pid': pid, 'host': local}


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


def dispatch_message(payload):
    """Return the full text of one sheet message, local or via peer. Returns (code, body dict)."""
    if not isinstance(payload, dict):
        return 400, {'ok': False, 'error': 'invalid json'}
    pane = clean(payload.get('pane') or '')
    pid = clean(str(payload.get('pid') or ''))
    host = clean(payload.get('host') or '')
    role = payload.get('role')
    index = payload.get('index', 0)
    sig = payload.get('sig') or ''
    if not pane or not pid:
        return 400, {'ok': False, 'error': 'pane and pid are required'}
    if role not in ('user', 'agent'):
        return 400, {'ok': False, 'error': 'role must be user or agent'}
    if type(index) is not int or index < 0:
        return 400, {'ok': False, 'error': 'index must be a non-negative integer'}
    if not isinstance(sig, str) or len(sig) > 400:
        return 400, {'ok': False, 'error': 'invalid sig'}
    local = local_host_name()
    if host and host != local and not payload.get('_local_only'):
        peer = next((row for row in load_peers() if row['name'] == host), None)
        if not peer:
            return 404, {'ok': False, 'error': f'unknown host {host}'}
        try:
            body = json.dumps({'pane': pane, 'pid': pid, 'role': role, 'index': index, 'sig': sig,
                               '_local_only': True}).encode()
            request = Request(
                peer['url'].rstrip('/') + '/api/message',
                data=body,
                headers={'Content-Type': 'application/json', 'Accept': 'application/json'},
                method='POST',
            )
            with urlopen(request, timeout=PEER_UPLOAD_TIMEOUT) as response:
                data = json.loads(response.read().decode())
            reply = peer_message_reply(data)
            return (200, reply) if reply else (502, {'ok': False, 'error': 'peer message failed: bad reply'})
        except HTTPError as exc:
            if exc.code == 404:
                return 502, {'ok': False, 'error': PEER_OUTDATED}
            try:
                return exc.code, json.loads(exc.read().decode())
            except (ValueError, UnicodeError):
                return 502, {'ok': False, 'error': f'peer message failed: HTTP {exc.code}'}
        except Exception as exc:
            return 502, {'ok': False, 'error': f'peer message failed: {type(exc).__name__}'}
    try:
        with closing(db()) as con:
            message, error = full_message_for_pane(con, pane, pid, role, index, sig)
    except Exception as exc:
        return 500, {'ok': False, 'error': f'{type(exc).__name__}'}
    if error:
        return 409, {'ok': False, 'error': error}
    return 200, {'ok': True, 'pane': pane, 'pid': pid, 'host': local, **message}


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


def dispatch_answer(payload):
    """Answer a pending agent question on its own host (local or via peer). Returns (code, body dict)."""
    if not isinstance(payload, dict):
        return 400, {'ok': False, 'error': 'invalid json'}
    pane = clean(payload.get('pane') or '')
    pid = clean(str(payload.get('pid') or ''))
    host = clean(payload.get('host') or '')
    group = payload.get('group')
    answers = payload.get('answers')
    if not pane or not pid or not isinstance(group, str) or not group or len(group) > 600:
        return 400, {'ok': False, 'error': 'pane, pid, and group are required'}
    if not isinstance(answers, list) or not 1 <= len(answers) <= 4:
        return 400, {'ok': False, 'error': 'answers must be a list of 1 to 4 items'}
    local = local_host_name()
    if host and host != local and not payload.get('_local_only'):
        peer = next((row for row in load_peers() if row['name'] == host), None)
        if not peer:
            return 404, {'ok': False, 'error': f'unknown host {host}'}
        try:
            request = Request(peer['url'].rstrip('/') + '/api/answer',
                              data=json.dumps({'pane': pane, 'pid': pid, 'group': group, 'answers': answers,
                                               '_local_only': True}).encode(),
                              headers={'Content-Type': 'application/json', 'Accept': 'application/json'},
                              method='POST')
            # Several confirmed key steps; a timeout after delivery would invite a duplicate answer.
            with urlopen(request, timeout=PEER_UPLOAD_TIMEOUT) as response:
                data = json.loads(response.read().decode())
            return (200 if data.get('ok') else 502), data
        except HTTPError as exc:
            if exc.code == 404:
                return 502, {'ok': False, 'error': PEER_OUTDATED}
            try:
                return exc.code, json.loads(exc.read().decode())
            except (ValueError, UnicodeError):
                return 502, {'ok': False, 'error': f'peer answer failed: HTTP {exc.code}'}
        except Exception as exc:
            return 502, {'ok': False, 'error': f'peer answer failed: {type(exc).__name__}'}
    try:
        error = answer_pane_questions(pane, pid, group, answers)
    except Exception as exc:
        return 500, {'ok': False, 'error': f'{type(exc).__name__}'}
    if error:
        return 409, {'ok': False, 'error': error}
    return 200, {'ok': True, 'pane': pane, 'pid': pid, 'host': local}


def make_web_handler(fleet_only=False):
    index = web_index_path()
    if not index.is_file():
        raise SystemExit(f'Missing web UI: {index}')

    actions = {
        '/api/kill': dispatch_kill, '/api/send': dispatch_send, '/api/upload': dispatch_upload,
        '/api/preview': dispatch_preview, '/api/message': dispatch_message, '/api/ack': dispatch_ack,
        '/api/answer': dispatch_answer,
    }

    class Handler(BaseHTTPRequestHandler):
        timeout = 30  # a stalled client must not hold a thread forever

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
                    'version': install_version(),
                    'started_version': STARTED_VERSION,
                }), 'application/json; charset=utf-8')
                return
            if path == '/api/agents':
                # Machine-local feed only — peer scrapers must use this path.
                try:
                    with closing(db()) as con:
                        payload = {**agents_payload(con), 'version': install_version()}
                    self._send(200, json.dumps(payload, ensure_ascii=False), 'application/json; charset=utf-8')
                except Exception as exc:
                    sys.stderr.write(f'web /api/agents: {type(exc).__name__}: {exc}\n')
                    self._send(500, json.dumps({'ok': False, 'error': 'failed to load agents'}),
                               'application/json; charset=utf-8')
                return
            if path == '/api/fleet':
                try:
                    payload = web_later_fleet(fleet_payload(include_local=not fleet_only))
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
            # Browsers often request /favicon.ico before reading <link rel="icon">.
            if path in ('/favicon.ico', '/favicon.svg'):
                icon = web_icon_path('favicon.svg')
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
            try:
                length = int(self.headers.get('Content-Length') or 0)
            except ValueError:
                length = -1
            if length < 0:
                self._send(400, json.dumps({'ok': False, 'error': 'invalid content-length'}), 'application/json; charset=utf-8')
                return
            if length > (MAX_UPLOAD_BODY_BYTES if path == '/api/upload' else MAX_BODY_BYTES):
                self._send(413, json.dumps({'ok': False, 'error': 'request too large'}), 'application/json; charset=utf-8')
                return
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
            if path == '/api/later':
                try:
                    code, body = dispatch_web_later(payload, include_local=not fleet_only)
                except Exception as exc:
                    sys.stderr.write(f'web /api/later: {type(exc).__name__}: {exc}\n')
                    code, body = 500, {'ok': False, 'error': 'failed to update later group'}
                self._send(code, json.dumps(body, ensure_ascii=False), 'application/json; charset=utf-8')
                return
            action = actions.get(path)
            if action:
                if fleet_only and not payload.get('host'):
                    self._send(400, json.dumps({'ok': False, 'error': 'host required in fleet mode'}),
                               'application/json; charset=utf-8')
                    return
                code, body = action(payload)
                self._send(code, json.dumps(body, ensure_ascii=False), 'application/json; charset=utf-8')
                return
            self._send(404, json.dumps({'ok': False, 'error': 'not found'}), 'application/json; charset=utf-8')

    return Handler


def serve_web(host='0.0.0.0', port=7788, fleet_only=False):
    prune_uploads()
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
    if sys.argv[1:2] == ['update']:
        sys.exit(run_update(sys.argv[2:]))
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
