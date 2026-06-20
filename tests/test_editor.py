#!/usr/bin/env python3
"""Tests for the paste-aware line editor's multi-row redraw math.

`_compose_redraw` is pure (no TTY), so the wrapping logic that caused long
lines to be re-spammed on every keystroke is unit-testable. Runs under pytest
or standalone.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402

_PROMPT = "> "   # rendered prompt
_PC = 2          # its visible width
_COLS = 20


def _redraw(buf: str, pos: int, old_pos: int, old_rows: int):
    return main._compose_redraw(_PROMPT, _PC, list(buf), pos, old_pos, old_rows, _COLS)


def test_row_count_accounts_for_prompt_and_wrap():
    # 2 (prompt) + 25 = 27 cells over 20 cols -> 2 rows.
    _, rows = _redraw("d" * 25, 25, old_pos=24, old_rows=2)
    assert rows == 2
    # Just under one row.
    _, rows = _redraw("d" * 10, 10, old_pos=10, old_rows=1)
    assert rows == 1


def test_delete_on_wrapped_line_clears_old_rows_not_spam():
    # The reported bug: editing a wrapped line must erase the prior rows, not
    # append a fresh copy. A 2-row prior render must be fully cleared.
    out, rows = _redraw("d" * 24, 24, old_pos=25, old_rows=2)
    # One clear-to-EOL per prior row (one in the loop + the top-row clear).
    assert out.count("\x1b[0K") == 2
    assert "\x1b[1A" in out          # walked up to clear the wrapped row
    assert out.count("\n") == 0      # never emits a newline mid-edit here


def test_shrink_below_one_row_clears_the_vacated_row():
    out, rows = _redraw("d" * 10, 10, old_pos=24, old_rows=2)
    assert rows == 1
    assert out.count("\x1b[0K") == 2  # both previously-used rows cleared


def test_exact_column_boundary_emits_explicit_wrap():
    # 2 + 18 == 20 == cols: the pending end-of-row wrap is made explicit.
    out, rows = _redraw("d" * 18, 18, old_pos=0, old_rows=1)
    assert "\r\n" in out
    assert rows == 2


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
