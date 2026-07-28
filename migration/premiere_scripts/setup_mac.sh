#!/bin/bash
# SLYBURN Premiere toolkit -- one-shot setup for macOS.
# Installs VS Code (if missing) + the ExtendScript Debugger extension,
# then opens the toolkit folder ready to run with F5.
#
# Run it from Terminal:
#   cd <the folder this file is in>
#   bash setup_mac.sh
set -e

echo "=== SLYBURN toolkit setup (macOS) ==="

APP="/Applications/Visual Studio Code.app"
CODE_CLI="$APP/Contents/Resources/app/bin/code"

# --- 1. VS Code -------------------------------------------------------------
if [ -d "$APP" ]; then
    echo "[1/3] VS Code already installed."
elif command -v brew >/dev/null 2>&1; then
    echo "[1/3] Installing VS Code via Homebrew..."
    brew install --cask visual-studio-code
else
    echo "[1/3] Downloading VS Code (~150 MB)..."
    TMPZIP="$(mktemp -d)/vscode.zip"
    curl -fL --progress-bar \
        "https://update.code.visualstudio.com/latest/darwin-universal/stable" \
        -o "$TMPZIP"
    echo "      Installing to /Applications..."
    ditto -x -k "$TMPZIP" /Applications
    rm -f "$TMPZIP"
fi

if [ ! -x "$CODE_CLI" ]; then
    echo "ERROR: VS Code install didn't land where expected ($APP)."
    echo "Install it manually from https://code.visualstudio.com then re-run."
    exit 1
fi

# --- 2. ExtendScript Debugger extension ------------------------------------
echo "[2/3] Installing the ExtendScript Debugger extension..."
"$CODE_CLI" --install-extension Adobe.extendscript-debug --force

# --- 3. Open the toolkit folder ---------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
echo "[3/3] Opening the toolkit in VS Code: $SCRIPT_DIR"
"$CODE_CLI" "$SCRIPT_DIR"

echo ""
echo "=== Done. Next steps ==="
echo "1. Open your project in Premiere; click the sequence's timeline so it's active. Save."
echo "2. In the VS Code window that just opened, press F5"
echo "   (pick 'Adobe Premiere Pro' if asked which app)."
echo "3. The SLYBURN toolkit dialog appears inside Premiere -> choose 1. AUDIT -> Run."
echo "Reports land in a slyburn_reports/ folder next to your .prproj."
