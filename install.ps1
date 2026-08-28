<#
.SYNOPSIS
    Installs ip-bar: checks Python, installs psutil, creates .env, and
    registers the widget to start automatically at login.

.DESCRIPTION
    Run once from the folder containing ip_bar.py:

        powershell -ExecutionPolicy Bypass -File .\install.ps1

    Re-running is safe — it updates the autostart entry and leaves an
    existing .env untouched.

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

$AppName    = 'IPBar'
$RunKey     = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$WidgetPath = Join-Path $ScriptDir 'ip_bar.py'

function Write-Step { param($m) Write-Host "  $m" -ForegroundColor Cyan }
function Write-Ok   { param($m) Write-Host "  OK   $m" -ForegroundColor Green }
function Write-Warn { param($m) Write-Host "  WARN $m" -ForegroundColor Yellow }
function Write-Err  { param($m) Write-Host "  FAIL $m" -ForegroundColor Red }

function Stop-Widget {
    # Match on the command line, so we never kill an unrelated pythonw.
    $procs = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
             Where-Object { $_.CommandLine -like '*ip_bar.py*' }
    foreach ($p in $procs) {
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }
    return @($procs).Count
}

Write-Host ''
Write-Host 'ip-bar installer' -ForegroundColor White
Write-Host '----------------'

# ── Uninstall ─────────────────────────────────────────────────────────────────
if ($Uninstall) {
    $stopped = Stop-Widget
    if ($stopped -gt 0) { Write-Ok "stopped $stopped running instance(s)" }
    if (Get-ItemProperty -Path $RunKey -Name $AppName -ErrorAction SilentlyContinue) {
        Remove-ItemProperty -Path $RunKey -Name $AppName
        Write-Ok 'autostart entry removed'
    } else {
        Write-Step 'no autostart entry found'
    }
    Write-Host ''
    Write-Host 'Uninstalled. Files were left in place; delete this folder to remove them.'
    Write-Host ''
    exit 0
}

# ── 1. The widget itself ──────────────────────────────────────────────────────
Write-Step 'Checking files'
if (-not (Test-Path -LiteralPath $WidgetPath)) {
    Write-Err "ip_bar.py not found next to this script ($ScriptDir)."
    Write-Host '       Run install.ps1 from inside the folder you cloned.'
    exit 1
}
Write-Ok 'ip_bar.py found'

# ── 2. Python ─────────────────────────────────────────────────────────────────
# pythonw.exe runs without a console window, which is what an always-on-top
# widget needs; python.exe would leave a black box on screen.
Write-Step 'Looking for Python'
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
# Quotes inside a -c argument are eaten by PowerShell's native-command
# quoting, so this expression deliberately contains none.
$version = (& $python -c 'import sys; print(sys.version.split()[0])' 2>&1 |
            Select-Object -First 1 | ForEach-Object { $_.ToString().Trim() })
if ($version -notmatch '^\d+\.\d+') {
    Write-Err "Could not run $python"
    Write-Host "       $version"
    exit 1
}
Write-Ok "Python $version at $pythonw"

# tkinter ships with the python.org installer but is a separate package in
# some distributions, so check rather than assume.
& $python -c 'import tkinter' 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Err 'tkinter is missing from this Python installation.'
    Write-Host '       Reinstall Python from python.org (tkinter is included), or'
    Write-Host '       install the tk package for your distribution.'
    exit 1
}
Write-Ok 'tkinter available'

# ── 3. Dependency ─────────────────────────────────────────────────────────────
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

# ── 4. Configuration ──────────────────────────────────────────────────────────
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

# ── 5. Autostart ──────────────────────────────────────────────────────────────
# The widget re-registers itself at startup whenever its own path stops
# matching the stored command, so moving the folder later needs no extra step.
if ($NoAutostart) {
    Write-Step 'Skipping autostart (-NoAutostart)'
} else {
    Write-Step 'Registering autostart'
    $command = '"{0}" "{1}"' -f $pythonw, $WidgetPath
    New-ItemProperty -Path $RunKey -Name $AppName -Value $command `
                     -PropertyType String -Force | Out-Null
    Write-Ok 'will start automatically at login'
}

# ── 6. Launch ─────────────────────────────────────────────────────────────────
Write-Step 'Starting the widget'
$stopped = Stop-Widget
if ($stopped -gt 0) { Write-Step "stopped $stopped previous instance(s)" }
Start-Process -FilePath $pythonw -ArgumentList "`"$WidgetPath`""

Start-Sleep -Seconds 3
$running = @(Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
             Where-Object { $_.CommandLine -like '*ip_bar.py*' }).Count
if ($running -gt 0) {
    Write-Ok 'widget is running'
} else {
    Write-Warn 'widget did not stay running'
    Write-Host "       Run it in a console to see the error:"
    Write-Host "       $python `"$WidgetPath`""
    exit 1
}

Write-Host ''
Write-Host 'Done.' -ForegroundColor Green
Write-Host '  Right-click the widget for the menu (log, autostart, quit).'
Write-Host '  AI usage rows show "setup" until you log in to the Claude or Codex CLI.'
Write-Host '  Uninstall with:  .\install.ps1 -Uninstall'
Write-Host ''
