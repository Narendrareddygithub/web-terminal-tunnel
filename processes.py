"""
processes.py
Live local process enumeration for web-terminal-tunnel (Windows).

Lists currently-running shell and AI-agent processes on this machine so the
dashboard can show them alongside the hub-managed ConPTY sessions. The rows
are read-only snapshots: an already-running external process cannot be adopted
into ConPTY, so agent rows get a "Control" action that spawns a parallel hub
session of the same agent instead.

Enumeration uses psutil (process name + command line). The launcher installs
psutil with the other deps; if it is missing the scan degrades to an empty
list rather than crashing the server.

Matching rules:
  - Shells match by executable name (powershell/pwsh/cmd/bash/wsl).
  - Agents match by executable name OR any command-line token containing a
    known agent bin stem (catches node.exe running opencode, and npm .cmd
    shims like `cmd.exe /c opencode.cmd`).
  - A shell whose command line hosts an agent shim is suppressed (it lists as
    the agent, not as a plain shell).
  - Hub-managed ConPTY pids (passed in as live_hub_pids) are never listed.
"""

import os

try:
    import psutil
except ImportError:
    psutil = None

import agents

SHELL_EXES = {
    "powershell.exe": "powershell",
    "pwsh.exe": "pwsh",
    "cmd.exe": "cmd",
    "bash.exe": "bash",
    "wsl.exe": "wsl",
}

SHELL_NAMES = {
    "powershell": "PowerShell",
    "pwsh": "PowerShell 7",
    "cmd": "Command Prompt",
    "bash": "Git Bash",
    "wsl": "WSL (Linux)",
}


def _stems():
    return agents.known_stems() if agents.ENABLED else {}


def _cmdline_has_agent(cmdline, stems):
    for token in cmdline:
        tl = token.lower()
        for stem in stems:
            if stem in tl:
                return True
    return False


def _cmdline_agent_id(cmdline, stems):
    for token in cmdline:
        tl = token.lower()
        for stem, aid in stems.items():
            if stem in tl:
                return aid
    return None


def scan(live_hub_pids=()):
    """Return [{pid, name, kind, id, status, created}] for local shell/agent
    processes, excluding hub-managed ConPTY pids. Empty list if psutil is
    unavailable."""
    if psutil is None:
        return []
    live = set(live_hub_pids)
    stems = _stems()
    out = []
    for p in psutil.process_iter(["pid", "name", "cmdline", "create_time"]):
        try:
            pid = p.info["pid"]
            if pid in live or pid == os.getpid():
                continue
            name = (p.info["name"] or "").lower()
            cmdline = p.info["cmdline"] or []
            created = p.info["create_time"] or 0
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

        # Agent by executable name (opencode.exe, claude.exe, ...).
        aid = stems.get(name.replace(".exe", ""))
        if aid:
            spec = agents.get_spec(aid) or {}
            out.append({
                "pid": pid,
                "name": spec.get("name", aid),
                "kind": "agent",
                "id": aid,
                "status": "running",
                "created": created,
            })
            continue

        # Plain shell by executable name.
        shell_id = SHELL_EXES.get(name)
        if shell_id:
            # Skip shells that are only hosting an agent shim (cmd.exe /c opencode.cmd).
            if _cmdline_has_agent(cmdline, stems):
                continue
            out.append({
                "pid": pid,
                "name": SHELL_NAMES.get(shell_id, name[:-4].capitalize()),
                "kind": "shell",
                "id": shell_id,
                "status": "running",
                "created": created,
            })
            continue

        # Agent by command line (node.exe ... opencode ...).
        aid = _cmdline_agent_id(cmdline, stems)
        if aid:
            spec = agents.get_spec(aid) or {}
            out.append({
                "pid": pid,
                "name": spec.get("name", aid),
                "kind": "agent",
                "id": aid,
                "status": "running",
                "created": created,
            })
    return out