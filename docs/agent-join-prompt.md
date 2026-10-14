# Agent join prompt

See [README.md](../README.md) → **給 coding agent 的短 prompt**.

Setups: **solo** or **multi** (server + clients).

Required after install (printed as NEXT STEPS):

1. `sh ~/.local/share/tmux-attn/install_web_service.sh` — not a one-shot `tmux-attn web`
2. `curl -s http://127.0.0.1:7788/api/health`
3. Reload agent hooks
4. `~/.local/bin/tmux-attn status` — live agents should show non-zero counts

Scripts: `install.sh`, `join_peer.py`, `install_web_service.sh`.
