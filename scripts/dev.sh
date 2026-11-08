#!/usr/bin/env bash
# Isolated Calibre development environment for the EvilFlowers plugin.
#
# Everything lives under .dev/ in the repository: a separate Calibre config
# directory (plugins, preferences, the library list) and a separate library.
# Your normal Calibre profile (~/Library/Preferences/calibre on macOS) and your
# real library are never read or written.
#
#   scripts/dev.sh seed        create .dev/library with a few sample books
#   scripts/dev.sh install     build and install the plugin into the dev profile
#   scripts/dev.sh run         install, then start Calibre in debug mode on the dev library
#   scripts/dev.sh test        run the test suite under Calibre's own Python (no network)
#   scripts/dev.sh test-dev    run the end-to-end test against the dev catalog
#   scripts/dev.sh calibredb … run calibredb against the dev library
#   scripts/dev.sh debug f.py run a script under Calibre's Python with the dev profile and token
#   scripts/dev.sh screenshots render every window, light and dark, to .dev/screenshots
#                              (--docs: refresh docs/screenshots instead)
#   scripts/dev.sh build       build dist/evilflowers-<version>.zip
#   scripts/dev.sh reset       delete .dev/config and .dev/library
#
# The dev catalog token comes from $ELVIRA_DEV_API_KEY, or from
# EVILFLOWERS_TOKEN=... in .dev/secrets.env (gitignored).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEV="$ROOT/.dev"
PLUGIN="$ROOT/calibre_plugin"

case "$(uname -s)" in
    Darwin) CALIBRE_BIN="${CALIBRE_BIN:-/Applications/calibre.app/Contents/MacOS}" ;;
    *) CALIBRE_BIN="${CALIBRE_BIN:-$(dirname "$(command -v calibre-debug)")}" ;;
esac

export CALIBRE_CONFIG_DIRECTORY="$DEV/config"
export CALIBRE_LIBRARY="$DEV/library"
export EVILFLOWERS_DEV_URL="${EVILFLOWERS_DEV_URL:-https://dev.evilflowers.elvira.stuba.sk}"
export EVILFLOWERS_DEV_CATALOG="${EVILFLOWERS_DEV_CATALOG:-calibre-sync-test}"

# The GUI's single-instance socket (/tmp/calibre-<uid>-gui.sock) does not depend on
# the config directory, so a second calibre would hand its arguments to an already
# running one -- which has your real library open. Refuse instead.
guard_no_running_gui() {
    if pgrep -x calibre >/dev/null 2>&1; then
        echo "A Calibre GUI is running. Quit it first: the dev instance would attach to it and its real library." >&2
        exit 1
    fi
}

load_token() {
    if [[ -z "${ELVIRA_DEV_API_KEY:-}" && -f "$DEV/secrets.env" ]]; then
        # shellcheck disable=SC1091
        set -a; source "$DEV/secrets.env"; set +a
    fi
    export EVILFLOWERS_TOKEN="${EVILFLOWERS_TOKEN:-${ELVIRA_DEV_API_KEY:-}}"
}

install_plugin() {
    mkdir -p "$CALIBRE_CONFIG_DIRECTORY"
    "$CALIBRE_BIN/calibre-customize" -b "$PLUGIN"
}

cmd="${1:-help}"
shift || true
case "$cmd" in
    seed)
        "$CALIBRE_BIN/calibre-debug" -e "$ROOT/scripts/seed_library.py" -- "$CALIBRE_LIBRARY"
        ;;
    install)
        install_plugin
        ;;
    run)
        guard_no_running_gui
        [[ -f "$CALIBRE_LIBRARY/metadata.db" ]] || "$CALIBRE_BIN/calibre-debug" -e "$ROOT/scripts/seed_library.py" -- "$CALIBRE_LIBRARY"
        install_plugin
        "$CALIBRE_BIN/calibre-debug" -e "$ROOT/scripts/prepare_profile.py" -- "$CALIBRE_LIBRARY"
        exec "$CALIBRE_BIN/calibre-debug" -g -- --with-library "$CALIBRE_LIBRARY" "$@"
        ;;
    test)
        exec "$CALIBRE_BIN/calibre-debug" -e "$ROOT/tests/run.py" -- "$@"
        ;;
    test-dev)
        load_token
        if [[ -z "$EVILFLOWERS_TOKEN" ]]; then
            echo "No token: export ELVIRA_DEV_API_KEY or put EVILFLOWERS_TOKEN=... in .dev/secrets.env" >&2
            exit 1
        fi
        exec "$CALIBRE_BIN/calibre-debug" -e "$ROOT/tests/run.py" -- --dev "$@"
        ;;
    calibredb)
        exec "$CALIBRE_BIN/calibredb" --with-library "$CALIBRE_LIBRARY" "$@"
        ;;
    debug)
        load_token
        exec "$CALIBRE_BIN/calibre-debug" -e "$@"
        ;;
    screenshots)
        exec "$CALIBRE_BIN/calibre-debug" -e "$ROOT/scripts/screenshots.py" -- "$@"
        ;;
    build)
        exec python3 "$ROOT/scripts/build.py"
        ;;
    reset)
        rm -rf "$DEV/config" "$DEV/library"
        echo "Removed $DEV/config and $DEV/library"
        ;;
    *)
        sed -n '2,22p' "$0"
        ;;
esac
