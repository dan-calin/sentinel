#!/usr/bin/env python3
"""Health monitoring — thresholds, checks, and the alert store for the agent.

Each Sentinel agent runs a background monitor (see ``agent/server.py``) that
periodically takes numeric readings of the host, compares them to configurable
thresholds, and records any breach as an *alert*. The controller (or an AI over
MCP) reads those alerts later — "any problems on the fleet today?".

This module is deliberately standalone (no UI, no core import): pure-ish numeric
collectors plus a pure :func:`evaluate` so the threshold logic is easy to test.
It runs on the managed host, where /proc, df, systemctl, and journalctl live.
"""

from __future__ import annotations

import datetime
import json
import os
import shutil
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class Thresholds:
    """When a reading crosses one of these, the monitor raises an alert."""

    disk_pct: int = 90          # any real filesystem at/above this % used
    memory_pct: int = 90        # RAM used at/above this %
    load_factor: float = 2.0    # 1-min load average ÷ cores at/above this
    error_count: int = 50       # error-priority journal entries in one interval
    services: list[str] = field(default_factory=list)  # units that must be active

    def to_dict(self) -> dict:
        return {
            "disk_pct": self.disk_pct, "memory_pct": self.memory_pct,
            "load_factor": self.load_factor, "error_count": self.error_count,
            "services": list(self.services),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Thresholds":
        base = cls()
        return cls(
            disk_pct=int(data.get("disk_pct", base.disk_pct)),
            memory_pct=int(data.get("memory_pct", base.memory_pct)),
            load_factor=float(data.get("load_factor", base.load_factor)),
            error_count=int(data.get("error_count", base.error_count)),
            services=list(data.get("services", [])),
        )


@dataclass
class MonitorConfig:
    """The agent's monitor settings, persisted on the host."""

    enabled: bool = False
    interval_seconds: int = 300
    thresholds: Thresholds = field(default_factory=Thresholds)

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled, "interval_seconds": self.interval_seconds,
            "thresholds": self.thresholds.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "MonitorConfig":
        return cls(
            enabled=bool(data.get("enabled", False)),
            interval_seconds=max(30, int(data.get("interval_seconds", 300))),
            thresholds=Thresholds.from_dict(data.get("thresholds", {})),
        )


@dataclass(frozen=True)
class Alert:
    """One recorded threshold breach."""

    id: str
    time: str
    check: str          # "disk" | "memory" | "load" | "service" | "errors"
    level: str          # "warning" | "critical"
    message: str
    value: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id, "time": self.time, "check": self.check,
            "level": self.level, "message": self.message, "value": self.value,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Alert":
        return cls(
            id=data.get("id", ""), time=data.get("time", ""),
            check=data.get("check", ""), level=data.get("level", "warning"),
            message=data.get("message", ""), value=data.get("value", ""),
        )


# ---------------------------------------------------------------------------
# State paths (XDG, on the monitored host)
# ---------------------------------------------------------------------------

def _state_dir() -> Path:
    base = os.getenv("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "sentinel"


def monitor_config_path() -> Path:
    return _state_dir() / "agent-monitor.json"


def alerts_path() -> Path:
    return _state_dir() / "agent-alerts.jsonl"


def load_monitor_config() -> MonitorConfig:
    try:
        with monitor_config_path().open(encoding="utf-8") as handle:
            return MonitorConfig.from_dict(json.load(handle))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return MonitorConfig()


def save_monitor_config(config: MonitorConfig) -> bool:
    path = monitor_config_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            json.dump(config.to_dict(), handle, indent=2)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Numeric readings (host introspection)
# ---------------------------------------------------------------------------

def _run(cmd: list[str], timeout: int = 15) -> str:
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return done.stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _disk_readings() -> list[tuple[str, int]]:
    """(mount, percent-used) for real filesystems, via df."""
    out = _run(["df", "-P", "--output=pcent,target"]) or _run(["df", "-P"])
    readings = []
    skip = ("tmpfs", "devtmpfs", "overlay", "squashfs", "udev", "efivarfs")
    for i, line in enumerate(out.splitlines()):
        if i == 0:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        # df -P columns: Filesystem Size Used Avail Use% Mounted-on  (or pcent,target)
        pct_token = next((p for p in parts if p.endswith("%")), "")
        mount = parts[-1]
        if not pct_token or any(s in line for s in skip):
            continue
        try:
            readings.append((mount, int(pct_token.rstrip("%"))))
        except ValueError:
            continue
    return readings


def _memory_percent() -> int | None:
    try:
        info = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, _, rest = line.partition(":")
            info[key] = int(rest.split()[0])  # kB
    except (OSError, ValueError, IndexError):
        return None
    total = info.get("MemTotal", 0)
    available = info.get("MemAvailable", info.get("MemFree", 0))
    if not total:
        return None
    return round(100 * (total - available) / total)


def _load_ratio() -> float | None:
    try:
        one = os.getloadavg()[0]
    except (OSError, AttributeError):
        return None
    return one / (os.cpu_count() or 1)


def _service_states(services: list[str]) -> dict[str, str]:
    states = {}
    if services and shutil.which("systemctl"):
        for unit in services:
            out = _run(["systemctl", "is-active", unit]).strip()
            states[unit] = out or "unknown"
    return states


def _error_count(since_iso: str) -> int | None:
    if not shutil.which("journalctl"):
        return None
    out = _run(["journalctl", "-p", "err", "--since", since_iso, "--no-pager", "-q"])
    return sum(1 for line in out.splitlines() if line.strip())


def collect_readings(thresholds: Thresholds, since_iso: str | None = None) -> dict:
    """Take a numeric snapshot of the host for threshold evaluation."""
    return {
        "disks": _disk_readings(),
        "memory_pct": _memory_percent(),
        "load_ratio": _load_ratio(),
        "services": _service_states(thresholds.services),
        "error_count": _error_count(since_iso) if since_iso else None,
    }


# ---------------------------------------------------------------------------
# Evaluation (pure) and the run loop
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def evaluate(readings: dict, thresholds: Thresholds) -> list[Alert]:
    """Compare readings to thresholds and return alerts. Pure and testable."""
    alerts: list[Alert] = []

    def add(check: str, level: str, message: str, value: str = "") -> None:
        alerts.append(Alert(uuid.uuid4().hex[:8], _now_iso(), check, level, message, value))

    for mount, pct in readings.get("disks") or []:
        if pct >= thresholds.disk_pct:
            level = "critical" if pct >= 95 else "warning"
            add("disk", level, f"Disk {mount} is {pct}% full", f"{pct}%")

    mem = readings.get("memory_pct")
    if mem is not None and mem >= thresholds.memory_pct:
        level = "critical" if mem >= 95 else "warning"
        add("memory", level, f"Memory is {mem}% used", f"{mem}%")

    load = readings.get("load_ratio")
    if load is not None and load >= thresholds.load_factor:
        add("load", "warning", f"Load is {load:.2f}× the core count", f"{load:.2f}x")

    for unit, state in (readings.get("services") or {}).items():
        if state != "active":
            add("service", "critical", f"Service {unit} is {state}", state)

    errors = readings.get("error_count")
    if errors is not None and errors >= thresholds.error_count:
        add("errors", "warning", f"{errors} error-log entries this interval", str(errors))

    return alerts


def append_alerts(alerts: list[Alert], cap: int = 500) -> None:
    """Append alerts to the on-host store, keeping at most ``cap`` recent ones."""
    if not alerts:
        return
    existing = recent_alerts(cap)
    combined = existing + alerts
    combined = combined[-cap:]
    path = alerts_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            for alert in combined:
                handle.write(json.dumps(alert.to_dict()) + "\n")
    except OSError:
        pass


def recent_alerts(limit: int = 100) -> list[Alert]:
    try:
        lines = alerts_path().read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        line = line.strip()
        if line:
            try:
                out.append(Alert.from_dict(json.loads(line)))
            except (json.JSONDecodeError, KeyError):
                continue
    return out[-limit:]


def run_once(config: MonitorConfig, since_iso: str | None = None) -> list[Alert]:
    """Take readings, evaluate, persist any alerts, and return them."""
    if since_iso is None:
        delta = datetime.timedelta(seconds=config.interval_seconds)
        since_iso = (datetime.datetime.now() - delta).isoformat(timespec="seconds")
    readings = collect_readings(config.thresholds, since_iso)
    alerts = evaluate(readings, config.thresholds)
    append_alerts(alerts)
    return alerts
