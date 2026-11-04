import unittest

import attention as a


class AntigravityUsageTests(unittest.TestCase):
    def test_cli_provider_and_rich_quota_buckets(self):
        commands = []

        def runner(command):
            commands.append(command)
            return [{'provider': 'antigravity', 'usage': {
                'primary': {'usedPercent': 1, 'windowMinutes': 300},
                'extraRateWindows': [
                    {'id': 'gemini-5h', 'title': 'Gemini 5-hour',
                     'window': {'usedPercent': 1, 'windowMinutes': 300}},
                    {'id': 'argon-weekly', 'title': 'Gemini 4 Argon weekly',
                     'window': {'usedPercent': 37.8, 'windowMinutes': 10080}},
                ],
            }}]

        rows = a._fetch_provider_usage('antigravity', binary='codexbar', runner=runner)
        self.assertEqual(commands[0][2:4], ['--provider', 'antigravity'])
        self.assertEqual(a.parse_usage_providers('antigravity'), ['antigravity'])
        windows = rows[0]['windows']
        self.assertEqual(len(windows), 2)  # primary duplicates the Gemini bucket
        self.assertEqual(windows[1]['label'], 'Gemini 4 Argon weekly')
        self.assertEqual(windows[1]['shortLabel'], '週')
        self.assertEqual(windows[1]['usedPercent'], 38)

    def test_basic_windows_survive_missing_or_invalid_extras(self):
        for extras in (None, {}, [None, {'window': {}}]):
            row = a._normalize_usage_account({'provider': 'antigravity', 'usage': {
                'primary': {'usedPercent': 10, 'windowMinutes': 300},
                'extraRateWindows': extras,
            }}, 'antigravity')
            self.assertEqual(row['windows'][0]['usedPercent'], 10)
