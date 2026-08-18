"""
agents.py
AI agent harness detection + launch specs for web-terminal-tunnel (Windows).

Detects installed coding-agent CLIs (Claude Code, OpenCode, Codex, Aider,
Antigravity/agy, Gemini, Goose, OpenHands, Mini SWE-agent, Hugging Face CLI)
and provides the argv/env/cwd needed to launch them in a ConPTY session.
Additional entries (SWE-agent, ChatDev, Mentat, Plandex) are detected and
listed but flagged not-launchable: they lack an interactive TUI or need extra
infrastructure (Docker / WSL / a server).

Detection is PATH-based (shutil.which) plus probes of known install dirs
(npm global, pipx/uv ~/.local/bin, cargo, agy, opencode, scoop, chocolatey).
Version probing runs `--version` with a short timeout; failures are non-fatal
and just yield an empty version string. All probes run in a background thread
so the web UI never blocks on them.

Users can add custom harnesses ("even others") via JSON at
%USERPROFILE%\\.web-terminal\\agents.json (override with TERMINAL_AGENT_CONFIG):

  [{"id":"myagent","name":"My Agent","bin":"myagent","args":["--flag"],
    "env":{"K":"V"},"cwd":"C:\\\\work","notes":"..."}]

Custom ids must not collide with built-ins. Disable the whole feature with
TERMINAL_AGENTS=0.
"""

import json
import os
import re
import shutil
import subprocess
import threading

ENABLED = os.environ.get("TERMINAL_AGENTS", "1") == "1"
DEFAULT_CWD = os.environ.get("TERMINAL_AGENT_CWD") or os.path.expanduser("~")
CONFIG_PATH = os.environ.get("TERMINAL_AGENT_CONFIG") or os.path.join(
    os.path.expanduser("~"), ".web-terminal", "agents.json"
)

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_VERSION_RE = re.compile(r"\d+\.\d+(\.\d+)*")

# --- known install dirs (checked in addition to PATH) ----------------------
_HOME = os.path.expanduser("~")
_KNOWN_DIRS = []


def _add_dir(p):
    if p and os.path.isdir(p):
        _KNOWN_DIRS.append(p)


_add_dir(os.path.join(os.environ.get("APPDATA", ""), "npm"))
_add_dir(os.path.join(_HOME, ".local", "bin"))
_add_dir(os.path.join(_HOME, ".cargo", "bin"))
_add_dir(os.path.join(os.environ.get("LOCALAPPDATA", ""), "agy", "bin"))
_add_dir(os.path.join(_HOME, ".opencode", "bin"))
_add_dir(os.path.join(os.environ.get("PROGRAMDATA", ""), "chocolatey", "bin"))
_add_dir(os.path.join(_HOME, "scoop", "shims"))

# --- built-in registry ------------------------------------------------------
# bins: probe names (PATH lookup then known dirs). launchable=False entries are
# detected + listed but refused at session creation.
_SPECS = {
    "claude": {"name": "Claude Code", "bins": ["claude", "claude.exe"],
               "tags": [], "notes": "Anthropic coding agent", "launchable": True},
    "opencode": {"name": "OpenCode", "bins": ["opencode", "opencode.exe"],
                 "tags": [], "notes": "SST/anomalyco agent (TUI)", "launchable": True},
    "codex": {"name": "OpenAI Codex", "bins": ["codex", "codex.exe"],
              "tags": [], "notes": "OpenAI coding agent", "launchable": True},
    "aider": {"name": "Aider", "bins": ["aider", "aider.exe"],
              "tags": [], "notes": "Pair-programming agent; git-native", "launchable": True},
    "agy": {"name": "Antigravity (agy)", "bins": ["agy", "agy.exe"],
            "tags": [], "notes": "Google's agent; replaces Gemini CLI", "launchable": True},
    "gemini": {"name": "Gemini CLI", "bins": ["gemini", "gemini.exe"],
               "tags": ["EOL"], "notes": "Sunset 2026-06-18; successor: agy", "launchable": True},
    "goose": {"name": "Goose", "bins": ["goose", "goose.exe"],
              "tags": [], "notes": "Block's agent (Rust)", "launchable": True},
    "openhands": {"name": "OpenHands", "bins": ["openhands", "openhands.exe"],
                  "tags": [], "notes": "formerly OpenDevin; needs Python 3.12+", "launchable": True},
    "mini": {"name": "Mini SWE-agent", "bins": ["mini", "mini.exe"],
             "tags": [], "notes": "SWE-agent successor", "launchable": True},
    "hf": {"name": "Hugging Face CLI", "bins": ["hf", "hf.exe"],
           "tags": [], "notes": "agent-first CLI", "launchable": True},
    "sweagent": {"name": "SWE-agent", "bins": ["sweagent", "sweagent.exe"],
                 "tags": ["Docker"], "notes": "run-based; needs Docker sandbox", "launchable": False},
    "chatdev": {"name": "ChatDev", "bins": ["chatdev", "chatdev.exe"],
                "tags": ["SDK"], "notes": "no standalone CLI (Python SDK / web)", "launchable": False},
    "mentat": {"name": "Mentat", "bins": ["mentat", "mentat.exe"],
               "tags": ["abandoned"], "notes": "project defunct", "launchable": False},
    "plandex": {"name": "Plandex", "bins": ["plandex", "plandex.exe", "pdx", "pdx.exe"],
                "tags": ["WSL"], "notes": "WSL-only; needs Plandex server",
                "version_argv": ["version"], "launchable": False},
}

# --- custom agents (JSON config) -------------------------------------------


def _load_custom():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return
    if not isinstance(data, list):
        return
    for item in data:
        if not isinstance(item, dict):
            continue
        aid = str(item.get("id") or "").strip().lower()
        name = str(item.get("name") or "").strip()
        bin_name = str(item.get("bin") or "").strip()
        if not (aid and name and bin_name) or aid in _SPECS:
            continue
        args = item.get("args") or []
        if not isinstance(args, list):
            args = []
        env = item.get("env") or {}
        if not isinstance(env, dict):
            env = {}
        _SPECS[aid] = {
            "name": name,
            "bins": [bin_name, bin_name + ".exe"],
            "tags": ["custom"],
            "notes": str(item.get("notes") or "user-defined agent"),
            "launchable": True,
            "extra_args": [str(a) for a in args],
            "extra_env": {str(k): str(v) for k, v in env.items()},
            "default_cwd": str(item.get("cwd") or DEFAULT_CWD),
        }


_load_custom()

# --- detection --------------------------------------------------------------
_lock = threading.Lock()
_CACHE = {}  # agent id -> {"found": bool, "bin": str|None, "version": str}


def _find_binary(names):
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    for n in names:
        for d in _KNOWN_DIRS:
            for ext in ("", ".exe", ".cmd"):
                cand = os.path.join(d, n + ext)
                if os.path.isfile(cand):
                    return cand
    return None


def _probe_version(bin_path, version_argv):
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        r = subprocess.run([bin_path] + version_argv, capture_output=True,
                           text=True, timeout=6, creationflags=flags)
    except Exception:
        return ""
    text = _ANSI_RE.sub("", (r.stdout or r.stderr) or "")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return ""
    for ln in lines:
        if re.match(r"^\s*v?\d", ln):
            return ln[:80]
    for ln in reversed(lines):
        if _VERSION_RE.search(ln):
            return ln[:80]
    return ""


def _probe_all():
    from concurrent.futures import ThreadPoolExecutor

    def probe_one(item):
        aid, spec = item
        bin_path = _find_binary(spec["bins"])
        version = ""
        if bin_path:
            version = _probe_version(bin_path, spec.get("version_argv", ["--version"]))
        with _lock:
            _CACHE[aid] = {"found": bool(bin_path), "bin": bin_path, "version": version}

    items = list(_SPECS.items())
    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(probe_one, items))


def rescan():
    if ENABLED:
        _probe_all()


# --- public API -------------------------------------------------------------


def get_spec(aid):
    return _SPECS.get(aid)


def is_agent(aid):
    return aid in _SPECS


def can_launch(aid):
    spec = _SPECS.get(aid)
    return bool(spec and spec.get("launchable"))


def find_bin(aid):
    with _lock:
        entry = _CACHE.get(aid)
    if entry and entry["found"]:
        return entry["bin"]
    return None


def spec_env(aid):
    spec = _SPECS.get(aid) or {}
    return dict(spec.get("extra_env", {}))


def spec_cwd(aid):
    spec = _SPECS.get(aid) or {}
    return spec.get("default_cwd") or DEFAULT_CWD


def launch_argv(aid, bin_path):
    """argv for the ConPTY spawn. npm-style .cmd/.bat shims are run through
    cmd.exe /c exactly like a local console would; native .exe spawn directly."""
    spec = _SPECS.get(aid) or {}
    extra = spec.get("extra_args") or []
    if bin_path and bin_path.lower().endswith((".cmd", ".bat")):
        return ["cmd.exe", "/c", bin_path] + extra
    if bin_path:
        return [bin_path] + extra
    return [aid] + extra


def available_agents():
    out = []
    order = sorted(_SPECS.items(), key=lambda kv: (not kv[1].get("launchable", True), kv[0]))
    for aid, spec in order:
        with _lock:
            entry = _CACHE.get(aid)
        if not entry or not entry["found"]:
            continue
        out.append({
            "id": aid,
            "name": spec["name"],
            "version": entry["version"],
            "tags": spec.get("tags", []),
            "notes": spec.get("notes", ""),
            "launchable": bool(spec.get("launchable", True)),
        })
    return out


def start_background():
    if ENABLED:
        threading.Thread(target=_probe_all, daemon=True).start()


start_background()