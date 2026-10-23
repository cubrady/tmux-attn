import json
from contextlib import closing
from pathlib import Path
import shlex
import tempfile
import unittest
from unittest.mock import patch

import attention as a
import codex_observer as o


DONE = '⏺ Waiting for the simulation.\n✻ Crunched for 1m 11s · done 晚上8:47 · 1 shell still running\n────\n❯\n────\n  ⏵⏵ bypass permissions on · 1 shell · ← 1 agent · ↓ to manage'
SESSION = '11111111-2222-3333-4444-555555555555'


class BackgroundShellTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / (SESSION + '.jsonl')
        self.output = self.path.with_suffix('') / 'tasks/job.output'
        self.output.parent.mkdir(parents=True)
        self.output.write_text('')
        self.rows = []
        self.user('run the simulation')
        self.launch('simulate.py', 'job')
        self.table = {123: (1, 'zsh'), 456: (123, 'claude --resume root'),
                      789: (456, self.wrapper('simulate.py')),
                      790: (789, 'python simulate.py')}

    def wrapper(self, command):
        return "/bin/zsh -c source snapshot && export CODEX_COMPANION_SESSION_ID='" + SESSION + "' && eval " + shlex.quote(command) + ' < /dev/null'

    def write(self):
        self.path.write_text('\n'.join(json.dumps(row) for row in self.rows) + '\n')

    def user(self, text):
        self.rows.append({'type': 'user', 'message': {'content': text}})
        self.write()

    def launch(self, command, task_id):
        call = 'call-' + task_id
        self.rows.extend([
            {'type': 'assistant', 'timestamp': '2026-10-22T12:47:10Z', 'message': {'content': [
                {'type': 'tool_use', 'name': 'Bash', 'id': call, 'input': {'command': command, 'run_in_background': True}}]}},
            {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': call,
                'content': f'Command running in background with ID: {task_id}. Output is being written to: {self.output}.'}]}}])
        self.write()

    def state(self, screen=DONE, table=None, path=None, now=1792673240):
        return o.parked_shells(screen, 456, self.table if table is None else table,
                               str(self.path if path is None else path), now)

    def test_live_simulation_is_background_wait_even_after_stop(self):
        self.assertEqual(self.state(), ('RUNNING', 'Background work: waiting on 1 shell', False))

    def test_footer_is_current_not_scrollback_or_quoted_text(self):
        for screen in (DONE.split('────')[0], DONE + '\n────\n❯\n────\n⏵⏵ bypass permissions on',
                       DONE.replace('⏵⏵ bypass permissions on · 1 shell', 'Quoted: 1 shell'),
                       DONE.replace('❯\n', '❯ Message @general-purpose…\n')):
            with self.subTest(screen=screen):
                self.assertIsNone(self.state(screen))
        self.assertEqual(o.shell_footer(DONE.replace('1 shell ·', '4 shells ·')), 4)
        self.assertEqual(o.shell_footer(DONE.replace('1 shell ·', '2 shells, 1 monitor ·')), 3)
        self.assertEqual(o.shell_footer(DONE.replace('1 shell ·', '1 monitor ·')), 1)
        for spacing in (' ', '  ', '\t'):
            with self.subTest(spacing=spacing):
                drawer = DONE + '\n\n  ⏺ main\n❯' + spacing + '◯ general-purpose  Reading diff   1h 49m · ↓ 171.3k tokens'
                self.assertEqual(self.state(drawer), ('RUNNING', 'Background work: waiting on 1 shell', False))

    def test_monitor_tool_tracks_active_watch_until_stream_end_or_expiry(self):
        self.user('watch the suite')
        self.rows.extend([
            {'type': 'assistant', 'timestamp': '2026-10-22T12:48:10Z', 'message': {'content': [
                {'type': 'tool_use', 'name': 'Monitor', 'id': 'call-mon', 'input': {'command': 'watch.sh'}}]}},
            {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'call-mon',
                'content': 'Monitor started (task mon1, expires in 25m unless the source ends first).'}]}}])
        self.write()
        table = self.table | {801: (456, self.wrapper('watch.sh'))}
        screen = DONE.replace('1 shell ·', '2 shells, 1 monitor ·')
        self.assertEqual(self.state(screen, table), ('RUNNING', 'Background work: waiting on 1 shell', False))
        # Intermediate monitor events do not retire a still-running watch.
        self.user('<task-notification><task-id>mon1</task-id><event>tick</event></task-notification>')
        self.assertEqual(self.state(screen, table), ('RUNNING', 'Background work: waiting on 1 shell', False))
        self.user('<task-notification><task-id>mon1</task-id><event>[Monitor expired after 25m with 1 event delivered.]</event></task-notification>')
        self.assertIsNone(self.state(screen, table))

    def test_shell_must_be_native_direct_child_with_matching_command(self):
        for table in ({123: (1, 'zsh'), 456: (123, 'claude'), 789: (456, 'npm exec mcp-server')},
                      {789: (999, self.wrapper('simulate.py'))},
                      {789: (456, self.wrapper('old-review.py'))}):
            self.assertIsNone(self.state(table=table))
        self.assertEqual(o.shell_command('/bin/zsh -c eval unterminated\''), '')

    def test_old_turn_shells_do_not_count(self):
        self.user('new task')
        self.assertIsNone(self.state())
        self.launch('new.py', 'new-job')
        table = self.table | {800: (456, self.wrapper('new.py'))}
        self.assertEqual(self.state(DONE.replace('1 shell ·', '4 shells ·'), table)[1],
                         'Background work: waiting on 1 shell')

    def test_completion_and_failure_notifications_retire_task_ids(self):
        for status in ('completed', 'failed', 'killed'):
            with self.subTest(status=status):
                before = len(self.rows)
                self.user(f'<task-notification><task-id>job</task-id><status>{status}</status></task-notification>')
                self.assertIsNone(self.state())
                self.rows = self.rows[:before]
                self.write()

    def test_structured_task_output_retires_completed_task(self):
        self.rows.append({'type': 'user', 'message': {'content': [{'type': 'tool_result', 'content': 'done'}]},
                          'toolUseResult': {'task': {'task_id': 'job', 'status': 'completed'}}})
        self.write()
        self.assertIsNone(self.state())

    def test_missing_process_is_not_running_despite_stale_footer(self):
        self.assertIsNone(self.state(table={456: (123, 'claude')}))

    def test_missing_or_truncated_transcript_is_unknown(self):
        self.path.unlink()
        self.assertIsNone(self.state())
        self.rows = self.rows[1:]
        self.write()
        self.assertIsNone(self.state())

    def test_resume_uses_wrapper_session_identity_not_latest_file(self):
        missing = self.path.with_name('99999999-2222-3333-4444-555555555555.jsonl')
        self.assertEqual(self.state(path=missing)[0], 'RUNNING')
        self.assertIsNone(self.state(path=missing, table={789: (456, '/bin/zsh -c source snapshot && eval simulate.py')}))

    def test_cache_refreshes_after_new_turn(self):
        self.assertEqual(self.state()[0], 'RUNNING')
        with patch.object(Path, 'open', side_effect=AssertionError('reread unchanged transcript')):
            # Output is read separately; only query the transcript ledger here.
            self.assertEqual(len(o.read_tasks(self.path)['tasks']), 1)
        self.user('next task')
        self.assertIsNone(self.state())

    def test_appended_events_preserve_identity_outside_cold_tail(self):
        self.assertEqual(self.state()[0], 'RUNNING')
        with self.path.open('ab') as stream:
            stream.write(b'not a task\n' * (o.TAIL_BYTES // 11))
        self.assertEqual(self.state()[0], 'RUNNING')
        with self.path.open('ab') as stream:
            stream.write(b'{"type":"assistant","message":{"content":[]}}\n')
        self.assertEqual(self.state()[0], 'RUNNING')
        # Invalidate by a new real user, even when the launch is outside the tail.
        with self.path.open('ab') as stream:
            stream.write(b'{"type":"user","message":{"content":"next"}}\n')
        self.assertIsNone(self.state())

    def test_large_transcript_reads_only_bounded_tail_and_skips_api_blobs(self):
        contents = self.path.read_bytes()
        self.path.write_bytes(b'x' * (o.TAIL_BYTES + 100) + b'\n' + contents)
        self.assertEqual(self.state()[0], 'RUNNING')
        self.path.write_bytes(contents + b'\n' + b'x' * (o.TAIL_BYTES + 100))
        self.assertIsNone(self.state())

    def test_unrelated_or_sidechain_messages_do_not_create_jobs(self):
        for row in self.rows[1:]:
            row['isSidechain'] = True
        self.write()
        self.assertIsNone(self.state())

    def test_error_output_prompts_progress_check_without_marking_task_failed(self):
        self.output.write_text('Traceback (most recent call last):\nFileNotFoundError: missing test')
        state = self.state()
        self.assertEqual(state[0], 'RUNNING')
        self.assertIn('task output contains an error; check progress', state[1])

    def test_age_alone_is_not_failure_and_recent_output_avoids_stall_hint(self):
        now = 1792673240 + o.STALE_SECONDS + 100
        with patch('codex_observer._output_info', return_value=(1792673240, False)):
            state = self.state(now=now)
            self.assertEqual(state[0], 'RUNNING')
            self.assertIn('possibly stalled', state[1])
        with patch('codex_observer._output_info', return_value=(now - 5, False)):
            self.assertNotIn('stalled', self.state(now=now)[1])

    def test_output_path_must_belong_to_the_identified_session_and_task(self):
        self.assertIsNone(o._output_info({'id': 'other', 'output': str(self.output)}, self.path))
        self.assertIsNone(o._output_info({'id': 'job', 'output': str(self.output)}, self.path.with_name('other.jsonl')))

    def test_observer_priorities_transitions_and_revision_stability(self):
        with patch.object(a, 'STATE', Path(self.tmp.name) / 'state'), closing(a.db()) as con:
            live = {('%1', '123'): {'path': '/project'}}
            def scan(screen=DONE, now=1792673240):
                with patch('codex_observer.tmux', return_value=screen), patch('codex_observer.time.time', return_value=now):
                    o.observe_claude(con, 'sock', live, self.table)
            a.record(con, 'sock', '%1', '123', 'claude', 'IDLE', 'Response finished', event_time=1792673230)
            con.execute('update panes set transcript_path=?', (str(self.path),))
            con.commit()
            scan()
            row = dict(con.execute('select * from panes').fetchone())
            self.assertEqual(a.agent_emoji(row), '⏳')
            scan()
            self.assertEqual(con.execute('select revision from panes').fetchone()[0], row['revision'])
            # A live foreground spinner wins over shells in its footer.
            scan(DONE.replace('✻ Crunched for 1m 11s · done 晚上8:47 · 1 shell still running',
                              '✢ Working… (2s)') + '\nesc to interrupt', now=1792673250)
            self.assertEqual(con.execute('select reason from panes').fetchone()[0], 'Working')
            # Native approval and answer dialogs must not become a parked shell.
            a.record(con, 'sock', '%1', '123', 'claude', 'WAIT', 'Approval needed', event_time=1792673260)
            scan(now=1792673270)
            self.assertEqual(con.execute('select state,reason from panes').fetchone()[:], ('WAIT', 'Approval needed'))
            a.record(con, 'sock', '%1', '123', 'claude', 'IDLE', 'Done', event_time=1792673280)
            scan(DONE + '\nDo you want to proceed?\n❯ 1. Yes', now=1792673290)
            self.assertEqual(con.execute('select state from panes').fetchone()[0], 'IDLE')
            scan(DONE.replace('✻ Crunched for 1m 11s · done 晚上8:47 · 1 shell still running',
                              '✻ Waiting for 1 background agent to finish'), now=1792673300)
            self.assertEqual(a.agent_emoji(dict(con.execute('select * from panes').fetchone())), '⏳')
            scan(DONE.replace('· 1 shell ·', '·'), now=1792673310)
            self.assertEqual(con.execute('select state from panes').fetchone()[0], 'IDLE')


if __name__ == '__main__':
    unittest.main()
