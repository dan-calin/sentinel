# Sentinel

**A natural-language Linux server manager.** Describe what you want in plain
English; Sentinel translates it into a single shell command, shows you exactly
what it will run, and executes it **only after you approve it**. It can also
explain what a command does, answer Linux questions, and summarize a command's
output back into plain English — all tuned to your experience level.

> Safety first: Sentinel never runs anything on its own. Every command is
> screened against a destructive-command blocklist **and** gated behind an
> explicit `y/n` confirmation before it can touch your machine.

---

## What it can do

### 🧠 Plain English → one shell command
Type `how much free disk space do I have?` and Sentinel produces `df -h`. The
LLM acts strictly as a *translator*: it returns exactly one command — no chatter,
no markdown — and refuses anything that isn't a real Linux operation.

### 🛡️ Two layers of safety
1. **Blocklist filter** — destructive patterns (`rm -rf`, `mkfs`, `dd of=…`,
   fork bombs, overwriting `/etc/passwd`, formatting devices, …) are refused
   *before* you're ever asked, with a reason.
2. **Confirmation gate** — everything else is shown in a panel and waits for an
   explicit `y`. A bare Enter (or anything but `y`) means "no."

### 🤖 Bring your own AI — many providers, current models
Switch providers and models at any time. Built-in support for:

| Provider | How it connects | Example models |
|---|---|---|
| **Anthropic (Claude)** | native SDK | `claude-sonnet-4-6`, `claude-opus-4-8`, `claude-haiku-4-5` |
| **OpenAI (GPT)** | OpenAI API | `gpt-5.5`, `gpt-5.4-mini`, `gpt-5.4-nano` |
| **Google (Gemini)** | OpenAI-compatible endpoint | `gemini-3.5-flash`, `gemini-2.5-pro` |
| **OpenRouter** | gateway | `anthropic/claude-sonnet-4-6`, `deepseek/deepseek-v4-flash`, … |
| **Ollama (local)** | no API key | whatever you've pulled (`llama3.3`, `qwen3`, …) |
| **Custom** | any OpenAI-compatible URL + key | your endpoint |

Each provider ships a **curated, dated model list** that appears instantly, and
the model picker has a **`refresh`** action that pulls the provider's live
catalog on demand.

### 🎓 Explanations tuned to *you*
On first launch Sentinel asks about your Linux experience (beginner /
intermediate / expert) and whether you want explanations. Based on that:
- **Before** running, it shows a plain-English *"What this does"* (jargon-free
  for beginners, terse for experts — or off entirely).
- **After** running, it reads the output and answers your original question in
  plain English — *"You have ~949 GB free on `/`."* — and explains failures when
  a command errors.

Re-take the questionnaire anytime with the `profile` command.

### 💬 Ask mode (Q&A / chat)
Use `ask <question>` for a one-shot answer, or `ask` / `chat` for a multi-turn
conversation about Linux, the shell, and system administration. This is
**information only** — nothing is executed here — and answers adapt to your
experience level.

### ✨ Polished terminal UI
A welcome box (owl mark + live status), syntax-highlighted commands, clear
stdout/stderr panels, and a dark-blue theme. Press **Esc** to cancel any
in-progress task - a slow translation, a long-running command (its whole
process tree is stopped), or a chat reply.

---

## The interaction loop

```
type English ─▶ LLM translates ─▶ safety filter screens
                                        │
                              ┌─────────┴─────────┐
                           blocked            allowed
                              │                   │
                         show reason      "What this does" (optional)
                                                  │
                                          you approve? ── n ─▶ skip
                                                  │ y
                                              execute
                                                  │
                                          "In short" summary (optional)
```

---

## Getting started

Requires **Python 3.10+**.

```bash
cd ~/Linux_LLM
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Add the key(s) for the provider(s) you'll use:
cp .env.example .env   # then edit .env

python main.py
```

> On a fresh Ubuntu/WSL box you may need `sudo apt install -y python3-venv`
> first. Use the virtual environment rather than installing globally.

First run walks you through a short questionnaire, then a provider/model picker.
**After that, Sentinel remembers your provider, model, and API key** — launches
go straight to the prompt with no menus and no re-entering keys.

## Architecture

```
   core.py     ← all logic, no UI (engines, safety, profile, settings, exec)
      │
   main.py     ← the terminal UI (rich)

   gui/        ← experimental web UI over the same core (paused; see gui/README.md)
```

`core.py` is the single source of truth. A web GUI lives under `gui/` but is a
**paused work-in-progress** while its design is reworked - it is not part of the
shipped CLI.

---

## Commands

A leading slash is optional (`/ask` works too).

| Command | Does |
|---|---|
| *(plain English)* | Translate → review → approve → run a command |
| `ask <q>` / `ask` / `chat` | Ask Linux questions (answers only; nothing runs) |
| `provider` | Switch AI provider |
| `model` | Pick a model (curated list, or `r` to refresh the live catalog) |
| `profile` | Re-take the experience questionnaire |
| `help` | Show the command reference |
| `exit` | Quit (Ctrl-D also works) |

---

## Configuration

- **Remembered automatically.** Your chosen provider, model, and API key are
  saved to `~/.config/sentinel/config.json` (written `chmod 600`, outside the
  repo, never committed), so you set them once. Change them anytime with the
  `provider` / `model` commands.
- **API keys** can also come from `.env` (git-ignored); env values take
  precedence over the saved ones. A key you set via `.env` is never prompted.
- **Override per launch** with `SENTINEL_PROVIDER` and `SENTINEL_MODEL`.
- **Your profile** (experience level + explanation preference) is saved to
  `~/.config/sentinel/profile.json`.

---

## Roadmap

- [x] **Shared core** (`core.py`) so the CLI (and a future GUI) run one engine.
- [x] **Esc to cancel** any in-progress task (LLM call or running command).
- [x] **Remembered provider / model / key** across launches.
- [ ] **Web GUI** — paused under `gui/` while the design is reworked.
- [ ] Remote execution (SSH) and an allow-list mode beyond V1's local execution.

---

## Safety notes

- V1 runs commands on the **local** machine via the shell. The blocklist is a
  conservative backstop, **not** a sandbox — the confirmation gate is your real
  control. Read each command before approving.
- Smaller/local models follow the "one command only" and scope rules less
  reliably than frontier models; for the sharpest behavior use Claude, GPT, or
  Gemini.

---

## License

Sentinel is released under the **PolyForm Noncommercial License 1.0.0** (see
[`LICENSE`](LICENSE)). In short: you may use, modify, and share it freely for
**noncommercial** purposes, but you may **not sell it or use it for commercial
advantage**. Note this is a *source-available, noncommercial* license, not an
OSI-approved "open source" license (true open-source licenses cannot restrict
commercial use). For commercial licensing, contact the author.
