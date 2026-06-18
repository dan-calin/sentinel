# GUI Design Prompt

Paste the prompt below into Claude (Claude Code, or the web app with the
frontend-design skill) to generate the GUI. It deliberately specifies **what
the interface must do and show** — the data, states, and flows — and leaves
**all aesthetic decisions to you**: layout, theme, color, typography, motion,
and overall personality. Don't pre-decide the look; let it surprise you.

---

## The prompt

> Design and build the front end for a desktop-style app called **Sentinel**, a
> natural-language Linux server manager. It lets someone manage Linux machines by
> typing requests in plain English. An LLM translates each request into a
> single shell command, and — this is the heart of the product — **no command
> ever runs until the user explicitly approves it.** The UI's job is to make
> that translate → review → approve → run loop feel safe, fast, and legible.
>
> **Be creative with the visual design.** I do not want a generic, templated
> admin-dashboard look. Choose a distinctive theme, palette, typography, and
> layout with real point of view. Avoid clichés (Inter/Roboto on a white card
> grid, purple gradients, default Bootstrap/Material shells). The product has a
> Linux-sysadmin/terminal soul and a safety-first conscience — let that inspire
> the aesthetic, but the direction is yours. Use motion and micro-interactions
> with intent, not decoration.
>
> ### Architecture constraint
> This is a thin client over an existing local Python backend. Talk to it over
> a small local HTTP/JSON API (assume `http://localhost:8765`) with these
> endpoints — design against this contract, don't reinvent it:
> - `GET /providers` → list of `{key, label, ready, notes}`
> - `GET /providers/{key}/models?refresh=true|false` → `{models: string[], source: "curated"|"live"}`
> - `POST /translate` `{provider, model, request}` → `{command, safe, block_reason}`
> - `POST /explain` `{provider, model, command}` → `{explanation}` (level-aware; see profile)
> - `POST /ask` `{provider, model, messages}` → `{answer}` (Q&A about Linux; `messages` is the
>   multi-turn conversation; answer is informational text — nothing is executed)
> - `POST /execute` `{command}` → `{stdout, stderr, exit_code}`
> - `POST /summarize` `{provider, model, request, command, stdout, stderr, exit_code}` →
>   `{summary}` (level-aware plain-English answer derived from the output; explains failures too)
> - `POST /settings/keys` `{provider, api_key, base_url?}` → `{ok}`
> - `GET /profile` → `{experience, explain}` · `POST /profile` `{experience, explain}` → `{ok}`
>   (`experience` is one of `beginner` | `intermediate` | `expert`; `explain` is a bool)
>
> (If a piece of state isn't covered by the API, surface it client-side; don't
> block on backend changes.)
>
> ### Screens / regions the UI must cover
> 0. **First-run onboarding.** The very first time the app opens (no saved
>    profile), greet the user and ask a couple of short questions about their
>    Linux/command-line experience (beginner / intermediate / expert) and
>    whether they want plain-English explanations of each command. Persist the
>    answers via `POST /profile` and let them revisit this later from a settings
>    affordance. This profile controls *how* commands are explained — never what
>    command is produced. Make onboarding feel welcoming and quick, not like a
>    form wall.
> 1. **Provider & model selection.** Pick the AI provider (Anthropic, OpenAI,
>    Google Gemini, OpenRouter, local Ollama, or a Custom OpenAI-compatible
>    endpoint). For each, choose a model from a curated list with a clear
>    **"refresh from provider"** action that pulls the live catalog. Show which
>    providers are configured vs. need a key. Allow entering an API key (and,
>    for Custom, a base URL) — treat keys as secrets (masked, never echoed).
> 2. **The request composer.** A primary input where the user types a
>    plain-English task. Show example prompts to get started. Indicate the
>    active provider/model and a translating/loading state.
> 3. **The proposed command.** Display the generated shell command prominently
>    with syntax styling and copy affordance. This is a review surface — make it
>    easy to read and scrutinize before deciding. When the user's profile has
>    explanations enabled, also show a level-appropriate plain-English
>    description of what the command does (from `POST /explain`) — concise for
>    experts, friendlier and risk-aware for beginners. Treat it as descriptive
>    only; it never replaces reviewing the command itself.
> 4. **The safety gate.** A deliberate, unmistakable approve/reject moment.
>    Approval must be an explicit, intentional act (not a default or an easy
>    mis-click). Convey the weight of running a command on a real machine.
> 5. **The "blocked" state.** Some commands are refused by a local safety filter
>    *before* the user is even asked (e.g. `rm -rf`, disk formatting). When
>    `safe` is false, show the command, mark it clearly as blocked, and explain
>    `block_reason` — there is no approve action in this state.
> 6. **Execution results.** After approval, show `stdout`, `stderr`, and the
>    exit code, with success/failure clearly distinguished. Long output should
>    stay readable.
> 7. **Ask / Learn mode.** A distinct conversational surface (separate from the
>    command flow) where the user can ask questions about Linux, the shell, and
>    system administration and get answers via `POST /ask`. It supports
>    multi-turn follow-ups and adapts depth to the user's profile. Make it
>    visually and behaviorally unmistakable from the command flow: this is
>    information only — there is no proposed command, no approval gate, and
>    nothing ever executes here. (Any commands mentioned in an answer are for
>    the user to copy into the command flow if they choose.)
> 8. **History / session log.** A running timeline of past requests: the
>    English ask, the resulting command, whether it ran/was rejected/was
>    blocked, and its outcome. Make it easy to re-run or re-edit a prior request.
> 9. **Empty, loading, and error states** for each of the above (no key set,
>    network/API error, provider returned nothing, command timed out, etc.).
>
> ### Interaction principles
> - The translate → review → approve → run sequence should be obvious at a
>   glance, even to a first-time user.
> - Make the safe path the easy path; make the destructive path require
>   intention.
> - Keyboard-friendly: a power user should be able to drive the whole loop
>   without reaching for the mouse.
> - Accessible: sufficient contrast, focus states, and screen-reader labels —
>   even within a bold visual theme.
>
> ### Deliverable
> Pick a modern front-end stack you can justify (e.g. React + Vite, or
> SvelteKit) and build a working, self-contained UI wired to the API contract
> above, using mock responses where the backend isn't running so it's
> demoable standalone. Include the full component code and a short note on the
> design direction you chose and why.

---

## Notes for us (not part of the prompt)

- The API contract above mirrors the functions already in `main.py`
  (`Engine.translate`, `screen_command`, `execute_command`,
  `Engine.list_models`, `resolve_credentials`). To support the GUI we'd add a
  thin backend (e.g. FastAPI) that imports a shared core module and exposes
  these endpoints — see the architecture discussion in chat.
- Keep the `safe`/`block_reason` decision **server-side** so the GUI can never
  bypass the safety filter. The client only renders the verdict.
