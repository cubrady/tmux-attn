import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import join_peer


class JoinPeerTests(unittest.TestCase):
    def test_api_enrollment_authenticates_hub_and_sends_private_local_key_without_printing_it(self):
        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / 'auth.key'
            output = io.StringIO()
            response = io.BytesIO(b'{"ok":true,"action":"added"}')
            hub_key = 'h' * 43
            with patch.dict(os.environ, {'SERVER_TOKEN': hub_key, 'TMUX_ATTN_AUTH_KEY_FILE': str(key)}), \
                    patch.object(join_peer, 'open_peer', return_value=response) as opened, \
                    contextlib.redirect_stdout(output):
                join_peer.register_via_api('http://100.64.0.1:7788', 'work', 'http://100.64.0.2:7788')
            request, credential = opened.call_args.args
            self.assertEqual(credential, hub_key)
            payload = json.loads(request.data)
            local_key = key.read_text().strip()
            self.assertEqual(payload, {'name': 'work', 'url': 'http://100.64.0.2:7788', 'token': local_key})
            self.assertEqual(key.stat().st_mode & 0o777, 0o600)
            for secret in (hub_key, local_key):
                self.assertNotIn(secret, output.getvalue())
                self.assertNotIn(secret, request.full_url)

    def test_missing_hub_credential_fails_before_creating_local_key_or_sending_request(self):
        with patch.dict(os.environ, {'SERVER_TOKEN': ''}), \
                patch.object(join_peer, 'open_peer') as opened, \
                patch.object(join_peer, 'load_owner_token') as load, \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                join_peer.register_via_api('http://100.64.0.1:7788', 'work', 'http://100.64.0.2:7788')
        opened.assert_not_called()
        load.assert_not_called()

    def test_ssh_registration_quotes_remote_code_and_values(self):
        real_run = subprocess.run
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            path = home / '.config/tmux-attn/peers.json'
            path.parent.mkdir(parents=True)
            original = {
                'peers': [{'name': 'existing', 'url': 'http://existing:7788'}],
                'custom': {'keep': True},
            }
            path.write_text(json.dumps(original))
            name = "desk's $(printf unsafe); 工作"
            url = 'http://100.1.2.3:7788/feed?view=a&title=two%20words#section'

            def ssh_remote_shell(command, **kwargs):
                self.assertEqual(command[:2], ['ssh', 'server'])
                # SSH joins its remote command arguments and passes them to a shell.
                return real_run(
                    ['/bin/sh', '-c', ' '.join(command[2:])],
                    env=os.environ | {'HOME': directory}, **kwargs,
                )

            with patch.object(join_peer.subprocess, 'run', side_effect=ssh_remote_shell), contextlib.redirect_stdout(io.StringIO()):
                join_peer.apply_remote_ssh('server', name, url)
                data = json.loads(path.read_text())
                self.assertEqual(data, original | {'peers': original['peers'] + [{'name': name, 'url': url}]})
                updated_url = url + '&updated=1'
                join_peer.apply_remote_ssh('server', name, updated_url)

            data = json.loads(path.read_text())
            self.assertEqual(data['custom'], original['custom'])
            self.assertEqual(data['peers'], original['peers'] + [{'name': name, 'url': updated_url}])

    def test_ssh_failure_reports_diagnostic_and_exits(self):
        failure = subprocess.CompletedProcess(['ssh'], 255, '', 'Connection refused\n')
        output = io.StringIO()
        with patch.object(join_peer.subprocess, 'run', return_value=failure), contextlib.redirect_stderr(output):
            with self.assertRaises(SystemExit) as raised:
                join_peer.apply_remote_ssh('server', 'desk', 'http://desk:7788')
        self.assertEqual(raised.exception.code, 1)
        self.assertIn('Connection refused', output.getvalue())
        self.assertIn('ssh server failed', output.getvalue())


if __name__ == '__main__':
    unittest.main()
