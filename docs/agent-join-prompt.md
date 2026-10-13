# Agent prompt：安裝本機 tmux-attn 並上線到 Attention Server

把下面整段（從 `---` 到結尾）貼給 coding agent 執行。  
**使用者必須先提供「上線所需的 server／網路資訊」**；缺資料時 agent 應停下詢問，不要猜測。

---

## 任務

在**這台工作機**完成：

1. Clone 並安裝 tmux-attn（底列狀態＋agent hooks）— 優先用 repo 的 `install.sh`
2. 常駐本機 web feed（`/api/agents`）
3. 讓本機出現在 **Attention Server**（聚合機）的手機／網頁面板上

- Repo（SSH）：`git@github.com:cubrady/tmux-attn.git`
- 功能說明：checkout 後讀 `README.md`
- 安裝腳本：`install.sh`（clone＋`bootstrap.sh`＋印出 peer JSON 提示）

## 硬性規則

- **無內建 auth**：只在 Tailscale／私有 LAN；禁止把 `:7788` 暴露到公網。
- **不要猜** Tailscale IP、聚合機 hostname、peers 路徑、SSH 目標。缺了就問使用者。
- 本機只跑 **`tmux-attn web`**（`/api/agents`）。聚合機才讀 `peers.json`／fleet。  
  **不要**兩台互相 `--fleet` 又互列 peers。
- 改聚合機設定前先確認授權（本機寫入／SSH／或只產出 JSON 給使用者貼）。
- 不提交 secrets；不改無關檔案。

## 執行前必問（缺一不可就 STOP）

| 欄位 | 說明 | 範例 |
| --- | --- | --- |
| `PEER_NAME` | 面板上的短名 | `brad-mba` |
| `LOCAL_FEED_URL` | **本機** web 根網址 | `http://100.91.179.5:7788` |
| `AGGREGATOR` | Attention Server 怎麼改 peers | A／B／C（見下） |
| 可選 `TMUX_ATTN_HOST` | `/api/agents` 的 host 標籤 | 同 `PEER_NAME` |

### `AGGREGATOR` 模式

- **A** — 使用者自己改 server：本機裝好＋開 web，交付 peer JSON。
- **B** — 本機就是聚合機：寫本機 `~/.config/tmux-attn/peers.json`。
- **C** — Agent 可改遠端：使用者給 SSH 目標、遠端 peers 路徑、是否重啟 `web --fleet`。

只說「上線」卻沒給上表 → **列出缺項並停止**。

## 步驟（依序執行並驗證）

### 1) 前置檢查

```sh
python3 --version    # ≥ 3.11
tmux -V              # ≥ 3.7（底列點擊需 control ranges）
```

需能 `git clone` GitHub。CLI（Claude／Codex／Cursor／Pi）建議已登入。不足就告訴使用者，不要默默跳過。

### 2) Clone ＋安裝（一定用這條路徑）

**優先（推薦）：**

```sh
git clone git@github.com:cubrady/tmux-attn.git
cd tmux-attn
# 若使用者已給 PEER_NAME / LOCAL_FEED_URL，可一併帶上以便結尾印出 peer JSON：
PEER_NAME='…' LOCAL_FEED_URL='http://…:7788' sh install.sh
```

常駐左側清單：`sh install.sh --sidebar`。

**已有 checkout：**

```sh
cd /path/to/tmux-attn
git pull --ff-only
sh install.sh --no-clone
```

**從任意目錄讓 `install.sh` 自己 clone**（可選）：

```sh
# 下載腳本後執行；或先 clone 再跑（更清楚，建議 agent 用上面「優先」）
TMUX_ATTN_DIR="$HOME/src/tmux-attn" sh -c '
  git clone git@github.com:cubrady/tmux-attn.git "$TMUX_ATTN_DIR" 2>/dev/null || git -C "$TMUX_ATTN_DIR" pull --ff-only
  sh "$TMUX_ATTN_DIR/install.sh" --no-clone
'
```

`install.sh` 會呼叫 `bootstrap.sh`：寫入 `~/.local/share/tmux-attn/`、`~/.local/bin/tmux-attn`，合併 hooks，source 底列設定。

**不要**只複製單一檔案；以官方 checkout＋`install.sh`／`bootstrap.sh` 為準。

### 3) 載入 hooks

| CLI | 動作 |
| --- | --- |
| Claude Code | 新 session；舊的重開／resume |
| Codex | `/hooks` 信任；舊 session 重開 |
| Cursor `agent` | 確認 `~/.cursor/hooks.json`；舊 session 重開 |
| Pi | `/reload` 或重開 |

```sh
~/.local/bin/tmux-attn status
~/.local/bin/tmux-attn dump
```

底列應有別名與 🙋／👀／🏃／💤；Prefix **Ctrl-b → a** 開清單。

### 4) 啟動本機 web feed

```sh
export TMUX_ATTN_HOST="${TMUX_ATTN_HOST:-$PEER_NAME}"
~/.local/bin/tmux-attn web
```

驗證：

```sh
curl -s http://127.0.0.1:7788/api/health
curl -s http://127.0.0.1:7788/api/agents | head
curl -s "$LOCAL_FEED_URL/api/health"    # 從同 tailnet 另一台更準
```

長期常駐：launchd／systemd／tmux pane（專案無內建 service）。機器休眠＝該 host 離線。

只印 peer JSON（已裝過時）：

```sh
PEER_NAME='…' LOCAL_FEED_URL='http://…:7788' sh install.sh --print-peer-only
```

### 5) 上線到 Attention Server

Peer 物件：

```json
{ "name": "PEER_NAME", "url": "LOCAL_FEED_URL" }
```

- **A**：把 JSON 交給使用者併入聚合機 `~/.config/tmux-attn/peers.json`。
- **B／C**：更新 peers（同名則改 url，否則 append）；確保聚合端 `tmux-attn web` 或 `web --fleet` 在跑。
- `url`＝工作機 **web 根**（聚合器打 `url + /api/agents`），不是 fleet URL。

手機：`http://<聚合機 Tailscale>:7788/` → 應看到新 host。

### 6) 完成回報

- [ ] `git clone`＋`sh install.sh` 成功  
- [ ] 底列有狀態；hooks 已提醒重載  
- [ ] `tmux-attn web` 常駐方式  
- [ ] `LOCAL_FEED_URL` health OK  
- [ ] peers 已更新或已交付 JSON  
- [ ] 手機聚合 URL  

## 可選（使用者要求才做）

- 別名：`~/.config/tmux-attn/project-aliases.json`
- ntfy：`notify.example.json` → `notify.json`
- 舊機別名：`python3 migrate.py export …`

## 常見失敗

| 現象 | 處理 |
| --- | --- |
| clone 失敗 | SSH key／GitHub 權限 |
| connection refused | 確認是**工作機** web、聽 `0.0.0.0:7788` |
| 聚合看不到 | peers 漏寫、URL 錯、刮到 fleet |
| 底列沒狀態 | hooks 未重載；`~/.local/state/tmux-attn/hook-errors.log` |
