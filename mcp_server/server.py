#!/usr/bin/env python3
"""Sentinel MCP server — expose the host to AIs as a connectable skill.

This turns Sentinel into a *tool another AI can call*. An assistant like Claude
(or your own Jarvis-style workspace) connects over the Model Context Protocol
and asks things like "how's the homelab's CPU and power looking?"; Sentinel runs
the corresponding read-only diagnostic on the machine and reports back.

SAFETY MODEL — the important part
---------------------------------
The single-host CLI guarantees that *nothing runs without explicit human
approval*. An autonomous AI caller has no human at the keyboard, so we preserve
that guarantee with a hard split:

* **Read-only diagnostics are exposed and run directly.** Every ``@mcp.tool``
  below maps to a fixed, vetted command or ``/proc``//``/sys`` read in
  ``diagnostics.py``. They only observe state, so there is nothing to approve.
  The AI cannot supply an arbitrary command to these — only parameters (a unit
  name, a time window, a row count), which are validated.

* **Arbitrary execution is NOT exposed.** There is no "run this command" tool.
  The closest thing, ``propose_command``, translates English into a command and
  returns it together with a safety verdict — but never executes it. A human
  still runs it through the Sentinel CLI's confirmation gate. The AI can plan;
  only a person can pull the trigger.

Transport is stdio (what Claude Desktop, Claude Code, and most MCP clients use).
See ``mcp_server/README.md`` for client configuration.
"""

from __future__ import annotations

import os
import sys

# The server lives in a subpackage; make the repo root importable so we reuse
# the exact same `core` the CLI uses (engines, the safety filter, execution).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp.server.fastmcp import FastMCP  # noqa: E402

import core  # noqa: E402
import diagnostics as diag  # noqa: E402

mcp = FastMCP(
    "sentinel",
    instructions=(
        "Sentinel reports on a Linux host it runs on. Use its read-only "
        "diagnostic tools (system_overview, cpu_usage, memory_usage, "
        "disk_usage, power_and_thermal, top_processes, network_overview, "
        "listening_ports, service_status, recent_errors) to answer questions "
        "about the machine's health, capacity, processes, power, and logs. "
        "These never change the system. To act ON the system, use "
        "propose_command to get a vetted command, then tell the user to run it "
        "in the Sentinel CLI — Sentinel never executes changes without a human "
        "approving them."
    ),
)


# ---------------------------------------------------------------------------
# Read-only diagnostics
# ---------------------------------------------------------------------------

@mcp.tool()
def system_overview() -> str:
    """Full health snapshot of the host in one call: identity, CPU and load,
    memory, disk capacity, the busiest processes, and power/thermal. Start here
    for an open-ended "how is the machine doing?" question."""
    return diag.system_overview()


@mcp.tool()
def cpu_usage() -> str:
    """Current overall CPU utilization (sampled live from /proc/stat) and the
    1/5/15-minute load average relative to core count."""
    return diag.cpu_usage()


@mcp.tool()
def memory_usage() -> str:
    """RAM and swap usage: how much is used, available, and the percentage."""
    return diag.memory_usage()


@mcp.tool()
def disk_usage(path: str | None = None) -> str:
    """Filesystem capacity for real storage (pseudo-filesystems filtered out).
    Pass an optional ``path`` to report only the filesystem holding it."""
    return diag.disk_usage(path)


@mcp.tool()
def power_and_thermal() -> str:
    """Power draw (Intel/AMD RAPL), battery/AC state, and temperatures —
    whatever telemetry the hardware exposes. Says so plainly if none is
    available (common on VMs and headless servers)."""
    return diag.power_and_thermal()


@mcp.tool()
def top_processes(by: str = "cpu", count: int = 10) -> str:
    """The most resource-hungry processes. ``by`` is "cpu" (default) or "mem";
    ``count`` is how many rows (1-50)."""
    return diag.top_processes(by, count)


@mcp.tool()
def network_overview() -> str:
    """Network interfaces and their IP addresses."""
    return diag.network_overview()


@mcp.tool()
def listening_ports() -> str:
    """TCP/UDP sockets in the LISTEN state, with the owning process where the
    caller has permission to see it."""
    return diag.listening_ports()


@mcp.tool()
def service_status(unit: str) -> str:
    """Read-only ``systemctl status`` for one systemd unit (e.g. "ssh",
    "docker"). Does not start, stop, or restart anything."""
    return diag.service_status(unit)


@mcp.tool()
def recent_errors(since: str = "1 hour ago", count: int = 50) -> str:
    """Error-priority journal entries since a time window (e.g. "30 min ago",
    "today"). Read-only."""
    return diag.recent_errors(since, count)


# ---------------------------------------------------------------------------
# Plan-only bridge (no execution)
# ---------------------------------------------------------------------------

@mcp.tool()
def check_command_safety(command: str) -> str:
    """Screen a shell command against Sentinel's destructive-command blocklist
    WITHOUT running it. Returns whether it is allowed and, if blocked, why."""
    verdict = core.screen_command(command)
    if verdict.allowed:
        return f"ALLOWED — no destructive pattern matched.\nCommand: {command}"
    return f"BLOCKED — {verdict.reason}\nCommand: {command}"


@mcp.tool()
def propose_command(request: str) -> str:
    """Translate a plain-English request into a single shell command and screen
    it — but DO NOT run it. Returns the command plus its safety verdict so a
    human can review and run it in the Sentinel CLI. Use this when the user
    wants to *change* something; execution always requires human approval.

    Requires an AI provider to be configured (saved Sentinel settings or a
    provider API key in the environment)."""
    engine = _build_engine()
    if engine is None:
        return (
            "No AI provider is configured for the MCP server. Set one up by "
            "running the Sentinel CLI once (it saves your provider/model/key), "
            "or export a provider key (e.g. ANTHROPIC_API_KEY) in the server's "
            "environment."
        )
    try:
        command = engine.translate(request)
    except core.TranslationError as exc:
        return f"Could not translate the request: {exc}"

    if command == core.UNSUPPORTED_SENTINEL or not command:
        return "Sentinel could not turn that into a single shell command (UNSUPPORTED)."

    verdict = core.screen_command(command)
    safety = (
        "ALLOWED by the blocklist (still requires human y/n approval to run)"
        if verdict.allowed
        else f"BLOCKED — {verdict.reason}"
    )
    return (
        f"Proposed command (NOT executed):\n  {command}\n\n"
        f"Safety: {safety}\n\n"
        "To run it, paste it into the Sentinel CLI and approve it there."
    )


def _build_engine() -> "core.Engine | None":
    """Build an engine from saved settings / environment, or ``None``.

    Mirrors the CLI's resolution order so the MCP server uses the same provider
    the user already configured, without any interactive prompting.
    """
    settings = core.load_settings()
    provider_key = os.getenv("SENTINEL_PROVIDER") or settings.provider
    spec = core.PROVIDERS.get(provider_key) if provider_key else None
    if spec is None:
        # Fall back to the first provider that is ready from the environment.
        spec = next((s for s in core.PROVIDERS.values() if core.provider_is_ready(s)), None)
    if spec is None:
        return None

    model = os.getenv("SENTINEL_MODEL") or settings.model or spec.default_model
    api_key = ""
    if spec.api_key_env:
        api_key = os.getenv(spec.api_key_env) or settings.api_keys.get(spec.key, "")
    base_url = settings.base_urls.get(spec.key) or spec.base_url
    if spec.runtime_config:
        base_url = os.getenv("CUSTOM_BASE_URL") or base_url
    if not spec.keyless and not spec.runtime_config and not api_key:
        return None
    try:
        return core.create_engine(spec, model, api_key, base_url)
    except core.TranslationError:
        return None


def main() -> None:
    """Run the MCP server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
