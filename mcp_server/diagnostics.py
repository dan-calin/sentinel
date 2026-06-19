#!/usr/bin/env python3
"""Read-only diagnostics — the safe telemetry catalog exposed over MCP.

Every function here answers a question about the host by *reading* state only:
``/proc`` and ``/sys`` pseudo-files, or well-known read-only commands (``df``,
``ps``, ``ss``, ``journalctl``, ``systemctl status``). Nothing here mutates the
system, so an external AI may call any of it without a human approval gate —
that is the whole point of the split documented in ``server.py``.

The functions return human-readable plain text (with the actual numbers), which
is what a calling model reads back to the user. They degrade gracefully: a
missing tool or pseudo-file yields a clear note, never an exception, so a query
about CPU never fails just because, say, ``sensors`` is not installed.
"""

from __future__ import annotations

import glob
import os
import platform
import shutil
import socket
import time

from core import COMMAND_TIMEOUT_SECONDS, run_command  # noqa: E402  (path set by caller)

# Cap journalctl-style queries so a chatty journal can't flood the model.
_MAX_LOG_LINES = 200


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _read(path: str) -> str | None:
    """Return the stripped contents of a pseudo-file, or ``None`` if unreadable."""
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return None


def _read_int(path: str) -> int | None:
    """Read an integer pseudo-file (e.g. a /sys counter), or ``None``."""
    raw = _read(path)
    if raw is None:
        return None
    try:
        return int(raw.split()[0])
    except (ValueError, IndexError):
        return None


def _have(tool: str) -> bool:
    """Whether an executable is on PATH."""
    return shutil.which(tool) is not None


def _fmt_bytes(num: float) -> str:
    """Format a byte count as a compact human string (e.g. '7.4 GB')."""
    step = 1024.0
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(num) < step:
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= step
    return f"{num:.1f} EB"


# ---------------------------------------------------------------------------
# Host identity
# ---------------------------------------------------------------------------

def _os_label() -> str:
    """One-line OS/distro description."""
    system = platform.system() or "Unknown"
    release = platform.release()
    pretty = ""
    raw = _read("/etc/os-release")
    if raw:
        for line in raw.splitlines():
            if line.startswith("PRETTY_NAME="):
                pretty = line.split("=", 1)[1].strip().strip('"')
                break
    label = f"{system} {release}".strip()
    return f"{label} ({pretty})" if pretty else label


def _uptime() -> str:
    """Human uptime derived from /proc/uptime, or 'unknown'."""
    raw = _read("/proc/uptime")
    if not raw:
        return "unknown"
    try:
        seconds = int(float(raw.split()[0]))
    except (ValueError, IndexError):
        return "unknown"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


def host_info() -> str:
    """Identity snapshot: hostname, OS, kernel, uptime, CPU model, core count."""
    hostname = socket.gethostname()
    cores = os.cpu_count() or "?"
    cpu_model = ""
    cpuinfo = _read("/proc/cpuinfo")
    if cpuinfo:
        for line in cpuinfo.splitlines():
            if line.lower().startswith("model name"):
                cpu_model = line.split(":", 1)[1].strip()
                break
    lines = [
        f"Hostname:  {hostname}",
        f"OS:        {_os_label()}",
        f"Kernel:    {platform.release()}",
        f"Uptime:    {_uptime()}",
        f"CPU:       {cpu_model or 'unknown'} ({cores} logical cores)",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CPU
# ---------------------------------------------------------------------------

def _cpu_times() -> tuple[int, int] | None:
    """Return (busy, total) jiffies from the aggregate line of /proc/stat."""
    raw = _read("/proc/stat")
    if not raw:
        return None
    first = raw.splitlines()[0].split()
    if not first or first[0] != "cpu":
        return None
    try:
        values = [int(v) for v in first[1:]]
    except ValueError:
        return None
    # user nice system idle iowait irq softirq steal guest guest_nice
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    total = sum(values)
    return total - idle, total


def cpu_usage(sample_seconds: float = 0.4) -> str:
    """Overall CPU utilization sampled over a short interval, plus load average.

    Computed straight from /proc/stat (two samples), so it needs no external
    tool and reflects true busy-vs-idle jiffies rather than a single snapshot.
    """
    first = _cpu_times()
    usage_line = "CPU usage: unavailable (could not read /proc/stat)"
    if first is not None:
        time.sleep(max(0.05, min(sample_seconds, 2.0)))
        second = _cpu_times()
        if second is not None:
            busy = second[0] - first[0]
            total = second[1] - first[1]
            pct = (100.0 * busy / total) if total > 0 else 0.0
            usage_line = f"CPU usage: {pct:.1f}% (averaged over {sample_seconds:.1f}s)"

    cores = os.cpu_count() or 1
    load_line = "Load average: unavailable"
    try:
        one, five, fifteen = os.getloadavg()
        load_line = (
            f"Load average: {one:.2f}, {five:.2f}, {fifteen:.2f} "
            f"(1/5/15 min; {cores} cores, so {one / cores * 100:.0f}% of capacity at 1 min)"
        )
    except (OSError, AttributeError):
        pass
    return f"{usage_line}\n{load_line}"


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------

def memory_usage() -> str:
    """RAM and swap usage parsed from /proc/meminfo (kB values)."""
    raw = _read("/proc/meminfo")
    if not raw:
        result = run_command("free -h")
        return result.stdout.strip() or "Memory info unavailable."
    info: dict[str, int] = {}
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            try:
                info[parts[0].rstrip(":")] = int(parts[1]) * 1024  # kB -> bytes
            except ValueError:
                continue
    total = info.get("MemTotal", 0)
    available = info.get("MemAvailable", info.get("MemFree", 0))
    used = total - available
    swap_total = info.get("SwapTotal", 0)
    swap_free = info.get("SwapFree", 0)
    swap_used = swap_total - swap_free
    lines = []
    if total:
        pct = 100.0 * used / total
        lines.append(
            f"RAM:  {_fmt_bytes(used)} used / {_fmt_bytes(total)} "
            f"({pct:.0f}% used, {_fmt_bytes(available)} available)"
        )
    if swap_total:
        spct = 100.0 * swap_used / swap_total
        lines.append(
            f"Swap: {_fmt_bytes(swap_used)} used / {_fmt_bytes(swap_total)} ({spct:.0f}% used)"
        )
    else:
        lines.append("Swap: none configured")
    return "\n".join(lines) or "Memory info unavailable."


# ---------------------------------------------------------------------------
# Disk
# ---------------------------------------------------------------------------

def disk_usage(path: str | None = None) -> str:
    """Filesystem capacity. With ``path``, just that mount; otherwise real ones.

    Uses ``df`` and filters out pseudo/virtual filesystems (tmpfs, devtmpfs,
    overlay, squashfs …) so the answer is about actual storage, not RAM disks.
    """
    if path:
        safe = path.replace("'", "'\\''")
        result = run_command(f"df -h -- '{safe}'")
        return result.stdout.strip() or result.stderr.strip() or "No output."

    result = run_command("df -h --output=source,size,used,avail,pcent,target")
    if result.exit_code != 0 or not result.stdout.strip():
        result = run_command("df -h")  # older df without --output
        return result.stdout.strip() or "Disk info unavailable."
    skip = ("tmpfs", "devtmpfs", "overlay", "squashfs", "udev", "efivarfs", "none")
    kept = []
    for i, line in enumerate(result.stdout.splitlines()):
        if i == 0 or not any(line.split() and line.split()[0].lower().startswith(s) for s in skip):
            kept.append(line)
    return "\n".join(kept).strip() or result.stdout.strip()


# ---------------------------------------------------------------------------
# Processes
# ---------------------------------------------------------------------------

def top_processes(by: str = "cpu", count: int = 10) -> str:
    """Top processes by CPU or memory.

    Args:
        by: ``"cpu"`` (default) or ``"mem"``.
        count: How many rows to return (1-50).
    """
    count = max(1, min(int(count), 50))
    sort_key = "-%mem" if str(by).lower().startswith("mem") else "-%cpu"
    cmd = (
        f"ps -eo pid,user,%cpu,%mem,rss,comm --sort={sort_key} | head -n {count + 1}"
    )
    result = run_command(cmd)
    return result.stdout.strip() or result.stderr.strip() or "No process data."


# ---------------------------------------------------------------------------
# Power and thermal
# ---------------------------------------------------------------------------

def _rapl_power() -> str | None:
    """Estimate package power draw (watts) from Intel/AMD RAPL energy counters.

    RAPL exposes a monotonically increasing microjoule counter per power domain;
    sampling it twice over an interval yields average watts. Returns ``None``
    when no RAPL domain is readable.
    """
    domains = sorted(glob.glob("/sys/class/powercap/intel-rapl:*"))
    readings: list[tuple[str, int]] = []
    for domain in domains:
        if ":" not in os.path.basename(domain).split("intel-rapl:", 1)[-1]:
            energy = _read_int(os.path.join(domain, "energy_uj"))
            name = _read(os.path.join(domain, "name")) or os.path.basename(domain)
            if energy is not None:
                readings.append((name, energy))
    if not readings:
        return None
    interval = 0.5
    time.sleep(interval)
    lines = []
    for name, before in readings:
        domain = next(
            (d for d in domains if (_read(os.path.join(d, "name")) or os.path.basename(d)) == name),
            None,
        )
        after = _read_int(os.path.join(domain, "energy_uj")) if domain else None
        if after is None:
            continue
        delta = after - before
        if delta < 0:  # counter wrapped; skip this sample
            continue
        watts = (delta / 1_000_000) / interval
        lines.append(f"  {name}: {watts:.1f} W")
    return "\n".join(lines) if lines else None


def _battery_power() -> list[str]:
    """Per-supply power/charge lines from /sys/class/power_supply, if present."""
    lines: list[str] = []
    for supply in sorted(glob.glob("/sys/class/power_supply/*")):
        kind = _read(os.path.join(supply, "type")) or ""
        name = os.path.basename(supply)
        if kind == "Mains":
            online = _read(os.path.join(supply, "online"))
            state = {"1": "online", "0": "offline"}.get(online, "unknown")
            lines.append(f"  AC adapter ({name}): {state}")
        elif kind == "Battery":
            power_uw = _read_int(os.path.join(supply, "power_now"))
            if power_uw is None:
                current = _read_int(os.path.join(supply, "current_now"))
                voltage = _read_int(os.path.join(supply, "voltage_now"))
                if current is not None and voltage is not None:
                    power_uw = int(current * voltage / 1_000_000)
            capacity = _read(os.path.join(supply, "capacity"))
            status = _read(os.path.join(supply, "status")) or "unknown"
            detail = f"{status}"
            if capacity is not None:
                detail += f", {capacity}%"
            if power_uw is not None:
                detail += f", {power_uw / 1_000_000:.1f} W"
            lines.append(f"  Battery ({name}): {detail}")
    return lines


def _thermal() -> list[str]:
    """Temperature lines from /sys/class/thermal zones (millidegrees C)."""
    lines: list[str] = []
    for zone in sorted(glob.glob("/sys/class/thermal/thermal_zone*")):
        milli = _read_int(os.path.join(zone, "temp"))
        if milli is None:
            continue
        label = _read(os.path.join(zone, "type")) or os.path.basename(zone)
        lines.append(f"  {label}: {milli / 1000:.1f} °C")
    return lines


def power_and_thermal() -> str:
    """Power draw, battery/AC state, and temperatures — whatever the host exposes.

    Power consumption depends on hardware support (Intel/AMD RAPL, a battery
    gauge, or ``lm-sensors``); when none is present this says so plainly rather
    than failing, since many headless servers expose no power telemetry at all.
    """
    sections: list[str] = []

    rapl = _rapl_power()
    if rapl:
        sections.append("Power draw (RAPL):\n" + rapl)

    battery = _battery_power()
    if battery:
        sections.append("Power supplies:\n" + "\n".join(battery))

    thermal = _thermal()
    if thermal:
        sections.append("Temperatures:\n" + "\n".join(thermal))

    if not thermal and _have("sensors"):
        out = run_command("sensors").stdout.strip()
        if out:
            sections.append("lm-sensors:\n" + out)

    if not sections:
        return (
            "No power or thermal telemetry is exposed by this host. "
            "This is common on VMs and many headless servers (no RAPL domain, "
            "no battery, and lm-sensors not installed)."
        )
    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------

def network_overview() -> str:
    """Interface addresses and a count of listening sockets."""
    sections = []
    if _have("ip"):
        addr = run_command("ip -brief address").stdout.strip()
        if addr:
            sections.append("Interfaces:\n" + addr)
    if not sections:
        out = run_command("hostname -I").stdout.strip()
        if out:
            sections.append("IP addresses: " + out)
    return "\n\n".join(sections) or "Network info unavailable."


def listening_ports() -> str:
    """TCP/UDP sockets in the LISTEN state (with owning process where visible)."""
    if _have("ss"):
        result = run_command("ss -tulnp")
    elif _have("netstat"):
        result = run_command("netstat -tulnp")
    else:
        return "Neither 'ss' nor 'netstat' is available."
    return result.stdout.strip() or result.stderr.strip() or "No listening sockets found."


# ---------------------------------------------------------------------------
# Services and logs (read-only systemd/journal queries)
# ---------------------------------------------------------------------------

def _valid_unit(unit: str) -> bool:
    """Allow only sane systemd unit names — defense in depth for a passed arg."""
    return bool(unit) and len(unit) <= 128 and all(
        c.isalnum() or c in "-_.@\\:" for c in unit
    )


def service_status(unit: str) -> str:
    """Read-only ``systemctl status`` for one unit (no start/stop/restart)."""
    if not _have("systemctl"):
        return "systemd is not available on this host."
    if not _valid_unit(unit):
        return f"Refusing to query an invalid unit name: {unit!r}"
    safe = unit.replace("'", "'\\''")
    # Options must precede `--`; everything after it is taken as the unit operand.
    result = run_command(f"systemctl status --no-pager -n 20 -- '{safe}'")
    return result.stdout.strip() or result.stderr.strip() or "No status output."


def recent_errors(since: str = "1 hour ago", count: int = 50) -> str:
    """Error-priority journal entries since a given time (read-only)."""
    if not _have("journalctl"):
        return "journalctl is not available on this host."
    count = max(1, min(int(count), _MAX_LOG_LINES))
    safe_since = since.replace("'", "'\\''")
    result = run_command(
        f"journalctl -p err --since '{safe_since}' --no-pager -n {count}"
    )
    out = result.stdout.strip()
    if not out:
        return f"No error-level journal entries since '{since}'."
    return out


# ---------------------------------------------------------------------------
# Combined snapshot
# ---------------------------------------------------------------------------

def system_overview() -> str:
    """One-call health snapshot stitched from the individual diagnostics."""
    blocks = [
        ("HOST", host_info()),
        ("CPU & LOAD", cpu_usage()),
        ("MEMORY", memory_usage()),
        ("DISK", disk_usage()),
        ("TOP PROCESSES (by CPU)", top_processes("cpu", 5)),
        ("POWER & THERMAL", power_and_thermal()),
    ]
    return "\n\n".join(f"=== {title} ===\n{body}" for title, body in blocks)
