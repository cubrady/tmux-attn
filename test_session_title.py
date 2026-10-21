"""Session titles: the agent's own title, read from the tmux pane title it sets."""
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('attention', Path(__file__).with_name('attention.py'))
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)


class PaneTitleTest(unittest.TestCase):
    def title(self, raw, path='/Users/dev/proj/seramey-mita-brain', command='codex'):
        return a.pane_session_title(raw, path, command)

    def test_claude_strips_status_glyph(self):
        self.assertEqual(self.title('✳ Cory 專案列表與內頁模組', command='2.1.303'), 'Cory 專案列表與內頁模組')

    def test_claude_strips_spinner_glyph(self):
        self.assertEqual(self.title('⠐ Cory Project 任務成本', command='2.1.301'), 'Cory Project 任務成本')

    def test_codex_strips_project_suffix(self):
        self.assertEqual(self.title('確認教材並規劃前端整合 | seramey-mita-brain'), '確認教材並規劃前端整合')

    def test_keeps_pipe_that_is_not_the_project(self):
        self.assertEqual(self.title('Fix a | b parser'), 'Fix a | b parser')

    def test_default_titles_are_not_titles(self):
        for raw in ('', '  ', 'Claude Code', '✳ Claude Code', 'codex', 'seramey-mita-brain', 'brad@host', 'zsh', '2.1.303'):
            with self.subTest(raw=raw):
                self.assertEqual(self.title(raw, command='2.1.303'), '')

    def test_title_is_clipped(self):
        self.assertEqual(len(self.title('x' * 500)), a.SESSION_TITLE_CHARS)


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
