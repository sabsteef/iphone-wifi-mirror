#!/bin/bash
# iPhone Mirror — uninstaller
# Removes tunnel service, sudoers rule, and venv.

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

info "iPhone Mirror uninstaller"

DAEMON_PLIST="/Library/LaunchDaemons/com.sabsteef.iphonemirror.tunneld.plist"
SUDOERS_FILE="/etc/sudoers.d/iphonemirror"
LOG_FILE="/var/log/iphonemirror-tunneld.log"

NEEDS_ROOT=false
[[ -f "$DAEMON_PLIST" ]] && NEEDS_ROOT=true
[[ -f "$SUDOERS_FILE" ]] && NEEDS_ROOT=true
[[ -f "$LOG_FILE" ]] && NEEDS_ROOT=true

if $NEEDS_ROOT; then
    info "Tunnel service verwijderen (vraagt admin wachtwoord)"
    sudo bash -c "
        /bin/launchctl unload '$DAEMON_PLIST' 2>/dev/null || true
        rm -f '$DAEMON_PLIST' '$SUDOERS_FILE' '$LOG_FILE'
    "
    ok "Service verwijderd"
else
    ok "Service was niet geïnstalleerd"
fi

if [[ -d "$SCRIPT_DIR/.venv" ]]; then
    read -p "Verwijder virtual environment (.venv)? [y/N] " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        rm -rf "$SCRIPT_DIR/.venv"
        ok "Venv verwijderd"
    fi
fi

warn "pymobiledevice3 blijft geïnstalleerd in system Python"
warn "Verwijder handmatig: /opt/homebrew/bin/python3 -m pip uninstall pymobiledevice3"

echo
ok "Uninstall voltooid"
