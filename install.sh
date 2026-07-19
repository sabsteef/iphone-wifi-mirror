#!/bin/bash
# iPhone Mirror — installer (v9)
# Zero sudo. Zero LaunchDaemon. pymobiledevice3 v9 draait de tunnel in-process.

set -e

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

BLUE='\033[0;34m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info()  { echo -e "${BLUE}==>${NC} $1"; }
ok()    { echo -e "${GREEN}✓${NC} $1"; }
warn()  { echo -e "${YELLOW}!${NC} $1"; }
error() { echo -e "${RED}✗${NC} $1"; exit 1; }

info "iPhone Mirror installer (v9, no sudo)"

if [[ "$(uname)" != "Darwin" ]]; then
    error "Alleen macOS wordt ondersteund"
fi

MACOS_MAJOR=$(sw_vers -productVersion | cut -d. -f1)
if [[ "$MACOS_MAJOR" -lt 14 ]]; then
    warn "macOS 14+ aanbevolen (jouw versie: $(sw_vers -productVersion))"
fi

# ─── Homebrew ────────────────────────────────────────────────────────────────

if ! command -v brew &> /dev/null; then
    info "Homebrew niet gevonden — installeren"
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    ok "Homebrew geïnstalleerd"
else
    ok "Homebrew aanwezig"
fi

BREW_PREFIX=$(brew --prefix)

# ─── Python 3 ────────────────────────────────────────────────────────────────

PYTHON_BIN="$BREW_PREFIX/bin/python3"
if [[ ! -x "$PYTHON_BIN" ]]; then
    info "Python 3 installeren via Homebrew"
    brew install python@3.14
    ok "Python geïnstalleerd"
else
    ok "Python 3 aanwezig ($($PYTHON_BIN --version))"
fi

# ─── Xcode Command Line Tools ────────────────────────────────────────────────

if ! xcode-select -p &> /dev/null; then
    warn "Xcode Command Line Tools niet gevonden"
    warn "Installeer via: xcode-select --install"
    warn "(nodig voor WebDriverAgent bouwen)"
fi

# ─── Venv + deps ─────────────────────────────────────────────────────────────

VENV_DIR="$SCRIPT_DIR/.venv"
if [[ ! -d "$VENV_DIR" ]]; then
    info "Virtual environment aanmaken"
    "$PYTHON_BIN" -m venv "$VENV_DIR"
    ok "Venv aangemaakt: $VENV_DIR"
else
    ok "Venv bestaat al"
fi

info "Python packages installeren"
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet -r requirements.txt
ok "Packages geïnstalleerd"

# ─── Run script ──────────────────────────────────────────────────────────────

cat > "$SCRIPT_DIR/run.sh" << 'EOF'
#!/bin/bash
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"
exec .venv/bin/python main.py
EOF
chmod +x "$SCRIPT_DIR/run.sh"
ok "Run script aangemaakt: run.sh"

echo
echo -e "${GREEN}══════════════════════════════════════════${NC}"
ok "Installatie voltooid"
echo -e "${GREEN}══════════════════════════════════════════${NC}"
echo
echo "Volgende stappen:"
echo
echo "  1) iPhone koppelen (eenmalig):"
echo "     ${BLUE}sudo pymobiledevice3 remote pair${NC}"
echo "     Volg de instructies op je iPhone."
echo
echo "  2) WebDriverAgent bouwen en installeren:"
echo "     Zie README.md sectie 'WebDriverAgent'"
echo "     (vereist Apple Developer account voor code signing)"
echo
echo "  3) App starten:"
echo "     ${BLUE}./run.sh${NC}"
echo
echo "  De tunnel wordt volledig in-process opgezet — geen sudo prompts,"
echo "  geen LaunchDaemon, geen macOS admin toegang nodig."
echo
