# Web Terminal Tunnel

A single PowerShell command that spins up a temporary, code-protected, web-accessible
terminal for your Windows laptop — scan a QR code (or open a URL) from your phone or
any other device, and control your terminal from anywhere, without touching your local
session.

Built from scratch on top of Windows' native ConPTY API (no third-party terminal-sharing
binaries) + a Cloudflare quick tunnel for the public URL.

## How it works

- `terminal_server.py` — a small FastAPI app that spawns a real PowerShell process via
  [`pywinpty`](https://pypi.org/project/pywinpty/) (ConPTY) and streams it to the browser
  over a WebSocket, rendered client-side with [`xterm.js`](https://xtermjs.org/).
- `Start-WebTerminal.ps1` — the one-command launcher. It installs Python dependencies on
  first run, downloads `cloudflared.exe`, starts the server, opens a Cloudflare quick
  tunnel, and prints a QR code + 2-digit access code.
- Your **local session is never affected** — the remote party gets their own PTY, your
  physical console keeps working exactly as before.

## Quick start

**Requirements:** Windows 10/11, Python 3.9+ on `PATH`.

```powershell
git clone https://github.com/<your-username>/web-terminal-tunnel.git
cd web-terminal-tunnel
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\Start-WebTerminal.ps1
```

You'll see something like:

```
==========================================================
 Remote terminal is LIVE
 URL:  https://knee-sphere-silk-wayne.trycloudflare.com
 Code: 07
==========================================================
```

Open the URL on any device, enter the code, and you're in. On a phone, a small toolbar
appears with Esc / Tab / Ctrl+C / Ctrl+D / Ctrl+Z / Ctrl+L / arrow keys, since mobile
keyboards can't send those directly.

Press **Ctrl+C** in the PowerShell window to shut everything down — the tunnel and the
access code both die with it.

## Security notes

- The tunnel URL is the real secret (a long, effectively unguessable random subdomain).
  Don't post it publicly.
- The 2-digit code is a lightweight confirmation step, not the primary defense — 5 wrong
  guesses from a client locks that client out for 5 minutes.
- Everything is ephemeral: closing the PowerShell window tears down the server and the
  tunnel, and a fresh URL + code are generated on every run.
- Treat an active session like you'd treat someone standing at your unlocked laptop.
  Don't leave it running unattended.

## Roadmap / ideas

- [ ] Auto-stop timer (session ends automatically after N minutes of inactivity)
- [ ] Connection logging (who connected, when, from what IP)
- [ ] Option to run a different shell (cmd, WSL, pwsh)
- [ ] Persistent named tunnel option for longer-lived setups

Have an idea, found a bug, or want a feature? Please open an
[Issue](../../issues) or start a [Discussion](../../discussions) — feedback from anyone
trying this out is very welcome.

## License

MIT — see [LICENSE](LICENSE).
