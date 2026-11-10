import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import attention as a


class WorkHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve()
        self.project = self.home / 'project'
        self.project.mkdir()
        self.discovery = {'agents': [{'id': 'codex', 'label': 'Codex', 'available': True,
                                      'executable': '/fake/bin/codex'},
                                     {'id': 'claude', 'label': 'Claude', 'available': True,
                                      'executable': '/fake/bin/claude'}], 'path': '/fake/bin:/usr/bin'}
        for mock in (patch.object(a, 'user_home', return_value=self.home),
                     patch.object(a, 'STATE', self.home / 'state'),
                     patch.object(a, 'local_host_name', return_value='local'),
                     patch.object(a, 'work_discovery', return_value=self.discovery),
                     patch.object(a, 'work_sessions', return_value=[{'id': '$1', 'name': 'main', 'attached': 1, 'windows': 1}])):
            mock.start()
            self.addCleanup(mock.stop)

    def write(self, name, text):
        (self.home / name).write_text(text)

    def payload(self, **overrides):
        return {'path': str(self.project), 'agent': 'codex', 'request_id': 'request-history-123',
                'session': '$1', **overrides}

    def test_alias_profiles_and_arguments_are_preserved_without_shell_execution(self):
        self.write('.zshrc', "alias codex-full='codex --sandbox danger-full-access'\n"
                   "alias codex-kayla='CODEX_HOME=$HOME/.codex-kayla codex --sandbox danger-full-access'\n"
                   "alias cx='codex-full --model gpt-5'\n")
        self.write('.zsh_history', ': 100:0;codex-full\n: 200:0;codex-kayla\n: 300:0;cx\n: 400:0;codex-full\n')
        history = a.work_launch_history()
        self.assertEqual([row['label'] for row in history], ['codex-full', 'cx', 'codex-kayla'])
        self.assertEqual(history[0]['count'], 2)
        kayla = next(row for row in history if row['label'] == 'codex-kayla')
        self.assertEqual(kayla['env'], {'CODEX_HOME': str(self.home / '.codex-kayla')})
        self.assertNotEqual(kayla['id'], history[0]['id'])
        self.assertEqual(kayla['argv'], ['--sandbox', 'danger-full-access'])
        self.assertNotIn('env', a.public_work_preset(kayla))
        self.assertNotIn('argv', a.public_work_preset(kayla))

    def test_self_alias_expands_once_and_cyclic_aliases_are_rejected(self):
        result = a.parse_work_launch('codex --model local', {'codex': 'codex --sandbox workspace-write'})
        self.assertEqual(result['argv'], ['--sandbox', 'workspace-write', '--model', 'local'])
        self.assertIsNone(a.parse_work_launch('a', {'a': 'b', 'b': 'a'}))
        self.assertIsNone(a.parse_work_launch('codex', {'codex': 'cx', 'cx': 'codex'}))

    def test_cd_context_is_only_inferred_from_same_entry(self):
        self.write('.zsh_history', f': 100:0;cd {self.project}\n: 110:0;cd\n: 120:0;codex\n'
                   f': 130:0;cd {self.project} && claude\n')
        history = a.work_launch_history()
        self.assertNotIn('path', next(row for row in history if row['agent'] == 'codex'))
        self.assertEqual(next(row for row in history if row['agent'] == 'claude')['path'], str(self.project))

    def test_multiline_scripts_and_heredocs_do_not_become_launch_candidates(self):
        self.write('.bash_history', "cat <<'END'\ncodex --model secret\nEND\n"
                   "function foo() {\nclaude --model hidden\n}\n"
                   "echo \\\nagy\ncodex --sandbox workspace-write\n")
        history = a.work_launch_history()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]['label'], 'codex --sandbox workspace-write')
        self.write('.bash_history', '')
        self.write('.zsh_history', ': 100:0;cat <<END\ncodex --model hidden\nEND\n: 200:0;claude\n')
        history = a.work_launch_history()
        self.assertEqual([row['agent'] for row in history], ['claude'])

    def test_rejects_shell_syntax_secrets_maintenance_resume_and_unknown_flags(self):
        bad = ['codex; touch /tmp/secret', 'codex && echo pwned', 'codex | tee x',
               'codex > /tmp/out', 'codex --model $(touch /tmp/pwned)',
               'codex --model `touch`', 'codex resume', 'codex resume thread-id',
               'codex --help', 'codex --version', 'claude update', 'claude --resume',
               'claude --continue', 'agent --resume', 'codex hello',
               'codex --api-key SECRET', 'API_KEY=SECRET codex',
               'CODEX_HOME=relative codex', 'CODEX_HOME=/tmp/profile claude',
               'CODEX_HOME=$OTHER/profile codex', 'CODEX_HOME=$(touch /tmp/profile) codex',
               'codex --sandbox', 'codex --sandbox read-wirte', 'codex --unknown',
               'codex -c mcp_servers.secret.command=bad', 'pi -p secret-prompt',
               'claude --model=api_key=secret', 'codex\ncodex']
        for command in bad:
            with self.subTest(command=command):
                self.assertIsNone(a.parse_work_launch(command))
        self.assertIsNone(a.parse_work_launch('a', {'a': 'b', 'b': 'a'}))
        self.assertIsNone(a.parse_work_launch('a', {'a': 'codex --model good;touch /tmp/out'}))
        self.assertIsNone(a.parse_work_launch('a', {'a': 'PATH=/evil codex'}))

    def test_explicit_cd_history_recovers_project_context_and_counts(self):
        self.write('.zsh_history', f': 100:0;cd {self.project}\n: 200:0;codex --model local/model\n'
                   f': 300:0;cd {self.project} && claude --model sonnet\n')
        history = a.work_launch_history()
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]['agent'], 'claude')
        self.assertEqual(history[0]['path'], str(self.project))
        with patch.object(a, 'db', return_value=io.StringIO()), \
             patch.object(a, 'agents_payload', return_value={'agents': []}):
            options = a.work_options()[1]
        self.assertEqual(options['projects'][0]['path'], str(self.project))
        self.assertEqual(options['projects'][0]['agent'], 'claude')
        self.assertEqual(options['agents'][0]['id'], 'claude')
        self.assertEqual(options['projects'][0]['launch_preset'], history[0]['id'])
        self.assertTrue(options['launch_presets'])
        self.assertNotIn('env', options['launch_presets'][0])

    def test_start_resolves_opaque_preset_and_remembers_project_selection(self):
        self.write('.zshrc', "alias codex-kayla='CODEX_HOME=$HOME/.codex-kayla codex --sandbox danger-full-access'\n")
        self.write('.zsh_history', ': 100:0;codex-kayla\n')
        preset = a.work_launch_history()[0]
        with patch.object(a, 'tmux', side_effect=['', '@7\t%42\t987']) as tmux:
            code, result = a.dispatch_work_start(self.payload(launch_preset=preset['id']))
            self.assertEqual(code, 200, result)
            args = tmux.call_args.args
            self.assertEqual(args[-5:], ('PATH=/fake/bin:/usr/bin', 'CODEX_HOME=' + str(self.home / '.codex-kayla'),
                                         '/fake/bin/codex', '--sandbox', 'danger-full-access'))
            self.assertEqual(result['launch_preset'], preset['id'])
            self.assertEqual(a.dispatch_work_start(self.payload(launch_preset=preset['id'])), (code, result))
            self.assertEqual(tmux.call_count, 2)
            self.assertEqual(a.dispatch_work_start(self.payload())[0], 409)
        with patch.object(a, 'db', return_value=io.StringIO()), \
             patch.object(a, 'agents_payload', return_value={'agents': []}):
            options = a.work_options()[1]
        self.assertEqual(options['projects'][0]['launch_preset'], preset['id'])
        # Completed requests still replay if the shell history entry is rotated away.
        self.write('.zsh_history', '')
        self.assertEqual(a.dispatch_work_start(self.payload(launch_preset=preset['id'])), (code, result))

    def test_rejects_unknown_or_mismatched_preset_and_ignores_client_commands(self):
        self.write('.zsh_history', ': 100:0;claude --model sonnet\n')
        preset = a.work_launch_history()[0]
        with patch.object(a, 'tmux') as tmux:
            self.assertEqual(a.dispatch_work_start(self.payload(launch_preset=preset['id']))[0], 409)
            self.assertEqual(a.dispatch_work_start(self.payload(launch_preset='history-' + '0' * 24))[0], 409)
            self.assertEqual(a.dispatch_work_start(self.payload(launch_preset='codex --model evil'))[0], 400)
            tmux.assert_not_called()
        with patch.object(a, 'tmux', side_effect=['', '@7\t%42\t987']) as tmux:
            self.assertEqual(a.dispatch_work_start(self.payload(request_id='request-new-base-123', argv=['--api-key', 'secret'], env={'PATH': '/evil'}))[0], 200)
            self.assertEqual(tmux.call_args.args[-3:], ('env', 'PATH=/fake/bin:/usr/bin', '/fake/bin/codex'))

    def test_bash_timestamps_and_bounded_tail(self):
        self.write('.bash_history', '#1000000000\ncodex\n#1000000001\nclaude --model sonnet\n')
        history = a.work_launch_history()
        self.assertEqual(history[0]['last_used'], 1000000001.)
        self.assertEqual(history[1]['last_used'], 1000000000.)
        self.write('.zsh_history', 'x' * 200 + '\ncodex\n')
        self.assertEqual(a.work_history_text(self.home / '.zsh_history', 20), 'codex\n')


if __name__ == '__main__':
    unittest.main()
