# Coding Agent 狀態偵測研調

盤點日期：2026-10-19。對照官方文件與 `tmux-attn` 實作，目標是更正確、精準地取得 WAIT / RUNNING / IDLE（±review）。

## 產品狀態模型

| DB `state` | UI | 意義 |
| --- | --- | --- |
| `WAIT` | 🙋 | 需要人核准或作答 |
| `RUNNING` | 🏃 / ⏳ | 工作中；⏳ = 明確在等背景 subagent，或本輪 shell 經畫面、task 與程序交叉確認仍在執行 |
| `IDLE` + `needs_review` | 👀 | 回合結束，建議回看 |
| `IDLE` | 💤 | 已登記 / 等待下一題 / 乾淨結束 |

來源：`hook`（主）· `observer`（備援交叉驗證）。尾端 `?` 只當 soft review，不當硬 WAIT。

## 官方事件來源

### Claude Code

- 文件：https://code.claude.com/docs/en/hooks 、https://code.claude.com/docs/en/hooks-guide
- WAIT：`PermissionRequest`、`Notification`（`permission_prompt` / `elicitation_dialog` / `elicitation_url_dialog` / `agent_needs_input` / `worker_permission_prompt`）、`Elicitation`
- RUNNING：`UserPromptSubmit` / `PreToolUse` / `PostToolUse`
- IDLE/錯誤：`Stop` / `StopFailure`；team：`TeammateIdle`；subagent：`SubagentStart` / `SubagentStop`
- 工具失敗：`PostToolUseFailure`；完成提示：`Notification` `agent_completed`

### Codex

- 文件：https://developers.openai.com/codex/hooks
- 事件：`SessionStart/End`、`UserPromptSubmit`、`Pre/PostToolUse`、`PermissionRequest`、`Stop`、`Interrupt`、`SubagentStart/Stop`（另有 `Pre/PostModelRequest`）
- WAIT：`PermissionRequest` + ask 類 tool；無 Claude 式 `Notification`
- 營運：非 managed command hooks 必須在 `/hooks` **trust**；信任寫在 `~/.codex/config.toml` 的 `[hooks.state."…"]`
- app-server hooks 常無 pane 祖先 → 需要 observer 對應 session→pane

### Cursor

- 文件：https://cursor.com/docs/hooks 、https://cursor.com/docs/reference/third-party-hooks
- 完成：`stop` + `status: completed|aborted|error`
- **沒有一等公民「waiting for approval」hook**（論壇官方確認；feature request 仍開）
- 近似訊號：`beforeShellExecution` / `beforeMCPExecution` 在 Run/Reject 前觸發；`permission: ask` 多模式不可靠 → 觀測時一律回 `allow`，只用來標記 WAIT
- Claude 第三方映射不含 `PermissionRequest` / `Notification`

### Pi

- 文件：https://pi.dev/docs/latest/extensions
- 最終 idle：用 **`agent_settled`**（不要用 `agent_end`）
- WAIT：`ui_prompt_start` / `ui_prompt_end`（專為 host/status 區分「等使用者」）
- `ui_prompt_*` 包住 `ctx.ui.select|confirm|input|editor|custom`；非此路徑需 observer 備援

## 缺口與修復對照（P0–P2）

| ID | 嚴重度 | 問題 | 修復方向 |
| --- | --- | --- | --- |
| 1 | P0 | Cursor WAIT 無官方 hook，只靠 scrape | 觀測 `beforeShell*` → early RUNNING + `allow`（不造假 WAIT）；WAIT 仍靠 pane scrape；裝齊 permission 觀測 hooks |
| 2 | P0 | Claude 漏 `Elicitation` / `elicitation_url_dialog` | 安裝並 classify → WAIT；`ElicitationResult` → RUNNING |
| 3 | P0 | Codex hook 未 trust 會靜默失效 | install 檢查 `[hooks.state]`，缺信任則明確警告 |
| 4 | P1 | Claude `TeammateIdle` / `SubagentStop` | 安裝並映射狀態 |
| 5 | P1 | Claude `PostToolUseFailure` | → RUNNING（工具失敗後常繼續）或錯誤提示 |
| 6 | P1 | Cursor 缺 `preToolUse` | 安裝 → RUNNING（較早進入工作中） |
| 7 | P1 | Pi 無 observer | observer 偵測 pi pane + 畫面備援；extension 心跳 |
| 8 | P1 | Pi `ui_prompt` 只涵蓋 `ctx.ui` | 畫面備援 + `tool_execution_start` 補強 RUNNING |
| 9 | P2 | Codex `SubagentStart/Stop` | 父 session 事件 → RUNNING；child transcript 仍忽略 |
| 10 | P2 | Cursor `afterAgentResponse` / `subagentStop` | 安裝並映射 |
| 11 | P2 | Claude `agent_completed` Notification | → IDLE + review |

## 原則（實作約束）

1. Hooks 只觀測：永不 `deny` / `block` agent 工作；Cursor permission hooks 回 `allow`。
2. Hooks 為主、observer 為輔；observer 不覆蓋剛寫入的 hook WAIT。
3. Codex child/subagent transcript hooks 不覆寫父 pane。
4. Soft `?` 維持 👀，硬 WAIT 只來自 permission / elicitation / ui_prompt / ask tools。

## Claude 背景 shells（2026-10-22）

`Stop` 表示主回合停下；單獨的 `background_tasks` 或歷史訊息中的
`4 shells still running` 不足以把狀態改成 ⏳。Observer 現在要求三項證據：

1. 目前輸入提示下方的原生狀態列顯示 `N shell(s)`／`M monitor(s)`（含展開底部 subagent 列表時的游標列）。
2. 該 pane 的唯一原生 Claude 程序，確實有直接子 shell，且 wrapper 中的
   command 與 transcript 的 `Bash`／`Monitor` 背景任務相符。
3. 在可辨識的本輪使用者輸入之後，有背景 task ID 尚未收到完成、失敗、取消或 Monitor 逾時通知。

滿足時保留 `RUNNING`，使用既有的 `background_wait`／⏳；一般前景 spinner
仍顯示 🏃，原生 WAIT 與核准／回答畫面優先。新使用者輸入會切斷舊回合的任務關聯；
任務終止、程序消失或目前狀態列不再顯示 shells，完成畫面便回到 IDLE／待回看。
MCP 常駐程序、其他 session 的任務與舊回合的輪詢不計入。

Transcript 每次最多讀取尾端 2 MiB；持續觀測時以檔案身分、大小和修改時間快取，
增量讀取保留 task ID 與未完整寫入的最後一行，最多快取 32 份 transcript。
冷啟動或資料缺口使本輪身分無法確認時，維持原本完成畫面的判定，不猜測。
若 hook 綁定的 transcript 尚不存在，只允許使用該原生 shell wrapper 明確攜帶的
唯一 companion session ID 找到同目錄檔案，禁止選取同專案「最新」檔案代替。

只讀取同 session、同 task ID 的 `.output` 最後 8 KiB。輸出含錯誤時顯示檢查提示；
任務啟動與最後輸出都超過一小時時，提示「可能卡住，請檢查進度」。兩者都不宣告
任務失敗、不發出核准請求，也不終止程序。閾值是排查提示，並非執行期限。

回歸測試見 `test_claude_background.py`：本輪模擬、舊回合與常駐程序、完成／失敗通知、
恢復後的 session 身分、增量與截斷 transcript、輸出警示，以及 WAIT／前景優先順序。

## Codex 背景 terminals（2026-10-27）

Codex 透過 `unified_exec`（`tools.exec_command` / `exec_command`）啟動逾 `yield_time_ms`
的背景程序時，程序掛在 `codex app-server` 下而非 TUI 子程序樹，且背景程序結束時只會在
rollout JSONL 追加 `event_msg/item_completed`（`CommandExecution`），不會自動喚醒主回合。
當主回合 `task_complete` 結束但仍在等待背景任務時，Observer 交叉驗證：

1. 輸入提示 `›` 正上方的原生狀態列顯示 `N background terminal(s) running · /ps to view · /stop to close`。
2. Rollout transcript 本輪（依 `task_started` 與 `turn_id` 切分）有回傳 `session_id`（無 `exit_code`）
   且在 `task_complete` 前尚未收到對應 `process_id` 之 `CommandExecution` `item_completed`。
3. 排除本輪早期啟動的常駐 helper（與本輪最後一波未完成背景任務相差逾 120 秒，或距 `task_complete`
   逾一小時），只將最新活躍批次（active cohort）視為等待中的背景任務。

滿足時覆寫為 `RUNNING` + `Background work: waiting on N terminal(s)`（顯示 ⏳）；當最新批次的
背景任務在 `task_complete` 後寫入 `item_completed`（即使舊常駐 helper 如 `sshfs` 仍使畫面顯示
`1 background terminal running`），或畫面底部列消失時，自動轉回 `IDLE` + `needs_review`（👀）提醒驗收。

