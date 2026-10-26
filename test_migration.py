import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import unittest

ROOT = Path(__file__).resolve().parent


class MigrationTests(unittest.TestCase):
    def test_export_fresh_install_merge_and_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source_home = base/'source'
            aliases = source_home/'.config/tmux-attn/project-aliases.json'
            aliases.parent.mkdir(parents=True)
            aliases.write_text(json.dumps({'aliases':{'custom-project':'custom','web-app-sampleapp':'myday'}}))
            private = source_home/'.claude/settings.json'
            private.parent.mkdir()
            private.write_text('{"private":"secret-marker"}')
            bundle = base/'migration.tar.gz'
            subprocess.run([sys.executable,str(ROOT/'migrate.py'),'export',str(bundle)],env=os.environ|{'HOME':str(source_home)},check=True,capture_output=True)
            extracted = base/'extract'
            with tarfile.open(bundle) as archive:
                names = archive.getnames()
                self.assertNotIn('tmux-attn/tasks/state-accuracy-audit.json',names)
                self.assertIn('tmux-attn/LICENSE', names)
                self.assertIn('tmux-attn/test_join_peer.py', names)
                self.assertIn('tmux-attn/test_observer_recovery.py', names)
                self.assertIn('tmux-attn/tests/web.test.cjs', names)
                self.assertFalse(any('.claude' in name or 'sqlite' in name for name in names))
                for member in archive.getmembers():
                    target = extracted/member.name
                    target.parent.mkdir(parents=True,exist_ok=True)
                    target.write_bytes(archive.extractfile(member).read())
            exported = json.loads((extracted/'tmux-attn/project-aliases.local.json').read_text())
            self.assertEqual(exported['aliases']['custom-project'],'custom')
            destination = base/'destination'
            destination.mkdir()
            bin_dir = base/'bin'
            bin_dir.mkdir()
            label = 'attn-migration-'+str(os.getpid())
            real_tmux = shutil.which('tmux')
            shim = bin_dir/'tmux'
            shim.write_text('#!/bin/sh\nexec '+shlex.join([real_tmux,'-L',label])+' "$@"\n')
            shim.chmod(0o755)
            env = os.environ|{'HOME':str(destination),'CODEX_HOME':str(destination/'.codex'),
                              'PATH':str(bin_dir)+os.pathsep+os.environ['PATH']}
            env.pop('TMUX',None)
            env.pop('TMUX_PANE',None)
            env.pop('TMUX_ATTN_STATE_DIR',None)
            tmux = [real_tmux,'-L',label]
            checkout = extracted/'tmux-attn'
            try:
                # No tmux server, no Codex config: bootstrap creates both safely.
                result = subprocess.check_output(['sh',str(checkout/'bootstrap.sh')],env=env,text=True)
                backup = json.loads(result)['backup']
                config = destination/'.codex/config.toml'
                self.assertTrue(tomllib.loads(config.read_text())['features']['hooks'])
                self.assertEqual(subprocess.check_output(tmux+['list-sessions','-F','#{session_name}'],text=True).strip(),'work')
                target_aliases = destination/'.config/tmux-attn/project-aliases.json'
                self.assertEqual(json.loads(target_aliases.read_text()),exported)
                app = destination/'.local/share/tmux-attn/attention.py'
                generated = (destination/'.claude/settings.json').read_text()
                self.assertIn(str(app),generated)
                self.assertNotIn(str(source_home),generated)
                # Destination aliases win; source-only aliases are merged on reinstall.
                target_aliases.write_text(json.dumps({'aliases':{'web-app-sampleapp':'localday'}}))
                config_text='model = "local-model"\n[features] # keep comment\nhooks = false # retain\nother = true\n[other]\nvalue = 1\n'
                config.write_text(config_text)
                second=json.loads(subprocess.check_output(['sh',str(checkout/'bootstrap.sh')],env=env,text=True))
                self.assertIn('hooks = true # retain',config.read_text())
                self.assertTrue(tomllib.loads(config.read_text())['features']['other'])
                self.assertEqual(json.loads(target_aliases.read_text())['aliases'],{'web-app-sampleapp':'localday','custom-project':'custom'})
                subprocess.run([sys.executable,str(checkout/'rollback.py'),second['backup']],env=env,check=True,capture_output=True)
                self.assertEqual(config.read_text(),config_text)
                subprocess.run([sys.executable,str(checkout/'rollback.py'),backup],env=env,check=True,capture_output=True)
                self.assertFalse(config.exists())
                self.assertFalse(target_aliases.exists())
            finally:
                subprocess.run(tmux+['kill-server'],check=False,capture_output=True)


if __name__ == '__main__':
    unittest.main(verbosity=2)
