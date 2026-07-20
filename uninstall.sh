#!/bin/bash
# iPhone WiFi Mirror — uninstaller
# Removes the virtualenv. Doesn't touch WebDriverAgent on the phone —
# uninstall that via iOS Settings → General → VPN & Device Management.

set -e

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

BLUE='\033[0;34m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info() { echo -e "${BLUE}==>${NC} $1"; }
ok()   { echo -e "${GREEN}✓${NC} $1"; }
warn() { echo -e "${YELLOW}!${NC} $1"; }

info "iPhone WiFi Mirror uninstaller"

if [[ -d "$SCRIPT_DIR/.venv" ]]; then
    read -p "Remove virtual environment (.venv)? [y/N] " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        rm -rf "$SCRIPT_DIR/.venv"
        ok "Venv removed"
    fi
fi

echo
warn "The WebDriverAgent runner is still installed on your iPhone."
warn "Remove it via iOS Settings → General → VPN & Device Management → your Apple ID."
echo
ok "Uninstall complete"
