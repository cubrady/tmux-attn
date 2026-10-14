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
import subprocess
import sys
from pathlib import Path
from urllib.request import Request, urlopen


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
    print(f'  2. sh {WEB_SERVICE}          # keep feed up; do not use bare `tmux-attn web`')
    print('  3. curl -s http://127.0.0.1:7788/api/health')
    print(f'  4. curl -s {url}/api/health')
    print('  5. Reload hooks: Claude new session | Codex /hooks | Cursor reopen | Pi /reload')
    print('Phone → http://<server-tailscale>:7788/')


def register_via_api(server_url: str, name: str, url: str) -> None:
    endpoint = server_url.rstrip('/') + '/api/peers'
    body = json.dumps({'name': name, 'url': url}).encode()
    request = Request(
        endpoint, data=body,
        headers={'Content-Type': 'application/json', 'Accept': 'application/json'},
        method='POST',
    )
    with urlopen(request, timeout=5) as response:
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
        ['ssh', ssh, 'python3', '-c', remote, name, url],
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
        action = upsert_local(name, url)
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
        apply_remote_ssh(server_ssh, name, url)
        print_client_next(name, url)
        return

    print('Preferred: set SERVER_URL=http://<server>:7788 (POST /api/peers).')
    print('Or paste the peer JSON into server ~/.config/tmux-attn/peers.json.')
    print_client_next(name, url)


if __name__ == '__main__':
    main()
