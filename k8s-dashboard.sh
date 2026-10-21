#!/usr/bin/env bash
# Launch the K8s Resource Dashboard desktop app.
# This script can live anywhere; it always runs the app from the project
# folder resolved relative to the script's own location.

set -euo pipefail

# Directory this script lives in (so it works no matter where it's invoked).
APPDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Prefer python3; fall back to python.
if command -v python3 >/dev/null 2>&1; then
    PY=python3
elif command -v python >/dev/null 2>&1; then
    PY=python
else
    echo "Python 3 not found on PATH." >&2
    exit 1
fi

"$PY" "$APPDIR/k8s_dashboard_gui.py"
