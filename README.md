# tmux Attention

在 tmux 裡同時使用多個 coding agent 時，讓你一眼看到**哪些正在工作、哪些需要回覆、哪些已完成但尚未查看**，減少來回巡查視窗。

支援 Claude Code、Codex CLI 和 Pi。適合在 Mac 工作機上執行 agent，再從 Ghostty 透過 SSH 連線使用；設定應安裝在執行 tmux／agent 的工作機上。

## 畫面與操作

底部視窗列以「專案別名＋圖示」顯示狀態，左側選單不常駐：

```text
minday 🏃    wallet 👀    lending 🙋
```

| 圖示 | 含意 |
| --- | --- |
| 🙋 | 等待你回答、批准或下判斷 |
| 🏃 | 工作進行中 |
| 👀 | 閒置，有完成回覆尚未查看 |
| 💤 | 閒置，沒有待查看回覆 |

底部右側會顯示總數，例如 `🙋 1 · 👀 2 · 🏃 4 · 💤 1`，四個數量分別表示等待介入、回覆待查看、工作中，以及沒有待查看回覆的閒置 agent。底部約每兩秒更新，popup 約每秒更新。

同一個視窗有多個 agent 時，優先顯示：等待回覆 → 尚未查看 → 工作中 → 閒置。尚未建立狀態追蹤的 pane 會先顯示專案名稱，不加狀態圖示。

預設 tmux prefix 為 **Ctrl-b**；若你有自訂 prefix，請使用自己的設定。

| 按鍵 | 功能 |
| --- | --- |
| Prefix → `a` | 開啟 popup，分群顯示所有已追蹤 agent |
| Prefix → `N` | 跳到第一個待處理 agent（大寫 N） |
| Popup 中 `j` / `k` 或方向鍵 | 選擇項目 |
| Enter | 跳到對應 pane 並關閉 popup |
| `v` | 開啟／關閉所選 pane 的預覽 |
| `d` | 標記已查看，將 `👀` 改為 `💤` |
| `1` | 暫緩這一筆待處理項目五分鐘 |
| `a` | 切換全部 agent／僅待處理項目 |
| Esc / `q` | 關閉 popup |

跳到 pane 不會自動標記已查看。`d` 只標記你已看過這次回覆，不會代替你批准操作或回覆 agent；下一次完成回覆會再次顯示 `👀`。

## 安裝

### 前置需求

- Python **3.11 或更新版本**。
- 支援 `display-popup`、`split-window -e` 和 hooks 的 tmux，已在 **tmux 3.7b** 驗證。
- 已安裝並登入你要使用的 agent CLI。
- clone 本 private repo 所需的 GitHub 存取權限。

目前主要在 macOS 驗證。Pi 整合需支援 `agent_settled` 與 `ui_prompt_start` 等事件，已在 Pi 1.0.0 驗證。

### 一個指令安裝

先取得專案，再執行 bootstrap：

```sh
git clone git@github.com:cubrady/tmux-attention.git
cd tmux-attention
sh bootstrap.sh
```

Bootstrap 會檢查 Python／tmux，並在尚無 tmux session 時建立 `work` session。安裝程式會：

- 安裝 popup、底部圖示及專案別名。
- 合併 Claude／Codex hooks，加入 Pi extension。
- 啟用 Codex 的 `[features] hooks = true`。
- 備份原設定，保留其他 hooks 與目的機已有的別名。
- 依新機器的使用者目錄和 Python 位置產生路徑。

Bootstrap 不會安裝前置軟體或代替 agent 登入。若既有 Codex TOML 使用無法安全編輯的 inline／dotted 寫法，安裝會提示你手動啟用 hooks。

若尚未進入 tmux，可執行：

```sh
tmux attach
```

接著按 **Ctrl-b → a** 開啟清單。

### 讓 agent 載入設定

- **Claude Code**：新 session 會使用已安裝的 hooks；舊 session 若沒有更新，請在方便時重新啟動／resume。
- **Codex**：使用 `/hooks` 檢查並載入 command hooks；首次可能需要信任這些命令。舊 session 若未載入，請重新啟動／resume。
- **Pi**：舊 session 執行 `/reload`，或重新啟動／resume。

空白 CLI 尚未開始任務時，可能還沒有狀態事件。系統也會唯讀追蹤可確認身分的既有 Codex 對話；名稱被截斷或對應不唯一時，會保留未配對狀態。

### 更新與其他顯示模式

```sh
git pull --ff-only
sh bootstrap.sh
```

每次安裝都會建立新的備份。若偏好常駐左側清單，可改用：

```sh
sh bootstrap.sh --sidebar
```

## 專案別名

編輯 `~/.config/tmux-attn/project-aliases.json`，約兩秒內生效：

```json
{
  "aliases": {
    "web-app-minday": "minday",
    "llm-wallet-gateway": "wallet",
    "/Users/your-name/projects/api": "my-api"
  }
}
```

完整路徑 mapping 優先，其次是資料夾名稱 mapping。沒有設定的新專案會自動顯示資料夾名稱；超過 12 字元時截短並加 `…`。不同專案名稱撞到時，優先加可讀後綴，否則使用穩定的短識別碼。同一專案的多個視窗沿用相同別名。

預設 mapping 參見 [project-aliases.example.json](project-aliases.example.json)。Popup 保留完整專案名稱，不受底部縮寫影響。

## 移植到另一台機器

直接 clone repo 並執行 `sh bootstrap.sh` 即可安裝預設設定。若要一起帶走**目前的個人別名**，在原機器匯出：

```sh
python3 migrate.py export dist/tmux-attn-migration.tar.gz
```

將壓縮檔複製到新工作機器，再執行：

```sh
tar -xzf tmux-attn-migration.tar.gz
cd tmux-attn
sh bootstrap.sh
```

匯出檔包含程式、安裝／還原工具、測試及目前別名，不包含 agent 帳號憑證、對話紀錄或執行狀態。目的機已有的別名優先保留，匯入檔只補上缺少項目。

資料夾名稱 mapping 可沿用；完整路徑 mapping 需依新機器調整。重新匯出請換新檔名，工具不會覆蓋同名壓縮檔。

## 還原

安裝完成會輸出 `backup` 路徑。使用該次安裝輸出的完整路徑還原：

```sh
python3 rollback.py /path/to/printed/backup
```

還原會恢復備份中的設定和快捷鍵，保留工作 pane。它會還原整份設定檔；若安裝後又做過修改，請先保存那些修改。Agent 也需重新載入還原後的 hooks／extension。

## 運作方式與判斷範圍

狀態主要來自 agent hooks／Pi 事件，保存在本機 SQLite。Codex 相容監控會唯讀讀取本機對話事件及必要的 pane 畫面，交叉驗證主對話身分；子 agent 的事件不會覆蓋主 agent 狀態。這些判斷不需要額外 LLM 呼叫。

一般完成回覆會成為未查看項目。若回覆最後以 `?` 或 `？` 結尾，目前會推測為等待回答；這是啟發式判斷，可能把反問誤判。明確的批准／輸入事件會直接標記為等待回覆。

`🏃` 表示主回合正在工作。主回覆完成後，即使背景 shell／dev server 還在執行，也會顯示 `👀`；背景工作的存在會另外顯示在 popup 說明中。

同一 CLI 程序內切換 `/new` 或 `/resume`、且未發出可定位的原生 hooks 時，追蹤可能無法跟上；請重新載入 hooks，或重新啟動並 resume。Codex 相容監控依賴本機資料格式，無法確認的對應不會猜測。

## 檔案與檢查

| 位置 | 用途 |
| --- | --- |
| `~/.local/bin/tmux-attn` | 開啟 popup 或查詢狀態 |
| `~/.local/share/tmux-attn/` | 安裝後的程式 |
| `~/.config/tmux-attn/sidebar.conf` | tmux 顯示及快捷鍵設定 |
| `~/.config/tmux-attn/project-aliases.json` | 個人別名 |
| `~/.local/state/tmux-attn/inbox.sqlite3` | 狀態及已查看標記 |
| `~/.local/state/tmux-attn/backups/` | 安裝備份 |

查詢狀態與診斷資料：

```sh
~/.local/bin/tmux-attn status
~/.local/bin/tmux-attn dump
```

Hooks／監控錯誤會記錄在 state 目錄中的 `hook-errors.log`／`observer-errors.log`。

開發驗證使用隔離的 tmux server，涵蓋狀態事件、resume 配對、查看標記、別名、移植及還原：

```sh
python3 -m unittest test_attention.py test_codex_observer.py test_migration.py
python3 tests/popup_roundtrip.py
```

Repo 追蹤程式、測試、文件及預設別名；本機狀態、匯出檔和個人紀錄由 `.gitignore` 排除。原始 polling starter 保留在 `legacy/` 作為歷史參考。
