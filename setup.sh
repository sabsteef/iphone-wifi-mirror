#!/bin/bash
set -euo pipefail

MIN_PYTHON="3.10"

find_python() {
    for cmd in python3 python; do
        if command -v "$cmd" &>/dev/null; then
            local ver
            ver=$("$cmd" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
            if printf '%s\n%s\n' "$MIN_PYTHON" "$ver" | sort -V -C; then
                echo "$cmd"
                return
            fi
        fi
    done
    echo ""
}

PYTHON=$(find_python)
if [ -z "$PYTHON" ]; then
    echo "ERROR: Python $MIN_PYTHON or higher is required."
    echo "Install via: brew install python@3.12"
    exit 1
fi

echo "Using $PYTHON ($($PYTHON --version))"

if [ ! -d ".venv" ]; then
    echo "Creating virtual environment..."
    $PYTHON -m venv .venv
fi

source .venv/bin/activate
echo "Installing dependencies..."
pip install --upgrade pip -q
pip install -r requirements.txt -q

echo ""
echo "=== Setup complete ==="
echo ""
echo "To run the app:"
echo "  source .venv/bin/activate"
echo "  python main.py"
echo ""
echo "=== Prerequisites ==="
echo ""
echo "1. Pair your iPhone (one-time, over WiFi or USB):"
echo "   python -m pymobiledevice3 remote pair"
echo ""
echo "2. Start the tunnel daemon (separate terminal, keep running):"
echo "   sudo .venv/bin/python3 -m pymobiledevice3 remote tunneld"
echo ""
echo "3. For touch control, clone and build WebDriverAgent:"
echo "   git clone https://github.com/appium/WebDriverAgent.git"
echo "   # Open WebDriverAgent.xcodeproj in Xcode"
echo "   # Set your Team in Signing & Capabilities"
echo "   # Product > Test to deploy to iPhone"
echo ""
echo "IMPORTANT: pymobiledevice3 is pinned to 7.8.3."
echo "Do NOT upgrade to 8.x+ — the async API change breaks this app."
echo ""
