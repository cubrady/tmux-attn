"""Answering Codex/Claude questions from the web: transcript parsing, validation and the TUI key drivers.

The fake TUIs below mirror screens captured from Codex 0.162.1 and Claude Code 2.1.301 in an isolated tmux.
"""
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

spec = importlib.util.spec_from_file_location('attention', Path(__file__).with_name('attention.py'))
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)


def jsonl(path, rows):
    Path(path).write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows))


def codex_call(call_id, questions):
    return {'type': 'response_item', 'payload': {'type': 'function_call', 'name': 'request_user_input_async',
                                                 'call_id': call_id, 'arguments': json.dumps({'questions': questions})}}


def codex_reply(*question_ids):
    replies = json.dumps([{'answer': 'x', 'question': 'q', 'questionItemId': qid} for qid in question_ids])
    text = f'<send_user_message_question_reply>\n{replies}\n</send_user_message_question_reply>'
    return {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
                                                 'content': [{'type': 'input_text', 'text': text}]}}


def event(kind):
    return {'type': 'event_msg', 'payload': {'type': kind}}


def claude_ask(tool_id, questions):
    return {'type': 'assistant', 'message': {'content': [
        {'type': 'tool_use', 'id': tool_id, 'name': 'AskUserQuestion', 'input': {'questions': questions}}]}}


def claude_result(tool_id):
    return {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': tool_id, 'content': 'ok'}]}}


def claude_question(text, labels, multi=False, previews=False, header='H'):
    return {'question': text, 'header': header, 'multiSelect': multi,
            'options': [{'label': label, 'description': label + ' desc', **({'preview': 'x'} if previews else {})}
                        for label in labels]}


class TranscriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 't.jsonl'

    def tearDown(self):
        self.tmp.cleanup()

    def test_codex_pending_questions_follow_answers_and_turn_end(self):
        first = [{'title': 'Which DB?', 'options': ['Postgres', 'SQLite']}, {'title': 'Any naming rule?'}]
        jsonl(self.path, [event('task_started'), codex_call('call_old', [{'title': 'Old?'}]), event('task_complete'),
                          event('task_started'), codex_call('call_1', first), codex_call('call_2', [{'title': 'Deploy?', 'options': ['Yes']}]),
                          codex_reply(a.codex_question_id('call_2', 0))])
        pending = a.codex_pending_questions(self.path)
        self.assertEqual([q['title'] for q in pending], ['Which DB?', 'Any naming rule?'])
        self.assertEqual(pending[0]['id'], '["request_user_input_async","call_1",0]')
        self.assertEqual(pending[0]['group'], pending[0]['id'])
        self.assertEqual([o['label'] for o in pending[0]['options']], ['Postgres', 'SQLite'])
        self.assertEqual(pending[1]['options'], [])
        self.assertTrue(all(q['free_text'] and not q['multi'] for q in pending))
        # Codex drops unanswered async questions when the turn ends.
        with self.path.open('a') as handle:
            handle.write(json.dumps(event('turn_aborted')) + '\n')
        self.assertEqual(a.codex_pending_questions(self.path), [])

    def test_codex_queued_answers_hide_questions_until_delivered(self):
        jsonl(self.path, [codex_call('c', [{'title': 'Question A: which database?'}, {'title': 'Question B: naming?'}])])
        pending = a.codex_pending_questions(self.path)
        screen = '  ↳ > Question A: which database?\n\n    Postgres\n• Queued follow-up inputs\n  ? 1 question\n    ⇧← to answer\n'
        self.assertEqual([q['title'] for q in a.codex_visible_questions(pending, screen)], ['Question B: naming?'])
        narrow = '  ↳ > Question A: whi…\n\n    Postgres\n'
        self.assertEqual([q['title'] for q in a.codex_visible_questions(pending, narrow)], ['Question B: naming?'])

    def test_codex_blank_options_keep_their_digit(self):
        jsonl(self.path, [codex_call('c', [{'title': 'Pick?', 'options': ['A', '', 'C']}])])
        self.assertEqual([o['label'] for o in a.codex_pending_questions(self.path)[0]['options']], ['A', '', 'C'])

    def test_keys_grow_past_shared_prefixes_and_refuse_identical_titles(self):
        api, worker, same = ('Which database should we use for the API?', 'Which database should we use for the worker?',
                             'Which database should we use for the API?')
        keys = [q['_key'] for q in a.keyed_questions([{'_title': api}, {'_title': worker}, {'_title': 'Deploy?'}])]
        self.assertEqual(keys, ['WhichdatabaseshouldweusefortheA', 'Whichdatabaseshouldweuseforthew', 'Deploy?'])
        self.assertEqual([q['_key'] for q in a.keyed_questions([{'_title': api}, {'_title': same}])], ['', ''])

    def test_claude_pending_questions_group_one_call_and_drop_answered(self):
        jsonl(self.path, [
            claude_ask('toolu_old', [claude_question('Old?', ['a', 'b'])]), claude_result('toolu_old'),
            claude_ask('toolu_new', [claude_question('Which DB?', ['Postgres', 'SQLite']),
                                     claude_question('Features?', ['Logs', 'Metrics'], multi=True),
                                     claude_question('Layout?', ['Side', 'Top'], previews=True)]),
        ])
        pending = a.claude_pending_questions(self.path)
        self.assertEqual([q['id'] for q in pending], ['toolu_new:0', 'toolu_new:1', 'toolu_new:2'])
        self.assertEqual({q['group'] for q in pending}, {'toolu_new'})
        self.assertEqual([(q['multi'], q['free_text']) for q in pending], [(False, True), (True, False), (False, False)])
        self.assertEqual(pending[0]['options'][1], {'label': 'SQLite', 'description': 'SQLite desc'})
        self.assertNotIn('_title', a.public_question(pending[0]))
        with self.path.open('a') as handle:
            handle.write(json.dumps(claude_result('toolu_new')) + '\n')
        self.assertEqual(a.claude_pending_questions(self.path), [])


class NormalizeAnswerTests(unittest.TestCase):
    single = {'id': 'q', 'options': [{'label': 'A'}, {'label': 'B'}], 'multi': False, 'free_text': True}
    multi = {'id': 'm', 'options': [{'label': 'A'}, {'label': 'B'}, {'label': 'C'}], 'multi': True, 'free_text': False}

    def test_accepts_one_kind_of_answer(self):
        self.assertEqual(a.normalize_answer(self.single, {'id': 'q', 'option': 1}), ({'option': 1}, None))
        self.assertEqual(a.normalize_answer(self.single, {'id': 'q', 'text': ' two\nlines  here '}), ({'text': 'two lines here'}, None))
        self.assertEqual(a.normalize_answer(self.multi, {'id': 'm', 'options': [2, 0]}), ({'options': [0, 2]}, None))
        self.assertEqual(a.normalize_answer(self.single, {'id': 'q', 'text': '👩‍💻 ok'}), ({'text': '👩‍💻 ok'}, None))

    def test_rejects_mismatched_or_malformed_answers(self):
        bad = [
            (self.single, {'id': 'other', 'option': 0}), (self.single, {'id': 'q'}),
            (self.single, {'id': 'q', 'option': 0, 'text': 'x'}), (self.single, {'id': 'q', 'option': 2}),
            (self.single, {'id': 'q', 'option': True}), (self.single, {'id': 'q', 'options': [0]}),
            (self.single, {'id': 'q', 'text': '   '}), (self.single, {'id': 'q', 'text': 'x' * 2001}),
            (self.multi, {'id': 'm', 'option': 0}), (self.multi, {'id': 'm', 'options': []}),
            (self.multi, {'id': 'm', 'options': [0, 0]}), (self.multi, {'id': 'm', 'options': [3]}),
            (self.multi, {'id': 'm', 'text': 'free'}),
            # ESC ends a bracketed paste early; what follows would reach the TUI as keys.
            (self.single, {'id': 'q', 'text': 'a\x1b[201~\x1b'}), (self.single, {'id': 'q', 'text': 'stop\x03'}),
            (self.single, {'id': 'q', 'text': 'csi\x9b2~'}), (self.single, {'id': 'q', 'text': 'half \ud800'}),
            (self.single, {'id': 'q', 'text': '** `'}),
        ]
        for question, answer in bad:
            value, error = a.normalize_answer(question, answer)
            self.assertIsNone(value, answer)
            self.assertTrue(error, answer)


class FakeDriver(a.PaneKeys):
    """Records keys; subclasses render the TUI state as text like capture-pane would."""

    def __init__(self):
        super().__init__('%1')
        self.sent = []

    def keys(self, *keys):
        for key in keys:
            self.sent.append(key)
            self.press(key)

    def paste(self, text):
        self.sent.append(('paste', text))
        self.pasted(text)


class FakeCodex(FakeDriver):
    def __init__(self, questions, queued_messages=False, narrow=False, frozen=False):
        super().__init__()
        self.pending = [dict(q) for q in questions]
        self.expanded, self.index, self.answers, self.draft, self.flash = False, 0, [], '', ''
        self.queued_messages, self.narrow, self.popped, self.frozen = queued_messages, narrow, False, frozen

    def screen(self):
        lines = ['• Working (5s • esc to interrupt)', '']
        for title, answer in self.answers:
            lines += [f'  ↳ > {title}', '', f'    {answer}']
        if self.pending and not self.expanded:
            count = len(self.pending)
            lines += ['• Queued follow-up inputs', f'  ? {count} question{"s" if count > 1 else ""}', '    ⇧← to answer']
        if self.expanded:
            q = self.pending[self.index]
            lines += ['• Queued follow-up inputs', '', '']
            if len(self.pending) > 1:
                lines.append(f'  {self.index + 1} of {len(self.pending)}')
            lines += [f"  {q['title']}", '']
            options = q.get('options') or []
            for i, label in enumerate(options + ['Other'] if options else []):
                text = self.draft if label == 'Other' and self.draft else label
                lines.append(f"  {'›' if i == q.get('selected', 0) else ' '} {i + 1}. {text}")
            if not options:
                lines.append(f'  {self.draft or "Type your answer"}')
            lines += ['', f'  {self.flash or "enter submit   ⌃] skip   ⇧→ main prompt"}']
        else:
            lines += ['', '› Ask Codex to do anything']
        return '\n'.join(lines) + '\n'

    def submit(self, text):
        q = self.pending.pop(self.index)
        self.answers.append((q['title'], text))
        self.draft = ''
        self.index = min(self.index, len(self.pending) - 1) if self.pending else 0
        self.expanded = bool(self.pending)

    def press(self, key):
        if key == 'Escape':
            raise AssertionError('Esc interrupts the Codex turn')
        if not self.expanded:
            if key == 'S-Left' and self.pending:
                self.expanded = True
            return
        q = self.pending[self.index]
        options = q.get('options') or []
        if self.frozen and key in ('S-Left', 'S-Right'):
            return
        if key == 'S-Left':
            if self.index + 1 < len(self.pending):
                self.index += 1
            elif self.queued_messages:
                self.popped, self.expanded = True, False
        elif key == 'S-Right':
            self.flash = ''
            if self.index:
                self.index -= 1
            else:
                self.expanded = False
        elif key.isdigit():
            choice = int(key) - 1
            if choice < len(options):
                q['selected'] = choice
                if self.narrow:
                    self.flash = 'Expand terminal to read the entire option'
                else:
                    self.submit(options[choice])
            elif choice == len(options):
                q['selected'] = choice
        elif key == 'Enter':
            self.submit(self.draft or options[q.get('selected', 0)])

    def pasted(self, text):
        if self.expanded:
            q = self.pending[self.index]
            q['selected'] = len(q.get('options') or [])
            self.draft += text


class CodexDriverTests(unittest.TestCase):
    QUESTIONS = [{'title': 'Question A: which database?', 'options': ['Postgres', 'SQLite', 'MySQL']},
                 {'title': 'Question B: naming preference?'},
                 {'title': 'Question C: deploy now?', 'options': ['Yes', 'No']}]

    def setUp(self):
        patcher = patch.multiple(a, QUESTION_WAIT_SECONDS=0.05, QUESTION_POLL_SECONDS=0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def question(self, index, questions=QUESTIONS):
        return a.keyed_questions([{'_title': q['title']} for q in questions])[index]

    def test_option_answer_navigates_to_question_and_collapses(self):
        tui = FakeCodex(self.QUESTIONS, queued_messages=True)
        self.assertIsNone(a.answer_codex_question(tui, self.question(2), option=1))
        self.assertEqual(tui.answers, [('Question C: deploy now?', 'No')])
        self.assertEqual(tui.sent[:3], ['S-Left', 'S-Left', 'S-Left'])
        self.assertFalse(tui.expanded)
        self.assertFalse(tui.popped)

    def test_text_answer_is_pasted_then_submitted(self):
        tui = FakeCodex(self.QUESTIONS)
        tui.expanded, tui.index = True, 2  # Left open on a later question: walk back first.
        self.assertIsNone(a.answer_codex_question(tui, self.question(0), text='CockroachDB'))
        self.assertEqual(tui.answers, [('Question A: which database?', 'CockroachDB')])
        self.assertIn(('paste', 'CockroachDB'), tui.sent)
        self.assertFalse(tui.expanded)

    def test_missing_question_never_steps_past_the_last_one(self):
        tui = FakeCodex(self.QUESTIONS, queued_messages=True)
        error = a.answer_codex_question(tui, a.keyed_questions([{'_title': 'Question Z: gone?'}])[0], option=0)
        self.assertIn('找不到這個問題', error)
        self.assertFalse(tui.popped)
        self.assertEqual(tui.answers, [])
        self.assertFalse(tui.expanded)

    def test_refuses_without_question_footer_or_with_clipped_options(self):
        tui = FakeCodex([])
        self.assertIn('沒有待回答的問題', a.answer_codex_question(tui, self.question(0), option=0))
        self.assertEqual(tui.sent, [])
        tui = FakeCodex(self.QUESTIONS[:1], narrow=True)
        self.assertIn('終端機太小', a.answer_codex_question(tui, self.question(0), option=2))
        self.assertEqual(tui.answers, [])
        self.assertFalse(tui.expanded)

    def test_unseen_paste_is_not_submitted(self):
        # "SQLite" is already on screen as option 2: only a new occurrence proves the paste landed.
        for text in ('mine', 'SQLite', 'Question A: which database'):
            tui = FakeCodex(self.QUESTIONS[:1])
            tui.pasted = lambda text: None
            self.assertIn('沒有顯示貼上的回答', a.answer_codex_question(tui, self.question(0), text=text))
            self.assertNotIn('Enter', tui.sent)
            self.assertEqual(tui.answers, [])
        tui = FakeCodex(self.QUESTIONS[:1])
        self.assertIsNone(a.answer_codex_question(tui, self.question(0), text='SQLite'))
        self.assertEqual(tui.sent[-2:], [('paste', 'SQLite'), 'Enter'])

    def test_look_alike_titles_reach_the_right_question(self):
        questions = [{'title': 'Which database should we use for the API?', 'options': ['Postgres', 'SQLite']},
                     {'title': 'Which database should we use for the worker?', 'options': ['Redis', 'SQLite']}]
        tui = FakeCodex(questions)
        self.assertIsNone(a.answer_codex_question(tui, self.question(1, questions), option=0))
        self.assertEqual(tui.answers, [('Which database should we use for the worker?', 'Redis')])

    def test_unresponsive_panel_is_not_pressed_again(self):
        tui = FakeCodex(self.QUESTIONS, queued_messages=True, frozen=True)
        self.assertIn('沒有回應', a.answer_codex_question(tui, self.question(2), option=0))
        self.assertEqual(tui.sent.count('S-Left'), 2)  # open, then one move that never rendered
        self.assertFalse(tui.popped)
        self.assertEqual(tui.answers, [])


class FakeClaude(FakeDriver):
    def __init__(self, questions):
        super().__init__()
        self.questions = questions
        self.tab, self.mode, self.cursor, self.typing, self.text = 0, 'question', 0, False, ''
        self.checked = [set() for _ in questions]
        self.picked = [None] * len(questions)
        self.submitted = self.cancelled = None

    def tabs(self):
        if len(self.questions) == 1 and not self.questions[0]['multiSelect']:
            return f" {'☒' if self.picked[0] else '☐'} {self.questions[0]['header']} "
        marks = '  '.join(f"{'☒' if self.picked[i] else '☐'} {q['header']}" for i, q in enumerate(self.questions))
        return f'←  {marks}  ✔ Submit  →'

    def screen(self):
        lines = ['❯ please ask', '⏺ Earlier ☐ todo item', '─' * 40]
        if self.mode == 'closed':
            return '\n'.join(lines + ['❯ ', '─' * 40]) + '\n'
        lines.append(self.tabs())
        if self.mode == 'review':
            lines.append('Review your answers')
            for q, answer in zip(self.questions, self.picked):
                lines += [f" ● {q['question']}", f'   → {answer}']
            return '\n'.join(lines + ['Ready to submit your answers?', '❯ 1. Submit answers', '  2. Cancel']) + '\n'
        q = self.questions[self.tab]
        lines.append(q['question'])
        options = q['options']
        for i, option in enumerate(options):
            box = f"[{'✔' if i in self.checked[self.tab] else ' '}] " if q['multiSelect'] else ''
            lines += [f"{'❯' if i == self.cursor else ' '} {i + 1}. {box}{option['label']}", f"     {option['description']}"]
        if not any(o.get('preview') for o in options):
            other = self.text if self.typing and self.text else 'Type something.'
            lines.append(f"{'❯' if self.cursor == len(options) else ' '} {len(options) + 1}. {other}")
        footer = 'Enter to select · ↑/↓ to navigate · ' + ('ctrl+g to edit in nano · ' if self.typing else '') + 'Esc to cancel'
        return '\n'.join(lines + ['─' * 40, '  Chat about this', footer]) + '\n'

    def advance(self, answer):
        self.picked[self.tab] = answer
        self.typing, self.text, self.cursor = False, '', 0
        if len(self.questions) == 1 and not self.questions[0]['multiSelect']:
            self.mode, self.submitted = 'closed', list(self.picked)
            return
        self.tab += 1
        if self.tab == len(self.questions):
            self.mode = 'review'

    def press(self, key):
        if key == 'Escape':
            raise AssertionError('Esc rejects the whole question call')
        if self.mode == 'review':
            if key == '1':
                self.mode, self.submitted = 'closed', list(self.picked)
            elif key == '2':
                self.mode, self.cancelled = 'closed', True
            elif key == 'Left':
                self.mode, self.tab = 'question', len(self.questions) - 1
            return
        if self.mode != 'question':
            return
        q = self.questions[self.tab]
        options = q['options']
        previews = any(o.get('preview') for o in options)
        if self.typing and key not in ('Enter',):
            self.text += key
        elif key.isdigit():
            choice = int(key) - 1
            if previews:
                self.cursor = choice
            elif q['multiSelect']:
                self.checked[self.tab] ^= {choice}
            elif choice < len(options):
                self.advance(options[choice]['label'])
            elif choice == len(options):
                self.cursor, self.typing = choice, True
        elif key == 'Enter':
            if self.typing:
                self.advance(self.text)
            elif previews:
                self.advance(options[self.cursor]['label'])
        elif key == 'Tab' and q['multiSelect']:
            self.advance(', '.join(options[i]['label'] for i in sorted(self.checked[self.tab])))
        elif key == 'Left' and self.tab:
            self.tab -= 1

    def pasted(self, text):
        if self.typing:
            self.text += text


def claude_entries(questions):
    return a.keyed_questions([{'_title': q['question'], 'options': [{'label': o['label']} for o in q['options']],
                               'multi': q['multiSelect'], '_previews': any(o.get('preview') for o in q['options'])}
                              for q in questions])


class ClaudeDriverTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.multiple(a, QUESTION_WAIT_SECONDS=0.05, QUESTION_POLL_SECONDS=0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_answers(self, questions, answers, tui=None):
        tui = tui or FakeClaude(questions)
        return a.answer_claude_questions(tui, claude_entries(questions), answers), tui

    def test_single_select_digit_submits_without_review(self):
        questions = [claude_question('要選哪個部署策略？Which deploy strategy?', ['藍綠部署 (Recommended)', '滾動更新'])]
        error, tui = self.run_answers(questions, [{'option': 1}])
        self.assertIsNone(error)
        self.assertEqual((tui.submitted, tui.sent), (['滾動更新'], ['2']))

    def test_several_questions_with_text_and_multiselect_then_review_submit(self):
        questions = [claude_question('Which database?', ['Postgres', 'SQLite'], header='DB'),
                     claude_question('Service name?', ['svc-a', 'svc-b'], header='Name'),
                     claude_question('Pick toppings', ['Cheese', 'Ham', 'Olive'], multi=True, header='Top')]
        tui = FakeClaude(questions)
        tui.tab = 2  # Opened on a later tab: the driver walks back with Left.
        error, tui = self.run_answers(questions, [{'option': 1}, {'text': 'my 服務'}, {'options': [0, 2]}], tui)
        self.assertIsNone(error)
        self.assertEqual(tui.submitted, ['SQLite', 'my 服務', 'Cheese, Olive'])
        self.assertIsNone(tui.cancelled)
        self.assertEqual(tui.sent[-1], '1')

    def test_multiselect_only_toggles_what_differs(self):
        questions = [claude_question('Features?', ['Logs', 'Metrics', 'Traces'], multi=True)]
        tui = FakeClaude(questions)
        tui.checked[0] = {0, 1}
        error, tui = self.run_answers(questions, [{'options': [0, 2]}], tui)
        self.assertIsNone(error)
        self.assertEqual(tui.sent, ['2', '3', 'Tab', '1'])
        self.assertEqual(tui.submitted, ['Logs, Traces'])

    def test_preview_layout_moves_cursor_then_enter(self):
        questions = [claude_question('Layout?', ['Sidebar', 'Topbar'], previews=True)]
        error, tui = self.run_answers(questions, [{'option': 1}])
        self.assertIsNone(error)
        self.assertEqual((tui.sent, tui.submitted), (['2', 'Enter'], ['Topbar']))

    def test_refuses_other_dialogs_and_wrong_review(self):
        questions = [claude_question('Which database?', ['Postgres', 'SQLite'])]
        tui = FakeClaude([claude_question('Something else?', ['a', 'b'])])
        error, tui = self.run_answers(questions, [{'option': 0}], tui)
        self.assertIn('不是這組問題', error)
        self.assertEqual(tui.sent, [])
        tui = FakeClaude(questions)
        tui.mode = 'closed'
        self.assertIn('沒有開啟中的問題', self.run_answers(questions, [{'option': 0}], tui)[0])
        # A review page that does not show the chosen answers must never be submitted.
        two = questions + [claude_question('Cache?', ['Redis', 'None'])]
        tui = FakeClaude(two)
        tui.advance = lambda answer, original=tui.advance: original('tampered')
        error, tui = self.run_answers(two, [{'option': 0}, {'option': 1}], tui)
        self.assertIn('不符', error)
        self.assertNotIn('1', tui.sent[2:])
        self.assertIsNone(tui.submitted)

    def test_review_must_show_the_whole_answer(self):
        questions = [claude_question('Which database?', ['Postgres', 'SQLite']), claude_question('Service name?', ['a', 'b'])]
        tui = FakeClaude(questions)
        tui.advance = lambda answer, original=tui.advance: original(answer + ' and more')
        error, tui = self.run_answers(questions, [{'option': 0}, {'text': 'a long service name past the key'}], tui)
        self.assertIn('不符', error)
        self.assertIsNone(tui.submitted)
        self.assertNotIn('2', tui.sent[1:])

    def test_look_alike_question_that_did_not_advance_is_not_answered_twice(self):
        questions = [claude_question('Which database should we use for the API?', ['Postgres', 'SQLite']),
                     claude_question('Which database should we use for the worker?', ['Redis', 'SQLite'])]
        tui = FakeClaude(questions)
        ignored = []
        tui.press = lambda key, original=tui.press: ignored.append(key) if not ignored else original(key)
        error, tui = self.run_answers(questions, [{'option': 0}, {'option': 1}], tui)
        self.assertIn('沒有切到預期的問題', error)
        self.assertEqual(tui.sent, ['1'])
        self.assertEqual(tui.picked, [None, None])

    def test_refuses_more_options_than_single_digits(self):
        questions = [claude_question('Pick?', [str(i) for i in range(9)])]
        self.assertIn('選項太多', self.run_answers(questions, [{'option': 0}])[0])

    def test_text_needs_focused_input_before_paste(self):
        questions = [claude_question('Service name?', ['svc-a', 'svc-b'])]
        tui = FakeClaude(questions)
        tui.press = lambda key, original=tui.press: None if key == '3' else original(key)
        error, tui = self.run_answers(questions, [{'text': 'mine'}], tui)
        self.assertIn('自訂回答欄位', error)
        self.assertNotIn(('paste', 'mine'), tui.sent)


class AnswerDispatchTests(unittest.TestCase):
    def test_validates_payload_before_anything_else(self):
        for payload in ([], {'pane': '%1', 'pid': '1', 'answers': [{}]}, {'pane': '%1', 'pid': '1', 'group': 'g'},
                        {'pane': '%1', 'pid': '1', 'group': 'g', 'answers': [{}] * 5}):
            with patch.object(a, 'answer_pane_questions') as answer:
                code, body = a.dispatch_answer(payload)
            self.assertEqual(code, 400, payload)
            answer.assert_not_called()

    def test_local_answer_and_error_mapping(self):
        payload = {'pane': '%1', 'pid': '123', 'group': 'g', 'answers': [{'id': 'g', 'option': 0}]}
        with patch.object(a, 'local_host_name', return_value='home'), \
             patch.object(a, 'answer_pane_questions', return_value=None) as answer:
            self.assertEqual(a.dispatch_answer(payload), (200, {'ok': True, 'pane': '%1', 'pid': '123', 'host': 'home'}))
            answer.assert_called_once_with('%1', '123', 'g', [{'id': 'g', 'option': 0}])
        with patch.object(a, 'local_host_name', return_value='home'), \
             patch.object(a, 'answer_pane_questions', return_value='Codex 沒有接受這個回答'):
            self.assertEqual(a.dispatch_answer(payload), (409, {'ok': False, 'error': 'Codex 沒有接受這個回答'}))

    def test_forwards_to_peer_and_reports_outdated_peer(self):
        payload = {'host': 'lab', 'pane': '%9', 'pid': '9', 'group': 'g', 'answers': [{'id': 'g', 'text': 'hi'}]}

        class FakeResp:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self): return json.dumps({'ok': True}).encode()

        with patch.object(a, 'local_host_name', return_value='home'), \
             patch.object(a, 'load_peers', return_value=[{'name': 'lab', 'url': 'http://lab:7788'}]), \
             patch.object(a, 'urlopen', return_value=FakeResp()) as urlopen:
            self.assertEqual(a.dispatch_answer(payload), (200, {'ok': True}))
            request = urlopen.call_args.args[0]
            self.assertEqual(request.full_url, 'http://lab:7788/api/answer')
            self.assertEqual(json.loads(request.data), {'pane': '%9', 'pid': '9', 'group': 'g',
                                                        'answers': [{'id': 'g', 'text': 'hi'}], '_local_only': True})
        missing = HTTPError('http://lab:7788/api/answer', 404, 'nf', {}, io.BytesIO(b''))
        with patch.object(a, 'local_host_name', return_value='home'), \
             patch.object(a, 'load_peers', return_value=[{'name': 'lab', 'url': 'http://lab:7788'}]), \
             patch.object(a, 'urlopen', side_effect=missing):
            self.assertEqual(a.dispatch_answer(payload), (502, {'ok': False, 'error': a.PEER_OUTDATED}))

    def test_answer_pane_questions_matches_group_and_validates(self):
        live = {'pane': '%1', 'pid': '123', 'session': 's', 'window': 'w', 'window_id': '@1', 'path': '/p'}
        row = {'socket': '/s', 'pane': '%1', 'pid': '123', 'agent': 'codex', 'state': 'WAIT'}
        question = {'id': 'g', 'group': 'g', 'options': [{'label': 'Yes'}], 'multi': False, 'free_text': True,
                    '_title': 'Deploy?', '_key': 'Deploy?'}

        class Con:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def close(self): pass
            def execute(self, *args): return self
            def fetchone(self): return row

        with patch.object(a, 'live_pane_meta', return_value=live), patch.object(a, 'db', return_value=Con()), \
             patch.object(a, 'pane_questions', return_value=[question]), \
             patch.object(a, 'tmux', return_value='123'), \
             patch.object(a, 'answer_codex_question', return_value=None) as codex:
            self.assertIn('找不到', a.answer_pane_questions('%1', '123', 'other', [{'id': 'other', 'option': 0}], sock='/s'))
            self.assertEqual(a.answer_pane_questions('%1', '123', 'g', [{'id': 'g', 'option': 3}], sock='/s'), 'invalid option')
            codex.assert_not_called()
            self.assertIsNone(a.answer_pane_questions('%1', '123', 'g', [{'id': 'g', 'option': 0}], sock='/s'))
            self.assertEqual(codex.call_args.kwargs, {'option': 0, 'text': None})
        with patch.object(a, 'live_pane_meta', return_value=live), patch.object(a, 'db', return_value=Con()), \
             patch.object(a, 'pane_questions', return_value=[question]), patch.object(a, 'tmux', return_value='999'), \
             patch.object(a, 'answer_codex_question') as codex:
            self.assertEqual(a.answer_pane_questions('%1', '123', 'g', [{'id': 'g', 'option': 0}], sock='/s'), 'pane pid changed')
            codex.assert_not_called()
        with patch.object(a, 'live_pane_meta', return_value=live), patch.object(a, 'db', return_value=Con()), \
             patch.object(a, 'pane_questions', return_value=[question | {'_key': ''}]), patch.object(a, 'tmux', return_value='123'), \
             patch.object(a, 'answer_codex_question') as codex:
            self.assertIn('終端機回答', a.answer_pane_questions('%1', '123', 'g', [{'id': 'g', 'option': 0}], sock='/s'))
            codex.assert_not_called()

    def test_replies_wait_for_an_answer_in_progress(self):
        held = []
        with patch.object(a, 'local_host_name', return_value='home'), \
             patch.object(a, 'send_to_pane', side_effect=lambda *args, **kwargs: held.append(a.PANE_INPUT_LOCK.locked())):
            a.dispatch_send({'pane': '%1', 'pid': '123', 'text': 'hello'})
        self.assertEqual(held, [True])
        self.assertFalse(a.PANE_INPUT_LOCK.locked())


if __name__ == '__main__':
    unittest.main()
