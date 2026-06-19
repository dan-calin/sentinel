#!/usr/bin/env python3
"""Sentinel agent — the per-host network service a controller (or AI) connects to.

Run this on each machine you want to manage (a VM, a homelab box). A Sentinel
*controller* (the CLI on your main machine) registers the agent's URL and drives
it over the LAN: reading diagnostics and — only with the admin token — executing
commands a human already approved on the controller.

SAFETY / PRIVILEGE TIERS
------------------------
Two bearer tokens, so observing and changing are separated across the network:

* ``SENTINEL_AGENT_READ_TOKEN``  → read-only diagnostics. This is what an external
  AI (Claude, a Jarvis-style workspace) would be given.
* ``SENTINEL_AGENT_ADMIN_TOKEN`` → ``/execute``. **Disabled unless this is set**, so
  an agent is read-only by default. Even then, ``/execute`` screens every command
  against Sentinel's destructive-command blocklist server-side (defense in depth);
  the human ``y/n`` approval happens on the controller before it is ever called.

Bind to your LAN/VPN and firewall it — the token is the only thing between a
caller and your host. Run behind TLS (a reverse proxy) for anything but a trusted
local network.

    SENTINEL_AGENT_READ_TOKEN=... SENTINEL_AGENT_ADMIN_TOKEN=... \
        uvicorn agent.server:app --host 0.0.0.0 --port 8765
"""

from __future__ import annotations

import os
import platform
import socket
import sys

# Reuse the project core (safety filter, execution) and the diagnostics catalog.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)
sys.path.insert(0, os.path.join(_REPO_ROOT, "mcp_server"))

from fastapi import Depends, FastAPI, Header, HTTPException  # noqa: E402
from pydantic import BaseModel  # noqa: E402

import core  # noqa: E402
import diagnostics as diag  # noqa: E402

AGENT_VERSION = "1"

# Diagnostic name -> (callable, takes-params?). All are read-only.
_DIAGNOSTICS = {
    "system_overview": diag.system_overview,
    "cpu_usage": diag.cpu_usage,
    "memory_usage": diag.memory_usage,
    "disk_usage": diag.disk_usage,
    "power_and_thermal": diag.power_and_thermal,
    "top_processes": diag.top_processes,
    "network_overview": diag.network_overview,
    "listening_ports": diag.listening_ports,
    "service_status": diag.service_status,
    "recent_errors": diag.recent_errors,
}

app = FastAPI(title="Sentinel agent", version=AGENT_VERSION)


def _read_token() -> str | None:
    return os.getenv("SENTINEL_AGENT_READ_TOKEN")


def _admin_token() -> str | None:
    return os.getenv("SENTINEL_AGENT_ADMIN_TOKEN")


def _bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token.")
    return authorization.split(" ", 1)[1].strip()


def require_read(authorization: str | None = Header(default=None)) -> None:
    """Allow callers holding the read OR admin token (admin is a superset)."""
    token = _bearer(authorization)
    valid = {t for t in (_read_token(), _admin_token()) if t}
    if not valid:
        raise HTTPException(status_code=503, detail="Agent has no tokens configured.")
    if token not in valid:
        raise HTTPException(status_code=403, detail="Invalid token.")


def require_admin(authorization: str | None = Header(default=None)) -> None:
    """Allow only the admin token; 404-style refusal when execution is disabled."""
    admin = _admin_token()
    if not admin:
        raise HTTPException(status_code=403, detail="Execution is disabled on this agent.")
    if _bearer(authorization) != admin:
        raise HTTPException(status_code=403, detail="Admin token required.")


class DiagnosticRequest(BaseModel):
    params: dict = {}


class ExecuteRequest(BaseModel):
    command: str


@app.get("/health")
def health() -> dict:
    """Unauthenticated liveness + identity, so a controller can probe an agent."""
    return {
        "ok": True,
        "service": "sentinel-agent",
        "version": AGENT_VERSION,
        "hostname": socket.gethostname(),
        "os": diag._os_label(),
        "kernel": platform.release(),
        "home": os.path.expanduser("~"),
        "exec_enabled": bool(_admin_token()),
    }


@app.get("/diagnostics")
def list_diagnostics(_: None = Depends(require_read)) -> dict:
    """The read-only diagnostics this agent can run."""
    return {"diagnostics": sorted(_DIAGNOSTICS)}


@app.post("/diagnostics/{name}")
def run_diagnostic(
    name: str, body: DiagnosticRequest = DiagnosticRequest(), _: None = Depends(require_read)
) -> dict:
    """Run one read-only diagnostic, optionally with parameters."""
    func = _DIAGNOSTICS.get(name)
    if func is None:
        raise HTTPException(status_code=404, detail=f"Unknown diagnostic: {name}")
    try:
        output = func(**body.params) if body.params else func()
    except TypeError as exc:  # bad params for this diagnostic
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"name": name, "output": output}


@app.post("/execute")
def execute(body: ExecuteRequest, _: None = Depends(require_admin)) -> dict:
    """Run a command — but only after it passes the destructive-command blocklist.

    The controller has already shown it to a human and gotten approval; this
    server-side screen is defense in depth so a bypassed controller still can't
    push a destructive command through.
    """
    command = body.command.strip()
    if not command:
        raise HTTPException(status_code=422, detail="Empty command.")
    verdict = core.screen_command(command)
    if not verdict.allowed:
        raise HTTPException(status_code=422, detail=f"Refused by safety filter: {verdict.reason}")
    result = core.run_command(command)
    return {
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }
