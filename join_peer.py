#!/usr/bin/env python3
"""Upsert a worker into ~/.config/tmux-attn/peers.json and print join checklist.

Env:
  PEER_NAME        required (fleet label)
  LOCAL_FEED_URL   required (http://host:7788 — worker web root)
  AGGREGATOR       A|B|C (default A)
    A = print JSON only (user edits Attention Server)
    B = write local peers.json (this machine is aggregator)
    C = print JSON + remote SSH hint (needs AGGREGATOR_SSH)

  AGGREGATOR_SSH   optional, e.g. user@attention (mode C hint only)
  PEERS_PATH       optional override for peers.json
"""
from __future__ import annotations
import json
import os
import sys
from pathlib import Path


def die(msg: str, code: int = 2) -> None:
    print(msg, file=sys.stderr)
    raise SystemExit(code)


def main() -> None:
    name = (os.environ.get('PEER_NAME') or '').strip()
    url = (os.environ.get('LOCAL_FEED_URL') or '').strip().rstrip('/')
    mode = (os.environ.get('AGGREGATOR') or 'A').strip().upper() or 'A'
    ssh = (os.environ.get('AGGREGATOR_SSH') or '').strip()
    peers_path = Path(os.environ.get('PEERS_PATH') or Path.home() / '.config/tmux-attn/peers.json')

    if not name or not url:
        die('Set PEER_NAME and LOCAL_FEED_URL (do not guess). Example:\n'
            '  PEER_NAME=brad-mba LOCAL_FEED_URL=http://100.x.x.x:7788 AGGREGATOR=A python3 join_peer.py')
    if not url.startswith('http://') and not url.startswith('https://'):
        die(f'LOCAL_FEED_URL must be http(s)://… got {url!r}')
    if mode not in ('A', 'B', 'C'):
        die('AGGREGATOR must be A, B, or C')

    peer = {'name': name, 'url': url}
    print('Peer object:')
    print(json.dumps(peer, ensure_ascii=False, indent=2))
    print()

    if mode == 'A':
        print('Mode A — give the object above to the user to merge into Attention Server:')
        print('  ~/.config/tmux-attn/peers.json  →  "peers": [ … ]')
        print('Aggregator must run: tmux-attn web   OR   tmux-attn web --fleet')
    elif mode == 'B':
        peers_path.parent.mkdir(parents=True, exist_ok=True)
        data = {'peers': []}
        if peers_path.is_file():
            try:
                data = json.loads(peers_path.read_text())
            except json.JSONDecodeError as exc:
                die(f'Invalid JSON in {peers_path}: {exc}')
        peers = list(data.get('peers') or [])
        updated = False
        for row in peers:
            if isinstance(row, dict) and row.get('name') == name:
                row['url'] = url
                updated = True
                break
        if not updated:
            peers.append(peer)
        data['peers'] = peers
        peers_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
        print(f'Mode B — wrote {peers_path} ({"updated" if updated else "added"} {name!r}).')
        print('Ensure this machine runs: tmux-attn web   (or web --fleet if fleet-only)')
    else:
        print('Mode C — apply the object on the remote aggregator (user must authorize SSH).')
        if ssh:
            print(f'  Hint: ssh {ssh}  then edit ~/.config/tmux-attn/peers.json and restart web --fleet')
        else:
            print('  Set AGGREGATOR_SSH=user@host for a concrete ssh hint, or hand JSON to the user.')

    print()
    print('NEXT STEPS (agent):')
    print(f'  1. export TMUX_ATTN_HOST={name!r}')
    print('  2. Start and keep running:  ~/.local/bin/tmux-attn web')
    print('  3. curl -s http://127.0.0.1:7788/api/health')
    print(f'  4. Prefer also: curl -s {url}/api/health')
    print('  5. Reload hooks: Claude new session | Codex /hooks | Cursor reopen | Pi /reload')
    print('  6. ~/.local/bin/tmux-attn status')
    print('  7. Phone: http://<aggregator-tailscale>:7788/  — confirm host online')
    print('Rules: Tailscale/LAN only; no public :7788; worker serves /api/agents only; do not guess IPs.')


if __name__ == '__main__':
    main()
