"""Viewing events confirm only a human client's selected pane and snapshot."""
from contextlib import closing
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import attention as a


class ViewedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        state = patch.object(a, 'STATE', Path(self.tmp.name))
        state.start()
        self.addCleanup(state.stop)
        publishing = patch.object(a, 'publish_view_revision')
        publishing.start()
        self.addCleanup(publishing.stop)
        self.con = a.db()
        self.addCleanup(self.con.close)
        self.record()

    def record(self, state='IDLE', reason='Finished', review=True):
        a.record(self.con, 'sock', '%1', '123', 'claude', state, reason, review)
        return dict(self.con.execute('SELECT * FROM panes').fetchone())

    def viewed(self, token='123:1', visible='%1 123 0', client='tty', pid='123'):
        with patch.object(a, 'tmux', return_value=visible) as tmux:
            result = a.acknowledge_visible_pane(self.con, 'sock', client, '%1', pid, token)
            if client and token == '123:1' and pid == '123':
                self.assertEqual(tmux.call_args.kwargs['sock'], 'sock')
            return result

    def test_selected_completion_persists_viewed_without_new_revision(self):
        self.assertTrue(self.viewed())
        with closing(a.db()) as con:
            row = con.execute('SELECT * FROM panes').fetchone()
            self.assertEqual((row['revision'], row['acknowledged']), (1, 1))

    def test_background_changed_and_control_clients_do_not_confirm(self):
        for client, visible in [('', '%1 123 0'), ('tty', '%2 456 0'), ('tty', '%1 123 1'), ('tty', '')]:
            with self.subTest(client=client, visible=visible):
                self.assertFalse(self.viewed(client=client, visible=visible))
        self.assertEqual(self.con.execute('SELECT acknowledged FROM panes').fetchone()[0], 0)

    def test_invalid_future_and_reused_pane_tokens_do_not_confirm(self):
        for token, pid in [('', '123'), ('123:0', '123'), ('123:2', '123'), ('123:1', '999'), ('999:1', '123')]:
            with self.subTest(token=token, pid=pid):
                self.assertFalse(self.viewed(token=token, pid=pid))
        self.assertEqual(self.con.execute('SELECT acknowledged FROM panes').fetchone()[0], 0)

    def test_delayed_selection_confirms_old_snapshot_only(self):
        current = self.record(reason='New result')
        self.assertTrue(self.viewed(token='123:1'))
        row = self.con.execute('SELECT * FROM panes').fetchone()
        self.assertEqual(row['acknowledged'], 1)
        self.assertEqual(row['revision'], current['revision'])
        self.assertLess(row['acknowledged'], row['revision'])

    def test_wait_running_and_untracked_panes_stay_unchanged(self):
        for state, review in [('WAIT', False), ('RUNNING', False), ('IDLE', False)]:
            row = self.record(state, state, review)
            self.assertFalse(self.viewed(token=f"123:{row['revision']}"))
        self.con.execute('DELETE FROM panes')
        self.con.commit()
        self.assertFalse(self.viewed())


if __name__ == '__main__':
    unittest.main()
