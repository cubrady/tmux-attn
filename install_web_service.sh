#!/bin/sh
# Install a persistent tmux-attn web service (macOS launchd / Linux systemd --user).
#
#   sh install_web_service.sh           # install + start
#   sh install_web_service.sh --status
#   sh install_web_service.sh --stop
#   sh install_web_service.sh --uninstall
#
# Env: TMUX_ATTN_WEB_HOST (default 127.0.0.1), TMUX_ATTN_WEB_PORT (default 7788)
#      TMUX_ATTN_ALLOWED_ORIGINS, TMUX_ATTN_AUTH_KEY_FILE, TMUX_ATTN_AGY_AUTH_SCOPE (persisted)
set -eu

HOST="${TMUX_ATTN_WEB_HOST:-127.0.0.1}"
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

darwin_domain_root() {
  if launchctl print "gui/${uid}" >/dev/null 2>&1; then
    echo "gui/${uid}"
  else
    # SSH and headless sessions may have a user domain without an Aqua GUI domain.
    echo "user/${uid}"
  fi
}

darwin_domain() {
  echo "$(darwin_domain_root)/${LABEL}"
}

linux_unit_path() {
  echo "${HOME}/.config/systemd/user/${UNIT}"
}


render_service() {
  # Python serializes untrusted environment values; shell/sed replacement is not escaping.
  python3 - "$script_dir" "$1" "$2" "$3" "$HOST" "$PORT" "$(id -un)" <<'PYRENDER'
import ipaddress
import os
from pathlib import Path
import plistlib
import sys

runtime, kind, source, destination, host, port, user = sys.argv[1:]
sys.path.insert(0, runtime)
try:
    from web_security import _origin
except ImportError:
    raise SystemExit('Missing web_security.py — install the current runtime before starting its web service')

try:
    if host != 'localhost':
        ipaddress.ip_address(host)
    if not port.isascii() or not port.isdigit() or not 1 <= int(port) <= 65535:
        raise ValueError('port must be between 1 and 65535')
    origins = os.environ.get('TMUX_ATTN_ALLOWED_ORIGINS', '')
    origin_values = [origin.strip() for origin in origins.split(',') if origin.strip()]
    for origin in origin_values:
        _origin(origin)
    if host in ('0.0.0.0', '::') and not origin_values:
        raise ValueError('wildcard bind requires TMUX_ATTN_ALLOWED_ORIGINS; prefer loopback or a Tailscale IP')
    configured_key = os.environ.get('TMUX_ATTN_AUTH_KEY_FILE')
    if configured_key and not Path(configured_key).expanduser().is_absolute():
        raise ValueError('TMUX_ATTN_AUTH_KEY_FILE must be an absolute path')
except ValueError as error:
    raise SystemExit(f'Invalid web service configuration: {error}')

environment = {key: os.environ[key] for key in ('TMUX_ATTN_ALLOWED_ORIGINS', 'TMUX_ATTN_AUTH_KEY_FILE', 'TMUX_ATTN_AGY_AUTH_SCOPE')
               if os.environ.get(key)}
replacements = {'__HOME__': os.environ['HOME'], '__USER__': user, '__HOST__': host, '__PORT__': port}

def replace(value):
    if isinstance(value, dict):
        return {key: replace(item) for key, item in value.items()}
    if isinstance(value, list):
        return [replace(item) for item in value]
    if isinstance(value, str):
        for key, item in replacements.items():
            value = value.replace(key, item)
    return value

source_path, destination_path = Path(source), Path(destination)
if kind == 'launchd':
    config = replace(plistlib.loads(source_path.read_bytes()))
    config['EnvironmentVariables'].update(environment)
    data = plistlib.dumps(config)
else:
    def quote(value):
        # systemd performs specifier expansion in both ExecStart and Environment.
        value = value.replace('%', '%%').replace('\\', '\\\\').replace('"', '\\"')
        value = value.replace('\n', '\\n').replace('\r', '\\r').replace('\t', '\\t')
        return '"' + value + '"'
    text = replace(source_path.read_text())
    text = text.replace('__ENVIRONMENT__', '\n'.join('Environment=' + quote(key + '=' + value)
                                                     for key, value in environment.items()))
    data = text.encode()
destination_path.parent.mkdir(parents=True, exist_ok=True)
temp = destination_path.with_name(destination_path.name + f'.{os.getpid()}.tmp')
try:
    with temp.open('xb') as target:
        os.chmod(temp, 0o600)
        target.write(data)
    os.replace(temp, destination_path)
finally:
    temp.unlink(missing_ok=True)
PYRENDER
}

probe_health() {
  # Consult the installed service file when --status is run without its install-time env.
  health_url=$(python3 - "$1" "$2" "$HOST" "$PORT" <<'PYHEALTH'
from pathlib import Path
import plistlib
import re
import shlex
import sys
kind, configured, host, port = sys.argv[1:]
try:
    path = Path(configured)
    if kind == 'launchd':
        args = plistlib.loads(path.read_bytes())['ProgramArguments']
    else:
        line = re.search(r'^ExecStart=(.+)$', path.read_text(), re.M)
        args = shlex.split(line[1]) if line else []
    if len(args) >= 3 and str(args[-1]).isdigit():
        host, port = args[-2:]
except (OSError, ValueError, KeyError):
    pass
host = '127.0.0.1' if host in ('0.0.0.0', '::') else host
host = '[' + host + ']' if ':' in host else host
print(f'http://{host}:{port}/api/health')
PYHEALTH
)
  curl -s -m 3 "$health_url" || true
  echo
}

print_access_steps() {
  echo '  access: tmux-attn auth-token (copy the owner token into the browser login)'
  echo '  remote: configure an explicit Tailscale bind, or proxy loopback with Tailscale Serve'
  echo '  proxy: set TMUX_ATTN_ALLOWED_ORIGINS to the exact HTTPS origin when installing'
}

case "$(uname -s)" in
  Darwin)
    case "$ACTION" in
      status)
        launchctl print "$(darwin_domain)" 2>/dev/null | head -40 || echo 'not loaded'
        probe_health launchd "$(darwin_plist_path)"
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
        plist=$(darwin_plist_path)
        render_service launchd "$script_dir/contrib/launchd/ai.tmux-attn.web.plist" "$plist"
        stop_ad_hoc
        mkdir -p "${HOME}/Library/Caches/tmux-attn-tmp"
        mkdir -p "${HOME}/.local/share/tmux-attn/contrib/launchd"
        if [ -f "$script_dir/contrib/launchd/web-run.sh" ]; then
          if ! cmp -s "$script_dir/contrib/launchd/web-run.sh" "${HOME}/.local/share/tmux-attn/contrib/launchd/web-run.sh"; then
            cp "$script_dir/contrib/launchd/web-run.sh" "${HOME}/.local/share/tmux-attn/contrib/launchd/web-run.sh"
          fi
          chmod +x "${HOME}/.local/share/tmux-attn/contrib/launchd/web-run.sh"
        fi
        launchctl bootout "$(darwin_domain)" 2>/dev/null || true
        launchctl bootstrap "$(darwin_domain_root)" "$plist"
        launchctl enable "$(darwin_domain)" 2>/dev/null || true
        launchctl kickstart -k "$(darwin_domain)"
        sleep 0.6
        echo "installed launchd ${LABEL}"
        echo "  plist: $plist"
        echo "  logs:  ${HOME}/.local/state/tmux-attn/web.{out,err}.log"
        probe_health launchd "$plist"
        print_access_steps
        ;;
    esac
    ;;
  Linux)
    case "$ACTION" in
      status)
        systemctl --user status "$UNIT" --no-pager || true
        probe_health systemd "$(linux_unit_path)"
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
        unit=$(linux_unit_path)
        render_service systemd "$script_dir/contrib/systemd/tmux-attn-web.service" "$unit"
        stop_ad_hoc
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
        probe_health systemd "$unit"
        print_access_steps
        ;;
    esac
    ;;
  *)
    echo "Unsupported OS: $(uname -s)" >&2
    exit 1
    ;;
esac
