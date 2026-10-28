#!/usr/bin/env python3
"""Upgrade an installed tmux-attn from its git checkout.

Fetch, deploy only the runtime files that changed, restart only what needs it, verify,
and roll back on failure. It never touches tmux or the web service's bind address.

  python3 update.py [--check] [--no-fetch] [--ref REF] [--src DIR] [--reinstall]
  python3 update.py --rollback [BACKUP_DIR]

The plan and the deployed bytes come from git objects, never from the working tree, and the
checkout is fast-forwarded only after the new files are verified, so a failed update leaves
both the runtime and the checkout as they were. Observers are respawned by `tmux-attn status`,
which reads tmux but changes nothing in it.
"""
import argparse
import datetime
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.request import ProxyHandler, build_opener

HOME = Path.home()
RUNTIME = HOME / '.local/share/tmux-attn'
STATE = HOME / '.local/state/tmux-attn'
RECORD = 'INSTALL.json'
RUNTIME_FILES = (
    'attention.py', 'codex_observer.py', 'install_web_service.sh', 'web/index.html',
    'contrib/systemd/tmux-attn-web.service', 'contrib/launchd/ai.tmux-attn.web.plist',
    'contrib/launchd/web-run.sh',
)
RUNTIME_DIRS = ('web/icons',)
# Changing these means tmux or agent hooks may need re-installing, which update never does silently.
INSTALLER_INPUTS = ('install.py', 'bootstrap.sh', 'pi-attention.ts', 'opencode-attention.ts')
WEB_LABEL = 'ai.tmux-attn.web'
WEB_UNIT = 'tmux-attn-web'
HEALTH_WAIT_S = 20.0
OBSERVER_STOP_WAIT_S = 5.0
FETCH_TIMEOUT_S = 120
SHA = re.compile(r'[0-9a-f]{40}')


class UpdateError(Exception):
    pass


# ---------------------------------------------------------------- git

def git(src, *args, check=True, binary=False, timeout=None):
    env = {**os.environ, 'GIT_TERMINAL_PROMPT': '0'}
    try:
        proc = subprocess.run(['git', '-C', str(src), *args], capture_output=True, env=env, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise UpdateError(f"git {' '.join(args)}: {error}") from error
    if check and proc.returncode:
        raise UpdateError(f"git {' '.join(args)}: {proc.stderr.decode(errors='replace').strip() or proc.returncode}")
    return proc.stdout if binary else proc.stdout.decode().strip()


def git_ok(src, *args):
    try:
        return subprocess.run(['git', '-C', str(src), *args], capture_output=True).returncode == 0
    except OSError:
        return False


def safe_rel(rel):
    """A relative path that cannot leave the directory it is joined to."""
    path = PurePosixPath(rel)
    return bool(path.parts) and not path.is_absolute() and all(part not in ('', '.', '..') for part in path.parts)


def tree_files(src, commit):
    """The managed runtime files exactly as `commit` has them: {relative path: bytes}."""
    wanted = list(RUNTIME_FILES)
    listing = git(src, 'ls-tree', '-r', '-z', '--full-tree', commit, '--', *RUNTIME_DIRS, binary=True)
    for entry in listing.split(b'\0'):
        meta, _, name = entry.partition(b'\t')
        rel = name.decode('utf-8', 'surrogateescape')
        # Regular files under the managed directories only; a hostile tree must not name paths outside them.
        if (meta.split()[:1] in ([b'100644'], [b'100755']) and safe_rel(rel)
                and rel.startswith(tuple(f'{d}/' for d in RUNTIME_DIRS))):
            wanted.append(rel)
    files = {}
    for rel in wanted:
        proc = subprocess.run(['git', '-C', str(src), 'cat-file', 'blob', f'{commit}:{rel}'], capture_output=True)
        if proc.returncode == 0:
            files[rel] = proc.stdout
    if 'attention.py' not in files:
        raise UpdateError(f'{commit[:12]} has no attention.py; is {src} a tmux-attn checkout?')
    return files


def installer_inputs_changed(src, old, new):
    """True when hooks or tmux config may need a re-install between two commits."""
    if not old or old == new or not git_ok(src, 'cat-file', '-e', f'{old}^{{commit}}'):
        return False
    if git(src, 'diff', '--name-only', old, new, '--', *INSTALLER_INPUTS):
        return True
    return bool(re.search(r'^[+-].*(VIEW_HOOK|view_hooks_config)', git(src, 'diff', '-U0', old, new, '--', 'attention.py'), re.M))


def changed_files(src, old, new):
    if not old or old == new or not git_ok(src, 'cat-file', '-e', f'{old}^{{commit}}'):
        return ()
    return tuple(git(src, 'diff', '--name-only', old, new).splitlines())


# ---------------------------------------------------------------- runtime

def read_record(runtime):
    """INSTALL.json with every field validated; anything malformed is dropped rather than trusted."""
    try:
        data = json.loads((Path(runtime) / RECORD).read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    record = {}
    for key in ('commit', 'installer_commit'):
        if isinstance(data.get(key), str) and SHA.fullmatch(data[key]):
            record[key] = data[key]
    for key in ('version', 'ref', 'updated'):
        if isinstance(data.get(key), str):
            record[key] = data[key]
    if isinstance(data.get('source'), str) and os.path.isabs(data['source']):
        record['source'] = data['source']
    return record


def runtime_files(runtime):
    runtime = Path(runtime)
    files = {}
    for rel in RUNTIME_FILES:
        if (runtime / rel).is_file():
            files[rel] = (runtime / rel).read_bytes()
    for directory in RUNTIME_DIRS:
        for path in sorted((runtime / directory).rglob('*')):
            if path.is_file():
                files[str(path.relative_to(runtime))] = path.read_bytes()
    return files


@dataclass(frozen=True)
class Plan:
    commit: str
    previous: str
    write: tuple
    remove: tuple
    restart_web: bool
    restart_observer: bool
    record: bool
    reinstall_hint: bool

    @property
    def empty(self):
        return not (self.write or self.remove or self.record)


def make_plan(wanted, have, commit, previous, reinstall_hint=False):
    write = tuple(rel for rel in wanted if have.get(rel) != wanted[rel])
    remove = tuple(rel for rel in have if rel not in wanted and rel.startswith(RUNTIME_DIRS))
    restart_web = 'attention.py' in write
    return Plan(
        commit=commit, previous=previous, write=write, remove=remove,
        restart_web=restart_web,
        # The observer is `attention.py observe`, so it runs stale code after either file changes.
        restart_observer=restart_web or 'codex_observer.py' in write,
        record=previous != commit, reinstall_hint=reinstall_hint,
    )


def write_atomic(path, data, executable=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f'.{path.name}.update-{os.getpid()}')
    try:
        temp.write_bytes(data)
        temp.chmod(0o755 if executable else 0o644)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def write_record(runtime, commit, src, ref, installer_commit=None):
    data = {
        'version': commit[:12], 'commit': commit, 'source': str(src), 'ref': ref,
        'updated': datetime.datetime.now().astimezone().isoformat(timespec='seconds'),
    }
    if installer_commit:
        data['installer_commit'] = installer_commit
    write_atomic(Path(runtime) / RECORD, (json.dumps(data, indent=2) + '\n').encode())


def record_install(runtime, src):
    """For install.py: remember which commit was installed, or forget when that cannot be known."""
    runtime = Path(runtime)
    try:
        dirty = git(src, 'status', '--porcelain', '--untracked-files=no')
        commit = git(src, 'rev-parse', 'HEAD')
    except UpdateError:
        dirty, commit = 'unknown', ''
    if dirty or not SHA.fullmatch(commit):
        (runtime / RECORD).unlink(missing_ok=True)
        return False
    write_record(runtime, commit, src, 'install', installer_commit=commit)
    return True


def backup_current(runtime, state, plan):
    """Copy everything the update will replace, so `--rollback` can undo it."""
    stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    backup = Path(state) / 'backups' / f'update-{stamp}'
    runtime = Path(runtime)
    created = []
    for rel in (*plan.write, *plan.remove, RECORD):
        if (runtime / rel).is_file():
            (backup / 'files' / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(runtime / rel, backup / 'files' / rel)
        elif rel != RECORD:
            created.append(rel)
    meta = {
        'runtime': str(runtime), 'from': plan.previous, 'to': plan.commit, 'created': created,
        'restarted_web': plan.restart_web, 'restarted_observer': plan.restart_observer,
        'had_record': (runtime / RECORD).is_file(),
    }
    backup.mkdir(parents=True, exist_ok=True)
    (backup / 'meta.json').write_text(json.dumps(meta, indent=2) + '\n')
    return backup


# ---------------------------------------------------------------- services

def parse_web_endpoint(text):
    """`... web <host> <port>` or `.../web-run.sh <host> <port>` as launchd/systemd print it -> (host, port) or None."""
    tokens = [t for t in re.split(r'[\s;{}=]+', text) if t]
    for i, token in enumerate(tokens[:-2]):
        if (token == 'web' or token.endswith('/web-run.sh')) and tokens[i + 2].isdigit():
            return tokens[i + 1], int(tokens[i + 2])
    return None


def parse_observers(ps_output, app, own_pid=None):
    """pids of `<python> <app> observe`, matched on the exact command so nothing else is ever hit."""
    wanted = f'{app} observe'
    pids = []
    for line in ps_output.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        command = parts[1].strip()
        # macOS framework Python runs as `.../MacOS/Python`, so the name check is case-insensitive.
        if (command.endswith(wanted) and Path(command.split(None, 1)[0]).name.lower().startswith('python')
                and int(parts[0]) != own_pid):
            pids.append(int(parts[0]))
    return pids


def local_url(host, port, path):
    host = '127.0.0.1' if host in ('0.0.0.0', '::') else host
    return f'http://[{host}]:{port}{path}' if ':' in host else f'http://{host}:{port}{path}'


class SystemServices:
    """Everything update does to the live machine; tests replace this whole class."""

    def __init__(self, runtime=RUNTIME, home=HOME):
        self.runtime = Path(runtime)
        self.home = Path(home)
        self._web_text = None

    @staticmethod
    def _run(args):
        return subprocess.run(args, capture_output=True, text=True)

    def _web(self):
        if self._web_text is None:
            self._web_text = ''
            if sys.platform == 'darwin':
                probe = self._run(['launchctl', 'print', f'gui/{os.getuid()}/{WEB_LABEL}'])
                if probe.returncode == 0:
                    self._web_text = 'launchd\n' + probe.stdout
            elif shutil.which('systemctl'):
                probe = self._run(['systemctl', '--user', 'show', WEB_UNIT, '-p', 'ActiveState', '-p', 'ExecStart'])
                if probe.returncode == 0 and 'ActiveState=active' in probe.stdout:
                    self._web_text = 'systemd\n' + probe.stdout
        return self._web_text

    def has_web_service(self):
        return bool(self._web())

    def web_endpoint(self):
        return parse_web_endpoint(self._web())

    def restart_web(self):
        if self._web().startswith('launchd'):
            args = ['launchctl', 'kickstart', '-k', f'gui/{os.getuid()}/{WEB_LABEL}']
        else:
            args = ['systemctl', '--user', 'restart', WEB_UNIT]
        result = self._run(args)
        if result.returncode:
            raise UpdateError(f"{' '.join(args)}: {(result.stderr or result.stdout).strip()}")

    def fetch(self, host, port, path):
        # Never through a proxy: this is the machine talking to itself.
        with build_opener(ProxyHandler({})).open(local_url(host, port, path), timeout=3) as response:
            return response.read(1 << 20)

    def observers(self):
        out = self._run(['ps', '-axo', 'pid=,args=']).stdout
        return parse_observers(out, self.runtime / 'attention.py', os.getpid())

    def restart_observers(self, _planned=()):
        """Stop the observers that exist right now, then let `tmux-attn status` respawn them."""
        pids = self.observers()
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except PermissionError as error:
                raise UpdateError(f'cannot signal observer {pid}: {error}') from error
        deadline = time.monotonic() + OBSERVER_STOP_WAIT_S
        while time.monotonic() < deadline and any(pid in self.observers() for pid in pids):
            time.sleep(0.2)
        if any(pid in self.observers() for pid in pids):
            raise UpdateError('observer did not stop after SIGTERM')
        try:
            self._run([str(self.home / '.local/bin/tmux-attn'), 'status'])
        except OSError as error:
            raise UpdateError(f'could not run tmux-attn status to respawn the observer: {error}') from error

    def reinstall(self, src, sidebar):
        args = ['sh', str(Path(src) / 'bootstrap.sh'), *(['--sidebar'] if sidebar else [])]
        result = subprocess.run(args, cwd=src)
        if result.returncode:
            raise UpdateError(f'bootstrap.sh exited {result.returncode}')

    def uses_sidebar(self):
        try:
            text = (self.home / '.config/tmux-attn/sidebar.conf').read_text()
        except OSError:
            return False
        return 'display-popup' not in text and ' focus ' in text


# ---------------------------------------------------------------- source and refs

def looks_like_checkout(path):
    return (Path(path) / '.git').exists() and (Path(path) / 'attention.py').is_file()


def resolve_source(explicit, runtime, environ=None, own_dir=None):
    environ = os.environ if environ is None else environ
    own_dir = Path(__file__).resolve().parent if own_dir is None else Path(own_dir)
    candidates = [explicit, environ.get('TMUX_ATTN_SRC'), own_dir if looks_like_checkout(own_dir) else None,
                  read_record(runtime).get('source')]
    for candidate in candidates:
        if candidate:
            if not looks_like_checkout(candidate):
                raise UpdateError(f'{candidate} is not a tmux-attn git checkout')
            return Path(candidate).resolve()
    raise UpdateError('cannot find the tmux-attn checkout; pass --src DIR or set TMUX_ATTN_SRC')


def default_ref(src):
    upstream = git(src, 'rev-parse', '--abbrev-ref', '--symbolic-full-name', '@{upstream}', check=False)
    if upstream and '@{' not in upstream:
        return upstream
    if git_ok(src, 'rev-parse', '--verify', '--quiet', 'origin/HEAD'):
        return 'origin/HEAD'
    raise UpdateError('the checkout has no upstream branch; pass --ref')


def choose_target(src, ref, explicit):
    """The commit to deploy. Moves only forward: a checkout that is ahead keeps its own HEAD."""
    head = git(src, 'rev-parse', 'HEAD')
    target = git(src, 'rev-parse', '--verify', f'{ref}^{{commit}}')
    if target == head or git_ok(src, 'merge-base', '--is-ancestor', head, target):
        return target
    if git_ok(src, 'merge-base', '--is-ancestor', target, head):
        if explicit:
            raise UpdateError(f'{ref} is behind this checkout; update only moves forward (use --rollback to undo)')
        return head
    raise UpdateError(f'this checkout has diverged from {ref}; resolve it by hand')


def sync_checkout(src, target, out):
    """Fast-forward the checkout to what was deployed; run on every update so a failure here converges later."""
    if git(src, 'rev-parse', 'HEAD') != target:
        out(f'fast-forwarding {src} to {target[:12]}')
        git(src, 'merge', '--ff-only', target)


# ---------------------------------------------------------------- the update

def describe(plan):
    lines = [f"version: {plan.previous[:12] or 'unrecorded'} -> {plan.commit[:12]}"]
    lines += [f'  write   {rel}' for rel in plan.write]
    lines += [f'  remove  {rel}' for rel in plan.remove]
    if plan.restart_web:
        lines.append('  restart web service')
    if plan.restart_observer:
        lines.append('  restart observers')
    if plan.reinstall_hint:
        lines.append('  note    hooks or tmux config changed upstream; run again with --reinstall to refresh them')
    return lines


def update(options, services=None, runtime=None, state=None, out=print):
    runtime = Path(runtime or options.runtime or RUNTIME)
    state = Path(state or options.state or STATE)
    services = services or SystemServices(runtime)
    if not (runtime / 'attention.py').is_file():
        raise UpdateError(f'tmux-attn is not installed in {runtime}; run install.sh first')
    src = resolve_source(options.src, runtime)
    dirty = git(src, 'status', '--porcelain', '--untracked-files=no')
    if dirty:
        raise UpdateError(f'{src} has uncommitted changes; commit or stash them first:\n{dirty}')
    if not options.no_fetch:
        git(src, 'fetch', '--quiet', '--tags', '--force', timeout=FETCH_TIMEOUT_S)
    ref = options.ref or default_ref(src)
    target = choose_target(src, ref, bool(options.ref))
    record = read_record(runtime)
    previous = record.get('commit', '')
    wanted = tree_files(src, target)
    plan = make_plan(wanted, runtime_files(runtime), target, previous,
                     installer_inputs_changed(src, record.get('installer_commit', ''), target))
    for line in describe(plan):
        out(line)
    behind = git(src, 'rev-parse', 'HEAD') != target
    if options.check:
        out('update available (dry run, nothing changed)' if not plan.empty or behind else 'already up to date')
        return 0
    if plan.empty and not options.reinstall:
        sync_checkout(src, target, out)
        out('already up to date')
        return 0
    observers = services.observers() if plan.restart_observer else []
    backup = backup_current(runtime, state, plan)
    try:
        deploy(plan, wanted, runtime, src, ref, record, services, observers, out)
    except BaseException as error:  # includes Ctrl-C and a dropped ssh session: never leave a half-deployed runtime
        out(f'update failed ({type(error).__name__}: {error}); rolling back from {backup}')
        try:
            rollback(backup, services, out)
        except Exception as rollback_error:
            out(f'rollback also failed ({rollback_error}); restore by hand from {backup}')
        if isinstance(error, OSError):
            raise UpdateError(f'{type(error).__name__}: {error}') from error
        raise
    sync_checkout(src, target, out)
    if options.reinstall:
        out('re-running the installer (bootstrap.sh)')
        services.reinstall(src, services.uses_sidebar())
        write_record(runtime, target, src, ref, installer_commit=target)
    if 'update.py' in changed_files(src, previous, target):
        out('note: update.py itself changed; run update once more to apply it with the new updater')
    out(f'updated to {target[:12]}; undo with: python3 {src / "update.py"} --rollback {backup}')
    return 0


def deploy(plan, wanted, runtime, src, ref, record, services, observers, out):
    for rel in (*plan.write, *plan.remove):
        if not safe_rel(rel):
            raise UpdateError(f'refusing unsafe path {rel!r}')
    for rel in plan.write:
        write_atomic(runtime / rel, wanted[rel], executable=rel.endswith('.sh'))
    for rel in plan.remove:
        (runtime / rel).unlink(missing_ok=True)
    write_record(runtime, plan.commit, src, ref, installer_commit=record.get('installer_commit'))
    for rel in plan.write:
        if (runtime / rel).read_bytes() != wanted[rel]:
            raise UpdateError(f'{rel} does not match the commit after writing')
    if plan.restart_web:
        restart_and_check_web(plan, services, out)
    elif 'web/index.html' in plan.write and services.has_web_service():
        check_served_page(services, wanted['web/index.html'], out)
    if plan.restart_observer and observers:
        restart_observers(services, observers, out)


def restart_observers(services, observers, out):
    # A missing observer is not worth undoing a good update: tmux's status tick starts one again.
    try:
        services.restart_observers(observers)
    except (UpdateError, OSError) as error:
        out(f'warning: observers not restarted ({error}); they pick up the new code the next time they start')
        return
    if services.observers():
        out(f'observers restarted ({len(observers)})')
    else:
        out('warning: no observer is running yet; the next tmux status refresh will start one')


def restart_and_check_web(plan, services, out):
    if not services.has_web_service():
        out('no web service found; if you run `tmux-attn web` by hand, restart it yourself')
        return
    services.restart_web()
    endpoint = services.web_endpoint()
    if endpoint is None:
        out('web service restarted; could not read its address, so health was not checked')
        return
    deadline = time.monotonic() + HEALTH_WAIT_S
    last = 'no answer'
    while time.monotonic() < deadline:
        try:
            health = json.loads(services.fetch(*endpoint, '/api/health'))
            # started_version is what the process loaded; `version` is re-read from disk and proves nothing.
            loaded = health.get('started_version')
            if health.get('ok') and (loaded is None or loaded == plan.commit[:12]):
                out(f'web service healthy at {endpoint[0]}:{endpoint[1]} (version {loaded or "not reported"})')
                return
            last = f'health says {health}'
        except (OSError, ValueError) as error:
            last = str(error)
        time.sleep(0.5)
    raise UpdateError(f'web service not healthy after restart: {last}')


def check_served_page(services, expected, out):
    endpoint = services.web_endpoint()
    if endpoint is None:
        return
    try:
        served = services.fetch(*endpoint, '/')
    except (OSError, ValueError) as error:
        out(f'warning: could not fetch the page to confirm it ({error})')
        return
    if served == expected:
        out('web service is serving the new page')
    else:
        out('warning: the running web service serves a different page than the one just installed; '
            'it may be started from another directory')


def rollback(backup, services, out=print, runtime=None, require_current=False):
    backup = Path(backup)
    try:
        meta = json.loads((backup / 'meta.json').read_text())
        target_runtime = Path(meta['runtime'])
        created, restored_to, restored_from = list(meta['created']), meta['to'], meta['from']
    except (OSError, ValueError, KeyError, TypeError):
        raise UpdateError(f'{backup} is not an update backup')
    if runtime is not None and Path(runtime) != target_runtime:
        raise UpdateError(f'{backup} was taken for {target_runtime}, not {runtime}')
    if require_current:
        # An older backup would restore only the files it holds and leave a mixed runtime.
        current = read_record(target_runtime).get('commit')
        if current and current != restored_to:
            raise UpdateError(f'{backup} undoes an update to {restored_to[:12]} but this runtime is at '
                              f'{current[:12]}; roll back from the most recent backup instead')
    saved = sorted(p.relative_to(backup / 'files') for p in (backup / 'files').rglob('*') if p.is_file())
    for rel in (*map(str, saved), *created):
        if not safe_rel(rel):
            raise UpdateError(f'refusing unsafe path {rel!r} in {backup}')
    for rel in saved:
        write_atomic(target_runtime / rel, (backup / 'files' / rel).read_bytes(), executable=str(rel).endswith('.sh'))
    for rel in created:
        (target_runtime / rel).unlink(missing_ok=True)
    if not meta['had_record']:
        (target_runtime / RECORD).unlink(missing_ok=True)
    out(f"restored files from {backup} (was {restored_to[:12]}, back to {restored_from[:12] or 'unrecorded'})")
    if meta['restarted_web'] and services.has_web_service():
        services.restart_web()
        out('web service restarted on the restored files')
    if meta['restarted_observer']:
        pids = services.observers()
        if pids:
            restart_observers(services, pids, out)
    return 0


def latest_backup(state):
    backups = sorted((Path(state) / 'backups').glob('update-*'))
    if not backups:
        raise UpdateError('no update backups found')
    return backups[-1]


def build_parser():
    parser = argparse.ArgumentParser(description='Upgrade an installed tmux-attn from its git checkout.')
    parser.add_argument('--check', action='store_true', help='show what would change and exit')
    parser.add_argument('--no-fetch', action='store_true', help='deploy what the checkout already has')
    parser.add_argument('--ref', help='commit, tag or branch to move to (default: the checkout upstream)')
    parser.add_argument('--src', help='tmux-attn git checkout (default: this file, $TMUX_ATTN_SRC, or the recorded one)')
    parser.add_argument('--reinstall', action='store_true', help='also re-run the installer to refresh hooks and tmux config')
    parser.add_argument('--rollback', nargs='?', const='latest', metavar='BACKUP_DIR', help='undo an update from its backup')
    parser.add_argument('--runtime', help=argparse.SUPPRESS)
    parser.add_argument('--state', help=argparse.SUPPRESS)
    return parser


def main(argv=None):
    options = build_parser().parse_args(argv)
    # A dropped ssh session sends SIGHUP; turn it into the same cleanup path as Ctrl-C.
    signal.signal(signal.SIGHUP, signal.default_int_handler)
    try:
        if options.rollback is not None:
            state = Path(options.state or STATE)
            runtime = Path(options.runtime or RUNTIME)
            backup = latest_backup(state) if options.rollback == 'latest' else Path(options.rollback)
            return rollback(backup, SystemServices(runtime), runtime=runtime, require_current=True)
        return update(options)
    except UpdateError as error:
        print(f'error: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
