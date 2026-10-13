# tmux Attention

在 tmux 裡同時跑多個 coding agent 時，一眼看到**誰在工作、誰在等你、誰做完還沒看**，並可從手機／另一台機器的網頁面板監看與貼上簡易回覆。

支援 **Claude Code**、**Codex CLI**、**Cursor CLI（`agent`）**、**Pi**。設定裝在執行 tmux／agent 的工作機上；適合本機跑 agent，再經 Ghostty／SSH 使用。

開源 repo：`git@github.com:cubrady/tmux-attn.git`  
腳本：[install.sh](install.sh) · [join_peer.py](join_peer.py)（細節在腳本輸出，不必貼成長 prompt）。

---

## Copy-paste：Agent install prompt

GitHub 點 **Copy**，貼給 agent。細節由腳本印出的 `NEXT STEPS` 執行。

```text
Install tmux-attn on THIS machine (tmux status + hooks), start local web feed, join Attention Server.

Ask user if missing (STOP; never guess): PEER_NAME, LOCAL_FEED_URL (this host http://…:7788), AGGREGATOR=A|B|C
  A=user edits server peers  B=this machine is aggregator  C=remote (need AGGREGATOR_SSH)
Rules: Tailscale/LAN only; no public :7788; this host only serves /api/agents; no mutual --fleet loops.

Run:
  git clone git@github.com:cubrady/tmux-attn.git && cd tmux-attn
  PEER_NAME='…' LOCAL_FEED_URL='http://…:7788' AGGREGATOR=A sh install.sh
Then follow the script's NEXT STEPS (start `~/.local/bin/tmux-attn web`, curl health, reload hooks).
Mode B/C: install.sh → join_peer.py upserts or prints remote hint. Re-run join only: sh install.sh --join-only
Report: install OK, how web stays up, health OK, peers done or JSON given to user.
```

---

## 功能總覽

### tmux 底列與清單

| 功能 | 說明 |
| --- | --- |
| 狀態圖示 | 🙋 等待介入 · 🏃 工作中 · 👀 完成待看 · 💤 閒置無待看 |
| 專案別名 | 底列顯示短名（可設定）；多 pane 同專案共用別名 |
| 右側計數 | 例如 `🙋 1 · 👀 2 · 🏃 4 · 💤 1`，約每 2 秒刷新 |
| Popup 清單 | Prefix → `a` 分群列出已追蹤 agent；可預覽 pane、標記已看、暫緩 |
| 底列點擊巡覽 | 點 🙋／👀／🏃／💤 圖示或數字，循環跳到該狀態 pane（每 client 獨立游標） |
| 優先序 | 同窗多 agent：等待 → 待看 → 工作中 → 閒置 |

### 狀態來源與判斷

| 功能 | 說明 |
| --- | --- |
| Hooks 為主 | Claude／Codex／Cursor／Pi 事件寫入本機 SQLite；無額外 LLM 呼叫 |
| Observer 補強 | Codex／Claude／Cursor 唯讀交叉驗證程序與明確畫面；未知畫面不猜測 |
| Soft `?` | 回覆結尾 `?`／`？` → soft 👀（建議查看），**不**硬標 🙋；真正等待來自批准／AskUserQuestion／Permission 等 |
| `source` | 狀態來源標為 `hook`／`observer`／`shell`（可解釋、調試用） |
| Codex 非同步提問 | `Queued follow-up inputs` 優先 🙋；答完回到當時工作／完成態 |
| Cursor | 原生 `~/.cursor/hooks.json`；繼承的 Claude hooks 辨為 Cursor；畫面補強已驗證於 Cursor CLI 2026.10.01 |

### 手機／網頁面板（Attention）

| 功能 | 說明 |
| --- | --- |
| 本機 feed | 工作機 `tmux-attn web` → `GET /api/agents`（預設 `0.0.0.0:7788`） |
| 多機聚合 | 聚合機讀 `peers.json` → `GET /api/fleet`；瀏覽器只打 fleet |
| 注意力分組 | 🙋 → 👀 → 🏃 → 💤 → 💻 其他 pane |
| 對話預覽 | 卡片顯示最近 you×3 + agent 摘要（Claude／Codex／Cursor transcript；長度有限） |
| 環境列 | 專案別名為主；需要時顯示 repo 名、branch、dirty |
| 回覆 sheet | 點已追蹤卡片 → 底部輸入 → `POST /api/send` 貼到 pane（驗證 `pane_pid`；可轉發 peer）。**是 paste，不是遠端批准** |
| 語音輸入 | 支援瀏覽器 Web Speech 的裝置可對 sheet 唸稿 |
| 離線 host | 工作機休眠／web 掛掉時該 host 顯示 offline，其餘仍可用 |
| 可選 ntfy | 僅在 pane **新進入** 🙋 時推播（見 `notify.example.json`） |

**安全：** 無內建 auth。只暴露在 Tailscale／私有 LAN，勿開公網。

### 安裝與維運

| 功能 | 說明 |
| --- | --- |
| 一鍵 bootstrap | hooks、底列、別名、備份、依本機路徑產生設定 |
| 移植 | `migrate.py export` 帶走別名；目的機別名優先保留 |
| 還原 | `rollback.py` 還原該次安裝備份 |
| Agent 代裝 | 本頁短 prompt + `install.sh` / `join_peer.py` |

---

## 畫面與操作（tmux）

```text
sampleapp 🏃    wallet 👀    lending 🙋
```

| 圖示 | 含意 |
| --- | --- |
| 🙋 | 等待你回答、批准或下判斷 |
| 🏃 | 工作進行中 |
| 👀 | 閒置，有完成回覆尚未查看 |
| 💤 | 閒置，沒有待查看回覆 |

尚未建立狀態追蹤的 pane 只顯示專案名稱。預設 prefix 為 **Ctrl-b**（自訂則用你的）。

| 按鍵 | 功能 |
| --- | --- |
| Prefix → `a` | 開啟 popup，分群顯示已追蹤 agent |
| Prefix → `N` | 跳到第一個待處理 agent（大寫 N） |
| Popup `j`／`k` 或方向鍵 | 選擇 |
| Enter | 跳到 pane 並關閉 popup |
| `v` | 開啟／關閉所選 pane 預覽 |
| `d` | 標記已查看（👀 → 💤） |
| `1` | 暫緩該待處理項五分鐘 |
| `a` | 全部／僅待處理 |
| Esc／`q` | 關閉 |

跳到 pane **不會**自動標記已看；`d` 不代替批准或回覆。點擊底列計數巡覽同樣不標記已看。

---

## 架構：工作機 vs Attention Server

```text
工作機 A ── /api/agents ──┐
工作機 B ── /api/agents ──┼──▶ 聚合機（Attention Server）/api/fleet ──▶ 手機瀏覽器
工作機 C ── /api/agents ──┘
```

| 角色 | 跑什麼 | 誰開瀏覽器 |
| --- | --- | --- |
| **工作機** | `bootstrap`＋常駐 `tmux-attn web` | 可選；通常給聚合器刮 |
| **聚合機** | `peers.json`＋`tmux-attn web` 或 `--fleet` | 手機／別台開這台的 `:7788` |

- Peer **只**提供本機 `/api/agents`，**不要**互刮 `/api/fleet`（避免迴圈）。
- 單機也可直接開工作機 `:7788/`（fleet = 本機＋可選 peers）。

---

## 安裝

### 前置

- Python **≥ 3.11**
- tmux **≥ 3.7**（點擊計數需 control ranges；已在 3.7b 驗證）
- 已安裝並登入要用的 agent CLI
- 能 clone repo 的 GitHub 權限  
目前主要在 macOS 驗證。Pi 需支援 `agent_settled`／`ui_prompt_start` 等（已在 Pi 1.0.0 驗證）。

### 一鍵安裝

```sh
git clone git@github.com:cubrady/tmux-attn.git
cd tmux-attn
sh install.sh          # 呼叫 bootstrap.sh；結尾可提示 peer JSON
# 等同：sh bootstrap.sh
```

若已有 Attention 資訊，可讓結尾直接印出要貼上聚合機的 peer：

```sh
PEER_NAME='brad-mba' LOCAL_FEED_URL='http://100.x.x.x:7788' sh install.sh
```

會安裝到 `~/.local/share/tmux-attn/`、`~/.local/bin/tmux-attn`；合併 Claude／Codex／Cursor hooks、Pi extension；啟用 Codex `[features] hooks = true`；寫入 `sidebar.conf` 並 source 進 `~/.tmux.conf`；備份原設定。常駐左側：`sh install.sh --sidebar`。

```sh
tmux attach          # 若尚未在 tmux
# Ctrl-b → a         開清單
```

更新：`git pull --ff-only && sh install.sh --no-clone`（或 `sh bootstrap.sh`）。

### 讓 agent 載入 hooks

| CLI | 建議 |
| --- | --- |
| Claude Code | 新 session 即可；舊的重開／resume |
| Codex | `/hooks` 檢查並信任；舊 session 重開／resume |
| Cursor `agent` | 確認 `~/.cursor/hooks.json`；舊 session 重開 |
| Pi | `/reload` 或重開 |

空白 CLI 尚未開工時可能還沒有事件。請 agent 代裝：複製上方短 prompt（步驟在 `install.sh`／`join_peer.py`）。

---

## 本機 Web feed 與聚合

### 工作機

```sh
~/.local/bin/tmux-attn web          # 預設 0.0.0.0:7788；也可 web 7789
curl -s http://127.0.0.1:7788/api/health
curl -s http://127.0.0.1:7788/api/agents | head
```

可選環境變數：`TMUX_ATTN_HOST`（面板短名）、`TMUX_ATTN_SOCKET`、`TMUX_ATTN_STATE_DIR`、`TMUX_ATTN_NOTIFY`。

長期常駐可用 launchd／systemd／tmux pane（專案尚未內建 service 檔）。

### Attention Server（聚合機）

```sh
mkdir -p ~/.config/tmux-attn
cp peers.example.json ~/.config/tmux-attn/peers.json
# 編輯 name / url（各工作機 web 根網址）

~/.local/bin/tmux-attn web --fleet   # VPS：只聚合、不讀本機 tmux
# 或在工作機兼聚合：tmux-attn web
```

手機開：`http://<聚合機 Tailscale IP 或主機名>:7788/`

### 可選 ntfy

僅 **新進入** 🙋 時推送。複製 [notify.example.json](notify.example.json) → `~/.config/tmux-attn/notify.json`，設 `enabled: true` 與私有 topic。

---

## 專案別名

編輯 `~/.config/tmux-attn/project-aliases.json`（約兩秒生效）。完整路徑優先，其次資料夾名；未設定則用資料夾名（過長截斷）。範例：[project-aliases.example.json](project-aliases.example.json)。

---

## 移植與還原

```sh
# 舊機：帶走個人別名
python3 migrate.py export dist/tmux-attn-migration.tar.gz
# 新機解壓後 sh bootstrap.sh
```

不包含憑證、對話或執行狀態。目的機已有別名優先。

```sh
python3 rollback.py /path/to/printed/backup
```

還原整份備份設定；安裝後若又改過請先另存。Agent 需重載 hooks。

---

## 路徑速查

| 位置 | 用途 |
| --- | --- |
| `~/.local/bin/tmux-attn` | CLI |
| `~/.local/share/tmux-attn/` | 安裝後程式與 `web/` |
| `~/.config/tmux-attn/sidebar.conf` | 底列／快捷鍵 |
| `~/.config/tmux-attn/project-aliases.json` | 別名 |
| `~/.config/tmux-attn/peers.json` | 聚合 peers |
| `~/.config/tmux-attn/notify.json` | 可選 ntfy |
| `~/.local/state/tmux-attn/inbox.sqlite3` | 狀態 DB |
| `~/.local/state/tmux-attn/hook-errors.log` | hook 錯誤 |
| `~/.local/state/tmux-attn/observer-errors.log` | observer 錯誤 |

```sh
~/.local/bin/tmux-attn status
~/.local/bin/tmux-attn dump
```

---

## 開發與測試

```sh
python3 -m unittest test_attention.py test_codex_observer.py test_migration.py
python3 tests/popup_roundtrip.py
python3 tests/status_click_roundtrip.py
```

調研衍生項：[docs/survey-actions.md](docs/survey-actions.md)。原始 polling starter 在 `legacy/`。

---

## 判斷細節（摘要）

- 狀態主要來自 hooks；Codex／Claude／Cursor observer 交叉驗證主對話身分與明確畫面。子 agent 事件不覆蓋主狀態。
- 主回覆完成後即使背景 shell 仍跑，顯示 👀；背景說明可出現在 popup。
- Codex `resume`：用 cwd 與對話名核對綁定；名稱截斷或不唯一時不猜測。
- 網頁預覽：Claude（`~/.claude/projects`）、Codex rollout、Cursor（`~/.cursor/projects/…/agent-transcripts`）；純 CLI 且無 transcript／hook 文字時可能仍空白。
