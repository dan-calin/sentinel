#!/usr/bin/env python3
"""Render a faithful SVG 'screenshot' of the Sentinel CLI for the README.

Uses rich's own recording console, so the image is the real UI output (the same
panels and styling the app prints) with representative sample content - no API
key or live run required. Regenerate with:  python assets/make_screenshot.py
"""

import os
import sys
from pathlib import Path

# Make the repo-root modules (core, main) importable when run from assets/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# A Linux-looking path + name for the banner, regardless of where this runs.
os.environ.setdefault("USER", "roxan")
os.getcwd = lambda: "/home/roxan"  # noqa: E731 - cosmetic, screenshot only

from rich.console import Console  # noqa: E402
from rich.panel import Panel  # noqa: E402
from rich.syntax import Syntax  # noqa: E402

import core  # noqa: E402
import main  # noqa: E402

rec = Console(record=True, width=92)
main.console = rec  # route the app's rendering helpers into the recording console


class _DemoEngine:
    label = "Anthropic (Claude)"
    model = "claude-sonnet-4-6"
    spec = core.PROVIDERS["anthropic"]


profile = core.UserProfile(experience="intermediate", explain=True)

# 1) Welcome box (the real banner).
main._print_banner(_DemoEngine(), profile)

# 2) A full request -> review -> approve -> run -> summary cycle.
rec.print()
rec.print(r"[bold cyan]>[/] [dim](anthropic:claude-sonnet-4-6)[/] how much free disk space do I have?")
rec.print(
    Panel(
        "Shows how much space each mounted filesystem is using, in human-readable "
        "units. This is read-only and safe to run.",
        title="[bold blue]What this does[/]", border_style="blue", title_align="left",
    )
)
rec.print(
    Panel(
        Syntax("df -h", "bash", theme="ansi_dark", word_wrap=True),
        title="[bold yellow]Proposed command - review before running[/]",
        subtitle="[dim]This will run on your local machine[/]",
        border_style="yellow", title_align="left",
    )
)
rec.print("[bold yellow]⚠  Nothing runs until you approve it.[/]")
rec.print(r"Execute this command? [magenta]\[y/n][/] (n): y")

df_output = (
    "Filesystem      Size  Used Avail Use% Mounted on\n"
    "/dev/sdd       1007G  6.9G  949G   1% /\n"
    "tmpfs           7.8G     0  7.8G   0% /dev/shm\n"
    "drivers         953G  848G  106G  89% /usr/lib/wsl/drivers"
)
rec.print(Panel(df_output, title="stdout", border_style="green", title_align="left"))
rec.print("[green]Exit code: 0[/]")
rec.print(
    Panel(
        "You're using about 6.9 GB of your 1 TB root disk, so roughly 949 GB "
        "(about 94%) is still free.",
        title="[bold green]In short[/]", border_style="green", title_align="left",
    )
)

out = os.path.join(os.path.dirname(__file__), "sentinel-demo.svg")
rec.save_svg(out, title="Sentinel")
print("wrote", out)
