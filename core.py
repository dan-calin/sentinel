#!/usr/bin/env python3
"""Sentinel core — provider-agnostic logic shared by the CLI and GUI backend.

This module has **no UI dependencies** (no `rich`, no prompts). It holds
everything that defines what Sentinel *does*:

* the translation contract and environment grounding,
* the provider catalog and pluggable translation engines,
* the destructive-command safety filter,
* local command execution,
* the user profile (experience level + preferences),
* and the explanation / Q&A / output-summarization features.

Both the terminal UI (`main.py`) and the HTTP backend (`server.py`) import this
module so they run the exact same engine — and so the safety filter can never
be bypassed by a client.
"""

from __future__ import annotations

import abc
import base64
import json
import os
import platform
import re
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import anthropic

# ---------------------------------------------------------------------------
# Translation contract
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "You are a translation engine that converts a natural-language request "
    "into exactly ONE valid Linux bash command. You are NOT a chat assistant.\n"
    "\n"
    "SCOPE:\n"
    "- Translate any request that asks about, inspects, or changes THIS Linux "
    "system: files, processes, storage, logs, networking, ports, packages, "
    "services, users, hardware, and the like.\n"
    "- A request phrased as a QUESTION still counts as long as a command can "
    "answer it. Translate it into that command. For example: 'what errors hit "
    "the journal in the last hour?' -> journalctl -p err --since '1 hour ago' "
    "--no-pager; 'how much memory is free?' -> free -h; 'what is listening on "
    "port 80?' -> ss -tlnp 'sport = :80'.\n"
    "- Output exactly UNSUPPORTED ONLY when the request is not about this "
    "computer at all: greetings, small talk, questions about you or your "
    "identity (e.g. 'who are you'), or general-knowledge questions.\n"
    "- NEVER use a command such as 'echo' to answer a question, describe "
    "yourself, or converse. A command must PERFORM the operation or query — "
    "it must never be a way to reply in prose.\n"
    "\n"
    "STRICT OUTPUT RULES:\n"
    "- Output ONLY the raw command text.\n"
    "- Do NOT wrap it in markdown or backticks.\n"
    "- Do NOT add explanations, comments, or any preamble.\n"
    "- Do NOT include a leading '$' prompt character.\n"
    "- Produce a single command (pipes and '&&' are allowed; newlines are not).\n"
    "- Prefer read-only, non-interactive commands when the intent is ambiguous.\n"
    "- If the request cannot be expressed as a safe single command, output "
    "exactly: UNSUPPORTED"
)

# Sentinel value the model is instructed to emit when it cannot translate.
UNSUPPORTED_SENTINEL = "UNSUPPORTED"

# Token budgets — each task gets only what it needs, bounding latency and cost.
MAX_TOKENS = 300            # one shell command
EXPLAIN_MAX_TOKENS = 220    # short "what this does" note
ASK_MAX_TOKENS = 800        # prose Q&A answer
SUMMARY_MAX_TOKENS = 320    # plain-English answer derived from output

# Max characters of stdout/stderr sent to the model for summarization.
SUMMARY_OUTPUT_CHAR_LIMIT = 4000

# Seconds before a running command is forcibly terminated.
COMMAND_TIMEOUT_SECONDS = 60


# ---------------------------------------------------------------------------
# Environment grounding
#
# Appended to the translator prompt so generated commands fit THIS host — this
# is what stops the model assuming a GUI ~/Desktop exists on a headless server.
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _os_description() -> str:
    """One-line OS description, e.g. 'Linux 6.6 (Ubuntu 24.04 LTS)' (cached)."""
    system = platform.system() or "Unknown"
    release = platform.release()
    distro = ""
    try:
        with open("/etc/os-release", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("PRETTY_NAME="):
                    distro = line.split("=", 1)[1].strip().strip('"')
                    break
    except OSError:
        pass
    label = f"{system} {release}".strip()
    return f"{label} ({distro})" if distro else label


def environment_context() -> str:
    """Describe the host so the translator emits commands valid for THIS system."""
    return (
        "\n\nENVIRONMENT — generate commands that are valid for THIS system:\n"
        f"- OS: {_os_description()}\n"
        f"- Shell: {os.getenv('SHELL', '/bin/bash')} (a Unix/Linux bash shell)\n"
        f"- User: {os.getenv('USER') or os.getenv('USERNAME') or 'unknown'}\n"
        f"- Home directory: {os.path.expanduser('~')}\n"
        f"- Current working directory: {os.getcwd()}\n"
        "- This is a Linux/Unix shell: use POSIX paths and standard Linux tools.\n"
        "- Do NOT assume GUI folders such as ~/Desktop, ~/Documents, or ~/Downloads "
        "exist — on a Linux server they usually do not. Create any missing parent "
        "directories (e.g. use 'mkdir -p'), or operate relative to the home "
        "directory or an absolute path the user names."
    )


# ---------------------------------------------------------------------------
# Image attachments
#
# Users can attach images (a screenshot of an error, terminal output, a
# dashboard) to give the model more context. We carry images in a neutral,
# provider-agnostic form; each engine converts them to its own image schema in
# `_chat`. Whether the image is actually *understood* depends on the chosen
# model being vision-capable — nearly all current frontier models are, and a
# model that isn't will surface a provider error, which the UI reports.
# ---------------------------------------------------------------------------

# Extension -> MIME type for the formats every major vision model accepts.
SUPPORTED_IMAGE_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

# Per-image ceiling. Anthropic rejects images much larger than this, and it
# keeps request size (and cost) sane.
MAX_IMAGE_BYTES = 5 * 1024 * 1024


class ImageError(ValueError):
    """Raised when an image cannot be loaded (missing, too big, wrong type)."""


@dataclass(frozen=True)
class ImageAttachment:
    """A single image carried with a request, ready to send to any provider."""

    media_type: str
    data: str          # base64-encoded bytes
    source: str = ""   # original path, for display only


def load_image(path: str) -> ImageAttachment:
    """Load an image file into an :class:`ImageAttachment`.

    Raises:
        ImageError: If the path is missing, the type is unsupported, or the
            file is empty or larger than :data:`MAX_IMAGE_BYTES`.
    """
    resolved = Path(os.path.expanduser(path))
    ext = resolved.suffix.lower()
    if ext not in SUPPORTED_IMAGE_TYPES:
        supported = ", ".join(sorted(SUPPORTED_IMAGE_TYPES))
        raise ImageError(f"unsupported image type '{ext or path}' (supported: {supported})")
    try:
        raw = resolved.read_bytes()
    except OSError as exc:
        raise ImageError(f"could not read '{path}': {exc}") from exc
    if not raw:
        raise ImageError(f"'{path}' is empty")
    if len(raw) > MAX_IMAGE_BYTES:
        raise ImageError(
            f"'{path}' is {len(raw) // (1024 * 1024)} MB; the limit is "
            f"{MAX_IMAGE_BYTES // (1024 * 1024)} MB"
        )
    return ImageAttachment(
        media_type=SUPPORTED_IMAGE_TYPES[ext],
        data=base64.b64encode(raw).decode("ascii"),
        source=str(resolved),
    )


def user_message(text: str, images: "list[ImageAttachment] | None" = None) -> dict:
    """Build a user turn, attaching images as neutral content parts when present.

    With no images the content is a plain string (the simple, common path). With
    images it becomes a list of ``{"type": "text"|"image", ...}`` parts that each
    engine maps to its provider's format. Images come first so the model reads
    them before the instruction (the order Anthropic recommends).
    """
    if not images:
        return {"role": "user", "content": text}
    parts: list[dict] = [
        {"type": "image", "media_type": img.media_type, "data": img.data} for img in images
    ]
    if text:
        parts.append({"type": "text", "text": text})
    return {"role": "user", "content": parts}


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------

# Curated lists are the instant defaults of the "hybrid" model strategy; the
# live catalog can be fetched on demand. Verified current as of this date.
CURATED_MODELS_UPDATED = "2026-06-18"

# Engine "kinds" — which adapter drives the provider.
KIND_ANTHROPIC = "anthropic"
KIND_OPENAI = "openai"  # any OpenAI-compatible HTTP API


@dataclass(frozen=True)
class ProviderSpec:
    """Static description of a selectable provider."""

    key: str
    label: str
    kind: str
    api_key_env: str | None
    base_url: str | None
    default_model: str | None
    models: tuple[str, ...] = ()
    keyless: bool = False
    runtime_config: bool = False
    notes: str = ""


PROVIDERS: dict[str, ProviderSpec] = {
    "anthropic": ProviderSpec(
        key="anthropic",
        label="Anthropic (Claude)",
        kind=KIND_ANTHROPIC,
        api_key_env="ANTHROPIC_API_KEY",
        base_url=None,
        default_model="claude-sonnet-4-6",
        models=(
            "claude-sonnet-4-6",
            "claude-opus-4-8",
            "claude-haiku-4-5",
            "claude-opus-4-7",
            "claude-fable-5",
        ),
        notes="Claude family",
    ),
    "openai": ProviderSpec(
        key="openai",
        label="OpenAI (GPT)",
        kind=KIND_OPENAI,
        api_key_env="OPENAI_API_KEY",
        base_url=None,
        default_model="gpt-5.4-mini",
        models=("gpt-5.5", "gpt-5.5-pro", "gpt-5.4", "gpt-5.4-mini", "gpt-5.4-nano", "chat-latest"),
        notes="GPT-5 family",
    ),
    "gemini": ProviderSpec(
        key="gemini",
        label="Google (Gemini)",
        kind=KIND_OPENAI,
        api_key_env="GEMINI_API_KEY",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        default_model="gemini-3.5-flash",
        models=(
            "gemini-3.5-flash",
            "gemini-3.1-pro-preview",
            "gemini-3.1-flash-lite",
            "gemini-2.5-pro",
            "gemini-2.5-flash",
        ),
        notes="OpenAI-compatible endpoint",
    ),
    "openrouter": ProviderSpec(
        key="openrouter",
        label="OpenRouter",
        kind=KIND_OPENAI,
        api_key_env="OPENROUTER_API_KEY",
        base_url="https://openrouter.ai/api/v1",
        default_model="anthropic/claude-sonnet-4-6",
        models=(
            "anthropic/claude-sonnet-4-6",
            "anthropic/claude-opus-4-8",
            "openai/gpt-5.5",
            "google/gemini-3.5-flash",
            "deepseek/deepseek-v4-flash",
            "meta-llama/llama-3.3-70b-instruct",
        ),
        notes="Gateway — refresh for the full catalog",
    ),
    "ollama": ProviderSpec(
        key="ollama",
        label="Local (Ollama)",
        kind=KIND_OPENAI,
        api_key_env=None,
        base_url="http://localhost:11434/v1",
        default_model="llama3.3",
        models=("llama3.3", "qwen3", "gemma3", "mistral"),
        keyless=True,
        notes="No key — refresh lists installed models",
    ),
    "custom": ProviderSpec(
        key="custom",
        label="Custom (OpenAI-compatible)",
        kind=KIND_OPENAI,
        api_key_env=None,
        base_url=None,
        default_model=None,
        models=(),
        runtime_config=True,
        notes="You provide the URL + key",
    ),
}


def provider_is_ready(spec: ProviderSpec) -> bool:
    """Whether a provider can be used from the environment without prompting."""
    if spec.runtime_config:
        return bool(os.getenv("CUSTOM_BASE_URL"))
    if spec.keyless:
        return True
    return bool(spec.api_key_env and os.getenv(spec.api_key_env))


# ---------------------------------------------------------------------------
# User profile
# ---------------------------------------------------------------------------

EXPERIENCE_LEVELS: tuple[str, ...] = ("beginner", "intermediate", "expert")

EXPERIENCE_BLURB = {
    "beginner": "New to Linux / the command line",
    "intermediate": "Comfortable with common commands",
    "expert": "Fluent — I know my way around a shell",
}


@dataclass
class UserProfile:
    """Captured preferences that tailor explanations to the user."""

    experience: str = "intermediate"
    explain: bool = True

    def explain_system_prompt(self) -> str:
        """System prompt for explaining a command at this level."""
        base = (
            "You explain what a single Linux bash command does. Be accurate and "
            "concise. Output only the explanation — no preamble, no markdown "
            "headings, and do not just restate the command verbatim."
        )
        if self.experience == "beginner":
            return base + (
                " The reader is NEW to Linux. Use 1-3 short, plain-English "
                "sentences, avoid jargon (or define any term in simple words), "
                "and clearly flag anything that changes, moves, or deletes data."
            )
        if self.experience == "expert":
            return base + (
                " The reader is an expert. Give at most one short line, noting "
                "only non-obvious flags, side effects, or risks. Skip the obvious."
            )
        return base + (
            " The reader knows Linux basics. One or two sentences; common "
            "technical terms are fine without definition."
        )

    def ask_system_prompt(self) -> str:
        """System prompt for the Q&A / chat mode at this level."""
        base = (
            "You are a knowledgeable, friendly assistant for Linux, the command "
            "line, system administration, and closely related technical topics "
            "(shells, networking, servers, DevOps). Answer accurately and stay "
            "on these subjects; if asked something unrelated, answer briefly or "
            "steer back. You may show example commands, but make clear the user "
            "must run them — never imply you executed anything yourself."
        )
        if self.experience == "beginner":
            return base + (
                " The reader is new to Linux. Use simple language, define jargon, "
                "keep paragraphs short (bullet points are welcome), and include a "
                "concrete example when it helps."
            )
        if self.experience == "expert":
            return base + (
                " The reader is an expert. Be concise and technical, skip the "
                "basics, and get to the point."
            )
        return base + (
            " The reader knows Linux basics. Be clear and practical; common "
            "technical terms are fine without definition."
        )

    def summary_system_prompt(self) -> str:
        """System prompt for summarizing a command's output."""
        base = (
            "You are given a user's original request, the Linux command that "
            "was run, and its output. Answer the user's question directly in "
            "plain English, citing the actual numbers or values from the output "
            "(e.g. how much space is free). If the command failed (non-zero exit "
            "code or error output), say what went wrong and, if it's clear, how "
            "to fix it. Do NOT repeat the raw output or reproduce tables — "
            "summarize the relevant facts. Keep it to a short paragraph."
        )
        if self.experience == "beginner":
            return base + (
                " The reader is new to Linux: avoid jargon or define it simply, "
                "and point out anything important to know."
            )
        if self.experience == "expert":
            return base + " The reader is an expert: one or two terse sentences."
        return base + " The reader knows Linux basics: be clear and practical."

    def to_dict(self) -> dict[str, object]:
        """Serialize for on-disk persistence."""
        return {"experience": self.experience, "explain": self.explain}

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "UserProfile":
        """Rebuild from persisted data, falling back to safe defaults."""
        experience = str(data.get("experience", "intermediate"))
        if experience not in EXPERIENCE_LEVELS:
            experience = "intermediate"
        return cls(experience=experience, explain=bool(data.get("explain", True)))


def _config_dir() -> Path:
    """The XDG-respecting Sentinel config directory."""
    base = os.getenv("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config"
    )
    return Path(base) / "sentinel"


def profile_path() -> Path:
    """Return the path to the persisted profile."""
    return _config_dir() / "profile.json"


def load_profile() -> UserProfile | None:
    """Load the saved profile, or ``None`` if absent/unreadable."""
    try:
        with profile_path().open(encoding="utf-8") as handle:
            return UserProfile.from_dict(json.load(handle))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def save_profile(profile: UserProfile) -> bool:
    """Persist the profile. Returns ``True`` on success (best-effort)."""
    path = profile_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            json.dump(profile.to_dict(), handle, indent=2)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Persistent settings (provider, model, credentials)
#
# So launching Sentinel doesn't re-prompt for the provider, model, or API key
# every time. This file can contain API keys, so it is written with owner-only
# permissions (chmod 600). It lives outside the repo, under the user's config
# directory, and is never committed.
# ---------------------------------------------------------------------------

@dataclass
class Settings:
    """Remembered connection choices and credentials."""

    provider: str | None = None
    model: str | None = None
    api_keys: dict[str, str] = field(default_factory=dict)
    base_urls: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "model": self.model,
            "api_keys": self.api_keys,
            "base_urls": self.base_urls,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "Settings":
        return cls(
            provider=data.get("provider") or None,
            model=data.get("model") or None,
            api_keys=dict(data.get("api_keys") or {}),
            base_urls=dict(data.get("base_urls") or {}),
        )


def settings_path() -> Path:
    """Return the path to the persisted settings/credentials file."""
    return _config_dir() / "config.json"


def load_settings() -> Settings:
    """Load saved settings, or empty defaults if absent/unreadable."""
    try:
        with settings_path().open(encoding="utf-8") as handle:
            return Settings.from_dict(json.load(handle))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return Settings()


def save_settings(settings: Settings) -> bool:
    """Persist settings with owner-only permissions. Returns ``True`` on success."""
    path = settings_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            json.dump(settings.to_dict(), handle, indent=2)
        try:
            os.chmod(path, 0o600)  # best-effort; not all filesystems support it
        except OSError:
            pass
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Safety filter (read-only V1)
#
# Conservative backstop matching unambiguously destructive operations. It runs
# before any human confirmation; a false negative is still caught by the gate.
# ---------------------------------------------------------------------------

_DANGEROUS_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # NOTE: flag tokens are matched with `(?:^|\s)-`, NOT `\b-`. A space-to-hyphen
    # transition is not a word boundary, so `\b-` would never match a flag and the
    # rule would silently never fire.
    (re.compile(r"\brm\b(?=.*(?:(?:^|\s)-\w*r|\s--recursive\b))", re.I),
     "Recursive delete (rm -r / -rf) can wipe entire directory trees."),
    (re.compile(r"\brm\b.*(?:^|\s|/)\*", re.I),
     "Deleting with a wildcard (rm ... *) can erase everything in a directory."),
    (re.compile(r"\bmkfs\b", re.I),
     "mkfs formats a filesystem, irreversibly erasing the target device."),
    (re.compile(r"\bdd\b.*\bof=", re.I),
     "dd writing to a device/file can overwrite disks or partitions."),
    (re.compile(r">\s*/dev/(sd|nvme|hd|vd)\w*", re.I),
     "Redirecting output onto a block device corrupts the disk."),
    (re.compile(r"\b(shred|wipefs)\b", re.I),
     "shred/wipefs are designed to make data unrecoverable."),
    (re.compile(r"\b(mkswap|fdisk|parted|sgdisk)\b", re.I),
     "Partition/swap tools can repartition or wipe storage."),
    (re.compile(r":\s*\(\s*\)\s*\{.*\|.*&\s*\}", re.I),
     "Looks like a fork bomb, which can exhaust system resources."),
    (re.compile(r"\bchmod\b.*(?:^|\s)-\w*R\w*\b.*\b000\b"),
     "Recursive chmod 000 can lock you out of files and directories."),
    (re.compile(r">\s*/etc/(passwd|shadow|fstab)\b", re.I),
     "Overwriting critical system files can render the host unbootable."),
    (re.compile(r"\brm\b.*\s/\s*$|\brm\b.*\s/\*", re.I),
     "Deleting from the filesystem root is catastrophic."),
)


@dataclass(frozen=True)
class SafetyVerdict:
    """Outcome of screening a candidate command."""

    allowed: bool
    reason: str = ""


def screen_command(command: str) -> SafetyVerdict:
    """Check a command against the destructive-pattern blocklist."""
    for pattern, reason in _DANGEROUS_PATTERNS:
        if pattern.search(command):
            return SafetyVerdict(allowed=False, reason=reason)
    return SafetyVerdict(allowed=True)


# ---------------------------------------------------------------------------
# Command execution
# ---------------------------------------------------------------------------

# Special exit codes for results that did not run to completion.
EXIT_COULD_NOT_RUN = -1   # timeout, or the shell/command could not be launched
EXIT_CANCELLED = -2       # the user cancelled an in-progress command


@dataclass(frozen=True)
class CommandResult:
    """Captured result of running a command.

    ``exit_code`` is ``EXIT_COULD_NOT_RUN`` (-1) when the command could not run
    (launch failure or timeout) and ``EXIT_CANCELLED`` (-2) when the user
    cancelled it mid-run.
    """

    exit_code: int
    stdout: str = ""
    stderr: str = ""

    @property
    def cancelled(self) -> bool:
        return self.exit_code == EXIT_CANCELLED


def _kill_process_tree(proc: "subprocess.Popen") -> None:
    """Terminate a process and its children, escalating to a hard kill.

    Killing the whole group/tree (not just the immediate shell) ensures a
    cancelled pipeline or a command that spawned children is fully stopped and
    its pipes close, so output readers unblock promptly.
    """
    if os.name == "posix":
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass
        try:
            proc.wait(timeout=3)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass
    else:  # Windows: kill the whole tree so grandchildren release the pipes
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except OSError:
            proc.kill()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass


def run_command(command: str, cancel_event=None) -> CommandResult:
    """Run a command locally and capture its output (no rendering).

    The caller is responsible for screening and confirmation; this function only
    executes. ``shell=True`` is required for pipes/globs/redirections and is safe
    only because callers gate the command first — never pass unvetted input.

    Args:
        command: The (already screened + approved) command.
        cancel_event: Optional ``threading.Event``. When set mid-run, the process
            is terminated and the result is marked cancelled. Without it, this is
            a plain blocking run (the path the GUI backend uses).
    """
    if cancel_event is None:
        try:
            completed = subprocess.run(
                command, shell=True, capture_output=True, text=True,
                timeout=COMMAND_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            return CommandResult(EXIT_COULD_NOT_RUN, "",
                                 f"Timed out after {COMMAND_TIMEOUT_SECONDS}s — command terminated.")
        except OSError as exc:
            return CommandResult(EXIT_COULD_NOT_RUN, "", str(exc))
        return CommandResult(completed.returncode, completed.stdout, completed.stderr)

    # Cancellable path: reader threads drain the pipes (no deadlock) while we
    # poll for completion, cancellation, and timeout. poll() is non-blocking and
    # reliable on every platform (unlike communicate(timeout=) in a loop).
    popen_kwargs: dict = {
        "shell": True, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "text": True,
    }
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True  # own process group, so we can kill children
    else:
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        proc = subprocess.Popen(command, **popen_kwargs)
    except OSError as exc:
        return CommandResult(EXIT_COULD_NOT_RUN, "", str(exc))

    out_lines: list[str] = []
    err_lines: list[str] = []

    def _drain(stream, sink: list[str]) -> None:
        try:
            for line in iter(stream.readline, ""):
                sink.append(line)
        except (ValueError, OSError):
            pass
        finally:
            try:
                stream.close()
            except OSError:
                pass

    readers = [
        threading.Thread(target=_drain, args=(proc.stdout, out_lines), daemon=True),
        threading.Thread(target=_drain, args=(proc.stderr, err_lines), daemon=True),
    ]
    for reader in readers:
        reader.start()

    deadline = time.monotonic() + COMMAND_TIMEOUT_SECONDS
    note, code = "", None
    while True:
        rc = proc.poll()
        if rc is not None:
            code = rc
            break
        if cancel_event.is_set():
            note, code = "[cancelled by user]", EXIT_CANCELLED
            break
        if time.monotonic() > deadline:
            note, code = f"Timed out after {COMMAND_TIMEOUT_SECONDS}s.", EXIT_COULD_NOT_RUN
            break
        time.sleep(0.05)

    if code in (EXIT_CANCELLED, EXIT_COULD_NOT_RUN):
        _kill_process_tree(proc)

    for reader in readers:
        reader.join(timeout=2)

    stdout = "".join(out_lines)
    stderr = "".join(err_lines)
    if note:
        stderr = (stderr + ("\n" if stderr and not stderr.endswith("\n") else "")) + note
    return CommandResult(exit_code=code, stdout=stdout, stderr=stderr)


def _truncate(text: str, limit: int) -> str:
    """Trim text to ``limit`` chars, marking it when truncated."""
    text = text.rstrip()
    if len(text) <= limit:
        return text
    return text[:limit] + "\n…[output truncated]"


def _format_result_for_summary(result: CommandResult) -> str:
    """Render a :class:`CommandResult` as a compact block for the summarizer."""
    parts = [f"Exit code: {result.exit_code}"]
    if result.stdout.strip():
        parts.append("STDOUT:\n" + _truncate(result.stdout, SUMMARY_OUTPUT_CHAR_LIMIT))
    if result.stderr.strip():
        parts.append("STDERR:\n" + _truncate(result.stderr, SUMMARY_OUTPUT_CHAR_LIMIT))
    if not result.stdout.strip() and not result.stderr.strip():
        parts.append("(the command produced no output)")
    return "\n\n".join(parts)


def _sanitize_command(raw: str) -> str:
    """Strip markdown fences, prompt characters, and surrounding whitespace."""
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
        text = text.strip()
    text = re.sub(r"^\$\s+", "", text)
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


# ---------------------------------------------------------------------------
# Translation engines
# ---------------------------------------------------------------------------

class TranslationError(RuntimeError):
    """Raised when a provider call fails, with a user-friendly message."""


class Engine(abc.ABC):
    """A provider-backed translator/assistant.

    Subclasses implement the single ``_chat`` primitive (and ``list_models``);
    ``translate`` / ``explain`` / ``ask`` / ``summarize`` are shared on the base.
    The model can be changed in place via :attr:`model`.
    """

    def __init__(self, spec: ProviderSpec, model: str) -> None:
        self.spec = spec
        self.model = model

    @property
    def label(self) -> str:
        return self.spec.label

    @abc.abstractmethod
    def _chat(self, system: str, messages: list[dict], max_tokens: int) -> str:
        """Run one completion (system prompt + message turns) and return text.

        ``messages`` is a list of ``{"role", "content"}`` turns (``user`` /
        ``assistant``). ``content`` is normally a string, but may be a list of
        neutral content parts (text + images), which each engine maps to its
        provider's multimodal format.

        Raises:
            TranslationError: If the provider request fails.
        """

    def translate(
        self, request: str, images: "list[ImageAttachment] | None" = None
    ) -> str:
        """Translate a natural-language request into one bash command.

        Optional ``images`` (e.g. a screenshot of an error) are passed to the
        model as extra context; the output rules are unchanged.
        """
        messages = [user_message(request, images)]
        system = SYSTEM_PROMPT + environment_context()
        if images:
            system += (
                "\n\nThe user attached one or more images for context (such as a "
                "screenshot of an error, terminal output, or a dashboard). Use "
                "them to inform the single command you output. Still obey every "
                "output rule above."
            )
        return _sanitize_command(self._chat(system, messages, MAX_TOKENS))

    def explain(self, command: str, profile: UserProfile) -> str:
        """Explain a proposed command at the user's experience level."""
        messages = [{"role": "user", "content": command}]
        return self._chat(
            profile.explain_system_prompt(), messages, EXPLAIN_MAX_TOKENS
        ).strip()

    def ask(self, messages: list[dict[str, str]], profile: UserProfile) -> str:
        """Answer Linux/sysadmin questions at the user's experience level."""
        return self._chat(
            profile.ask_system_prompt(), messages, ASK_MAX_TOKENS
        ).strip()

    def summarize(
        self,
        request: str,
        command: str,
        result: CommandResult,
        profile: UserProfile,
    ) -> str:
        """Answer the user's original question from a command's output."""
        user = (
            f"Original request: {request}\n"
            f"Command run: {command}\n\n"
            f"{_format_result_for_summary(result)}"
        )
        messages = [{"role": "user", "content": user}]
        return self._chat(
            profile.summary_system_prompt(), messages, SUMMARY_MAX_TOKENS
        ).strip()

    @abc.abstractmethod
    def list_models(self) -> list[str]:
        """Fetch the provider's live model catalog (sorted)."""


class AnthropicEngine(Engine):
    """Translation engine backed by the native Anthropic SDK."""

    def __init__(self, spec: ProviderSpec, model: str, api_key: str) -> None:
        super().__init__(spec, model)
        self._client = anthropic.Anthropic(api_key=api_key)

    @staticmethod
    def _to_native(message: dict) -> dict:
        """Convert a neutral message to Anthropic's content-block format."""
        content = message["content"]
        if isinstance(content, str):
            return message
        blocks: list[dict] = []
        for part in content:
            if part["type"] == "image":
                blocks.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": part["media_type"],
                        "data": part["data"],
                    },
                })
            else:
                blocks.append({"type": "text", "text": part["text"]})
        return {"role": message["role"], "content": blocks}

    def _chat(self, system: str, messages: list[dict], max_tokens: int) -> str:
        try:
            # `thinking` is omitted: the safe default across every Claude model
            # (some, like Fable, reject `disabled`), and these prompts need no
            # extended reasoning. Anthropic takes `system` as a top-level arg.
            message = self._client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                messages=[self._to_native(m) for m in messages],
            )
        except anthropic.APIError as exc:
            raise TranslationError(str(exc)) from exc
        return "".join(b.text for b in message.content if b.type == "text")

    def list_models(self) -> list[str]:
        try:
            return sorted(m.id for m in self._client.models.list())
        except anthropic.APIError as exc:
            raise TranslationError(str(exc)) from exc


class OpenAICompatEngine(Engine):
    """Engine for any OpenAI-compatible provider (OpenAI, Gemini, OpenRouter, …)."""

    def __init__(
        self, spec: ProviderSpec, model: str, api_key: str, base_url: str | None
    ) -> None:
        super().__init__(spec, model)
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - environment guard
            raise TranslationError(
                "The 'openai' package is required for this provider. "
                "Install it with: pip install openai"
            ) from exc
        # openai>=1.x rejects a falsy api_key; keyless providers (Ollama) accept
        # any non-empty placeholder.
        self._client = OpenAI(api_key=api_key or "not-needed", base_url=base_url)

    @staticmethod
    def _to_native(message: dict) -> dict:
        """Convert a neutral message to OpenAI's multimodal content format."""
        content = message["content"]
        if isinstance(content, str):
            return message
        parts: list[dict] = []
        for part in content:
            if part["type"] == "image":
                url = f"data:{part['media_type']};base64,{part['data']}"
                parts.append({"type": "image_url", "image_url": {"url": url}})
            else:
                parts.append({"type": "text", "text": part["text"]})
        return {"role": message["role"], "content": parts}

    def _chat(self, system: str, messages: list[dict], max_tokens: int) -> str:
        try:
            response = self._client.chat.completions.create(
                model=self.model,
                max_tokens=max_tokens,
                messages=[
                    {"role": "system", "content": system},
                    *(self._to_native(m) for m in messages),
                ],
            )
        except Exception as exc:  # openai exception hierarchy varies by version
            raise TranslationError(str(exc)) from exc
        return response.choices[0].message.content or ""

    def list_models(self) -> list[str]:
        try:
            return sorted(m.id for m in self._client.models.list().data)
        except Exception as exc:
            raise TranslationError(str(exc)) from exc


# ---------------------------------------------------------------------------
# Engine construction
# ---------------------------------------------------------------------------

@dataclass
class Credentials:
    """Resolved connection details for a provider."""

    api_key: str = ""
    base_url: str | None = None


def create_engine(
    spec: ProviderSpec,
    model: str | None = None,
    api_key: str = "",
    base_url: str | None = None,
) -> Engine:
    """Construct an engine for a provider (non-interactive).

    Args:
        spec: The provider.
        model: Model ID; falls back to the provider default / first curated.
        api_key: API key (ignored for keyless providers).
        base_url: Base URL override (defaults to the provider's).

    Raises:
        TranslationError: If the engine/SDK cannot be initialized.
    """
    chosen_model = model or spec.default_model or (spec.models[0] if spec.models else "")
    resolved_base = base_url or spec.base_url
    if spec.kind == KIND_ANTHROPIC:
        return AnthropicEngine(spec, chosen_model, api_key)
    return OpenAICompatEngine(spec, chosen_model, api_key, resolved_base)
