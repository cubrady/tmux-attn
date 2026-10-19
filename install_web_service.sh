#!/bin/sh
# Install a persistent tmux-attn web service (macOS launchd / Linux systemd --user).
#
#   sh install_web_service.sh           # install + start
#   sh install_web_service.sh --status
#   sh install_web_service.sh --stop
#   sh install_web_service.sh --uninstall
#
# Env: TMUX_ATTN_WEB_HOST (default 0.0.0.0), TMUX_ATTN_WEB_PORT (default 7788)
set -eu

HOST="${TMUX_ATTN_WEB_HOST:-0.0.0.0}"
PORT="${TMUX_ATTN_WEB_PORT:-7788}"
LABEL='ai.tmux-attn.web'
UNIT='tmux-attn-web.service'
ACTION='install'

for arg in "$@"; do
  case "$arg" in
    --status) ACTION=status ;;
    --stop) ACTION=stop ;;
    --uninstall) ACTION=uninstall ;;
    -h|--help)
      sed -n '2,12p' "$0"
      exit 0
      ;;
    *)
      echo "Unknown flag: $arg" >&2
      exit 2
      ;;
  esac
done

script_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
bin="${HOME}/.local/bin/tmux-attn"
uid=$(id -u)

if [ ! -x "$bin" ]; then
  echo "Missing $bin — run install.sh / bootstrap first." >&2
  exit 1
fi

mkdir -p "${HOME}/.local/state/tmux-attn"

stop_ad_hoc() {
  # Stop temporary tmux/nohup web so the service owns :PORT.
  if command -v tmux >/dev/null 2>&1; then
    tmux kill-session -t attn-web 2>/dev/null || true
  fi
  # Best-effort: free the port if something else is bound.
  if command -v lsof >/dev/null 2>&1; then
    pids=$(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true)
    if [ -n "${pids:-}" ]; then
      # shellcheck disable=SC2086
      kill $pids 2>/dev/null || true
      sleep 0.4
    fi
  fi
}

darwin_plist_path() {
  echo "${HOME}/Library/LaunchAgents/${LABEL}.plist"
}

darwin_domain() {
  echo "gui/${uid}/${LABEL}"
}

linux_unit_path() {
  echo "${HOME}/.config/systemd/user/${UNIT}"
}

case "$(uname -s)" in
  Darwin)
    case "$ACTION" in
      status)
        launchctl print "$(darwin_domain)" 2>/dev/null | head -40 || echo 'not loaded'
        curl -s -m 2 "http://127.0.0.1:${PORT}/api/health" || true
        echo
        ;;
      stop)
        launchctl bootout "$(darwin_domain)" 2>/dev/null || true
        echo "stopped ${LABEL}"
        ;;
      uninstall)
        launchctl bootout "$(darwin_domain)" 2>/dev/null || true
        rm -f "$(darwin_plist_path)"
        echo "uninstalled ${LABEL}"
        ;;
      install)
        stop_ad_hoc
        plist=$(darwin_plist_path)
        mkdir -p "$(dirname "$plist")"
        mkdir -p "${HOME}/Library/Caches/tmux-attn-tmp"
        mkdir -p "${HOME}/.local/share/tmux-attn/contrib/launchd"
        if [ -f "$script_dir/contrib/launchd/web-run.sh" ]; then
          cp "$script_dir/contrib/launchd/web-run.sh" "${HOME}/.local/share/tmux-attn/contrib/launchd/web-run.sh"
          chmod +x "${HOME}/.local/share/tmux-attn/contrib/launchd/web-run.sh"
        fi
        user_name=$(id -un)
        sed -e "s|__HOME__|${HOME}|g" \
            -e "s|__USER__|${user_name}|g" \
            -e "s|0\.0\.0\.0|${HOST}|g" \
            -e "s|7788|${PORT}|g" \
            "$script_dir/contrib/launchd/ai.tmux-attn.web.plist" > "$plist"
        launchctl bootout "$(darwin_domain)" 2>/dev/null || true
        launchctl bootstrap "gui/${uid}" "$plist"
        launchctl enable "$(darwin_domain)" 2>/dev/null || true
        launchctl kickstart -k "$(darwin_domain)"
        sleep 0.6
        echo "installed launchd ${LABEL}"
        echo "  plist: $plist"
        echo "  logs:  ${HOME}/.local/state/tmux-attn/web.{out,err}.log"
        curl -s -m 3 "http://127.0.0.1:${PORT}/api/health" || true
        echo
        ;;
    esac
    ;;
  Linux)
    case "$ACTION" in
      status)
        systemctl --user status "$UNIT" --no-pager || true
        curl -s -m 2 "http://127.0.0.1:${PORT}/api/health" || true
        echo
        ;;
      stop)
        systemctl --user stop "$UNIT" || true
        echo "stopped ${UNIT}"
        ;;
      uninstall)
        systemctl --user disable --now "$UNIT" 2>/dev/null || true
        rm -f "$(linux_unit_path)"
        systemctl --user daemon-reload
        echo "uninstalled ${UNIT}"
        ;;
      install)
        stop_ad_hoc
        unit=$(linux_unit_path)
        mkdir -p "$(dirname "$unit")"
        sed -e "s|0\\.0\\.0\\.0|${HOST}|g" \
            -e "s|7788|${PORT}|g" \
            "$script_dir/contrib/systemd/tmux-attn-web.service" > "$unit"
        systemctl --user daemon-reload
        systemctl --user enable --now "$UNIT"
        # Survive logout on headless hosts when permitted.
        if command -v loginctl >/dev/null 2>&1; then
          loginctl enable-linger "$(id -un)" 2>/dev/null || true
        fi
        sleep 0.6
        echo "installed systemd --user ${UNIT}"
        echo "  unit: $unit"
        systemctl --user --no-pager --full status "$UNIT" | head -15 || true
        curl -s -m 3 "http://127.0.0.1:${PORT}/api/health" || true
        echo
        ;;
    esac
    ;;
  *)
    echo "Unsupported OS: $(uname -s)" >&2
    exit 1
    ;;
esac
