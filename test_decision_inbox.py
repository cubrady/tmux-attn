"""Conservative handoff extraction from completed responses."""
from copy import deepcopy
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import attention as a


class DecisionExtractionTests(unittest.TestCase):
    def test_explicit_decisions_are_distinct_from_ci_success(self):
        ci = ('對,CI 已經恢復,而且是真的跑完整套件後通過的。\n'
              'vitest 完整套件全部通過。\n' + '成功紀錄\n' * 50 +
              '還沒收尾的事項\n事故殘留的任務 a1b2c3d4(blocked)需要你在 管理頁 點取消。')
        result = a.extract_decision_inbox(ci)
        self.assertEqual([item['kind'] for item in result['items']], ['operation'])
        self.assertIn('管理頁', result['items'][0]['next_step'])
        decisions = a.extract_decision_inbox(
            '要你決定\n1. P1 要不要照上面做？\n2. #42 怎麼 merge？\n3. 布林欄位那一行要補嗎？')
        self.assertEqual([item['kind'] for item in decisions['items']], ['decision'] * 3)
        self.assertEqual(len({item['id'] for item in decisions['items']}), 3)
        self.assertEqual(a.extract_decision_inbox('CI 已恢復，完整套件通過。')['items'], [])

    def test_consent_and_negation_and_examples(self):
        text = '任務 e5f6a7b8 還卡在 blocked，沒有恢復選項。修 e5f6a7b8 要寫 正式環境資料，需要你同意。'
        self.assertEqual(a.extract_decision_inbox(text)['items'][0]['kind'], 'decision')
        for text in ('完整測試通過，要不要下一步？', '不需要你同意，CI 已經成功。',
                     '```\n需要你同意\n```', '> 需要你同意', 'No approval required. CI green.',
                     "CI restored. We don't need your approval.", 'Your approval is not required.'):
            with self.subTest(text=text):
                self.assertEqual(a.extract_decision_inbox(text)['items'], [])
        self.assertEqual(len(a.extract_decision_inbox(
            '要你決定\n1. P1 要不要做？\n## 結果\n1. CI 有通過嗎？')['items']), 1)

    def test_operation_phrase_precedes_section_question_unless_explicit_approval(self):
        result = a.extract_decision_inbox(
            '要你決定\n1. 需要你在 管理頁 點取消？\n'
            '2. 請你在 管理頁 點取消，需要你同意嗎？\n'
            '3. P1 要不要照上面做？\n4. 不需要你在 管理頁 點取消？')
        self.assertEqual([item['kind'] for item in result['items']],
                         ['operation', 'decision', 'decision'])
        self.assertEqual(result['items'][0]['rule'], 'explicit human operation phrase')
        for text in ('需要你決定是否在 管理頁 點取消？', '請你批准後在 管理頁 點取消？'):
            self.assertEqual(a.extract_decision_inbox('要你決定\n1. ' + text)['items'][0]['kind'], 'decision')

    def test_bounded_hostile_and_malformed_input(self):
        text = '需要你同意 <img src=x onerror=alert(1)> ' + 'x' * 5000
        result = a.extract_decision_inbox(text)
        self.assertLessEqual(len(result['items'][0]['next_step']), 240)
        self.assertLessEqual(len(result['items'][0]['evidence']), 360)
        self.assertIn('<img', result['items'][0]['evidence'])  # UI must escape plain text.
        self.assertTrue(a.extract_decision_inbox('x' * (a.INBOX_SCAN_CHARS + 1))['truncated'])
        result = a.extract_decision_inbox('要你決定\n' + '\n'.join(f'{n}. 要不要做？' for n in range(1, 30)))
        self.assertEqual(len(result['items']), a.INBOX_MAX_ITEMS)
        self.assertTrue(result['truncated'])
        for value in (None, [], {'items': ['need your approval']}, 42):
            self.assertEqual(a.extract_decision_inbox(value)['items'], [])


def fixture(revision=2, state='IDLE', online=True):
    handoff = {'revision': revision, 'source': 'completed response',
               **a.extract_decision_inbox('需要你同意寫 正式環境資料。')}
    return {'hosts': [{'name': 'Work', 'ok': online}], 'summary': {},
            'agents': [{'host': 'Work', 'pane': '%3', 'pid': '123', 'project': 'Atlas',
                        'tracked': True, 'state': state, 'needs_review': state == 'IDLE',
                        'revision': revision, 'acknowledged': revision, 'unread': False,
                        'reviewed': True, 'handoff': handoff}]}


class DecisionMetadataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        state = patch.object(a, 'STATE', Path(self.temp.name))
        state.start()
        self.addCleanup(state.stop)
        self.target = {'host': 'Work', 'pane': '%3', 'pid': '123'}
        self.item_id = fixture()['agents'][0]['handoff']['items'][0]['id']

    def dispatch(self, payload, current=None):
        with patch.object(a, 'fleet_payload', return_value=deepcopy(current or fixture())):
            return a.dispatch_web_inbox(self.target | payload)

    def test_viewed_idle_action_persists_and_handled_does_not_ack_or_change_later(self):
        current = fixture()
        current['agents'][0].update(later=True, unread=True, acknowledged=1)
        self.assertEqual(self.dispatch({'action': 'handle', 'revision': 2, 'item_id': self.item_id}, current)[0], 200)
        shown = a.web_inbox_fleet(deepcopy(current))['agents'][0]
        self.assertTrue(shown['handoff']['items'][0]['handled'])
        self.assertTrue(shown['later'])
        self.assertTrue(shown['unread'])
        self.assertEqual(shown['acknowledged'], 1)
        # A separate browser reads the same metadata even after preview acknowledgement.
        self.assertTrue(a.web_later_fleet(fixture())['agents'][0]['handoff']['items'][0]['handled'])
        self.assertEqual(self.dispatch({'action': 'restore', 'revision': 2, 'item_id': self.item_id})[0], 200)
        self.assertFalse(a.web_inbox_fleet(fixture())['agents'][0]['handoff']['items'][0]['handled'])
        self.assertFalse((a.STATE / 'inbox.sqlite3').exists())

    def test_stale_retry_cannot_clear_new_action_and_new_prompt_invalidates(self):
        handled = {'action': 'handle', 'revision': 2, 'item_id': self.item_id}
        self.assertEqual(self.dispatch(handled)[0], 200)
        new = fixture(revision=3)
        self.assertFalse(a.web_inbox_fleet(deepcopy(new))['agents'][0]['handoff']['items'][0]['handled'])
        self.assertEqual(self.dispatch(handled, new)[0], 409)
        self.assertEqual(self.dispatch(handled | {'revision': 3}, new)[0], 200)
        self.assertEqual(self.dispatch(handled | {'action': 'restore'}, new)[0], 409)
        a.web_inbox_fleet(fixture())  # a delayed older poll cannot delete newer metadata
        self.assertTrue(a.web_inbox_fleet(deepcopy(new))['agents'][0]['handoff']['items'][0]['handled'])
        running = a.web_inbox_fleet(fixture(revision=4, state='RUNNING'))['agents'][0]
        self.assertNotIn('handoff', running)

    def test_labels_survive_revisions_but_are_case_and_process_scoped(self):
        self.assertEqual(self.dispatch({'action': 'label', 'label': 'CI recovery'})[0], 200)
        current = fixture(revision=5)
        current['agents'].extend([current['agents'][0] | {'pane': '%4'},
                                  current['agents'][0] | {'host': 'work'},
                                  current['agents'][0] | {'pid': '999'}])
        shown = a.web_inbox_fleet(current)['agents']
        self.assertEqual(shown[0]['workstream_label'], 'CI recovery')
        self.assertTrue(all('workstream_label' not in row for row in shown[1:]))
        offline = fixture(online=False)
        offline['agents'] = []
        a.web_inbox_fleet(offline)
        self.assertEqual(a.web_inbox_fleet(fixture())['agents'][0]['workstream_label'], 'CI recovery')
        absent = fixture()
        absent['agents'] = []
        a.web_inbox_fleet(absent)
        self.assertNotIn('workstream_label', a.web_inbox_fleet(fixture())['agents'][0])

    def test_offline_and_invalid_requests_fail_safely(self):
        handle = {'action': 'handle', 'revision': 2, 'item_id': self.item_id}
        self.assertEqual(self.dispatch(handle, fixture(online=False))[0], 409)
        for change in ({'revision': True}, {'revision': 2**63}, {'revision': -1},
                       {'item_id': []}, {'item_id': 'x'}, {'action': 'unknown'},
                       {'host': {}}, {'action': 'label', 'label': 'x' * 81},
                       {'action': 'label', 'label': {'nested': 'x'}}):
            with self.subTest(change=change):
                self.assertEqual(self.dispatch(handle | change)[0], 400)
        self.assertEqual(self.dispatch(handle | {'unknown_extra': [1, 2]})[0], 200)

    def test_old_and_malformed_peers_and_item_bounds(self):
        for bad in (None, [], {}, {'revision': 2, 'items': {}},
                    {'revision': True, 'items': []}, {'revision': 3, 'items': []}):
            current = fixture()
            current['agents'][0]['handoff'] = bad
            self.assertNotIn('handoff', a.web_inbox_fleet(current)['agents'][0])
        current = fixture()
        current['agents'][0].pop('handoff')
        self.assertNotIn('handoff', a.web_inbox_fleet(current)['agents'][0])
        current = fixture()
        items = current['agents'][0]['handoff']['items']
        items[0].update(next_step='x' * 100000, evidence='y' * 100000, rule='z' * 100000,
                        handled=True, unknown={'nested': ['a'] * 100})
        items.extend([None, [], {}, items[0]])
        safe = a.web_inbox_fleet(current)['agents'][0]['handoff']['items']
        self.assertEqual(len(safe), 1)
        self.assertEqual([len(safe[0][key]) for key in ('next_step', 'evidence', 'rule')], [240, 360, 80])
        self.assertFalse(safe[0]['handled'])  # peer cannot spoof hub metadata
        self.assertNotIn('unknown', safe[0])

    def test_http_route_shares_metadata_without_touching_pane_state(self):
        handler = object.__new__(a.make_web_handler())
        handler.path = '/api/inbox'
        body = json.dumps(self.target | {'action': 'label', 'label': 'P1 / #42'}).encode()
        handler.headers = {'Content-Length': str(len(body))}
        handler.rfile = io.BytesIO(body)
        sent = []
        handler._send = lambda code, body, mime: sent.append((code, json.loads(body)))
        with patch.object(a, 'fleet_payload', side_effect=lambda **_: fixture()):
            handler.do_POST()
        self.assertEqual(sent, [(200, {'ok': True})])
        self.assertEqual(a.web_later_fleet(fixture())['agents'][0]['workstream_label'], 'P1 / #42')


class DecisionSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        state = patch.object(a, 'STATE', Path(self.temp.name))
        state.start()
        self.addCleanup(state.stop)
        self.con = a.db()
        self.addCleanup(self.con.close)
        a.record(self.con, 'sock', '%3', '123', 'claude', 'IDLE', 'Finished', True)

    def row(self):
        return dict(self.con.execute('SELECT * FROM panes').fetchone())

    def test_full_hook_response_beyond_card_and_sheet_clip(self):
        text = '對,CI 已經恢復,而且是真的跑完整套件後通過的。\n' + 'vitest 完整套件通過。\n' * 500
        text += '還沒收尾的事項\n事故殘留的任務 a1b2c3d4(blocked)需要你在 管理頁 點取消。'
        a.remember_preview(self.con, 'sock', '%3', '123', agent=text, user='recover CI')
        result = a.pane_handoff(self.con, self.row())
        self.assertEqual([item['kind'] for item in result['items']], ['operation'])
        live = {('%3', '123'): {'pane': '%3', 'pid': '123', 'session': 'Atlas', 'window': 'Atlas', 'path': ''}}
        with patch.object(a, 'pane_model', return_value=('', '')), patch.object(a, 'resolve_transcript_path', return_value=''):
            payload = a.agents_payload(self.con, sock='sock', live=live)
        self.assertNotIn('handoff_response', payload['agents'][0])
        self.assertLess(len(json.dumps(payload)), 4000)
        a.acknowledge(self.con, self.row())
        self.assertEqual(len(a.pane_handoff(self.con, self.row())['items']), 1)
        a.record(self.con, 'sock', '%3', '123', 'claude', 'RUNNING', 'New prompt')
        self.assertEqual(a.pane_handoff(self.con, self.row())['items'], [])
        a.record(self.con, 'sock', '%3', '123', 'claude', 'IDLE', 'Finished', True)
        self.assertEqual(a.pane_handoff(self.con, self.row())['items'], [])

    def test_old_full_preview_requires_current_revision_and_bound_matching_source(self):
        text = 'CI restored\n' + 'successful run\n' * 500 + '需要你在 管理頁 點取消。'
        path = Path(self.temp.name) / 'session.jsonl'
        # datetime rounds to microseconds; a raw time.time() value can round
        # below updated, violating the parser's required stopped >= updated.
        stamp = datetime.fromtimestamp(self.row()['updated'] + 0.001, timezone.utc).isoformat()
        self.assertGreaterEqual(datetime.fromisoformat(stamp).timestamp(), self.row()['updated'])
        entries = [{'type': 'user', 'message': {'content': 'recover'}},
                   {'type': 'assistant', 'timestamp': stamp, 'message': {'content': text, 'stop_reason': 'end_turn'}},
                   {'type': 'system', 'timestamp': stamp, 'subtype': 'stop_hook_summary'}]
        path.write_text('\n'.join(json.dumps(entry) for entry in entries) + '\n')
        a.remember_preview(self.con, 'sock', '%3', '123', user='recover', agent=text, transcript_path=str(path))
        with self.con:
            self.con.execute("UPDATE panes SET handoff_response='',handoff_revision=0")
        self.assertEqual(len(a.pane_handoff(self.con, self.row())['items']), 1)
        # A new user message or a different response must not expand a cached clip.
        entries.append({'type': 'user', 'message': {'content': 'new task'}})
        path.write_text('\n'.join(json.dumps(entry) for entry in entries) + '\n')
        result = a.pane_handoff(self.con, self.row())
        self.assertEqual(result['items'], [])
        self.assertTrue(result['truncated'])
        with self.con:
            self.con.execute('UPDATE panes SET preview_revision=0')
        self.assertEqual(a.pane_handoff(self.con, self.row())['source'], 'unavailable')

    def test_bound_new_prompt_suppresses_short_and_clipped_hook_caches(self):
        path = Path(self.temp.name) / 'bound.jsonl'
        for padding in ('', 'successful run\n' * 500):
            with self.subTest(clipped=bool(padding)):
                text = '需要你在 管理頁 點取消。\n' + padding
                entries = [{'type': 'user', 'message': {'content': 'recover CI'}},
                           {'type': 'assistant', 'message': {'content': text}}]
                path.write_text('\n'.join(json.dumps(entry) for entry in entries) + '\n')
                a.remember_preview(self.con, 'sock', '%3', '123', user='recover CI',
                                   agent=text, transcript_path=str(path))
                self.assertEqual(len(a.pane_handoff(self.con, self.row())['items']), 1)
                # No DB revision/hook update yet, but this owned source has moved on.
                entries.append({'type': 'user', 'message': {'content': 'new task'}})
                path.write_text('\n'.join(json.dumps(entry) for entry in entries) + '\n')
                self.assertEqual(a.pane_handoff(self.con, self.row())['items'], [])
                live = {('%3', '123'): {'pane': '%3', 'pid': '123', 'session': 'Atlas', 'window': 'Atlas', 'path': ''}}
                with patch.object(a, 'pane_model', return_value=('', '')):
                    self.assertEqual(a.agents_payload(self.con, sock='sock', live=live)['agents'][0]['handoff']['items'], [])

    def test_bound_new_prompt_suppresses_clipped_legacy_preview(self):
        path = Path(self.temp.name) / 'legacy.jsonl'
        text = '需要你在 管理頁 點取消。\n' + 'successful run\n' * 500
        entries = [{'type': 'user', 'message': {'content': 'recover CI'}},
                   {'type': 'assistant', 'message': {'content': text}},
                   {'type': 'user', 'message': {'content': 'new task'}},
                   {'type': 'assistant', 'message': {'content': 'New result'}}]
        path.write_text('\n'.join(json.dumps(entry) for entry in entries) + '\n')
        a.remember_preview(self.con, 'sock', '%3', '123', user='recover CI',
                           agent=text, transcript_path=str(path))
        with self.con:
            self.con.execute("UPDATE panes SET handoff_response='',handoff_revision=0")
        self.assertTrue(self.row()['preview_agent'].endswith('…'))
        self.assertEqual(a.pane_handoff(self.con, self.row())['items'], [])

    def test_bound_history_can_precede_the_first_cached_user(self):
        path = Path(self.temp.name) / 'history.jsonl'
        text = '需要你在 管理頁 點取消。'
        entries = [{'type': 'user', 'message': {'content': 'earlier task before tracking'}},
                   {'type': 'assistant', 'message': {'content': 'Earlier result'}},
                   {'type': 'user', 'message': {'content': 'recover CI'}},
                   {'type': 'assistant', 'message': {'content': text}}]
        path.write_text('\n'.join(json.dumps(entry) for entry in entries) + '\n')
        a.remember_preview(self.con, 'sock', '%3', '123', user='recover CI',
                           agent=text, transcript_path=str(path))
        self.assertEqual(len(a.pane_handoff(self.con, self.row())['items']), 1)
        entries.append({'type': 'user', 'message': {'content': 'new task'}})
        path.write_text('\n'.join(json.dumps(entry) for entry in entries) + '\n')
        self.assertEqual(a.pane_handoff(self.con, self.row())['items'], [])

    def test_lagging_unavailable_or_unbound_transcript_preserves_fresh_hook(self):
        path = Path(self.temp.name) / 'lagging.jsonl'
        text = '需要你在 管理頁 點取消。'
        entries = [{'type': 'user', 'message': {'content': 'older task'}},
                   {'type': 'assistant', 'message': {'content': 'Older result'}}]
        path.write_text('\n'.join(json.dumps(entry) for entry in entries) + '\n')
        a.remember_preview(self.con, 'sock', '%3', '123', user='current task',
                           agent=text, transcript_path=str(path))
        self.assertEqual(len(a.pane_handoff(self.con, self.row())['items']), 1)
        # Identical wording from an older task is not proof that this fresh
        # hook response has been superseded while its transcript is flushing.
        entries[1]['message']['content'] = text
        entries.append({'type': 'user', 'message': {'content': 'current task'}})
        path.write_text('\n'.join(json.dumps(entry) for entry in entries) + '\n')
        self.assertEqual(len(a.pane_handoff(self.con, self.row())['items']), 1)
        path.unlink()
        self.assertEqual(len(a.pane_handoff(self.con, self.row())['items']), 1)
        with self.con:
            self.con.execute("UPDATE panes SET transcript_path=''")
        with patch.object(a, 'resolve_transcript_path', return_value=str(path)) as discovery:
            self.assertEqual(len(a.pane_handoff(self.con, self.row())['items']), 1)
        discovery.assert_not_called()

    def test_bound_codex_and_cursor_new_user_supersedes_cache(self):
        path = Path(self.temp.name) / 'bound.jsonl'
        text = '需要你在 管理頁 點取消。'
        sources = {
            'codex': [{'type': 'event_msg', 'payload': {'type': 'user_message', 'message': 'recover'}},
                      {'type': 'event_msg', 'payload': {'type': 'agent_message', 'message': text}},
                      {'type': 'event_msg', 'payload': {'type': 'user_message', 'message': 'next task'}}],
            'cursor': [{'role': 'user', 'message': 'recover'},
                       {'role': 'assistant', 'message': text},
                       {'role': 'user', 'message': '<user_query>next task</user_query>'}],
        }
        for agent, entries in sources.items():
            with self.subTest(agent=agent):
                a.record(self.con, 'sock', '%3', '123', agent, 'IDLE', 'Finished', True)
                a.remember_preview(self.con, 'sock', '%3', '123', user='recover',
                                   agent=text, transcript_path=str(path))
                path.write_text('\n'.join(json.dumps(entry) for entry in entries[:-1]) + '\n')
                self.assertEqual(len(a.pane_handoff(self.con, self.row())['items']), 1)
                path.write_text('\n'.join(json.dumps(entry) for entry in entries) + '\n')
                self.assertEqual(a.pane_handoff(self.con, self.row())['items'], [])

    def test_bounded_transcript_malformed_rows(self):
        path = Path(self.temp.name) / 'malformed.jsonl'
        path.write_text('[]\nnull\n' + '[' * 2000 + '0' + ']' * 2000 + '\n' +
                        json.dumps({'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': {}}]}}) + '\n')
        self.assertEqual(a._bounded_handoff_transcript(path, 'claude'), ([], ''))
        path.write_text('x' * (a.TRANSCRIPT_TAIL_BYTES * 2) + '\n' +
                        json.dumps({'type': 'assistant', 'message': {'content': '需要你同意'}}) + '\n')
        self.assertEqual(a._bounded_handoff_transcript(path, 'claude'), ([], '需要你同意'))


if __name__ == '__main__':
    unittest.main()
