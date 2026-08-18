"""
terminal_server.py
A session-hub web terminal for Windows: spawns one or more real consoles via
ConPTY (pywinpty) and streams them to a browser over WebSocket using xterm.js.

Access is gated by a short numeric code. The first client to enter the correct
code claims ownership of the whole hub for that client IP; every later client is
refused a terminal and only shown who owns the session and how much time is
left. The owner sees a dashboard listing the active sessions and can create,
close and attach to them. Sessions persist across client disconnects (like
tmux): the ConPTY keeps running until the session is closed, the server shuts
down, or the time limit elapses.

Because a short code has a small keyspace, it is treated as a confirmation
step (similar to GitHub/Microsoft "number matching" device approval) rather
than the sole line of defense -- the real secret is the hard-to-guess
Cloudflare quick-tunnel URL itself. Wrong guesses are slowed with a growing
delay, and a client that fails repeatedly is locked out entirely for a cooldown
period.

Configured via environment variables (set by the launcher script):
  TERMINAL_PORT           - port to bind on 127.0.0.1 (default 8765)
  TERMINAL_CODE           - access code (required)
  TERMINAL_SHELL          - default shell id: powershell|pwsh|cmd|bash|wsl
                            (default "powershell"; "powershell.exe" also accepted)
  TERMINAL_SHELL_CHOICE   - "1" lets the client pick a shell when creating
                            sessions in the browser (default off)
  TERMINAL_WSL_DISTRO     - optional WSL distro name for the wsl shell (default distro)
  TERMINAL_SESSION_MINUTES- hard session limit in minutes, 0 = no limit
  TERMINAL_SESSIONS       - number of sessions pre-created at startup (default 1)
  TERMINAL_LOG            - connection log path (default ~/.web-terminal/connections.log)
  TERMINAL_AGENTS         - "1" enables AI-agent harness detection (default on; "0" off)
  TERMINAL_AGENT_CWD      - default working dir for agent sessions (default ~)
  TERMINAL_AGENT_CONFIG   - optional path to custom agents JSON (default
                            ~/.web-terminal/agents.json)
"""

import asyncio
import itertools
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

import agents

try:
    from winpty import PtyProcess
except ImportError:
    sys.exit("pywinpty is not installed. Run: pip install pywinpty")

PORT = int(os.environ.get("TERMINAL_PORT", "8765"))
CODE = os.environ.get("TERMINAL_CODE")
SESSION_SECONDS = int(os.environ.get("TERMINAL_SESSION_MINUTES", "0")) * 60
PRE_CREATED_SESSIONS = max(1, int(os.environ.get("TERMINAL_SESSIONS", "1")))
LOG_PATH = os.environ.get(
    "TERMINAL_LOG",
    os.path.join(os.path.expanduser("~"), ".web-terminal", "connections.log"),
)

# --- shell registry --------------------------------------------------------
# Whitelisted shells only: the client can create a session from this list, never
# an arbitrary command. `argv` is a callable returning the argv to spawn; `env`
# is extra environment for that shell; `detect` checks presence.


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

# --- hub state -------------------------------------------------------------
# One owner per server lifetime: the first correct code claim binds the hub to a
# client IP. The owner sees the dashboard and can create/close/attach sessions.
# `_sessions` maps session id -> dict. A session's ConPTY is spawned lazily on
# first attach and kept alive across disconnects.
_owner = {"ip": None, "at_wall": 0.0, "until": None}
_sessions = {}  # sid -> session dict
_session_counter = itertools.count(1)
_active_ws = set()  # accepted websockets (dashboard + terminal), for shutdown
_state_lock = threading.Lock()


def _make_session(shell_id, cwd=None):
    n = next(_session_counter)
    return {
        "id": f"s{n}",
        "name": f"Session {n}",
        "shell_id": shell_id,
        "cwd": cwd,
        "created_wall": time.time(),
        "proc": None,        # PtyProcess, spawned on first attach
        "ws": None,          # currently attached websocket (one per session)
        "reader_stop": None, # threading.Event for the reader thread
        "loop": None,        # asyncio loop the reader thread sends on
    }


_installed = available_shells()
_DEFAULT_INSTALLED = (
    DEFAULT_SHELL_ID
    if DEFAULT_SHELL_ID in _installed
    else next(iter(_installed), "powershell")
)
for _ in range(PRE_CREATED_SESSIONS):
    sess = _make_session(_DEFAULT_INSTALLED)
    _sessions[sess["id"]] = sess

# --- brute-force protection -------------------------------------------------
# Short codes have a small keyspace, so on top of a growing per-attempt
# delay, a client gets fully locked out for LOCK_SECONDS after
# MAX_ATTEMPTS wrong guesses in a row. Only wrong guesses count; correct
# codes bypass this entirely.
MAX_ATTEMPTS = 5
LOCK_SECONDS = 300  # 5 minutes

_throttle_state = {}  # client_ip -> {"count": int, "locked_until": float}


async def throttle_guard(client_ip: str) -> bool:
    """Brute-force defense for wrong codes. Applies a growing per-attempt delay
    and locks the client out after MAX_ATTEMPTS. Returns False if the client is
    locked out (a fixed 2s sleep is consumed so lock state isn't revealed)."""
    now = time.monotonic()

    with _state_lock:
        entry = _throttle_state.get(client_ip, {"count": 0, "locked_until": 0.0})
        locked = entry["locked_until"] > now

    if locked:
        await asyncio.sleep(2)
        return False

    with _state_lock:
        entry = _throttle_state.get(client_ip, {"count": 0, "locked_until": 0.0})
        entry["count"] += 1
        if entry["count"] >= MAX_ATTEMPTS:
            entry["locked_until"] = now + LOCK_SECONDS
            entry["count"] = 0
        _throttle_state[client_ip] = entry
        delay = min(entry["count"] * 2, 10)

    await asyncio.sleep(delay)
    return True


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


def _session_status(sess) -> str:
    proc = sess["proc"]
    if sess["ws"] is not None:
        return "attached"
    if proc is not None and proc.isalive():
        return "running"
    return "idle"


def _session_info(sess) -> dict:
    return {
        "id": sess["id"],
        "name": sess["name"],
        "shell": sess["shell_id"],
        "status": _session_status(sess),
        "created": sess["created_wall"],
    }


def _dashboard_state() -> dict:
    now = time.monotonic()
    with _state_lock:
        owner_ip = _owner["ip"]
        until = _owner["until"]
        remaining = -1 if until is None else max(0, int(until - now))
        sessions = [_session_info(s) for s in _sessions.values()]
    return {
        "type": "dashboard",
        "owner": owner_ip,
        "remaining": remaining,
        "sessions": sessions,
    }


def _terminate_session(sess) -> None:
    """Kill a session's ConPTY + reader thread + attached websocket. Sync-safe."""
    stop = sess.get("reader_stop")
    if stop is not None:
        stop.set()
    proc = sess.get("proc")
    if proc is not None:
        try:
            proc.terminate(force=True)
        except Exception:
            pass
    ws = sess.get("ws")
    loop = sess.get("loop")
    if ws is not None and loop is not None:
        try:
            fut = asyncio.run_coroutine_threadsafe(ws.close(), loop)
            try:
                fut.result(timeout=2)
            except Exception:
                pass
        except Exception:
            pass


async def _monitor() -> None:
    """Background task: end the session when its fixed duration elapses."""
    while True:
        await asyncio.sleep(1)
        with _state_lock:
            until = _owner["until"]
            sessions_snapshot = list(_sessions.values())
        if until is None or time.monotonic() < until:
            continue
        print("Session duration reached; shutting down.", flush=True)
        for sess in sessions_snapshot:
            _terminate_session(sess)
        for w in list(_active_ws):
            try:
                await w.close()
            except Exception:
                pass
        if SERVER is not None:
            SERVER.should_exit = True
        return


@asynccontextmanager
async def lifespan(_app):
    # Arm the hard deadline at server start so -SessionMinutes shuts the whole
    # thing down even if no client ever claims the hub.
    with _state_lock:
        _owner["until"] = time.monotonic() + SESSION_SECONDS if SESSION_SECONDS > 0 else None
    task = asyncio.create_task(_monitor())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(lifespan=lifespan)

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
  #app { height:100vh; display:none; flex-direction:column; }
  #terminal { flex:1; min-height:0; padding:4px; }
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
  #gateerr { color:#f55; font-size:13px; min-height:16px; }

  /* Session-already-claimed screen */
  #claimed {
    height:100vh; display:none; flex-direction:column; align-items:center; justify-content:center;
    font-family:Consolas, monospace; color:#ddd; gap:12px; padding:24px; text-align:center;
  }
  #claimed .claim-title { font-size:18px; color:#ff9d00; }
  #claimed .claim-info { font-size:14px; opacity:0.9; line-height:1.7; }
  #claimed .claim-count { font-size:30px; font-weight:bold; color:#eee; }

  /* Dashboard */
  #dash { display:none; height:100vh; flex-direction:column; }
  #dash .dash-head {
    display:flex; align-items:center; gap:10px; flex-wrap:wrap;
    padding:10px 14px; font-family:Consolas, monospace; color:#ddd;
    border-bottom:1px solid #333;
  }
  #dash .dash-head .title { font-size:16px; color:#eee; }
  #dash .dash-head .countdown { font-size:13px; opacity:0.8; margin-left:auto; }
  #shelltabs, #subtabs {
    display:flex; flex-wrap:wrap; align-items:center;
    padding:8px 14px 0; font-family:Consolas, monospace;
  }
  /* shell row: VS Code boxed editor tabs */
  #shelltabs { gap:2px; background:#2d2d30; padding:4px 14px 0; }
  .stab {
    font-family:Consolas, monospace; font-size:13px;
    padding:6px 14px; background:#252526; color:#bbb;
    border:1px solid #2a2a2a; border-bottom:none;
    border-radius:4px 4px 0 0; cursor:pointer; position:relative;
  }
  .stab:hover { color:#eee; background:#2d2d30; }
  .stab.on { background:#1e1e1e; color:#eee; }
  .stab.on::before {
    content:''; position:absolute; top:-1px; left:-1px; right:-1px; height:2px;
    background:#2d6cdf; border-radius:4px 4px 0 0;
  }
  .stab .stab-count { opacity:0.75; margin-left:5px; font-size:11px; }
  /* status row: segmented control */
  #subtabs {
    gap:0; margin:8px 14px; padding:0; align-self:flex-start;
    border:1px solid #333; border-radius:6px; overflow:hidden;
  }
  .stab.sub {
    font-size:12px; padding:5px 12px; border:none; border-radius:0;
    background:#252526; color:#bbb;
  }
  .stab.sub + .stab.sub { border-left:1px solid #333; }
  .stab.sub:hover { background:#2d2d30; }
  .stab.sub.on { background:#2d6cdf; color:#fff; }
  /* agent tab row (distinct purple accent) */
  #agenttabs {
    display:none; flex-wrap:wrap; align-items:center; gap:2px;
    background:#2d2d30; padding:4px 14px 0; font-family:Consolas, monospace;
  }
  #agenttabs.has { display:flex; }
  #agenttabs .stab.on { color:#e9d5ff; }
  #agenttabs .stab.on::before { background:#8b5cf6; }
  .tag { font-size:10px; padding:1px 6px; border-radius:999px; border:1px solid #555; color:#aaa; margin-left:6px; }
  .tag.eol { color:#ff9d00; border-color:#7a4f00; }
  .tag.custom { color:#c4b5fd; border-color:#5b3a9e; }
  /* landing-page detected-harnesses card */
  #detect {
    display:none; max-width:480px; width:100%; background:#252526;
    border:1px solid #333; border-radius:10px; padding:14px 16px;
    font-family:Consolas, monospace;
  }
  #detect.on { display:block; }
  #detect h3 { margin:0 0 8px; font-size:13px; color:#9cdcfe; font-weight:normal; }
  #detect ul { list-style:none; margin:0; padding:0; }
  #detect li { font-size:13px; color:#ddd; padding:3px 0; display:flex; align-items:center; flex-wrap:wrap; gap:6px; }
  #detect .dver { color:#888; font-size:12px; }
  #detect .dnl { color:#888; font-size:11px; margin-left:auto; }
  /* agent working-directory input in the create row */
  #newcwd {
    font-family:Consolas, monospace; font-size:13px; padding:8px 10px;
    background:#111; color:#eee; border:1px solid #444; border-radius:6px;
    display:none; min-width:220px;
  }
  #newcwd.on { display:inline-block; }
  #dashmsg { font-size:12px; color:#ff9d00; }
  .srow .sagent {
    font-size:10px; padding:1px 6px; border-radius:999px;
    background:#2a1d4d; color:#c4b5fd; border:1px solid #5b3a9e;
  }
  #rescanbtn {
    font-family:Consolas, monospace; font-size:12px; padding:4px 10px;
    background:#3a3a3a; color:#ddd; border:1px solid #4a4a4a; border-radius:6px; cursor:pointer;
  }
  #rescanbtn:hover { background:#4a4a4a; }
  .secbanner { font-size:12px; color:#ff9d00; padding:6px 14px; border-bottom:1px solid #333; font-family:Consolas, monospace; }
  #sesslist { flex:1; overflow-y:auto; padding:10px 14px; font-family:Consolas, monospace; }
  .srow {
    display:flex; align-items:center; gap:10px; flex-wrap:wrap;
    padding:9px 12px; margin-bottom:6px; background:#252526;
    border:1px solid #333; border-radius:8px; cursor:pointer;
  }
  .srow:hover { border-color:#555; background:#2d2d30; }
  .srow .sdot { width:10px; height:10px; border-radius:50%; flex:0 0 auto; }
  .sdot.idle { background:#888; }
  .sdot.running { background:#4ec9b0; }
  .sdot.attached { background:#2d6cdf; }
  .srow .sname { font-size:14px; color:#eee; }
  .srow .sstatus {
    font-size:12px; padding:1px 8px; border-radius:999px;
    text-transform:capitalize; color:#bbb; background:#333;
  }
  .srow .sstatus.running, .srow .sstatus.attached { color:#4ec9b0; background:#1f3b33; }
  .srow .screated { font-size:11px; color:#888; margin-left:auto; }
  .srow .sclose {
    background:none; border:none; color:#888; cursor:pointer;
    font-size:16px; line-height:1; padding:2px 6px; border-radius:50%;
  }
  .srow .sclose:hover { color:#f88; background:#3a3a3a; }
  #empty { color:#666; font-size:13px; padding:14px; }
  #active-count { font-size:12px; color:#4ec9b0; background:#1f3b33; padding:3px 10px; border-radius:999px; }
  #dash .dash-new {
    display:flex; gap:8px; align-items:center; flex-wrap:wrap;
    padding:10px 14px; border-top:1px solid #333;
    font-family:Consolas, monospace; color:#ddd;
  }
  #dash .dash-new select {
    font-family:Consolas, monospace; font-size:14px; padding:8px 10px;
    background:#111; color:#eee; border:1px solid #444; border-radius:6px;
  }
  #dash .dash-new button {
    font-family:Consolas, monospace; font-size:14px; padding:8px 18px; cursor:pointer;
    background:#2d6cdf; color:#fff; border:none; border-radius:6px;
  }
  #dash .dash-new .hint { font-size:12px; opacity:0.6; }

  /* Terminal back bar */
  #backbar {
    display:none; align-items:center; gap:10px; padding:6px 10px;
    background:#252526; border-bottom:1px solid #3a3a3a;
    font-family:Consolas, monospace;
  }
  #backbar button {
    font-family:Consolas, monospace; font-size:13px; padding:6px 14px;
    background:#3a3a3a; color:#eee; border:1px solid #4a4a4a; border-radius:6px; cursor:pointer;
  }
  #backbar .sess-tag { color:#9cdcfe; font-size:13px; }

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
  <button id="go">Connect</button>
  <div id="gateerr"></div>
  <div id="detect">
    <h3>AI harnesses detected on this machine</h3>
    <ul id="detectlist"></ul>
  </div>
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

<div id="dash">
  <div class="dash-head">
    <span class="title">Sessions</span>
    <span id="active-count"></span>
    <button id="rescanbtn" title="Re-detect installed AI harnesses">&#8635; Rescan agents</button>
    <span class="countdown" id="dash-left"></span>
  </div>
  <div class="secbanner" id="secbanner">Agent sessions run with your local machine credentials and API keys.</div>
  <div id="shelltabs"></div>
  <div id="agenttabs"></div>
  <div id="subtabs"></div>
  <div id="sesslist"></div>
  <div class="dash-new">
    <span>New session:</span>
    <select id="newshell"></select>
    <input id="newcwd" placeholder="working directory (agents)" spellcheck="false"/>
    <button id="addbtn">Add</button>
    <span class="hint" id="addhint"></span>
    <span id="dashmsg"></span>
  </div>
</div>

<div id="app">
  <div id="backbar">
    <button id="backbtn">&larr; Sessions</button>
    <span class="sess-tag" id="term-tag"></span>
  </div>
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

  let code = '';
  let dashWs = null;
  let termWs = null;
  let term = null;
  let fitAddon = null;
  let countTimer = null;
  let shellMap = {};    // shell id -> display name
  let dashState = null; // last dashboard payload
  let activeShell = 'all';
  let activeStatus = 'all';
  let agentMap = {};    // agent id -> {name, version, tags, launchable, notes}
  let agentIds = [];    // ordered agent ids (launchable first)
  let agentDefaultCwd = '~';
  let activeAgent = 'all';

  const codeInput = document.getElementById('code');
  codeInput.addEventListener('input', () => {
    codeInput.value = codeInput.value.replace(/[^0-9]/g, '').slice(0, 2);
  });

  // Populate the New-session dropdown (shells + agents), the shell tab map,
  // the landing-page detected card and the agent tab row.
  function loadShells() {
    fetch('/shells').then(r => r.json()).then(data => {
      shellMap = {};
      agentMap = {};
      agentIds = [];
      agentDefaultCwd = data.agent_default_cwd || '~';
      const sel = document.getElementById('newshell');
      sel.innerHTML = '';
      let og = document.createElement('optgroup');
      og.label = 'Shells';
      for (const s of data.available) {
        shellMap[s.id] = s.name;
        const o = document.createElement('option');
        o.value = s.id; o.textContent = s.name;
        if (s.id === data.default) o.selected = true;
        og.appendChild(o);
      }
      sel.appendChild(og);
      const ag = data.agents || [];
      if (ag.length) {
        og = document.createElement('optgroup');
        og.label = 'AI Agents';
        for (const a of ag) {
          agentMap[a.id] = a;
          agentIds.push(a.id);
          const o = document.createElement('option');
          o.value = a.id;
          o.textContent = a.name + (a.version ? ' \u00b7 v' + a.version : '');
          og.appendChild(o);
        }
        sel.appendChild(og);
      }
      if (!data.choice_allowed) {
        document.getElementById('addhint').textContent = '(shell fixed by owner)';
      }
      renderDetectCard(ag);
      renderAgentTabs();
      if (dashState) renderSessions(dashState);
    }).catch(() => {});
  }
  loadShells();
  // Refresh once more a few seconds later: version probes run in a background
  // thread server-side, so late results just landed.
  setTimeout(loadShells, 4000);

  function decodeSeq(s) {
    return s.replace(/\\u([0-9a-fA-F]{4})/g, (_, h) => String.fromCharCode(parseInt(h, 16)));
  }

  function fmtCountdown(sec) {
    sec = Math.max(0, Math.floor(sec));
    return String(Math.floor(sec / 60)).padStart(2, '0') + ':' + String(sec % 60).padStart(2, '0');
  }

  function showClaimed(obj) {
    document.getElementById('dash').style.display = 'none';
    document.getElementById('app').style.display = 'none';
    document.getElementById('gate').style.display = 'none';
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

  function renderDetectCard(agents) {
    const card = document.getElementById('detect');
    const ul = document.getElementById('detectlist');
    ul.innerHTML = '';
    const ordered = agents.slice().sort((a, b) => (a.launchable === b.launchable ? 0 : a.launchable ? -1 : 1));
    for (const a of ordered) {
      const li = document.createElement('li');
      li.textContent = a.name;
      const ver = document.createElement('span');
      ver.className = 'dver';
      ver.textContent = a.version ? 'v' + a.version : '';
      li.appendChild(ver);
      for (const t of (a.tags || [])) {
        const tag = document.createElement('span');
        tag.className = 'tag ' + t.toLowerCase();
        tag.textContent = t;
        li.appendChild(tag);
      }
      if (!a.launchable) {
        const nl = document.createElement('span');
        nl.className = 'dnl'; nl.textContent = 'not launchable';
        li.appendChild(nl);
      }
      ul.appendChild(li);
    }
    card.classList.toggle('on', agents.length > 0);
    document.getElementById('rescanbtn').style.display = agents.length ? '' : 'none';
    document.getElementById('secbanner').style.display = agents.length ? '' : 'none';
  }

  function renderAgentTabs() {
    const bar = document.getElementById('agenttabs');
    bar.innerHTML = '';
    bar.className = '';
    if (agentIds.length === 0) return;
    bar.classList.add('has');
    for (const id of agentIds) {
      const b = document.createElement('button');
      b.className = 'stab' + (activeAgent === id ? ' on' : '');
      const count = dashState ? dashState.sessions.filter(s => s.shell === id).length : 0;
      b.innerHTML = agentMap[id].name + '<span class="stab-count">(' + count + ')</span>';
      b.addEventListener('click', () => {
        activeAgent = (activeAgent === id ? 'all' : id);
        syncShellSelect();
        renderSessions(dashState);
      });
      bar.appendChild(b);
    }
  }

  function renderSessions(st) {
    dashState = st;
    renderShellTabs();
    renderAgentTabs();
    renderSubTabs();
    renderRows();

    document.getElementById('active-count').textContent =
      st.sessions.length + ' session' + (st.sessions.length === 1 ? '' : 's');

    const left = document.getElementById('dash-left');
    if (st.remaining < 0) {
      left.textContent = 'no time limit';
    } else {
      let rem = st.remaining;
      const tick = () => { rem -= 1; left.textContent = rem <= 0 ? 'Session ended' : fmtCountdown(rem); };
      left.textContent = fmtCountdown(rem);
      clearInterval(countTimer);
      countTimer = setInterval(tick, 1000);
    }
  }

  function shellTabIds() {
    const ids = Object.keys(shellMap);
    if (ids.length > 0) return ids;
    // /shells not loaded yet: derive tabs from sessions present.
    const seen = {};
    (dashState ? dashState.sessions : []).forEach(s => { seen[s.shell] = true; });
    return Object.keys(seen);
  }

  function renderShellTabs() {
    const bar = document.getElementById('shelltabs');
    bar.innerHTML = '';
    const mk = (id, label) => {
      const b = document.createElement('button');
      b.className = 'stab' + (activeShell === id ? ' on' : '');
      const count = dashState ? dashState.sessions.filter(s => id === 'all' || s.shell === id).length : 0;
      b.innerHTML = label + '<span class="stab-count">(' + count + ')</span>';
      b.addEventListener('click', () => {
        activeShell = id;
        syncShellSelect();
        renderSessions(dashState);
      });
      return b;
    };
    bar.appendChild(mk('all', 'All'));
    for (const id of shellTabIds()) bar.appendChild(mk(id, shellMap[id] || id));
  }

  function renderSubTabs() {
    const bar = document.getElementById('subtabs');
    bar.innerHTML = '';
    const base = dashState ? dashState.sessions.filter(s => activeShell === 'all' || s.shell === activeShell) : [];
    const cnt = (kind) => base.filter(s =>
      kind === 'all' ? true : kind === 'active' ? s.status !== 'idle' : s.status === 'idle').length;
    const mk = (id, label) => {
      const b = document.createElement('button');
      b.className = 'stab sub' + (activeStatus === id ? ' on' : '');
      b.innerHTML = label + '<span class="stab-count">(' + cnt(id) + ')</span>';
      b.addEventListener('click', () => { activeStatus = id; renderSessions(dashState); });
      return b;
    };
    bar.appendChild(mk('all', 'All'));
    bar.appendChild(mk('active', 'Active'));
    bar.appendChild(mk('idle', 'Idle'));
  }

  function visibleSessions() {
    let list = dashState ? dashState.sessions : [];
    if (activeShell !== 'all') list = list.filter(s => s.shell === activeShell);
    if (activeAgent !== 'all') list = list.filter(s => s.shell === activeAgent);
    if (activeStatus === 'active') list = list.filter(s => s.status !== 'idle');
    else if (activeStatus === 'idle') list = list.filter(s => s.status === 'idle');
    return list;
  }

  function renderRows() {
    const list = document.getElementById('sesslist');
    list.innerHTML = '';
    const rows = visibleSessions();
    if (rows.length === 0) {
      const e = document.createElement('div');
      e.id = 'empty'; e.textContent = 'No sessions here.';
      list.appendChild(e);
      return;
    }
    for (const s of rows) {
      const row = document.createElement('div');
      row.className = 'srow';
      row.title = 'Connect to ' + s.name;

      const dot = document.createElement('span');
      dot.className = 'sdot ' + s.status;
      const name = document.createElement('span');
      name.className = 'sname'; name.textContent = s.name;
      row.appendChild(dot); row.appendChild(name);
      if (agentMap[s.shell]) {
        const badge = document.createElement('span');
        badge.className = 'sagent'; badge.textContent = 'AGENT';
        row.appendChild(badge);
      }
      const stt = document.createElement('span');
      stt.className = 'sstatus ' + s.status; stt.textContent = s.status;
      const cr = document.createElement('span');
      cr.className = 'screated';
      cr.textContent = new Date(s.created * 1000).toLocaleTimeString();

      const close = document.createElement('button');
      close.className = 'sclose'; close.textContent = '×';
      close.addEventListener('click', (e) => {
        e.stopPropagation();
        if (dashWs && dashWs.readyState === 1) {
          dashWs.send(JSON.stringify({type:'close', id:s.id}));
        }
      });

      row.addEventListener('click', () => openTerminal(s.id, s.name));
      row.appendChild(stt);
      row.appendChild(cr); row.appendChild(close);
      list.appendChild(row);
    }
  }

  function syncShellSelect() {
    const sel = document.getElementById('newshell');
    if (activeAgent !== 'all' && sel.querySelector('option[value="' + activeAgent + '"]')) {
      sel.value = activeAgent;
      showCwdField();
      return;
    }
    if (activeShell !== 'all' && sel.querySelector('option[value="' + activeShell + '"]')) {
      sel.value = activeShell;
      showCwdField();
    }
  }

  function showCwdField() {
    const sel = document.getElementById('newshell');
    const cwd = document.getElementById('newcwd');
    const isAgent = !!agentMap[sel.value];
    cwd.classList.toggle('on', isAgent);
    if (isAgent && !cwd.value) cwd.value = agentDefaultCwd;
  }
  document.getElementById('newshell').addEventListener('change', showCwdField);

  function connect() {
    code = codeInput.value.trim();
    const gateerr = document.getElementById('gateerr');
    if (!code) { gateerr.textContent = 'Enter the code.'; return; }
    const proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
    let opened = false;
    dashWs = new WebSocket(proto + '://' + window.location.host + '/ws?code=' + encodeURIComponent(code));
    dashWs.onopen = () => {
      opened = true;
      document.getElementById('gate').style.display = 'none';
      document.getElementById('dash').style.display = 'flex';
    };
    dashWs.onmessage = (ev) => {
      const d = ev.data;
      if (typeof d !== 'string') return;
      try {
        const obj = JSON.parse(d);
        if (obj.type === 'claimed') { showClaimed(obj); return; }
        if (obj.type === 'dashboard') { renderSessions(obj); return; }
        if (obj.type === 'error') {
          const msg = document.getElementById('dashmsg');
          if (msg) msg.textContent = obj.message || 'error';
          return;
        }
      } catch (e) {}
    };
    dashWs.onclose = () => { if (!opened) gateerr.textContent = 'Wrong code. Try again.'; };
    dashWs.onerror = () => { if (!opened) gateerr.textContent = 'Connection error. Try again.'; };
  }

  function openTerminal(sid, name) {
    const proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
    const termDiv = document.getElementById('terminal');
    document.getElementById('dash').style.display = 'none';
    document.getElementById('app').style.display = 'flex';
    document.getElementById('term-tag').textContent = name + ' (' + sid + ')';
    document.getElementById('backbar').style.display = 'flex';
    termDiv.style.display = 'block';
    termDiv.innerHTML = '';

    term = new Terminal({ cursorBlink: true, fontFamily: "Consolas, monospace", fontSize: 14 });
    fitAddon = new FitAddon.FitAddon();
    term.loadAddon(fitAddon);
    term.open(termDiv);
    fitAddon.fit();

    let opened = false;
    termWs = new WebSocket(proto + '://' + window.location.host +
      '/ws?code=' + encodeURIComponent(code) + '&session=' + encodeURIComponent(sid));
    termWs.onopen = () => {
      opened = true;
      termWs.send(JSON.stringify({type:'resize', cols: term.cols, rows: term.rows}));
    };
    termWs.onmessage = (ev) => {
      const d = ev.data;
      if (typeof d === 'string' && d.charCodeAt(0) === 123) {
        try {
          const obj = JSON.parse(d);
          if (obj.type === 'busy' || obj.type === 'error') {
            term.write('\\r\\n[' + (obj.message || obj.type) + ']\\r\\n');
            return;
          }
        } catch (e) {}
      }
      term.write(d);
    };
    termWs.onclose = () => { if (!opened) term.write('\\r\\n[connection closed]\\r\\n'); };
    term.onData(data => { if (termWs) termWs.send(JSON.stringify({type:'input', data})); });

    document.getElementById('toolbar').querySelectorAll('button').forEach(btn => {
      btn.addEventListener('click', () => {
        if (termWs) termWs.send(JSON.stringify({type:'input', data: decodeSeq(btn.dataset.seq)}));
        term.focus();
      });
    });
    term.focus();
  }

  function backToDash() {
    if (termWs) { try { termWs.close(); } catch (e) {} termWs = null; }
    if (term) { term.dispose(); term = null; fitAddon = null; }
    document.getElementById('terminal').style.display = 'none';
    document.getElementById('backbar').style.display = 'none';
    document.getElementById('app').style.display = 'none';
    document.getElementById('dash').style.display = 'flex';
    if (dashWs && dashWs.readyState === 1) {
      dashWs.send(JSON.stringify({type:'list'}));
    }
  }

  window.addEventListener('resize', () => {
    if (term && fitAddon && termWs) {
      fitAddon.fit();
      termWs.send(JSON.stringify({type:'resize', cols: term.cols, rows: term.rows}));
    }
  });

  document.getElementById('backbtn').addEventListener('click', backToDash);
  document.getElementById('addbtn').addEventListener('click', () => {
    if (!dashWs || dashWs.readyState !== 1) return;
    const sel = document.getElementById('newshell');
    const shell = sel.value ? sel.value : '';
    const cwdIn = document.getElementById('newcwd');
    const cwd = cwdIn.classList.contains('on') ? cwdIn.value.trim() : '';
    const msg = document.getElementById('dashmsg');
    if (msg) msg.textContent = '';
    if (agentMap[shell] && !confirm(
        'Agent sessions run with your local machine credentials and API keys.\n' +
        'Start ' + agentMap[shell].name + (cwd ? ' in ' + cwd : '') + '?')) {
      return;
    }
    dashWs.send(JSON.stringify({type:'create', shell, cwd}));
  });
  document.getElementById('rescanbtn').addEventListener('click', () => {
    const btn = document.getElementById('rescanbtn');
    btn.disabled = true;
    fetch('/rescan', {method:'POST'}).then(r => r.json()).then(() => {
      btn.disabled = false;
      loadShells();
    }).catch(() => { btn.disabled = false; });
  });
  document.getElementById('go').addEventListener('click', connect);
  codeInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') connect(); });
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
        "agents": agents.available_agents() if agents.ENABLED else [],
        "agent_default_cwd": agents.DEFAULT_CWD if agents.ENABLED else "",
    }


@app.post("/rescan")
async def rescan_agents():
    agents.rescan()
    return {"agents": agents.available_agents() if agents.ENABLED else []}


def _claim_owner(client_ip: str) -> None:
    """Bind the hub to the first client that authenticates (idempotent)."""
    with _state_lock:
        if _owner["ip"] is None:
            _owner["ip"] = client_ip
            _owner["at_wall"] = time.time()


def _claimed_payload() -> dict:
    with _state_lock:
        owner_ip = _owner["ip"]
        owner_at = _owner["at_wall"]
        until = _owner["until"]
    remaining = -1 if until is None else max(0, int(until - time.monotonic()))
    return {"type": "claimed", "ip": owner_ip, "connected_at": owner_at, "remaining": remaining}


async def _send_claimed(websocket: WebSocket, client_ip: str) -> None:
    log_line(client_ip, "watcher")
    await websocket.accept()
    _active_ws.add(websocket)
    try:
        await websocket.send_text(json.dumps(_claimed_payload()))
        await websocket.close(code=4401)
    finally:
        _active_ws.discard(websocket)


def _reader(sess, loop) -> None:
    """Push a session's ConPTY output to whichever websocket is attached.
    Lives for the session's lifetime: keeps running across ws detach/attach."""
    proc = sess["proc"]
    stop = sess["reader_stop"]
    while not stop.is_set() and proc.isalive():
        try:
            data = proc.read(4096)
        except Exception:
            break
        if not data:
            continue
        with _state_lock:
            ws = sess["ws"]
        if ws is None:
            continue  # detached: output is dropped
        fut = asyncio.run_coroutine_threadsafe(ws.send_text(data), loop)
        try:
            fut.result()
        except Exception:
            pass
    stop.set()


async def _handle_dashboard(websocket: WebSocket, client_ip: str) -> None:
    log_line(client_ip, "accepted")
    await websocket.accept()
    _active_ws.add(websocket)
    try:
        await websocket.send_text(json.dumps(_dashboard_state()))
        while True:
            msg = await websocket.receive_text()
            try:
                obj = json.loads(msg)
            except json.JSONDecodeError:
                continue
            t = obj.get("type")
            if t == "create":
                shell = (obj.get("shell") or "").strip().lower()
                cwd = (obj.get("cwd") or "").strip()
                avail = available_shells()
                if shell not in avail:
                    if not (agents.ENABLED and agents.is_agent(shell) and agents.can_launch(shell)):
                        shell = _DEFAULT_INSTALLED
                        cwd = ""
                    elif cwd and not os.path.isdir(cwd):
                        await websocket.send_text(json.dumps(
                            {"type": "error", "message": "Working directory does not exist"}))
                        continue
                    elif not cwd:
                        cwd = agents.spec_cwd(shell)
                else:
                    cwd = ""
                with _state_lock:
                    sess = _make_session(shell, cwd)
                    _sessions[sess["id"]] = sess
                await websocket.send_text(json.dumps(_dashboard_state()))
            elif t == "close":
                sid = obj.get("id")
                with _state_lock:
                    sess = _sessions.pop(sid, None)
                if sess is not None:
                    _terminate_session(sess)
                await websocket.send_text(json.dumps(_dashboard_state()))
            elif t == "list":
                await websocket.send_text(json.dumps(_dashboard_state()))
    except WebSocketDisconnect:
        pass
    finally:
        _active_ws.discard(websocket)


async def _handle_attach(websocket: WebSocket, session_id: str, client_ip: str) -> None:
    with _state_lock:
        sess = _sessions.get(session_id)

    if sess is None:
        await websocket.accept()
        _active_ws.add(websocket)
        try:
            await websocket.send_text(json.dumps({"type": "error", "message": "no such session"}))
            await websocket.close(code=4404)
        finally:
            _active_ws.discard(websocket)
        return

    with _state_lock:
        attached = sess["ws"] is not None
    if attached:
        log_line(client_ip, "rejected")
        await websocket.accept()
        _active_ws.add(websocket)
        try:
            await websocket.send_text(json.dumps({"type": "busy", "message": "session in use"}))
            await websocket.close(code=4401)
        finally:
            _active_ws.discard(websocket)
        return

    await websocket.accept()
    _active_ws.add(websocket)
    try:
        with _state_lock:
            sess["ws"] = websocket
            proc = sess["proc"]
            reader = sess.get("reader")
            reattach = proc is not None and proc.isalive()
            reader_alive = reader is not None and reader.is_alive()

        if not reattach:
            sid = sess["shell_id"]
            cwd = sess.get("cwd")
            if sid in SHELLS:
                spec = SHELLS[sid]
                argv = spec["argv"]()
                env = dict(os.environ)
                env.update(spec.get("env", {}))
                proc_cwd = None
            else:
                bin_path = agents.find_bin(sid)
                argv = agents.launch_argv(sid, bin_path)
                env = dict(os.environ)
                env.update(agents.spec_env(sid))
                proc_cwd = cwd or agents.spec_cwd(sid) or None
            proc = PtyProcess.spawn(argv, cwd=proc_cwd, dimensions=(30, 120), env=env)
            loop = asyncio.get_event_loop()
            sess["proc"] = proc
            sess["loop"] = loop
            sess["reader_stop"] = threading.Event()
            t = threading.Thread(target=_reader, args=(sess, loop), daemon=True)
            sess["reader"] = t
            t.start()
        elif not reader_alive:
            # Proc survived a client detach but its reader thread died:
            # restart the reader so output flows to the new attach.
            loop = asyncio.get_event_loop()
            sess["loop"] = loop
            sess["reader_stop"] = threading.Event()
            t = threading.Thread(target=_reader, args=(sess, loop), daemon=True)
            sess["reader"] = t
            t.start()

        log_line(client_ip, "accepted" if not reattach else "reconnected", sess["shell_id"])

        while True:
            msg = await websocket.receive_text()
            try:
                obj = json.loads(msg)
            except json.JSONDecodeError:
                continue
            if obj.get("type") == "input":
                try:
                    proc.write(obj.get("data", ""))
                except Exception:
                    break
            elif obj.get("type") == "resize":
                try:
                    proc.setwinsize(int(obj.get("rows", 24)), int(obj.get("cols", 80)))
                except Exception:
                    pass
    except WebSocketDisconnect:
        pass
    finally:
        with _state_lock:
            if sess["ws"] is websocket:
                sess["ws"] = None
        _active_ws.discard(websocket)


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    client_ip = _real_ip(websocket)
    code = websocket.query_params.get("code", "")
    ok = secrets.compare_digest(code, CODE)

    if not ok:
        await throttle_guard(client_ip)
        log_line(client_ip, "rejected")
        await websocket.close(code=4401)
        return

    _claim_owner(client_ip)
    with _state_lock:
        owner_ip = _owner["ip"]
    if owner_ip != client_ip:
        await _send_claimed(websocket, client_ip)
        return

    session_id = websocket.query_params.get("session", "")
    if session_id:
        await _handle_attach(websocket, session_id, client_ip)
    else:
        await _handle_dashboard(websocket, client_ip)


if __name__ == "__main__":
    print(f"Terminal hub listening on 127.0.0.1:{PORT} ({PRE_CREATED_SESSIONS} session(s) pre-created)", flush=True)
    config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning")
    server = uvicorn.Server(config)
    SERVER = server
    server.run()
