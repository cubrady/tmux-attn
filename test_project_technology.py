import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import attention as a


class ProjectTechnologyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / '.git').mkdir()
        a._PROJECT_TECH_CACHE.clear()

    def write(self, name, text=''):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        a._PROJECT_TECH_CACHE.clear()
        return path

    def package(self, deps=(), scripts=None, **extra):
        self.write('package.json', json.dumps({
            'dependencies': dict.fromkeys(deps, '*'), 'scripts': scripts or {}, **extra,
        }))

    def detect(self, suffix=''):
        return a.detect_project_technology(str(self.root / suffix))

    def test_android_applied_plugins_and_nested_cwd(self):
        for plugin in ('application', 'library'):
            self.write('app/build.gradle.kts', f'plugins {{ id("com.android.{plugin}") }}')
            (self.root / 'app/src').mkdir(exist_ok=True)
            self.assertEqual(self.detect(), 'android')
            self.assertEqual(self.detect('app/src'), 'android')
        self.write('app/build.gradle.kts', "apply plugin: 'com.android.library'")
        self.assertEqual(self.detect(), 'android')

    def test_android_comments_apply_false_and_invalid_catalog(self):
        for gradle in (
            '// plugins { id("com.android.application") }',
            '/* id("com.android.library") */',
            'plugins { id("com.android.application") version "8.7" apply false }',
            'plugins { id("com.android.application") apply(false) }',
            'plugins { id("com.android.application")\n version "8.7"\n apply false }',
            '"id(\'com.android.application\')"',
            'plugins { alias(libs.plugins.android.application) }',
        ):
            with self.subTest(gradle=gradle):
                self.write('build.gradle.kts', gradle)
                self.assertEqual(self.detect(), '')
        self.write('gradle/libs.versions.toml', '[plugins]\nandroid-application = { id = "com.android.application", version.ref = "agp" }')
        self.assertEqual(self.detect(), 'android')
        self.write('build.gradle.kts', 'plugins { alias(libs.plugins.android.application) apply false }')
        self.assertEqual(self.detect(), '')
        self.write('gradle/libs.versions.toml', '[plugins\nandroid-application = "com.android.application"')
        self.assertEqual(self.detect(), '')

    def test_gradle_alias_ancestor_catalog_and_nested_repo_boundary(self):
        self.write('gradle/libs.versions.toml', '[plugins]\nandroid-lib = {id="com.android.library"}')
        self.write('module/build.gradle.kts', 'plugins { alias(libs.plugins.android.lib) }')
        self.assertEqual(self.detect('module'), 'android')
        self.write('app/build.gradle.kts', 'plugins { alias(libs.plugins.android.lib) }')
        self.write('app/.git', 'gitdir: ../.git/worktrees/app')
        self.assertEqual(self.detect(), '')
        self.write('module/.git', 'gitdir: ../.git/worktrees/module')
        self.assertEqual(self.detect('module'), '')

    def test_react_web_app_requires_runtime_deps_and_app_evidence(self):
        self.package(('react', 'react-dom'))
        self.assertEqual(self.detect(), '')
        self.package(('react', 'react-dom'), {'build': 'vite build'})
        self.assertEqual(self.detect(), '')
        self.write('index.html')
        self.assertEqual(self.detect(), 'react')
        self.package((), {'build': 'vite build'}, devDependencies={'react': '*', 'react-dom': '*'})
        self.assertEqual(self.detect(), '')
        self.package(('react', 'react-dom'), {'start': 'react-scripts start'})
        self.assertEqual(self.detect(), 'react')

    def test_next_specificity_and_dependency_only_negative(self):
        self.package(('next', 'react', 'react-dom'))
        self.assertEqual(self.detect(), '')
        self.package(('next', 'react', 'react-dom'), {'build': 'next build', 'start': 'react-scripts start'})
        self.assertEqual(self.detect(), 'nextjs')
        self.package(('next', 'react'), {'dev': 'next dev'})
        self.assertEqual(self.detect(), '')

    def test_vue_app_and_nuxt_specificity(self):
        self.package(('vue',))
        self.assertEqual(self.detect(), '')
        self.package(('vue',), {'build': 'vite build'})
        self.write('index.html')
        self.assertEqual(self.detect(), 'vue')
        self.package(('vue', 'nuxt'), {'build': 'nuxt build'})
        self.assertEqual(self.detect(), 'nuxt')
        self.package(('nuxt',))
        self.assertEqual(self.detect(), '')
        self.write('nuxt.config.ts', 'export default defineNuxtConfig({})')
        self.assertEqual(self.detect(), 'nuxt')

    def test_svelte_and_dev_dependency_kit(self):
        self.package(('svelte',), {'build': 'vite build'})
        self.write('index.html')
        self.write('vite.config.js')
        self.assertEqual(self.detect(), '')
        self.package(('svelte',), {'build': 'vite build'}, devDependencies={'@sveltejs/vite-plugin-svelte': '*'})
        self.assertEqual(self.detect(), 'svelte')
        self.package(devDependencies={'svelte': '*', '@sveltejs/kit': '*', 'vite': '*'})
        self.assertEqual(self.detect(), '')
        self.write('src/app.html')
        (self.root / 'src/routes').mkdir()
        self.assertEqual(self.detect(), 'svelte')

    def test_angular_application_not_library_or_malformed(self):
        self.package(('@angular/core',))
        for angular in ('{}', '{invalid', '{"projects":{"ui":{"projectType":"library"}}}'):
            self.write('angular.json', angular)
            self.assertEqual(self.detect(), '')
        self.write('angular.json', '{"projects":{"web":{"projectType":"application"}}}')
        self.assertEqual(self.detect(), 'angular')

    def test_flutter_canonical_dependency_and_main(self):
        self.write('pubspec.yaml', 'name: example\ndependencies:\n  flutter:\n    sdk: flutter\n')
        self.assertEqual(self.detect(), '')
        self.write('lib/main.dart')
        self.assertEqual(self.detect(), 'flutter')
        for yaml in ('dev_dependencies:\n  flutter:\n    sdk: flutter\n',
                     '# dependencies:\n#   flutter:\n#     sdk: flutter\n',
                     'dependencies: {flutter: {sdk: flutter}}\n',
                     'dependencies:\n  flutter: ^1.0\n',
                     'dependencies:\n  other:\n    flutter:\n      sdk: flutter\n'):
            self.write('pubspec.yaml', yaml)
            self.assertEqual(self.detect(), '')

    def test_react_native_requires_native_or_expo_evidence(self):
        self.package(('react', 'react-dom', 'react-native'), {'build': 'vite build'})
        self.write('index.html')
        self.assertEqual(self.detect(), '')
        self.write('app.json', '{"expo":{"name":"Example","slug":"example"}}')
        self.assertEqual(self.detect(), 'react-native')
        self.write('app.json', '{invalid')
        self.assertEqual(self.detect(), '')
        self.write('metro.config.js')
        self.assertEqual(self.detect(), 'react-native')
        self.write('app/build.gradle', 'plugins { id "com.android.application" }')
        self.assertEqual(self.detect(), 'react-native')

    def test_python_metadata_service_and_scripts(self):
        for manifest in ('[tool.ruff]\nline-length = 100', '[project]\nname=""', '[project\nname="broken"'):
            self.write('pyproject.toml', manifest)
            self.assertEqual(self.detect(), '')
        for section in ('project', 'tool.poetry'):
            self.write('pyproject.toml', f'[{section}]\nname="example"')
            self.assertEqual(self.detect(), 'python')
            self.write('pyproject.toml', f'[{section}]\nname="example"\n[{section}.scripts]\nexample="example:main"')
            self.assertEqual(self.detect(), 'cli')
        for scripts in ('[project.scripts]\n', '[project.scripts]\ntool=17',
                        '[project.scripts]\ntool="missing-callable"'):
            self.write('pyproject.toml', scripts)
            self.assertEqual(self.detect(), '')
        self.write('requirements.txt', '# fastapi\nfastapi-tools>=1')
        self.assertEqual(self.detect(), '')
        self.write('requirements.txt', 'FastAPI[standard]>=0.115')
        self.assertEqual(self.detect(), 'python')

    def test_npm_bin_validation(self):
        for value in ({}, [], True, {'tool': ''}, {'tool': 1}, ''):
            self.package(bin=value)
            self.assertEqual(self.detect(), '')
        for value in ('cli.js', {'tool': 'cli.js'}):
            self.package(bin=value)
            self.assertEqual(self.detect(), 'cli')

    def test_standalone_wrapper_is_cli_with_bounded_large_source(self):
        launcher = self.write('tool', '#!/bin/sh\nexec python3 "$(dirname "$0")/attention.py" "$@"\n')
        launcher.chmod(0o755)
        self.write('attention.py', 'import argparse\n' + '# source\n' * 10000)
        self.assertEqual(self.detect(), 'cli')
        launcher.chmod(0o644)
        a._PROJECT_TECH_CACHE.clear()
        self.assertEqual(self.detect(), '')

    def test_rust_package_and_go_source_are_language_icons(self):
        for cargo in ('# [package]\n# name="x"', '[package]\nname=""', '[package\nname="x"', '[workspace]\nmembers=["tool"]'):
            self.write('Cargo.toml', cargo)
            self.assertEqual(self.detect(), '')
        self.write('Cargo.toml', '[package]\nname="tool"\nversion="0.1.0"')
        self.write('src/main.rs', 'fn main() {}')
        self.assertEqual(self.detect(), 'rust')
        (self.root / 'Cargo.toml').unlink()
        self.write('go.mod', 'module example.com/tool\n\ngo 1.23\n')
        self.assertEqual(self.detect(), '')
        self.write('main.go', 'package main\nfunc main() {}')
        self.assertEqual(self.detect(), 'go')
        self.write('go.mod', '// module example.com/tool\n')
        self.assertEqual(self.detect(), '')

    def test_unrelated_candidates_conflict(self):
        self.package(('react', 'react-dom'), {'build': 'vite build'})
        self.write('index.html')
        self.write('pyproject.toml', '[project]\nname="backend"')
        self.assertEqual(self.detect(), '')
        self.write('app/build.gradle', 'plugins { id "com.android.application" }')
        self.assertEqual(self.detect(), '')

    def test_workspace_root_blank_nearest_child_detects(self):
        self.write('requirements.txt', 'fastapi>=0.115')
        self.write('apps/web/package.json', '{"dependencies":{"react":"*","react-dom":"*"},"scripts":{"start":"react-scripts start"}}')
        self.assertEqual(self.detect(), '')
        self.assertEqual(self.detect('apps/web'), 'react')
        self.assertEqual(self.detect('apps'), '')

    def test_workspace_declarations_suppress_inference(self):
        self.package(('react', 'react-dom'), {'start': 'react-scripts start'}, workspaces=['web'])
        self.assertEqual(self.detect(), '')
        self.package(('react', 'react-dom'), {'start': 'react-scripts start'})
        self.write('pnpm-workspace.yaml', 'packages: [web]')
        self.assertEqual(self.detect(), '')

    def test_every_child_boundary_exists_even_empty_invalid_or_special(self):
        self.package(('react', 'react-dom'), {'start': 'react-scripts start'})
        for index, manifest in enumerate(a._PROJECT_MANIFESTS):
            child = f'child{index}'
            self.write(f'{child}/{manifest}', '')
            self.assertEqual(self.detect(child), '', manifest)
        self.write('bad/package.json', '{malformed')
        self.assertEqual(self.detect('bad'), '')
        self.write('worktree/.git', 'gitdir: /tmp/another-worktree')
        self.assertEqual(self.detect('worktree'), '')
        (self.root / 'fifo').mkdir()
        os.mkfifo(self.root / 'fifo/package.json')
        self.assertEqual(self.detect('fifo'), '')
        (self.root / 'link').mkdir()
        (self.root / 'link/package.json').symlink_to('/nonexistent/project-manifest')
        self.assertEqual(self.detect('link'), '')

    def test_oversized_manifests_rejected_and_read_is_regular_only(self):
        self.write('package.json', '{"bin":"cli.js"}' + ' ' * 65536)
        self.assertEqual(self.detect(), '')
        self.write('pyproject.toml', '[project]\nname="x"\n' + '#' * 65536)
        self.assertEqual(self.detect(), '')
        os.mkfifo(self.root / 'pipe')
        self.assertEqual(a._project_read(self.root / 'pipe'), '')
        self.assertEqual(a._project_read(self.root), '')

    def test_bounded_iteration_ancestry_and_cache(self):
        self.assertEqual(a.detect_project_technology(''), '')
        self.assertEqual(a.detect_project_technology('relative'), '')
        self.package(bin='cli.js')
        self.assertEqual(self.detect(), 'cli')
        (self.root / 'package.json').write_text('{}')
        self.assertEqual(self.detect(), 'cli')
        with patch.object(a.time, 'monotonic', return_value=a.time.monotonic() + 61):
            self.assertEqual(self.detect(), '')
        deep = '/'.join(['nested'] * 8)
        (self.root / deep).mkdir(parents=True)
        self.package(bin='cli.js')
        self.assertEqual(self.detect(deep), '')
        for index in range(260):
            a.detect_project_technology(str(self.root / f'missing{index}'))
        self.assertLessEqual(len(a._PROJECT_TECH_CACHE), 256)
        for index in range(129):
            self.write(f'many/file{index}')
        self.assertEqual(a._project_entries(self.root / 'many'), [])

    def test_local_payload_emits_technology_for_tracked_and_shell_panes(self):
        self.package(bin='cli.js')
        self.write('shell/pyproject.toml', '[project]\nname="service"')
        live = {
            ('%1', '123'): {'pane': '%1', 'pid': '123', 'session': 'work', 'window': 'one',
                            'window_id': '@1', 'path': str(self.root), 'command': 'codex'},
            ('%2', '456'): {'pane': '%2', 'pid': '456', 'session': 'work', 'window': 'two',
                            'window_id': '@2', 'path': str(self.root / 'shell'), 'command': 'zsh'},
        }
        with patch.object(a, 'STATE', self.root / 'state'), \
                patch.object(a, 'git_workdir_info', return_value={'repo': 'project', 'branch': '', 'dirty': False}), \
                patch.object(a, 'shell_pane_preview', return_value={}), \
                patch.object(a, 'pane_conversation_preview', return_value={}), \
                patch.object(a, 'pane_model', return_value=('', '')), \
                patch.object(a, 'pane_tasks', return_value=None), \
                patch.object(a, 'project_technology', side_effect=a.detect_project_technology):
            con = a.db()
            try:
                a.record(con, 'socket', '%1', '123', 'codex', 'WAIT', 'Approval')
                payload = a.agents_payload(con, 'socket', live, now=a.time.time() + 3)
            finally:
                con.close()
        self.assertEqual({row['tracked'] for row in payload['agents']}, {True, False})
        self.assertEqual({row['tracked']: row['project_technology'] for row in payload['agents']},
                         {True: 'cli', False: 'python'})

    def test_fleet_preserves_remote_technology_and_missing_legacy_field(self):
        peer = {'name': 'peer-project-icons', 'url': 'http://example.invalid'}
        base = {'pane': '%1', 'pid': '123', 'state': 'RUNNING', 'tracked': True,
                'path': str(self.root), 'project': 'remote', 'updated': 100, 'reason': 'Working'}
        payload = {'ok': True, 'agents': [dict(base, project_technology='android'), dict(base, pane='%2')]}
        with patch.object(a, 'load_peers', return_value=[peer]), \
                patch.object(a, 'fetch_peer_agents', return_value=payload), \
                patch.object(a, 'project_technology', side_effect=AssertionError('remote path must not be inspected')):
            fleet = a.fleet_payload(include_local=False)
        rows = {row['pane']: row for row in fleet['agents']}
        self.assertEqual(rows['%1']['project_technology'], 'android')
        self.assertNotIn('project_technology', rows['%2'])

    def test_command_strings_and_invalid_dependency_values_are_not_app_evidence(self):
        self.package(('next', 'react', 'react-dom'), {'build': 'echo next build'})
        self.assertEqual(self.detect(), '')
        self.write('package.json', '{"dependencies":{"next":true,"react":"*","react-dom":"*"},"scripts":{"build":"next build"}}')
        self.assertEqual(self.detect(), '')

    def test_unreadable_child_manifest_does_not_inherit(self):
        self.package(('react', 'react-dom'), {'start': 'react-scripts start'})
        child = self.write('child/package.json', '{}')
        real_open = os.open

        def guarded_open(path, *args, **kwargs):
            if path == child:
                raise PermissionError('unreadable')
            return real_open(path, *args, **kwargs)

        with patch.object(a.os, 'open', side_effect=guarded_open):
            self.assertEqual(self.detect('child'), '')

    def test_gradle_quoted_examples_are_not_applied_plugins(self):
        for gradle in (
            '''println("plugins { id('com.android.application') }")''',
            '''println("apply plugin: 'com.android.application'")''',
            '''val text = "apply(plugin = 'com.android.library')"''',
            '''println("plugins { alias(libs.plugins.android.application) }")''',
        ):
            with self.subTest(gradle=gradle):
                self.write('build.gradle.kts', gradle)
                self.write('gradle/libs.versions.toml', '[plugins]\nandroid-application = {id="com.android.application"}')
                self.assertEqual(self.detect(), '')

    def test_quoted_commented_and_escaped_script_commands_are_not_app_evidence(self):
        for script in (
            'echo example # && next build',
            'echo "example && next build "',
            'echo "&&" next build',
            "echo 'example ; next build '",
            'echo example \\; next build',
            'echo "next build"',
            '"next build"',
            'echo $(echo example && next build)',
            'echo `echo example && next build`',
        ):
            with self.subTest(script=script):
                self.package(('next', 'react', 'react-dom'), {'build': script})
                self.assertEqual(self.detect(), '')
        for script in ('next build', 'echo example && next build', 'NODE_ENV=production next build',
                       'npx next build', 'pnpm exec next build', 'echo "example"; next build',
                       'echo example # comment\nnext build', '"next" "build"'):
            with self.subTest(script=script):
                self.package(('next', 'react', 'react-dom'), {'build': script})
                self.assertEqual(self.detect(), 'nextjs')
