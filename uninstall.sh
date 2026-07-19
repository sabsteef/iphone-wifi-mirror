#!/bin/bash
# iPhone Mirror — uninstaller (v9)
# Verwijdert venv + eventuele v7-legacy tunnel service.

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

# v7 legacy — als hij er is, ruim hem op
LEGACY_PLIST="/Library/LaunchDaemons/com.sabsteef.iphonemirror.tunneld.plist"
LEGACY_SUDOERS="/etc/sudoers.d/iphonemirror"
LEGACY_LOG="/var/log/iphonemirror-tunneld.log"

if [[ -f "$LEGACY_PLIST" ]] || [[ -f "$LEGACY_SUDOERS" ]] || [[ -f "$LEGACY_LOG" ]]; then
    info "v7 tunnel service opruimen (vraagt admin wachtwoord)"
    sudo bash -c "
        /bin/launchctl unload '$LEGACY_PLIST' 2>/dev/null || true
        rm -f '$LEGACY_PLIST' '$LEGACY_SUDOERS' '$LEGACY_LOG'
    "
    ok "v7 service verwijderd"
else
    ok "Geen v7 service om op te ruimen"
fi

if [[ -d "$SCRIPT_DIR/.venv" ]]; then
    read -p "Verwijder virtual environment (.venv)? [y/N] " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        rm -rf "$SCRIPT_DIR/.venv"
        ok "Venv verwijderd"
    fi
fi

echo
ok "Uninstall voltooid"
