#!/usr/bin/env python3
"""Sentinel — a natural-language Linux server manager (CLI).

This is the terminal UI. All the actual logic — translation, the safety filter,
command execution, providers/engines, and the user profile — lives in
``core.py``, which is shared with the GUI backend (``server.py``) so both run the
exact same engine.

Describe what you want in plain English; Sentinel translates it into a single
shell command, shows it to you, and runs it **only after you approve it**. It can
also explain commands, answer Linux questions (``ask`` / ``chat``), and summarize
a command's output back into plain English — all tuned to your experience level.
"""

from __future__ import annotations

import os
import select
import sys
import threading
from contextlib import contextmanager

from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

import core

# ---------------------------------------------------------------------------
# Theme & branding
# ---------------------------------------------------------------------------

# Primary brand accent — a dark blue. Semantic colors (green=success, red=error,
# yellow=caution, blue=info) are kept separate so they still read at a glance.
ACCENT = "#2563eb"

# Sentinel's mark: a little owl — a silent, watchful guardian (and a nod to the
# "owl" models). Each line is the same width so the centered box keeps it aligned.
_MASCOT = r"""
^___^
(o,o)
( _ )
-"-"-
"""

# A single shared console keeps styling consistent across the whole app.
console = Console()


# ---------------------------------------------------------------------------
# Esc-to-cancel support
#
# Runs a unit of work in a worker thread while the main thread watches for Esc.
# Works on an interactive POSIX TTY (e.g. WSL); degrades to a plain blocking
# run anywhere else (no TTY, native Windows) so behavior is never broken.
# ---------------------------------------------------------------------------

try:
    import termios
    import tty

    _TERMIOS_OK = True
except ImportError:  # non-POSIX (e.g. native Windows)
    _TERMIOS_OK = False


@contextmanager
def _raw_tty():
    """Put the terminal in cbreak mode so single keypresses read immediately.

    Yields ``True`` when raw mode is active; otherwise ``False`` (callers then
    skip Esc handling and run uninterrupted).
    """
    if not (_TERMIOS_OK and sys.stdin.isatty()):
        yield False
        return
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        yield True
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


def _esc_pressed(timeout: float) -> bool:
    """Return ``True`` if Esc was pressed within ``timeout`` seconds.

    A bare Esc is distinguished from an arrow-key escape sequence (which is
    drained and ignored).
    """
    if not select.select([sys.stdin], [], [], timeout)[0]:
        return False
    if sys.stdin.read(1) != "\x1b":
        return False
    if select.select([sys.stdin], [], [], 0)[0]:  # bytes follow → a sequence
        while select.select([sys.stdin], [], [], 0)[0]:
            sys.stdin.read(1)
        return False
    return True


def run_with_cancel(target, status_text: str, cancel_event=None):
    """Run ``target()`` with a spinner; Esc requests cancellation.

    Returns ``(result, cancelled)``. With a ``cancel_event`` (command
    execution), Esc sets it and we wait for the terminated result. Without one
    (LLM calls), Esc abandons the wait and the background result is discarded.
    Re-raises any exception ``target`` raised when not cancelled.
    """
    box: dict = {}

    def worker() -> None:
        try:
            box["value"] = target()
        except BaseException as exc:  # surfaced to the caller below
            box["error"] = exc

    thread = threading.Thread(target=worker, daemon=True)
    cancelled = False

    with _raw_tty() as can_cancel:
        hint = "  [dim](Esc to cancel)[/]" if can_cancel else ""
        with console.status(f"{status_text}{hint}", spinner="dots"):
            thread.start()
            while thread.is_alive():
                if can_cancel and _esc_pressed(0.12):
                    cancelled = True
                    if cancel_event is not None:
                        cancel_event.set()
                        thread.join(timeout=5)
                    break
                if not can_cancel:
                    thread.join(timeout=0.12)

    if cancelled and cancel_event is None:
        return None, True
    if "error" in box:
        raise box["error"]
    return box.get("value"), cancelled


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------

def _render_result(result: core.CommandResult) -> None:
    """Render a command's captured output in clearly-labeled panels."""
    if result.stdout:
        console.print(
            Panel(result.stdout.rstrip(), title="stdout", border_style="green", title_align="left")
        )
    if result.stderr:
        console.print(
            Panel(result.stderr.rstrip(), title="stderr", border_style="red", title_align="left")
        )
    status_style = "green" if result.exit_code == 0 else "red"
    console.print(f"[{status_style}]Exit code: {result.exit_code}[/]")


def confirm_execution(command: str, explanation: str = "") -> bool:
    """Display the command (and optional explanation) and require an explicit ``y``.

    This is the non-negotiable safety gate: returns ``True`` only when the user
    types ``y``. Anything else (including a bare Enter) is a refusal.
    """
    if explanation:
        console.print(
            Panel(
                explanation,
                title="[bold blue]What this does[/]",
                border_style="blue",
                title_align="left",
            )
        )
    console.print(
        Panel(
            Syntax(command, "bash", theme="ansi_dark", word_wrap=True),
            title="[bold yellow]Proposed command — review before running[/]",
            subtitle="[dim]This will run on your local machine[/]",
            border_style="yellow",
            title_align="left",
        )
    )
    console.print("[bold yellow]⚠  Nothing runs until you approve it.[/]")
    return Prompt.ask("Execute this command?", choices=["y", "n"], default="n") == "y"


def _print_answer(engine: core.Engine, answer: str) -> None:
    """Render a Q&A answer in a distinct (non-command) panel."""
    console.print(
        Panel(
            answer,
            title=f"[bold blue]Answer[/] [dim]· {engine.model}[/]",
            border_style="blue",
            title_align="left",
        )
    )


def _print_summary(summary: str) -> None:
    """Render the post-execution plain-English summary of a command's output."""
    console.print(
        Panel(summary, title="[bold green]In short[/]", border_style="green", title_align="left")
    )


# ---------------------------------------------------------------------------
# Provider & model selection (interactive)
# ---------------------------------------------------------------------------

# Remembered connection settings, loaded once in main(). Credentials come from
# the environment (.env) first, then this store, so a key is only ever entered
# once and reused on subsequent launches.
_settings: core.Settings = core.Settings()


def _remember(engine: core.Engine) -> None:
    """Persist the active provider + model so the next launch skips the menus."""
    _settings.provider = engine.spec.key
    _settings.model = engine.model
    core.save_settings(_settings)


def resolve_credentials(spec: core.ProviderSpec) -> core.Credentials:
    """Gather the API key and base URL for a provider.

    Order: environment (.env) first, then the saved settings store, then a
    one-time prompt. A prompted value is saved so it is never asked again.
    """
    creds = core.Credentials(base_url=spec.base_url)

    if spec.runtime_config:  # Custom: the user supplies everything.
        base = os.getenv("CUSTOM_BASE_URL") or _settings.base_urls.get(spec.key)
        if not base:
            base = Prompt.ask("Base URL (OpenAI-compatible, e.g. https://host/v1)")
        key = os.getenv("CUSTOM_API_KEY") or _settings.api_keys.get(spec.key)
        if key is None:
            key = Prompt.ask("API key [dim](leave blank if not required)[/]", default="", password=True)
        creds.base_url, creds.api_key = base, key
        _settings.base_urls[spec.key] = base
        if key:
            _settings.api_keys[spec.key] = key
        core.save_settings(_settings)
        return creds

    if spec.keyless:  # e.g. Ollama — allow overriding the host but no key.
        if spec.key == "ollama":
            creds.base_url = (
                os.getenv("OLLAMA_HOST") or _settings.base_urls.get(spec.key) or spec.base_url
            )
        return creds

    key = os.getenv(spec.api_key_env or "") or _settings.api_keys.get(spec.key)
    if not key:
        key = Prompt.ask(f"Enter API key for {spec.label}", password=True)
        _settings.api_keys[spec.key] = key
        core.save_settings(_settings)
        console.print("[dim]Saved to ~/.config/sentinel/config.json (chmod 600) - won't ask again.[/]")
    creds.api_key = key
    return creds


def build_engine(spec: core.ProviderSpec, model: str | None = None) -> core.Engine:
    """Resolve credentials interactively, then construct the engine."""
    creds = resolve_credentials(spec)
    return core.create_engine(spec, model, creds.api_key, creds.base_url)


def select_provider() -> core.ProviderSpec:
    """Render the provider menu and return the chosen spec."""
    specs = list(core.PROVIDERS.values())

    table = Table(title="Choose a provider", title_style=f"bold {ACCENT}", expand=False)
    table.add_column("#", justify="right", style="cyan")
    table.add_column("Provider", style="bold")
    table.add_column("Status")
    table.add_column("Notes", style="dim")
    for index, spec in enumerate(specs, start=1):
        status = "[green]ready[/]" if core.provider_is_ready(spec) else "[yellow]needs setup[/]"
        table.add_row(str(index), spec.label, status, spec.notes)
    console.print(table)

    choices = [str(i) for i in range(1, len(specs) + 1)]
    pick = Prompt.ask("Select provider", choices=choices, default="1")
    return specs[int(pick) - 1]


def select_model(engine: core.Engine) -> None:
    """Interactively choose the model for an engine (curated list + live refresh)."""
    candidates = list(engine.spec.models)
    refreshed = False

    while True:
        if candidates:
            table = Table(
                title=f"{engine.label} — models",
                caption=("live catalog" if refreshed
                         else f"curated defaults (updated {core.CURATED_MODELS_UPDATED})"),
                title_style=f"bold {ACCENT}",
                expand=False,
            )
            table.add_column("#", justify="right", style="cyan")
            table.add_column("Model", style="bold")
            for index, name in enumerate(candidates, start=1):
                marker = " [green](current)[/]" if name == engine.model else ""
                table.add_row(str(index), f"{name}{marker}")
            console.print(table)
        else:
            console.print("[yellow]No curated models for this provider.[/]")

        console.print(
            "[dim]Enter a number, type a model ID, 'r' to refresh from the "
            f"provider, or press Enter to keep [/][cyan]{engine.model or '(none)'}[/][dim].[/]"
        )
        answer = Prompt.ask("Model", default="").strip()

        if not answer:
            if engine.model:
                return
            console.print("[yellow]A model is required — pick one.[/]")
            continue
        if answer.lower() == "r":
            try:
                with console.status("[cyan]Fetching live model list…[/]", spinner="dots"):
                    candidates = engine.list_models()
                refreshed = True
                if not candidates:
                    console.print("[yellow]Provider returned no models.[/]")
            except core.TranslationError as exc:
                console.print(f"[red]Could not refresh models:[/] {exc}")
            continue
        if answer.isdigit():
            idx = int(answer)
            if 1 <= idx <= len(candidates):
                engine.model = candidates[idx - 1]
                console.print(f"[green]Model set to[/] [bold]{engine.model}[/].")
                return
            console.print("[yellow]Number out of range.[/]")
            continue
        engine.model = answer  # treat any other input as a literal model ID
        console.print(f"[green]Model set to[/] [bold]{engine.model}[/].")
        return


# ---------------------------------------------------------------------------
# Onboarding
# ---------------------------------------------------------------------------

def run_onboarding() -> core.UserProfile:
    """Ask the first-run questions, persist the answers, and return the profile."""
    console.print(
        Panel(
            Text.from_markup(
                "[bold]Welcome![/] A couple of quick questions so I can explain "
                "commands at the right level for you.\n"
                "[dim]You can change these anytime with the[/] [cyan]profile[/] "
                "[dim]command.[/]"
            ),
            title="First-time setup",
            title_align="left",
            border_style=ACCENT,
        )
    )

    table = Table(
        title="Your Linux / command-line experience",
        title_style=f"bold {ACCENT}",
        expand=False,
    )
    table.add_column("#", justify="right", style="cyan")
    table.add_column("Level", style="bold")
    table.add_column("Means", style="dim")
    for index, level in enumerate(core.EXPERIENCE_LEVELS, start=1):
        table.add_row(str(index), level.capitalize(), core.EXPERIENCE_BLURB[level])
    console.print(table)

    choices = [str(i) for i in range(1, len(core.EXPERIENCE_LEVELS) + 1)]
    pick = Prompt.ask("Pick the closest match", choices=choices, default="2")
    experience = core.EXPERIENCE_LEVELS[int(pick) - 1]

    default_explain = experience != "expert"
    explain = (
        Prompt.ask(
            "Show a plain-English explanation of each command before running it?",
            choices=["y", "n"],
            default="y" if default_explain else "n",
        )
        == "y"
    )

    profile = core.UserProfile(experience=experience, explain=explain)
    if not core.save_profile(profile):
        console.print("[yellow]Couldn't save your profile (it won't persist).[/]")
    console.print(
        f"[green]Got it.[/] [dim]Experience: {experience}; "
        f"explanations {'on' if explain else 'off'}.[/]"
    )
    return profile


def get_or_create_profile() -> core.UserProfile:
    """Return the saved profile, running first-time onboarding if none exists."""
    existing = core.load_profile()
    return existing if existing is not None else run_onboarding()


# ---------------------------------------------------------------------------
# Ask / chat mode
# ---------------------------------------------------------------------------

def ask_once(engine: core.Engine, profile: core.UserProfile, question: str) -> None:
    """Answer a single inline question (e.g. ``ask how do permissions work``)."""
    try:
        answer, cancelled = run_with_cancel(
            lambda: engine.ask([{"role": "user", "content": question}], profile),
            "[cyan]Thinking…[/]",
        )
    except core.TranslationError as exc:
        console.print(f"[bold red]Couldn't answer:[/] {exc}")
        return
    if cancelled:
        console.print("[dim]Cancelled.[/]")
        return
    _print_answer(engine, answer)


def run_chat(engine: core.Engine, profile: core.UserProfile) -> None:
    """Enter a multi-turn Q&A session about Linux and related topics."""
    console.print(
        Panel(
            Text.from_markup(
                "[bold]Chat mode[/] — ask anything about Linux, the shell, or "
                "system administration.\n"
                "[dim]Answers are informational; nothing is executed. Type[/] "
                "[cyan]back[/] [dim]to return to the command prompt.[/]"
            ),
            title="💬 Ask the assistant",
            border_style="blue",
            title_align="left",
        )
    )

    history: list[dict[str, str]] = []
    while True:
        try:
            question = Prompt.ask("[bold blue]ask[/]").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]Leaving chat.[/]")
            return
        if not question:
            continue
        if question.lower() in {"back", "exit", "quit"}:
            console.print("[dim]Leaving chat.[/]")
            return

        history.append({"role": "user", "content": question})
        try:
            answer, cancelled = run_with_cancel(
                lambda: engine.ask(history, profile), "[cyan]Thinking…[/]"
            )
        except core.TranslationError as exc:
            console.print(f"[bold red]Couldn't answer:[/] {exc}")
            history.pop()
            continue
        if cancelled:
            history.pop()
            console.print("[dim]Cancelled.[/]")
            continue

        history.append({"role": "assistant", "content": answer})
        _print_answer(engine, answer)
        if len(history) > 20:  # bound context so a long session can't grow cost
            history = history[-20:]


# ---------------------------------------------------------------------------
# Welcome box & help
# ---------------------------------------------------------------------------

def _print_banner(engine: core.Engine, profile: core.UserProfile) -> None:
    """Render the welcome box: owl mark + live status, tips alongside."""
    user = os.getenv("USER") or os.getenv("USERNAME") or "there"
    cwd = os.getcwd()
    explanations = "on" if profile.explain else "off"

    left = Table.grid(padding=(0, 0))
    left.add_column(justify="center")
    left.add_row(Text(_MASCOT.strip("\n"), style=ACCENT))
    left.add_row("")
    left.add_row(Text.from_markup(f"[bold]Welcome back, {user}![/]"))
    left.add_row(Text.from_markup(f"[{ACCENT}]{engine.label}[/] [dim]·[/] {engine.model}"))
    left.add_row(Text.from_markup("[green]●[/] [dim]engine ready[/]"))
    left.add_row(Text.from_markup(
        f"[dim]profile: {profile.experience} · explanations {explanations}[/]"
    ))
    left.add_row(Text(cwd, style="dim"))

    right = Table.grid(padding=(0, 0))
    right.add_column()
    right.add_row(Text.from_markup(f"[bold {ACCENT}]Getting started[/]"))
    right.add_row(Text.from_markup("Describe a task in plain English, e.g."))
    right.add_row(Text.from_markup("  [cyan]\"how much free disk space?\"[/]"))
    right.add_row(Text.from_markup("  [cyan]\"show the last 50 syslog lines\"[/]"))
    right.add_row("")
    right.add_row(Text.from_markup(f"[bold {ACCENT}]Commands[/]"))
    right.add_row(Text.from_markup("[cyan]ask[/]       ask a Linux question"))
    right.add_row(Text.from_markup("[cyan]provider[/]  switch AI provider"))
    right.add_row(Text.from_markup("[cyan]model[/]     pick / refresh model"))
    right.add_row(Text.from_markup("[cyan]profile[/]   tune explanation level"))
    right.add_row(Text.from_markup("[cyan]help[/] · [cyan]exit[/]"))
    right.add_row("")
    right.add_row(Text.from_markup("[yellow]⚠  Nothing runs without your y/n approval.[/]"))

    columns = Table.grid(padding=(0, 5))
    columns.add_column()
    columns.add_column()
    columns.add_row(left, right)

    console.print(
        Panel(
            columns,
            title="[bold]Sentinel[/] [dim]v1 · natural-language Linux server manager[/]",
            title_align="left",
            border_style=ACCENT,
            padding=(1, 2),
        )
    )


def _print_help() -> None:
    """Print the in-loop command reference."""
    console.print(
        Panel(
            Text.from_markup(
                "[bold]Type a request[/] in plain English to translate + run a command.\n\n"
                "[cyan]ask[/] <q>   Ask a Linux question (or just [cyan]ask[/] / "
                "[cyan]chat[/] for a back-and-forth) — answers only, nothing runs\n"
                "[cyan]provider[/]  Switch AI provider (Anthropic, OpenAI, Gemini, "
                "OpenRouter, Ollama, Custom)\n"
                "[cyan]model[/]     Pick a model (curated list, or 'r' to refresh live)\n"
                "[cyan]profile[/]   Re-take the experience questionnaire (sets how "
                "commands are explained)\n"
                "[cyan]help[/]      Show this help\n"
                "[cyan]exit[/]      Quit (Ctrl-D also works)\n\n"
                "[dim]Tip: a leading slash is optional — [/][cyan]/ask[/][dim] works too.[/]"
            ),
            title="Help",
            border_style=ACCENT,
            title_align="left",
        )
    )


# ---------------------------------------------------------------------------
# Bootstrapping & CLI loop
# ---------------------------------------------------------------------------

def _startup_engine() -> core.Engine:
    """Resolve the initial engine.

    Precedence: SENTINEL_PROVIDER / SENTINEL_MODEL env vars, then the remembered
    provider/model from the last session, then the interactive menus. Once a
    provider, model, and key are known, subsequent launches skip the menus
    entirely and go straight to the prompt.
    """
    preset_provider = (os.getenv("SENTINEL_PROVIDER", "").strip().lower()
                       or (_settings.provider or "")).strip().lower()
    preset_model = (os.getenv("SENTINEL_MODEL", "").strip() or _settings.model or None)

    try:
        if preset_provider in core.PROVIDERS:
            engine = build_engine(core.PROVIDERS[preset_provider], preset_model)
            if not engine.model:
                select_model(engine)
            _remember(engine)
            return engine
        spec = select_provider()
        engine = build_engine(spec)
        select_model(engine)
        _remember(engine)
        return engine
    except core.TranslationError as exc:
        console.print(f"[bold red]Could not initialize provider:[/] {exc}")
        sys.exit(1)
    except (EOFError, KeyboardInterrupt):
        console.print("\n[dim]Goodbye.[/]")
        sys.exit(0)


def main() -> None:
    """Run the interactive read-translate-confirm-execute loop."""
    # Ensure the owl and box-drawing glyphs render on any console, including
    # legacy Windows code pages. No-op where stdout is already UTF-8 (e.g. WSL).
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    load_dotenv()  # Reads .env into the environment if present.

    global _settings
    _settings = core.load_settings()  # remembered provider/model/keys

    try:
        profile = get_or_create_profile()
    except (EOFError, KeyboardInterrupt):
        console.print("\n[dim]Goodbye.[/]")
        return

    engine = _startup_engine()
    _print_banner(engine, profile)

    while True:
        try:
            request = Prompt.ask(
                f"\n[bold cyan]>[/] [dim]({engine.spec.key}:{engine.model})[/]"
            ).strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]Goodbye.[/]")
            break

        if not request:
            continue

        # A leading slash is optional on commands (so /ask, /help also work).
        if request.startswith("/"):
            request = request[1:].lstrip()
            if not request:
                continue

        command_word = request.lower()
        first_word = command_word.split(maxsplit=1)[0]
        if command_word in {"exit", "quit"}:
            console.print("[dim]Goodbye.[/]")
            break
        if command_word == "help":
            _print_help()
            continue
        if first_word in {"ask", "chat"}:
            parts = request.split(maxsplit=1)
            inline_question = parts[1].strip() if len(parts) > 1 else ""
            if inline_question:
                ask_once(engine, profile, inline_question)
            else:
                run_chat(engine, profile)
            continue
        if command_word == "provider":
            try:
                engine = build_engine(select_provider())
                select_model(engine)
                _remember(engine)
            except core.TranslationError as exc:
                console.print(f"[red]Could not switch provider:[/] {exc}")
            except (EOFError, KeyboardInterrupt):
                console.print("\n[dim]Provider change cancelled.[/]")
            continue
        if command_word == "model":
            try:
                select_model(engine)
                _remember(engine)
            except (EOFError, KeyboardInterrupt):
                console.print("\n[dim]Model change cancelled.[/]")
            continue
        if command_word == "profile":
            try:
                profile = run_onboarding()
            except (EOFError, KeyboardInterrupt):
                console.print("\n[dim]Profile unchanged.[/]")
            continue

        # --- Translate --------------------------------------------------------
        try:
            command, cancelled = run_with_cancel(
                lambda: engine.translate(request), "[cyan]Translating…[/]"
            )
        except core.TranslationError as exc:
            console.print(f"[bold red]Translation failed:[/] {exc}")
            continue
        if cancelled:
            console.print("[dim]Cancelled.[/]")
            continue

        if not command or command == core.UNSUPPORTED_SENTINEL:
            console.print(
                "[yellow]That isn't a Linux operation I can run.[/] "
                "[dim]I translate plain-English system tasks into shell "
                'commands — try e.g. [/][cyan]"how much disk is free?"[/] '
                "[dim]or[/] [cyan]\"list running processes\"[/][dim].[/]"
            )
            continue

        # --- Safety filter (defense in depth, before any prompt) --------------
        verdict = core.screen_command(command)
        if not verdict.allowed:
            console.print(
                Panel(
                    Text.from_markup(
                        f"[bold]Blocked command:[/]\n  [red]{command}[/]\n\n"
                        f"[bold]Reason:[/] {verdict.reason}"
                    ),
                    title="[bold red]🛑 Refused by safety filter[/]",
                    border_style="red",
                    title_align="left",
                )
            )
            continue

        # --- Optional, profile-aware explanation (best-effort) ----------------
        explanation = ""
        if profile.explain:
            try:
                explanation, cancelled = run_with_cancel(
                    lambda: engine.explain(command, profile), "[cyan]Explaining…[/]"
                )
            except core.TranslationError:
                explanation = ""
            else:
                if cancelled:
                    console.print("[dim]Cancelled.[/]")
                    continue

        # --- Human confirmation gate — the heart of the safety model ----------
        if not confirm_execution(command, explanation):
            console.print("[dim]Skipped — nothing was run.[/]")
            continue

        # Execution is cancellable: Esc terminates the running command.
        cancel_event = threading.Event()
        result, cancelled = run_with_cancel(
            lambda: core.run_command(command, cancel_event),
            "[cyan]Running…[/]",
            cancel_event=cancel_event,
        )
        _render_result(result)
        if cancelled:
            console.print("[yellow]Cancelled — the command was stopped.[/]")
            continue

        # --- Plain-English answer derived from the output (best-effort) -------
        if profile.explain:
            try:
                summary, cancelled = run_with_cancel(
                    lambda: engine.summarize(request, command, result, profile),
                    "[cyan]Summarizing result…[/]",
                )
                if summary and not cancelled:
                    _print_summary(summary)
            except core.TranslationError:
                pass


if __name__ == "__main__":
    main()
