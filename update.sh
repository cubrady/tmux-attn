#!/bin/sh
# One-command upgrade for an installed tmux-attn client. It finds the git checkout this install came
# from (or clones one), then runs that checkout's update.py, which does the real work.
#
#   curl -fsSL https://raw.githubusercontent.com/cubrady/tmux-attn/<tag-or-sha>/update.sh | sh
#   curl -fsSL https://raw.githubusercontent.com/cubrady/tmux-attn/<tag-or-sha>/update.sh | sh -s -- --check
#
# Pin the URL (and TMUX_ATTN_REF) to a tag or commit you trust: `main` means whatever is on main right now.
# First install is still install.sh; this only upgrades an existing install.
#
# Env: TMUX_ATTN_SRC   checkout to use (default: the one recorded at install, else ~/.local/share/tmux-attn-src)
#      TMUX_ATTN_REF   commit, tag or branch to move to (default: the checkout's upstream)
#      TMUX_ATTN_REPO  clone URL, used only when there is no checkout yet
set -eu

main() {
    REPO="${TMUX_ATTN_REPO:-https://github.com/cubrady/tmux-attn.git}"
    SHARE="${HOME}/.local/share/tmux-attn"

    python=''
    for candidate in python3 python3.14 python3.13 python3.12 python3.11 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
        if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
            python=$candidate
            break
        fi
    done
    if [ -z "$python" ]; then
        echo 'Requires Python 3.11 or newer.' >&2
        exit 1
    fi
    for value in "$REPO" "${TMUX_ATTN_REF:-}"; do
        case "$value" in
            -*) echo "Refusing option-like value: $value" >&2; exit 1 ;;
        esac
    done
    if ! command -v git >/dev/null 2>&1; then
        echo 'Requires git.' >&2
        exit 1
    fi
    if [ ! -f "$SHARE/attention.py" ]; then
        echo "tmux-attn is not installed in $SHARE. Install it first: clone the repo and run sh install.sh" >&2
        exit 1
    fi

    is_checkout() {
        [ -e "$1/.git" ]
    }

    src="${TMUX_ATTN_SRC:-}"
    if [ -z "$src" ] && [ -f "$SHARE/INSTALL.json" ]; then
        src=$("$python" -c 'import json,sys; print(json.load(open(sys.argv[1])).get("source", ""))' "$SHARE/INSTALL.json" 2>/dev/null || true)
    fi
    case "$src" in
        ''|/*) ;;
        *) echo "Checkout path must be absolute: $src" >&2; exit 1 ;;
    esac
    if [ -n "$src" ] && ! is_checkout "$src"; then
        echo "$src is not a tmux-attn checkout" >&2
        exit 1
    fi
    if [ -z "$src" ]; then
        src="${HOME}/.local/share/tmux-attn-src"
        if [ ! -e "$src" ]; then
            echo "No checkout recorded for this install; cloning $REPO into $src"
            git clone --quiet -- "$REPO" "$src"
            if [ -n "${TMUX_ATTN_REF:-}" ]; then
                git -C "$src" checkout --quiet --detach "$TMUX_ATTN_REF"
            fi
        fi
        if ! is_checkout "$src"; then
            echo "$src exists but is not a tmux-attn checkout" >&2
            exit 1
        fi
    fi

    updater="$src/update.py"
    borrowed=''
    if [ ! -f "$updater" ]; then
        # A checkout older than update.py: run the copy from the commit we are moving to, and leave the
        # working tree alone until that updater has checked it for local changes.
        default_ref='@{upstream}'
        git -C "$src" fetch --quiet --tags
        borrowed=$(mktemp)
        trap 'rm -f "$borrowed"' EXIT
        git -C "$src" show "${TMUX_ATTN_REF:-$default_ref}:update.py" > "$borrowed"
        updater=$borrowed
    fi
    if [ -n "${TMUX_ATTN_REF:-}" ]; then
        set -- --ref "$TMUX_ATTN_REF" "$@"
    fi
    status=0
    "$python" "$updater" --src "$src" "$@" || status=$?
    exit "$status"
}

main "$@"
