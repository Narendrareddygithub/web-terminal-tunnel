#!/usr/bin/env node
"use strict";

const { spawn } = require("child_process");
const fs = require("fs");
const path = require("path");

const PS1 = path.join(__dirname, "..", "Start-WebTerminal.ps1");
const args = process.argv.slice(2);

const USAGE = `wtt-web - web terminal tunnel for Windows

Usage:
  wtt-web [options]

Options:
  -Shell <id>        powershell | pwsh | cmd | bash (Git Bash) | wsl
                     (default: powershell)
  -ShellChoice       let the client pick any installed shell from a dropdown
  -SessionMinutes N  hard time limit in minutes (0 = unlimited)
  --help, -h         show this help

Starts a temporary, code-protected, web-accessible terminal: spawns a real
shell via ConPTY, streams it to a browser over WebSocket, exposes it through
a Cloudflare quick tunnel, and prints the URL, 2-digit code and a QR code.
Press Ctrl+C to tear down the server and tunnel.`;

if (args.some((a) => a === "--help" || a === "-h")) {
  process.stdout.write(USAGE + "\n");
  process.exit(0);
}

if (!fs.existsSync(PS1)) {
  process.stderr.write(`wtt-web: not found: ${PS1}\nReinstall the package (npm i -g wtt-web).\n`);
  process.exit(1);
}

function findPowerShellHost() {
  if (process.env.WTT_PSHOST) return process.env.WTT_PSHOST;
  const pathVar = process.env.Path || process.env.PATH || "";
  for (const dir of pathVar.split(";")) {
    if (!dir) continue;
    try {
      if (fs.existsSync(path.join(dir, "pwsh.exe"))) return "pwsh";
    } catch (_) {
      /* ignore unreadable dir */
    }
  }
  return "powershell.exe";
}

const host = findPowerShellHost();
const child = spawn(
  host,
  ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", PS1, ...args],
  { stdio: "inherit", windowsHide: false }
);

process.on("SIGINT", () => {
  // The child shares this console and already received the same Ctrl+C.
  // Let it run its finally-block teardown; exit with its status below.
});

child.on("error", (err) => {
  process.stderr.write(`wtt-web: failed to start ${host}: ${err.message}\n`);
  process.exit(1);
});

child.on("exit", (code) => {
  process.exit(code === null ? 1 : code);
});