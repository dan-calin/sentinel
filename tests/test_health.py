#!/usr/bin/env python3
"""Tests for the health-monitor threshold logic and config round-trips.

`health.evaluate` is pure (readings + thresholds -> alerts), so it's the part
worth pinning; the collectors and the alert store touch the host/filesystem and
are exercised by the agent end-to-end. Runs under pytest or standalone.
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import health  # noqa: E402

_T = health.Thresholds(disk_pct=90, memory_pct=90, load_factor=2.0,
                        error_count=50, services=["nginx"])


def test_evaluate_flags_each_breach():
    readings = {
        "disks": [("/", 97), ("/data", 50)], "memory_pct": 93,
        "load_ratio": 3.1, "services": {"nginx": "failed"}, "error_count": 80,
    }
    by_check = {a.check: a for a in health.evaluate(readings, _T)}
    assert set(by_check) == {"disk", "memory", "load", "service", "errors"}
    assert by_check["disk"].level == "critical"      # 97% >= 95
    assert by_check["service"].level == "critical"
    assert by_check["memory"].level == "warning"     # 93% < 95


def test_evaluate_clean_when_under_thresholds():
    readings = {
        "disks": [("/", 10)], "memory_pct": 20, "load_ratio": 0.1,
        "services": {"nginx": "active"}, "error_count": 0,
    }
    assert health.evaluate(readings, _T) == []


def test_missing_readings_are_ignored():
    # None readings (tool unavailable on the host) must not raise or alert.
    readings = {"disks": [], "memory_pct": None, "load_ratio": None,
                "services": {}, "error_count": None}
    assert health.evaluate(readings, _T) == []


def test_config_roundtrip():
    with tempfile.TemporaryDirectory() as cfg:
        os.environ["XDG_CONFIG_HOME"] = cfg
        config = health.MonitorConfig(
            enabled=True, interval_seconds=120,
            thresholds=health.Thresholds(disk_pct=80, services=["ssh"]),
        )
        assert health.save_monitor_config(config)
        loaded = health.load_monitor_config()
        assert loaded.enabled and loaded.interval_seconds == 120
        assert loaded.thresholds.disk_pct == 80 and loaded.thresholds.services == ["ssh"]


def _run_standalone() -> int:
    failures = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except AssertionError as exc:
                print(f"FAIL {name}: {exc}")
                failures += 1
    print("all passed" if not failures else f"{failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run_standalone())
