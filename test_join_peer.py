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
