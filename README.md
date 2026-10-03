# tmux attention inbox

A global agent inbox for a Ghostty client connected over SSH to a Mac running
tmux. The status bar stays visible; Prefix + a opens a temporary popup.
The popup groups all tracked agents into WAIT, IDLE and RUNNING. Existing
sessions, windows and agent panes keep running.

## Three agent states

- **RUNNING**: working; listed in the RUNNING group.
- **IDLE**: not working. A completed response has an unread flag (**⚪ •**), so
  it enters the inbox until you review it. Initial registration does not notify.
- **WAIT**: waiting for approval, an answer or a decision; shown first.

Opening a pane does **not** acknowledge it. A new work event clears its previous
pending item. Marking an item reviewed leaves the agent's actual state unchanged.
All completed responses enter unread IDLE, including interrupted/error endings
when the agent emits the corresponding event. A response ending in `?` or `？`
is inferred to need an answer and appears as WAIT; this rule can misclassify
rhetorical questions. Completion has a two-second settling delay to avoid
flashing an inbox item during automatic continuation.

Claude background tasks keep the agent RUNNING. Pi uses `agent_settled`, rather
than `agent_end`, so retries and queued work do not appear prematurely completed.
Native hooks drive the main lifecycle. A read-only compatibility observer also
tracks existing Codex CLIs without requiring restart; no LLM calls are used.

## Install and activate

On a new Mac with Python 3.11+ and tmux available, copy/clone this folder and run:

```sh
sh bootstrap.sh
```

The bootstrap checks dependencies, creates a `work` tmux session if no session
exists, and installs the popup, status badges and hooks. It opts in to enabling
Codex `[features] hooks = true`; the original config is backed up first. Other
settings and hooks are retained. Unusual inline/dotted TOML hook configuration
is left unchanged with instructions to enable hooks manually. Dependency
installation and agent login remain separate steps. Codex may ask you to trust
the new hook commands in `/hooks`; Pi needs `/reload` for existing sessions.


Requires Python 3.11+, tmux with `split-window -e` and hooks (tested on tmux
3.7b), Claude Code, Codex with `[features] hooks = true`, and Pi with
`agent_settled` / `ui_prompt_start` events (tested on Pi 1.0.0).

From this directory, with an existing tmux server:

```sh
python3 -m unittest test_attention.py test_codex_observer.py test_migration.py
python3 install.py
```

The installer backs up affected settings, installs the Python app under
`~/.local/share/tmux-attn`, adds `~/.local/bin/tmux-attn`, merges agent hooks,
installs `~/.pi/agent/extensions/tmux-attention.ts`, and sources the generated
`~/.config/tmux-attn/sidebar.conf`. It replaces only the previous
`tmux-agent-status` hook handlers; unrelated hooks are retained. The old plugin
files remain untouched and its commented-out tmux loading stays disabled.

Run the installer once. Re-running it makes a new backup, including the prior
installed app. Hook array index 90 is reserved for this integration; installation
refuses to replace an unrelated hook occupying that index.

### Existing agent processes

- **Claude Code**: this installation has already received a live hook event.
  If an older session does not update, restart/resume that agent when convenient.
- **Codex**: the eight installed handlers have been reviewed and trusted in
  a fresh CLI. For existing sessions, open `/hooks` to inspect/reload command hooks and trust new commands
  if prompted. Restart/resume the agent if the updated hooks are not loaded.
- **Pi**: run `/reload`, or restart/resume Pi, to load the extension.

Agent initialization never launches provider requests. For existing Codex, the compatibility observer pairs the native process to a
unique startup session using process start time, cwd and validated session metadata.
It reads task_started/task_complete events, and can route detached native hooks
by session_id once the mapping is established. It never chooses a session just
because it is newest in the same project. Until events arrive, the
sidebar reports how many panes have no events. That count includes shell panes;
it is not a count of broken agents. A fresh Codex starts its conversation lifecycle on the first submitted prompt;
just opening an empty CLI may not emit a SessionStart event.

Existing work is not interrupted to force
reloads. Approval/answer handling is observed, never automated.

## Keyboard

Default tmux prefix is **Ctrl-b**.

| Key | Action |
| --- | --- |
| Prefix, `a` | Open the inbox popup |
| Prefix, `N` | Jump to the first pending item |
| `j` / `k`, arrows | Select an item inside the inbox |
| Enter | Jump to its exact agent pane; retain pending status |
| `v` | Toggle a terminal preview of the selected pane |
| `d` | Mark selected response reviewed; show confirmation and reviewed label |
| `1` | Snooze this revision for five minutes |
| `a` | Switch between all agents (default) and pending items |
| Esc | Close the popup (also `q`) |

A new event overrides an old snooze. When a snooze expires, the same pending
item returns. Duplicate events with the same state and reason do not repeat a
notification. Selection follows pane identity as the list changes.

## Commands and files

```sh
~/.local/bin/tmux-attn          # open inbox popup
~/.local/bin/tmux-attn status   # WAIT / IDLE (unread) / RUN counts
~/.local/bin/tmux-attn dump     # registered live panes and metadata
```

The state store is `~/.local/state/tmux-attn/inbox.sqlite3`. SQLite transactions
serialize concurrent hooks and review actions. Keys include tmux socket, pane ID
and pane PID to avoid cross-server state and reused-pane mistakes. Hooks resolve the actual
parent process chain across local tmux sockets, rather than relying on possibly
stale `TMUX_PANE` values from Codex shell snapshots. Window labels
and project paths are resolved live, so renaming does not lose tracking.

Hook failures are recorded in `~/.local/state/tmux-attn/hook-errors.log`; hooks
return an empty JSON object without making agent approval decisions. No full
transcripts or preview contents are stored. Closed-pane records are filtered
from the UI, but historical records currently remain in the database.

The status bar refreshes every two seconds and displays `🟠 1 · ⚪ 3 (•2) · 🟢 4`.
The number in parentheses is unread completed responses within IDLE. The popup
refreshes every second; Enter jumps to the exact agent pane and closes it.
Multiple clients share review flags; each popup targets the client that opened it.
Visible counts and the inbox work over the existing SSH link.

The bottom window strip shows project names with explicit state labels:
`🟠` (waiting), `🟢` (working), `⚪ •` (idle with unread response), `⚪` (idle without unread response).
Windows with multiple tracked agents use that priority order. Shell windows with
no tracked events have no badge. Reviewing a completed response clears its
unread dot on the next two-second refresh. The project display name comes from
the tracked pane cwd and the alias settings below; the popup retains the full
project name. Original window names remain as the fallback for untracked windows.
The installer retains window flags and backs up both window formats for rollback.

For the previous persistent 38-column left sidebar, install with `--sidebar`.
The default installer removes only marked sidebar panes and disables their
creation hooks. Agent panes and background tracking keep running.

## Rollback

The installer prints a timestamped backup directory. Use that exact path:

```sh
python3 rollback.py ~/.local/state/tmux-attn/backups/YYYYMMDD-HHMMSS
```

Rollback restores saved settings and keybindings, removes only marked attention
sidebar panes, and preserves agent panes. When rolling back an upgrade, it also
restores the prior installed app/hooks and display mode. Reload agents
to restore the previous hook/extension behavior. Rollback restores whole backed-up
configuration files, so preserve any edits made after installation first.

The Codex compatibility observer has one process per tmux server and reads local
session records incrementally every two seconds. It discovers new CLI processes
periodically. Legacy pending approval is detected only from an actual visible
approval dialog with Yes/No options; escalation arguments alone do not count.
A completed response ending in a question uses the same inferred WAIT rule as hooks.

Startup matching cannot establish an arbitrary `/new` or `/resume` switch inside
an older CLI PID. Native session registrations supersede startup inference; if
an older CLI switches sessions without emitting hooks, reload its hooks or restart
and resume it. This adapter depends on the installed Codex's local session schema;
unsupported/ambiguous metadata is left unmapped rather than guessed. Adapter
errors go to `~/.local/state/tmux-attn/observer-errors.log`.

The original polling/scheduling starter is archived under `legacy/`; its old
JSON state and configuration are not used by the event-based sidebar.

## Validation evidence

`python3 -m unittest test_attention.py test_codex_observer.py test_migration.py` runs nineteen tests. Integration runs in an
isolated tmux server and explicitly poisons `TMUX_PANE` to verify actual process
ancestry wins over stale environment. It covers rendering, review/snooze, new
windows, precise jumps and first-install/upgrade rollback.

After installation, optional native checks are available:

```sh
python3 tests/native_codex_roundtrip.py
python3 tests/native_pi_dialog.py
```

Codex uses an in-process localhost Responses stub, so no remote model is called.
Pi invokes a local confirmation command without a model request. Both launch
disposable isolated tmux servers and remove only their own test server. These
checks were run successfully against the installed CLI versions.

`python3 tests/popup_roundtrip.py` attaches a disposable PTY client to verify
three-group rendering, popup close and precise navigation without agent pane loss.

Reviewing retains the agent in its actual state group. ⚪ • loses its unread
dot and displays `reviewed 已查看`; WAIT retains WAIT until the agent resumes.
Pressing d on RUNNING reports that there is no response to review. New completed
responses become unread again. Review confirmation remains visible for four seconds.

Codex child hooks can share their parent session_id. The integration validates
the transcript session_meta.id and subagent source before changing visible pane
state. Child tools, permissions, completion and exit events are excluded from
parent state. The observer also validates mapped transcript identity before reading
it; canonical root paths are recovered from the local threads database.

Popup reasons describe the action in Chinese: approval, answer/decision, work
in progress, or completed/interrupted/error response awaiting review. Inferred
questions are explicitly labelled as inference. Reviewing keeps the underlying
agent state and removes the unread dot.

## Project aliases

Edit `~/.config/tmux-attn/project-aliases.json`. The default mappings are minday,
tmux, qiu, mail, lending, wallet and cory; see `project-aliases.example.json`.
An exact absolute project path takes priority over a folder-name mapping:

```json
{"aliases": {"web-app-minday": "minday", "/Users/dev/proj/another/api": "other-api"}}
```

Unmapped folder names are shown unchanged up to 12 characters, otherwise as
11 characters plus an ellipsis. Different projects with duplicate display names
get a readable folder suffix when possible, otherwise a stable short hash of the
full path. Multiple windows for the same project retain the same alias. Changes
apply on the next two-second refresh. The installer preserves existing mappings
and includes the file in its rollback backup. Popup project names stay complete.

## 移植到另一台 Mac

設定安裝在**執行 tmux／agent 的機器**。如果 M3 仍透過 SSH 使用目前的 M1，
這套環境繼續放在 M1 即可；只有換工作 server，或在另一台機器直接跑 agent，才需移植。

匯出目前程式及你的別名設定：

```sh
python3 migrate.py export dist/tmux-attn-migration.tar.gz
```

將壓縮檔複製到新機器，再執行：

```sh
tar -xzf tmux-attn-migration.tar.gz
cd tmux-attn
sh bootstrap.sh
```

匯出只使用固定檔案清單，包含程式、安裝／還原工具、測試與別名。
不包含 tmux 執行狀態、agent 帳號憑證、完整個人設定或對話紀錄。
新機器的路徑會重新產生；別名以資料夾名稱為鍵時可直接沿用。
使用完整絕對路徑的 mapping，需按新機器的專案位置調整。
目的機已有別名時，保留目的機的設定，補上匯入檔中的缺少項目。
匯出檔不會覆蓋同名檔案；重新匯出時請使用新的輸出檔名。

新機器須先備好 Python 3.11+、支援 popup 的 tmux，以及要使用的 agent CLI。
安裝不會代替 agent 登入或同意 Codex 的 hook 信任提示。
備份位置由安裝指令輸出；用 `python3 rollback.py <備份路徑>` 還原。
遷移工具不會遠端登入或直接寫入另一台機器。

## Repository tracking

Track the app, observer, installer/bootstrap, migration and rollback scripts,
tests, README and default alias example. The provided `.gitignore` excludes
Python caches, export bundles, local exported aliases, SQLite files and machine
installation/audit records. Personal full-path mappings belong in the local
alias file; reusable folder aliases can live in the versioned example.
The `legacy/` directory preserves the original starter and can remain archived.
A private repository is a suitable home for this personalized tool.

Named `codex resume` processes can also be paired using the exact current footer
project path and full conversation name, provided the local thread database has
one matching root transcript with matching cwd and CLI originator. Ambiguous or
truncated names are left unpaired; the observer never selects the newest thread
by project directory. Untracked panes still display their cwd-derived project
alias, with no state badge until actual events establish their state.
