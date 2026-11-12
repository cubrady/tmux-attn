#!/usr/bin/env python3
"""Register a client on the Attention server (or print what to add).

Setups:
  solo   — one machine; no peers.json
  multi  — clients + one server

Env (multi client):
  PEER_NAME         panel label
  LOCAL_FEED_URL    this client's web root (http://100.x.x.x:7788)
  SERVER_URL        preferred — POST {name,url} to SERVER_URL/api/peers
  SERVER_SSH        fallback — ssh and write peers.json (legacy)
  PEERS_PATH        optional when using --apply locally

Flags:
  --solo     Solo checklist
  --apply    Upsert peers.json on THIS machine (run on the server)
"""
from __future__ import annotations
import argparse
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request
from peer_transport import TOKEN_RE, open_peer, validate_peer_url
from web_security import load_owner_token


def die(msg: str, code: int = 2) -> None:
    print(msg, file=sys.stderr)
    raise SystemExit(code)


def peers_path() -> Path:
    return Path(os.environ.get('PEERS_PATH') or Path.home() / '.config/tmux-attn/peers.json')


def upsert_local(name: str, url: str) -> str:
    path = peers_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {'peers': []}
    if path.is_file():
        try:
            data = json.loads(path.read_text() or '{}')
        except json.JSONDecodeError as exc:
            die(f'Invalid JSON in {path}: {exc}')
    peers = list(data.get('peers') or [])
    action = 'added'
    for row in peers:
        if isinstance(row, dict) and row.get('name') == name:
            row['url'] = url
            action = 'updated'
            break
    else:
        peers.append({'name': name, 'url': url})
    data['peers'] = peers
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    return action


WEB_SERVICE = '~/.local/share/tmux-attn/install_web_service.sh'


def print_solo() -> None:
    print('Setup: solo (one machine — no peers.json)')
    print('NEXT STEPS:')
    print(f'  1. sh {WEB_SERVICE}          # launchd / systemd — survives logout')
    print('  2. curl -s http://127.0.0.1:7788/api/health')
    print('  3. Open http://127.0.0.1:7788/ (or Tailscale:7788)')
    print('  4. Reload hooks: Claude new session | Codex /hooks | Cursor reopen | Pi /reload')


def print_client_next(name: str, url: str) -> None:
    print()
    print('NEXT STEPS (CLIENT):')
    print(f'  1. export TMUX_ATTN_HOST={name!r}')
    bind = urlsplit(url).hostname if urlsplit(url).scheme == 'http' else '127.0.0.1'
    print(f'  2. TMUX_ATTN_WEB_HOST={shlex.quote(bind or "127.0.0.1")} sh {WEB_SERVICE}')
    print('  3. curl -s http://127.0.0.1:7788/api/health')
    print(f'  4. curl -s {url}/api/health')
    print('  5. Reload hooks: Claude new session | Codex /hooks | Cursor reopen | Pi /reload')
    print('Phone → http://<server-tailscale>:7788/')
    print('The browser requires the server access key; machine requests use the enrolled peer key.')


def register_via_api(server_url: str, name: str, url: str) -> None:
    server_token = os.environ.get('SERVER_TOKEN') or ''
    if not TOKEN_RE.fullmatch(server_token):
        die('SERVER_TOKEN is required; obtain it with tmux-attn auth-token on the server.')
    try:
        server_url = validate_peer_url(server_url)
        url = validate_peer_url(url)
    except ValueError as exc:
        die(str(exc))
    key_path = Path(os.environ.get('TMUX_ATTN_AUTH_KEY_FILE') or Path.home() / '.config/tmux-attn/auth.key')
    key_path = key_path.expanduser()
    if not key_path.is_absolute():
        die('TMUX_ATTN_AUTH_KEY_FILE must be an absolute path')
    token = load_owner_token(key_path)
    endpoint = server_url.rstrip('/') + '/api/peers'
    body = json.dumps({'name': name, 'url': url, 'token': token}).encode()
    request = Request(
        endpoint, data=body,
        headers={'Content-Type': 'application/json', 'Accept': 'application/json'},
        method='POST',
    )
    with open_peer(request, server_token, timeout=5) as response:
        data = json.loads(response.read().decode())
    if not data.get('ok'):
        die(f'server rejected peer: {data.get("error") or data}')
    print(f'Registered via API {endpoint} ({data.get("action")})')


def apply_remote_ssh(ssh: str, name: str, url: str) -> None:
    remote = r'''
import json, sys
from pathlib import Path
name, url = sys.argv[1], sys.argv[2]
path = Path.home() / ".config/tmux-attn/peers.json"
path.parent.mkdir(parents=True, exist_ok=True)
data = {"peers": []}
if path.is_file():
    data = json.loads(path.read_text() or "{}")
peers = list(data.get("peers") or [])
for row in peers:
    if isinstance(row, dict) and row.get("name") == name:
        row["url"] = url
        break
else:
    peers.append({"name": name, "url": url})
data["peers"] = peers
path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
print("wrote", path)
'''
    proc = subprocess.run(
        ['ssh', ssh, shlex.join(['python3', '-c', remote, name, url])],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        print(proc.stderr.strip() or proc.stdout.strip(), file=sys.stderr)
        die(f'ssh {ssh} failed — use SERVER_URL=/api/peers or paste JSON on server', 1)
    print(proc.stdout.strip())
    print(f'Server peers updated via ssh {ssh}')


def main() -> None:
    ap = argparse.ArgumentParser(description='tmux-attn join helper')
    ap.add_argument('--solo', action='store_true')
    ap.add_argument('--apply', action='store_true', help='Write peers.json here (server)')
    args = ap.parse_args()

    if args.solo or (os.environ.get('SETUP') or '').strip().lower() == 'solo':
        print_solo()
        return

    name = (os.environ.get('PEER_NAME') or '').strip()
    url = (os.environ.get('LOCAL_FEED_URL') or '').strip().rstrip('/')
    server_url = (os.environ.get('SERVER_URL') or '').strip()
    server_ssh = (os.environ.get('SERVER_SSH') or os.environ.get('AGGREGATOR_SSH') or '').strip()

    if not name or not url:
        die(
            'Need PEER_NAME + LOCAL_FEED_URL for multi-client (do not guess).\n'
            '  Solo:   python3 join_peer.py --solo\n'
            '  Client: PEER_NAME=… LOCAL_FEED_URL=http://…:7788 SERVER_URL=http://server:7788 python3 join_peer.py\n'
            '  Server: PEER_NAME=… LOCAL_FEED_URL=… python3 join_peer.py --apply'
        )
    if not url.startswith(('http://', 'https://')):
        die(f'LOCAL_FEED_URL must be http(s)://… got {url!r}')

    print('Setup: multi')
    print(json.dumps({'name': name, 'url': url}, ensure_ascii=False, indent=2))
    print()

    if args.apply:
        token = os.environ.get('PEER_TOKEN', '')
        if not TOKEN_RE.fullmatch(token):
            die('PEER_TOKEN is required for --apply; obtain it on the peer with tmux-attn auth-token.')
        # Use the same locked, private, atomic writer as authenticated enrollment.
        from attention import upsert_peer
        ok, action = upsert_peer(name, url, token, config_path=peers_path())
        if not ok:
            die(action)
        print(f'Server local: {action} {name!r} → {peers_path()}')
        print(f'Keep server web up: sh {WEB_SERVICE}')
        print_client_next(name, url)
        return

    if server_url:
        try:
            register_via_api(server_url, name, url)
        except Exception as exc:
            die(f'SERVER_URL register failed: {type(exc).__name__}: {exc}', 1)
        print_client_next(name, url)
        return

    if server_ssh:
        die('Use SERVER_URL with SERVER_TOKEN for authenticated enrollment, or --apply with PEER_TOKEN on the server.')

    print('Preferred: set SERVER_URL=http://<server>:7788 (POST /api/peers).')
    print('Or paste the peer JSON into server ~/.config/tmux-attn/peers.json.')
    print_client_next(name, url)


if __name__ == '__main__':
    main()
