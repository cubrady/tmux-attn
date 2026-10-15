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
                env = os.environ | {'HOME': directory, 'PATH': str(bin_dir) + os.pathsep + os.environ['PATH']}
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
