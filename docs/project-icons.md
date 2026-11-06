# Project technology icons

## Design and detection contract

Show one representative SVG before the project name only when local files provide strong evidence. Never infer technology from names, conversation text, extensions alone, agent command, or transitive dependencies. Mixed/unknown roots remain unlabelled. All detection is read-only and runs on the host owning the directory; old peers without metadata remain unlabelled.

Keys: `android`, `react`, `nextjs`, `vue`, `nuxt`, `svelte`, `angular`, `flutter`, `react-native`, `python`, `cli`, `rust`, `go`.

- Android: applied application/library Gradle plugin. Exclude comments and `apply false`; resolve used aliases via parsed `gradle/libs.versions.toml`. Common root `app/build.gradle(.kts)` can corroborate Android module. Label Android project, not necessarily App.
- React website: direct react/react-dom, no react-native, plus web app build script and index.html, or react-scripts. Component libraries alone do not qualify.
- Next.js: next/react/react-dom and next dev/build/start script; more specific than React.
- Vue: vue with corroborating web app build script and index.html, or vue-cli-service. Nuxt takes precedence with nuxt dependency and nuxt/nuxi script or config.
- Svelte: svelte plus Vite plugin/build config and index.html; Kit can use devDependencies, requiring @sveltejs/kit, vite, src/app.html and src/routes.
- Angular: @angular/core plus parsed angular.json with an application project. Libraries alone do not qualify.
- Flutter: only canonical indentation-based dependencies.flutter.sdk: flutter with lib/main.dart; unsupported YAML syntax remains unknown. No general YAML parser added.
- React Native: direct react/react-native plus Metro config, native app evidence, or parsed Expo app.json/config entry. Excludes generic React website.
- Python: parsed pyproject project/Poetry metadata with nonempty project name, or known service requirements. Tool-only pyproject and bare .py files are insufficient. Nonempty valid script entry tables take CLI precedence.
- CLI: valid nonempty npm bin or Python scripts; bounded standalone executable shell wrapper delegating to argparse CLI. Rust/Go binaries do not establish CLI.
- Rust: parsed Cargo [package] with nonempty package name. Workspace-only root blank.
- Go: go.mod module declaration and local .go source. Label Go, not CLI.

Collect independent candidates and return blank on unrelated conflicts. Related framework specificity wins (Next over React, Nuxt over Vue, SvelteKit shares Svelte). Workspace declarations or conservatively mixed apps/services/packages directories suppress root inference; nearest valid child can still be identified.

Read regular files only; cap each manifest at 64KiB and reject oversized/truncated content. Cap any directory listing (128 entries) and ancestor lookup (8 levels); no recursive repo scanning or executed project code. Cache results for at most 60 seconds with a bounded cache (256 entries).

Stop ancestry on existence, not readable contents: package.json, pyproject.toml, requirements.txt, Cargo.toml, go.mod, pubspec.yaml, Gradle build/settings, pnpm-workspace.yaml, workspace markers, or .git (including worktree files). Empty/malformed/unreadable/unsupported child manifests may not borrow parent technology. At a child module resolving Gradle aliases, read ancestor catalogs only within its nearest repository and bounded lookup; do not cross nested repository boundaries.

## Research sources

These rules are conservative heuristics inferred from the following primary documents, not a universal classifier supplied by the frameworks.

- Android plugins/catalogs: https://developer.android.com/build/migrate-to-catalogs
- React application tooling: https://react.dev/learn/build-a-react-app-from-scratch
- Next dependencies/scripts: https://nextjs.org/docs/app/getting-started/installation
- Vue app setup: https://vuejs.org/guide/quick-start.html
- Nuxt: https://nuxt.com/docs/4.x/getting-started/installation
- Svelte/Kit structure and dev dependencies: https://svelte.dev/docs/kit/project-structure
- Angular applications vs libraries: https://angular.dev/reference/configs/workspace-config
- Flutter SDK dependency: https://docs.flutter.dev/tools/pubspec
- React Native native integration: https://reactnative.dev/docs/integration-with-existing-apps
- Expo configuration: https://docs.expo.dev/versions/latest/config/app/
- Python metadata/scripts: https://packaging.python.org/en/latest/specifications/pyproject-toml/
- npm bin/workspaces: https://docs.npmjs.com/cli/v11/configuring-npm/package-json/
- Rust manifests/targets: https://doc.rust-lang.org/cargo/reference/manifest.html
- Go layout: https://go.dev/doc/modules/layout

## Known limits

Dynamic configuration is never evaluated. Unsupported YAML layouts, custom Gradle catalogs, obscure build conventions, and mixed roots may remain blank. Each peer must be updated to emit project technology metadata. Cached results can take 60 seconds to reflect edits.
