"""
terminal_server.py
A minimal web-based terminal for Windows: spawns a real console via ConPTY
(pywinpty) and streams it to a browser over a WebSocket using xterm.js.

Access is gated by a short numeric code, entered via an in-page prompt.
The first client to enter the correct code claims the session; every later
client is refused a terminal and only shown who owns the session and how much
time is left. Because a short code has a small keyspace, it is treated as a
confirmation step (similar to GitHub/Microsoft "number matching" device
approval) rather than the sole line of defense -- the real secret is the
hard-to-guess Cloudflare quick-tunnel URL itself. Wrong guesses are slowed
with a growing delay, and a client that fails repeatedly is locked out
entirely for a cooldown period.

Configured via environment variables (set by the launcher script):
  TERMINAL_PORT           - port to bind on 127.0.0.1 (default 8765)
  TERMINAL_CODE           - access code (required)
  TERMINAL_SHELL          - default shell id: powershell|pwsh|cmd|bash|wsl
                            (default "powershell"; "powershell.exe" also accepted)
  TERMINAL_SHELL_CHOICE   - "1" lets the client pick a shell in the browser (default off)
  TERMINAL_WSL_DISTRO     - optional WSL distro name for the wsl shell (default distro)
  TERMINAL_SESSION_MINUTES- hard session limit in minutes, 0 = no limit
  TERMINAL_LOG            - connection log path (default ~/.web-terminal/connections.log)
"""

import asyncio
import json
import os
import secrets
import shutil
import sys
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
import uvicorn

try:
    from winpty import PtyProcess
except ImportError:
    sys.exit("pywinpty is not installed. Run: pip install pywinpty")

PORT = int(os.environ.get("TERMINAL_PORT", "8765"))
CODE = os.environ.get("TERMINAL_CODE")
SESSION_SECONDS = int(os.environ.get("TERMINAL_SESSION_MINUTES", "0")) * 60
LOG_PATH = os.environ.get(
    "TERMINAL_LOG",
    os.path.join(os.path.expanduser("~"), ".web-terminal", "connections.log"),
)

# --- shell registry --------------------------------------------------------
# Whitelisted shells only: the client can pick any installed shell from this
# list, never an arbitrary command. `argv` is a callable returning the argv to
# spawn; `env` is extra environment for that shell; `detect` checks presence.


def _git_bash_path():
    bases = [
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
    ]
    for base in bases:
        # Prefer usr\bin\bash.exe: bin\bash.exe is a wrapper that double-forks,
        # which can deadlock MSYS2's fork emulation under ConPTY.
        for sub in ("usr\\bin\\bash.exe", "bin\\bash.exe"):
            p = os.path.join(base, "Git", sub)
            if os.path.isfile(p):
                return p
    return shutil.which("bash")


def _wsl_argv():
    distro = os.environ.get("TERMINAL_WSL_DISTRO")
    if distro:
        return ["wsl.exe", "-d", distro]
    return ["wsl.exe"]


SHELLS = {
    "powershell": {
        "name": "PowerShell",
        "detect": lambda: True,
        "argv": lambda: ["powershell.exe"],
        "env": {},
    },
    "pwsh": {
        "name": "PowerShell 7",
        "detect": lambda: shutil.which("pwsh") is not None,
        "argv": lambda: ["pwsh.exe"],
        "env": {},
    },
    "cmd": {
        "name": "Command Prompt",
        "detect": lambda: True,
        "argv": lambda: ["cmd.exe"],
        "env": {},
    },
    "bash": {
        "name": "Git Bash",
        "detect": lambda: _git_bash_path() is not None,
        "argv": lambda: [_git_bash_path(), "--login", "-i"],
        # Disable MSYS2 POSIX-to-Windows path rewriting; plain Git Bash on this
        # machine mangles args that look like /paths when calling native exes.
        "env": {"MSYS_NO_PATHCONV": "1"},
    },
    "wsl": {
        "name": "WSL (Linux)",
        "detect": lambda: shutil.which("wsl") is not None,
        "argv": _wsl_argv,
        "env": {},
    },
}

_ALIASES = {
    "powershell.exe": "powershell",
    "pwsh.exe": "pwsh",
    "cmd.exe": "cmd",
    "bash.exe": "bash",
    "wsl.exe": "wsl",
}


def _default_shell():
    value = (os.environ.get("TERMINAL_SHELL") or "powershell").strip().lower()
    if value in SHELLS:
        return value
    return _ALIASES.get(value, "powershell")


DEFAULT_SHELL_ID = _default_shell()
SHELL_CHOICE = os.environ.get("TERMINAL_SHELL_CHOICE", "0") == "1"


def available_shells():
    return {sid: spec for sid, spec in SHELLS.items() if spec["detect"]()}

if not CODE:
    sys.exit("TERMINAL_CODE environment variable is required.")

SERVER = None  # uvicorn.Server instance, set in __main__

async def _monitor() -> None:
    """Background task: end the session when its fixed duration elapses."""
    while True:
        await asyncio.sleep(1)
        with _state_lock:
            until = _session["until"]
            ws_snapshot = list(_active_ws)
            proc_snapshot = list(_procs)
        if until is None or time.monotonic() < until:
            continue
        print("Session duration reached; shutting down.")
        for w in ws_snapshot:
            try:
                await w.close()
            except Exception:
                pass
        for p in proc_snapshot:
            try:
                p.terminate(force=True)
            except Exception:
                pass
        if SERVER is not None:
            SERVER.should_exit = True
        return


@asynccontextmanager
async def lifespan(_app):
    task = asyncio.create_task(_monitor())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(lifespan=lifespan)

# --- brute-force protection -------------------------------------------------
# Short codes have a small keyspace, so on top of a growing per-attempt
# delay, a client gets fully locked out for LOCK_SECONDS after
# MAX_ATTEMPTS wrong guesses in a row. Only wrong guesses count; correct
# codes bypass this entirely.
MAX_ATTEMPTS = 5
LOCK_SECONDS = 300  # 5 minutes

_state = {}  # client_ip -> {"count": int, "locked_until": float}
_state_lock = threading.Lock()

# Single-session claim state: the first correct code claim binds the session to
# one client IP. Other clients only see who owns the session and how long is
# left (watcher screen); they never get a terminal.
_session = {"ip": None, "at_wall": 0.0, "active": False, "until": None}
_active_ws = set()  # websockets with an open PTY session
_procs = set()      # live PtyProcess objects, for shutdown cleanup


def _real_ip(websocket: WebSocket) -> str:
    # The server only ever sees cloudflared connecting from localhost, so the
    # real client IP must come from the tunnel's forwarding headers.
    headers = websocket.headers
    ip = headers.get("cf-connecting-ip")
    if not ip:
        fwd = headers.get("x-forwarded-for")
        if fwd:
            ip = fwd.split(",")[0].strip()
    if not ip:
        ip = websocket.client.host if websocket.client else "unknown"
    return ip


def log_line(ip: str, result: str, shell: str = "-") -> None:
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(
                f"{datetime.now().isoformat(timespec='seconds')} {ip} {result} {shell}\n"
            )
    except Exception:
        pass


async def throttle_guard(client_ip: str) -> bool:
    """Brute-force defense for wrong codes. Applies a growing per-attempt delay
    and locks the client out after MAX_ATTEMPTS. Returns False if the client is
    locked out (a fixed 2s sleep is consumed so lock state isn't revealed)."""
    now = time.monotonic()

    with _state_lock:
        entry = _state.get(client_ip, {"count": 0, "locked_until": 0.0})
        locked = entry["locked_until"] > now

    if locked:
        await asyncio.sleep(2)
        return False

    with _state_lock:
        entry = _state.get(client_ip, {"count": 0, "locked_until": 0.0})
        entry["count"] += 1
        if entry["count"] >= MAX_ATTEMPTS:
            entry["locked_until"] = now + LOCK_SECONDS
            entry["count"] = 0
        _state[client_ip] = entry
        delay = min(entry["count"] * 2, 10)

    await asyncio.sleep(delay)
    return True


INDEX_HTML = """<!doctype html>
<html>
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no"/>
<meta name="referrer" content="no-referrer"/>
<title>Remote Terminal</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/css/xterm.min.css"/>
<style>
  * { box-sizing: border-box; }
  html, body { margin:0; padding:0; background:#1e1e1e; height:100%; overflow:hidden; }
  #app { height:100vh; display:flex; flex-direction:column; }
  #terminal { flex:1; min-height:0; padding:4px; display:none; }
  #loaderr { color:#f55; font-family:monospace; padding:12px; white-space:pre-wrap; }

  #gate {
    height:100vh; display:flex; flex-direction:column; align-items:center; justify-content:center;
    font-family:Consolas, monospace; color:#ddd; gap:16px; padding:16px;
  }
  #gate .label { font-size:15px; opacity:0.8; }
  #gate input {
    font-family:Consolas, monospace; font-size:36px; letter-spacing:14px; text-align:center;
    width:140px; padding:10px 4px 10px 18px; background:#111; color:#eee;
    border:1px solid #444; border-radius:8px;
  }
  #gate button {
    font-family:Consolas, monospace; font-size:16px; padding:10px 24px; cursor:pointer;
    background:#2d6cdf; color:#fff; border:none; border-radius:6px;
  }
  #gate select {
    font-family:Consolas, monospace; font-size:15px; padding:8px 10px;
    background:#111; color:#eee; border:1px solid #444; border-radius:6px;
  }
  #gateerr { color:#f55; font-size:13px; min-height:16px; }

  /* Session-already-claimed screen */
  #claimed {
    height:100vh; display:none; flex-direction:column; align-items:center; justify-content:center;
    font-family:Consolas, monospace; color:#ddd; gap:12px; padding:24px; text-align:center;
  }
  #claimed .claim-title { font-size:18px; color:#ff9d00; }
  #claimed .claim-info { font-size:14px; opacity:0.9; line-height:1.7; }
  #claimed .claim-count { font-size:30px; font-weight:bold; color:#eee; }

  /* Touch toolbar: keys a phone keyboard doesn't send */
  #toolbar {
    display:none;
    flex-wrap: nowrap;
    overflow-x:auto;
    gap:6px;
    padding:6px;
    background:#252526;
    border-top:1px solid #3a3a3a;
    -webkit-overflow-scrolling: touch;
  }
  #toolbar button {
    flex: 0 0 auto;
    font-family:Consolas, monospace;
    font-size:13px;
    padding:10px 12px;
    background:#3a3a3a;
    color:#eee;
    border:1px solid #4a4a4a;
    border-radius:6px;
    white-space:nowrap;
  }
  #toolbar button:active { background:#2d6cdf; }

  /* Show the touch toolbar on small / coarse-pointer (touch) screens */
  @media (pointer: coarse), (max-width: 700px) {
    #toolbar { display:flex; }
    #gate input { font-size:30px; letter-spacing:10px; width:110px; }
  }
</style>
</head>
<body>
<div id="gate">
  <div class="label">Enter access code</div>
  <input id="code" maxlength="2" inputmode="numeric" pattern="[0-9]*"
         autocomplete="off" autocapitalize="off" spellcheck="false" autofocus/>
  <div id="shellrow" style="display:none; flex-direction:column; align-items:center; gap:6px;">
    <div class="label">Shell</div>
    <select id="shellsel"></select>
  </div>
  <button id="go">Connect</button>
  <div id="gateerr"></div>
</div>

<div id="claimed">
  <div class="claim-title">Session already in use</div>
  <div class="claim-info">
    Claimed by IP <span id="claim-ip"></span><br/>
    Connected: <span id="claim-at"></span><br/>
    Session ends in:
  </div>
  <div class="claim-count" id="claim-left">--:--</div>
</div>

<div id="app">
  <div id="terminal"></div>
  <div id="toolbar">
    <button data-seq="\\u001b">Esc</button>
    <button data-seq="\\t">Tab</button>
    <button data-seq="\\u0003">Ctrl+C</button>
    <button data-seq="\\u0004">Ctrl+D</button>
    <button data-seq="\\u001a">Ctrl+Z</button>
    <button data-seq="\\u000c">Ctrl+L</button>
    <button data-seq="\\u001b[A">&uarr;</button>
    <button data-seq="\\u001b[B">&darr;</button>
    <button data-seq="\\u001b[D">&larr;</button>
    <button data-seq="\\u001b[C">&rarr;</button>
  </div>
</div>
<div id="loaderr"></div>

<script src="https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/lib/xterm.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/@xterm/addon-fit@0.10.0/lib/addon-fit.min.js"></script>
<script>
  if (typeof Terminal === 'undefined' || typeof FitAddon === 'undefined') {
    document.getElementById('loaderr').textContent =
      'Failed to load terminal library from CDN. Check your internet connection and try reloading.';
    throw new Error('xterm.js failed to load');
  }

  const codeInput = document.getElementById('code');
  codeInput.addEventListener('input', () => {
    codeInput.value = codeInput.value.replace(/[^0-9]/g, '').slice(0, 2);
  });

  // Populate the shell picker when the owner allows choosing one.
  fetch('/shells').then(r => r.json()).then(data => {
    if (!data.choice_allowed) return;
    const sel = document.getElementById('shellsel');
    for (const s of data.available) {
      const o = document.createElement('option');
      o.value = s.id; o.textContent = s.name;
      if (s.id === data.default) o.selected = true;
      sel.appendChild(o);
    }
    document.getElementById('shellrow').style.display = 'flex';
  }).catch(() => {});

  function fmtCountdown(sec) {
    sec = Math.max(0, Math.floor(sec));
    return String(Math.floor(sec / 60)).padStart(2, '0') + ':' + String(sec % 60).padStart(2, '0');
  }

  function showClaimed(obj) {
    const gate = document.getElementById('gate');
    document.getElementById('app').style.display = 'none';
    gate.style.display = 'none';
    document.getElementById('claim-ip').textContent = obj.ip || 'unknown';
    const d = new Date((obj.connected_at || 0) * 1000);
    document.getElementById('claim-at').textContent = isNaN(d.getTime()) ? 'n/a' : d.toLocaleString();
    const left = document.getElementById('claim-left');
    const panel = document.getElementById('claimed');
    panel.style.display = 'flex';
    if (obj.remaining < 0) { left.textContent = 'no limit'; return; }
    let rem = obj.remaining;
    left.textContent = fmtCountdown(rem);
    setInterval(() => {
      rem -= 1;
      if (rem <= 0) { left.textContent = 'Session ended'; return; }
      left.textContent = fmtCountdown(rem);
    }, 1000);
  }

  function connect(code) {
    const gate = document.getElementById('gate');
    const gateerr = document.getElementById('gateerr');
    const termDiv = document.getElementById('terminal');
    const toolbar = document.getElementById('toolbar');

    const proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
    const sel = document.getElementById('shellsel');
    const shell = sel.value ? '&shell=' + encodeURIComponent(sel.value) : '';
    const ws = new WebSocket(proto + '://' + window.location.host + '/ws?code=' + encodeURIComponent(code) + shell);
    let opened = false;

    ws.onopen = () => {
      opened = true;
      gate.style.display = 'none';
      termDiv.style.display = 'block';

      const term = new Terminal({ cursorBlink: true, fontFamily: "Consolas, monospace", fontSize: 14 });
      const fitAddon = new FitAddon.FitAddon();
      term.loadAddon(fitAddon);
      term.open(termDiv);
      fitAddon.fit();
      ws.send(JSON.stringify({type:'resize', cols: term.cols, rows: term.rows}));

      ws.onmessage = (ev) => {
        const d = ev.data;
        if (typeof d === 'string' && d.charCodeAt(0) === 123) {
          try {
            const obj = JSON.parse(d);
            if (obj.type === 'claimed') { showClaimed(obj); return; }
          } catch (e) {}
        }
        term.write(d);
      };
      ws.onclose = () => term.write('\\r\\n[connection closed]\\r\\n');
      term.onData(data => ws.send(JSON.stringify({type:'input', data})));

      window.addEventListener('resize', () => {
        fitAddon.fit();
        ws.send(JSON.stringify({type:'resize', cols: term.cols, rows: term.rows}));
      });

      toolbar.querySelectorAll('button').forEach(btn => {
        btn.addEventListener('click', () => {
          ws.send(JSON.stringify({type:'input', data: btn.dataset.seq}));
          term.focus();
        });
      });
    };

    ws.onerror = () => { if (!opened) gateerr.textContent = 'Wrong code or connection error. Try again.'; };
    ws.onclose = () => { if (!opened) gateerr.textContent = 'Wrong code. Try again.'; };
  }

  document.getElementById('go').addEventListener('click', () => connect(codeInput.value.trim()));
  codeInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') connect(codeInput.value.trim());
  });
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def index():
    return INDEX_HTML


@app.get("/shells")
async def shells():
    avail = available_shells()
    default = DEFAULT_SHELL_ID if DEFAULT_SHELL_ID in avail else next(iter(avail), "powershell")
    return {
        "available": [{"id": sid, "name": spec["name"]} for sid, spec in avail.items()],
        "default": default,
        "choice_allowed": SHELL_CHOICE,
    }


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket, code: str = ""):
    client_ip = _real_ip(websocket)
    ok = secrets.compare_digest(code or "", CODE)

    if not ok:
        await throttle_guard(client_ip)
        log_line(client_ip, "rejected")
        await websocket.close(code=4401)
        return

    now = time.monotonic()
    with _state_lock:
        claimed_ip = _session["ip"]
        claimed_active = _session["active"]

    if claimed_ip is not None and (claimed_active or client_ip != claimed_ip):
        # Session already owned by someone else: show who owns it + time left.
        with _state_lock:
            owner = _session["ip"]
            owner_at = _session["at_wall"]
            until = _session["until"]
        remaining = -1 if until is None else max(0, int(until - now))
        log_line(client_ip, "watcher")
        await websocket.accept()
        await websocket.send_text(json.dumps({
            "type": "claimed",
            "ip": owner,
            "connected_at": owner_at,
            "remaining": remaining,
        }))
        await websocket.close(code=4401)
        return

    with _state_lock:
        was_new = _session["ip"] is None
        if was_new:
            _session["at_wall"] = time.time()
            _session["until"] = now + SESSION_SECONDS if SESSION_SECONDS > 0 else None
        _session["ip"] = client_ip
        _session["active"] = True
        _active_ws.add(websocket)

    avail = available_shells()
    shell_id = DEFAULT_SHELL_ID if DEFAULT_SHELL_ID in avail else next(iter(avail), "powershell")
    if SHELL_CHOICE:
        requested = (websocket.query_params.get("shell") or "").strip().lower()
        if requested in avail:
            shell_id = requested
    log_line(client_ip, "accepted" if was_new else "reconnected", shell_id)

    await websocket.accept()
    spec = SHELLS[shell_id]
    env = dict(os.environ)
    env.update(spec.get("env", {}))
    proc = PtyProcess.spawn(spec["argv"](), dimensions=(30, 120), env=env)
    with _state_lock:
        _procs.add(proc)

    loop = asyncio.get_event_loop()
    stop = threading.Event()

    def reader():
        while not stop.is_set() and proc.isalive():
            try:
                data = proc.read(4096)
            except EOFError:
                break
            if data:
                fut = asyncio.run_coroutine_threadsafe(websocket.send_text(data), loop)
                try:
                    fut.result()
                except Exception:
                    break
        stop.set()

    t = threading.Thread(target=reader, daemon=True)
    t.start()

    try:
        while True:
            msg = await websocket.receive_text()
            try:
                obj = json.loads(msg)
            except json.JSONDecodeError:
                continue
            if obj.get("type") == "input":
                proc.write(obj.get("data", ""))
            elif obj.get("type") == "resize":
                cols = int(obj.get("cols", 80))
                rows = int(obj.get("rows", 24))
                try:
                    proc.setwinsize(rows, cols)
                except Exception:
                    pass
    except WebSocketDisconnect:
        pass
    finally:
        stop.set()
        try:
            proc.terminate(force=True)
        except Exception:
            pass
        with _state_lock:
            _procs.discard(proc)
            _active_ws.discard(websocket)
            _session["active"] = False


if __name__ == "__main__":
    print(f"Terminal server listening on 127.0.0.1:{PORT}")
    config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning")
    server = uvicorn.Server(config)
    SERVER = server
    server.run()
