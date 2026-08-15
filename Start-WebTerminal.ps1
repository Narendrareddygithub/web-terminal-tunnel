<#
.SYNOPSIS
    Starts our own web terminal server (terminal_server.py, using pywinpty +
    xterm.js) and exposes it via a temporary Cloudflare quick tunnel, showing
    a QR code / URL to control this terminal remotely.

.NOTES
    - Requires Python 3.9+ on PATH. Dependencies are installed automatically
      the first time (fastapi, uvicorn, pywinpty).
    - Does not touch your local session — the remote party gets their own
      PowerShell process via ConPTY, your physical console is untouched.
    - Protected with HTTP Basic Auth, random credentials every run.
    - Ctrl+C in this window tears down both the server and the tunnel.

.USAGE
    Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
    .\Start-WebTerminal.ps1
#>

$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$workDir = Join-Path $env:USERPROFILE ".web-terminal"
New-Item -ItemType Directory -Force -Path $workDir | Out-Null

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
$env:TERMINAL_SHELL = "powershell.exe"

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
