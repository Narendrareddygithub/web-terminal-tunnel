# Web Terminal Tunnel

A single PowerShell command that spins up a temporary, code-protected, web-accessible
terminal for your Windows laptop — scan a QR code (or open a URL) from your phone or
any other device, and control your terminal from anywhere, without touching your local
session.

Built from scratch on top of Windows' native ConPTY API (no third-party terminal-sharing
binaries) + a Cloudflare quick tunnel for the public URL.

## How it works

- `terminal_server.py` — a small FastAPI app that spawns real shells via
  [`pywinpty`](https://pypi.org/project/pywinpty/) (ConPTY) and streams them to the
  browser over WebSockets, rendered client-side with
  [`xterm.js`](https://xtermjs.org/). Supported shells: PowerShell, pwsh,
  Command Prompt, Git Bash and WSL. It runs as a **session hub**: the first
  client to enter the correct code claims the hub and gets a dashboard listing
  the active sessions (with create / close / connect controls); later visitors
  only see who owns it and how much time is left. Sessions persist across
  disconnects — the shell keeps running until you close it.
- `Start-WebTerminal.ps1` — the one-command launcher. It installs Python dependencies on
  first run, downloads `cloudflared.exe`, starts the server, opens a Cloudflare quick
  tunnel, and prints a QR code + 2-digit access code.
- Your **local session is never affected** — the remote party gets their own PTY, your
  physical console keeps working exactly as before.

## Quick start

**Requirements:** Windows 10/11, Python 3.9+ on `PATH`. Node.js 18+ only needed for the npm install method.

**Option A — npm (recommended, works from any folder/shell):**

```powershell
npm i -g wtt-web
wtt-web                # one PowerShell session by default
wtt-web -Shell cmd     # or: pwsh, bash (Git Bash), wsl
wtt-web -Sessions 3    # dashboard with 3 pre-created sessions
wtt-web -ShellChoice   # let the client pick any installed shell
```

**Option B — run from source:**

```powershell
git clone https://github.com/Narendrareddygithub/web-terminal-tunnel.git
cd web-terminal-tunnel
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\Start-WebTerminal.ps1                # one PowerShell session by default
.\Start-WebTerminal.ps1 -Shell cmd     # or: pwsh, bash (Git Bash), wsl
.\Start-WebTerminal.ps1 -Sessions 3    # dashboard with 3 pre-created sessions
.\Start-WebTerminal.ps1 -ShellChoice   # let the client pick any installed shell
```

You'll see something like:

```
==========================================================
 Remote terminal is LIVE
 URL:  https://knee-sphere-silk-wayne.trycloudflare.com
 Code: 07
 Shell: PowerShell
==========================================================
```

Open the URL on any device, enter the code, and you get a dashboard of the
active sessions. Connect to one (or click "New session" and add another, with
`-ShellChoice` letting you pick any installed shell: PowerShell, pwsh, cmd,
Git Bash, WSL). Sessions keep running while you're away — close the browser tab
and the shell survives until you close it from the dashboard. On a phone, a
small toolbar appears with Esc / Tab / Ctrl+C / Ctrl+D / Ctrl+Z / Ctrl+L /
arrow keys, since mobile keyboards can't send those directly.

Press **Ctrl+C** in the PowerShell window to shut everything down — the tunnel and the
access code both die with it.

## Security notes

- The tunnel URL is the real secret (a long, effectively unguessable random subdomain).
  Don't post it publicly.
- The 2-digit code is a lightweight confirmation step, not the primary defense — 5 wrong
  guesses from a client locks that client out for 5 minutes. The first client to enter the
  code claims the whole hub (all sessions); later visitors only see who owns it and how
  much time is left.
- Only whitelisted shells (PowerShell, pwsh, cmd, Git Bash, WSL) can be spawned — never an
  arbitrary command. With `-ShellChoice`, the landing page lists which of those are
  installed; that's already public information once someone has the URL.
- Everything is ephemeral: closing the PowerShell window tears down the server and the
  tunnel, and a fresh URL + code are generated on every run. Pass `-SessionMinutes N` to
  cap the session at N minutes (server + tunnel auto-shut-down when it elapses). Every
  code attempt is logged to `%USERPROFILE%\.web-terminal\connections.log`.
- Treat an active session like you'd treat someone standing at your unlocked laptop.
  Don't leave it running unattended.

## Roadmap / ideas

- [x] Auto-stop timer (done, tested — `-SessionMinutes N` hard limit)
- [x] Connection logging (done, tested — who connected, when, from what IP)
- [x] Option to run a different shell (done, tested — PowerShell, pwsh, cmd, Git Bash, WSL)
- [ ] Persistent named tunnel option for longer-lived setups

### Phase 2

- [x] Multi-shell support (done, tested — whitelisted shell registry in
      `terminal_server.py`, `-Shell` / `-ShellChoice` in the launcher, `/shells`
      endpoint + in-browser picker)

- [x] npm one-command launcher (done, published — `npm i -g wtt-web`, then run
      `wtt-web` from any shell/folder; thin Node wrapper around the .ps1)

- [x] Session hub / dashboard (done, tested — `-Sessions N` pre-creates N
      sessions; the owner's dashboard lists them with create / close / connect;
      sessions persist across disconnects, `-ShellChoice` picks the shell when
      creating)

Have an idea, found a bug, or want a feature? Please open an
[Issue](../../issues) or start a [Discussion](../../discussions) — feedback from anyone
trying this out is very welcome.

## License

MIT — see [LICENSE](LICENSE).
