#!/usr/bin/env python3
import json
import os
import signal
import time
import shlex
from pathlib import Path
import shutil
import subprocess
import sys
backup = Path(sys.argv[1])
# Stop only this integration's observer, never any agent process.
for lock in (Path.home()/'.local/state/tmux-attn').glob('codex-observer-*.lock'):
    try:
        pid = int(lock.read_text())
        args = subprocess.check_output(['ps','-p',str(pid),'-o','args='],text=True).strip()
        tokens = shlex.split(args)
        expected = str(Path.home()/'.local/share/tmux-attn/attention.py')
        if tokens[-2:] == [expected,'observe']:
            os.kill(pid,signal.SIGTERM)
            for _ in range(30):
                try: os.kill(pid,0)
                except ProcessLookupError: break
                time.sleep(.1)
    except (OSError,ValueError,subprocess.SubprocessError):
        pass

for entry in json.loads((backup/'manifest.json').read_text()):
    p = Path(entry['path'])
    if entry['backup']:
        shutil.copy2(entry['backup'], p)
    elif p.exists():
        p.unlink()
manifest = json.loads((backup/'manifest.json').read_text())
prior_app = next((e for e in manifest if e['path'].endswith('/share/tmux-attn/attention.py')), None)
for name in ['after-new-window','session-created','client-attached','pane-exited','after-kill-pane']:
    subprocess.run(['tmux','set-hook','-gu',name+'[90]'],check=False)
for key in ['a','N']:
    subprocess.run(['tmux','unbind-key',key],check=False)
    path = backup/f'key-{key}.conf'
    if path.read_text().strip(): subprocess.run(['tmux','source-file',str(path)],check=True)
for k,v in json.loads((backup/'runtime.json').read_text()).items():
    subprocess.run(['tmux','set-option','-gw' if k.startswith('window-status-') else '-g',k,v],check=True)
for line in subprocess.check_output(['tmux','list-panes','-a','-F','#{pane_id}\t#{@tmux_attn_sidebar}'],text=True).splitlines():
    if line.endswith('\t1'):
        subprocess.run(['tmux','kill-pane','-t',line.split('\t')[0]],check=True)
hook_file = backup/'runtime-hooks.json'
if hook_file.exists():
    for name,command in json.loads(hook_file.read_text()).items():
        if command:
            subprocess.run(['tmux','set-hook','-g',name+'[90]',command],check=True)
if prior_app and prior_app['backup']:
    config = Path.home()/'.config/tmux-attn/sidebar.conf'
    if config.exists() and ' ensure' in config.read_text():
        subprocess.run([sys.executable,prior_app['path'],'ensure'],check=True)
    if config.exists() and ' observe' in config.read_text():
        command = shlex.join(['env','TMUX_ATTN_STATE_DIR='+str(Path.home()/'.local/state/tmux-attn'),sys.executable,prior_app['path'],'observe'])
        subprocess.run(['tmux','run-shell','-b',command],check=True)
print('Original settings restored; only attention sidebar panes removed. Restart agents to reload original hooks.')
