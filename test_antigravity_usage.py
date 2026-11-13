import unittest
import os
import subprocess
from unittest.mock import patch

import attention as a


class AntigravityUsageTests(unittest.TestCase):
    def test_actual_provider_source_survives_live_cache_provenance(self):
        for upstream in ('cli', 'app', 'ide', 'oauth'):
            with self.subTest(upstream=upstream):
                row = a._normalize_usage_account({
                    'provider': 'antigravity', 'source': upstream,
                    'usage': {'primary': {'usedPercent': 55.5253, 'windowMinutes': 300}},
                }, 'antigravity')
                row['observed_at'] = 1000
                merged = a._merge_usage_accounts('antigravity', [row], None, 1000, 'live')
                self.assertEqual(merged[0]['source'], 'live')
                self.assertEqual(merged[0]['upstream_source'], upstream)
                self.assertEqual(merged[0]['windows'][0]['usedPercent'], 56)

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


class AntigravityAuthScopeTests(unittest.TestCase):
    def test_explicit_scope_changes_only_antigravity_child_environment(self):
        for scope in ('file', 'keychain'):
            with patch.dict(os.environ, {'TMUX_ATTN_AGY_AUTH_SCOPE': scope, 'SSH_TTY': 'original', 'SSH_CONNECTION': 'original'}), patch.object(a.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, stdout='[{"provider":"antigravity"}]')) as run:
                rows = a._run_codexbar_json(['codexbar', 'usage', '--provider', 'antigravity'])
                env = run.call_args.kwargs['env']
                self.assertEqual(env.get('SSH_TTY'), 'tmux-attn-file-auth' if scope == 'file' else None)
                self.assertNotIn('SSH_CONNECTION', env)
                self.assertEqual(rows[0]['auth_scope'], scope)
                self.assertEqual(os.environ['SSH_TTY'], 'original')
                a._run_codexbar_json(['codexbar', 'usage', '--provider', 'codex'])
                self.assertNotIn('env', run.call_args.kwargs)

    def test_auth_scope_survives_normalization(self):
        row = a._normalize_usage_account({'provider':'antigravity','source':'cli','auth_scope':'file','usage':{'primary':{'usedPercent':55.53}}}, 'antigravity')
        self.assertEqual(row['auth_scope'], 'file')

    def test_explicit_scope_rejects_other_login_and_unscoped_widget_cache(self):
        base = {'provider':'antigravity','account':'antigravity','observed_at':100,'windows':[{'usedPercent':31}]}
        scoped = {**base,'auth_scope':'file'}
        with patch.dict(os.environ, {'TMUX_ATTN_AGY_AUTH_SCOPE':'file'}):
            self.assertEqual(a._usage_cached_rows([base], 'antigravity',100,101,3600), [])
            rows = a._merge_usage_accounts('antigravity',[{**base,'observed_at':102}],(100,[scoped]),103,'codexbar-cache')
            self.assertEqual(rows[0]['auth_scope'],'file')
            self.assertEqual(rows[0]['observed_at'],100)


if __name__ == '__main__':
    unittest.main()
