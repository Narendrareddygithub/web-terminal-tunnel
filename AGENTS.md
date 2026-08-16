# AGENTS.md

Web terminal tunnel: spawns a real shell (PowerShell, cmd, Git Bash, WSL) via ConPTY, streams it to a browser over WebSocket, exposed via a Cloudflare quick tunnel. Windows-only.

## Layout

- `terminal_server.py` — FastAPI app (only backend). Serves an embedded HTML page (`INDEX_HTML` string, xterm.js from CDN) at `/`, a JSON shell list at `/shells`, and a WebSocket at `/ws`. Spawns shells via `pywinpty` (`PtyProcess`) using a whitelisted registry (`SHELLS`): powershell, pwsh, cmd, bash (Git Bash), wsl. Owner default via `TERMINAL_SHELL`; optional client picker via `TERMINAL_SHELL_CHOICE`.
- `Start-WebTerminal.ps1` — one-command launcher / entrypoint. Everything else (deps, cloudflared, server, tunnel, QR code) is orchestrated here.
- `requirements.txt` — NOT the install source. The launcher pip-installs `fastapi`, `uvicorn[standard]`, `pywinpty`, and on demand `qrcode` into whatever Python is on PATH. Keep it in sync manually.

## Run / verify

No tests, no lint, no typecheck. Manual verification only:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\Start-WebTerminal.ps1
```

Expect output: tunnel URL (`https://<random>.trycloudflare.com`), 2-digit code, ASCII QR. Ctrl+C tears down server + tunnel.

Manual verification checklist (all confirmed working on the current build): first device claims session, second device sees claimed screen (IP + connect time + countdown), wrong code → lockout delay + close, same-IP reconnect after disconnect, `connections.log` shows all four outcomes, `-SessionMinutes` hard limit shuts everything down on time, tunnel E2E from a foreign device. Multi-shell: each available shell (powershell, pwsh, cmd, bash, wsl) spawns and answers input via ConPTY; `-Shell <id>` picks the owner default; `-ShellChoice` shows the picker and the chosen shell appears in the log; `/shells` returns the installed list.

To run the server alone (e.g. debugging a change):

```powershell
$env:TERMINAL_CODE = "42"
python terminal_server.py   # binds 127.0.0.1:8765
```

`TERMINAL_CODE` is required or the server exits. Env vars: `TERMINAL_PORT` (default 8765), `TERMINAL_CODE`, `TERMINAL_SHELL` (default `powershell`; ids: `powershell|pwsh|cmd|bash|wsl`, legacy `powershell.exe` also accepted), `TERMINAL_SHELL_CHOICE` (`1` = client can pick shell in browser), `TERMINAL_WSL_DISTRO` (optional WSL distro), `TERMINAL_SESSION_MINUTES` (default 0 = unlimited), `TERMINAL_LOG` (default `%USERPROFILE%\.web-terminal\connections.log`).

## Gotchas

- **Runtime state lives in `%USERPROFILE%\.web-terminal\`, not the repo** (deps marker, `cloudflared.exe`, logs). The in-repo `.gitignore` entry `.web-terminal/` does not cover this; the launcher creates the dir itself. Debug via `server.err.log`, `server.out.log`, `cf.log` there.
- Launcher runs the server and cloudflared as hidden processes (`Start-Process -WindowStyle Hidden`) and parses the tunnel URL from `cf.log` with a regex — don't rename cloudflared output.
- Server binds `127.0.0.1` only; the tunnel is the only public exposure. Tunnel URL is the real secret; the 2-digit code is just a confirmation step.
- **One-time claim, single session:** the first correct code claim binds the session to that client IP. Every later client (foreign IP, or same-IP while a session is live) is refused a terminal and gets a `{type:"claimed"}` status message — owner IP, connect time, countdown — rendered by the `showClaimed()` JS in `INDEX_HTML`. Same-IP reconnect is allowed only after the previous WS dropped (`_session["active"]` cleared in the handler `finally`).
- **Client IP comes from headers, not the socket.** Behind cloudflared, `websocket.client.host` is always `127.0.0.1`. `_real_ip()` (`terminal_server.py`) reads `CF-Connecting-IP`, falls back to first `X-Forwarded-For` entry.
- **Connection log:** every code attempt appends `<iso-ts> <ip> <accepted|rejected|watcher|reconnected> <shell-id>` to `TERMINAL_LOG` (default `%USERPROFILE%\.web-terminal\connections.log`). `-` in the shell column for rejected/watcher (no terminal spawned).
- **Session duration:** `TERMINAL_SESSION_MINUTES` arms a hard deadline. A background `_monitor()` task closes active websockets, terminates tracked `PtyProcess`es, and sets `server.should_exit` — launcher's `Wait-Process` then returns and the `finally` block tears down the tunnel. No limit (0) = no deadline.
- Brute-force guard in `throttle_guard` (`terminal_server.py`): 5 wrong guesses → client IP locked 300s, per-attempt delay grows to 10s. Don't weaken casually. Correct-code attempts bypass it and never increment the counter.
- Access-code check is plaintext env var compared with `secrets.compare_digest` — keep it that way.
- xterm.js + addon-fit load from jsDelivr CDN in the browser; the page breaks offline. No bundled vendored copy.
- Every run generates a fresh random code, port (20000–40000), and tunnel URL. Nothing persists across runs.
- Only runs on Windows: `pywinpty` (ConPTY) is Windows-only; the `.ps1` launcher is PowerShell 5.1.
- **Shell whitelist (`SHELLS` in `terminal_server.py`):** only registered shells can be spawned, never an arbitrary command — a client-provided `shell` query param is validated against the installed list, else the owner default is used. `TERMINAL_SHELL_CHOICE` must be `1` for the browser picker to show at all.
- **Git Bash:** spawn `Git\usr\bin\bash.exe --login -i` with `MSYS_NO_PATHCONV=1`; avoid the `bin\bash.exe` wrapper (double-fork can deadlock MSYS2 under ConPTY). WSL spawns `wsl.exe` (`-d <distro>` when `TERMINAL_WSL_DISTRO` set). Detection probes known Git paths + `shutil.which`.

## Roadmap (README Phase 2)

- **Multi-shell support** (cmd, WSL, bash, etc.): DONE — registry + picker shipped (see Layout/Gotchas).
- **npm one-command launcher** (`wtt`): wraps the whole flow in a single npm command.
