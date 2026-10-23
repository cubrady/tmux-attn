"""Read-only legacy Codex session adapter. Native hooks remain primary."""
from __future__ import annotations
from datetime import datetime, timezone
import fcntl
from collections import Counter, OrderedDict
from contextlib import closing
import json
import os
from pathlib import Path
import re
import sqlite3
import signal
import shlex
import subprocess
import time

from attention import (
    STATE, BACKGROUND_WAIT, background_wait, classify, codex_root_transcript, codex_transcript_meta, db, live_panes,
    prune_stale_panes, record, resolve_socket, tmux, observer_lock_path, log_observer_error,
)


def processes():
    result = {}
    p = subprocess.run(['ps','-axo','pid=,ppid=,args='],capture_output=True,text=True,timeout=4)
    for line in p.stdout.splitlines():
        fields = line.strip().split(None,2)
        if len(fields) == 3:
            result[int(fields[0])] = (int(fields[1]),fields[2])
    return result


def _process_exe(pid, args=''):
    """Best-effort absolute executable path (ps args may be a short name)."""
    try:
        return os.readlink(f'/proc/{pid}/exe')
    except OSError:
        return (args.split(None, 1)[0] if args else '')


def is_native_codex_cli(pid, args=''):
    """True for interactive Codex CLI; false for app-server / helper daemons."""
    args = args or ''
    tokens = args.split()
    if any(token in args for token in ('app-server', 'pid-update-loop', 'codex-code-mode-host')):
        return False
    if 'daemon' in tokens or '--managed-daemon' in tokens:
        return False
    exe = _process_exe(pid, args)
    name = Path(exe).name if exe else Path((args.split(None, 1)[0] if args else '')).name
    if name != 'codex':
        return False
    # Legacy npm vendor layout, standalone packages, or PATH symlink targets.
    if '/vendor/' in exe or '/packages/standalone/' in exe or '/bin/codex' in exe:
        return True
    # Short argv ("codex …") with /proc exe pointing at a real CLI binary.
    return bool(exe) and Path(exe).name == 'codex'


def native_codex_pids(pane_pid, table):
    descendants = {int(pane_pid)}
    for _ in range(30):
        more = {pid for pid,(parent,_) in table.items() if parent in descendants}
        if more.issubset(descendants):
            break
        descendants |= more
    return [pid for pid in descendants if is_native_codex_cli(pid, table.get(pid, (0, ''))[1])]


def native_cursor_pids(pane_pid, table):
    descendants = {int(pane_pid)}
    for _ in range(30):
        more = {pid for pid,(parent,_) in table.items() if parent in descendants}
        if more.issubset(descendants):
            break
        descendants |= more
    return [pid for pid in descendants
            if '/cursor-agent/versions/' in table.get(pid,(0,''))[1]
            and '/index.js' in table[pid][1]]


def cursor_screen(screen, cwd):
    """Only the native Cursor prompt/footer, after verified CLI ancestry."""
    lines = screen.rstrip().splitlines()
    if not cwd or not any(line.strip().startswith(cwd+' · ') for line in lines[-4:]):
        return None
    tail = '\n'.join(lines[-20:])
    if (re.search(r'(?:Run this command(?: outside the sandbox)?|Run this MCP tool|Delete this file|Write to this file|Read this file|Allow this web search|Allow this web fetch)\?',tail)
        and re.search(r'(?:Run .*\(y\)|Proceed \(y\)|Delete \(y\)|Allow once \(y\)|Allow search \(y\)|Fetch \(y\)|Reject & propose changes|Skip \(esc or n\))',tail)):
        return 'WAIT','Approval dialog visible',False
    prompts = [i for i,line in enumerate(lines[-9:]) if re.match(r'^\s*→ Add a follow-up',line)]
    if not prompts:
        return None
    index = len(lines)-min(len(lines),9)+prompts[-1]
    if 'ctrl+c to stop' in lines[index]:
        return 'RUNNING','Working',False
    body = '\n'.join(lines[:index]).rstrip()
    if not body:
        return 'IDLE','Waiting for next task',False
    classified = classify('cursor','Stop',{'last_assistant_message':body})
    return (*classified,False) if len(classified) == 2 else classified


def observe_cursor(con, sock, live, table):
    for (pane,pid),meta in live.items():
        if len(native_cursor_pids(pid,table)) != 1:
            continue
        screen = tmux('capture-pane','-p','-J','-t',pane,check=False,sock=sock)
        state = cursor_screen(screen,meta['path'])
        if state:
            current = con.execute('SELECT * FROM panes WHERE socket=? AND pane=? AND pid=?',(sock,pane,pid)).fetchone()
            # Preserve native hook error/interruption reasons and acknowledged results.
            if current and current['agent'] == 'cursor' and current['state'] == state[0]:
                continue
            record(con,sock,pane,pid,'cursor',*state,source='observer')


def native_claude_pids(pane_pid, table):
    descendants = {int(pane_pid)}
    for _ in range(30):
        more = {pid for pid,(parent,_) in table.items() if parent in descendants}
        if more.issubset(descendants):
            break
        descendants |= more
    candidates = set()
    for pid in descendants:
        args = table.get(pid,(0,''))[1]
        executable = args.split(None,1)[0] if args else ''
        if (Path(executable).name == 'claude' or '/claude/versions/' in executable):
            if not any(flag in args.split() for flag in ('daemon','bg-pty-host','bg-spare','--bg-spare','--bg-pty-host')):
                candidates.add(pid)
    # An inner Claude subprocess is not the visible terminal's primary agent.
    roots = []
    for pid in candidates:
        parent = table[pid][0]
        for _ in range(30):
            if parent in candidates:
                break
            if parent not in descendants or parent not in table:
                roots.append(pid)
                break
            parent = table[parent][0]
    return roots


TAIL_BYTES = 2 * 1024 * 1024
STALE_SECONDS = 3600
_cache = OrderedDict()
CLAUDE_MODAL_RE = re.compile(
    r'(?:Do you want to proceed\?|needs your approval|Enter to (?:select|confirm|continue|submit)'
    r'|Session paused|Esc to cancel|Submit answers|❯\s*\d+\.\s*(?:Yes|Allow|No|Deny|Reject))',
    re.I,
)


def shell_footer(screen):
    """Only a live footer below the final prompt, never a done/scrollback line."""
    lines = screen.rstrip().splitlines()
    if CLAUDE_MODAL_RE.search('\n'.join(lines[-35:])):
        return 0
    prompts = [i for i, line in enumerate(lines) if re.match(r'^\s*❯(?!\s+[⏺◯●◐◌◉])(?:\s|$)', line)]
    if not prompts:
        return 0
    index = prompts[-1]
    if len(lines) - index > 8 or 'Message @' in lines[index]:
        return 0
    after = lines[index + 1:]
    if not any(re.fullmatch(r'[─━-]{4,}', line.strip()) for line in after):
        return 0
    for line in after:
        if line.strip().startswith('⏵') and 'esc to interrupt' not in line.lower():
            counts = [int(n) for n in re.findall(
                r'(?:^|[·,])\s*([1-9]\d*)\s+(?:shells?|monitors?)(?=\s*(?:[·,]|$))', line.strip())]
            if counts:
                return sum(counts)
    return 0


def shell_command(args):
    """Extract Claude's snapshot/eval wrapper without executing its contents."""
    executable = args.split(None, 1)[0] if args else ''
    if Path(executable).name not in ('sh', 'bash', 'zsh', 'dash'):
        return ''
    if ' && eval ' not in args or ' -c ' not in args:
        return ''
    try:
        return shlex.split(args.split(' && eval ', 1)[1].replace('\\012', '\n'))[0].strip()
    except (ValueError, IndexError):
        return ''


def _text(content):
    if isinstance(content, str):
        return content
    return '\n'.join(b['text'] for b in content if isinstance(b, dict)
                     and b.get('type') == 'text' and isinstance(b.get('text'), str)) if isinstance(content, list) else ''


def _stamp(value):
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError, AttributeError):
        return 0


def read_tasks(path):
    """Bounded, cached task ledger; None means unavailable, not zero tasks."""
    if not path:
        return None
    path = Path(path)
    try:
        stat = path.stat()
        key = (str(path), stat.st_ino, stat.st_size, stat.st_mtime_ns)
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
        previous = next((value for old, value in reversed(_cache.items())
                         if old[0] == str(path) and old[1] == stat.st_ino
                         and old[2] < stat.st_size
                         and value['offset'] <= stat.st_size
                         and stat.st_size - value['offset'] <= TAIL_BYTES), None)
        with path.open('rb') as stream:
            if previous:
                stream.seek(previous['offset'])
            elif stat.st_size > TAIL_BYTES:
                stream.seek(stat.st_size - TAIL_BYTES)
                stream.readline(TAIL_BYTES)
            raw = stream.read(TAIL_BYTES)
            # Preserve partial appended records for the next scan.
            complete = raw.rfind(b'\n') + 1
            offset = stream.tell() - len(raw) + complete
            raw = raw[:complete]
    except OSError:
        return None
    calls = dict(previous['calls']) if previous else {}
    tasks = {task['id']: task for task in previous['tasks']} if previous else {}
    known_turn = bool(previous and previous['known_turn'])
    for line in raw.splitlines():
        # Claude also writes enormous API request blobs; those are not task events.
        if b'"message"' not in line or not any(marker in line for marker in
                (b'"tool_use"', b'"tool_result"', b'"type":"user"', b'"type": "user"')):
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or row.get('isSidechain'):
            continue
        message = row.get('message')
        if not isinstance(message, dict):
            continue
        content = message.get('content', [])
        text = _text(content)
        if row.get('type') == 'user' and '<task-notification>' in text:
            task_id = re.search(r'<task-id>([^<]+)</task-id>', text)
            status = (re.search(r'<status>(completed|failed|killed)</status>', text)
                      or '[Monitor expired after ' in text)
            if task_id and status:
                tasks.pop(task_id[1], None)
            continue
        results = [b for b in content if isinstance(b, dict) and b.get('type') == 'tool_result'] if isinstance(content, list) else []
        if row.get('type') == 'user' and not results and not row.get('isMeta') and text.strip():
            calls, tasks, known_turn = {}, {}, True
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            inputs = block.get('input')
            if block.get('type') == 'tool_use' and block.get('name') in ('Bash', 'Monitor') and isinstance(inputs, dict):
                command = inputs.get('command')
                if isinstance(command, str) and block.get('id'):
                    calls[block['id']] = {'command': command.strip(), 'started': _stamp(row.get('timestamp'))}
        for result in results:
            call = calls.pop(result.get('tool_use_id'), None)
            text = _text(result.get('content'))
            match = re.search(r'(?:Command running in background with ID: |Monitor started \(task )([\w-]+)', text)
            if call and match:
                output = re.search(r'Output is being written to: (.+?\.output)(?:\.|\s|$)', text)
                tasks[match[1]] = dict(call, id=match[1], output=output[1] if output else '')
            # TaskOutput's structured result is also a terminal-state source.
        result = row.get('toolUseResult')
        if isinstance(result, dict):
            task = result.get('task')
            if isinstance(task, dict) and task.get('status') in ('completed', 'failed', 'killed'):
                tasks.pop(task.get('task_id') or task.get('id'), None)
    answer = {'known_turn': known_turn, 'tasks': list(tasks.values()), 'calls': calls, 'offset': offset}
    for old in list(_cache):
        if old[0] == str(path):
            del _cache[old]
    _cache[key] = answer
    while len(_cache) > 32:
        _cache.popitem(last=False)
    return answer


def _output_info(task, transcript):
    """Only inspect this session's task output, with a fixed read bound."""
    output = Path(task.get('output') or '')
    if (output.name != task['id'] + '.output' or output.parent.name != 'tasks'
            or output.parent.parent.name != Path(transcript).stem):
        return None
    try:
        stat = output.stat()
        with output.open('rb') as stream:
            stream.seek(max(0, stat.st_size - 8192))
            tail = stream.read(8192).decode(errors='replace')
        error = bool(re.search(r'Traceback \(most recent call last\)|\bError:|exit code [1-9]\d*', tail))
        return stat.st_mtime, error
    except OSError:
        return None


def parked_shells(screen, root, table, transcript, now):
    """A current footer + native shell + task identity outweigh a Stop event."""
    count = shell_footer(screen)
    if not count:
        return None
    commands = Counter(shell_command(args) for parent, args in table.values()
                       if parent == root and shell_command(args))
    if not commands:
        return None
    ledger = read_tasks(transcript)
    if ledger is None and transcript:
        # Resumed sessions can emit a hook path before that file exists. The
        # native shell wrapper names its owning transcript; never guess "latest".
        session_ids = set()
        for parent, args in table.values():
            if parent == root and shell_command(args):
                session_ids.update(re.findall(r"CODEX_COMPANION_SESSION_ID='([0-9a-f-]{36})'", args))
        if len(session_ids) == 1:
            transcript = str(Path(transcript).with_name(next(iter(session_ids)) + '.jsonl'))
            ledger = read_tasks(transcript)
    tasks = []
    for task in ledger['tasks'] if ledger else []:
        if commands[task['command']]:
            tasks.append(task)
            commands[task['command']] -= 1
    # Missing/truncated identity evidence is unknown, not permission to count old
    # polling shells or MCP services as the current turn's work.
    if not ledger or not ledger['known_turn'] or not tasks:
        return None
    count = min(count, len(tasks))
    reason = f'Background work: waiting on {count} shell' + ('s' if count != 1 else '')
    for task in tasks:
        info = _output_info(task, transcript)
        if info and info[1]:
            reason += '; task output contains an error; check progress'
            break
        if task['started'] and now - max(task['started'], info[0] if info else 0) >= STALE_SECONDS:
            reason += '; possibly stalled (no recent task output); check progress'
            break
    return 'RUNNING', reason, False


def claude_screen(screen):
    lines = screen.rstrip().splitlines()
    tail = '\n'.join(lines[-35:])
    # Permission and question menus may be drawn over earlier activity output.
    if CLAUDE_MODAL_RE.search(tail):
        return None
    for index in range(len(lines)-1,max(-1,len(lines)-35),-1):
        line = lines[index].strip()
        if not re.match(r'^[·✢✳✶✻✽]\s',line):
            continue
        waiting = re.match(r'^[·✢✳✶✻✽]\s+Waiting for (\d+) background agents? to finish\s*$',line)
        if waiting:
            # A prompt drawn after this line means the wait is only scrollback.
            if any(re.match(r'^\s*❯(?:\s|$)', later) for later in lines[index+1:]):
                continue
            return 'RUNNING',f'Background work: waiting on {waiting[1]} subagent',False
        if re.search(r'…\s*\(',line) and 'esc to interrupt' in tail.lower():
            return 'RUNNING','Working',False
        if re.search(r'\bfor\s+\d+[hms](?:\s|\b)',line) and 'esc to interrupt' not in tail.lower():
            body = '\n'.join(lines[:index]).rstrip()
            classified = classify('claude','Stop',{'last_assistant_message':body})
            return (*classified,False) if len(classified) == 2 else classified
    return None


def observe_claude(con, sock, live, table):
    for (pane,pid) in live:
        current = con.execute('SELECT * FROM panes WHERE socket=? AND pane=? AND pid=?',(sock,pane,pid)).fetchone()
        roots = native_claude_pids(pid,table)
        if not current or current['agent'] != 'claude' or len(roots) != 1:
            continue
        screen = tmux('capture-pane','-p','-J','-t',pane,check=False,sock=sock)
        state = claude_screen(screen)
        if state and state[0] == 'IDLE' and current['state'] == 'WAIT':
            # A finished line underneath a permission/question overlay is stale.
            # Native tools or a foreground spinner can still clear a stale WAIT.
            continue
        if (state is None or state[0] == 'IDLE') and current['state'] != 'WAIT':
            state = parked_shells(screen, roots[0], table, current['transcript_path'], time.time()) or state
        # Let a freshly emitted native permission/stop event settle before a
        # screen update can replace it. Delayed old notifications still recover.
        running_wait_changed = (state and state[0] == current['state'] == 'RUNNING'
                                and (state[1].startswith(BACKGROUND_WAIT) != background_wait(current)
                                     or (background_wait(current) and current['reason'] != state[1])))
        if (state and (current['state'] != state[0] or running_wait_changed)
                and time.time()-max(current['updated'],current['last_event_time']) >= 2):
            record(con,sock,pane,pid,'claude',*state,source='observer')


def native_pi_pids(pane_pid, table):
    descendants = {int(pane_pid)}
    for _ in range(30):
        more = {pid for pid,(parent,_) in table.items() if parent in descendants}
        if more.issubset(descendants):
            break
        descendants |= more
    roots = []
    for pid in descendants:
        args = table.get(pid, (0, ''))[1]
        exe = Path((args.split(None, 1)[0] if args else '')).name
        if exe in ('pi', 'pi-agent') or 'pi-coding-agent' in args:
            tokens = args.split()
            if 'daemon' in tokens or '--managed-daemon' in tokens:
                continue
            roots.append(pid)
    return roots


def pi_extension_heartbeat_age(pane):
    path = STATE / 'pi-heartbeats' / f'{str(pane).lstrip("%")}.json'
    try:
        data = json.loads(path.read_text())
        return time.time() - float(data.get('at') or 0)
    except (OSError, ValueError, TypeError):
        return None


def pi_screen(screen):
    """Fallback when extension UI prompts did not report (non-ctx.ui dialogs)."""
    lines = screen.rstrip().splitlines()
    tail = '\n'.join(lines[-30:])
    if re.search(
        r'(?:\(Y/n\)|\(y/N\)|\[Y/n\]|\[y/N\]|Yes\s*/\s*No|Confirm\?|Select an option|Enter a value)',
        tail, re.I,
    ):
        return 'WAIT', 'Answer or decision needed', False
    if re.search(r'(?:Thinking|Working|Running tools|Streaming)\b', tail, re.I):
        return 'RUNNING', 'Working', False
    return None


def observe_pi(con, sock, live, table):
    for (pane, pid) in live:
        if len(native_pi_pids(pid, table)) != 1:
            continue
        current = con.execute(
            'SELECT * FROM panes WHERE socket=? AND pane=? AND pid=?', (sock, pane, pid)
        ).fetchone()
        screen = tmux('capture-pane', '-p', '-J', '-t', pane, check=False, sock=sock)
        state = pi_screen(screen)
        if not state:
            heartbeat_age = pi_extension_heartbeat_age(pane)
            # Extension silent while Pi is alive: keep a tracked IDLE row so status sees it.
            if not current and (heartbeat_age is None or heartbeat_age > 30):
                record(con, sock, pane, pid, 'pi', 'IDLE', 'Pi running; extension heartbeat stale', source='observer')
            continue
        if current and current['agent'] == 'pi' and current['state'] == state[0]:
            continue
        if current and time.time() - max(current['updated'], current['last_event_time']) < 2:
            continue
        record(con, sock, pane, pid, 'pi', *state, source='observer')


def process_start(pid):
    p = subprocess.run(['ps','-p',str(pid),'-o','lstart='],env=os.environ|{'TZ':'UTC','LC_ALL':'C'},capture_output=True,text=True,timeout=4)
    return int(datetime.strptime(p.stdout.strip(),'%a %b %d %H:%M:%S %Y').replace(tzinfo=timezone.utc).timestamp())


def process_cwd(pid):
    p = subprocess.run(['lsof','-n','-a','-p',str(pid),'-d','cwd','-Fn'],capture_output=True,text=True,timeout=4)
    return next((line[1:] for line in p.stdout.splitlines() if line.startswith('n')),None)


def startup_match(state_db, cwd, started):
    """Only an unambiguous validated startup match is accepted, never newest cwd."""
    if not state_db.exists() or not cwd:
        return None
    with closing(sqlite3.connect(state_db.as_uri()+'?mode=ro',uri=True,timeout=1)) as con:
        rows = con.execute('SELECT id,rollout_path FROM threads WHERE cwd=? AND created_at BETWEEN ? AND ? AND agent_path IS NULL', (cwd,started,started+5)).fetchall()
    accepted = []
    for thread,path in rows:
        try:
            with Path(path).open() as f:
                header = json.loads(f.readline())
            meta = header.get('payload',{})
            stamp = datetime.fromisoformat(meta['timestamp'].replace('Z','+00:00')).timestamp()
            if (header.get('type') == 'session_meta' and meta.get('originator') == 'codex-tui'
                and meta.get('id') == thread and meta.get('cwd') == cwd
                and started <= stamp <= started+6):
                accepted.append((thread,path))
        except (OSError,ValueError,KeyError,TypeError):
            continue
    return accepted[0] if len(accepted) == 1 else None


def resumed_match(state_db, cwd, screen):
    """Bind a named resumed CLI only from an exact footer/cwd and unique root."""
    if not state_db.exists() or not cwd:
        return None
    names, aligned_names = set(), set()
    for line in screen.rstrip().splitlines()[-8:]:
        fields = [part.strip() for part in line.split(' · ')]
        if len(fields) >= 3 and str(Path(fields[1]).expanduser()) == cwd:
            # The terminal puts goal/progress indicators in a separate, right-
            # aligned column on this same line; they are not part of the name.
            names.add(fields[2])
            aligned_names.add(re.split(r'[ \t]{8,}', fields[2], maxsplit=1)[0])
    if not names:
        return None
    try:
        with closing(sqlite3.connect(state_db.as_uri()+'?mode=ro',uri=True,timeout=1)) as con:
            rows = con.execute('SELECT id,rollout_path,name FROM threads WHERE cwd=? AND agent_path IS NULL', (cwd,)).fetchall()
    except sqlite3.Error:
        return None
    # Prefer a complete exact title, including any legitimate internal spaces.
    # Only fall back to removing a wide terminal alignment gap if none matches.
    for candidates in (names, aligned_names):
        accepted = []
        for thread,path,name in rows:
            if name not in candidates or not codex_root_transcript(path,thread):
                continue
            meta = codex_transcript_meta(path)
            if meta and meta.get('originator') == 'codex-tui' and meta.get('cwd') == cwd:
                accepted.append((thread,path))
        if accepted:
            return accepted[0] if len(accepted) == 1 else None
    return None


def lifecycle(entry):
    if entry.get('type') != 'event_msg':
        return None
    payload = entry.get('payload',{})
    kind = payload.get('type')
    if kind not in ('task_started','task_complete','turn_aborted'):
        return None
    try:
        stamp = datetime.fromisoformat(entry['timestamp'].replace('Z','+00:00')).timestamp()
    except (ValueError,KeyError,TypeError):
        return None
    if kind == 'task_started':
        state = ('RUNNING','Working',False)
    elif kind == 'turn_aborted':
        state = ('IDLE','Interrupted; review current work',True)
    else:
        classified = classify('codex','Stop',{'last_assistant_message':payload.get('last_agent_message','')})
        state = (*classified,False) if len(classified) == 2 else classified
    return state,stamp,payload.get('turn_id','')


def read_lifecycle(path, offset=0):
    latest = None
    with Path(path).open('rb') as f:
        if offset > Path(path).stat().st_size:
            offset = 0
        f.seek(offset)
        while True:
            beginning = f.tell()
            line = f.readline()
            if not line:
                break
            if not line.endswith(b'\n'):
                f.seek(beginning)
                break
            try:
                event = lifecycle(json.loads(line))
            except (ValueError,TypeError):
                continue
            if event:
                latest = event
        return f.tell(),latest


def approval_screen(text):
    """Fallback detects the actual approval dialog, never escalation arguments."""
    tail = '\n'.join(text.splitlines()[-35:])
    return (bool(re.search(r'Would you like to (?:run|make|allow|apply)',tail,re.I))
            and bool(re.search(r'1\.\s*(?:Yes|Allow|Approve)',tail,re.I))
            and bool(re.search(r'\b(?:No|deny|reject)\b',tail,re.I)))


def queued_questions_screen(text):
    """Require Codex's actual queued-question footer, not prose or tool args."""
    tail = '\n'.join(text.rstrip().splitlines()[-18:])
    if not re.search(r'^\s*• Queued follow-up inputs\s*$',tail,re.M):
        return 0
    count = re.search(r'^\s*\?\s+(\d+) questions?\s*$',tail,re.M)
    if not count or not re.search(r'^\s*⇧← to answer\s*$',tail,re.M):
        return 0
    return int(count[1])


def sync_attention_overlay(con, sock, pane, pid, key, screen, previous, overlays):
    questions = queued_questions_screen(screen)
    reason = ('Approval dialog visible' if approval_screen(screen) else
              f'Queued follow-up questions: {questions}' if questions else None)
    if reason:
        overlays[key] = reason
        record(con,sock,pane,pid,'codex','WAIT',reason,source='observer')
    else:
        old_reason = overlays.pop(key,None)
        current = con.execute('SELECT state,reason FROM panes WHERE socket=? AND pane=? AND pid=?',(sock,pane,pid)).fetchone()
        # Recover our persisted screen state after an observer restart, too.
        if not old_reason and current and (current['reason'] == 'Approval dialog visible' or current['reason'].startswith('Queued follow-up questions: ')):
            old_reason = current['reason']
        if previous and current and tuple(current) == ('WAIT',old_reason):
            state,_,turn = previous
            record(con,sock,pane,pid,'codex',*state,event_key=turn,source='observer')


def discover(con, sock, live, table, attempted):
    state_db = Path.home()/'.codex/state_5.sqlite'
    for (pane,pane_pid),meta in live.items():
        candidates = native_codex_pids(pane_pid,table)
        if len(candidates) != 1:
            continue
        cli_pid = candidates[0]
        existing = con.execute('SELECT * FROM codex_sessions WHERE socket=? AND pane=? AND pid=?',(sock,pane,pane_pid)).fetchone()
        resumed = 'resume' in table[cli_pid][1].split()
        same_cli = bool(existing and existing['cli_pid'] == cli_pid)
        if not existing and cli_pid in attempted and time.monotonic()-attempted[cli_pid] < 60:
            continue
        attempted[cli_pid] = time.monotonic()
        try:
            cwd = process_cwd(cli_pid)
            origin = 'startup'
            match = None
            # /new and in-app resume keep the same PID and original argv.
            # Revalidate their foreground identity just like explicit resume.
            if resumed or existing:
                screen = tmux('capture-pane','-p','-J','-t',pane,check=False,sock=sock)
                match = resumed_match(state_db,cwd,screen)
                origin = 'resume'
            if not match and not resumed and not same_cli:
                match = startup_match(state_db,cwd,process_start(cli_pid))
                origin = 'startup'
        except (OSError,ValueError,subprocess.SubprocessError,sqlite3.Error):
            continue
        if not match:
            continue
        thread,path = match
        changed = bool(existing and (existing['thread_id'],existing['rollout_path']) != (thread,path))
        if existing and existing['cli_pid'] == cli_pid and not changed:
            continue
        latest = read_lifecycle(path)[1] if origin == 'resume' and changed else None
        with con:
            # Explicit hook registrations have priority over inferred startup matches.
            # A uniquely verified foreground thread can repair an old
            # native registration belonging to a different root conversation.
            if existing and existing['origin'] == 'native' and origin != 'resume':
                con.execute('UPDATE codex_sessions SET cli_pid=? WHERE socket=? AND pane=? AND pid=?',(cli_pid,sock,pane,pane_pid))
            else:
                con.execute('''INSERT INTO codex_sessions(socket,pane,pid,cli_pid,thread_id,rollout_path,origin)
                    VALUES(?,?,?,?,?,?,?) ON CONFLICT(socket,pane,pid) DO UPDATE SET
                    cli_pid=excluded.cli_pid,thread_id=excluded.thread_id,rollout_path=excluded.rollout_path,origin=excluded.origin''', (sock,pane,pane_pid,cli_pid,thread,path,origin))
                if changed:
                    # The former root's cached conversation is a different
                    # identity, even though the terminal PID stayed the same.
                    con.execute("""UPDATE panes SET transcript_path=?,preview_users='[]',
                        preview_agent='',preview_mtime=0,preview_revision=0
                        WHERE socket=? AND pane=? AND pid=? AND agent='codex'""",
                        (path,sock,pane,pane_pid))
                if latest:
                    current = con.execute('SELECT agent FROM panes WHERE socket=? AND pane=? AND pid=?',(sock,pane,pane_pid)).fetchone()
                    if current and current['agent'] == 'codex':
                        state,stamp,turn = latest
                        # Only a changed, verified mapping permits discarding the
                        # incompatible root's timestamp. Subsequent scans retain
                        # ordinary freshness and acknowledgement checks.
                        record(con,sock,pane,pane_pid,'codex',*state,event_time=stamp,event_key=turn,replace_identity=True,source='observer')


def retire_mapping(con,mapping):
    current = con.execute('SELECT * FROM panes WHERE socket=? AND pane=? AND pid=?',
                          (mapping['socket'],mapping['pane'],mapping['pid'])).fetchone()
    if current and current['agent'] == 'codex' and current['state'] in ('RUNNING','WAIT'):
        record(con,mapping['socket'],mapping['pane'],mapping['pid'],'codex','IDLE',
               'Agent exited; review current work',True,event_key='exit:'+str(mapping['cli_pid']),source='observer')


def repair_mapping(con, mapping, state_db):
    """One-time recovery from child hooks corrupting a root transcript/turn."""
    with closing(sqlite3.connect(state_db.as_uri()+'?mode=ro',uri=True,timeout=1)) as source:
        canonical = source.execute('SELECT rollout_path FROM threads WHERE id=?',(mapping['thread_id'],)).fetchone()
    if not canonical or not codex_root_transcript(canonical[0], mapping['thread_id']):
        return False
    _, event = read_lifecycle(canonical[0])
    with con:
        current = con.execute('SELECT * FROM panes WHERE socket=? AND pane=? AND pid=?',
                              (mapping['socket'],mapping['pane'],mapping['pid'])).fetchone()
        wrong_path = canonical[0] != mapping['rollout_path']
        wrong_turn = bool(current and event and current['event_key'] and event[2] and current['event_key'] != event[2])
        if not (wrong_path or wrong_turn):
            return False
        con.execute('UPDATE codex_sessions SET rollout_path=? WHERE socket=? AND pane=? AND pid=?',
                    (canonical[0],mapping['socket'],mapping['pane'],mapping['pid']))
        if event and current and current['agent'] == 'codex':
            state, _, turn = event
            # A child may have stamped its completion later than root task_started.
            # Recovery bypasses that invalid freshness stamp exactly once.
            record(con,mapping['socket'],mapping['pane'],mapping['pid'],'codex',*state,event_key=turn,source='observer')
    return True


def observe_codex_unmapped(con, sock, live, table):
    """Screen-track a unique foreground Codex when session→pane discovery has no row yet.

    Idle TUIs often lack a stable thread match; without this, status stays all zeros.
    """
    for (pane, pid), meta in live.items():
        if len(native_codex_pids(pid, table)) != 1:
            continue
        if con.execute('SELECT 1 FROM codex_sessions WHERE socket=? AND pane=? AND pid=?',
                       (sock, pane, pid)).fetchone():
            continue
        screen = tmux('capture-pane', '-p', '-J', '-t', pane, check=False, sock=sock)
        questions = queued_questions_screen(screen)
        if approval_screen(screen):
            record(con, sock, pane, pid, 'codex', 'WAIT', 'Approval dialog visible', source='observer')
            continue
        if questions:
            record(con, sock, pane, pid, 'codex', 'WAIT', f'Queued follow-up questions: {questions}', source='observer')
            continue
        if re.search(r'(?:• Working \(|esc to interrupt)', screen):
            record(con, sock, pane, pid, 'codex', 'RUNNING', 'Working', source='observer')
            continue
        if re.search(r'Ask Codex to do anything', screen) and re.search(r'← for agents', screen):
            record(con, sock, pane, pid, 'codex', 'IDLE', 'Waiting for next task', source='observer')


def observe():
    sock = resolve_socket()
    STATE.mkdir(parents=True,exist_ok=True)
    lock = observer_lock_path(sock)
    with lock.open('a') as handle:
        try:
            fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            return
        handle.seek(0); handle.truncate(); handle.write(str(os.getpid())); handle.flush()
        stopping = False
        def stop(*_):
            nonlocal stopping
            stopping = True
        signal.signal(signal.SIGTERM,stop)
        signal.signal(signal.SIGINT,stop)
        con = db()
        cursors, attempted, overlays = {},{},{}
        table = {}; refreshed = 0
        while not stopping and tmux('list-sessions',check=False,sock=sock):
            try:
                live = live_panes(sock=sock)
                if time.monotonic()-refreshed > 10:
                    table = processes(); refreshed = time.monotonic()
                    prune_stale_panes(con, sock, live)
                    discover(con,sock,live,table,attempted)
                observe_cursor(con,sock,live,table)
                observe_claude(con,sock,live,table)
                observe_pi(con,sock,live,table)
                observe_codex_unmapped(con,sock,live,table)
                for mapping in con.execute('SELECT * FROM codex_sessions WHERE socket=?',(sock,)).fetchall():
                    pane,pid = mapping['pane'],mapping['pid']
                    if (pane,pid) not in live:
                        continue
                    if mapping['rollout_path'] and not codex_root_transcript(mapping['rollout_path'], mapping['thread_id']):
                        repair_mapping(con,mapping,Path.home()/'.codex/state_5.sqlite')
                        continue
                    if mapping['cli_pid'] not in table:
                        if mapping['cli_pid']:
                            retire_mapping(con,mapping)
                        continue
                    key = (pane,pid,mapping['thread_id'],mapping['rollout_path'])
                    offset,previous = cursors.get(key,(0,None))
                    if mapping['rollout_path'] and Path(mapping['rollout_path']).is_file():
                        offset,event = read_lifecycle(mapping['rollout_path'],offset)
                        previous = event or previous
                        cursors[key] = (offset,previous)
                        if event:
                            state,stamp,turn = event
                            record(con,sock,pane,pid,'codex',*state,event_time=stamp,event_key=turn,source='observer')
                    # The UI can await human input while the main turn keeps running.
                    screen = tmux('capture-pane','-p','-t',pane,'-S','-35',check=False,sock=sock)
                    sync_attention_overlay(con,sock,pane,pid,key,screen,previous,overlays)
            except Exception as exc:
                log_observer_error(exc)
            time.sleep(2)
        con.close()
