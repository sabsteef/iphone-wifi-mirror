#!/bin/bash
# iPhone Mirror — launcher (v9)
# Verifies the venv is set up and pymobiledevice3 v9+ is installed,
# then starts the app.

set -e

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

if [[ ! -d ".venv" ]]; then
    echo "! Venv niet gevonden. Draai eerst ./install.sh"
    exit 1
fi

# Check pymobiledevice3 version — must be v9+
V=$(.venv/bin/python -c "import pymobiledevice3; import importlib.metadata as m; print(m.version('pymobiledevice3'))" 2>&1)
MAJOR="${V%%.*}"
if [[ "$MAJOR" -lt 9 ]]; then
    echo "! pymobiledevice3 $V detected (need v9+). Draai ./install.sh"
    exit 1
fi

exec .venv/bin/python main.py
