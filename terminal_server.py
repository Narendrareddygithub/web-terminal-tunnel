"""
terminal_server.py
A minimal web-based terminal for Windows: spawns a real console via ConPTY
(pywinpty) and streams it to a browser over a WebSocket using xterm.js.

Access is gated by a short numeric code, entered via an in-page prompt.
Because a short code has a small keyspace, this is treated as a
confirmation step (similar to GitHub/Microsoft "number matching" device
approval) rather than the sole line of defense -- the real secret is the
hard-to-guess Cloudflare quick-tunnel URL itself. Wrong guesses are slowed
with a growing delay, and a client that fails repeatedly is locked out
entirely for a cooldown period.

Configured via environment variables (set by the launcher script):
  TERMINAL_PORT   - port to bind on 127.0.0.1 (default 8765)
  TERMINAL_CODE   - access code (required)
  TERMINAL_SHELL  - shell to spawn (default "powershell.exe")
"""

import asyncio
import json
import os
import secrets
import sys
import threading
import time

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
import uvicorn

try:
    from winpty import PtyProcess
except ImportError:
    sys.exit("pywinpty is not installed. Run: pip install pywinpty")

PORT = int(os.environ.get("TERMINAL_PORT", "8765"))
CODE = os.environ.get("TERMINAL_CODE")
SHELL = os.environ.get("TERMINAL_SHELL", "powershell.exe")

if not CODE:
    sys.exit("TERMINAL_CODE environment variable is required.")

app = FastAPI()

# --- brute-force protection -------------------------------------------------
# Short codes have a small keyspace, so on top of a growing per-attempt
# delay, a client gets fully locked out for LOCK_SECONDS after
# MAX_ATTEMPTS wrong guesses in a row. Resets on a correct guess.
MAX_ATTEMPTS = 5
LOCK_SECONDS = 300  # 5 minutes

_state = {}  # client_ip -> {"count": int, "locked_until": float}
_state_lock = threading.Lock()


async def throttle_and_check(client_ip: str, code_attempt: str) -> bool:
    now = time.monotonic()

    with _state_lock:
        entry = _state.get(client_ip, {"count": 0, "locked_until": 0.0})
        if entry["locked_until"] > now:
            locked = True
        else:
            locked = False

    if locked:
        await asyncio.sleep(2)  # don't reveal lock state instantly
        return False

    ok = secrets.compare_digest(code_attempt or "", CODE)

    with _state_lock:
        entry = _state.get(client_ip, {"count": 0, "locked_until": 0.0})
        if ok:
            _state.pop(client_ip, None)
            return True
        entry["count"] += 1
        if entry["count"] >= MAX_ATTEMPTS:
            entry["locked_until"] = now + LOCK_SECONDS
            entry["count"] = 0
        _state[client_ip] = entry
        delay = min(entry["count"] * 2, 10)

    await asyncio.sleep(delay)
    return False


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
  #gateerr { color:#f55; font-size:13px; min-height:16px; }

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

  function connect(code) {
    const gate = document.getElementById('gate');
    const gateerr = document.getElementById('gateerr');
    const termDiv = document.getElementById('terminal');
    const toolbar = document.getElementById('toolbar');

    const proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
    const ws = new WebSocket(proto + '://' + window.location.host + '/ws?code=' + encodeURIComponent(code));
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

      ws.onmessage = (ev) => term.write(ev.data);
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


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket, code: str = ""):
    client_ip = websocket.client.host if websocket.client else "unknown"
    ok = await throttle_and_check(client_ip, code)
    if not ok:
        await websocket.close(code=4401)
        return

    await websocket.accept()
    proc = PtyProcess.spawn([SHELL], dimensions=(30, 120))

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


if __name__ == "__main__":
    print(f"Terminal server listening on 127.0.0.1:{PORT}")
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
