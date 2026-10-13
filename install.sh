#!/bin/sh
# Clone (if needed) + bootstrap local tmux-attn status.
# Optional: print Attention Server peer JSON when PEER_NAME + LOCAL_FEED_URL are set.
#
# Typical (fresh machine):
#   git clone git@github.com:cubrady/tmux-attn.git
#   cd tmux-attn
#   sh install.sh
#
# One-liner from anywhere:
#   curl -fsSL https://raw.githubusercontent.com/cubrady/tmux-attn/main/install.sh | sh
#   # or with SSH clone preferred by agents — see docs/agent-join-prompt.md
#
# Env (optional):
#   TMUX_ATTN_DIR     Clone/install checkout path (default: $PWD if already a checkout, else ./tmux-attn)
#   TMUX_ATTN_REPO    git URL (default: git@github.com:cubrady/tmux-attn.git)
#   PEER_NAME         Short name for fleet panel
#   LOCAL_FEED_URL    This machine's web root, e.g. http://100.x.x.x:7788
#   TMUX_ATTN_HOST    Host label for /api/agents (optional)
#
# Flags:
#   --sidebar         Pass through to bootstrap (persistent left list)
#   --no-clone        Fail if this script is not run from an existing checkout
#   --print-peer-only Skip bootstrap; only print peer JSON (needs env)
set -eu

REPO_DEFAULT='git@github.com:cubrady/tmux-attn.git'
REPO="${TMUX_ATTN_REPO:-$REPO_DEFAULT}"
SIDEBAR=0
NO_CLONE=0
PRINT_PEER_ONLY=0

for arg in "$@"; do
  case "$arg" in
    --sidebar) SIDEBAR=1 ;;
    --no-clone) NO_CLONE=1 ;;
    --print-peer-only) PRINT_PEER_ONLY=1 ;;
    -h|--help)
      sed -n '2,25p' "$0"
      exit 0
      ;;
    *)
      echo "Unknown flag: $arg (supported: --sidebar --no-clone --print-peer-only)" >&2
      exit 2
      ;;
  esac
done

print_peer() {
  name="${PEER_NAME:-}"
  url="${LOCAL_FEED_URL:-}"
  if [ -z "$name" ] || [ -z "$url" ]; then
    echo ""
    echo "To join Attention Server, set PEER_NAME and LOCAL_FEED_URL, then rerun:"
    echo "  PEER_NAME=my-mac LOCAL_FEED_URL=http://100.x.x.x:7788 sh install.sh --print-peer-only"
    echo "Or ask the user for those values (do not guess)."
    return 0
  fi
  echo ""
  echo "Add this object to the aggregator's ~/.config/tmux-attn/peers.json → peers[]:"
  printf '  { "name": "%s", "url": "%s" }\n' "$name" "$url"
  echo ""
  echo "Then ensure aggregator runs: tmux-attn web   OR   tmux-attn web --fleet"
  echo "Phone URL: http://<aggregator-tailscale-host>:7788/"
}

if [ "$PRINT_PEER_ONLY" -eq 1 ]; then
  print_peer
  exit 0
fi

is_checkout() {
  [ -f "$1/bootstrap.sh" ] && [ -f "$1/attention.py" ] && [ -f "$1/install.py" ]
}

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
root=''

if is_checkout "$script_dir"; then
  root=$script_dir
elif [ -n "${TMUX_ATTN_DIR:-}" ] && is_checkout "$TMUX_ATTN_DIR"; then
  root=$TMUX_ATTN_DIR
elif is_checkout "${TMUX_ATTN_DIR:-./tmux-attn}"; then
  root=$(CDPATH= cd -- "${TMUX_ATTN_DIR:-./tmux-attn}" && pwd)
elif [ "$NO_CLONE" -eq 1 ]; then
  echo "Not inside a tmux-attn checkout and --no-clone was set." >&2
  exit 1
else
  dest="${TMUX_ATTN_DIR:-$PWD/tmux-attn}"
  if [ -e "$dest" ] && ! is_checkout "$dest"; then
    echo "Path exists but is not a tmux-attn checkout: $dest" >&2
    exit 1
  fi
  if [ ! -e "$dest" ]; then
    echo "Cloning $REPO → $dest"
    git clone "$REPO" "$dest"
  else
    echo "Using existing checkout $dest (git pull --ff-only)"
    git -C "$dest" pull --ff-only
  fi
  root=$(CDPATH= cd -- "$dest" && pwd)
fi

echo "Installing from $root"
cd "$root"
if [ "$SIDEBAR" -eq 1 ]; then
  sh ./bootstrap.sh --sidebar
else
  sh ./bootstrap.sh
fi

bin="${HOME}/.local/bin/tmux-attn"
echo ""
echo "Local install OK."
echo "  CLI: $bin"
echo "  Start feed:  ${TMUX_ATTN_HOST:+TMUX_ATTN_HOST=$TMUX_ATTN_HOST }$bin web"
echo "  Health:      curl -s http://127.0.0.1:7788/api/health"
echo "  Reload agent hooks (Claude new session / Codex /hooks / Cursor reattach / Pi /reload)."
print_peer
