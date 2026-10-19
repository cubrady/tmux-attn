#!/bin/bash
# launchd entrypoint: warm identity before Python/CF init when Open Directory is slow.
set -euo pipefail
HOST="${1:-0.0.0.0}"
PORT="${2:-7788}"
: "${HOME:?HOME must be set by launchd}"
: "${USER:=}"
: "${LOGNAME:=}"
if [ -z "$USER" ]; then USER=$(id -un 2>/dev/null || echo ''); fi
if [ -z "$LOGNAME" ]; then LOGNAME=$USER; fi
export HOME USER LOGNAME
mkdir -p "${TMPDIR:-/tmp}"
# Soft identity warm-up with a short timeout so a stuck DirectoryService cannot block forever.
if command -v python3 >/dev/null 2>&1; then
  python3 - <<'PY' || true
import os, signal
signal.alarm(3)
try:
    import pwd
    pwd.getpwuid(os.getuid())
except Exception:
    pass
PY
fi
exec "${HOME}/.local/bin/tmux-attn" web "$HOST" "$PORT"
