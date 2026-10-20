# Watchdog for the forward test: restarts the bot if it exits or crashes.
# The bot restores position / risk state from SQLite, so restarting is safe.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\forward_bot.ps1
#
# Stop: create logs\forward_bot.stop, then end the python process; the watchdog
# sees the file and exits instead of restarting.
#
# Two separate logs, both plain UTF-8:
#   logs\forward_bot.log       - the bot's own stdout/stderr
#   logs\forward_watchdog.log  - start / exit / restart events
#
# Single instance: logs\forward_bot.lock holds the watchdog PID; a second launch
# exits instead of starting a second bot on the same account.
#
# NOTE: keep this file ASCII-only. Windows PowerShell 5.1 reads .ps1 files
# without a BOM as ANSI, and non-ASCII text can break the parser.
param(
    [string]$Config = "config.forward.yaml",
    [int]$RestartDelay = 60
)

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$env:PYTHONIOENCODING = "utf-8"

$python = Join-Path $root ".venv\Scripts\python.exe"
$logDir = Join-Path $root "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$botLog = Join-Path $logDir "forward_bot.log"
$wdLog = Join-Path $logDir "forward_watchdog.log"
$stopFile = Join-Path $logDir "forward_bot.stop"
$lockFile = Join-Path $logDir "forward_bot.lock"

function Write-Log([string]$Message) {
    Add-Content -Path $wdLog -Value ("{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message) -Encoding UTF8
}

if (-not (Test-Path $python)) { Write-Output "python not found: $python"; exit 1 }

if (Test-Path $lockFile) {
    $oldPid = Get-Content $lockFile -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($oldPid -and (Get-Process -Id $oldPid -ErrorAction SilentlyContinue)) {
        Write-Log ("another watchdog is already running (pid {0}), exiting" -f $oldPid)
        exit 0
    }
}
Set-Content -Path $lockFile -Value $PID -Encoding ASCII

try {
    Write-Log ("start config={0} delay={1}s pid={2}" -f $Config, $RestartDelay, $PID)
    $round = 0
    while ($true) {
        if (Test-Path $stopFile) { Write-Log "stop file found, exiting"; break }
        $round++
        Write-Log ("launch #{0}" -f $round)
        # cmd handles the append redirect, so the bot's UTF-8 output stays UTF-8
        cmd /c "`"$python`" main.py testnet --config `"$Config`" --market perp >> `"$botLog`" 2>&1"
        $code = $LASTEXITCODE
        Write-Log ("exit code {0}" -f $code)
        if (Test-Path $stopFile) { Write-Log "stop file found, exiting"; break }
        Start-Sleep -Seconds $RestartDelay
    }
}
finally {
    Remove-Item $lockFile -Force -ErrorAction SilentlyContinue
    Write-Log "watchdog stopped"
}
