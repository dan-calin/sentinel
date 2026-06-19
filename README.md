# Sentinel

A natural-language Linux server manager. Describe what you want in plain
English; Sentinel translates it into the right shell command, shows you exactly
what it will run, and executes it **only after you approve it**. Then it reads
the output back to you in plain English.

It is built for real server work, the kind that means hunting for the right
flags: checking capacity, parsing error logs, watching game-server processes.
Sentinel is more than a thin LLM wrapper around a terminal: it knows the flags,
**refuses destructive commands**, and **interprets messy output** so you do not
have to, all tuned to your experience level.

![Sentinel CLI demo](assets/cli-demo.gif?v=2)

> **Safety first.** Sentinel never runs anything on its own. Every command is
> screened against a destructive-command blocklist **and** gated behind an
> explicit `y/n` confirmation before it can touch your machine.

---

## Features

**Plain English to the right command.**
Ask *what errors hit the system journal in the last hour?* and Sentinel runs
`journalctl -p err --since "1 hour ago" --no-pager` - the exact flags you would
otherwise have to look up. Ask *which processes are using the most memory?* and
it runs `ps -eo pid,comm,%mem,rss --sort=-%mem | head`. It returns exactly one
command, with no chatter or markdown, and refuses anything that is not a real
Linux operation.

**Two layers of safety.**
A blocklist filter refuses destructive patterns (`rm -rf`, `mkfs`, `dd of=…`,
fork bombs, formatting devices, overwriting `/etc/passwd`, and the like)
*before* you are ever asked, with a reason. Everything else is shown for review
and waits for an explicit `y`; anything else means no.

**Bring your own AI.**
Switch providers and models at any time. Each provider ships a curated, dated
model list that appears instantly, plus a `refresh` action that pulls the live
catalog on demand.

| Provider | How it connects | Example models |
|---|---|---|
| Anthropic (Claude) | native SDK | `claude-sonnet-4-6`, `claude-opus-4-8`, `claude-haiku-4-5` |
| OpenAI (GPT) | OpenAI API | `gpt-5.5`, `gpt-5.4-mini`, `gpt-5.4-nano` |
| Google (Gemini) | OpenAI-compatible endpoint | `gemini-3.5-flash`, `gemini-2.5-pro` |
| OpenRouter | gateway | `anthropic/claude-sonnet-4-6`, `deepseek/deepseek-v4-flash`, … |
| Ollama (local) | no API key | whatever you have pulled (`llama3.3`, `qwen3`, …) |
| Custom | any OpenAI-compatible URL + key | your endpoint |

**Explanations tuned to you.**
On first launch Sentinel asks about your Linux experience (beginner,
intermediate, or expert) and whether you want explanations. Before running, it
can show a plain-English description of the command; after running, it reads the
output and answers your original question, turning a wall of `journalctl` errors
into something like *"Most failures are the Bluetooth service failing to start;
the rest are harmless timeouts."* It explains non-obvious failures too. Re-take
the questionnaire anytime with `profile`.

**Ask mode.**
Use `ask <question>` for a one-shot answer, or `ask` / `chat` for a multi-turn
conversation about Linux, the shell, and system administration. This is
information only; nothing is executed, and answers adapt to your experience level.

**Attach an image for context.**
Paste a screenshot, press **Ctrl-V** to grab the clipboard image, or include an
image path in your prompt — Sentinel sends it to the model as context. A
pasted file path is replaced inline with a tidy `[Image #1]` token instead of a
long path, so *"why is this failing? ~/screenshot.png"* reads cleanly. Useful
for a screenshot of an error, a dashboard, or output from another tool. Works in
both translate and `ask`/`chat` modes.

**Vision fallback for text-only models.**
If your main model can't read images (many fast or free models are text-only),
Sentinel automatically routes the image to a vision model on the *same* provider
to transcribe and describe it, then feeds that text to your main model — so a
text-only model effectively gains eyes. The default fallback on OpenRouter is a
free vision model (`nvidia/nemotron-nano-12b-v2-vl:free`); change or disable it
anytime with the `vision` command.

**Undo and checkpoints.**
Sentinel keeps a journal of what it ran (`history`) and can undo the last change
(`undo`). Undo is layered: before a command that edits files, it snapshots the
paths it will touch and restores them exactly on undo (including removing files
the command created); for changes with nothing to snapshot — stopping a daemon,
disabling a service, installing a package — it asks the model for a safe inverse
command (`systemctl stop X` → `systemctl start X`) and runs it through the same
`y/n` gate. You can also snapshot a path yourself with `checkpoint <path>` and
bring it back with `restore`. It is honest about limits: truly irreversible
actions (deleted data with no snapshot, network requests) report that they can't
be undone rather than guessing.

**Built for the terminal.**
A clean welcome box with live status, syntax-highlighted commands, and clear
output panels. Type a request with an image path (or paste a screenshot) and it
becomes a tidy `[Image #1]` token inline. Press **Esc** to cancel any
in-progress task: a slow translation, a long-running command (its whole process
tree is stopped), or a chat reply.

---

## How it works

```
type English  ->  model translates  ->  safety filter screens
                                              |
                                    +---------+---------+
                                 blocked            allowed
                                    |                   |
                               show reason     "what this does" (optional)
                                                        |
                                                you approve?  -- n -->  skip
                                                        | y
                                                    execute
                                                        |
                                              plain-English summary (optional)
```

---

## Getting started

Requires Python 3.10+.

```bash
cd ~/Linux_LLM
./run.sh          # sets up the venv on first run, then launches the CLI
```

That's it — `run.sh` creates `.venv` and installs dependencies the first time,
then starts Sentinel. To set up without launching, run `./setup.sh`. By hand:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

On a fresh Ubuntu/WSL/Mint box you may first need `sudo apt install -y python3-venv`.
Add provider keys interactively on first run, or via `.env` (`cp .env.example .env`).

First run walks you through a short questionnaire, then a provider/model picker.
After that, Sentinel remembers your provider, model, and API key, so launches go
straight to the prompt with no menus and no re-entering keys.

---

## Architecture

```
core.py      all logic, no UI (engines, safety filter, profile, settings, fleet, execution)
main.py      the terminal UI (built on rich)
mcp_server/  MCP server — exposes the host to AIs as a connectable skill
agent/       per-host HTTP agent so the CLI can manage remote machines (fleet)
gui/         experimental web UI over the same core (paused; see gui/README.md)
```

`core.py` is the single source of truth. A web GUI lives under `gui/` but is a
paused work in progress while its design is reworked; it is not part of the
shipped CLI.

The destructive-command blocklist has regression tests in `tests/`:

```bash
python tests/test_safety.py            # standalone, no dependencies
# or, with pytest:
pip install -r requirements-dev.txt && pytest
```

---

## Connect an AI to your machine (MCP)

Sentinel also runs as a **[Model Context Protocol](https://modelcontextprotocol.io)
server**, so an assistant like Claude — or your own agent — can connect to the
host and query it in plain language:

> *"How's the homelab's CPU and power consumption looking right now?"*

The AI calls Sentinel, Sentinel reads the machine, and reports back. The safety
guarantee holds even with no human at the keyboard, because of a hard split:

- **Read-only diagnostics** (CPU, memory, disk, power/thermal, top processes,
  network, listening ports, service status, recent errors) are exposed and
  answered directly — they only observe state, so there is nothing to approve.
- **Arbitrary execution is never exposed.** `propose_command` translates English
  into a command and screens it against the blocklist, but **returns it without
  running it** — a human still approves and runs it in the CLI.

See [`mcp_server/README.md`](mcp_server/README.md) for setup and the full tool
list.

---

## Manage more than one machine (fleet)

On each machine you want to manage (a VM, a homelab box), run
[`./run-agent.sh`](agent/README.md) — it sets up and starts the agent and prints
the URL + tokens to register. Your main Sentinel CLI becomes the **controller**
that drives them over the LAN. The LLM, the safety blocklist, and the `y/n` gate
stay on the controller — only execution is forwarded to the target's agent,
which screens the command again server-side before running it.

```
host add               # register a host: name, agent URL, tokens
hosts                  # list machines + health
use homelab            # target a host for following requests
on homelab what's using the most disk?
on all uptime          # fan out to every host (and local)
```

You don't even need `use` — just name a host in the request and Sentinel targets
it, routing read-only questions straight to the matching diagnostic:

```
how is my homelab's cpu usage looking?     # → cpu diagnostic on homelab
what about the CPU?                          # follow-up — stays on homelab
restart nginx on homelab                    # → translated + run on homelab
```

Naming a host also makes it the active target, and the prompt remembers recent
turns, so follow-ups like *"what about the CPU?"* or *"now restart it"* resolve
against the conversation instead of needing a full new sentence.

Agents use two tokens: a **read** token (diagnostics — safe to give an AI) and an
**admin** token (`/execute`, disabled unless set). See
[`agent/README.md`](agent/README.md) for setup and the safety model.

Manage all of it from one place with the **`settings`** menu — switch AI
provider/model, add or edit hosts, flip a host's execute lever on/off, and
configure each host's health monitor. When a command will run on a remote host,
the confirmation prompt says so prominently ("runs on: homelab") so it's always
clear *where* it acts.

**Always-on health checks.** Each agent can run a background monitor that
periodically checks disk, memory, load, watched services, and error-log volume
against thresholds, recording any breach. Ask **`alerts`** (or open `settings →
Fleet alerts`) for *"any problems on the fleet?"* — the agent does the watching
even when the CLI is closed. The agent's first-run installer sets this up for you.

---

## Commands

A leading slash is optional (`/ask` works too).

| Command | Description |
|---|---|
| *(plain English)* | Translate, review, approve, and run a command |
| *(… + an image path)* | Attach a screenshot/image as context (vision models) |
| `ask <q>` / `ask` / `chat` | Ask Linux questions (answers only; nothing runs) |
| `provider` | Switch AI provider |
| `model` | Pick a model (curated list, or `r` to refresh the live catalog) |
| `vision` | Set/disable the fallback model that reads images for text-only models |
| `history` | Show recently run commands and their undo status |
| `undo [ID]` | Undo the last change (restore a snapshot, or run a safe inverse) |
| `checkpoint <path>` | Snapshot a file/dir; `checkpoints` lists them, `restore [ID]` brings one back |
| `hosts` / `host add` / `host remove <name>` | Manage machines in the fleet |
| `use <host>` / `on <host\|all> <request>` | Target a host; run a one-off on one or all |
| `status [host]` / `diag <metric> [host]` | Read-only health snapshot or one metric (cpu, memory, disk, power, …) |
| `settings` | Interactive menu: AI, instances, execute & health toggles, profile |
| `alerts` | Recent health alerts from every host |
| `profile` | Re-take the experience questionnaire |
| `help` | Show the command reference |
| `exit` | Quit (Ctrl-D also works) |

---

## Configuration

- **Remembered automatically.** Your chosen provider, model, API key, and vision
  fallback are saved to `~/.config/sentinel/config.json` (written `chmod 600`,
  outside the repo, never committed), so you set them once. Change them anytime
  with the `provider` / `model` / `vision` commands.
- **API keys** can also come from `.env` (git-ignored); environment values take
  precedence over the saved ones. A key set via `.env` is never prompted for.
- **Override per launch** with `SENTINEL_PROVIDER`, `SENTINEL_MODEL`, and
  `SENTINEL_VISION_MODEL`.
- **Your profile** (experience level and explanation preference) is saved to
  `~/.config/sentinel/profile.json`.
- **History and checkpoints** live under `~/.config/sentinel/` (`history.jsonl`
  and `checkpoints/`); snapshots are size-capped and are a convenience, not a
  backup system.
- **Fleet hosts** (names, agent URLs, and tokens) are saved to
  `~/.config/sentinel/hosts.json`, written `chmod 600` since it holds tokens.

---

## Roadmap

- [x] Shared core (`core.py`) so the CLI (and a future GUI) run one engine.
- [x] Esc to cancel any in-progress task (LLM call or running command).
- [x] Remembered provider, model, and key across launches.
- [x] MCP server so external AIs can query the host (`mcp_server/`).
- [x] Attach images (paste, Ctrl-V, or path) with inline `[Image #N]` tokens.
- [x] Vision fallback so text-only models can still read images.
- [x] Undo / checkpoints for commands that change the system.
- [x] Multi-host fleet: per-host agents the CLI controls (`agent/`).
- [x] Always-on health monitor + alerts, with a `settings` menu to manage it all.
- [ ] Remote undo/checkpoints and per-host environment grounding.
- [ ] Streamable-HTTP MCP transport so AIs connect to an agent directly.
- [ ] Web GUI (paused under `gui/` while the design is reworked).

---

## Safety notes

- V1 runs commands on the local machine via the shell. The blocklist is a
  conservative backstop, not a sandbox; the confirmation gate is your real
  control. Read each command before approving.
- Smaller and local models follow the "one command only" and scope rules less
  reliably than frontier models. For the sharpest behavior, use Claude, GPT, or
  Gemini.

---

## License

Sentinel is released under the **PolyForm Noncommercial License 1.0.0** (see
[`LICENSE`](LICENSE)). In short: you may use, modify, and share it freely for
noncommercial purposes, but you may not sell it or use it for commercial
advantage. This is a source-available, noncommercial license, not an
OSI-approved open-source license (a true open-source license cannot restrict
commercial use). For commercial licensing, contact the author.
