#!/usr/bin/env python3
"""Export portable source and current project aliases, never agent credentials/state."""
import argparse
import io
import json
from pathlib import Path
import tarfile

ROOT = Path(__file__).resolve().parent
FILES = [
    'README.md', 'LICENSE', '.gitignore', 'bootstrap.sh', 'migrate.py', 'install.py',
    'rollback.py', 'attention.py', 'codex_observer.py', 'pi-attention.ts',
    'tmux-attn', 'tmux.conf.snippet', 'project-aliases.example.json',
    'web/index.html', 'peers.example.json', 'join_peer.py', 'install.sh',
    'install_web_service.sh', 'update.py', 'update.sh',
    'contrib/systemd/tmux-attn-web.service', 'contrib/launchd/ai.tmux-attn.web.plist',
    'web/icons/claude.svg', 'web/icons/codex.svg', 'web/icons/agent.svg', 'web/icons/pi.svg',
    'web/icons/favicon.svg',

    'test_attention.py', 'test_codex_observer.py', 'test_observer_recovery.py',
    'test_migration.py', 'tests/popup_roundtrip.py',
    'test_join_peer.py', 'tests/status_click_roundtrip.py', 'tests/web.test.cjs',
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['export'])
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    alias_path = Path.home()/'.config/tmux-attn/project-aliases.json'
    aliases = json.loads((alias_path if alias_path.exists() else ROOT/'project-aliases.example.json').read_text())
    if not isinstance(aliases, dict) or not isinstance(aliases.get('aliases'), dict):
        parser.error('Project aliases must contain an aliases object')
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in aliases['aliases'].items()):
        parser.error('Project alias keys and values must be strings')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation avoids accidentally overwriting a previous export.
    with args.output.open('xb') as output, tarfile.open(fileobj=output, mode='w:gz') as archive:
        for name in FILES:
            archive.add(ROOT/name, arcname='tmux-attn/'+name, recursive=False)
        payload = (json.dumps(aliases, ensure_ascii=False, indent=2)+'\n').encode()
        info = tarfile.TarInfo('tmux-attn/project-aliases.local.json')
        info.size = len(payload)
        info.mode = 0o600
        archive.addfile(info, io.BytesIO(payload))
    print(json.dumps({'bundle':str(args.output.resolve()),'aliases':len(aliases['aliases']),
                      'install':'cd tmux-attn && sh bootstrap.sh'},ensure_ascii=False,indent=2))


if __name__ == '__main__':
    main()
