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
import datetime
import json
import os
import platform
import re
import shlex
import shutil
import signal
import subprocess
import threading
import time
import uuid
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


def is_unsupported(command: str) -> bool:
    """Whether a translated 'command' is really the UNSUPPORTED sentinel.

    Tolerant of the model's near-misses — case, surrounding punctuation, and
    common misspellings like 'UNSUPORTED' — so a typo isn't run as a literal
    command. Only matches a short, single-token UNSUP* word, never a real
    command that happens to contain the substring.
    """
    if not command:
        return True
    token = re.sub(r"[^a-z]", "", command.lower())
    return token == "unsupported" or (token.startswith("unsup") and len(token) <= 14)

# Prompt for generating an inverse ("undo") command — the fallback when a change
# wasn't snapshotted (services, packages, links, dirs). It SHOULD reverse clearly
# symmetrical state changes (e.g. stopping a daemon) and refuse the rest.
INVERT_SYSTEM = (
    "You are given a Linux command that was already executed. Output exactly ONE "
    "command that reverses its effect — an undo. Preserve a leading 'sudo' if the "
    "original had one.\n"
    "Reverse these (give the inverse command):\n"
    "- Filesystem: 'mkdir foo' -> 'rmdir foo'; 'mv a b' -> 'mv b a'; "
    "'ln -s t l' -> 'rm l'.\n"
    "- Services/daemons: 'systemctl stop X' -> 'systemctl start X' (and start->"
    "stop); 'systemctl disable X' -> 'systemctl enable X' (and enable->disable); "
    "'systemctl mask X' -> 'systemctl unmask X'; likewise 'service X stop' -> "
    "'service X start'.\n"
    "- Packages (best effort): 'apt install X' -> 'apt remove X'; "
    "'apt remove X' -> 'apt install X' (same for apt-get/dnf/yum).\n"
    "STRICT OUTPUT RULES: output ONLY the raw command — no markdown, no backticks, "
    "no prose, a single line.\n"
    "If it cannot be reliably undone — it deleted or overwrote data with no backup, "
    "made a network request, killed a process you can't know how to restart, or is "
    "read-only with nothing to undo — output exactly: UNSUPPORTED"
)
INVERT_MAX_TOKENS = 200

# Multi-step task / plan mode. The model first sketches a human-readable plan,
# then drives an agentic loop one command at a time — each still screened and
# approved by the human. Used by the CLI `plan` / `task` command.
PLAN_OUTLINE_MAX_TOKENS = 500
PLAN_STEP_MAX_TOKENS = 300
PLAN_OUTLINE_SYSTEM = (
    "You are planning a multi-step task on a Linux system. Given the user's "
    "goal, produce a short, readable numbered plan of the concrete steps you "
    "will take — for example: find the latest official release, download it, "
    "create a directory, configure it, run it (in the background), optionally "
    "open the firewall port. 3–8 steps, high-level, NO shell commands yet. Note "
    "where you'll need a choice from the user (version, port, directory). End "
    "with one line noting that every command will need their approval."
)
PLAN_STEP_SYSTEM = (
    "You are carrying out a multi-step task on a Linux system, ONE command at a "
    "time. You are given the goal and a log of the commands already run with "
    "their output. Decide the single next shell command that makes progress.\n"
    "OUTPUT RULES:\n"
    "- Output ONLY the raw command — no markdown, no backticks, no prose, one "
    "line (pipes and && allowed; no newlines).\n"
    "- Prefer official sources (the vendor's own site/API), and read versions/"
    "filenames from the previous output instead of guessing.\n"
    "- Long-running services MUST start in the background so the command returns "
    "— use 'nohup CMD >log 2>&1 &' or create/enable a systemd unit. Never start "
    "a foreground server that blocks.\n"
    "- Prefer non-interactive flags (-y, etc.).\n"
    "SPECIAL OUTPUTS (use these exactly):\n"
    "- When the goal is fully accomplished, output exactly: DONE\n"
    "- When you need a decision or value from the user before continuing, output "
    "exactly: ASK: <one clear question>\n"
    "- Never output a destructive command (rm -rf, mkfs, dd, …)."
)


def parse_plan_step(raw: str) -> tuple[str, str]:
    """Interpret a plan-step model output → ('done'|'ask'|'command', value)."""
    text = raw.strip()
    if not text:
        return ("done", "")
    first = text.split()[0].strip(".:").upper()
    if first == "DONE":
        return ("done", "")
    if text.upper().startswith("ASK:"):
        return ("ask", text[4:].strip())
    return ("command", _sanitize_command(text))

# Token budgets — each task gets only what it needs, bounding latency and cost.
MAX_TOKENS = 300            # one shell command
EXPLAIN_MAX_TOKENS = 220    # short "what this does" note
ASK_MAX_TOKENS = 800        # prose Q&A answer
SUMMARY_MAX_TOKENS = 320    # plain-English answer derived from output
DESCRIBE_MAX_TOKENS = 700   # image transcription + description (vision bridge)

# Vision bridge: when the active model can't see images, route them to one of
# these (same provider, vision-capable model) to transcribe + describe, then
# feed that text to the primary model. None => no default for that provider.
DEFAULT_VISION_MODELS: dict[str, str] = {
    "openrouter": "nvidia/nemotron-nano-12b-v2-vl:free",
    "anthropic": "claude-haiku-4-5",
    "openai": "gpt-5.4-mini",
    "gemini": "gemini-3.5-flash",
}

# System prompt for the bridge model: it acts as "eyes" for a text-only model.
VISION_DESCRIBE_SYSTEM = (
    "You are the eyes for another AI that cannot see images. Render the attached "
    "image(s) as faithful plain text so that other model can work from it:\n"
    "- Transcribe ALL visible text verbatim — commands, code, file paths, error "
    "messages, numbers, table values — exactly as shown. This is OCR; be precise.\n"
    "- Then briefly describe what the image is (a terminal, a UI, a chart, a "
    "diagram) and any layout or visual detail that matters.\n"
    "Do not answer the underlying request or take any action; only transcribe and "
    "describe. If several images are attached, label each Image #1, Image #2, …"
)

# Max characters of stdout/stderr sent to the model for summarization.
SUMMARY_OUTPUT_CHAR_LIMIT = 4000

# Reasoning / extended-thinking effort. None = off. Mapped per provider:
# Anthropic → thinking:{type:adaptive} + output_config:{effort}; OpenAI-compatible
# → reasoning_effort. A model that rejects these is detected and reasoning is
# disabled for the session (graceful fallback), so it never breaks a request.
REASONING_LEVELS: tuple[str, ...] = ("low", "medium", "high")

# When reasoning is on, the model spends tokens thinking before answering, so add
# headroom to the small per-task budgets. Additive (not a large flat floor) so it
# stays under tight free-tier credit caps that reject big max_tokens requests.
REASONING_HEADROOM = 2000

# Substrings in a provider error that indicate the reasoning params aren't supported.
_REASONING_ERROR_HINTS = (
    "thinking", "effort", "output_config", "reasoning", "budget_tokens",
    "adaptive", "max_completion_tokens",
)


def _looks_like_reasoning_error(message: str) -> bool:
    low = message.lower()
    return any(hint in low for hint in _REASONING_ERROR_HINTS)

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


def _windows_to_wsl(path: str) -> str | None:
    """Map a Windows path (``C:\\Users\\x.png``) to its WSL mount, or ``None``.

    A screenshot dragged/pasted from Windows arrives as a ``C:\\…`` path, but on
    WSL that drive is mounted under ``/mnt/c``. Returns the translated path so a
    pasted Windows screenshot actually loads instead of being mistaken for text.
    """
    match = re.match(r"^([A-Za-z]):[\\/](.*)$", path)
    if not match:
        return None
    drive, rest = match.group(1).lower(), match.group(2).replace("\\", "/")
    return f"/mnt/{drive}/{rest}"


def _resolve_image_path(path: str) -> Path | None:
    """Return the first existing file among the path and its WSL translation."""
    candidates = [os.path.expanduser(path)]
    translated = _windows_to_wsl(path)
    if translated:
        candidates.append(translated)
    for candidate in candidates:
        resolved = Path(candidate)
        if resolved.is_file():
            return resolved
    return None


def load_image(path: str) -> ImageAttachment:
    """Load an image file into an :class:`ImageAttachment`.

    Accepts both POSIX paths and Windows paths (the latter are mapped to their
    WSL ``/mnt`` mount), so a screenshot pasted from Windows loads correctly.

    Raises:
        ImageError: If the path is missing, the type is unsupported, or the
            file is empty or larger than :data:`MAX_IMAGE_BYTES`.
    """
    ext = Path(path).suffix.lower()
    if ext not in SUPPORTED_IMAGE_TYPES:
        supported = ", ".join(sorted(SUPPORTED_IMAGE_TYPES))
        raise ImageError(f"unsupported image type '{ext or path}' (supported: {supported})")
    resolved = _resolve_image_path(path)
    if resolved is None:
        raise ImageError(f"could not find '{path}'")
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
    # Optional override for the vision-bridge model (else a per-provider default).
    vision_model: str | None = None
    # Reasoning effort: None (off) or one of REASONING_LEVELS.
    reasoning: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "model": self.model,
            "api_keys": self.api_keys,
            "base_urls": self.base_urls,
            "vision_model": self.vision_model,
            "reasoning": self.reasoning,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "Settings":
        reasoning = data.get("reasoning") or None
        if reasoning not in (None, *REASONING_LEVELS):
            reasoning = None
        return cls(
            provider=data.get("provider") or None,
            model=data.get("model") or None,
            api_keys=dict(data.get("api_keys") or {}),
            base_urls=dict(data.get("base_urls") or {}),
            vision_model=data.get("vision_model") or None,
            reasoning=reasoning,
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
# Fleet: hosts and remote agents
#
# A controller (the CLI) keeps a registry of hosts, each running a Sentinel
# agent (see agent/server.py). "local" is implicit and always available. Remote
# hosts are reached over HTTP with a read token (diagnostics) and an optional
# admin token (execute). Tokens live in hosts.json, written chmod 600.
# ---------------------------------------------------------------------------

LOCAL_HOST = "local"


@dataclass
class HostConfig:
    """A managed host: where its agent lives and the tokens to reach it."""

    name: str
    base_url: str
    read_token: str = ""
    admin_token: str = ""
    allow_execute: bool = True   # controller-side lever: off = treat as read-only

    def to_dict(self) -> dict:
        return {
            "name": self.name, "base_url": self.base_url,
            "read_token": self.read_token, "admin_token": self.admin_token,
            "allow_execute": self.allow_execute,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "HostConfig":
        return cls(
            name=str(data.get("name", "")), base_url=str(data.get("base_url", "")),
            read_token=str(data.get("read_token", "")),
            admin_token=str(data.get("admin_token", "")),
            allow_execute=bool(data.get("allow_execute", True)),
        )

    @property
    def can_execute(self) -> bool:
        """Whether the controller will run commands here (lever on + token set)."""
        return self.allow_execute and bool(self.admin_token)


def hosts_path() -> Path:
    return _config_dir() / "hosts.json"


def load_hosts() -> dict[str, HostConfig]:
    """Load the host registry (name -> HostConfig); empty if none saved."""
    try:
        with hosts_path().open(encoding="utf-8") as handle:
            data = json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    hosts = {}
    for item in data.get("hosts", []):
        host = HostConfig.from_dict(item)
        if host.name and host.name != LOCAL_HOST:
            hosts[host.name] = host
    return hosts


def save_hosts(hosts: dict[str, HostConfig]) -> bool:
    """Persist the host registry with owner-only permissions (holds tokens)."""
    path = hosts_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            json.dump({"hosts": [h.to_dict() for h in hosts.values()]}, handle, indent=2)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return True
    except OSError:
        return False


class RemoteError(RuntimeError):
    """Raised when talking to a remote agent fails, with a readable message."""


class RemoteAgent:
    """Thin HTTP client for a Sentinel agent (stdlib only)."""

    def __init__(self, host: HostConfig, timeout: float = 30.0) -> None:
        self.host = host
        self.timeout = timeout

    def _request(self, method: str, path: str, token: str, body: dict | None = None) -> dict:
        import urllib.error
        import urllib.request

        url = self.host.base_url.rstrip("/") + path
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode() or "{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            try:
                detail = json.loads(detail).get("detail", detail)
            except json.JSONDecodeError:
                pass
            raise RemoteError(f"{exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RemoteError(f"could not reach {self.host.name} ({url}): {exc.reason}") from exc
        except (TimeoutError, OSError) as exc:
            raise RemoteError(f"could not reach {self.host.name}: {exc}") from exc

    def health(self) -> dict:
        return self._request("GET", "/health", token="")

    def diagnostic(self, name: str, params: dict | None = None) -> str:
        body = {"params": params or {}}
        result = self._request("POST", f"/diagnostics/{name}", self.host.read_token, body)
        return result.get("output", "")

    def execute(self, command: str) -> "CommandResult":
        result = self._request("POST", "/execute", self.host.admin_token, {"command": command})
        return CommandResult(
            exit_code=int(result.get("exit_code", EXIT_COULD_NOT_RUN)),
            stdout=result.get("stdout", ""), stderr=result.get("stderr", ""),
        )

    def get_monitor(self) -> dict:
        return self._request("GET", "/monitor", self.host.read_token)

    def set_monitor(self, changes: dict) -> dict:
        return self._request("POST", "/monitor", self.host.admin_token, changes)

    def run_monitor(self) -> list[dict]:
        return self._request("POST", "/monitor/run", self.host.admin_token).get("alerts", [])

    def get_alerts(self, limit: int = 100) -> list[dict]:
        return self._request("GET", f"/alerts?limit={limit}", self.host.read_token).get("alerts", [])


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
# Command journal, checkpoints, and undo
#
# A layered undo: before a command that changes files, snapshot the paths it
# will touch so we can restore them exactly; for anything we can't snapshot, an
# engine generates a best-effort inverse command that still goes through the
# normal safety gate. Every executed command is recorded in a journal so the
# user can review history and undo the last change.
#
# This is a convenience and an audit trail, NOT disaster recovery: not every
# command is reversible (you can't un-restart a service or un-send a request),
# and snapshots are size-capped. The confirmation gate remains the real control.
# ---------------------------------------------------------------------------

# Commands whose first word means "this changes the filesystem". Used to decide
# whether to snapshot before running and to flag the entry as mutating.
_MUTATING_TOKENS = frozenset({
    "rm", "mv", "cp", "dd", "touch", "mkdir", "rmdir", "tee", "install", "ln",
    "truncate", "chmod", "chown", "chgrp", "shred", "mkfifo", "unlink", "rsync",
})

# Wrappers to skip when finding the real command word in a segment.
_COMMAND_WRAPPERS = frozenset({"sudo", "env", "nice", "nohup", "time", "doas"})

# State changes with no file to snapshot but which ARE reversible via an inverse
# command (e.g. a stopped daemon -> start it). Flagged mutating so `undo` sees
# them; subcommand verbs keep read-only queries (systemctl status) from counting.
_SERVICE_MUTATING_VERBS = frozenset({
    "start", "stop", "restart", "reload", "reload-or-restart", "try-restart",
    "enable", "disable", "mask", "unmask", "kill", "isolate", "set-default",
})
_PKG_MANAGERS = frozenset({"apt", "apt-get", "dnf", "yum", "zypper", "snap", "pacman"})
_PKG_MUTATING_VERBS = frozenset({
    "install", "remove", "purge", "autoremove", "reinstall",
    "upgrade", "full-upgrade", "dist-upgrade",
})


def _is_state_change(cmd: str, rest: list[str]) -> bool:
    """Whether a non-file command changes reversible system state (service/pkg)."""
    verbs = {tok for tok in rest if not tok.startswith("-")}
    if cmd == "systemctl":
        return bool(verbs & _SERVICE_MUTATING_VERBS)
    if cmd == "service":
        return bool(set(rest) & _SERVICE_MUTATING_VERBS)
    if cmd in _PKG_MANAGERS:
        if cmd == "pacman":  # flag-style: -S install, -R remove, -U upgrade
            return any(tok.startswith(("-S", "-R", "-U")) for tok in rest)
        return bool(verbs & _PKG_MUTATING_VERBS)
    return False

# Don't copy snapshots larger than this (per path) — keeps undo cheap and safe.
MAX_CHECKPOINT_BYTES = 50 * 1024 * 1024


def _new_id() -> str:
    """A sortable, unique id, e.g. '20260619-160501-a1b2c3'."""
    return time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]


def _now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _path_size(path: Path) -> int:
    """Total bytes of a file or directory tree (best-effort)."""
    if path.is_file():
        return path.stat().st_size
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def _looks_like_path(token: str) -> bool:
    """Heuristic: is this argument a file/dir path we could snapshot?"""
    if not token or token.startswith("-"):
        return False
    if any(c in token for c in "|<>;&$`*?"):  # shell metachars / globs → not a literal path
        return False
    try:
        candidate = Path(os.path.expanduser(token))
    except (ValueError, OSError):
        return False
    # Accept it if it exists, or if its parent does (a soon-to-be-created file).
    parent = candidate.parent
    return candidate.exists() or (parent != candidate and parent.exists())


def classify_command(command: str) -> tuple[bool, list[str]]:
    """Return (is_mutating, candidate_target_paths) for a command.

    Heuristic and deliberately conservative: it recognizes redirections, in-place
    ``sed``, and known file-mutating commands, and extracts path-like arguments.
    Imperfect parsing is fine — unsnapshotted changes fall back to the inverse
    command, and undo always asks for confirmation.
    """
    mutating = False
    paths: list[str] = []

    for match in re.finditer(r">>?\s*([^\s;|&>]+)", command):  # redirect targets
        mutating = True
        paths.append(match.group(1))

    for segment in re.split(r"\|\||&&|[|;&]", command):
        try:
            tokens = shlex.split(segment)
        except ValueError:
            tokens = segment.split()
        index = 0
        while index < len(tokens) and (
            tokens[index] in _COMMAND_WRAPPERS or (index == 0 and "=" in tokens[index])
        ):
            index += 1
        if index >= len(tokens):
            continue
        cmd = os.path.basename(tokens[index])
        rest = tokens[index + 1:]
        is_sed_inplace = cmd == "sed" and any(
            t == "--in-place" or t.startswith("--in-place=") or re.match(r"-[a-zA-Z]*i", t)
            for t in rest
        )
        if cmd in _MUTATING_TOKENS or is_sed_inplace:
            mutating = True
            paths.extend(tok for tok in rest if _looks_like_path(tok))
        elif _is_state_change(cmd, rest):
            mutating = True  # reversible via inverse command; nothing to snapshot

    seen, unique = set(), []
    for path in paths:
        absolute = os.path.abspath(os.path.expanduser(path))
        if absolute not in seen:
            seen.add(absolute)
            unique.append(path)
    return mutating, unique


@dataclass
class CheckpointFile:
    """One path captured in a checkpoint."""

    original: str
    backup: str | None        # path to the saved copy, or None if not saved
    existed_before: bool      # False => the command created it (undo removes it)
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "original": self.original, "backup": self.backup,
            "existed_before": self.existed_before, "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CheckpointFile":
        return cls(
            original=data["original"], backup=data.get("backup"),
            existed_before=bool(data.get("existed_before", True)),
            note=data.get("note", ""),
        )


@dataclass
class Checkpoint:
    """A pre-command snapshot of the paths a command was about to touch."""

    id: str
    timestamp: str
    command: str
    label: str
    files: list[CheckpointFile]

    def to_dict(self) -> dict:
        return {
            "id": self.id, "timestamp": self.timestamp, "command": self.command,
            "label": self.label, "files": [f.to_dict() for f in self.files],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Checkpoint":
        return cls(
            id=data["id"], timestamp=data.get("timestamp", ""),
            command=data.get("command", ""), label=data.get("label", ""),
            files=[CheckpointFile.from_dict(f) for f in data.get("files", [])],
        )

    @property
    def saved_count(self) -> int:
        return sum(1 for f in self.files if f.backup)


def _checkpoints_dir() -> Path:
    return _config_dir() / "checkpoints"


def create_checkpoint(
    command: str, target_paths: list[str], label: str = "auto"
) -> Checkpoint | None:
    """Snapshot the existing target paths before a command runs.

    Existing files/dirs are copied into the checkpoint directory; paths that
    don't exist yet are recorded so undo can delete what the command creates.
    Returns ``None`` when there is nothing to track.
    """
    if not target_paths:
        return None
    checkpoint_id = _new_id()
    directory = _checkpoints_dir() / checkpoint_id
    files: list[CheckpointFile] = []

    for index, raw in enumerate(target_paths):
        original = Path(os.path.expanduser(raw))
        existed = original.exists()
        backup, note = None, ""
        if existed:
            try:
                if _path_size(original) > MAX_CHECKPOINT_BYTES:
                    note = f"not saved (larger than {MAX_CHECKPOINT_BYTES // (1024 * 1024)} MB)"
                else:
                    directory.mkdir(parents=True, exist_ok=True)
                    dest = directory / f"{index}_{original.name}"
                    if original.is_dir():
                        shutil.copytree(original, dest, symlinks=True)
                    else:
                        shutil.copy2(original, dest)
                    backup = str(dest)
            except OSError as exc:
                note = f"backup failed: {exc}"
        files.append(CheckpointFile(
            original=str(original.absolute()), backup=backup,
            existed_before=existed, note=note,
        ))

    checkpoint = Checkpoint(
        id=checkpoint_id, timestamp=_now_iso(), command=command, label=label, files=files,
    )
    try:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "meta.json").write_text(json.dumps(checkpoint.to_dict(), indent=2))
    except OSError:
        pass
    return checkpoint


def load_checkpoint(checkpoint_id: str) -> Checkpoint | None:
    """Load a checkpoint by id, or ``None`` if absent/unreadable."""
    try:
        data = json.loads((_checkpoints_dir() / checkpoint_id / "meta.json").read_text())
        return Checkpoint.from_dict(data)
    except (OSError, json.JSONDecodeError, KeyError):
        return None


def list_checkpoints() -> list[Checkpoint]:
    """All saved checkpoints, newest first."""
    directory = _checkpoints_dir()
    if not directory.is_dir():
        return []
    found = []
    for child in directory.iterdir():
        if child.is_dir():
            checkpoint = load_checkpoint(child.name)
            if checkpoint:
                found.append(checkpoint)
    found.sort(key=lambda c: c.id, reverse=True)
    return found


def restore_checkpoint(checkpoint: Checkpoint) -> list[str]:
    """Restore a checkpoint: bring back saved files, remove created ones.

    Returns a human-readable log of what it did.
    """
    log: list[str] = []
    for entry in checkpoint.files:
        original = Path(entry.original)
        if entry.existed_before:
            if entry.backup and Path(entry.backup).exists():
                source = Path(entry.backup)
                try:
                    if source.is_dir():
                        if original.exists():
                            shutil.rmtree(original, ignore_errors=True)
                        shutil.copytree(source, original, symlinks=True)
                    else:
                        original.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(source, original)
                    log.append(f"restored {original}")
                except OSError as exc:
                    log.append(f"could not restore {original}: {exc}")
            else:
                log.append(f"no saved copy of {original} ({entry.note or 'not saved'})")
        else:  # the command created it — undo by removing it
            if original.exists():
                try:
                    if original.is_dir():
                        shutil.rmtree(original)
                    else:
                        original.unlink()
                    log.append(f"removed {original} (created by the command)")
                except OSError as exc:
                    log.append(f"could not remove {original}: {exc}")
            else:
                log.append(f"{original} was already gone")
    return log


@dataclass
class JournalEntry:
    """One executed command, recorded for history and undo."""

    id: str
    timestamp: str
    request: str
    command: str
    cwd: str
    exit_code: int
    mutating: bool
    checkpoint_id: str | None = None
    undone: bool = False
    host: str = LOCAL_HOST

    def to_dict(self) -> dict:
        return {
            "id": self.id, "timestamp": self.timestamp, "request": self.request,
            "command": self.command, "cwd": self.cwd, "exit_code": self.exit_code,
            "mutating": self.mutating, "checkpoint_id": self.checkpoint_id,
            "undone": self.undone, "host": self.host,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "JournalEntry":
        return cls(
            id=data["id"], timestamp=data.get("timestamp", ""),
            request=data.get("request", ""), command=data.get("command", ""),
            cwd=data.get("cwd", ""), exit_code=int(data.get("exit_code", 0)),
            mutating=bool(data.get("mutating", False)),
            checkpoint_id=data.get("checkpoint_id"), undone=bool(data.get("undone", False)),
            host=data.get("host") or LOCAL_HOST,
        )


def _journal_path() -> Path:
    return _config_dir() / "history.jsonl"


def record_command(
    request: str, command: str, cwd: str, exit_code: int,
    mutating: bool, checkpoint_id: str | None, host: str = LOCAL_HOST,
) -> JournalEntry | None:
    """Append an executed command to the journal (best-effort)."""
    entry = JournalEntry(
        id=_new_id(), timestamp=_now_iso(), request=request, command=command,
        cwd=cwd, exit_code=exit_code, mutating=mutating, checkpoint_id=checkpoint_id,
        host=host,
    )
    try:
        path = _journal_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry.to_dict()) + "\n")
    except OSError:
        return None
    return entry


def load_journal(limit: int | None = None) -> list[JournalEntry]:
    """Load journal entries, oldest first. ``limit`` keeps only the most recent."""
    try:
        lines = _journal_path().read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    entries = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(JournalEntry.from_dict(json.loads(line)))
        except (json.JSONDecodeError, KeyError):
            continue
    return entries[-limit:] if limit else entries


def _rewrite_journal(entries: list[JournalEntry]) -> None:
    try:
        with _journal_path().open("w", encoding="utf-8") as handle:
            for entry in entries:
                handle.write(json.dumps(entry.to_dict()) + "\n")
    except OSError:
        pass


def mark_undone(entry_id: str) -> None:
    """Flag a journal entry as undone."""
    entries = load_journal()
    for entry in entries:
        if entry.id == entry_id:
            entry.undone = True
    _rewrite_journal(entries)


def last_undoable() -> JournalEntry | None:
    """The most recent change-making command that hasn't been undone."""
    for entry in reversed(load_journal()):
        if entry.mutating and not entry.undone:
            return entry
    return None


def find_entry(entry_id: str) -> JournalEntry | None:
    """Find a journal entry by id (prefix match allowed)."""
    matches = [e for e in load_journal() if e.id == entry_id or e.id.startswith(entry_id)]
    return matches[-1] if matches else None


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
        self.reasoning: str | None = None  # None = off; else a REASONING_LEVELS value

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
        self, request: str, images: "list[ImageAttachment] | None" = None,
        history: "list[dict] | None" = None,
    ) -> str:
        """Translate a natural-language request into one bash command.

        Optional ``images`` (e.g. a screenshot of an error) are passed to the
        model as extra context; the output rules are unchanged. Optional
        ``history`` is prior ``{"role", "content"}`` turns (earlier requests and
        the commands produced) so a follow-up like "what about the CPU?" or "now
        restart it" resolves against the conversation.
        """
        messages = [*(history or []), user_message(request, images)]
        system = SYSTEM_PROMPT + environment_context()
        if images:
            system += (
                "\n\nThe user attached one or more images for context (such as a "
                "screenshot of an error, terminal output, or a dashboard). Use "
                "them to inform the single command you output. Still obey every "
                "output rule above."
            )
        return _sanitize_command(self._chat(system, messages, MAX_TOKENS))

    def describe_images(
        self, request: str, images: "list[ImageAttachment]"
    ) -> str:
        """Transcribe + describe attached images (the vision-bridge role).

        Used when the primary model is text-only: a vision-capable engine turns
        the image(s) into plain text that the primary model can then read.
        """
        prompt = request.strip() or "Describe the attached image(s)."
        user = (
            f'A text-only model received this request: "{prompt}". '
            "Transcribe and describe the attached image(s) so it can respond."
        )
        messages = [user_message(user, images)]
        return self._chat(VISION_DESCRIBE_SYSTEM, messages, DESCRIBE_MAX_TOKENS).strip()

    def plan_outline(self, goal: str) -> str:
        """Sketch a short, human-readable plan for a multi-step goal."""
        messages = [{"role": "user", "content": goal}]
        system = PLAN_OUTLINE_SYSTEM + environment_context()
        return self._chat(system, messages, PLAN_OUTLINE_MAX_TOKENS).strip()

    def plan_step(self, goal: str, transcript: list[dict]) -> str:
        """Decide the next command for a task, given what's run so far.

        ``transcript`` is a list of ``{"command", "exit_code", "output"}`` (and
        optional ``{"note": ...}``) entries. Returns raw model text; interpret it
        with :func:`parse_plan_step` (command / DONE / ASK).
        """
        lines = [f"Goal: {goal}", "", "Steps so far:"]
        if not transcript:
            lines.append("(none yet)")
        for entry in transcript[-10:]:  # bound tokens to the recent steps
            if "note" in entry:
                lines.append(f"- {entry['note']}")
                continue
            lines.append(f"$ {entry['command']}  (exit {entry['exit_code']})")
            out = _truncate(entry.get("output", ""), 400)
            if out:
                lines.append(out)
        lines.append("")
        lines.append("Output the next command, or DONE, or ASK: <question>.")
        messages = [{"role": "user", "content": "\n".join(lines)}]
        system = PLAN_STEP_SYSTEM + environment_context()
        return self._chat(system, messages, PLAN_STEP_MAX_TOKENS).strip()

    def invert(self, command: str) -> str:
        """Produce a best-effort command that undoes ``command``.

        Returns :data:`UNSUPPORTED_SENTINEL` when the command cannot be reliably
        reversed (deleted data, restarted a service, made a network request).
        The result is still screened and confirmed before it can run.
        """
        messages = [{"role": "user", "content": command}]
        return _sanitize_command(self._chat(INVERT_SYSTEM, messages, INVERT_MAX_TOKENS))

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

    def list_vision_models(self, free_only: bool = False) -> list[tuple[str, bool]]:
        """Return ``(model_id, is_free)`` for image-capable models, best-effort.

        Empty when the provider exposes no modality metadata (the caller then
        falls back to letting the user type a model ID).
        """
        return []


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
        kwargs: dict = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [self._to_native(m) for m in messages],
        }
        if self.reasoning:
            # Adaptive thinking + effort (current Claude models reject budget_tokens).
            # Give the answer headroom since thinking consumes tokens.
            kwargs["thinking"] = {"type": "adaptive"}
            kwargs["output_config"] = {"effort": self.reasoning}
            kwargs["max_tokens"] = max_tokens + REASONING_HEADROOM
        try:
            message = self._client.messages.create(**kwargs)
        except anthropic.APIError as exc:
            if self.reasoning and _looks_like_reasoning_error(str(exc)):
                self.reasoning = None  # this model can't reason — disable for the session
                return self._chat(system, messages, max_tokens)
            raise TranslationError(str(exc)) from exc
        # Only text blocks; any thinking blocks are ignored.
        return "".join(b.text for b in message.content if b.type == "text")

    def list_models(self) -> list[str]:
        try:
            return sorted(m.id for m in self._client.models.list())
        except anthropic.APIError as exc:
            raise TranslationError(str(exc)) from exc

    def list_vision_models(self, free_only: bool = False) -> list[tuple[str, bool]]:
        # Every current Claude model accepts images; none are free.
        if free_only:
            return []
        return [(model, False) for model in self.spec.models]


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
        kwargs: dict = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                *(self._to_native(m) for m in messages),
            ],
        }
        if self.reasoning:
            kwargs["reasoning_effort"] = self.reasoning
            kwargs["max_tokens"] = max_tokens + REASONING_HEADROOM
        try:
            response = self._client.chat.completions.create(**kwargs)
        except Exception as exc:  # openai exception hierarchy varies by version
            if self.reasoning and _looks_like_reasoning_error(str(exc)):
                self.reasoning = None  # this model can't reason — disable for the session
                return self._chat(system, messages, max_tokens)
            raise TranslationError(str(exc)) from exc
        return response.choices[0].message.content or ""

    def list_models(self) -> list[str]:
        try:
            return sorted(m.id for m in self._client.models.list().data)
        except Exception as exc:
            raise TranslationError(str(exc)) from exc

    def list_vision_models(self, free_only: bool = False) -> list[tuple[str, bool]]:
        """Parse the catalog's per-model modality + pricing (e.g. OpenRouter).

        Providers that don't return an ``architecture`` block (plain OpenAI)
        yield nothing, so the caller falls back to manual entry.
        """
        try:
            listing = self._client.models.list()
        except Exception as exc:
            raise TranslationError(str(exc)) from exc

        out: list[tuple[str, bool]] = []
        for model in getattr(listing, "data", listing) or []:
            arch = getattr(model, "architecture", None)
            modalities = arch.get("input_modalities") if isinstance(arch, dict) else None
            if not modalities or "image" not in modalities:
                continue
            model_id = model.id
            is_free = model_id.endswith(":free")
            pricing = getattr(model, "pricing", None)
            if not is_free and isinstance(pricing, dict):
                try:
                    is_free = (
                        float(pricing.get("prompt") or 1) == 0
                        and float(pricing.get("completion") or 1) == 0
                    )
                except (TypeError, ValueError):
                    is_free = False
            if free_only and not is_free:
                continue
            out.append((model_id, is_free))
        out.sort(key=lambda item: (not item[1], item[0]))  # free first, then alphabetical
        return out


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
