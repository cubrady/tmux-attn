"""Exercise installer capability fallback against an isolated real tmux server."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent


@unittest.skipUnless(shutil.which('tmux'), 'requires tmux')
class InstallTmuxCompatibilityTests(unittest.TestCase):
    def test_custom_codex_home_install_and_rollback_leave_default_profile_untouched(self):
        import tomllib

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            home = base / 'home'
            profile = base / 'custom profile'
            home.mkdir()
            profile.mkdir()
            default = home / '.codex'
            default.mkdir()
            # Invalid TOML proves the default profile is never read for this installation.
            default_config = 'default profile must not be read\n'
            (default / 'config.toml').write_text(default_config)
            original_config = '[features]\nhooks = false\n\n[hooks.state."keep"]\ntrusted = true\n'
            original_hooks = json.dumps({'hooks': {'Stop': [{'hooks': [
                {'type': 'command', 'command': 'echo unrelated-profile-hook'},
            ]}]}})
            (profile / 'config.toml').write_text(original_config)
            (profile / 'hooks.json').write_text(original_hooks)
            bin_dir = base / 'bin'
            bin_dir.mkdir()
            command = [shutil.which('tmux'), '-S', str(base / 'tmux.sock')]
            shim = bin_dir / 'tmux'
            shim.write_text(f'#!{sys.executable}\nimport os, sys\nos.execv({command[0]!r}, {command!r} + sys.argv[1:])\n')
            shim.chmod(0o755)
            env = os.environ | {
                'HOME': str(home), 'CODEX_HOME': str(profile),
                'PATH': str(bin_dir) + os.pathsep + os.environ['PATH'],
            }
            for key in ('TMUX', 'TMUX_PANE', 'TMUX_ATTN_STATE_DIR'):
                env.pop(key, None)
            try:
                subprocess.run(command + ['-f', '/dev/null', 'new-session', '-d', '-s', 'work'], check=True)
                result = subprocess.run(
                    [sys.executable, str(ROOT / 'install.py'), '--enable-codex-hooks'],
                    env=env, text=True, capture_output=True, check=True,
                )
                installed = json.loads(result.stdout)
                self.assertEqual(installed['codex_home'], str(profile))
                config = tomllib.loads((profile / 'config.toml').read_text())
                self.assertTrue(config['features']['hooks'])
                self.assertEqual(config['hooks']['state'], {'keep': {'trusted': True}})
                hooks = json.loads((profile / 'hooks.json').read_text())['hooks']
                commands = [h['command'] for group in hooks['Stop'] for h in group['hooks']]
                self.assertIn('echo unrelated-profile-hook', commands)
                self.assertTrue(any('event codex Stop' in command for command in commands))
                self.assertIn('Stop', installed['codex_hooks_untrusted'])
                self.assertEqual((default / 'config.toml').read_text(), default_config)
                self.assertFalse((default / 'hooks.json').exists())
                manifest = json.loads((Path(installed['backup']) / 'manifest.json').read_text())
                backed_up = {entry['path'] for entry in manifest}
                self.assertIn(str(profile / 'config.toml'), backed_up)
                self.assertIn(str(profile / 'hooks.json'), backed_up)
                self.assertNotIn(str(default / 'config.toml'), backed_up)
                # Rollback follows its manifest even when the invoking profile has changed.
                subprocess.run(
                    [sys.executable, str(ROOT / 'rollback.py'), installed['backup']],
                    env=env | {'CODEX_HOME': str(base / 'another profile')},
                    text=True, capture_output=True, check=True,
                )
                self.assertEqual((profile / 'config.toml').read_text(), original_config)
                self.assertEqual((profile / 'hooks.json').read_text(), original_hooks)
                self.assertEqual((default / 'config.toml').read_text(), default_config)
                self.assertFalse((base / 'another profile').exists())
            finally:
                subprocess.run(command + ['kill-server'], capture_output=True)

    def test_missing_popup_title_support_rejected_before_mutation(self):
        self.check_popup_title_rejection(shutil.which('tmux'), simulate_old=True)

    @unittest.skipUnless(os.environ.get('TMUX_ATTN_TEST_OLD_TMUX'), 'optional upstream tmux 3.2a binary')
    def test_real_old_tmux_rejected_before_mutation(self):
        self.check_popup_title_rejection(os.environ['TMUX_ATTN_TEST_OLD_TMUX'])

    def check_popup_title_rejection(self, executable, simulate_old=False):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            home = base / 'home'
            home.mkdir()
            bin_dir = base / 'bin'
            bin_dir.mkdir()
            command = [executable, '-S', str(base / 'tmux.sock')]
            shim = bin_dir / 'tmux'
            shim.write_text(f'''#!{sys.executable}
import os
import subprocess
import sys
args = sys.argv[1:]
if {simulate_old!r} and args == ['list-commands']:
    output = subprocess.check_output({command!r} + args, text=True)
    print(output.replace(' [-T title]', ''), end='')
    sys.exit(0)
os.execv({executable!r}, {command!r} + args)
''')
            shim.chmod(0o755)
            original = {
                '.tmux.conf': '# original tmux settings\n',
                '.codex/config.toml': '[features]\nhooks = false\n',
                '.codex/hooks.json': '{}\n',
                '.claude/settings.json': '{}\n',
                '.cursor/hooks.json': '{"version":1}\n',
                '.config/tmux-attn/sidebar.conf': '# original sidebar\n',
            }
            for name, content in original.items():
                path = home / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            before_paths = sorted(str(path.relative_to(home)) for path in home.rglob('*'))
            env = os.environ | {'HOME': str(home), 'CODEX_HOME': str(home / '.codex'),
                                'PATH': str(bin_dir) + os.pathsep + os.environ['PATH']}
            for key in ('TMUX', 'TMUX_PANE', 'TMUX_ATTN_STATE_DIR'):
                env.pop(key, None)
            def runtime():
                return [subprocess.check_output(command + args, text=True) for args in
                        (['show-options', '-g'], ['show-options', '-gw'], ['show-hooks', '-g'], ['list-keys'])]
            try:
                subprocess.run(command + ['-f', '/dev/null', 'new-session', '-d', '-s', 'work'], check=True)
                before_runtime = runtime()
                for entry in ([sys.executable, str(ROOT / 'install.py'), '--enable-codex-hooks'],
                              [sys.executable, str(ROOT / 'install.py'), '--enable-codex-hooks', '--sidebar'],
                              ['sh', str(ROOT / 'bootstrap.sh')]):
                    with self.subTest(entry=entry, executable=executable):
                        result = subprocess.run(entry, env=env, text=True, capture_output=True)
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIn('display-popup -T title support', result.stderr)
                        self.assertEqual(runtime(), before_runtime)
                        self.assertEqual(sorted(str(path.relative_to(home)) for path in home.rglob('*')), before_paths)
                        for name, content in original.items():
                            self.assertEqual((home / name).read_text(), content)
            finally:
                subprocess.run(command + ['kill-server'], capture_output=True)

    def test_capabilities_override_client_version_and_rollback_restores_status(self):
        for version, clickable in [('3.6a', False), ('3.8', False), ('3.6a', True)]:
            with self.subTest(version=version, clickable=clickable), tempfile.TemporaryDirectory() as directory:
                home = Path(directory)
                bin_dir = home / 'bin'
                bin_dir.mkdir()
                command = [shutil.which('tmux'), '-S', str(home / 'tmux.sock')]
                shim = bin_dir / 'tmux'
                shim.write_text(f'''#!{sys.executable}
import os
from pathlib import Path
import sys
args = sys.argv[1:]
if args == ['-V']:
    print('tmux {version}')
    sys.exit(0)
if not {clickable!r}:
    if args[0] in ('bind-key', 'unbind-key') and any('Control' in arg for arg in args):
        sys.exit(1)
    if args[0] == 'source-file' and args[1] != '-':
        if 'Control' in Path(args[1]).read_text():
            sys.exit('unknown Control key in config')
os.execv({command[0]!r}, {command!r} + args)
''')
                shim.chmod(0o755)
                env = os.environ | {'HOME': directory, 'CODEX_HOME': str(home / '.codex'),
                                    'PATH': str(bin_dir) + os.pathsep + os.environ['PATH']}
                for key in ('TMUX', 'TMUX_PANE', 'TMUX_ATTN_STATE_DIR'):
                    env.pop(key, None)
                try:
                    subprocess.run(command + ['-f', '/dev/null', 'new-session', '-d', '-s', 'work'], check=True)
                    subprocess.run(command + ['set-option', '-g', 'status-right', 'original status'], check=True)
                    result = subprocess.run(
                        ['sh', str(ROOT / 'bootstrap.sh')], env=env, text=True, capture_output=True, check=True,
                    )
                    installed = json.loads(result.stdout)
                    self.assertEqual(installed['status_control_clicks'], clickable)
                    config = Path(installed['config']).read_text()
                    self.assertEqual('MouseDown1Control0' in config, clickable)
                    self.assertEqual('status-clickable)' in config, clickable)
                    if not clickable:
                        self.assertIn(' status)', config)
                        self.assertNotIn('Control', config)
                    self.assertIn('bind-key a display-popup', config)
                    self.assertIn('bind-key N ', config)
                    subprocess.run(
                        [sys.executable, str(ROOT / 'rollback.py'), installed['backup']],
                        env=env, check=True, capture_output=True, text=True,
                    )
                    restored = subprocess.check_output(command + ['show-options', '-gv', 'status-right'], text=True)
                    self.assertEqual(restored.strip(), 'original status')
                    self.assertFalse(Path(installed['config']).exists())
                finally:
                    subprocess.run(command + ['kill-server'], capture_output=True)
