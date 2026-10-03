#!/bin/sh
# Install on another machine; generates paths locally and preserves user settings.
set -eu
task_root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
task_python=''
for task_candidate in python3 python3.14 python3.13 python3.12 python3.11 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
    if command -v "$task_candidate" >/dev/null 2>&1 && "$task_candidate" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
        task_python=$task_candidate
        break
    fi
done
if [ -z "$task_python" ]; then
    echo 'Requires Python 3.11 or newer. Install it, then rerun this command.' >&2
    exit 1
fi
if ! command -v tmux >/dev/null 2>&1; then
    echo 'Requires tmux with display-popup support. Install it, then rerun this command.' >&2
    exit 1
fi
"$task_python" -c 'import subprocess,sys; commands=subprocess.check_output(["tmux","list-commands"],text=True); sys.exit(0 if "display-popup " in commands else "tmux must support display-popup")'
# Ensure a server exists so the installer can back up and activate live settings.
if ! tmux has-session 2>/dev/null; then
    tmux new-session -d -s work
fi
exec "$task_python" "$task_root/install.py" --enable-codex-hooks "$@"
