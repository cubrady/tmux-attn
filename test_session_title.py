"""Session titles: the agent's own title, read from the tmux pane title it sets."""
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('attention', Path(__file__).with_name('attention.py'))
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)


class PaneTitleTest(unittest.TestCase):
    def title(self, raw, path='/Users/example/proj/example-project', command='codex'):
        return a.pane_session_title(raw, path, command)

    def test_claude_strips_status_glyph(self):
        self.assertEqual(self.title('✳ Sample 專案列表與內頁模組', command='2.1.303'), 'Sample 專案列表與內頁模組')

    def test_claude_strips_spinner_glyph(self):
        self.assertEqual(self.title('⠐ Sample Project 任務成本', command='2.1.301'), 'Sample Project 任務成本')

    def test_codex_strips_project_suffix(self):
        self.assertEqual(self.title('確認教材並規劃前端整合 | example-project'), '確認教材並規劃前端整合')

    def test_keeps_pipe_that_is_not_the_project(self):
        self.assertEqual(self.title('Fix a | b parser'), 'Fix a | b parser')

    def test_default_titles_are_not_titles(self):
        for raw in ('', '  ', 'Claude Code', '✳ Claude Code', 'codex', 'example-project', 'user@host', 'zsh', '2.1.303',
                    'Pi-Bolt - example-project', 'π - example-project', 'pi-bin', 'Pi-Bolt'):
            with self.subTest(raw=raw):
                self.assertEqual(self.title(raw, command='2.1.303'), '')

    def test_pi_bolt_custom_session_title_strips_prefix_and_project_suffix(self):
        self.assertEqual(
            self.title('Pi-Bolt - Round 5 Frozen Prefix Eval - example-project', command='pi-bin'),
            'Round 5 Frozen Prefix Eval',
        )

    def test_title_is_clipped(self):
        self.assertEqual(len(self.title('x' * 500)), a.SESSION_TITLE_CHARS)

    def test_cli_launch_commands_are_not_session_titles(self):
        for raw in ('agy --dangerously-skip-permissions', 'claude --dangerously-skip-permissions',
                    'codex --full-auto', 'agent --model auto', 'cursor --resume',
                    'opencode --continue', 'pi -c', 'pi-bolt --resume',
                    'antigravity-cli --resume', '/opt/bin/agy --resume',
                    '"/Applications/My Tools/agy" --resume'):
            with self.subTest(raw=raw):
                self.assertEqual(self.title(raw), '')

    def test_task_titles_can_mention_cli_flags(self):
        for raw in ('修正 agy --dangerously-skip-permissions 的行為',
                    'Fix --resume handling', 'codex migration plan', "Fix user's --help text",
                    '2026.11.04', '1.2.3'):
            with self.subTest(raw=raw):
                self.assertEqual(self.title(raw), raw)

    def test_launch_command_hint_falls_back_to_real_pane_title(self):
        self.assertEqual(a.pane_session_title('Fix OAuth flow', '/project', 'opencode',
                                             'opencode --continue'), 'Fix OAuth flow')

    def test_peer_launch_title_is_hidden_without_changing_other_fields(self):
        row = {'pane': '%29', 'agent': 'agy', 'state': 'IDLE', 'tracked': True,
               'title': 'agy --dangerously-skip-permissions', 'project': 'roven-edge',
               'preview': {'users': ['Build Demo Studio'], 'agent': 'Done'}}
        tagged, _ = a.tag_host({'agents': [row]}, 'peer')
        self.assertEqual(tagged[0]['title'], '')
        self.assertEqual(tagged[0]['project'], row['project'])
        self.assertEqual(tagged[0]['preview'], row['preview'])
        self.assertEqual(row['title'], 'agy --dangerously-skip-permissions')
        row['title'] = 'Fix --resume handling'
        tagged, _ = a.tag_host({'agents': [row]}, 'peer')
        self.assertEqual(tagged[0]['title'], row['title'])

    def test_opencode_plugin_hint_beats_the_generic_terminal_title(self):
        self.assertEqual(a.pane_session_title('OpenCode', '/project', 'opencode', 'Fix OAuth flow'),
                         'Fix OAuth flow')
        self.assertEqual(a.pane_session_title('OpenCode', '/project', 'opencode', 'OpenCode'), '')
        self.assertEqual(a.pane_session_title('new session', '/project', 'opencode'), '')


class LivePanesTitleTest(unittest.TestCase):
    def test_live_panes_reads_pane_title(self):
        line = '\t'.join(['%1', '10', 's', 'w', '/p', '0', '@1', 'codex', '', '2', '1', '✳ 標題'])
        a_tmux = a.tmux
        a.tmux = lambda *args, **kw: line
        try:
            live = a.live_panes('sock')
        finally:
            a.tmux = a_tmux
        self.assertEqual(live[('%1', '10')]['pane_title'], '✳ 標題')
        self.assertEqual((live[('%1', '10')]['window_index'], live[('%1', '10')]['pane_index']), (2, 1))


if __name__ == '__main__':
    unittest.main()
