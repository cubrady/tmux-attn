"""Regression coverage for version-specific review acknowledgement."""
import importlib.util
import json
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen


spec = importlib.util.spec_from_file_location('attention_review_tests', Path(__file__).with_name('attention.py'))
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)


class ReviewAcknowledgementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.previous_state = a.STATE
        a.STATE = Path(self.tmp.name)
        self.con = a.db()
        self.live = {
            ('%1', '123'): dict(pane='%1', pid='123', session='work', window='one',
                                window_id='@1', path='/not-a-real-repo'),
        }
        self.patches = [
            patch.object(a, 'local_host_name', return_value='home'),
            patch.object(a, 'resolve_socket', return_value='socket'),
            patch.object(a, 'live_panes', return_value=self.live),
            patch.object(a, 'maybe_notify_wait'),
        ]
        for mock in self.patches:
            mock.start()

    def tearDown(self):
        for mock in reversed(self.patches):
            mock.stop()
        self.con.close()
        a.STATE = self.previous_state
        self.tmp.cleanup()

    def record(self, reason='Finished', pane='%1', pid='123', state='IDLE', review=True):
        a.record(self.con, 'socket', pane, pid, 'codex', state, reason, review)
        return dict(self.con.execute('SELECT * FROM panes WHERE pane=? AND pid=?', (pane, pid)).fetchone())

    def item(self, pane='%1'):
        rows, _ = a.items(self.con, 'socket', self.live, True, now=time.time() + 3)
        return next(row for row in rows if row['pane'] == pane)

    def payload(self, revision=None, **changes):
        if revision is None:
            revision = self.item()['revision']
        return dict(host='home', pane='%1', pid='123', revision=revision) | changes

    def test_reviewed_completion_stays_in_review_group_and_payload(self):
        self.record()
        unread = self.item()
        self.assertTrue(unread['unread'])
        self.assertFalse(unread['reviewed'])
        a.acknowledge(self.con, unread)
        reviewed = self.item()
        self.assertFalse(reviewed['unread'])
        self.assertTrue(reviewed['reviewed'])
        groups = a.status_groups([reviewed])
        self.assertEqual(groups['review'], [reviewed])
        self.assertEqual(groups['idle'], [])
        payload = a.agents_payload(self.con, 'socket', self.live, now=time.time() + 3)
        row = payload['agents'][0]
        for field in ('needs_review', 'unread', 'reviewed', 'revision', 'acknowledged'):
            self.assertIn(field, row)
        self.assertTrue(row['reviewed'])
        self.assertFalse(row['unread'])
        self.assertEqual(row['revision'], row['acknowledged'])
        self.assertEqual(payload['summary']['review'], 0)
        self.assertEqual(payload['summary']['review_seen'], 1)
        self.assertEqual(payload['summary']['idle'], 0)

    def test_unread_review_sorts_before_newer_reviewed_then_latest_within_group(self):
        rows = [
            dict(pane='%1', state='IDLE', needs_review=True, unread=True, updated=100),
            dict(pane='%2', state='IDLE', needs_review=True, unread=True, updated=200),
            dict(pane='%3', state='IDLE', needs_review=True, unread=False, reviewed=True, updated=400),
            dict(pane='%4', state='IDLE', needs_review=True, unread=False, reviewed=True, updated=300),
            dict(pane='%5', state='RUNNING', needs_review=False, updated=500),
            dict(pane='%6', state='IDLE', needs_review=False, updated=600),
        ]
        self.assertEqual([row['pane'] for row in sorted(rows, key=a.agent_sort_key)],
                         ['%2', '%1', '%3', '%4', '%5', '%6'])

    def test_new_completion_returns_to_unread_after_persisted_ack(self):
        old = self.record()
        code, body = a.dispatch_ack(self.payload(old['revision']))
        self.assertEqual(code, 200)
        self.assertTrue(body['ok'])
        with closing(a.db()) as reopened:
            persisted, _ = a.items(reopened, 'socket', self.live, True, now=time.time() + 3)
        self.assertTrue(persisted[0]['reviewed'])
        self.record('New result')
        current = self.item()
        self.assertTrue(current['unread'])
        self.assertFalse(current['reviewed'])
        self.assertEqual(current['acknowledged'], old['revision'])

    def test_old_web_ack_leaves_new_completion_unread(self):
        old = self.record()
        current = self.record('New result')
        code, body = a.dispatch_ack(self.payload(old['revision']))
        self.assertEqual(code, 200)
        self.assertTrue(body['ok'])
        item = self.item()
        self.assertEqual(item['revision'], current['revision'])
        self.assertEqual(item['acknowledged'], old['revision'])
        self.assertTrue(item['unread'])

    def test_concurrent_duplicate_and_out_of_order_acknowledgements_do_not_regress(self):
        revisions = [self.record(f'Result {number}')['revision'] for number in range(6)]
        payloads = [self.payload(revision) for revision in revisions[::-1] + revisions + revisions[::-1]]
        with ThreadPoolExecutor(max_workers=6) as pool:
            responses = list(pool.map(a.dispatch_ack, payloads))
        self.assertTrue(all(code == 200 and body['ok'] for code, body in responses))
        item = self.item()
        self.assertEqual(item['acknowledged'], max(revisions))
        self.assertFalse(item['unread'])

    def test_snooze_and_settle_do_not_erase_unread_identity(self):
        self.record()
        row = self.item()
        a.acknowledge(self.con, row, 300)
        snoozed = self.item()
        self.assertFalse(snoozed['pending'])
        self.assertTrue(snoozed['unread'])
        self.assertFalse(snoozed['reviewed'])
        self.record('Fresh result')
        settling, _ = a.items(self.con, 'socket', self.live, True, now=time.time())
        self.assertFalse(settling[0]['pending'])
        self.assertTrue(settling[0]['unread'])

    def test_jump_acknowledges_only_the_opened_revision(self):
        self.record()
        target = self.item()

        def complete_during_jump(item, client=None):
            self.record('New result during jump')
            return True

        with patch.object(a, 'jump', side_effect=complete_during_jump) as jump:
            self.assertTrue(a.jump_and_acknowledge(self.con, target, 'client-one'))
        jump.assert_called_once_with(target, 'client-one')
        latest = self.item()
        self.assertEqual(latest['acknowledged'], target['revision'])
        self.assertTrue(latest['unread'])

    def test_failed_jump_and_wait_jump_do_not_acknowledge(self):
        self.record()
        target = self.item()
        with patch.object(a, 'jump', return_value=False):
            self.assertFalse(a.jump_and_acknowledge(self.con, target))
        self.assertTrue(self.item()['unread'])
        with patch.object(a, 'jump', side_effect=RuntimeError('client vanished')):
            with self.assertRaises(RuntimeError):
                a.jump_and_acknowledge(self.con, target)
        self.assertTrue(self.item()['unread'])
        self.record('Approval required', state='WAIT', review=False)
        with patch.object(a, 'jump', return_value=True):
            self.assertTrue(a.jump_and_acknowledge(self.con, self.item()))
        self.assertTrue(self.item()['unread'])
        self.assertEqual(self.item()['acknowledged'], 0)

    def test_ack_rejects_invalid_revision_types_and_future_revision(self):
        row = self.record()
        for value in (None, True, False, 0, -1, '1', 1.0, [], {}):
            with self.subTest(revision=value):
                request = self.payload(row['revision']) | {'revision': value}
                code, body = a.dispatch_ack(request)
                self.assertEqual(code, 400)
                self.assertFalse(body['ok'])
                self.assertEqual(self.item()['acknowledged'], 0)
        code, body = a.dispatch_ack(self.payload(row['revision'] + 1))
        self.assertEqual(code, 409)
        self.assertFalse(body['ok'])
        self.assertTrue(self.item()['unread'])

    def test_ack_rejects_invalid_body_and_missing_identity(self):
        self.record()
        for request in (None, [], 'bad', {}, self.payload(pane=''), self.payload(pid='')):
            with self.subTest(request=request):
                code, body = a.dispatch_ack(request)
                self.assertEqual(code, 400)
                self.assertFalse(body['ok'])
        self.assertTrue(self.item()['unread'])

    def test_ack_rejects_stale_pane_pid_untracked_and_nonreviewable_state(self):
        self.record()
        for changes in ({'pid': '999'}, {'pane': '%999'}):
            with self.subTest(changes=changes):
                code, body = a.dispatch_ack(self.payload(**changes))
                self.assertEqual(code, 409)
                self.assertFalse(body['ok'])
        with patch.object(a, 'live_panes', return_value={}):
            code, body = a.dispatch_ack(self.payload())
        self.assertEqual(code, 409)
        self.assertFalse(body['ok'])
        self.con.execute('DELETE FROM panes')
        self.con.commit()
        code, body = a.dispatch_ack(self.payload(1))
        self.assertEqual(code, 409)
        self.assertFalse(body['ok'])
        current = self.record('Working', state='RUNNING', review=False)
        code, body = a.dispatch_ack(self.payload(current['revision']))
        self.assertEqual(code, 409)
        self.assertFalse(body['ok'])

    def test_explicit_wait_ack_preserves_wait_group(self):
        self.record('Answer needed', state='WAIT', review=False)
        code, body = a.dispatch_ack(self.payload())
        self.assertEqual(code, 200)
        self.assertTrue(body['ok'])
        item = self.item()
        self.assertTrue(item['reviewed'])
        self.assertEqual(a.status_groups([item])['wait'], [item])

    def test_peer_ack_forwards_exact_version_and_cannot_mark_local_pane(self):
        self.record()
        peer_reply = {'ok': True, 'revision': 17, 'host': 'lab'}
        with patch.object(a, 'load_peers', return_value=[dict(name='lab', url='http://lab:7788/')]), \
             patch.object(a, 'urlopen') as remote:
            remote.return_value.__enter__.return_value.read.return_value = json.dumps(peer_reply).encode()
            code, body = a.dispatch_ack(self.payload(17, host='lab'))
        self.assertEqual(code, 200)
        self.assertEqual(body, peer_reply)
        request = remote.call_args.args[0]
        self.assertEqual(request.full_url, 'http://lab:7788/api/ack')
        self.assertEqual(request.get_method(), 'POST')
        forwarded = json.loads(request.data)
        self.assertEqual(forwarded['revision'], 17)
        self.assertEqual(forwarded['pane'], '%1')
        self.assertEqual(forwarded['pid'], '123')
        self.assertTrue(forwarded['_local_only'])
        self.assertTrue(self.item()['unread'])

    def test_web_ack_route_persists_reviewed_state_in_next_feed(self):
        self.record()
        base_handler = a.make_web_handler()

        class QuietHandler(base_handler):
            def log_message(self, *args):
                pass

        server = a.ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f'http://127.0.0.1:{server.server_address[1]}'
            request = Request(base + '/api/ack', data=json.dumps(self.payload()).encode(),
                              headers={'Content-Type': 'application/json'}, method='POST')
            with urlopen(request, timeout=2) as response:
                result = json.loads(response.read())
            self.assertTrue(result['ok'])
            with urlopen(base + '/api/agents', timeout=2) as response:
                feed = json.loads(response.read())
            self.assertTrue(feed['agents'][0]['reviewed'])
            self.assertFalse(feed['agents'][0]['unread'])
            self.assertEqual(feed['summary']['review'], 0)
            self.assertEqual(feed['summary']['review_seen'], 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_offline_or_unknown_peer_ack_does_not_clear_unread(self):
        self.record()
        with patch.object(a, 'load_peers', return_value=[]):
            code, body = a.dispatch_ack(self.payload(host='missing'))
        self.assertEqual(code, 404)
        self.assertFalse(body['ok'])
        with patch.object(a, 'load_peers', return_value=[dict(name='lab', url='http://lab:7788')]), \
             patch.object(a, 'urlopen', side_effect=OSError('offline')):
            code, body = a.dispatch_ack(self.payload(host='lab'))
        self.assertEqual(code, 502)
        self.assertFalse(body['ok'])
        self.assertTrue(self.item()['unread'])

    def test_preview_reports_displayed_version_without_acknowledging(self):
        row = self.record()
        preview = {'users': ['Please finish'], 'agent': 'Finished successfully'}
        with patch.object(a, 'pane_conversation_preview', return_value=(preview, row['revision'])):
            code, body = a.dispatch_preview(dict(pane='%1', pid='123'))
        self.assertEqual(code, 200)
        self.assertEqual(body['preview'], preview)
        self.assertEqual(body['revision'], row['revision'])
        self.assertTrue(body['viewable'])
        self.assertTrue(self.item()['unread'])
        wait_row = self.record('Approval required', state='WAIT', review=False)
        with patch.object(a, 'pane_conversation_preview', return_value=(preview, wait_row['revision'])):
            code, body = a.dispatch_preview(dict(pane='%1', pid='123'))
        self.assertEqual(code, 200)
        self.assertFalse(body['viewable'])
        self.assertTrue(self.item()['unread'])

    def test_empty_or_failed_preview_does_not_claim_viewable_content(self):
        row = self.record()
        with patch.object(a, 'pane_conversation_preview', return_value=({'users': [], 'agent': ''}, row['revision'])):
            code, body = a.dispatch_preview(dict(pane='%1', pid='123'))
        self.assertEqual(code, 200)
        self.assertFalse(body['viewable'])
        with patch.object(a, 'pane_conversation_preview', side_effect=RuntimeError('transcript unavailable')):
            code, body = a.dispatch_preview(dict(pane='%1', pid='123'))
        self.assertEqual(code, 500)
        self.assertFalse(body['ok'])
        self.assertTrue(self.item()['unread'])

    def test_preview_revision_change_during_fetch_requires_retry(self):
        original = self.record()

        def newer_result(*args, **kwargs):
            self.record('New result while fetching')
            return {'users': ['Old prompt'], 'agent': 'Old result'}, original['revision']

        with patch.object(a, 'pane_conversation_preview', side_effect=newer_result):
            code, body = a.dispatch_preview(dict(pane='%1', pid='123'))
        self.assertEqual(code, 409)
        self.assertFalse(body['ok'])
        latest = self.item()
        self.assertGreater(latest['revision'], original['revision'])
        self.assertTrue(latest['unread'])

    def test_previous_result_cache_cannot_acknowledge_new_result_without_transcript(self):
        self.record('First result')
        a.remember_preview(self.con, 'socket', '%1', '123', agent='First answer')
        self.record('Working on next task', state='RUNNING', review=False)
        self.record('Second result')
        with patch.object(a, 'resolve_transcript_path', return_value=''):
            code, body = a.dispatch_preview(dict(pane='%1', pid='123'))
        self.assertEqual(code, 200)
        self.assertFalse(body['viewable'])
        self.assertTrue(self.item()['unread'])

    def transcript(self, messages, *, bound=True):
        path = Path(self.tmp.name) / 'rollout-review.jsonl'
        path.write_text(''.join(json.dumps({
            'type': 'event_msg', 'payload': {'type': kind, 'message': content},
        }) + '\n' for kind, content in messages))
        self.transcript_mtime = getattr(self, 'transcript_mtime', 1000) + 1
        os.utime(path, (self.transcript_mtime, self.transcript_mtime))
        if bound:
            with self.con:
                self.con.execute('UPDATE panes SET transcript_path=? WHERE pane=? AND pid=?',
                                 (str(path), '%1', '123'))
        return path

    def test_unchanged_old_transcript_does_not_bind_new_completion(self):
        first = self.record('First result')
        path = self.transcript([('user_message', 'First request'), ('agent_message', 'First answer')])
        with patch.object(a, 'resolve_transcript_path', return_value=str(path)):
            code, first_body = a.dispatch_preview(dict(pane='%1', pid='123'))
            self.assertEqual(code, 200)
            self.assertTrue(first_body['viewable'])
            self.assertEqual(first_body['revision'], first['revision'])
            self.record('Working again', state='RUNNING', review=False)
            latest = self.record('Second result')
            code, body = a.dispatch_preview(dict(pane='%1', pid='123'))
        self.assertEqual(code, 200)
        self.assertEqual(body['revision'], latest['revision'])
        self.assertEqual(body['preview']['agent'], 'First answer')
        self.assertFalse(body['viewable'])
        self.assertTrue(self.item()['unread'])

    def test_old_transcript_cannot_bind_after_hook_has_cached_new_user(self):
        self.record('First result')
        path = self.transcript([('user_message', 'First request'), ('agent_message', 'First answer')])
        with patch.object(a, 'resolve_transcript_path', return_value=str(path)):
            _, body = a.dispatch_preview(dict(pane='%1', pid='123'))
            self.assertTrue(body['viewable'])
            running = self.record('Working again', state='RUNNING', review=False)
            a.remember_preview(self.con, 'socket', '%1', '123', user='Second request',
                               expected_revision=running['revision'])
            self.record('Second result')
            code, body = a.dispatch_preview(dict(pane='%1', pid='123'))
        self.assertEqual(code, 200)
        self.assertFalse(body['viewable'])
        self.assertTrue(self.item()['unread'])

    def test_new_user_without_answer_is_not_viewable_then_fresh_answer_binds(self):
        self.record('First result')
        original_messages = [('user_message', 'First request'), ('agent_message', 'First answer')]
        path = self.transcript(original_messages)
        with patch.object(a, 'resolve_transcript_path', return_value=str(path)):
            _, first_body = a.dispatch_preview(dict(pane='%1', pid='123'))
            self.assertTrue(first_body['viewable'])
            self.record('Working again', state='RUNNING', review=False)
            latest = self.record('Second result')
            messages = original_messages + [('user_message', 'Second request')]
            self.transcript(messages)
            code, waiting_body = a.dispatch_preview(dict(pane='%1', pid='123'))
            self.assertEqual(code, 200)
            self.assertEqual(waiting_body['preview']['agent'], '')
            self.assertFalse(waiting_body['viewable'])
            self.transcript(messages + [('agent_message', 'Second answer')])
            code, fresh_body = a.dispatch_preview(dict(pane='%1', pid='123'))
        self.assertEqual(code, 200)
        self.assertEqual(fresh_body['preview']['agent'], 'Second answer')
        self.assertTrue(fresh_body['viewable'])
        self.assertEqual(fresh_body['revision'], latest['revision'])
        saved = self.con.execute('SELECT preview_revision FROM panes').fetchone()
        self.assertEqual(saved['preview_revision'], latest['revision'])
        self.assertTrue(self.item()['unread'])

    def test_stale_hook_preview_cannot_write_newer_row(self):
        old = self.record('First result')
        current = self.record('Second result')
        a.remember_preview(self.con, 'socket', '%1', '123', agent='Second answer',
                           expected_revision=current['revision'])
        before = dict(self.con.execute('SELECT * FROM panes').fetchone())
        a.remember_preview(self.con, 'socket', '%1', '123', user='Stale user', agent='First answer',
                           transcript_path='/stale/path', expected_revision=old['revision'])
        after = dict(self.con.execute('SELECT * FROM panes').fetchone())
        self.assertEqual(after, before)
        self.assertEqual(after['preview_revision'], current['revision'])

    def test_current_hook_answer_is_not_replaced_by_lagging_transcript(self):
        current = self.record()
        path = self.transcript([('user_message', 'Old request'), ('agent_message', 'Old answer')])
        a.remember_preview(self.con, 'socket', '%1', '123', user='Current request', agent='Current answer',
                           transcript_path=str(path), expected_revision=current['revision'])
        with patch.object(a, 'resolve_transcript_path', return_value=str(path)):
            code, body = a.dispatch_preview(dict(pane='%1', pid='123'))
        self.assertEqual(code, 200)
        self.assertEqual(body['preview']['agent'], 'Current answer')
        self.assertTrue(body['viewable'])
        self.assertTrue(self.item()['unread'])

    def test_unbound_cwd_transcript_remains_display_only_after_updates(self):
        self.record('First result')
        path = self.transcript([('user_message', 'Unrelated request'), ('agent_message', 'Unrelated answer')],
                               bound=False)
        with patch.object(a, 'latest_claude_transcript', return_value=str(path)):
            code, body = a.dispatch_preview(dict(pane='%1', pid='123'))
            self.assertEqual(code, 200)
            self.assertEqual(body['preview']['agent'], 'Unrelated answer')
            self.assertFalse(body['viewable'])
            self.record('New result')
            self.transcript([('user_message', 'Unrelated request'), ('agent_message', 'Unrelated answer'),
                             ('user_message', 'Unrelated second request'), ('agent_message', 'Unrelated second answer')],
                            bound=False)
            code, body = a.dispatch_preview(dict(pane='%1', pid='123'))
        self.assertEqual(code, 200)
        self.assertFalse(body['viewable'])
        self.assertTrue(self.item()['unread'])

    def test_reviewed_inbox_lines_render_read_group_and_label(self):
        self.record()
        a.acknowledge(self.con, self.item())
        lines = a.inbox_lines([self.item()])
        self.assertTrue(any(identity is None and '待看 · 已看 (1)' in text for identity, text in lines))
        self.assertTrue(any(identity == ('%1', '123') and '已查看' in text for identity, text in lines))

    def test_pending_items_choose_newest_completion_first(self):
        self.live[('%2', '456')] = dict(pane='%2', pid='456', session='work', window='two',
                                       window_id='@2', path='/not-a-real-repo')
        a.record(self.con, 'socket', '%1', '123', 'codex', 'IDLE', 'Older', True, event_time=100)
        a.record(self.con, 'socket', '%2', '456', 'codex', 'IDLE', 'Newest', True, event_time=200)
        pending, _ = a.items(self.con, 'socket', self.live, now=203)
        self.assertEqual([row['pane'] for row in pending], ['%2', '%1'])

    def test_running_preview_keeps_refreshing_until_final_result(self):
        self.record('Working', state='RUNNING', review=False)
        path = self.transcript([('user_message', 'Request'), ('agent_message', 'First progress')])
        with patch.object(a, 'resolve_transcript_path', return_value=str(path)):
            _, first = a.dispatch_preview(dict(pane='%1', pid='123'))
            self.assertEqual(first['preview']['agent'], 'First progress')
            self.assertFalse(first['viewable'])
            self.transcript([('user_message', 'Request'), ('agent_message', 'Second progress')])
            _, second = a.dispatch_preview(dict(pane='%1', pid='123'))
            self.assertEqual(second['preview']['agent'], 'Second progress')
            self.assertFalse(second['viewable'])
            self.record('Finished')
            self.transcript([('user_message', 'Request'), ('agent_message', 'Final result')])
            _, final = a.dispatch_preview(dict(pane='%1', pid='123'))
            self.assertEqual(final['preview']['agent'], 'Final result')
            self.assertTrue(final['viewable'])


if __name__ == '__main__':
    unittest.main()
