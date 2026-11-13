import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent


class WebServiceInstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / 'home & work'
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.log = self.root / 'commands.jsonl'
        commands = ('uname', 'id', 'launchctl', 'systemctl', 'loginctl', 'tmux', 'lsof', 'curl', 'sleep')
        stub = '''import json, os, sys
from pathlib import Path
name = Path(sys.argv[0]).name
with open(os.environ['TEST_COMMAND_LOG'], 'a') as stream:
    stream.write(json.dumps([name, *sys.argv[1:]]) + '\\n')
if name == 'uname': print(os.environ['TEST_PLATFORM'])
elif name == 'id': print('502' if '-u' in sys.argv else 'fixture-user')
elif name == 'lsof': sys.exit(1)
elif name == 'curl': print('{"ok":true}')
'''
        for name in commands:
            target = self.bin / name
            target.write_text(f'#!{sys.executable}\n' + stub)
            target.chmod(0o755)
        executable = self.home / '.local/bin/tmux-attn'
        executable.parent.mkdir(parents=True)
        executable.write_text('#!/bin/sh\nexit 0\n')
        executable.chmod(0o755)

    def run_install(self, platform, *args, script=None, **overrides):
        env = {key: value for key, value in os.environ.items()
               if not key.startswith('TMUX_ATTN_')}
        env.update(HOME=str(self.home), PATH=str(self.bin) + os.pathsep + env['PATH'],
                   TEST_COMMAND_LOG=str(self.log), TEST_PLATFORM=platform, **overrides)
        return subprocess.run(['sh', str(script or ROOT / 'install_web_service.sh'), *args], env=env,
                              cwd=ROOT, capture_output=True, text=True, timeout=10)

    def commands(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def plist(self):
        return self.home / 'Library/LaunchAgents/ai.tmux-attn.web.plist'

    def unit(self):
        return self.home / '.config/systemd/user/tmux-attn-web.service'

    def test_launchd_defaults_to_loopback_and_preserves_xml_sensitive_home(self):
        result = self.run_install('Darwin')
        self.assertEqual(result.returncode, 0, result.stderr)
        config = plistlib.loads(self.plist().read_bytes())
        self.assertEqual(config['ProgramArguments'][-2:], ['127.0.0.1', '7788'])
        self.assertEqual(config['WorkingDirectory'], str(self.home))
        self.assertEqual(config['EnvironmentVariables']['HOME'], str(self.home))
        self.assertEqual(self.plist().stat().st_mode & 0o777, 0o600)
        self.assertIn('auth-token', result.stdout)
        self.assertIn(['curl', '-s', '-m', '3', 'http://127.0.0.1:7788/api/health'], self.commands())

    def test_systemd_defaults_to_loopback(self):
        result = self.run_install('Linux')
        self.assertEqual(result.returncode, 0, result.stderr)
        text = self.unit().read_text()
        self.assertIn('ExecStart=%h/.local/bin/tmux-attn web 127.0.0.1 7788', text)
        self.assertNotIn('__', text)
        self.assertEqual(self.unit().stat().st_mode & 0o777, 0o600)

    def test_installed_launchd_helper_does_not_copy_wrapper_onto_itself(self):
        runtime = self.home / '.local/share/tmux-attn'
        shutil.copytree(ROOT / 'contrib', runtime / 'contrib')
        shutil.copy2(ROOT / 'install_web_service.sh', runtime / 'install_web_service.sh')
        shutil.copy2(ROOT / 'web_security.py', runtime / 'web_security.py')
        result = self.run_install('Darwin', script=runtime / 'install_web_service.sh')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(plistlib.loads(self.plist().read_bytes())['ProgramArguments'][-2:],
                         ['127.0.0.1', '7788'])

    def test_launchd_persists_explicit_bind_origins_and_key_path(self):
        key = str(self.home / 'key "%n" & token.key')
        origins = 'https://work.tailnet.ts.net,http://100.64.0.10:7799'
        result = self.run_install('Darwin', TMUX_ATTN_WEB_HOST='100.64.0.10', TMUX_ATTN_WEB_PORT='7799',
                                  TMUX_ATTN_ALLOWED_ORIGINS=origins, TMUX_ATTN_AUTH_KEY_FILE=key, TMUX_ATTN_AGY_AUTH_SCOPE='file')
        self.assertEqual(result.returncode, 0, result.stderr)
        config = plistlib.loads(self.plist().read_bytes())
        self.assertEqual(config['ProgramArguments'][-2:], ['100.64.0.10', '7799'])
        self.assertEqual(config['EnvironmentVariables']['TMUX_ATTN_ALLOWED_ORIGINS'], origins)
        self.assertEqual(config['EnvironmentVariables']['TMUX_ATTN_AUTH_KEY_FILE'], key)
        self.assertEqual(config['EnvironmentVariables']['TMUX_ATTN_AGY_AUTH_SCOPE'], 'file')
        self.assertIn(['curl', '-s', '-m', '3', 'http://100.64.0.10:7799/api/health'], self.commands())

    def test_systemd_escapes_environment_without_injecting_directives(self):
        key = '/tmp/auth "%n"\\key\nInjectedDirective=yes'
        result = self.run_install('Linux', TMUX_ATTN_WEB_HOST='100.64.0.10',
                                  TMUX_ATTN_ALLOWED_ORIGINS='https://work.tailnet.ts.net',
                                  TMUX_ATTN_AUTH_KEY_FILE=key)
        self.assertEqual(result.returncode, 0, result.stderr)
        text = self.unit().read_text()
        self.assertIn('Environment="TMUX_ATTN_ALLOWED_ORIGINS=https://work.tailnet.ts.net"', text)
        self.assertIn(r'Environment="TMUX_ATTN_AUTH_KEY_FILE=/tmp/auth \"%%n\"\\key\nInjectedDirective=yes"', text)
        self.assertNotIn('\nInjectedDirective=', text)

    def test_status_reads_installed_address_without_original_environment(self):
        for platform in ('Darwin', 'Linux'):
            with self.subTest(platform=platform):
                result = self.run_install(platform, TMUX_ATTN_WEB_HOST='100.64.0.10', TMUX_ATTN_WEB_PORT='7799')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.log.write_text('')
                result = self.run_install(platform, '--status')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(['curl', '-s', '-m', '3', 'http://100.64.0.10:7799/api/health'], self.commands())

    def test_ipv6_status_uses_bracketed_address(self):
        result = self.run_install('Linux', TMUX_ATTN_WEB_HOST='::1', TMUX_ATTN_WEB_PORT='7799')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(['curl', '-s', '-m', '3', 'http://[::1]:7799/api/health'], self.commands())

    def test_wildcard_requires_explicit_allowed_origin_before_stopping_services(self):
        for platform, host in (('Darwin', '0.0.0.0'), ('Linux', '::')):
            with self.subTest(platform=platform):
                result = self.run_install(platform, TMUX_ATTN_WEB_HOST=host)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('wildcard bind requires', result.stderr)
        self.assertFalse(any(command[0] in ('tmux', 'launchctl', 'systemctl', 'lsof') for command in self.commands()))

    def test_explicit_wildcard_uses_loopback_for_health(self):
        result = self.run_install('Linux', TMUX_ATTN_WEB_HOST='0.0.0.0',
                                  TMUX_ATTN_ALLOWED_ORIGINS='https://work.tailnet.ts.net')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('web 0.0.0.0 7788', self.unit().read_text())
        self.assertIn(['curl', '-s', '-m', '3', 'http://127.0.0.1:7788/api/health'], self.commands())

    def test_invalid_configuration_leaves_existing_service_untouched(self):
        self.unit().parent.mkdir(parents=True)
        self.unit().write_text('existing service\n')
        for settings in ({'TMUX_ATTN_WEB_PORT': '0'}, {'TMUX_ATTN_WEB_HOST': 'bad\nExecStart=bad'},
                         {'TMUX_ATTN_ALLOWED_ORIGINS': 'https://*.ts.net'},
                         {'TMUX_ATTN_ALLOWED_ORIGINS': 'http://user:password@localhost'},
                         {'TMUX_ATTN_ALLOWED_ORIGINS': 'http://@localhost'},
                         {'TMUX_ATTN_ALLOWED_ORIGINS': 'https://work.ts.net:0'},
                         {'TMUX_ATTN_ALLOWED_ORIGINS': 'https://work.ts.net\\evil'},
                         {'TMUX_ATTN_AUTH_KEY_FILE': 'relative/auth.key'}):
            with self.subTest(settings=settings):
                result = self.run_install('Linux', **settings)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.unit().read_text(), 'existing service\n')
        self.assertFalse(any(command[0] in ('tmux', 'systemctl', 'lsof') for command in self.commands()))

    def test_launchd_strict_preflight_preserves_existing_plist_and_running_service(self):
        self.plist().parent.mkdir(parents=True)
        original = {'Label': 'existing', 'ProgramArguments': ['old-service']}
        self.plist().write_bytes(plistlib.dumps(original))
        for settings in ({'TMUX_ATTN_ALLOWED_ORIGINS': 'http://@localhost'},
                         {'TMUX_ATTN_ALLOWED_ORIGINS': 'https://work.ts.net:0'},
                         {'TMUX_ATTN_ALLOWED_ORIGINS': 'https://work.ts.net\\evil'},
                         {'TMUX_ATTN_AUTH_KEY_FILE': 'relative/auth.key'}):
            with self.subTest(settings=settings):
                result = self.run_install('Darwin', **settings)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(plistlib.loads(self.plist().read_bytes()), original)
        self.assertFalse(any(command[0] in ('tmux', 'launchctl', 'lsof') for command in self.commands()))

    def test_tilde_key_path_matches_web_security_expansion(self):
        result = self.run_install('Linux', TMUX_ATTN_AUTH_KEY_FILE='~/.config/tmux-attn/auth.key')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Environment="TMUX_ATTN_AUTH_KEY_FILE=~/.config/tmux-attn/auth.key"',
                      self.unit().read_text())
