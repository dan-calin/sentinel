# Sentinel Agent

Run Sentinel on more than one machine. Each managed host (a VM, a homelab box)
runs this **agent** — a small HTTP service — and your main machine runs the
Sentinel CLI as the **controller**, which registers each agent and drives it
over the LAN.

```
            ┌─────────────── controller (Sentinel CLI) ───────────────┐
            │  LLM translate · safety blocklist · y/n gate · history   │
            └───────────┬───────────────────────┬─────────────────────┘
                        │ HTTP                   │ HTTP
                 ┌──────▼──────┐          ┌──────▼──────┐
                 │  agent: vm  │          │ agent:homelab│
                 │ diagnostics │          │ diagnostics  │
                 │  /execute   │          │  /execute    │
                 └─────────────┘          └──────────────┘
```

## Safety: two token tiers

The agent separates *observing* from *changing* with two bearer tokens:

- **`SENTINEL_AGENT_READ_TOKEN`** — read-only diagnostics (`/diagnostics/*`).
  This is what you'd hand to an external AI (Claude, a Jarvis-style workspace).
- **`SENTINEL_AGENT_ADMIN_TOKEN`** — `/execute`. **`/execute` is disabled unless
  this is set**, so an agent is read-only by default. Even with it, every command
  is screened against Sentinel's destructive-command blocklist *on the agent*
  (defense in depth); the human `y/n` approval already happened on the controller.

The token is the only thing protecting the host — **bind to a trusted LAN/VPN and
firewall the port**, and put TLS (a reverse proxy) in front for anything else.

## Endpoints

| Method | Path | Token | Purpose |
|---|---|---|---|
| GET | `/health` | none | Liveness + identity (hostname, OS, `exec_enabled`, `monitor_enabled`) |
| GET | `/diagnostics` | read | List available diagnostics |
| POST | `/diagnostics/{name}` | read | Run one diagnostic (optional `{"params": {…}}`) |
| POST | `/execute` | admin | Screen a command, then run it |
| GET | `/monitor` | read | The health-monitor config |
| POST | `/monitor` | admin | Update it (enable, interval, thresholds) |
| POST | `/monitor/run` | admin | Run the checks now; returns alerts |
| GET | `/alerts` | read | Recent recorded alerts |

Diagnostics mirror the read-only MCP tools: `system_overview`, `cpu_usage`,
`memory_usage`, `disk_usage`, `power_and_thermal`, `top_processes`,
`network_overview`, `listening_ports`, `service_status`, `recent_errors`.

## Run it (on each managed host)

One command — it sets up the venv on first run, generates and remembers the
tokens, and prints the URL + tokens to register on the controller. On the first
run it also **offers to install itself as an always-on systemd service** (starts
on boot) and enable periodic health checks — say yes and it's done:

```bash
cd ~/Linux_LLM
./run-agent.sh                 # asks to install as a boot service; else foreground
./run-agent.sh --service       # non-interactive: install + enable health checks
./run-agent.sh --read-only     # diagnostics only (no execute)
PORT=9000 ./run-agent.sh       # pick a port (also HOST=127.0.0.1)
```

Installed as a service, manage it with `systemctl status sentinel-agent` and
`journalctl -u sentinel-agent -f`. The **health monitor** then runs in the
background, comparing disk/memory/load/services/error-log readings to thresholds
and recording alerts the controller reads with `alerts` (or `settings → Fleet
alerts`). Tune thresholds per host from the controller's `settings` menu.

Or do it by hand:

```bash
./setup.sh --agent             # venv + deps
export SENTINEL_AGENT_READ_TOKEN="$(openssl rand -hex 16)"
export SENTINEL_AGENT_ADMIN_TOKEN="$(openssl rand -hex 16)"   # omit for read-only
uvicorn agent.server:app --host 0.0.0.0 --port 8765
```

Then, on the controller (your main Sentinel CLI):

```
host add          # name, URL (http://<host>:8765), and the tokens
hosts             # list + health
use vm            # target it
on vm  what is using the most disk?
on all uptime     # fan out to every host (and local)
```

## Notes / roadmap

- Phase 1: read diagnostics + execute approved commands remotely. Undo and
  checkpoints currently apply to the **local** host only (remote undo is next).
- Translation grounds on the controller's environment; per-host grounding (using
  the agent's `/health`) is a planned refinement.
- A streamable-HTTP MCP transport (so external AIs connect to an agent the same
  way Claude Desktop uses the stdio MCP server) is on the roadmap.
