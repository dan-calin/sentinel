# Sentinel GUI (work in progress - paused)

This folder holds an experimental web UI for Sentinel. It is **not part of the
shipped experience yet** - the visual design is being reworked.

It is a thin client over the same `core.py` engine the CLI uses; the safety
verdict is enforced server-side, so the browser cannot bypass the
destructive-command filter.

## Try it (optional)

```bash
pip install -r gui/requirements.txt
python gui/server.py            # then open http://127.0.0.1:8765
```

- `server.py` - FastAPI backend exposing the API in `gui-design-prompt.md`.
- `web/index.html` - single-file frontend (no build step).
- `gui-design-prompt.md` - the design brief / API contract used to (re)build the UI.
