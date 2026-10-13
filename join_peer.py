#!/usr/bin/env python3
"""Register a client on the Attention server (or print what to add).

Setups:
  solo   — one machine; no peers.json
  multi  — one or more clients + one server

Env (multi client):
  PEER_NAME         panel label
  LOCAL_FEED_URL    this client's web root (http://100.x.x.x:7788)
  SERVER_SSH        optional user@host — SSH to server and --apply there
  PEERS_PATH        optional (default ~/.config/tmux-attn/peers.json)

Flags:
  --solo     Solo checklist (no peer env)
  --apply    Upsert peers.json on THIS machine (run on the server)
"""
from __future__ import annotations
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def die(msg: str, code: int = 2) -> None:
    print(msg, file=sys.stderr)
    raise SystemExit(code)


def peers_path() -> Path:
    return Path(os.environ.get('PEERS_PATH') or Path.home() / '.config/tmux-attn/peers.json')


def upsert(path: Path, name: str, url: str) -> str:
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


def print_solo() -> None:
    print('Setup: solo (one machine — no peers.json)')
    print('NEXT STEPS:')
    print('  1. ~/.local/bin/tmux-attn web')
    print('  2. curl -s http://127.0.0.1:7788/api/health')
    print('  3. Open http://127.0.0.1:7788/ (or Tailscale:7788)')
    print('  4. Reload hooks: Claude new session | Codex /hooks | Cursor reopen | Pi /reload')


def print_client_next(name: str, url: str) -> None:
    print()
    print('NEXT STEPS (CLIENT):')
    print(f'  1. export TMUX_ATTN_HOST={name!r}')
    print('  2. Keep running: ~/.local/bin/tmux-attn web')
    print('  3. curl -s http://127.0.0.1:7788/api/health')
    print(f'  4. curl -s {url}/api/health')
    print('  5. Reload hooks: Claude new session | Codex /hooks | Cursor reopen | Pi /reload')
    print('SERVER: peers.json includes this client + tmux-attn web running')
    print('Phone → http://<server-tailscale>:7788/')


def apply_remote(ssh: str, name: str, url: str) -> None:
    # Keep remote snippet tiny; no need to ship the whole file.
    remote = r'''
import json, sys
from pathlib import Path
name, url = sys.argv[1], sys.argv[2]  # noqa: passed after python3 -c
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
        die(f'ssh {ssh} failed — give peer JSON to the user to add on the server', 1)
    print(proc.stdout.strip())
    print(f'Server peers updated via {ssh}')


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
    # Prefer SERVER_SSH; keep AGGREGATOR_SSH as alias for old prompts
    server_ssh = (os.environ.get('SERVER_SSH') or os.environ.get('AGGREGATOR_SSH') or '').strip()

    if not name or not url:
        die(
            'Need PEER_NAME + LOCAL_FEED_URL for multi-client (do not guess).\n'
            '  Solo:            python3 join_peer.py --solo\n'
            '  Client:          PEER_NAME=… LOCAL_FEED_URL=http://…:7788 python3 join_peer.py\n'
            '  Apply on server: PEER_NAME=… LOCAL_FEED_URL=… python3 join_peer.py --apply\n'
            '  Apply via SSH:   … SERVER_SSH=user@host python3 join_peer.py'
        )
    if not url.startswith(('http://', 'https://')):
        die(f'LOCAL_FEED_URL must be http(s)://… got {url!r}')

    print('Setup: multi')
    print(json.dumps({'name': name, 'url': url}, ensure_ascii=False, indent=2))
    print()

    if args.apply:
        action = upsert(peers_path(), name, url)
        print(f'Server local: {action} {name!r} → {peers_path()}')
        print('Keep server web up: ~/.local/bin/tmux-attn web')
        print_client_next(name, url)
        return

    if server_ssh:
        apply_remote(server_ssh, name, url)
        print_client_next(name, url)
        return

    print('Hand this peer to the SERVER peers.json, or rerun with SERVER_SSH=user@host.')
    print_client_next(name, url)


if __name__ == '__main__':
    main()
