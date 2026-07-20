#!/bin/bash
# iPhone WiFi Mirror — installer
# No sudo, no LaunchDaemon; pymobiledevice3 v9 runs the tunnel in-process.

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

info "iPhone WiFi Mirror installer"

if [[ "$(uname)" != "Darwin" ]]; then
    error "Only macOS is supported"
fi

MACOS_MAJOR=$(sw_vers -productVersion | cut -d. -f1)
if [[ "$MACOS_MAJOR" -lt 14 ]]; then
    warn "macOS 14 (Sonoma) or newer recommended (yours: $(sw_vers -productVersion))"
fi

# ─── Homebrew ────────────────────────────────────────────────────────────────

if ! command -v brew &> /dev/null; then
    info "Homebrew not found — installing"
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    ok "Homebrew installed"
else
    ok "Homebrew present"
fi

BREW_PREFIX=$(brew --prefix)

# ─── Python 3 ────────────────────────────────────────────────────────────────

PYTHON_BIN="$BREW_PREFIX/bin/python3"
if [[ ! -x "$PYTHON_BIN" ]]; then
    info "Installing Python 3 via Homebrew"
    brew install python@3.14
    ok "Python installed"
else
    ok "Python 3 present ($($PYTHON_BIN --version))"
fi

# ─── Xcode Command Line Tools ────────────────────────────────────────────────

if ! xcode-select -p &> /dev/null; then
    warn "Xcode Command Line Tools not found"
    warn "Install them with: xcode-select --install"
    warn "(needed to build WebDriverAgent)"
fi

# ─── Venv + deps ─────────────────────────────────────────────────────────────

VENV_DIR="$SCRIPT_DIR/.venv"
if [[ ! -d "$VENV_DIR" ]]; then
    info "Creating virtual environment"
    "$PYTHON_BIN" -m venv "$VENV_DIR"
    ok "Venv created: $VENV_DIR"
else
    ok "Venv already exists"
fi

info "Installing Python packages"
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet -r requirements.txt
ok "Packages installed"

# ─── Run script ──────────────────────────────────────────────────────────────

cat > "$SCRIPT_DIR/run.sh" << 'EOF'
#!/bin/bash
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"
exec .venv/bin/python main.py
EOF
chmod +x "$SCRIPT_DIR/run.sh"
ok "Launcher created: run.sh"

echo
echo -e "${GREEN}══════════════════════════════════════════${NC}"
ok "Install complete"
echo -e "${GREEN}══════════════════════════════════════════${NC}"
echo
echo "Next steps:"
echo
echo "  1) Pair your iPhone (once, over USB):"
echo "     ${BLUE}sudo pymobiledevice3 remote pair${NC}"
echo "     Confirm the prompt on the iPhone."
echo
echo "  2) Build & install WebDriverAgent with your Apple ID:"
echo "     See README.md → 'Build & install WebDriverAgent'"
echo "     (requires Xcode + a free Apple Developer account)"
echo
echo "  3) Export your WDA bundle ID:"
echo "     ${BLUE}export WDA_BUNDLE_ID=\"com.yourname.WebDriverAgentRunner.xctrunner\"${NC}"
echo "     (add this line to ~/.zshrc so it survives reboots)"
echo
echo "  4) Launch:"
echo "     ${BLUE}./run.sh${NC}"
echo
echo "The tunnel starts in-process — no sudo prompts, no admin access."
echo
