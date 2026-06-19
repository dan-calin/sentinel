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
import re
import select
import shutil
import subprocess
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


def confirm_execution(command: str, explanation: str = "", target: str = core.LOCAL_HOST) -> bool:
    """Display the command (and optional explanation) and require an explicit ``y``.

    This is the non-negotiable safety gate: returns ``True`` only when the user
    types ``y``. Anything else (including a bare Enter) is a refusal. ``target``
    is shown prominently so it's always clear *where* the command will run.
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
    local = target == core.LOCAL_HOST
    where = "this machine (local)" if local else target
    border = "yellow" if local else "magenta"
    console.print(
        Panel(
            Syntax(command, "bash", theme="ansi_dark", word_wrap=True),
            title=f"[bold {border}]Proposed command — runs on: {where}[/]",
            subtitle=f"[dim]This will run on {where}[/]",
            border_style=border,
            title_align="left",
        )
    )
    extra = "" if local else f" on [bold magenta]{target}[/]"
    console.print(f"[bold yellow]⚠  Nothing runs until you approve it[/]{extra}.")
    return Prompt.ask(f"Execute this command on {where}?", choices=["y", "n"], default="n") == "y"


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
# Image attachments
#
# Users attach an image by including its path in the prompt (typing it, or just
# dragging the file into the terminal). We pull recognized image paths out of
# the text, load them, and pass them to the model as extra context — so
# "why is this failing? ~/err.png" sends both the question and the screenshot.
# ---------------------------------------------------------------------------

# Image-path token: a double/single-quoted path, or a bare path (allowing
# backslash-escaped spaces), ending in a supported image extension.
_IMG_EXT = "|".join(sorted(ext.lstrip(".") for ext in core.SUPPORTED_IMAGE_TYPES))
_IMG_PATH_RE = re.compile(
    rf'"([^"]+?\.(?:{_IMG_EXT}))"'
    rf"|'([^']+?\.(?:{_IMG_EXT}))'"
    rf'|((?:\\ |\S)+?\.(?:{_IMG_EXT}))(?=\s|$)',
    re.IGNORECASE,
)


def extract_images(text: str, start_index: int = 0):
    """Split a prompt into (clean_text, images, notes).

    ``images`` are successfully loaded :class:`core.ImageAttachment` objects.
    Each loaded image's path is replaced in ``clean_text`` with a tidy
    ``[Image #N]`` placeholder (so a pasted, GUID-laden file path becomes a
    clean reference the model can correlate with the attachment). A token that
    looks like an image path but can't be loaded is left in the text and
    reported via ``notes`` (``("ok"|"err", message)``), so a stray ".png" word
    is never silently swallowed.

    ``start_index`` is how many images were already attached (e.g. live in the
    paste-aware editor), so placeholder numbering continues from there instead
    of restarting at #1.
    """
    matches = list(_IMG_PATH_RE.finditer(text))
    if not matches:
        return text, [], []

    images, notes, replacements = [], [], []
    for match in matches:
        path = (match.group(1) or match.group(2) or match.group(3)).replace("\\ ", " ")
        try:
            images.append(core.load_image(path))
            label = f"Image #{start_index + len(images)}"
            notes.append(("ok", label))
            replacements.append((match.span(), f"[{label}]"))
        except core.ImageError as exc:
            notes.append(("err", f"{path}: {exc}"))

    clean = text
    for (start, end), placeholder in sorted(replacements, reverse=True):  # back-to-front
        clean = clean[:start] + placeholder + clean[end:]
    clean = re.sub(r"\s{2,}", " ", clean).strip()
    return clean, images, notes


# Matches the placeholders inserted by extract_images, for "is there any real
# instruction left?" checks.
_PLACEHOLDER_RE = re.compile(r"\[Image #\d+\]")


def _only_placeholders(text: str) -> bool:
    """True if ``text`` is nothing but image placeholders / whitespace."""
    return not _PLACEHOLDER_RE.sub("", text).strip()


def _report_image_notes(notes) -> None:
    """Print a short line per attached (or skipped) image."""
    for kind, message in notes:
        if kind == "ok":
            console.print(f"[dim]Attached {message}[/]")
        else:
            console.print(f"[yellow]Skipped image[/] [dim]({message})[/]")


def _vision_hint(images, error: str = "") -> str:
    """Markup hint to append on failure when images were attached."""
    if not images:
        return ""
    low = error.lower()
    if "image input" in low or "no endpoints" in low or "support image" in low:
        # The model may well be vision-capable elsewhere; the gateway just isn't
        # routing this request to an image-capable endpoint.
        return (
            "\n[dim]The provider returned no image-capable endpoint for this "
            "model. Choose a vision model with [/][cyan]model[/][dim], or connect "
            "the vision endpoint directly via [/][cyan]provider[/][dim] → Custom.[/]"
        )
    return (
        "\n[dim]If the selected model can't read images, switch to a "
        "vision-capable one (Claude, GPT, Gemini) or omit the image.[/]"
    )


# ---------------------------------------------------------------------------
# Vision bridge
#
# When the active model is text-only but an image is attached, we route the
# image to a vision-capable model on the SAME provider (Gemini by default on
# OpenRouter), get a transcription + description, and feed that text to the
# primary model. This mirrors a multimodal "fallback" pipeline: the primary
# model never sees the image, it reads the vision model's words.
# ---------------------------------------------------------------------------

# Error fragments that mean "this model/endpoint won't accept the image".
_IMAGE_ERROR_MARKERS = (
    "image input", "no endpoints", "support image", "image_url",
    "multimodal", "does not support image", "cannot process image",
)


def _is_image_error(error: str) -> bool:
    """Whether a provider error looks like an image-not-supported failure."""
    low = error.lower()
    return any(marker in low for marker in _IMAGE_ERROR_MARKERS)


# Stored in settings.vision_model to turn the bridge off entirely.
_VISION_OFF = "off"


def _bridge_model(provider_key: str) -> str | None:
    """The vision-bridge model for a provider (env / saved override, else default).

    Returns ``None`` when the user has disabled the bridge, or no model applies.
    """
    override = os.getenv("SENTINEL_VISION_MODEL") or _settings.vision_model
    if override:
        return None if override.lower() == _VISION_OFF else override
    return core.DEFAULT_VISION_MODELS.get(provider_key)


def _build_bridge_engine(engine: core.Engine, model: str) -> core.Engine:
    """Build a sibling engine on the same provider/credentials, different model."""
    creds = _creds_for(engine.spec)
    return core.create_engine(engine.spec, model, creds.api_key, creds.base_url)


def _augment_with_description(text: str, description: str) -> str:
    """Fold a vision model's transcription into a text-only request."""
    stripped = _PLACEHOLDER_RE.sub("", text).strip()
    base = stripped or "(respond based on the attached image)"
    return (
        f"{base}\n\n[Transcription and description of the attached image(s), "
        f"provided by a vision model]:\n{description}"
    )


def _bridge_request(engine: core.Engine, text: str, images, error: str) -> str | None:
    """Describe images via a vision bridge and return augmented (text-only) input.

    Returns ``None`` when bridging doesn't apply (not an image error, no bridge
    model, bridge == primary) or fails/cancels, so the caller falls back to its
    normal error handling.
    """
    if not (images and _is_image_error(error)):
        return None
    bridge_model = _bridge_model(engine.spec.key)
    if not bridge_model or bridge_model == engine.model:
        return None
    try:
        bridge = _build_bridge_engine(engine, bridge_model)
        description, cancelled = run_with_cancel(
            lambda: bridge.describe_images(text, images),
            f"[cyan]{engine.model} can't see images — reading them with {bridge_model}…[/]",
        )
    except core.TranslationError:
        return None
    if cancelled or not description:
        return None
    console.print(
        f"[dim]Read the image(s) with {bridge_model}, then handed the text to "
        f"{engine.model}.[/]"
    )
    return _augment_with_description(text, description)


# ---------------------------------------------------------------------------
# Paste-aware line editor
#
# A small raw-mode line reader (used in place of rich's Prompt on an
# interactive TTY) that turns on the terminal's *bracketed paste* mode. When
# you paste a screenshot — Windows Terminal pastes the file path; we also grab a
# real clipboard image on Ctrl-V — it inserts a clean [Image #N] token in the
# line instead of a long path, and stashes the image to send as context. This is
# how a tool like Claude Code shows [Image #N] inline as you type.
#
# Anywhere this can't run (no TTY, native Windows without termios) it falls back
# to rich's Prompt, and image paths are still recognized after you press Enter.
# ---------------------------------------------------------------------------

# A console forced into terminal mode so we can render the prompt's markup to
# ANSI and redraw it ourselves on each keystroke.
_ansi_console = Console(force_terminal=True, color_system="truecolor")


def _render_markup(markup: str) -> str:
    """Render rich markup to a raw ANSI string for manual redrawing."""
    with _ansi_console.capture() as cap:
        _ansi_console.print(markup, end="")
    return cap.get()


# Bytes read from the fd but not yet consumed. We read straight from the file
# descriptor with os.read (not sys.stdin.read) so that select() — which peeks at
# the fd — and our reads agree. Reading via sys.stdin buffers inside Python,
# leaving select() blind to bytes already pulled off the fd, which made multi-
# byte sequences like arrow keys (ESC [ C) leak in as the literal text "[C".
_input_buf = b""


def _read_raw_byte(timeout: float | None = None) -> int | None:
    """Read one byte from stdin's fd. ``None`` on timeout or EOF.

    ``timeout=None`` blocks; ``timeout=0`` is a non-blocking peek-and-read.
    """
    global _input_buf
    if not _input_buf:
        if timeout is not None and not select.select([sys.stdin], [], [], timeout)[0]:
            return None
        chunk = os.read(sys.stdin.fileno(), 256)
        if not chunk:
            return None
        _input_buf = chunk
    byte, _input_buf = _input_buf[0], _input_buf[1:]
    return byte


def _read_paste_body() -> str:
    """Read a bracketed-paste payload up to the ``ESC[201~`` terminator."""
    data = b""
    while not data.endswith(b"\x1b[201~"):
        byte = _read_raw_byte()
        if byte is None:
            break
        data += bytes([byte])
    if data.endswith(b"\x1b[201~"):
        data = data[:-6]
    text = data.decode("utf-8", errors="replace")
    # A pasted path/command is one logical line; fold newlines to spaces.
    return text.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")


def _read_key():
    """Read one logical key press, returning a small ``(kind, …)`` token."""
    byte = _read_raw_byte()
    if byte is None:
        return ("eof",)
    if byte == 0x03:
        return ("interrupt",)
    if byte == 0x04:
        return ("eof",)
    if byte == 0x16:  # Ctrl-V → attach clipboard image
        return ("paste-image",)
    if byte in (0x0D, 0x0A):
        return ("enter",)
    if byte in (0x7F, 0x08):
        return ("backspace",)
    if byte == 0x01:  # Ctrl-A
        return ("home",)
    if byte == 0x05:  # Ctrl-E
        return ("end",)
    if byte == 0x1B:  # ESC — maybe a CSI sequence (arrows, home/end, paste)
        if _read_raw_byte(0.05) != 0x5B:  # next must be '['; else lone Esc / Alt-key
            return ("escape",)
        seq = b""
        while True:
            nxt = _read_raw_byte(0.05)
            if nxt is None:
                break
            seq += bytes([nxt])
            if 0x40 <= nxt <= 0x7E:  # final byte of a CSI sequence
                break
        if seq == b"200~":
            return ("paste", _read_paste_body())
        return {
            b"C": ("right",), b"D": ("left",), b"A": ("up",), b"B": ("down",),
            b"H": ("home",), b"F": ("end",), b"1~": ("home",), b"4~": ("end",),
            b"3~": ("delete",),
        }.get(seq, ("ignore",))
    if byte < 0x20:
        return ("ignore",)  # other control characters
    if byte < 0x80:
        return ("char", chr(byte))
    # UTF-8 multibyte: read the continuation bytes for this character.
    length = 2 if byte < 0xE0 else 3 if byte < 0xF0 else 4
    raw = bytes([byte])
    for _ in range(length - 1):
        nxt = _read_raw_byte(0.05)
        if nxt is None:
            break
        raw += bytes([nxt])
    try:
        return ("char", raw.decode("utf-8"))
    except UnicodeDecodeError:
        return ("ignore",)


def _grab_clipboard_image() -> "core.ImageAttachment | None":
    """Save a clipboard image to a temp PNG via PowerShell (WSL) and load it."""
    powershell = shutil.which("powershell.exe")
    if not powershell:
        return None
    script = (
        "Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
        "$i=[Windows.Forms.Clipboard]::GetImage();"
        "if($i){$p=[IO.Path]::ChangeExtension([IO.Path]::GetTempFileName(),'png');"
        "$i.Save($p,[Drawing.Imaging.ImageFormat]::Png);[Console]::Out.Write($p)}"
    )
    try:
        done = subprocess.run(
            [powershell, "-NoProfile", "-Command", script],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    path = done.stdout.strip()
    if not path:
        return None
    try:
        return core.load_image(path)
    except core.ImageError:
        return None


def _insert_paste(pasted: str, images: list) -> str:
    """Turn a pasted string into text to insert, attaching any image paths.

    Recognized, loadable image paths become ``[Image #N]`` (numbering continues
    from ``images``); everything else is inserted literally.
    """
    out, last = [], 0
    for match in _IMG_PATH_RE.finditer(pasted):
        path = (match.group(1) or match.group(2) or match.group(3)).replace("\\ ", " ")
        try:
            attachment = core.load_image(path)
        except core.ImageError:
            continue  # not a real image — leave it as literal text
        out.append(pasted[last:match.start()])
        images.append(attachment)
        out.append(f"[Image #{len(images)}]")
        last = match.end()
    out.append(pasted[last:])
    return "".join(out)


def read_prompt(prompt: str):
    """Read a line, returning ``(text, images)``.

    On an interactive TTY this is a paste-aware editor with live ``[Image #N]``
    placeholders. Otherwise it falls back to rich's Prompt and returns no images
    (the caller still scans the text for typed paths afterward).

    Raises:
        EOFError / KeyboardInterrupt: like ``input()``.
    """
    if not (_TERMIOS_OK and sys.stdin.isatty()):
        return Prompt.ask(prompt), []

    # Print any leading blank lines once; they must not be part of the redraw.
    lead, body = "", prompt
    while body.startswith("\n"):
        lead += "\n"
        body = body[1:]
    sys.stdout.write(lead)
    prompt_ansi = _render_markup(body) + " "

    buf: list[str] = []
    pos = 0
    images: list = []

    def redraw() -> None:
        out = "\r" + prompt_ansi + "".join(buf) + "\x1b[K"
        if (back := len(buf) - pos) > 0:
            out += f"\x1b[{back}D"
        sys.stdout.write(out)
        sys.stdout.flush()

    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        sys.stdout.write("\x1b[?2004h")  # enable bracketed paste
        redraw()
        while True:
            key = _read_key()
            kind = key[0]
            if kind == "enter":
                break
            if kind == "interrupt":
                raise KeyboardInterrupt
            if kind == "eof":
                if not buf:
                    raise EOFError
                continue
            if kind == "char":
                buf.insert(pos, key[1]); pos += 1
            elif kind == "backspace":
                if pos > 0:
                    del buf[pos - 1]; pos -= 1
            elif kind == "delete":
                if pos < len(buf):
                    del buf[pos]
            elif kind == "left":
                pos = max(0, pos - 1)
            elif kind == "right":
                pos = min(len(buf), pos + 1)
            elif kind == "home":
                pos = 0
            elif kind == "end":
                pos = len(buf)
            elif kind == "paste":
                inserted = _insert_paste(key[1], images)
                buf[pos:pos] = list(inserted); pos += len(inserted)
            elif kind == "paste-image":
                attachment = _grab_clipboard_image()
                if attachment is None:
                    sys.stdout.write("\a")  # nothing usable on the clipboard
                else:
                    images.append(attachment)
                    token = f"[Image #{len(images)}]"
                    buf[pos:pos] = list(token); pos += len(token)
            redraw()
        pos = len(buf)
        redraw()
        sys.stdout.write("\n")
        sys.stdout.flush()
    finally:
        sys.stdout.write("\x1b[?2004l")  # disable bracketed paste
        sys.stdout.flush()
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)

    return "".join(buf), images


def read_request(prompt: str):
    """Read input and return ``(clean_text, images)``.

    Wraps :func:`read_prompt` and additionally scans the final text for *typed*
    (non-pasted) image paths, continuing the same ``[Image #N]`` numbering.
    """
    text, images = read_prompt(prompt)
    clean, typed, notes = extract_images(text, start_index=len(images))
    _report_image_notes(notes)  # pasted images already showed as a live token
    return clean.strip(), images + typed


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


def _creds_for(spec: core.ProviderSpec) -> core.Credentials:
    """Resolve a provider's credentials from env + saved settings (no prompting).

    Used to build sibling engines (e.g. the vision bridge) without re-asking for
    anything; the primary engine's key already came from the same sources.
    """
    base = spec.base_url
    if spec.runtime_config:
        base = os.getenv("CUSTOM_BASE_URL") or _settings.base_urls.get(spec.key) or base
    elif spec.key == "ollama":
        base = os.getenv("OLLAMA_HOST") or _settings.base_urls.get(spec.key) or base
    key = ""
    if spec.api_key_env:
        key = os.getenv(spec.api_key_env) or _settings.api_keys.get(spec.key, "")
    elif spec.runtime_config:
        key = os.getenv("CUSTOM_API_KEY") or _settings.api_keys.get(spec.key, "")
    return core.Credentials(api_key=key, base_url=base)


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


def _set_vision_model(value: str | None, label: str) -> None:
    """Persist the vision-fallback choice and confirm."""
    _settings.vision_model = value
    core.save_settings(_settings)
    console.print(f"[green]Vision fallback {label}.[/]")


def select_vision_model(engine: core.Engine) -> None:
    """Set the vision 'fallback' model used when the main model can't see images."""
    provider_default = core.DEFAULT_VISION_MODELS.get(engine.spec.key)
    console.print(
        Panel(
            Text.from_markup(
                "[bold]Vision fallback[/] — when your main model can't read an "
                "attached image, Sentinel routes it to this model (on the same "
                "provider) to transcribe and describe it, then hands that text to "
                "your main model.\n"
                f"[dim]Active model: [/][cyan]{engine.model}[/][dim]; provider "
                f"default fallback: [/][cyan]{provider_default or '(none)'}[/][dim].[/]"
            ),
            title="Vision fallback model",
            border_style=ACCENT,
            title_align="left",
        )
    )

    candidates: list[tuple[str, bool]] = []
    while True:
        if candidates:
            table = Table(
                title=f"{engine.label} — vision-capable models",
                caption="free first; type an ID for any other",
                title_style=f"bold {ACCENT}",
                expand=False,
            )
            table.add_column("#", justify="right", style="cyan")
            table.add_column("Model", style="bold")
            table.add_column("Cost")
            for index, (name, is_free) in enumerate(candidates, start=1):
                cost = "[green]free[/]" if is_free else "[dim]paid[/]"
                table.add_row(str(index), name, cost)
            console.print(table)

        console.print(
            f"[dim]Currently using: [/][cyan]{_bridge_model(engine.spec.key) or '(disabled)'}[/]\n"
            "[dim]Enter a model ID or number, [/][cyan]l[/][dim] to list vision "
            "models, [/][cyan]free[/][dim] for free ones only, [/][cyan]default[/]"
            "[dim], [/][cyan]off[/][dim], or Enter to keep.[/]"
        )
        answer = Prompt.ask("Vision fallback model", default="").strip()

        if not answer:
            return
        low = answer.lower()
        if low in {"l", "list", "free"}:
            try:
                with console.status("[cyan]Fetching vision-capable models…[/]", spinner="dots"):
                    candidates = engine.list_vision_models(free_only=(low == "free"))
                if not candidates:
                    console.print(
                        "[yellow]This provider doesn't expose image-capability info — "
                        "type a model ID directly.[/]"
                    )
            except core.TranslationError as exc:
                console.print(f"[red]Could not fetch models:[/] {exc}")
            continue
        if low == "default":
            _set_vision_model(None, f"using the provider default ({provider_default or 'none'})")
            return
        if low in {"off", "none", "disable"}:
            _set_vision_model(_VISION_OFF, "disabled")
            return
        if answer.isdigit() and candidates:
            idx = int(answer)
            if 1 <= idx <= len(candidates):
                _set_vision_model(candidates[idx - 1][0], f"set to {candidates[idx - 1][0]}")
                return
            console.print("[yellow]Number out of range.[/]")
            continue
        _set_vision_model(answer, f"set to {answer}")
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

def ask_once(
    engine: core.Engine, profile: core.UserProfile, question: str, images=None
) -> None:
    """Answer a single inline question (e.g. ``ask how do permissions work``).

    ``question`` may already contain ``[Image #N]`` placeholders, with the
    matching ``images`` attached by the caller's editor.
    """
    images = images or []
    if images and _only_placeholders(question):
        question = "Describe the attached image(s) and answer based on them."
    try:
        answer, cancelled = run_with_cancel(
            lambda: engine.ask([core.user_message(question, images)], profile),
            "[cyan]Thinking…[/]",
        )
    except core.TranslationError as exc:
        augmented = _bridge_request(engine, question, images, str(exc))
        if augmented is None:
            console.print(f"[bold red]Couldn't answer:[/] {exc}{_vision_hint(images, str(exc))}")
            return
        try:
            answer, cancelled = run_with_cancel(
                lambda: engine.ask([core.user_message(augmented, [])], profile),
                "[cyan]Thinking…[/]",
            )
        except core.TranslationError as exc2:
            console.print(f"[bold red]Couldn't answer:[/] {exc2}")
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

    history: list[dict] = []
    while True:
        try:
            question, images = read_request("[bold blue]ask[/]")
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]Leaving chat.[/]")
            return
        if not question and not images:
            continue
        if question.lower() in {"back", "exit", "quit"}:
            console.print("[dim]Leaving chat.[/]")
            return

        if images and _only_placeholders(question):
            question = "Describe the attached image(s) and answer based on them."

        history.append(core.user_message(question, images))
        try:
            answer, cancelled = run_with_cancel(
                lambda: engine.ask(history, profile), "[cyan]Thinking…[/]"
            )
        except core.TranslationError as exc:
            augmented = _bridge_request(engine, question, images, str(exc))
            if augmented is None:
                console.print(f"[bold red]Couldn't answer:[/] {exc}{_vision_hint(images, str(exc))}")
                history.pop()
                continue
            history[-1] = core.user_message(augmented, [])  # swap image turn for text
            try:
                answer, cancelled = run_with_cancel(
                    lambda: engine.ask(history, profile), "[cyan]Thinking…[/]"
                )
            except core.TranslationError as exc2:
                console.print(f"[bold red]Couldn't answer:[/] {exc2}")
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
# History, checkpoints, and undo
# ---------------------------------------------------------------------------

def _print_history(limit: int = 15) -> None:
    """Show recently executed commands and their undo status."""
    entries = core.load_journal(limit=limit)
    if not entries:
        console.print("[dim]No commands recorded yet.[/]")
        return
    table = Table(title="Recent commands", title_style=f"bold {ACCENT}", expand=False)
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("When", style="dim", no_wrap=True)
    table.add_column("Exit", justify="right")
    table.add_column("Command")
    table.add_column("Undo", style="dim")
    for entry in entries:
        exit_style = "green" if entry.exit_code == 0 else "red"
        if entry.undone:
            undo = "[dim]undone[/]"
        elif entry.checkpoint_id:
            undo = "checkpoint"
        elif entry.mutating:
            undo = "revertible"
        else:
            undo = "[dim]—[/]"
        table.add_row(
            entry.id, entry.timestamp.replace("T", " "),
            f"[{exit_style}]{entry.exit_code}[/]", entry.command, undo,
        )
    console.print(table)
    console.print("[dim]Undo the last change with [/][cyan]undo[/][dim], or a specific one with [/][cyan]undo <ID>[/][dim].[/]")


def _apply_restore(checkpoint: core.Checkpoint) -> None:
    """Run a checkpoint restore and print its log."""
    for line in core.restore_checkpoint(checkpoint):
        console.print(f"  [dim]{line}[/]")


def do_undo(engine: core.Engine, arg: str) -> None:
    """Undo the last change-making command (or a specific one by ID)."""
    entry = core.find_entry(arg) if arg else core.last_undoable()
    if entry is None:
        console.print(
            f"[yellow]No command found for '{arg}'.[/]" if arg
            else "[yellow]Nothing to undo.[/]"
        )
        return
    if entry.undone:
        console.print("[dim]That command was already undone.[/]")
        return

    console.print(f"[dim]Undoing:[/] [cyan]{entry.command}[/]")
    checkpoint = core.load_checkpoint(entry.checkpoint_id) if entry.checkpoint_id else None

    # Layer 1: a file snapshot exists — restore it exactly.
    if checkpoint and checkpoint.files:
        plan = "\n".join(
            f"• restore [cyan]{f.original}[/]" if f.existed_before
            else f"• remove [cyan]{f.original}[/] [dim](it was created)[/]"
            for f in checkpoint.files
        )
        console.print(Panel(Text.from_markup(plan), title="[bold yellow]Undo via checkpoint[/]",
                            border_style="yellow", title_align="left"))
        if Prompt.ask("Restore this checkpoint?", choices=["y", "n"], default="n") != "y":
            console.print("[dim]Undo cancelled.[/]")
            return
        _apply_restore(checkpoint)
        core.mark_undone(entry.id)
        console.print("[green]Undone.[/]")
        return

    # Layer 2: no snapshot (a service/package/etc. change) — generate an inverse.
    if not entry.mutating:
        console.print("[dim]That command didn't change anything — nothing to undo.[/]")
        return
    try:
        inverse, cancelled = run_with_cancel(
            lambda: engine.invert(entry.command), "[cyan]Working out how to undo…[/]"
        )
    except core.TranslationError as exc:
        console.print(f"[red]Couldn't work out an undo:[/] {exc}")
        return
    if cancelled:
        console.print("[dim]Cancelled.[/]")
        return
    if not inverse or inverse == core.UNSUPPORTED_SENTINEL:
        console.print(
            "[yellow]This command can't be undone automatically.[/] "
            "[dim]You'll need to reverse it manually.[/]"
        )
        return

    verdict = core.screen_command(inverse)
    if not verdict.allowed:
        console.print(Panel(
            Text.from_markup(f"[bold]Proposed undo:[/]\n  [red]{inverse}[/]\n\n[bold]Reason:[/] {verdict.reason}"),
            title="[bold red]Undo blocked by safety filter[/]", border_style="red", title_align="left",
        ))
        return
    if not confirm_execution(inverse, f"Reverses: {entry.command}"):
        console.print("[dim]Undo cancelled — nothing was run.[/]")
        return

    cancel_event = threading.Event()
    result, cancelled = run_with_cancel(
        lambda: core.run_command(inverse, cancel_event), "[cyan]Undoing…[/]",
        cancel_event=cancel_event,
    )
    _render_result(result)
    mutating, _paths = core.classify_command(inverse)
    core.record_command(f"[undo of {entry.id}]", inverse, os.getcwd(), result.exit_code, mutating, None)
    if not cancelled and result.exit_code == 0:
        core.mark_undone(entry.id)
        console.print("[green]Undone.[/]")
    else:
        console.print("[yellow]The undo command did not complete cleanly — check the output above.[/]")


def do_checkpoint(arg: str) -> None:
    """Manually snapshot a file or directory so it can be restored later."""
    path = arg.strip().strip("'\"")
    if not path:
        console.print("[yellow]Usage:[/] [cyan]checkpoint <path>[/]")
        return
    if not os.path.exists(os.path.expanduser(path)):
        console.print(f"[yellow]No such path:[/] {path}")
        return
    checkpoint = core.create_checkpoint(f"manual checkpoint of {path}", [path], label="manual")
    if checkpoint and checkpoint.saved_count:
        console.print(
            f"[green]Checkpoint {checkpoint.id} saved[/] [dim]({path}). Restore with[/] "
            f"[cyan]restore {checkpoint.id}[/][dim].[/]"
        )
    else:
        console.print("[yellow]Nothing saved[/] [dim](path missing or larger than the size limit).[/]")


def _print_checkpoints(limit: int = 20) -> None:
    """List saved checkpoints."""
    checkpoints = core.list_checkpoints()
    if not checkpoints:
        console.print("[dim]No checkpoints saved.[/]")
        return
    table = Table(title="Checkpoints", title_style=f"bold {ACCENT}", expand=False)
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("When", style="dim", no_wrap=True)
    table.add_column("Kind", style="dim")
    table.add_column("Saved", justify="right")
    table.add_column("From")
    for checkpoint in checkpoints[:limit]:
        table.add_row(
            checkpoint.id, checkpoint.timestamp.replace("T", " "), checkpoint.label,
            f"{checkpoint.saved_count}/{len(checkpoint.files)}", checkpoint.command,
        )
    console.print(table)
    console.print("[dim]Restore one with [/][cyan]restore <ID>[/][dim] (newest if omitted).[/]")


def do_restore(arg: str) -> None:
    """Restore a checkpoint by ID (or the most recent one)."""
    checkpoints = core.list_checkpoints()
    if not checkpoints:
        console.print("[dim]No checkpoints to restore.[/]")
        return
    arg = arg.strip()
    if not arg:
        checkpoint = checkpoints[0]
    else:
        checkpoint = core.load_checkpoint(arg) or next(
            (c for c in checkpoints if c.id.startswith(arg)), None
        )
    if checkpoint is None:
        console.print(f"[yellow]No checkpoint matching '{arg}'.[/]")
        return
    plan = "\n".join(
        f"• restore [cyan]{f.original}[/]" if f.existed_before
        else f"• remove [cyan]{f.original}[/] [dim](created after the snapshot)[/]"
        for f in checkpoint.files
    )
    console.print(Panel(
        Text.from_markup(f"[dim]Checkpoint {checkpoint.id} — from:[/] {checkpoint.command}\n\n{plan}"),
        title="[bold yellow]Restore checkpoint[/]", border_style="yellow", title_align="left",
    ))
    if Prompt.ask("Restore now?", choices=["y", "n"], default="n") != "y":
        console.print("[dim]Restore cancelled.[/]")
        return
    _apply_restore(checkpoint)
    console.print("[green]Restore complete.[/]")


# ---------------------------------------------------------------------------
# Fleet: hosts and targeting
#
# The controller (this CLI) can run a request against the local machine or a
# remote host running a Sentinel agent. `_target` is the active host; commands
# `hosts` / `host add` / `use` / `on` manage and select it.
# ---------------------------------------------------------------------------

_hosts: dict[str, core.HostConfig] = {}
_target: str = core.LOCAL_HOST

# Rolling conversation context for the command loop: recent {"role","content"}
# turns (a request and the command it produced) so follow-ups like "what about
# the CPU?" or "now restart it" resolve. Bounded in handle_request.
_history: list[dict] = []


def _known_target(name: str) -> bool:
    return name == core.LOCAL_HOST or name in _hosts


def _print_hosts() -> None:
    """List the controller's hosts and probe each remote agent's health."""
    table = Table(title="Hosts", title_style=f"bold {ACCENT}", expand=False)
    table.add_column("", width=1)
    table.add_column("Name", style="bold")
    table.add_column("Where")
    table.add_column("Status")
    table.add_column("Execute", style="dim")

    marker = "[green]›[/]" if _target == core.LOCAL_HOST else " "
    table.add_row(marker, core.LOCAL_HOST, "this machine", "[green]ready[/]", "yes")
    for name, host in _hosts.items():
        status, execute = "[yellow]unknown[/]", "—"
        try:
            health = core.RemoteAgent(host, timeout=5).health()
            status = "[green]online[/]"
            execute = "yes" if health.get("exec_enabled") and host.admin_token else "read-only"
        except core.RemoteError:
            status = "[red]offline[/]"
        marker = "[green]›[/]" if _target == name else " "
        table.add_row(marker, name, host.base_url, status, execute)
    console.print(table)
    console.print(
        "[dim]Switch with [/][cyan]use <name>[/][dim], run one-off with [/]"
        "[cyan]on <name> <request>[/][dim] or [/][cyan]on all <request>[/][dim].[/]"
    )


def host_add_interactive() -> None:
    """Prompt for a remote host's URL and tokens, then save it."""
    name = Prompt.ask("Host name (e.g. vm, homelab)").strip()
    if not name or name == core.LOCAL_HOST:
        console.print("[yellow]Pick a non-empty name other than 'local'.[/]")
        return
    base_url = Prompt.ask("Agent URL (e.g. http://192.168.1.20:8765)").strip()
    if not base_url:
        console.print("[yellow]A URL is required.[/]")
        return
    read_token = Prompt.ask("Read token [dim](diagnostics)[/]", password=True, default="")
    admin_token = Prompt.ask(
        "Admin token [dim](execute; leave blank for read-only)[/]", password=True, default=""
    )
    host = core.HostConfig(name=name, base_url=base_url, read_token=read_token, admin_token=admin_token)
    try:
        health = core.RemoteAgent(host, timeout=5).health()
        console.print(f"[green]Reached {name}[/] [dim]({health.get('os', '?')}).[/]")
    except core.RemoteError as exc:
        console.print(f"[yellow]Saved, but couldn't reach it yet:[/] {exc}")
    _hosts[name] = host
    core.save_hosts(_hosts)
    console.print(f"[green]Host '{name}' saved.[/] [dim]Use it with[/] [cyan]use {name}[/][dim].[/]")


def host_remove(name: str) -> None:
    if name in _hosts:
        del _hosts[name]
        core.save_hosts(_hosts)
        global _target
        if _target == name:
            _target = core.LOCAL_HOST
        console.print(f"[green]Removed host '{name}'.[/]")
    else:
        console.print(f"[yellow]No such host: {name}[/]")


def execute_on(target: str, command: str):
    """Run an approved command on a target. Returns (CommandResult|None, cancelled)."""
    if target == core.LOCAL_HOST:
        cancel_event = threading.Event()
        return run_with_cancel(
            lambda: core.run_command(command, cancel_event),
            "[cyan]Running…[/]", cancel_event=cancel_event,
        )
    host = _hosts.get(target)
    if host is None:
        console.print(f"[red]Unknown host:[/] {target}")
        return None, False
    if not host.can_execute:
        reason = ("execute is turned off for this host" if host.admin_token
                  else "no admin token")
        console.print(
            f"[yellow]{target} is read-only — {reason}.[/] "
            f"[dim]Change it in[/] [cyan]settings[/][dim].[/]"
        )
        return None, False
    agent = core.RemoteAgent(host)
    try:
        return run_with_cancel(
            lambda: agent.execute(command), f"[cyan]Running on {target}…[/]"
        )
    except core.RemoteError as exc:
        console.print(f"[red]{target}:[/] {exc}")
        return None, False


# Short, friendly aliases for the read-only diagnostics the agent exposes.
_DIAGNOSTIC_ALIASES = {
    "status": "system_overview", "overview": "system_overview", "health": "system_overview",
    "cpu": "cpu_usage", "load": "cpu_usage",
    "mem": "memory_usage", "memory": "memory_usage", "ram": "memory_usage",
    "disk": "disk_usage", "storage": "disk_usage",
    "power": "power_and_thermal", "thermal": "power_and_thermal",
    "temp": "power_and_thermal", "temps": "power_and_thermal",
    "proc": "top_processes", "procs": "top_processes", "processes": "top_processes", "top": "top_processes",
    "net": "network_overview", "network": "network_overview",
    "ports": "listening_ports", "logs": "recent_errors", "errors": "recent_errors",
}
_DIAGNOSTIC_FUNCS = frozenset(_DIAGNOSTIC_ALIASES.values())

_local_diag = None


def _local_diagnostics():
    """Lazily import the diagnostics catalog for the local host."""
    global _local_diag
    if _local_diag is None:
        import importlib
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcp_server"))
        _local_diag = importlib.import_module("diagnostics")
    return _local_diag


def _resolve_diagnostic(name: str) -> str | None:
    name = name.lower()
    if name in _DIAGNOSTIC_ALIASES:
        return _DIAGNOSTIC_ALIASES[name]
    return name if name in _DIAGNOSTIC_FUNCS else None


# Natural-language routing: detect a registered host named in the request, and
# whether the request is a read-only "how is X looking?" diagnostic question.
_ACTION_CUES = frozenset({
    "delete", "remove", "rm", "stop", "start", "restart", "install", "uninstall",
    "kill", "create", "make", "mkdir", "move", "copy", "edit", "change", "set",
    "enable", "disable", "reboot", "shutdown", "clear", "truncate", "write",
    "chmod", "chown", "update", "upgrade", "purge", "mount", "unmount", "run",
})
_STATUS_CUES = ("how", "what", "show", "looking", "usage", "status", "health",
                "check", "doing", "report", "?")


def _detect_host(text: str) -> str | None:
    """Return the first registered host name mentioned in the request, if any."""
    low = text.lower()
    for name in _hosts:
        if re.search(rf"\b{re.escape(name.lower())}\b", low):
            return name
    return None


def _strip_host_ref(text: str, name: str) -> str:
    """Remove a host reference (e.g. 'on homelab', \"homelab's\") from a request."""
    text = re.sub(rf"\b(?:my|the|on|for|in)\s+{re.escape(name)}(?:'s|s')?\b", "", text, flags=re.I)
    text = re.sub(rf"\b{re.escape(name)}(?:'s|s')?\b", "", text, flags=re.I)
    return re.sub(r"\s{2,}", " ", text).strip()


def _diagnostic_intent(text: str) -> str | None:
    """Map a read-only 'how is <metric> looking?' question to a diagnostic name."""
    low = text.lower()
    if set(re.findall(r"[a-z]+", low)) & _ACTION_CUES:   # an action → not a read
        return None
    if not any(cue in low for cue in _STATUS_CUES):
        return None
    for keyword, canonical in _DIAGNOSTIC_ALIASES.items():
        if re.search(rf"\b{re.escape(keyword)}\b", low):
            return canonical
    return None


def run_diagnostic(engine: core.Engine, profile: core.UserProfile, target: str, name: str) -> None:
    """Run a read-only diagnostic on a target (local or a remote agent) and show it."""
    canonical = _resolve_diagnostic(name)
    if canonical is None:
        console.print(
            f"[yellow]Unknown diagnostic '{name}'.[/] [dim]Try: status, cpu, memory, "
            "disk, power, processes, network, ports, logs.[/]"
        )
        return
    where = "" if target == core.LOCAL_HOST else f" on {target}"
    try:
        if target == core.LOCAL_HOST:
            output, cancelled = run_with_cancel(
                lambda: getattr(_local_diagnostics(), canonical)(),
                f"[cyan]Reading {canonical}…[/]",
            )
        else:
            host = _hosts.get(target)
            if host is None:
                console.print(f"[red]Unknown host:[/] {target}")
                return
            output, cancelled = run_with_cancel(
                lambda: core.RemoteAgent(host).diagnostic(canonical),
                f"[cyan]Reading {canonical}{where}…[/]",
            )
    except core.RemoteError as exc:
        console.print(f"[red]{target}:[/] {exc}")
        return
    if cancelled:
        console.print("[dim]Cancelled.[/]")
        return

    console.print(Panel(
        output or "(no output)",
        title=f"[bold {ACCENT}]{canonical}{where}[/]", border_style=ACCENT, title_align="left",
    ))
    # Plain-English read of the numbers, like a command's post-run summary.
    if profile.explain and output:
        try:
            summary, cancelled = run_with_cancel(
                lambda: engine.summarize(
                    f"{name}{where}", f"[diagnostic] {canonical}",
                    core.CommandResult(0, output), profile,
                ),
                "[cyan]Summarizing…[/]",
            )
            if summary and not cancelled:
                _print_summary(summary)
        except core.TranslationError:
            pass


def handle_request(
    engine: core.Engine, profile: core.UserProfile, request: str, images, target: str,
    history: "list[dict] | None" = None,
) -> None:
    """Translate a request, screen + confirm it, run it on ``target``, summarize.

    The full read→translate→approve→execute flow for one request, factored out so
    the main loop, ``on <host>``, and ``on all`` all share it. When ``history`` is
    given, prior turns are passed to the translator (so "what about the CPU?" or
    "now restart it" resolves) and this turn is appended to it.
    """
    if images and _only_placeholders(request):
        request = "Use the attached image(s) to determine the right command."

    # Translate (with a vision-bridge fallback for text-only models).
    try:
        command, cancelled = run_with_cancel(
            lambda: engine.translate(request, images, history), "[cyan]Translating…[/]"
        )
    except core.TranslationError as exc:
        augmented = _bridge_request(engine, request, images, str(exc))
        if augmented is None:
            console.print(f"[bold red]Translation failed:[/] {exc}{_vision_hint(images, str(exc))}")
            return
        try:
            request = augmented
            command, cancelled = run_with_cancel(
                lambda: engine.translate(augmented, None, history), "[cyan]Translating…[/]"
            )
        except core.TranslationError as exc2:
            console.print(f"[bold red]Translation failed:[/] {exc2}")
            return
    if cancelled:
        console.print("[dim]Cancelled.[/]")
        return
    if not command or command == core.UNSUPPORTED_SENTINEL:
        console.print(
            "[yellow]That isn't a Linux operation I can run.[/] "
            "[dim]Try e.g. [/][cyan]\"how much disk is free?\"[/][dim].[/]"
        )
        return

    # Remember this turn so follow-ups can refer back to it (bounded window).
    if history is not None:
        history.append({"role": "user", "content": request})
        history.append({"role": "assistant", "content": command})
        del history[:-8]  # keep the last 4 turns

    # Safety filter (defense in depth, before any prompt).
    verdict = core.screen_command(command)
    if not verdict.allowed:
        console.print(Panel(
            Text.from_markup(f"[bold]Blocked command:[/]\n  [red]{command}[/]\n\n[bold]Reason:[/] {verdict.reason}"),
            title="[bold red]🛑 Refused by safety filter[/]", border_style="red", title_align="left",
        ))
        return

    # Optional, profile-aware explanation (best-effort).
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
                return

    # Human confirmation gate — the heart of the safety model (shows the target).
    if not confirm_execution(command, explanation, target):
        console.print("[dim]Skipped — nothing was run.[/]")
        return

    # Snapshot files this command will touch (local target only — we can't
    # snapshot a remote host's files from here yet).
    mutating, targets = core.classify_command(command)
    checkpoint = None
    if target == core.LOCAL_HOST and mutating and targets:
        checkpoint = core.create_checkpoint(command, targets)
        if checkpoint and checkpoint.saved_count:
            console.print(f"[dim]Checkpoint {checkpoint.id} saved — undo with[/] [cyan]undo[/][dim].[/]")

    result, cancelled = execute_on(target, command)
    if result is None:
        return
    _render_result(result)
    core.record_command(
        request, command, os.getcwd(), result.exit_code, mutating,
        checkpoint.id if checkpoint else None, host=target,
    )
    if cancelled:
        console.print("[yellow]Cancelled — the command was stopped.[/]")
        return

    # Plain-English answer derived from the output (best-effort).
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


# ---------------------------------------------------------------------------
# Settings menu (interactive hub) and fleet alerts
# ---------------------------------------------------------------------------

def _read_menu_key() -> str:
    """Read one menu selection keypress. Returns "" on Esc/Enter (= back/close).

    Single keypress on a raw TTY (no Enter needed); falls back to a typed line
    where raw mode isn't available.
    """
    if not (_TERMIOS_OK and sys.stdin.isatty()):
        try:
            return Prompt.ask("Select [dim](Esc to go back)[/]", default="").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return ""
    console.print("[dim]Select (Esc to go back):[/] ", end="")
    global _input_buf
    _input_buf = b""
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        while True:
            kind, *rest = _read_key()
            if kind == "char" and rest[0].strip():
                console.print(rest[0])
                return rest[0].strip().lower()
            if kind in ("escape", "interrupt", "eof", "enter"):
                console.print("")
                return ""
            # arrows, backspace, paste, etc. → ignore and keep waiting
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


def _menu(title: str, rows: list[str]) -> str:
    """Render a titled menu and return the chosen key ("" = back, e.g. Esc)."""
    body = "\n".join(rows)
    console.print(Panel(Text.from_markup(body), title=f"[bold {ACCENT}]{title}[/]",
                        border_style=ACCENT, title_align="left"))
    return _read_menu_key()


def _ask_int(label: str, current: int) -> int:
    raw = Prompt.ask(label, default=str(current)).strip()
    try:
        return int(raw)
    except ValueError:
        return current


def _ask_float(label: str, current: float) -> float:
    raw = Prompt.ask(label, default=str(current)).strip()
    try:
        return float(raw)
    except ValueError:
        return current


def _settings_ai(engine: core.Engine) -> core.Engine:
    """Submenu: provider / model / vision fallback."""
    while True:
        choice = _menu("AI", [
            f"[cyan]1[/] Provider   [dim]current: {engine.label}[/]",
            f"[cyan]2[/] Model      [dim]current: {engine.model}[/]",
            f"[cyan]3[/] Vision fallback   [dim]current: {_bridge_model(engine.spec.key) or '(disabled)'}[/]",
            "[cyan]Esc[/]/[cyan]b[/] Back",
        ])
        try:
            if choice == "1":
                engine = build_engine(select_provider())
                select_model(engine)
                _remember(engine)
            elif choice == "2":
                select_model(engine)
                _remember(engine)
            elif choice == "3":
                select_vision_model(engine)
            else:
                return engine
        except core.TranslationError as exc:
            console.print(f"[red]{exc}[/]")
        except (EOFError, KeyboardInterrupt):
            return engine


def _host_monitor_summary(host: core.HostConfig) -> str:
    """One-line monitor state for a host (best-effort)."""
    try:
        mon = core.RemoteAgent(host, timeout=5).get_monitor()
    except core.RemoteError:
        return "[dim]offline[/]"
    if mon.get("enabled"):
        return f"[green]on[/] every {mon.get('interval_seconds', '?')}s"
    return "[dim]off[/]"


def _settings_instances(engine: core.Engine) -> None:
    """Submenu: list, add, and manage hosts."""
    while True:
        names = list(_hosts)
        rows = ["[cyan]local[/]   [dim]this machine — execute: yes[/]"]
        for i, name in enumerate(names, start=1):
            host = _hosts[name]
            execute = "[green]on[/]" if host.can_execute else "[yellow]off[/]"
            rows.append(f"[cyan]{i}[/] {name}   [dim]{host.base_url}[/]   execute: {execute}")
        rows += ["[cyan]a[/] Add a host", "[cyan]Esc[/]/[cyan]b[/] Back"]
        choice = _menu("Instances", rows)
        if choice == "a":
            try:
                host_add_interactive()
            except (EOFError, KeyboardInterrupt):
                console.print("\n[dim]Cancelled.[/]")
        elif choice.isdigit() and 1 <= int(choice) <= len(names):
            _manage_host(names[int(choice) - 1])
        else:
            return


def _manage_host(name: str) -> None:
    """Submenu for one host: execute lever, health check, edit, remove."""
    while True:
        host = _hosts.get(name)
        if host is None:
            return
        execute = "[green]on[/]" if host.can_execute else "[yellow]off[/]"
        token_note = "" if host.admin_token else " [dim](no admin token)[/]"
        choice = _menu(f"Host · {name}", [
            f"[dim]URL:[/] {host.base_url}",
            f"[cyan]1[/] Execute (run commands here): {execute}{token_note}",
            f"[cyan]2[/] Health check: {_host_monitor_summary(host)}",
            "[cyan]3[/] Edit URL / tokens",
            "[cyan]4[/] Remove this host",
            "[cyan]Esc[/]/[cyan]b[/] Back",
        ])
        if choice == "1":
            if not host.admin_token:
                console.print("[yellow]This host has no admin token — add one with Edit first.[/]")
            else:
                host.allow_execute = not host.allow_execute
                core.save_hosts(_hosts)
                console.print(f"[green]Execute {'enabled' if host.allow_execute else 'disabled'} for {name}.[/]")
        elif choice == "2":
            _settings_health(host)
        elif choice == "3":
            try:
                host_add_interactive()  # same name overwrites; re-enter details
            except (EOFError, KeyboardInterrupt):
                console.print("\n[dim]Cancelled.[/]")
        elif choice == "4":
            if Prompt.ask(f"Remove host '{name}'?", choices=["y", "n"], default="n") == "y":
                host_remove(name)
                return
        else:
            return


def _settings_health(host: core.HostConfig) -> None:
    """Configure a host's always-on health monitor (via its agent)."""
    if not host.admin_token:
        console.print("[yellow]Configuring the monitor needs the admin token — add one via Edit.[/]")
        return
    agent = core.RemoteAgent(host)
    while True:
        try:
            mon = agent.get_monitor()
        except core.RemoteError as exc:
            console.print(f"[red]{host.name}: {exc}[/]")
            return
        thresholds = mon.get("thresholds", {})
        choice = _menu(f"Health check · {host.name}", [
            f"[cyan]1[/] Monitoring: {'[green]on[/]' if mon.get('enabled') else '[dim]off[/]'}",
            f"[cyan]2[/] Interval: {mon.get('interval_seconds')}s",
            f"[cyan]3[/] Disk alert at: {thresholds.get('disk_pct')}%",
            f"[cyan]4[/] Memory alert at: {thresholds.get('memory_pct')}%",
            f"[cyan]5[/] Load alert at: {thresholds.get('load_factor')}× cores",
            f"[cyan]6[/] Error-log alert at: {thresholds.get('error_count')}/interval",
            f"[cyan]7[/] Watched services: {', '.join(thresholds.get('services') or []) or '(none)'}",
            "[cyan]8[/] Run a check now",
            "[cyan]Esc[/]/[cyan]b[/] Back",
        ])
        try:
            if choice == "1":
                agent.set_monitor({"enabled": not mon.get("enabled")})
            elif choice == "2":
                agent.set_monitor({"interval_seconds": _ask_int("Interval (seconds)", mon.get("interval_seconds", 300))})
            elif choice == "3":
                agent.set_monitor({"thresholds": {"disk_pct": _ask_int("Disk % to alert at", thresholds.get("disk_pct", 90))}})
            elif choice == "4":
                agent.set_monitor({"thresholds": {"memory_pct": _ask_int("Memory % to alert at", thresholds.get("memory_pct", 90))}})
            elif choice == "5":
                agent.set_monitor({"thresholds": {"load_factor": _ask_float("Load × cores to alert at", thresholds.get("load_factor", 2.0))}})
            elif choice == "6":
                agent.set_monitor({"thresholds": {"error_count": _ask_int("Error-log entries to alert at", thresholds.get("error_count", 50))}})
            elif choice == "7":
                raw = Prompt.ask("Services to watch (comma-separated, blank for none)",
                                 default=",".join(thresholds.get("services") or []))
                services = [s.strip() for s in raw.split(",") if s.strip()]
                agent.set_monitor({"thresholds": {"services": services}})
            elif choice == "8":
                alerts = agent.run_monitor()
                console.print(f"[green]Ran checks — {len(alerts)} alert(s).[/]" if alerts
                              else "[green]Ran checks — all clear.[/]")
            else:
                return
        except core.RemoteError as exc:
            console.print(f"[red]{host.name}: {exc}[/]")


def show_fleet_alerts(limit: int = 20) -> None:
    """Pull recent health alerts from every remote host and show them."""
    if not _hosts:
        console.print("[dim]No remote hosts. Add one in[/] [cyan]settings[/][dim].[/]")
        return
    any_shown = False
    for name, host in _hosts.items():
        try:
            alerts = core.RemoteAgent(host, timeout=8).get_alerts(limit)
        except core.RemoteError as exc:
            console.print(f"[red]{name}:[/] {exc}")
            continue
        if not alerts:
            console.print(f"[green]✓ {name}: no alerts[/]")
            continue
        any_shown = True
        table = Table(title=f"{name} — recent alerts", title_style=f"bold {ACCENT}", expand=False)
        table.add_column("When", style="dim", no_wrap=True)
        table.add_column("Level")
        table.add_column("Check")
        table.add_column("Message")
        for alert in alerts[-limit:]:
            colour = "red" if alert.get("level") == "critical" else "yellow"
            table.add_row(alert.get("time", "").replace("T", " "),
                          f"[{colour}]{alert.get('level')}[/]", alert.get("check", ""),
                          alert.get("message", ""))
        console.print(table)
    if not any_shown:
        console.print("[dim]Fleet looks healthy.[/]")


def settings_menu(engine: core.Engine, profile: core.UserProfile):
    """Top-level interactive settings hub. Returns the (engine, profile)."""
    while True:
        choice = _menu("Settings", [
            "[cyan]1[/] AI            [dim]provider · model · vision fallback[/]",
            "[cyan]2[/] Instances     [dim]add / manage hosts, execute & health toggles[/]",
            "[cyan]3[/] Fleet alerts  [dim]recent health alerts from each host[/]",
            "[cyan]4[/] Profile       [dim]experience level · explanations[/]",
            "[cyan]Esc[/]/[cyan]b[/] Back to the prompt",
        ])
        try:
            if choice == "1":
                engine = _settings_ai(engine)
            elif choice == "2":
                _settings_instances(engine)
            elif choice == "3":
                show_fleet_alerts()
            elif choice == "4":
                profile = run_onboarding()
            else:
                return engine, profile
        except (EOFError, KeyboardInterrupt):
            return engine, profile


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
    right.add_row(Text.from_markup("  [cyan]\"what errors hit the journal this hour?\"[/]"))
    right.add_row(Text.from_markup("  [cyan]\"how is my homelab's cpu usage looking?\"[/]"))
    right.add_row(Text.from_markup("[dim]Name a host to target it; add one with [/][cyan]host add[/]"))
    right.add_row("")
    right.add_row(Text.from_markup(f"[bold {ACCENT}]Commands[/]"))
    right.add_row(Text.from_markup("[cyan]ask[/]       ask a Linux question"))
    right.add_row(Text.from_markup("[cyan]provider[/]  switch AI provider"))
    right.add_row(Text.from_markup("[cyan]model[/]     pick / refresh model"))
    right.add_row(Text.from_markup("[cyan]vision[/]    image fallback model"))
    right.add_row(Text.from_markup("[cyan]undo[/]      undo the last change · [cyan]history[/]"))
    right.add_row(Text.from_markup("[cyan]hosts[/]     manage machines · [cyan]on <host> …[/]"))
    right.add_row(Text.from_markup("[cyan]status[/]    health snapshot · [cyan]diag power[/]"))
    right.add_row(Text.from_markup("[cyan]settings[/]  menu: AI · hosts · health · [cyan]alerts[/]"))
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
                "[cyan]vision[/]    Set the fallback model that reads images when "
                "your main model can't\n"
                "[cyan]history[/]   Show recently run commands and their undo status\n"
                "[cyan]undo[/] [dim][ID][/]  Undo the last change (restore a snapshot, or "
                "run a safe inverse command)\n"
                "[cyan]checkpoint[/] <path>  Snapshot a file/dir; [cyan]checkpoints[/] "
                "lists them, [cyan]restore[/] [dim][ID][/] brings one back\n"
                "[cyan]hosts[/]     List machines; [cyan]host add[/] / [cyan]host remove <name>[/] "
                "manage them\n"
                "[cyan]use[/] <host>  Target a host for following commands; "
                "[cyan]on <host|all> <request>[/] runs one-off\n"
                "[cyan]status[/] [dim][host][/]  Full health snapshot; [cyan]diag <cpu|memory|"
                "disk|power|…> [host][/] for one metric\n"
                "[dim]Or just name a host in a question — [/][cyan]\"how is my homelab's "
                "cpu looking?\"[/][dim] — no [/][cyan]use[/][dim] needed.[/]\n"
                "[cyan]settings[/]  Interactive menu: AI, instances, execute & health "
                "toggles, profile\n"
                "[cyan]alerts[/]    Recent health alerts from every host\n"
                "[cyan]profile[/]   Re-take the experience questionnaire (sets how "
                "commands are explained)\n"
                "[cyan]help[/]      Show this help\n"
                "[cyan]exit[/]      Quit (Ctrl-D also works)\n\n"
                "[dim]Tip: a leading slash is optional — [/][cyan]/ask[/][dim] works too.[/]\n"
                "[dim]Tip: attach an image for context — paste a screenshot, press[/] "
                "[cyan]Ctrl-V[/] [dim]for the clipboard image, or include a path like[/]\n"
                '[dim]  [/][cyan]why does this fail? ~/screenshot.png[/][dim] — it shows as[/] '
                "[cyan][Image #1][/][dim] (needs a vision-capable model).[/]"
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

    global _settings, _hosts
    _settings = core.load_settings()  # remembered provider/model/keys
    _hosts = core.load_hosts()        # registered remote hosts (fleet)

    try:
        profile = get_or_create_profile()
    except (EOFError, KeyboardInterrupt):
        console.print("\n[dim]Goodbye.[/]")
        return

    engine = _startup_engine()
    _print_banner(engine, profile)

    global _target
    while True:
        target_tag = "" if _target == core.LOCAL_HOST else f"{_target} · "
        try:
            request, images = read_request(
                f"\n[bold cyan]>[/] [dim]({target_tag}{engine.spec.key}:{engine.model})[/]"
            )
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]Goodbye.[/]")
            break

        if not request and not images:
            continue

        # A leading slash is optional on commands (so /ask, /help also work).
        if request.startswith("/"):
            request = request[1:].lstrip()
            if not request and not images:
                continue

        command_word = request.lower()
        first_word = command_word.split(maxsplit=1)[0] if command_word else ""
        if command_word in {"exit", "quit"}:
            console.print("[dim]Goodbye.[/]")
            break
        if command_word == "help":
            _print_help()
            continue
        if command_word == "settings":
            engine, profile = settings_menu(engine, profile)
            continue
        if command_word == "alerts":
            show_fleet_alerts()
            continue
        if first_word in {"ask", "chat"}:
            parts = request.split(maxsplit=1)
            inline_question = parts[1].strip() if len(parts) > 1 else ""
            if inline_question or images:
                ask_once(engine, profile, inline_question, images)
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
        if command_word == "vision":
            try:
                select_vision_model(engine)
            except (EOFError, KeyboardInterrupt):
                console.print("\n[dim]Vision fallback unchanged.[/]")
            continue
        if command_word == "history":
            _print_history()
            continue
        if first_word == "undo":
            parts = request.split(maxsplit=1)
            try:
                do_undo(engine, parts[1].strip() if len(parts) > 1 else "")
            except (EOFError, KeyboardInterrupt):
                console.print("\n[dim]Undo cancelled.[/]")
            continue
        if command_word == "checkpoints":
            _print_checkpoints()
            continue
        if first_word == "checkpoint":
            parts = request.split(maxsplit=1)
            do_checkpoint(parts[1] if len(parts) > 1 else "")
            continue
        if first_word == "restore":
            parts = request.split(maxsplit=1)
            try:
                do_restore(parts[1].strip() if len(parts) > 1 else "")
            except (EOFError, KeyboardInterrupt):
                console.print("\n[dim]Restore cancelled.[/]")
            continue
        if command_word == "hosts":
            _print_hosts()
            continue
        if first_word == "host":
            parts = request.split(maxsplit=2)
            sub = parts[1].lower() if len(parts) > 1 else ""
            try:
                if sub == "add":
                    host_add_interactive()
                elif sub in {"remove", "rm", "del"} and len(parts) > 2:
                    host_remove(parts[2].strip())
                else:
                    console.print("[dim]Usage: [/][cyan]host add[/][dim] or [/][cyan]host remove <name>[/][dim].[/]")
            except (EOFError, KeyboardInterrupt):
                console.print("\n[dim]Cancelled.[/]")
            continue
        if first_word == "use":
            parts = request.split(maxsplit=1)
            name = parts[1].strip() if len(parts) > 1 else ""
            if _known_target(name):
                _target = name
                _history.clear()  # switching target starts a fresh conversation
                console.print(f"[green]Target set to[/] [bold]{name}[/].")
            else:
                console.print(f"[yellow]Unknown host '{name}'.[/] [dim]See[/] [cyan]hosts[/][dim].[/]")
            continue
        if first_word in {"status", "diag"}:
            parts = request.split()
            if first_word == "status":
                name = "system_overview"
                rest = parts[1:]
            else:
                name = parts[1] if len(parts) > 1 else "system_overview"
                rest = parts[2:]
            dest = rest[0] if rest and _known_target(rest[0]) else _target
            run_diagnostic(engine, profile, dest, name)
            continue
        if first_word == "on":
            parts = request.split(maxsplit=2)
            dest = parts[1] if len(parts) > 1 else ""
            sub_request = parts[2].strip() if len(parts) > 2 else ""
            if not dest or not sub_request:
                console.print("[dim]Usage: [/][cyan]on <host|all> <request>[/][dim].[/]")
                continue
            if dest == "all":
                destinations = [core.LOCAL_HOST, *_hosts]
            elif _known_target(dest):
                destinations = [dest]
            else:
                console.print(f"[yellow]Unknown host '{dest}'.[/] [dim]See[/] [cyan]hosts[/][dim].[/]")
                continue
            for destination in destinations:
                if len(destinations) > 1:
                    console.rule(f"[bold {ACCENT}]{destination}[/]")
                handle_request(engine, profile, sub_request, images, destination)
            continue

        # Natural-language targeting: naming a host switches the active target
        # (and it sticks, so "what about the CPU?" stays on that host), and a
        # read-only metric question goes straight to the diagnostic.
        detected = _detect_host(request)
        if detected and detected != _target:
            _target = detected
            _history.clear()  # new host context — drop the old conversation
        intent = _diagnostic_intent(request) if not images else None
        if intent:
            run_diagnostic(engine, profile, _target, intent)
            continue
        request = _strip_host_ref(request, detected) if detected else request
        handle_request(engine, profile, request, images, _target, history=_history)


if __name__ == "__main__":
    main()
