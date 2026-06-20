# CLAUDE.md — working on Sentinel

Context for any Claude session picking up this repo. Read this first.

## What Sentinel is

A natural-language Linux manager. The user types plain English; an LLM
translates it into **exactly one** shell command; Sentinel screens it, shows it,
and runs it **only after explicit `y/n` approval**, then summarizes the output in
plain English. It manages the local machine *or a fleet* of remote machines, can
undo changes, exposes itself to other AIs over MCP, and runs an always-on health
monitor on each machine. It's a polished portfolio piece (published at
`github.com/dan-calin/sentinel`) and the first piece of a larger "AI
Workspace"/Jarvis ecosystem the user is building.

## Non-negotiable safety invariants (never break these)

1. **Nothing runs without a human `y/n`.** The LLM only translates; it never
   auto-executes. Keep the confirmation gate (`confirm_execution` in `main.py`).
2. **The destructive-command blocklist is the backstop** (`core.screen_command`
   / `_DANGEROUS_PATTERNS`). It runs *before* the prompt. Has regression tests —
   if you touch the regexes, run `tests/test_safety.py` and keep it green.
3. **Remote agents re-screen server-side.** The agent's `/execute` calls
   `screen_command` again, so a bypassed controller still can't push a
   destructive command. Diagnostics are read-only and need no approval.
4. **Two-token agent model.** Read token = diagnostics (safe for an AI); admin
   token = `/execute`, disabled unless set. The MCP server never exposes
   arbitrary execution — only read-only tools + `propose_command` (returns a
   screened command, does not run it).

## Architecture / where things live

- **`core.py`** — all UI-free logic, the single source of truth shared by the
  CLI, the agent, and the MCP server. Contains: `SYSTEM_PROMPT` +
  `environment_context()`; provider catalog (`PROVIDERS`, `ProviderSpec`);
  `Engine` base + `AnthropicEngine` / `OpenAICompatEngine` (one `_chat`
  primitive; `translate`/`ask`/`explain`/`summarize`/`describe_images`/`invert`);
  `screen_command`; `run_command`; `UserProfile`; `Settings` (persisted); image
  attachments (`load_image`, `user_message`); fleet (`HostConfig`, `load/save_hosts`,
  `RemoteAgent` HTTP client); journal + checkpoints + `classify_command` (undo);
  `is_unsupported`; reasoning helpers.
- **`main.py`** — the rich terminal UI (the controller). Prompt loop, command
  dispatch, the paste-aware raw-mode line editor (`read_prompt`/`_read_key`/
  `_compose_redraw`), the `settings` menu, fleet targeting + NL routing
  (`_detect_host`, `_diagnostic_intent`, `_looks_like_question`), reasoning/vision
  selectors, undo/checkpoint/history UI, `handle_request` (the shared
  translate→approve→execute flow).
- **`agent/server.py`** — FastAPI per-host agent: `/health`, `/diagnostics/*`,
  `/execute`, `/monitor`, `/alerts`. Reuses `core` + `diagnostics`. Runs a
  background health-monitor thread.
- **`mcp_server/`** — MCP (stdio) server exposing read-only diagnostics +
  `propose_command` to external AIs. `diagnostics.py` is the host-introspection
  catalog (also imported by the agent).
- **`health.py`** — standalone monitor logic: `Thresholds`/`MonitorConfig`/`Alert`,
  numeric collectors, pure `evaluate()`, alert store, `run_once()`.
- **`gui/`** — paused web UI (FastAPI + vanilla SPA) over the same `core`. Not shipped.
- **`tests/`** — `test_safety.py`, `test_checkpoints.py`, `test_health.py`,
  `test_editor.py`. All have a standalone runner *and* pytest.
- **`setup.sh` / `run.sh` / `run-agent.sh`** — venv setup, launch CLI, launch agent.

User config lives **outside the repo** at `~/.config/sentinel/`
(`config.json` chmod 600, `profile.json`, `hosts.json` chmod 600,
`history.jsonl`, `checkpoints/`, `agent-monitor.json`, `agent-alerts.jsonl`).

## Dev environment (important — it's unusual)

- The repo is on a **WSL Ubuntu** path (`/home/roxan/Linux_LLM`) but this Claude
  session runs on **Windows**; the Bash tool is **Git Bash**, not WSL.
- To run project code, shell into WSL: `wsl -d Ubuntu -- bash -lc '…'`. The
  Python venv is `~/Linux_LLM/.venv` (Python 3.14). Use `.venv/bin/python`.
- **Heredoc gotcha:** single quotes inside a `bash -lc '…'` blob terminate the
  outer quote. Use Python heredocs (`<<"PY" … PY`) and avoid inner single quotes,
  or run simple things directly in Git Bash.
- **Line endings:** files written via the Windows tools can get CRLF. Shell
  scripts must be LF or they break in WSL — after editing a `.sh`, run
  `sed -i 's/\r$//' file.sh` and `bash -n file.sh` to verify.
- Quick checks: `python -m py_compile core.py main.py` (Git Bash side works for
  syntax). Full tests: `wsl -d Ubuntu -- bash -lc 'cd ~/Linux_LLM && .venv/bin/python -m pytest tests/ -q'`.
- I can't drive an **interactive TTY** from here — the live line editor, menu
  keypresses, Esc, and the systemd install can only be verified by the user.

## Conventions

- **Commits go to `main`** (solo portfolio repo, no PR flow). End commit messages
  with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`. Commit/push
  only when the user asks (they usually do, in batches).
- **Secrets never committed.** `.env`, `~/.config/sentinel/config.json`, and
  `.agent-tokens.env` are gitignored / outside the repo. There's a stray
  screenshot `{56D5...}.png` in the working tree that predates this work — don't
  commit it.
- **README**: professional, minimal emoji. Keep it in sync when features land.
- **Models**: default to current Claude IDs (`claude-opus-4-8`, etc.). Reasoning
  on Claude = `thinking:{type:adaptive}` + `output_config:{effort}` (NOT
  `budget_tokens`, which 400s on current models); OpenAI-compatible = `reasoning_effort`.
  Read the `claude-api` skill before touching Anthropic API calls.

## Testing notes / gotchas

- The user's everyday model is **`openrouter/owl-alpha`** — weak and text-only.
  It misspells things (it emitted `UNSUPORTED`; hence `core.is_unsupported` is
  fuzzy) and needs the vision fallback for images. Frontier models behave better.
- Free-tier OpenRouter has a low per-request token cap — don't set large
  `max_tokens` (reasoning uses a small additive headroom, `REASONING_HEADROOM`,
  for this reason).

## Current status & roadmap

Done: single-host translate/approve/run + summary; multi-provider; reasoning;
images + vision fallback; undo/checkpoints; MCP server; multi-host fleet
(agent + controller); always-on health monitor + `alerts`; `settings` menu;
natural-language host targeting + conversational memory; paste-aware editor.

Open (roadmap): remote undo/checkpoints; per-host environment grounding (translate
currently grounds on the controller's env); streamable-HTTP MCP transport so AIs
connect to an agent directly; add the MCP/fleet angle to the `dan-calin` profile
README; web GUI (paused). Eventually: wire Sentinel into the user's Jarvis project.
