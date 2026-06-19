#!/usr/bin/env python3
"""Smoke-test the MCP server without a client.

Confirms the server imports, every tool is registered, and a couple of
read-only diagnostics actually run on this host. Exits non-zero on failure so
it can gate CI or a pre-publish check.

    python mcp_server/selfcheck.py
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import server  # noqa: E402


EXPECTED_TOOLS = {
    "system_overview", "cpu_usage", "memory_usage", "disk_usage",
    "power_and_thermal", "top_processes", "network_overview", "listening_ports",
    "service_status", "recent_errors", "check_command_safety", "propose_command",
}


def main() -> int:
    tools = {t.name for t in asyncio.run(server.mcp.list_tools())}
    missing = EXPECTED_TOOLS - tools
    if missing:
        print(f"FAIL: tools not registered: {sorted(missing)}")
        return 1
    print(f"OK: {len(tools)} tools registered.")

    # Run a few read-only diagnostics so we exercise the real code paths.
    import diagnostics as diag

    print("\n--- cpu_usage ---")
    print(diag.cpu_usage())
    print("\n--- memory_usage ---")
    print(diag.memory_usage())
    print("\n--- check_command_safety (rm -rf /) ---")
    print(server.check_command_safety("rm -rf /"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
