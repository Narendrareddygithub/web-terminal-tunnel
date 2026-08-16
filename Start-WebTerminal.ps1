<#
.SYNOPSIS
    Starts our own web terminal server (terminal_server.py, using pywinpty +
    xterm.js) and exposes it via a temporary Cloudflare quick tunnel, showing
    a QR code / URL to control this terminal remotely.

.DESCRIPTION
    The first client to enter the correct 2-digit code claims the session and
    gets a live shell. Any later client is shown who owns the session and how
    long is left, and is refused a terminal.

.PARAMETER SessionMinutes
    Optional. Hard time limit (in minutes) for the whole session. When it
    elapses, the server (and tunnel) shut down. 0 (default) means no limit.

.PARAMETER Shell
    Optional. Default shell for the remote session: powershell, pwsh, cmd,
    bash (Git Bash) or wsl. Default: powershell.

.PARAMETER ShellChoice
    Optional. When set, the client can pick any installed shell from a
    dropdown in the browser instead of the -Shell default.

.NOTES
    - Requires Python 3.9+ on PATH. Dependencies are installed automatically
      the first time (fastapi, uvicorn, pywinpty).
    - Does not touch your local session — the remote party gets their own
      shell process via ConPTY, your physical console is untouched.
    - Protected with a 2-digit code; the first client to authenticate claims
      the session.
    - Ctrl+C in this window tears down both the server and the tunnel.

.USAGE
    Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
    .\Start-WebTerminal.ps1                        # PowerShell, no time limit
    .\Start-WebTerminal.ps1 -SessionMinutes 30
    .\Start-WebTerminal.ps1 -Shell cmd
    .\Start-WebTerminal.ps1 -ShellChoice           # client picks shell
#>

param(
    [int]$SessionMinutes = 0,
    [ValidateSet("powershell", "pwsh", "cmd", "bash", "wsl")]
    [string]$Shell = "powershell",
    [switch]$ShellChoice
)

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$workDir = Join-Path $env:USERPROFILE ".web-terminal"
New-Item -ItemType Directory -Force -Path $workDir | Out-Null

$shellNames = @{
    powershell = "PowerShell"
    pwsh       = "PowerShell 7 (pwsh)"
    cmd        = "Command Prompt"
    bash       = "Git Bash"
    wsl        = "WSL (Linux)"
}

function Test-ShellAvailable([string]$id) {
    switch ($id) {
        "powershell" { return $true }
        "cmd"        { return $true }
        "pwsh"       { return $null -ne (Get-Command pwsh -ErrorAction SilentlyContinue) }
        "bash" {
            $p = Join-Path $env:ProgramFiles "Git\usr\bin\bash.exe"
            $q = Join-Path ${env:ProgramFiles(x86)} "Git\usr\bin\bash.exe"
            return (Test-Path $p) -or (Test-Path $q) -or ($null -ne (Get-Command bash -ErrorAction SilentlyContinue))
        }
        "wsl"        { return $null -ne (Get-Command wsl -ErrorAction SilentlyContinue) }
    }
    return $false
}

if (-not (Test-ShellAvailable $Shell)) {
    Write-Host "Shell '$Shell' ($($shellNames[$Shell])) is not available on this machine." -ForegroundColor Red
    Write-Host "Choose from: powershell, pwsh, cmd, bash, wsl." -ForegroundColor Yellow
    exit 1
}

# ---------------------------------------------------------------------------
# 1. Check Python is available
# ---------------------------------------------------------------------------
try {
    $null = python --version
} catch {
    Write-Host "Python not found on PATH. Install Python 3.9+ and re-run." -ForegroundColor Red
    exit 1
}

# ---------------------------------------------------------------------------
# 2. Install dependencies (once) — checked via a marker file so re-runs are fast
# ---------------------------------------------------------------------------
$marker = Join-Path $workDir "deps_ok.marker"
if (-not (Test-Path $marker)) {
    Write-Host "Installing Python dependencies (fastapi, uvicorn, pywinpty)..."
    python -m pip install --quiet fastapi "uvicorn[standard]" pywinpty
    if ($LASTEXITCODE -ne 0) {
        Write-Host "pip install failed. See errors above." -ForegroundColor Red
        exit 1
    }
    New-Item -ItemType File -Force -Path $marker | Out-Null
}

# ---------------------------------------------------------------------------
# 3. Ensure cloudflared.exe (tunnel client) is present
# ---------------------------------------------------------------------------
$cfPath = Join-Path $workDir "cloudflared.exe"
if (-not (Test-Path $cfPath)) {
    Write-Host "Downloading cloudflared (tunnel client)..."
    Invoke-WebRequest -Uri "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe" -OutFile $cfPath
}

# ---------------------------------------------------------------------------
# 4. Random 2-digit access code + port for this run
# ---------------------------------------------------------------------------
$accessCode = "{0:D2}" -f (Get-Random -Minimum 0 -Maximum 100)
$port = Get-Random -Minimum 20000 -Maximum 40000

# ---------------------------------------------------------------------------
# 5. Start the Python terminal server
# ---------------------------------------------------------------------------
$serverScript = Join-Path $scriptDir "terminal_server.py"
if (-not (Test-Path $serverScript)) {
    Write-Host "terminal_server.py not found next to this script." -ForegroundColor Red
    exit 1
}

Write-Host "`nStarting web terminal server on 127.0.0.1:$port ..."
$env:TERMINAL_PORT = $port
$env:TERMINAL_CODE = $accessCode
$env:TERMINAL_SHELL = $Shell
if ($ShellChoice) { $env:TERMINAL_SHELL_CHOICE = "1" }
$env:TERMINAL_SESSION_MINUTES = $SessionMinutes

$serverOutLog = Join-Path $workDir "server.out.log"
$serverErrLog = Join-Path $workDir "server.err.log"
$serverProc = Start-Process -FilePath "python" -ArgumentList @("`"$serverScript`"") `
    -RedirectStandardOutput $serverOutLog -RedirectStandardError $serverErrLog `
    -PassThru -WindowStyle Hidden

Start-Sleep -Seconds 2
if ($serverProc.HasExited) {
    Write-Host "Server failed to start. Log:" -ForegroundColor Red
    Get-Content $serverOutLog -ErrorAction SilentlyContinue
    Get-Content $serverErrLog -ErrorAction SilentlyContinue
    exit 1
}

# ---------------------------------------------------------------------------
# 6. Start Cloudflare quick tunnel and grab its URL
# ---------------------------------------------------------------------------
Write-Host "Starting Cloudflare quick tunnel..."
$cfLog = Join-Path $workDir "cf.log"
$cfProc = Start-Process -FilePath $cfPath `
    -ArgumentList @("tunnel", "--url", "http://127.0.0.1:$port") `
    -RedirectStandardError $cfLog -PassThru -WindowStyle Hidden

$tunnelUrl = $null
$deadline = (Get-Date).AddSeconds(30)
while (-not $tunnelUrl -and (Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 500
    if (Test-Path $cfLog) {
        $m = Select-String -Path $cfLog -Pattern "https://[a-zA-Z0-9\-]+\.trycloudflare\.com" | Select-Object -First 1
        if ($m) { $tunnelUrl = $m.Matches[0].Value }
    }
}

if (-not $tunnelUrl) {
    Write-Host "Could not establish the tunnel. Check the log: $cfLog" -ForegroundColor Red
    Stop-Process -Id $serverProc.Id -ErrorAction SilentlyContinue
    exit 1
}

Write-Host "`n=========================================================="
Write-Host " Remote terminal is LIVE"
Write-Host " URL:  $tunnelUrl"
Write-Host " Code: $accessCode"
Write-Host " Shell: $($shellNames[$Shell])"
if ($SessionMinutes -gt 0) {
    Write-Host " Session ends in $SessionMinutes min"
}
if ($ShellChoice) {
    Write-Host " Client may switch shell in the browser"
}
Write-Host "=========================================================="
Write-Host " Open the URL, type the 2-digit code into the on-page prompt."
Write-Host " (The URL itself is the real secret - keep it private. The"
Write-Host " code is a lightweight confirmation step, backed by a"
Write-Host " lockout after repeated wrong guesses.)"
Write-Host "=========================================================="

# ---------------------------------------------------------------------------
# 7. Show a QR code for the URL directly in this terminal
# ---------------------------------------------------------------------------
python -c "import qrcode" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "`nInstalling 'qrcode' Python package (one-time)..."
    python -m pip install --quiet qrcode
}
$qrScript = "import sys, qrcode; q = qrcode.QRCode(border=1); q.add_data(sys.argv[1]); q.print_ascii(invert=True)"
python -c $qrScript "$tunnelUrl"

Write-Host "`nScan the QR code above, or open the URL on any device."
Write-Host "Your local session on THIS laptop is unaffected the whole time."
Write-Host "Press Ctrl+C to stop sharing and tear everything down."

# ---------------------------------------------------------------------------
# 8. Stay alive; clean up on exit
# ---------------------------------------------------------------------------
try {
    Wait-Process -Id $serverProc.Id
} finally {
    Write-Host "`nShutting down terminal server and tunnel..."
    Stop-Process -Id $serverProc.Id -ErrorAction SilentlyContinue
    Stop-Process -Id $cfProc.Id -ErrorAction SilentlyContinue
}
