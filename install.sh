#!/bin/sh
# Clone (if needed) + bootstrap. Optional multi-client join via join_peer.py.
#
# Solo:
#   sh install.sh
#   python3 join_peer.py --solo
#
# Multi client:
#   PEER_NAME=client-2 LOCAL_FEED_URL=http://100.x:7788 \
#     SERVER_SSH=dev@100.64.0.11 sh install.sh
#
# Env: SETUP=solo|multi (default: multi if PEER_NAME set, else solo tips)
#      PEER_NAME, LOCAL_FEED_URL, SERVER_SSH (or AGGREGATOR_SSH), TMUX_ATTN_DIR, TMUX_ATTN_REPO
# Flags: --sidebar  --no-clone  --join-only  --check  --solo
set -eu

REPO_DEFAULT='git@github.com:cubrady/tmux-attn.git'
REPO="${TMUX_ATTN_REPO:-$REPO_DEFAULT}"
SIDEBAR=0
NO_CLONE=0
JOIN_ONLY=0
CHECK_ONLY=0
SOLO=0

for arg in "$@"; do
  case "$arg" in
    --sidebar) SIDEBAR=1 ;;
    --no-clone) NO_CLONE=1 ;;
    --join-only|--print-peer-only) JOIN_ONLY=1 ;;
    --check) CHECK_ONLY=1 ;;
    --solo) SOLO=1 ;;
    -h|--help)
      sed -n '2,16p' "$0"
      exit 0
      ;;
    *)
      echo "Unknown flag: $arg" >&2
      exit 2
      ;;
  esac
done

is_checkout() {
  [ -f "$1/bootstrap.sh" ] && [ -f "$1/attention.py" ] && [ -f "$1/install.py" ] && [ -f "$1/join_peer.py" ]
}

check_prereqs() {
  ok=1
  if ! command -v python3 >/dev/null 2>&1 || ! python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'; then
    echo 'Need Python ≥ 3.11' >&2
    ok=0
  else
    echo "python3: $(python3 --version 2>&1)"
  fi
  if ! command -v tmux >/dev/null 2>&1; then
    echo 'Need tmux ≥ 3.7' >&2
    ok=0
  else
    echo "tmux: $(tmux -V 2>&1)"
  fi
  if ! command -v git >/dev/null 2>&1; then
    echo 'Need git' >&2
    ok=0
  fi
  [ "$ok" -eq 1 ]
}

run_join() {
  root=$1
  if [ "$SOLO" -eq 1 ] || [ "${SETUP:-}" = 'solo' ]; then
    python3 "$root/join_peer.py" --solo
    return 0
  fi
  if [ -z "${PEER_NAME:-}" ] || [ -z "${LOCAL_FEED_URL:-}" ]; then
    echo ""
    echo "Solo?  python3 $root/join_peer.py --solo"
    echo "Multi client? set PEER_NAME + LOCAL_FEED_URL (+ optional SERVER_SSH=user@host), then:"
    echo "  PEER_NAME=… LOCAL_FEED_URL=http://…:7788 sh install.sh --join-only"
    return 0
  fi
  export PEER_NAME LOCAL_FEED_URL
  [ -n "${SERVER_SSH:-}" ] && export SERVER_SSH
  [ -n "${AGGREGATOR_SSH:-}" ] && export AGGREGATOR_SSH
  [ -n "${PEERS_PATH:-}" ] && export PEERS_PATH
  python3 "$root/join_peer.py"
}

if [ "$CHECK_ONLY" -eq 1 ]; then
  check_prereqs
  exit $?
fi

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

if [ "$JOIN_ONLY" -eq 1 ]; then
  if is_checkout "$script_dir"; then
    run_join "$script_dir"
  elif [ -n "${TMUX_ATTN_DIR:-}" ] && is_checkout "$TMUX_ATTN_DIR"; then
    run_join "$TMUX_ATTN_DIR"
  else
    echo 'Run from a tmux-attn checkout.' >&2
    exit 1
  fi
  exit 0
fi

check_prereqs || exit 1

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

echo ""
echo "Local install OK. CLI: ${HOME}/.local/bin/tmux-attn"
run_join "$root"
