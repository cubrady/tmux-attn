# Coding Agent 狀態偵測研調

盤點日期：2026-10-19。對照官方文件與 `tmux-attn` 實作，目標是更正確、精準地取得 WAIT / RUNNING / IDLE（±review）。

## 產品狀態模型

| DB `state` | UI | 意義 |
| --- | --- | --- |
| `WAIT` | 🙋 | 需要人核准或作答 |
| `RUNNING` | 🏃 / ⏳ | 工作中；⏳ = 明確在等背景 subagent |
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
