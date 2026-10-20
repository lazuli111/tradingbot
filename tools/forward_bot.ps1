# Supervises the multi-symbol forward test: one bot process per symbol plus the
# portfolio guard, all restarted from this single watchdog.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\forward_bot.ps1 -Symbols "BTC/USDT,SOL/USDT"
#
# Stop: create logs\forward_bot.stop; the watchdog sees it within 5s, kills the
# children and exits. The bots save state each bar, so a restart resumes cleanly.
#
# Logs (all UTF-8):
#   logs\forward_<SYMBOL>.log   - that symbol's bot output
#   logs\forward_guard.log      - portfolio guard output
#   logs\forward_watchdog.log   - start / exit / restart events
#
# NOTE: keep this file ASCII-only. Windows PowerShell 5.1 reads .ps1 files
# without a BOM as ANSI; non-ASCII text can swallow a newline and merge the next
# line into a comment, which silently breaks the script.
param(
    [string]$Config = "config.portfolio.yaml",
    [string[]]$Symbols = @("BTC/USDT", "SOL/USDT"),
    [int]$RestartDelay = 60,
    [int]$GuardSeconds = 30
)

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$env:PYTHONIOENCODING = "utf-8"

$python = Join-Path $root ".venv\Scripts\python.exe"
$logDir = Join-Path $root "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$wdLog = Join-Path $logDir "forward_watchdog.log"
$guardLog = Join-Path $logDir "forward_guard.log"
$stopFile = Join-Path $logDir "forward_bot.stop"
$lockFile = Join-Path $logDir "forward_bot.lock"

function Write-Log([string]$Message) {
    Add-Content -Path $wdLog -Value ("{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message) -Encoding UTF8
}

function Start-Child([string]$Symbol) {
    $tag = ($Symbol -split "/")[0]
    $childLog = Join-Path $logDir ("forward_{0}.log" -f $tag)
    # Write a tiny launcher .cmd instead of fighting Start-Process quoting; it also
    # gives us append semantics (Start-Process -RedirectStandardOutput truncates).
    $launcher = Join-Path $logDir ("run_{0}.cmd" -f $tag)
    $line = ("`"{0}`" main.py testnet --config `"{1}`" --symbol `"{2}`" >> `"{3}`" 2>&1" -f $python, $Config, $Symbol, $childLog)
    Set-Content -Path $launcher -Value @("@echo off", $line) -Encoding ASCII
    return Start-Process -FilePath $launcher -WorkingDirectory $root -WindowStyle Hidden -PassThru
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

# -Symbols arrives as an array when the comma is unquoted on the command line,
# so accept comma / whitespace / semicolon as separators.
$symbols = $Symbols -split "[,\s;]+" | Where-Object { $_ }
$children = @{}
Write-Log ("parsed count={0} items={1}" -f @($symbols).Count, ($symbols -join "|"))

try {
    Write-Log ("start symbols={0} config={1} pid={2}" -f ($symbols -join " "), $Config, $PID)
    $lastGuard = Get-Date
    while ($true) {
        if (Test-Path $stopFile) { Write-Log "stop file found, exiting"; break }

        foreach ($symbol in $symbols) {
            $child = $children[$symbol]
            if ($null -eq $child -or $child.HasExited) {
                $code = "-"
                if ($child) { $code = $child.ExitCode }
                Write-Log ("launch {0} (previous exit {1})" -f $symbol, $code)
                $children[$symbol] = Start-Child $symbol
            }
        }

        if (((Get-Date) - $lastGuard).TotalSeconds -ge $GuardSeconds) {
            cmd /c "`"$python`" tools\portfolio_guard.py --once >> `"$guardLog`" 2>&1"
            if ($LASTEXITCODE -eq 3) {
                Write-Log "portfolio guard triggered: positions flattened, halting"
                New-Item -ItemType File -Path $stopFile -Force | Out-Null
            }
            $lastGuard = Get-Date
        }

        Start-Sleep -Seconds 5
    }
}
finally {
    foreach ($symbol in $symbols) {
        $child = $children[$symbol]
        if ($child -and -not $child.HasExited) { Stop-Process -Id $child.Id -Force -ErrorAction SilentlyContinue }
    }
    Remove-Item $lockFile -Force -ErrorAction SilentlyContinue
    Write-Log "watchdog stopped"
}


