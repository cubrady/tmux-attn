#!/usr/bin/env python3
"""Install this checkout's attention sidebar with timestamped, restorable backups."""
import datetime
import argparse
import json
import re
from pathlib import Path
import shutil
import shlex
import subprocess
import sys
from attention import VIEW_HOOKS, VIEW_HOOK_INDEX, view_hooks_config

parser=argparse.ArgumentParser()
parser.add_argument('--sidebar',action='store_true',help='Use the previous persistent sidebar instead of popup')
parser.add_argument('--enable-codex-hooks',action='store_true',help='Enable Codex hooks, backing up the original config first')
options=parser.parse_args()
HOME = Path.home()
ROOT = Path(__file__).resolve().parent
DEST = HOME / '.local/share/tmux-attn'
BACKUP = HOME / '.local/state/tmux-attn/backups' / datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f')
import tomllib
codex_path = HOME/'.codex/config.toml'
codex_text = codex_path.read_text() if codex_path.exists() else ''
codex = tomllib.loads(codex_text)
new_codex_text = None
if codex.get('features', {}).get('hooks') is not True:
    if not options.enable_codex_hooks:
        raise SystemExit('Enable [features] hooks = true in Codex config, or use --enable-codex-hooks.')
    section = re.search(r'(?m)^\[features\][ \t]*(?:#.*)?$', codex_text)
    if section:
        following = re.search(r'(?m)^\s*\[', codex_text[section.end():])
        end = section.end()+following.start() if following else len(codex_text)
        body = codex_text[section.end():end]
        if 'hooks' in codex.get('features', {}):
            body, count = re.subn(r'(?m)^([ \t]*hooks[ \t]*=[ \t]*)(?:false|true)([ \t]*(?:#.*)?)$', r'\g<1>true\2', body)
            if not count:
                raise SystemExit('Set [features] hooks = true manually; this hooks syntax cannot be edited safely.')
        else:
            body = '\nhooks = true\n'+body
        new_codex_text = codex_text[:section.end()]+body+codex_text[end:]
    else:
        new_codex_text = codex_text.rstrip()+'\n\n[features]\nhooks = true\n'
    try:
        assert tomllib.loads(new_codex_text)['features']['hooks'] is True
    except (tomllib.TOMLDecodeError, AssertionError):
        raise SystemExit('Set [features] hooks = true manually; existing inline/dotted configuration is preserved.')
alias_config = HOME/'.config/tmux-attn/project-aliases.json'
alias_seed = ROOT/'project-aliases.local.json'
if not alias_seed.exists():
    alias_seed = ROOT/'project-aliases.example.json'
alias_data = json.loads(alias_seed.read_text())
existing_alias_data = json.loads(alias_config.read_text()) if alias_config.exists() else {'aliases':{}}
for data in [alias_data, existing_alias_data]:
    if not isinstance(data, dict) or not isinstance(data.get('aliases'), dict) or not all(isinstance(k,str) and isinstance(v,str) for k,v in data['aliases'].items()):
        raise SystemExit('Project aliases must contain an aliases object with string keys/values.')
alias_data = existing_alias_data | {'aliases':alias_data['aliases'] | existing_alias_data['aliases']}
for name in ['.claude/settings.json', '.codex/hooks.json', '.cursor/hooks.json']:
    path = HOME/name
    if path.exists():
        data = json.loads(path.read_text())
        if not isinstance(data, dict) or not isinstance(data.get('hooks', {}), dict):
            raise SystemExit('Invalid hook configuration: '+str(path))
# Confirm server access and save runtime before any live-file mutation.
def tmux(*args):
    return subprocess.check_output(['tmux',*args],text=True).strip()
version = re.search(r'(\d+)\.(\d+)',tmux('-V'))
if not version or tuple(map(int,version.groups())) < (3,7):
    raise SystemExit('Clickable attention counts require tmux 3.7 or newer.')
runtime = {k: tmux('show-options','-gv',k) for k in ['status-interval','status-right-length','status-right','mouse']}
for key in ['window-status-format', 'window-status-current-format']:
    runtime[key] = tmux('show-options', '-gwv', key)
for name in ['after-new-window','session-created','client-attached','pane-exited','after-kill-pane']:
    if name+'[90]' in tmux('show-hooks','-g') and 'tmux-attn' not in tmux('show-hooks','-g',name+'[90]'):
        raise SystemExit('Hook index 90 already used: '+name)
for name in VIEW_HOOKS:
    key = f'{name}[{VIEW_HOOK_INDEX}]'
    if key in tmux('show-hooks', '-g') and 'tmux-attn' not in tmux('show-hooks', '-g', key):
        raise SystemExit('Hook index 91 already used: ' + name)

BACKUP.mkdir(parents=True)
DEST.mkdir(parents=True, exist_ok=True)
python = sys.executable
app = DEST / 'attention.py'
config = HOME / '.config/tmux-attn/sidebar.conf'
web_index = DEST / 'web' / 'index.html'
paths = [HOME/'.tmux.conf', HOME/'.claude/settings.json', HOME/'.codex/hooks.json', HOME/'.codex/config.toml', HOME/'.pi/agent/extensions/tmux-attention.ts', config, HOME/'.local/bin/tmux-attn', app, DEST/'codex_observer.py']
paths.append(alias_config)
paths.append(HOME/'.cursor/hooks.json')
paths.append(web_index)
paths.extend([
    DEST/'web/icons', DEST/'install_web_service.sh',
    DEST/'contrib/systemd/tmux-attn-web.service',
    DEST/'contrib/launchd/ai.tmux-attn.web.plist',
])
manifest = []
for i, p in enumerate(paths):
    copy = BACKUP / str(i)
    directory = p.is_dir() or p == DEST/'web/icons'
    if p.exists():
        if directory:
            shutil.copytree(p, copy)
        else:
            shutil.copy2(p, copy)
    manifest.append({'path': str(p), 'backup': str(copy) if p.exists() else None,
                     'directory': directory})
(BACKUP/'manifest.json').write_text(json.dumps(manifest, indent=2))
(BACKUP/'runtime.json').write_text(json.dumps(runtime,indent=2))
prior_hooks = {}
for name in ['after-new-window','session-created','client-attached','pane-exited','after-kill-pane']:
    result = subprocess.run(['tmux','show-hooks','-g',name+'[90]'],capture_output=True,text=True)
    line = result.stdout.strip()
    prior_hooks[name] = line.split(' ',1)[1] if ' ' in line else None
for name in VIEW_HOOKS:
    key = f'{name}[{VIEW_HOOK_INDEX}]'
    result = subprocess.run(['tmux','show-hooks','-g',key],capture_output=True,text=True)
    line = result.stdout.strip()
    prior_hooks[key] = line.split(' ',1)[1] if ' ' in line else None
(BACKUP/'runtime-hooks.json').write_text(json.dumps(prior_hooks,indent=2))
prefix_bindings = {}
for line in tmux('list-keys','-T','prefix').splitlines():
    parts = shlex.split(line)
    prefix_bindings[parts[parts.index('-T')+2]] = line+'\n'
for key in ['a','N']:
    (BACKUP/f'key-{key}.conf').write_text(prefix_bindings.get(key,''))
root_bindings = {}
for line in tmux('list-keys','-T','root').splitlines():
    parts = shlex.split(line)
    root_bindings[parts[parts.index('-T')+2]] = line+'\n'
for index in range(4):
    for event in ['MouseDown1','SecondClick1','TripleClick1','DoubleClick1']:
        key = f'{event}Control{index}'
        # Mouse keys are not reliably returned by list-keys' positional filter.
        (BACKUP/f'key-root-{key}.conf').write_text(root_bindings.get(key,''))
shutil.copy2(ROOT/'attention.py', app)
if new_codex_text is not None:
    codex_path.parent.mkdir(parents=True, exist_ok=True)
    codex_path.write_text(new_codex_text)
shutil.copy2(ROOT/'codex_observer.py', DEST/'codex_observer.py')
web_index.parent.mkdir(parents=True, exist_ok=True)
shutil.copy2(ROOT/'web'/'index.html', web_index)
icons_src = ROOT/'web'/'icons'
icons_dest = DEST/'web'/'icons'
if icons_src.is_dir():
    if icons_dest.exists():
        shutil.rmtree(icons_dest)
    shutil.copytree(icons_src, icons_dest)
# Persistent web helper lives next to the installed app so NEXT STEPS work without a checkout.
web_service = DEST / 'install_web_service.sh'
shutil.copy2(ROOT / 'install_web_service.sh', web_service)
web_service.chmod(0o755)
for unit in ['contrib/systemd/tmux-attn-web.service', 'contrib/launchd/ai.tmux-attn.web.plist']:
    src = ROOT / unit
    if src.is_file():
        dest = DEST / unit
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
paths[6].parent.mkdir(parents=True, exist_ok=True)
paths[6].write_text('#!/bin/sh\nif [ "$#" -eq 0 ]; then set -- popup; fi\nexec '+shlex.join([python,str(app)])+' "$@"\n')
paths[6].chmod(0o755)
pi = (ROOT/'pi-attention.ts').read_text().replace('__PYTHON__', python).replace('__ATTENTION__', str(app))
paths[4].parent.mkdir(parents=True, exist_ok=True)
paths[4].write_text(pi)

for agent, path in [('claude', paths[1]), ('codex', paths[2])]:
    data = json.loads(path.read_text()) if path.exists() else {}
    hooks = data.setdefault('hooks', {})
    # Replace only the previous attention handlers. Preserve unrelated user hooks.
    for event, groups in list(hooks.items()):
        for group in groups:
            group['hooks'] = [h for h in group.get('hooks', []) if 'tmux-agent-status/hooks/' not in h.get('command','') and str(app) not in h.get('command','')]
        hooks[event] = [g for g in groups if g.get('hooks')]
    events = ['SessionStart', 'UserPromptSubmit', 'PreToolUse', 'PostToolUse', 'PermissionRequest', 'Stop', 'SessionEnd']
    events += ['Notification', 'StopFailure'] if agent == 'claude' else ['Interrupt']
    for event in events:
        group = {'hooks': [{'type':'command','command':shlex.join([python, str(app), 'event', agent, event])}]}
        if event == 'Notification':
            group['matcher'] = 'permission_prompt|elicitation_dialog|agent_needs_input|worker_permission_prompt'
        hooks.setdefault(event, []).append(group)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2)+'\n')

# Native Cursor stop is required: its CLI does not invoke inherited Claude Stop
# unless a native stop handler exists. Use observational hooks only.
cursor_path = paths[10]
cursor = json.loads(cursor_path.read_text()) if cursor_path.exists() else {'version':1}
cursor_hooks = cursor.setdefault('hooks', {})
for event, handlers in cursor_hooks.items():
    cursor_hooks[event] = [h for h in handlers if str(app) not in h.get('command','')]
for event in ['sessionStart', 'beforeSubmitPrompt', 'postToolUse', 'stop', 'sessionEnd']:
    cursor_hooks.setdefault(event, []).append({'command':shlex.join([python,str(app),'event','cursor',event])})
cursor_path.parent.mkdir(parents=True, exist_ok=True)
cursor_path.write_text(json.dumps(cursor,indent=2)+'\n')

launch = shlex.join(['env', 'TMUX_ATTN_STATE_DIR='+str(HOME/'.local/state/tmux-attn'), python,str(app)])
config.parent.mkdir(parents=True, exist_ok=True)
alias_config.write_text(json.dumps(alias_data, ensure_ascii=False, indent=2)+'\n')


def supports_status_control_clicks():
    """Some tmux builds report ≥3.7 but reject MouseDown1Control* (source-file would fail)."""
    key = 'MouseDown1Control0'
    prior = root_bindings.get(key, '')
    probe = subprocess.run(
        ['tmux', 'bind-key', '-T', 'root', key, 'display-message', 'tmux-attn-probe'],
        capture_output=True, text=True,
    )
    subprocess.run(['tmux', 'unbind-key', '-T', 'root', key], capture_output=True)
    if prior.strip():
        subprocess.run(['tmux', 'source-file', '-'], input=prior, text=True, capture_output=True)
    return probe.returncode == 0


control_clicks = supports_status_control_clicks()
# Clear prior Control binds out-of-band: unknown keys must not appear in sidebar.conf
# or `tmux source-file` aborts on hosts that reject MouseDown1Control*.
for index in range(4):
    for event in ['MouseDown1', 'SecondClick1', 'TripleClick1', 'DoubleClick1']:
        subprocess.run(
            ['tmux', 'unbind-key', '-T', 'root', f'{event}Control{index}'],
            capture_output=True,
        )
base = f'''# Managed tmux attention inbox.
set -g mouse on
set -g status-interval 2
set -g status-right-length 110
set -g status-right '#({launch} status-clickable) | %H:%M'
bind-key N run-shell -b "{launch} next '#{{client_name}}'"
run-shell -b '{launch} observe'
'''
base += '# Begin automatic viewing hooks\n' + view_hooks_config(launch) + '# End automatic viewing hooks\n'
if control_clicks:
    for index, category in enumerate(['wait', 'review', 'running', 'idle']):
        for event in ['MouseDown1', 'SecondClick1', 'TripleClick1']:
            base += (
                f'bind-key -T root {event}Control{index} '
                f'run-shell -b "{launch} cycle {category} \'#{{client_name}}\'"\n'
            )
for key in ['window-status-format', 'window-status-current-format']:
    value = runtime[key]
    if '@tmux_attn_project' not in value:
        value = value.replace('#W', '#{?@tmux_attn_project,#{@tmux_attn_project},#W}').replace('#{window_name}', '#{?@tmux_attn_project,#{@tmux_attn_project},#{window_name}}')
    if '@tmux_attn_badge' not in value:
        value += '#{?@tmux_attn_badge, #{@tmux_attn_badge},}'
    base += 'set -gw '+key+' '+shlex.quote(value)+'\n'
if options.sidebar:
    config.write_text(base + f'''bind-key a run-shell -b "{launch} focus '#{{window_id}}'"
set-hook -g 'after-new-window[90]' 'run-shell -b "{launch} ensure \\"#{{window_id}}\\""'
set-hook -g 'session-created[90]' 'run-shell -b "{launch} ensure \\"#{{window_id}}\\""'
set-hook -g 'client-attached[90]' 'run-shell -b "{launch} ensure"'
set-hook -g 'pane-exited[90]' 'run-shell -b "{launch} prune"'
set-hook -g 'after-kill-pane[90]' 'run-shell -b "{launch} prune"'
run-shell -b '{launch} ensure'
''')
else:
    cleanup = ''.join("set-hook -gu '"+name+"[90]'\n" for name in ['after-new-window','session-created','client-attached','pane-exited','after-kill-pane'])
    config.write_text(base + cleanup + f'''bind-key a display-popup -E -w 90% -h 85% -T '待處理清單' '{launch} inbox "#{{client_name}}"'
''')
tmux_config = paths[0].read_text() if paths[0].exists() else ''
source = f'source-file {shlex.quote(str(config))}'
if source not in tmux_config:
    paths[0].write_text(tmux_config.rstrip()+'\n\n# Global agent attention inbox\n'+source+'\n')

subprocess.run(['tmux','source-file',str(config)],check=True)
if not options.sidebar:
    for line in tmux('list-panes','-a','-F','#{pane_id}\t#{@tmux_attn_sidebar}').splitlines():
        if line.endswith('\t1'):
            subprocess.run(['tmux','kill-pane','-t',line.split('\t')[0]],check=True)
print(json.dumps({
    'installed': str(app),
    'backup': str(BACKUP),
    'config': str(config),
    'status_control_clicks': control_clicks,
    'web_service': str(web_service),
}, indent=2))
