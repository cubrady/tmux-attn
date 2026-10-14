# tmux Attention

**同時開一堆 coding agent 時，你不該再一個 pane 一個 pane 猜誰在等你。**

tmux Attention 讓你一眼知道：誰在問你、誰做完還沒看、誰還在跑——在 tmux 底列、手機或另一台電腦的網頁上都一樣。

支援 **Claude Code** · **Codex** · **Cursor CLI** · **Pi**。

主 repo：[`cubrady/tmux-attn`](https://github.com/cubrady/tmux-attn)。所有更新與 issue 集中在此；舊私人 repo `tmux-attention` 已封存。

---

## 解決什麼問題

| 痛點 | 怎麼解 |
| --- | --- |
| 多 agent 並行，注意力被打散 | 狀態分組：🙋 等你 · 👀 做完未讀 · 🏃 進行中 · 💤 閒置 |
| 人不在電腦前，錯過批准／問句 | 手機開網頁面板；可選 ntfy 只在新進入 🙋 時推播 |
| 想快速回一句，卻要先找對 pane | 點卡片 → 輸入／語音 → 貼進正確 pane |
| 多台工作機（本機 + CI / 實驗室） | 一台 server 聚合多 client，一頁看完整 fleet |

**不是**遠端遙控 IDE，也**不是**再包一層 LLM。狀態來自各 CLI 的 hooks（observer 只做交叉驗證），資料留在你的機器上。

---

## 30 秒印象

```text
tmux 底列    sampleapp 🏃 · wallet 👀 · lending 🙋
網頁面板     最新完成置頂 · Markdown 預覽 · 一鍵回覆 / 關 pane
```

- **看**：底列計數、popup 清單、fleet 卡片（含對話預覽與 git 環境）
- **動**：跳到該 pane、標記已看、從手機貼回覆
- **穩**：web 可用 launchd / systemd 常駐，掛掉會自己起來

「待看」保留 **未看 → 已看** 兩組，各組按最新訊息排序。網頁 header 的 👀 主數字
合計未看與已看，只在仍有未看時顯示「未看 N」；tmux 主計數仍只計未看。
已看的結果仍留在待看，新一輪工作會更新該 pane 的狀態。
點 tmux 底部視窗標籤、切換 pane／session，或從底列計數、清單 Enter、
`tmux-attn next` 跳轉後，會自動標記實際顯示的 pane 結果已看。多 pane 視窗只確認
選到的那個 pane。Web 開啟卡片並成功顯示可確認版本的內容，也會同步保存。
僅載入總覽、失敗的跳轉／預覽、背景或 control-mode 選擇，不會自動標記。

確認綁定實際開啟的內容版本：閱讀期間的新結果仍是未看。Web 展開後保留當下內容，
可按「重新整理內容」載入新訊息或重試儲存；跨主機確認保存在 pane 所在主機。
若只有舊快取或無法確認歸屬的預覽，保留未看，待最新內容就緒再確認。
「等待回覆／批准」看過仍須處理，保留在等待回覆。

更新既有安裝時可執行 `tmux-attn install-view-hooks` 補上一般切換的確認 hooks；
它會備份並保留既有 tmux 設定。Web 約每 10 秒同步 tmux 的已看狀態。

---

## 兩種用法

**Solo** — 一台機器跑 agent + web，瀏覽器開 `:7788`。

**Multi** — 若干 client（各跑 agent + 本機 feed）+ 一台 server（聚合給手機開）：

```text
client A ── /api/agents ──┐
client B ── /api/agents ──┼──▶ server /api/fleet ──▶ 手機
```

安全：**無內建登入**。只綁 Tailscale / 私有 LAN，不要暴露公網。

---

## 安裝

需要 Python ≥ 3.11、tmux ≥ 3.7、已登入的 agent CLI。

```sh
git clone https://github.com/cubrady/tmux-attn.git
cd tmux-attn
sh install.sh --solo          # 單機
# 多機 client：
# PEER_NAME='…' LOCAL_FEED_URL='http://$(tailscale ip -4):7788' \
#   SERVER_URL='http://<server>:7788' sh install.sh

sh install_web_service.sh     # 建議：常駐 web（macOS launchd / Linux systemd）
```

然後重開／resume agent session（或 Codex `/hooks`、Pi `/reload`），讓 hooks 掛上。

```sh
tmux attach                   # Prefix → a 開清單
# 手機：http://<這台或 server 的 IP>:7788/
```

更新：`git pull --ff-only && sh install.sh --no-clone`，必要時再跑一次 `install_web_service.sh`。

### 給 coding agent 的短 prompt

```text
Install tmux-attn on THIS machine. Tailscale/LAN only; never expose :7788 publicly. Never guess IPs.

Ask: SETUP=solo|multi
  solo → clone https://github.com/cubrady/tmux-attn.git && cd tmux-attn && sh install.sh --solo
  multi client → PEER_NAME, LOCAL_FEED_URL=http://$(tailscale ip -4):7788, SERVER_URL=http://<server>:7788
       clone … && cd tmux-attn
       PEER_NAME='…' LOCAL_FEED_URL='…' SERVER_URL='…' sh install.sh
Then EXACTLY follow printed NEXT STEPS:
  sh ~/.local/share/tmux-attn/install_web_service.sh   # required (not bare `tmux-attn web`)
  curl -s http://127.0.0.1:7788/api/health
  reload hooks (Claude new session | Codex /hooks | Cursor reopen | Pi /reload)
  verify: ~/.local/bin/tmux-attn status   # expect non-zero counts when an agent is live
Join uses POST SERVER_URL/api/peers (no SSH). Phone opens server:7788.
Report: install JSON (status_control_clicks), web health, peers, tmux status line.
```

| 你給 agent | 內容 |
| --- | --- |
| Solo | `SETUP=solo` |
| Multi client | `PEER_NAME`、本機 `LOCAL_FEED_URL`、`SERVER_URL` |
| Multi server | 常駐 web（建議 `install_web_service.sh`）+ `peers.json` |

---

## 常用操作

| 何處 | 做什麼 |
| --- | --- |
| Prefix → `a` | Popup：分組清單、預覽、標記已看、暫緩 |
| Prefix → `N` | 跳到第一個待處理 |
| 底列 🙋／👀／🏃／💤 | 點擊循環跳到該狀態 pane |
| 網頁卡片 | 開 sheet：看完整 Markdown、回覆、語音、關 pane（兩次確認） |

狀態語意：🙋 要你介入 · 👀 做完未讀（含 soft `?`）· 🏃 進行中 · 💤 已看／真閒置。跳過去**不會**自動標已看。

---

## 維運速查

```sh
sh install_web_service.sh --status
~/.local/bin/tmux-attn status
curl -s http://127.0.0.1:7788/api/health
```

若 Codex 視窗只有專案名、沒有狀態 emoji，先跑 `tmux-attn status`，再用
`tmux-attn dump` 確認 pane 是否已登記。status 刷新會自動恢復停止的 observer，
通常幾秒內就能重新建立對應；不需要重開 agent。observer 使用每個 tmux socket
各自的鎖，避免重複啟動及跨 server 讀錯 pane。

Codex 的 hooks 可能由共用 app-server 執行，無法從程序祖先直接找到 tmux pane；
這時需要 observer 驗證 CLI 與 session 的對應，再讓 hooks 更新正確的 pane。
若仍未恢復，檢查 `~/.local/state/tmux-attn/hook-errors.log` 與
`~/.local/state/tmux-attn/observer-errors.log`，並確認已重新載入 hooks。

| 路徑 | 用途 |
| --- | --- |
| `~/.local/bin/tmux-attn` | CLI |
| `~/.local/share/tmux-attn/` | 程式與 web |
| `~/.config/tmux-attn/peers.json` | 多機聚合 |
| `~/.config/tmux-attn/project-aliases.json` | 專案短名 |
| `~/.local/state/tmux-attn/inbox.sqlite3` | 狀態 DB |

別名、移植、ntfy、還原：見 `project-aliases.example.json`、`peers.example.json`、`notify.example.json`、`migrate.py`、`rollback.py`。

```sh
python3 -m unittest discover -v
node --test tests/web.test.cjs
python3 tests/popup_roundtrip.py
python3 tests/status_click_roundtrip.py
```

開發檢查（需另裝 Ruff、ShellCheck）：

```sh
ruff check --select F .
shellcheck bootstrap.sh install.sh install_web_service.sh
```

GitHub Actions 會在 push／PR 自動執行上述檢查，涵蓋 Python 3.11 與 3.14。
Python 整合測試使用臨時 HOME 和獨立 tmux server，驗證安裝、hooks、遷移與還原。
