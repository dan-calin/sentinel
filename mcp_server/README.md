# Sentinel MCP Server

Expose a Linux host to AI assistants as a connectable skill over the
[Model Context Protocol](https://modelcontextprotocol.io). Connect Claude (or
your own agent/workspace) to Sentinel and ask, in plain language:

> *"How's the homelab's CPU and power consumption looking right now?"*

Claude calls Sentinel, Sentinel reads the machine, and reports back — no SSH
session, no remembering which flags `journalctl` wants.

This is the same idea as the Sentinel CLI, pointed the other way: instead of a
human typing English at the terminal, another **AI** queries the host through a
defined, safe tool surface.

## Safety model

The CLI's promise is that *nothing runs without explicit human approval*. An
autonomous AI caller has no human at the keyboard, so the server keeps that
promise with a hard split:

- **Read-only diagnostics are exposed and answered directly.** Every diagnostic
  tool maps to a fixed, vetted command or a `/proc`//`/sys` read. They only
  observe state, so there is nothing to approve. The AI supplies parameters (a
  unit name, a time window, a row count) — never an arbitrary command.
- **Arbitrary execution is never exposed.** There is no "run this command"
  tool. `propose_command` translates English into a command and screens it
  against the destructive-command blocklist, then returns it **without
  running it**. A human still approves and runs it in the Sentinel CLI.

So the connected AI can *observe* freely and *plan* safely; only a person can
change the system.

## Tools

| Tool | What it returns | Changes the system? |
|---|---|---|
| `system_overview` | One-call snapshot: host, CPU/load, memory, disk, top processes, power/thermal | No |
| `cpu_usage` | Live CPU utilization + load average vs core count | No |
| `memory_usage` | RAM and swap usage | No |
| `disk_usage` | Filesystem capacity (real storage only) | No |
| `power_and_thermal` | RAPL power draw, battery/AC, temperatures | No |
| `top_processes` | Busiest processes by CPU or memory | No |
| `network_overview` | Interfaces and IP addresses | No |
| `listening_ports` | Sockets in LISTEN state | No |
| `service_status` | Read-only `systemctl status` for one unit | No |
| `recent_errors` | Error-priority journal entries in a time window | No |
| `check_command_safety` | Screens a command against the blocklist | No (does not run it) |
| `propose_command` | English → a vetted command, returned for human approval | No (does not run it) |

`propose_command` is the only tool that needs an AI provider configured (it
reuses your saved Sentinel settings, or a provider key from the environment).
Everything else works with no API key.

## Install

On the machine you want to report on:

```bash
cd ~/Linux_LLM
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt            # core deps (for propose_command)
pip install -r mcp_server/requirements.txt # the MCP SDK
```

## Connect a client

### Claude Code

```bash
claude mcp add sentinel -- /home/youruser/Linux_LLM/.venv/bin/python \
  /home/youruser/Linux_LLM/mcp_server/server.py
```

### Claude Desktop / other MCP clients

Add the `sentinel` block from
[`claude_desktop_config.example.json`](claude_desktop_config.example.json) to
your client's MCP config (for Claude Desktop, `claude_desktop_config.json`),
adjust the absolute paths, and restart the client. Use the Python interpreter
from the venv where you installed the dependencies.

### Quick check

```bash
# Smoke-test that tools register and a diagnostic runs (no client needed):
python mcp_server/selfcheck.py
```

## Roadmap

- [ ] Multi-host / fleet: report on more than the local machine (homelab + box),
      selecting a target per call.
- [ ] Optional HTTP/SSE transport for remote clients (stdio is local-only).
- [ ] Richer power accounting (per-process energy, GPU draw).
