#!/usr/bin/env bash
# Launch the K8s Resource Dashboard desktop app.
# This script can live anywhere; it always runs the app from the project
# folder resolved relative to the script's own location.
#
# It deliberately keeps the Terminal window open if anything goes wrong, so a
# double-click that fails shows you the reason instead of vanishing.

# Directory this script lives in (so it works no matter where it's invoked).
APPDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$APPDIR"

# Keep the window open on any error so the message is readable (Finder/Terminal
# otherwise closes it the instant the script exits).
pause_on_exit() {
    local code=$?
    if [ "$code" -ne 0 ]; then
        echo
        echo "The dashboard exited with an error (code $code)."
        echo "Press Return to close this window."
        read -r _
    fi
}
trap pause_on_exit EXIT

# Prefer python3; fall back to python.
if command -v python3 >/dev/null 2>&1; then
    PY=python3
elif command -v python >/dev/null 2>&1; then
    PY=python
else
    echo "Python 3 not found on PATH."
    echo "Install it from https://www.python.org/downloads/ (bundles tkinter)."
    exit 1
fi

"$PY" "$APPDIR/k8s_dashboard_gui.py"
