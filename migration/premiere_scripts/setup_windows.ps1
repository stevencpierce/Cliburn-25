# SLYBURN Premiere toolkit -- one-shot setup for Windows.
# Installs VS Code (if missing) + the ExtendScript Debugger extension,
# then opens the toolkit folder ready to run with F5.
#
# Run it from PowerShell (right-click Start -> Terminal):
#   cd <the folder this file is in>
#   powershell -ExecutionPolicy Bypass -File setup_windows.ps1

Write-Host "=== SLYBURN toolkit setup (Windows) ==="

# --- 1. VS Code -------------------------------------------------------------
$code = Get-Command code -ErrorAction SilentlyContinue
if (-not $code) {
    $userCode = "$env:LOCALAPPDATA\Programs\Microsoft VS Code\bin\code.cmd"
    if (Test-Path $userCode) {
        $code = $userCode
    } else {
        Write-Host "[1/3] Installing VS Code via winget..."
        winget install -e --id Microsoft.VisualStudioCode --accept-package-agreements --accept-source-agreements
        $code = "$env:LOCALAPPDATA\Programs\Microsoft VS Code\bin\code.cmd"
        if (-not (Test-Path $code)) {
            Write-Host "ERROR: VS Code didn't install where expected."
            Write-Host "Install manually from https://code.visualstudio.com then re-run."
            exit 1
        }
    }
} else {
    $code = $code.Source
    Write-Host "[1/3] VS Code already installed."
}

# --- 2. ExtendScript Debugger extension ------------------------------------
Write-Host "[2/3] Installing the ExtendScript Debugger extension..."
& $code --install-extension Adobe.extendscript-debug --force

# --- 3. Open the toolkit folder ---------------------------------------------
Write-Host "[3/3] Opening the toolkit in VS Code: $PSScriptRoot"
& $code $PSScriptRoot

Write-Host ""
Write-Host "=== Done. Next steps ==="
Write-Host "1. Open your project in Premiere; click the sequence's timeline so it's active. Save."
Write-Host "2. In the VS Code window that just opened, press F5 (pick 'Adobe Premiere Pro' if asked)."
Write-Host "3. The SLYBURN toolkit dialog appears inside Premiere -> choose 1. AUDIT -> Run."
Write-Host "Reports land in a slyburn_reports\ folder next to your .prproj."
