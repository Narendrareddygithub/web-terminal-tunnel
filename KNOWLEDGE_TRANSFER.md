# Knowledge Transfer — Web Terminal Tunnel

A handoff document for incoming developers. Explains what the project is, how it
works end to end, what was built recently, the config surface, and how to build /
verify / ship. For day-to-day conventions see `AGENTS.md`; for user-facing docs see
`README.md`.

---

## 1. Project overview

`web-terminal-tunnel` turns a Windows machine into a temporary, code-protected,
web-accessible terminal. Run one command, get a Cloudflare quick-tunnel URL + a
2-digit access code + a QR code. Open the URL on any device, enter the code, and
you get a real shell (PowerShell, pwsh, cmd, Git Bash, or WSL) rendered in the
browser with xterm.js.

The whole thing is built on Windows' native **ConPTY** API via the `pywinpty`
Python package — no third-party terminal-sharing binaries. The public exposure is
a **Cloudflare quick tunnel**; the FastAPI server itself binds `127.0.0.1` only.

**Quick start (published npm launcher):**

```powershell
npm i -g wtt-web
wtt-web                # one PowerShell session by default
wtt-web -Shell cmd     # or: pwsh, bash (Git Bash), wsl
wtt-web -Sessions 3    # dashboard with 3 pre-created sessions
wtt-web -ShellChoice   # let the client pick the shell when creating sessions
```

**From source:**

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\Start-WebTerminal.ps1 [-Shell <id>] [-ShellChoice] [-Sessions N] [-SessionMinutes N]
```

The server is a **session hub**: the first client to enter the correct code
claims the whole hub for their IP and gets a dashboard listing the active
sessions, with controls to connect, create, and close them. Later visitors only
see who owns it and how much time is left. Sessions persist across client
disconnects — the shell keeps running until closed.

---

## 2. Architecture & data flow

Three components:

| Component | File(s) | Role |
|---|---|---|
| Backend | `terminal_server.py` | FastAPI app: serves the embedded HTML page (`INDEX_HTML`), a `/shells` JSON endpoint, and the `/ws` WebSocket. Spawns real shells via `pywinpty.PtyProcess`. |
| Launcher | `Start-WebTerminal.ps1` | Orchestrates everything: pip-installs deps (once), downloads `cloudflared.exe` (once), generates the random code + port, starts server + tunnel as hidden processes, prints banner + QR, waits, tears everything down on Ctrl+C. |
| npm shim | `package.json` + `bin/wtt.js` | Published as `wtt-web`. Thin, zero-dependency Node wrapper that spawns the `.ps1` via `pwsh` (fallback `powershell.exe`) and forwards all args. |

### Data flow

```
[Browser: xterm.js]
      │  wss://<random>.trycloudflare.com/ws?code=<2-digit-code>
      ▼
[Cloudflare quick tunnel: cloudflared.exe]
      │  forwards to http://127.0.0.1:<port>
      ▼
[FastAPI server on 127.0.0.1:<port>]
      │  WebSocket → spawns PtyProcess (ConPTY)
      ▼
[Real shell: powershell | pwsh | cmd | bash (Git Bash) | wsl]
```

- The server only ever sees cloudflared connecting from `localhost`, so the real
  client IP is read from forwarding headers (`CF-Connecting-IP`, then first
  `X-Forwarded-For` entry) — see `_real_ip()`.
- The server is the single source of truth. The launcher just starts it (hidden,
  logs redirected to `server.out.log` / `server.err.log`), starts the tunnel, and
  regex-parses the tunnel URL out of `cf.log`.

---

## 3. Features (recent work)

### 3.1 Multi-shell support

- `SHELLS` registry in `terminal_server.py` whitelists exactly five shells:
  `powershell`, `pwsh`, `cmd`, `bash` (Git Bash), `wsl`. Each entry declares a
  `name`, a `detect()` presence probe, an `argv()` callable, and extra `env`.
- **Only registered shells can be spawned — never an arbitrary command.** A
  client-provided `shell` query param is validated against the installed list;
  anything unknown falls back to the owner default.
- **Git Bash gotcha:** spawn `Git\usr\bin\bash.exe --login -i` with
  `MSYS_NO_PATHCONV=1`. Avoid `Git\bin\bash.exe` — it's a wrapper that
  double-forks, which can deadlock MSYS2's fork emulation under ConPTY.
- **WSL:** spawns `wsl.exe`; adds `-d <distro>` when `TERMINAL_WSL_DISTRO` is set.
- `/shells` returns `{available: [{id, name}...], default, choice_allowed}`. The
  browser picker only shows when the owner passes `-ShellChoice`
  (`TERMINAL_SHELL_CHOICE=1`).
- Launcher flags: `-Shell <id>` sets the owner's default shell;
  `-ShellChoice` enables the in-browser dropdown.

### 3.2 One-time claim + session hub

- **First correct code claims the hub** and binds it to that client IP
  (`_owner`). The owner gets a dashboard listing `_sessions` and can
  create/close/attach sessions.
- Every later client (foreign IP) is refused a terminal. They get a WebSocket
  close `4401` and a `{type:"claimed"}` JSON message with owner IP, connect
  time, and remaining time, rendered by `showClaimed()` in `INDEX_HTML` (a
  "session in use" panel with a live countdown).
- **Sessions persist across client detaches** (tmux-like): the per-session
  ConPTY + reader thread survive a WebSocket close. Re-attaching pushes output
  to the new ws; output produced while detached is dropped (no scrollback).
- **One attached ws per session.** A second attach to a busy session gets
  `{type:"busy"}` and is closed.
- **Dashboard WebSocket protocol** (`/ws` without `session`): server sends
  `{type:"dashboard", owner, remaining, sessions:[{id,name,shell,status,created}]}`,
  then accepts `{type:"create", shell}` / `{type:"close", id}` / `{type:"list"}`.
  Terminal attach (`/ws?session=<id>`) is the pre-existing input/resize/raw
  output protocol.

### 3.3 Brute-force guard

- `throttle_guard` (`terminal_server.py`): per-attempt delay grows (2s → 10s),
  and after `MAX_ATTEMPTS = 5` wrong guesses the client IP is locked out for
  `LOCK_SECONDS = 300` (5 minutes). A locked client gets a fixed 2s sleep so the
  lock state isn't revealable by timing.
- Correct-code attempts bypass the guard entirely and never increment the
  counter.

### 3.4 Session time limit

- `TERMINAL_SESSION_MINUTES` (launcher `-SessionMinutes`) arms a hard deadline
  **at server start** (in `lifespan`), so it counts down even if nobody ever
  claims the hub. A background `_monitor()` task polls every second; when the
  deadline elapses it closes all active websockets, force-terminates session
  ConPTYs (`_terminate_session`), and sets `server.should_exit`. The launcher's
  `Wait-Process` returns and the `finally` block tears down the tunnel.
- `0` (default) = no deadline.

### 3.5 Connection logging

- Every code attempt appends a line to `TERMINAL_LOG`
  (default `%USERPROFILE%\.web-terminal\connections.log`):
  `<iso-ts> <ip> <accepted|rejected|watcher|reconnected> <shell-id>`.
- `-` in the shell column for `rejected` / `watcher` (no terminal spawned).
- `accepted` fires on first claim, `reconnected` on same-IP reconnect.

### 3.6 npm launcher (`wtt-web`)

- Published as **`wtt-web@1.3.0`** on the npm registry.
- `package.json`: `bin: {"wtt-web": "bin/wtt.js"}`, `files` whitelist ships
  `bin/`, `Start-WebTerminal.ps1`, `terminal_server.py`, `requirements.txt`,
  `README.md`, `LICENSE`. `engines: node>=18`, `os: ["win32"]`. No runtime deps.
- `bin/wtt.js`: resolves the `.ps1` relative to `__dirname`; picks the shell host
  (`WTT_PSHOST` override, else `pwsh` if on `PATH`, else `powershell.exe`);
  spawns `-NoProfile -ExecutionPolicy Bypass -File <ps1> <args...>` with
  `stdio: 'inherit'` so the child shares the console. The wrapper swallows its
  own SIGINT (the child already got the Ctrl+C) and exits with the child's exit
  code so the `.ps1` `finally` teardown still runs. `--help` / `-h` prints usage.

### 3.7 QR code fix (shipped)

- `qrcode.print_ascii(invert=True)` crashed with
  `UnicodeEncodeError: 'charmap' codec can't encode character '\u2588'` on
  cp1252 consoles (the default on many Windows setups) — so the printed QR would
  break for a large share of real users.
- Replaced with an ASCII-only QR: build the matrix with `q.get_matrix()` and
  print `##` / `  ` cells. Works under any codepage.

---

## 4. File map

| File | Purpose | Ships on npm |
|---|---|---|
| `terminal_server.py` | FastAPI backend (the only server code) | ✅ |
| `Start-WebTerminal.ps1` | One-command launcher / orchestrator | ✅ |
| `bin/wtt.js` | npm shim that spawns the launcher | ✅ |
| `package.json` | npm metadata (`wtt-web`) | ✅ |
| `requirements.txt` | doc-only list of Python deps (launcher pip-installs directly) | ✅ |
| `README.md` / `LICENSE` | user docs / license | ✅ |
| `AGENTS.md` | agent/dev conventions | ❌ |
| `KNOWLEDGE_TRANSFER.md` | this doc | ❌ |
| `.gitignore` | ignore rules (`.web-terminal/`, `*.pyc`, `*.log`, `.npmrc`) | ❌ |

**Note:** the in-repo `.gitignore` entry `.web-terminal/` does not cover the real
runtime state dir (`%USERPROFILE%\.web-terminal\`). The launcher creates that dir
itself; it is intentionally outside the repo.

---

## 5. Configuration surface

### Environment variables (read by `terminal_server.py`)

| Variable | Default | Meaning |
|---|---|---|
| `TERMINAL_PORT` | `8765` | Port to bind on `127.0.0.1` |
| `TERMINAL_CODE` | — (required) | Access code; server exits if unset |
| `TERMINAL_SHELL` | `powershell` | Default shell id: `powershell\|pwsh\|cmd\|bash\|wsl` (legacy `powershell.exe` also accepted) |
| `TERMINAL_SHELL_CHOICE` | off | `1` = client can pick a shell in the browser |
| `TERMINAL_WSL_DISTRO` | default distro | WSL distro name for the `wsl` shell |
| `TERMINAL_SESSION_MINUTES` | `0` (unlimited) | Hard session limit in minutes |
| `TERMINAL_SESSIONS` | `1` | Number of sessions pre-created at startup |
| `TERMINAL_LOG` | `%USERPROFILE%\.web-terminal\connections.log` | Connection log path |

### Launcher flags (`Start-WebTerminal.ps1` / `wtt-web`)

| Flag | Meaning |
|---|---|
| `-Shell <id>` | Owner default shell: `powershell\|pwsh\|cmd\|bash\|wsl` |
| `-ShellChoice` | Show the client shell picker when creating sessions |
| `-Sessions N` | Number of sessions pre-created at startup (default 1) |
| `-SessionMinutes N` | Hard time limit (minutes), `0` = unlimited |
| `--help` / `-h` | (npm shim) print usage |

### npm shim

| Variable | Meaning |
|---|---|
| `WTT_PSHOST` | Override the PowerShell host binary (default `pwsh` → `powershell.exe`) |

---

## 6. Gotchas / hard-won lessons

- **Runtime state lives in `%USERPROFILE%\.web-terminal\`, not the repo**:
  `deps_ok.marker`, `cloudflared.exe`, `server.out.log`, `server.err.log`,
  `cf.log`, `connections.log`. Debug via those logs.
- **Launcher runs server + cloudflared as hidden processes** and parses the
  tunnel URL from `cf.log` with a regex — don't rename cloudflared output.
- **Client IP comes from headers, not the socket.** Behind cloudflared,
  `websocket.client.host` is always `127.0.0.1`. Read `CF-Connecting-IP` first.
- **Access-code check is a plaintext env var** compared with
  `secrets.compare_digest`. Keep it that way (constant-time, no storage).
- **`.npmrc` must never be committed** — it can hold an auth token. It is
  gitignored. Publish via `npm login` (2FA) or a granular bypass token.
- **Git Bash**: spawn `usr\bin\bash.exe`, not `bin\bash.exe` (double-fork
  deadlock under ConPTY). Set `MSYS_NO_PATHCONV=1`.
- **xterm.js + addon-fit load from jsDelivr CDN** in the browser; the page
  breaks offline. No bundled vendored copy.
- **Windows-only**: `pywinpty` (ConPTY) is Windows-only; the launcher targets
  PowerShell 5.1+.
- **Fresh state every run**: random code, random port (20000–40000), random
  tunnel URL. Nothing persists across runs.
- **Do not weaken the brute-force guard** casually (5 wrong guesses → 300s
  lockout, growing delay). It's the only thing standing between the URL and a
  password-guessing attack on a 2-digit code.
- **Sessions persist; output while detached is dropped.** A session's ConPTY and
  reader thread outlive the client WebSocket; closing the tab does not kill the
  shell. Re-attach pushes new output; anything produced while unattached is
  lost (no scrollback buffering). Close a session from the dashboard to kill it.
- **One attached ws per session.** The second attach gets `{type:"busy"}` and is
  closed until the first drops. Dashboard ws is separate from terminal ws.
- **Shell startup is slow under winpty on this machine** (~3s before cmd prints
  its banner, longer for PowerShell profile load). Don't write tests or asserts
  that expect output instantly after spawn.

---

## 7. Build / verify / ship

No automated tests, lint, or typecheck — manual verification only.

### Run the server alone (debugging a backend change)

```powershell
$env:TERMINAL_CODE = "42"
python terminal_server.py   # binds 127.0.0.1:8765
```

`TERMINAL_CODE` is required or the server exits.

### Full manual verification checklist (all confirmed on current build)

1. `wtt-web -Sessions 3` (or no args = 1) → dashboard lists the pre-created
   sessions; owner connects to each and they answer input via ConPTY.
2. "New session" button + shell dropdown → a session appears live, spawns on
   first connect; `-ShellChoice` controls whether the dropdown is shown.
3. Close a session → it disappears from the list and its ConPTY is killed.
4. Disconnect mid-session → shell keeps running; reconnect re-attaches with
   state intact (status shows `running`, then `attached`).
5. Second device → claimed/watcher screen (owner IP + connect time + countdown),
   no session list.
6. Wrong code → growing delay / lockout + WebSocket close.
7. Busy session → second attach refused (`{type:"busy"}`).
8. `connections.log` shows all four outcomes (`accepted|rejected|watcher|reconnected`).
9. `-SessionMinutes N` shuts down server + tunnel when the deadline elapses.
10. Tunnel E2E from a foreign device.
11. Multi-shell: each installed shell spawns and answers input via ConPTY;
    `-Shell <id>` sets the owner default; `/shells` returns the installed list.

### Publishing `wtt-web` to npm

```powershell
npm login                      # 2FA/OTP, or use a granular bypass token
npm publish                    # or: npm publish --otp <code>
```

After any wrapper change, verify before publishing:

```powershell
npm pack                       # produce the tarball
npm i -g ./wtt-web-1.3.0.tgz   # install from the tarball
wtt-web -Shell cmd             # run + Ctrl+C teardown check
```

Then bump `version` in `package.json` and `npm publish`. Never commit `.npmrc`.

### Expect banner output

```
==========================================================
 Remote terminal is LIVE
 URL:  https://<random>.trycloudflare.com
 Code: 07
 Shell: Command Prompt
 Sessions: 3
==========================================================
```
