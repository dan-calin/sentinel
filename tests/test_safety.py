#!/usr/bin/env python3
"""Regression tests for the destructive-command blocklist (``core.screen_command``).

The confirmation gate is Sentinel's real safety control, but the blocklist is the
backstop that refuses unmistakably destructive commands *before* the user is even
asked. These tests pin that behavior so a future regex tweak can't silently let a
``rm -rf /`` through — the exact bug this suite was born from.

Runs under pytest, or standalone (``python tests/test_safety.py``) with no
dependencies so it works on a bare box.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core  # noqa: E402

# ---------------------------------------------------------------------------
# Commands that MUST be blocked. Each was chosen to exercise a distinct rule —
# and several are the precise forms that slipped through earlier.
# ---------------------------------------------------------------------------
DANGEROUS = [
    # Recursive / wildcard rm — the original miss was the flag/wildcard forms.
    "rm -rf /",
    "rm -rf /*",
    "rm -rf -- *",                      # space-to-hyphen: `\b-` never matched this
    "rm -rf --no-preserve-root /",
    "rm -rf ~",
    "rm -r mydir",
    "rm --recursive mydir",
    "sudo rm -rf /var/log",
    "rm -f *",
    "rm *.txt",
    "rm ./*",
    # Filesystem / device destruction.
    "mkfs.ext4 /dev/sdb1",
    "dd if=/dev/zero of=/dev/sda",
    "dd of=/dev/sdb bs=1M",
    "echo boom > /dev/sda",
    "cat data > /dev/nvme0n1",
    "shred -u secret.txt",
    "wipefs -a /dev/sda",
    "mkswap /dev/sdb",
    "fdisk /dev/sda",
    "parted /dev/sda",
    "sgdisk --zap-all /dev/sda",
    # Fork bomb.
    ":(){ :|:& };:",
    # Lock-out and critical-file clobbering.
    "chmod -R 000 /",
    "echo x > /etc/passwd",
    "cat junk > /etc/shadow",
    "echo '' > /etc/fstab",
]

# ---------------------------------------------------------------------------
# Commands that MUST be allowed. These are the read-only / benign operations
# Sentinel exists to run, including tokens that look dangerous but are not
# (rmdir, grep -r, `cat /etc/passwd`, a piped `grep dd`).
# ---------------------------------------------------------------------------
SAFE = [
    "ls -la",
    "df -h",
    "free -h",
    "ps aux",
    "journalctl -p err --since '1 hour ago' --no-pager",
    "rm file.txt",                       # single file, no -r, no wildcard
    "rmdir emptydir",                    # 'rm' is not a word here
    "grep -r pattern .",                 # -r belongs to grep, not rm
    "cat /etc/passwd",                   # reading, not redirecting onto it
    "find . -name '*.log'",              # wildcard, but no rm
    "du -sh *",                          # wildcard, but no rm
    "ps aux | grep dd",                  # 'dd' present, but no of=
    "systemctl status nginx",
    "tar -czf backup.tar.gz mydir",
    "docker ps",
    "head -n 20 /var/log/syslog",
    "uptime",
]


def test_dangerous_commands_are_blocked():
    """Every dangerous command is refused, with a non-empty reason."""
    for command in DANGEROUS:
        verdict = core.screen_command(command)
        assert not verdict.allowed, f"NOT blocked but should be: {command!r}"
        assert verdict.reason, f"blocked without a reason: {command!r}"


def test_safe_commands_are_allowed():
    """Every safe command passes the blocklist."""
    for command in SAFE:
        verdict = core.screen_command(command)
        assert verdict.allowed, f"blocked but should be allowed: {command!r} ({verdict.reason})"


def test_rm_rf_root_is_blocked():
    """The canonical catastrophe stays blocked (guards the original regression)."""
    assert not core.screen_command("rm -rf /").allowed
    assert not core.screen_command("rm -rf -- *").allowed


def _run_standalone() -> int:
    """Tiny runner so the suite works without pytest installed."""
    failures = 0
    for command in DANGEROUS:
        if core.screen_command(command).allowed:
            print(f"FAIL (should block): {command!r}")
            failures += 1
    for command in SAFE:
        if not core.screen_command(command).allowed:
            print(f"FAIL (should allow): {command!r}")
            failures += 1
    total = len(DANGEROUS) + len(SAFE)
    print(f"{total - failures}/{total} cases passed "
          f"({len(DANGEROUS)} dangerous, {len(SAFE)} safe).")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run_standalone())
