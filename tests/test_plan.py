#!/usr/bin/env python3
"""Tests for plan-step parsing (the pure part of plan/task mode).

`core.parse_plan_step` turns a model's step output into ('done'|'ask'|'command',
value). The outline/step LLM calls are exercised live; this pins the parsing.
Runs under pytest or standalone.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core  # noqa: E402

_FENCE = chr(96) * 3


def test_done_variants():
    for raw in ("DONE", "DONE.", "done", "  DONE  ", ""):
        assert core.parse_plan_step(raw) == ("done", ""), raw


def test_ask_extracts_question():
    kind, value = core.parse_plan_step("ASK: which Minecraft version?")
    assert kind == "ask" and value == "which Minecraft version?"


def test_commands_are_sanitized():
    assert core.parse_plan_step("$ wget https://x/y.jar") == ("command", "wget https://x/y.jar")
    assert core.parse_plan_step(f"{_FENCE}bash\nmkdir foo\n{_FENCE}") == ("command", "mkdir foo")


def test_done_only_triggers_as_first_token():
    # A command that merely mentions DONE elsewhere is still a command.
    kind, _ = core.parse_plan_step("touch DONE.flag")
    assert kind == "command"


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
