<#
.SYNOPSIS
    Installs Net Watch: checks Python and Node, installs dependencies, builds
    the UI, and registers the widget to start automatically at login.

.DESCRIPTION
    Run once from the folder containing sidecar.py:

        powershell -ExecutionPolicy Bypass -File .\install.ps1

    Re-running is safe - it rebuilds the UI, updates the autostart entry and
    leaves an existing .env untouched.

    The widget is two halves. Python gathers the data (core.py, driven by
    sidecar.py); an Electron app under app\ draws it. Both are needed, which is
    why this checks for two toolchains rather than one.

.PARAMETER NoAutostart
    Set up and launch the widget without registering it to start at login.

.PARAMETER Uninstall
    Remove the autostart entry and stop the widget. Files are left in place.
#>

[CmdletBinding()]
param(
    [switch]$NoAutostart,
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'

$AppName   = 'NetWatch'
$LegacyName = 'IPBar'          # what the tkinter build registered itself as
$RunKey    = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$AppDir    = Join-Path $ScriptDir 'app'
$Electron  = Join-Path $AppDir 'node_modules\electron\dist\electron.exe'

function Write-Step { param($m) Write-Host "  $m" -ForegroundColor Cyan }
function Write-Ok   { param($m) Write-Host "  OK   $m" -ForegroundColor Green }
function Write-Warn { param($m) Write-Host "  WARN $m" -ForegroundColor Yellow }
function Write-Err  { param($m) Write-Host "  FAIL $m" -ForegroundColor Red }

function Stop-Widget {
    # Match on the path, so an unrelated Electron app is never killed.
    $n = 0
    Get-Process electron -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -eq $Electron } |
        ForEach-Object { $n++; Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue }
    Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
        Where-Object { $_.CommandLine -like '*sidecar.py*' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    return $n
}

Write-Host ''
Write-Host 'Net Watch installer' -ForegroundColor White
Write-Host '-------------------'

# ── Uninstall ─────────────────────────────────────────────────────────────────
if ($Uninstall) {
    $stopped = Stop-Widget
    if ($stopped -gt 0) { Write-Ok "stopped $stopped running instance(s)" }
    foreach ($name in @($AppName, $LegacyName)) {
        if (Get-ItemProperty -Path $RunKey -Name $name -ErrorAction SilentlyContinue) {
            Remove-ItemProperty -Path $RunKey -Name $name
            Write-Ok "autostart entry '$name' removed"
        }
    }
    Write-Host ''
    Write-Host 'Uninstalled. Files were left in place; delete this folder to remove them.'
    Write-Host ''
    exit 0
}

# ── 1. The widget itself ──────────────────────────────────────────────────────
Write-Step 'Checking files'
foreach ($f in @('sidecar.py', 'core.py')) {
    if (-not (Test-Path -LiteralPath (Join-Path $ScriptDir $f))) {
        Write-Err "$f not found next to this script ($ScriptDir)."
        Write-Host '       Run install.ps1 from inside the folder you cloned.'
        exit 1
    }
}
if (-not (Test-Path -LiteralPath $AppDir)) {
    Write-Err "app\ not found next to this script ($ScriptDir)."
    exit 1
}
Write-Ok 'sidecar.py, core.py and app\ found'

# ── 2. Python ─────────────────────────────────────────────────────────────────
# pythonw.exe runs the sidecar without a console window. python.exe would leave
# a black box on screen for as long as the widget is up.
$pythonw = $null
foreach ($candidate in @('pythonw.exe', 'python.exe')) {
    $found = Get-Command $candidate -ErrorAction SilentlyContinue
    if ($found) {
        $pythonw = Join-Path (Split-Path -Parent $found.Source) 'pythonw.exe'
        break
    }
}
if (-not $pythonw -or -not (Test-Path -LiteralPath $pythonw)) {
    Write-Err 'Python was not found on PATH.'
    Write-Host '       Install Python 3.9 or newer from https://python.org/downloads'
    Write-Host '       and tick "Add python.exe to PATH" during setup.'
    exit 1
}
$python = Join-Path (Split-Path -Parent $pythonw) 'python.exe'

$version = (& $python -c 'import sys; print(sys.version.split()[0])' 2>&1 |
            Select-Object -First 1)
if ($LASTEXITCODE -ne 0) {
    Write-Err "Could not run $python"
    exit 1
}
Write-Ok "Python $version at $pythonw"

# No tkinter check any more: the interface is Electron, and core.py imports no
# GUI toolkit at all.

# ── 3. Dependencies ───────────────────────────────────────────────────────────
Write-Step 'Checking psutil'
& $python -c 'import psutil' 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Step 'installing psutil...'
    & $python -m pip install --quiet --user psutil
    & $python -c 'import psutil' 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Err 'psutil could not be installed.'
        Write-Host "       Try manually:  $python -m pip install psutil"
        exit 1
    }
}
Write-Ok 'psutil available'

Write-Step 'Checking Node'
$node = Get-Command node -ErrorAction SilentlyContinue
if (-not $node) {
    Write-Err 'Node.js was not found on PATH.'
    Write-Host '       Install Node 20 or newer from https://nodejs.org'
    exit 1
}
Write-Ok "Node $(& node --version) at $($node.Source)"

# ── 4. The UI ─────────────────────────────────────────────────────────────────
# `npm install` pulls Electron, which is a ~100 MB download the first time.
Write-Step 'Installing UI dependencies (first run downloads Electron, ~100 MB)'
Push-Location $AppDir
try {
    & npm install --no-audit --no-fund
    if ($LASTEXITCODE -ne 0) { Write-Err 'npm install failed.'; exit 1 }
    Write-Ok 'dependencies installed'

    Write-Step 'Building the UI'
    & npm run build
    if ($LASTEXITCODE -ne 0) { Write-Err 'npm run build failed.'; exit 1 }
    Write-Ok 'UI built'
} finally {
    Pop-Location
}
if (-not (Test-Path -LiteralPath $Electron)) {
    Write-Err "Electron binary missing at $Electron"
    Write-Host '       Try:  cd app; node node_modules\electron\install.js'
    exit 1
}

# ── 5. Configuration ──────────────────────────────────────────────────────────
Write-Step 'Checking configuration'
$envPath     = Join-Path $ScriptDir '.env'
$examplePath = Join-Path $ScriptDir '.env.example'
if (Test-Path -LiteralPath $envPath) {
    Write-Ok '.env already exists, left unchanged'
} elseif (Test-Path -LiteralPath $examplePath) {
    Copy-Item -LiteralPath $examplePath -Destination $envPath
    Write-Ok '.env created from .env.example'
    Write-Host '       Every setting is optional; the widget runs as-is.' -ForegroundColor DarkGray
} else {
    Write-Warn '.env.example missing, skipping (the widget runs without it)'
}

# ── 6. Autostart ──────────────────────────────────────────────────────────────
# Any leftover entry from the tkinter build is removed first, or both would
# launch at login and the old one would sit on top of the new.
if (Get-ItemProperty -Path $RunKey -Name $LegacyName -ErrorAction SilentlyContinue) {
    Remove-ItemProperty -Path $RunKey -Name $LegacyName
    Write-Ok "removed the old '$LegacyName' autostart entry"
}
if ($NoAutostart) {
    Write-Step 'Skipping autostart (-NoAutostart)'
} else {
    Write-Step 'Registering autostart'
    $command = '"{0}" "{1}"' -f $Electron, $AppDir
    New-ItemProperty -Path $RunKey -Name $AppName -Value $command `
                     -PropertyType String -Force | Out-Null
    Write-Ok 'will start automatically at login'
}

# ── 7. Launch ─────────────────────────────────────────────────────────────────
Write-Step 'Starting the widget'
$stopped = Stop-Widget
if ($stopped -gt 0) { Write-Step "stopped $stopped previous instance(s)" }

# ELECTRON_RUN_AS_NODE, if it is set in this shell, makes electron.exe behave as
# a plain Node binary and the app dies on a stack trace that points nowhere near
# the cause. Clearing it here costs nothing.
$env:ELECTRON_RUN_AS_NODE = $null
Start-Process -FilePath $Electron -ArgumentList "`"$AppDir`"" -WorkingDirectory $AppDir

Start-Sleep -Seconds 5
$running = @(Get-Process electron -ErrorAction SilentlyContinue |
             Where-Object { $_.Path -eq $Electron }).Count
if ($running -gt 0) {
    Write-Ok 'widget is running'
} else {
    Write-Warn 'widget did not stay running'
    Write-Host '       Run it in a console to see the error:'
    Write-Host "       cd `"$AppDir`"; npm start"
    exit 1
}

Write-Host ''
Write-Host 'Done.' -ForegroundColor Green
Write-Host '  Drag the title bar to move it; right-click to switch compact/full.'
Write-Host '  AI usage rows show "setup" until you log in to the Claude or Codex CLI.'
Write-Host '  Uninstall with:  .\install.ps1 -Uninstall'
Write-Host ''
