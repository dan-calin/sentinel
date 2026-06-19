#!/usr/bin/env python3
"""Tests for undo's reversible machinery: classification, checkpoints, journal.

Covers the parts that don't need a model — command classification (which
commands are mutating, which paths to snapshot, which state changes are
reversible), the checkpoint save/restore round-trip (modify + create), and the
journal's undo bookkeeping. The LLM-generated inverse command is exercised
manually since it needs a provider.

Runs under pytest, or standalone (``python tests/test_checkpoints.py``).
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core  # noqa: E402

# (command, expected_mutating, expected_to_capture_a_path)
CLASSIFY_CASES = [
    ("rm notes.txt", True, True),
    ("mkdir foo", True, True),
    ("echo hi > out.txt", True, True),
    ("sed -i s/a/b/ conf.txt", True, True),
    ("sed -i.bak s/a/b/ conf.txt", True, True),
    ("sed s/a/b/ conf.txt", False, False),          # no -i: writes to stdout
    ("cp a.txt b.txt", True, True),
    # State changes: reversible via inverse, nothing to snapshot.
    ("systemctl stop nginx", True, False),
    ("sudo systemctl disable docker", True, False),
    ("service ssh restart", True, False),
    ("apt install htop", True, False),
    ("pacman -S vim", True, False),
    # Read-only: must NOT be flagged.
    ("systemctl status nginx", False, False),
    ("apt list --installed", False, False),
    ("ls -la", False, False),
    ("docker ps", False, False),
    ("cat /etc/passwd", False, False),
]


def test_classify_command():
    for command, expect_mutating, expect_path in CLASSIFY_CASES:
        mutating, paths = core.classify_command(command)
        assert mutating == expect_mutating, f"mutating wrong for {command!r}: {mutating}"
        assert bool(paths) == expect_path, f"path capture wrong for {command!r}: {paths}"


def test_checkpoint_restore_roundtrip(tmp_path_factory=None):
    """A checkpoint restores a modified file and removes a created one."""
    with tempfile.TemporaryDirectory() as work, tempfile.TemporaryDirectory() as cfg:
        os.environ["XDG_CONFIG_HOME"] = cfg  # keep checkpoints out of the real config
        existing = os.path.join(work, "config.txt")
        created = os.path.join(work, "new.txt")
        with open(existing, "w") as handle:
            handle.write("ORIGINAL")

        checkpoint = core.create_checkpoint("edit+create", [existing, created])
        assert checkpoint is not None and checkpoint.saved_count == 1

        # Simulate the command's effect.
        with open(existing, "w") as handle:
            handle.write("MODIFIED")
        with open(created, "w") as handle:
            handle.write("NEW")

        core.restore_checkpoint(checkpoint)

        with open(existing) as handle:
            assert handle.read() == "ORIGINAL", "modified file was not restored"
        assert not os.path.exists(created), "created file was not removed on undo"


def test_journal_undo_bookkeeping():
    with tempfile.TemporaryDirectory() as cfg:
        os.environ["XDG_CONFIG_HOME"] = cfg
        entry = core.record_command("make dir", "mkdir foo", "/tmp", 0, True, None)
        assert entry is not None
        assert core.last_undoable().id == entry.id
        core.mark_undone(entry.id)
        assert core.last_undoable() is None, "undone entry should no longer be undoable"


def _run_standalone() -> int:
    failures = 0
    for command, expect_mutating, expect_path in CLASSIFY_CASES:
        mutating, paths = core.classify_command(command)
        if mutating != expect_mutating or bool(paths) != expect_path:
            print(f"FAIL classify {command!r}: mutating={mutating} paths={paths}")
            failures += 1
    for name, fn in (("restore", test_checkpoint_restore_roundtrip),
                     ("journal", test_journal_undo_bookkeeping)):
        try:
            fn()
        except AssertionError as exc:
            print(f"FAIL {name}: {exc}")
            failures += 1
    print(f"{'all passed' if not failures else str(failures) + ' failed'} "
          f"({len(CLASSIFY_CASES)} classify cases + 2 round-trips).")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run_standalone())
