"""One attention contract for tmux status, API clients, and dashboard groups."""
import importlib.util
import json
from collections import Counter
from pathlib import Path
import unittest
from unittest.mock import patch


ROOT = Path(__file__).parent
CASES = json.loads((ROOT / 'tests/fixtures/attention-categories.json').read_text())
spec = importlib.util.spec_from_file_location('attention_category_tests', ROOT / 'attention.py')
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)


def fixture_row(case, index):
    return dict(pane=f'%{index + 1}', pid=str(index + 100), session='fixtures',
                window=case['id'], window_id=f'@{index + 1}', path='/fixtures',
                updated=index + 1, reason='Finished', agent='codex') | case['row']


class AttentionCategoryTests(unittest.TestCase):
    def test_shared_cases_agree_for_category_emoji_and_window_badge(self):
        for index, case in enumerate(CASES):
            with self.subTest(case=case['id']):
                row = fixture_row(case, index)
                self.assertEqual(a.attention_category(row), case['category'])
                self.assertEqual(a.agent_emoji(row), case['emoji'])
                badge = '' if case['category'] == 'shell' else case['emoji']
                self.assertEqual(a.window_badge([row]), badge)

    def test_status_groups_partition_tracked_categories_and_counts(self):
        rows = [fixture_row(case, index) for index, case in enumerate(CASES)]
        groups = a.status_groups(rows)
        expected_counts = Counter(case['category'] for case in CASES)
        self.assertEqual(set(groups), {'wait', 'review', 'running', 'idle'})
        for category, group in groups.items():
            with self.subTest(category=category):
                expected_panes = {rows[index]['pane'] for index, case in enumerate(CASES)
                                  if case['category'] == category}
                self.assertEqual({row['pane'] for row in group}, expected_panes)
                self.assertEqual(len(group), expected_counts[category])
        grouped = [row['pane'] for group in groups.values() for row in group]
        self.assertEqual(len(grouped), len(set(grouped)))
        self.assertEqual(len(grouped), len(CASES) - expected_counts['shell'])
        expected_text = ' · '.join(f'{emoji} {expected_counts[category]}'
                                   for category, emoji in a.STATUS_GROUPS)
        self.assertEqual(a.status_text(groups), expected_text)

    def test_mixed_window_badge_follows_attention_priority_and_ignores_shell(self):
        rows = {case['id']: fixture_row(case, index) for index, case in enumerate(CASES)}
        self.assertEqual(a.window_badge([]), '')
        self.assertEqual(a.window_badge([rows['untracked-wait'], rows['shell']]), '')
        self.assertEqual(a.window_badge([rows['idle-reviewed-completion'],
                                       rows['running-lingering-unread']]), '🏃')
        self.assertEqual(a.window_badge([rows['running-lingering-unread'],
                                       rows['idle-snoozed-unread']]), '👀')
        self.assertEqual(a.window_badge([rows['idle-unread-completion'],
                                       rows['wait-acknowledged']]), '🙋')

    def test_sorting_uses_the_same_category_order(self):
        rows = [fixture_row(case, index) for index, case in enumerate(CASES)]
        expected = {row['pane']: case['category'] for row, case in zip(rows, CASES)}
        ranks = {'wait': 0, 'review': 1, 'running': 2, 'idle': 3, 'shell': 4}
        ordered = sorted(reversed(rows), key=a.agent_sort_key)
        categories = [expected[row['pane']] for row in ordered]
        self.assertEqual(categories, sorted(categories, key=ranks.get))
        self.assertEqual(a.grouped_rows(list(reversed(rows))), ordered)

    def test_inbox_rows_follow_the_same_partition_and_badges(self):
        cases = [case for case in CASES if 'unread' in case['row']]
        rows = [fixture_row(case, index) for index, case in enumerate(cases)]
        expected = {(row['pane'], row['pid']): case for row, case in zip(rows, cases)}
        identities = Counter()
        category = None
        for identity, text in a.inbox_lines(rows):
            if identity is None:
                category = next(key for key, emoji in a.STATUS_GROUPS
                                if text.startswith(f'── {emoji} '))
                continue
            case = expected[identity]
            self.assertEqual(category, case['category'])
            if identities[identity] == 0:
                self.assertTrue(text.startswith(case['emoji'] + ' · '))
            identities[identity] += 1
        self.assertEqual(dict(identities), {identity: 3 for identity, case in expected.items()
                                           if case['category'] != 'shell'})

    def test_fleet_normalizes_legacy_peer_counts_and_badges(self):
        cases = [case for case in CASES if case['id'] in {
            'idle-unread-completion', 'idle-reviewed-completion', 'shell'}]
        rows = [fixture_row(case, index) for index, case in enumerate(cases)]
        reviewed = next(row for row in rows if row['reviewed'])
        reviewed['category'], reviewed['emoji'] = 'review', '👀'
        payload = {'ok': True, 'agents': rows, 'summary': {
            'review': 1, 'review_seen': 1, 'idle': 0, 'tracked': 2, 'untracked': 1}}
        with patch.object(a, 'load_peers', return_value=[{'name': 'old-peer', 'url': 'http://peer:7788'}]), \
                patch.object(a, 'fetch_peer_agents', return_value=payload), \
                patch.dict(a._FLEET_HOST_CACHE, clear=True):
            fleet = a.fleet_payload(include_local=False)
        self.assertEqual(fleet['summary'], a.empty_summary() | {
            'review': 1, 'idle': 1, 'tracked': 2, 'untracked': 1})
        by_pane = {row['pane']: row for row in fleet['agents']}
        self.assertEqual(by_pane[reviewed['pane']]['category'], 'idle')
        self.assertEqual(by_pane[reviewed['pane']]['emoji'], '💤')
        self.assertEqual({row['host'] for row in fleet['agents']}, {'old-peer'})
        self.assertEqual(reviewed['category'], 'review', 'peer data must remain unchanged')

    def test_api_exposes_category_emoji_and_partition_counts(self):
        selected = {'wait-acknowledged', 'idle-unread-completion',
                    'running-lingering-unread', 'idle-reviewed-completion',
                    'idle-without-completion-has-newer-revision', 'shell'}
        cases = [case for case in CASES if case['id'] in selected]
        rows = [fixture_row(case, index) for index, case in enumerate(cases)
                if case['category'] != 'shell']
        live = {(row['pane'], row['pid']): row for row in
                [fixture_row(case, index) for index, case in enumerate(cases)]}
        with patch.object(a, 'items', return_value=(rows, 1)), \
                patch.object(a, 'load_project_aliases', return_value={}), \
                patch.object(a, 'pane_conversation_preview', return_value={'users': [], 'agent': ''}), \
                patch.object(a, 'pane_model', return_value=('', '')), \
                patch.object(a, 'git_workdir_info', return_value={'repo': '', 'branch': '', 'dirty': False}):
            payload = a.agents_payload(None, 'fixtures', live, now=101)
        expected = {case['id']: case for case in cases}
        for agent in payload['agents']:
            with self.subTest(agent=agent['window']):
                case = expected[agent['window']]
                self.assertEqual(agent['category'], case['category'])
                self.assertEqual(agent['emoji'], case['emoji'])
        for category, count in {'wait': 1, 'review': 1, 'running': 1, 'idle': 2,
                                'tracked': 5, 'untracked': 1}.items():
            self.assertEqual(payload['summary'][category], count)
        self.assertEqual(payload['summary'].get('review_seen', 0), 0)
        self.assertEqual([agent['category'] for agent in payload['agents']],
                         ['wait', 'review', 'running', 'idle', 'idle', 'shell'])


if __name__ == '__main__':
    unittest.main()
