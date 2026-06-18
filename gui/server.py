#!/usr/bin/env python3
"""Sentinel GUI backend (WORK IN PROGRESS - paused).

A thin local HTTP API over the shared core engine (``core.py``). The web UI it
serves is being redesigned; this backend is kept here but is not part of the
shipped CLI experience yet.

The safety verdict is computed server-side and re-checked at execution, so a
browser client can never bypass the destructive-command filter.

Run it (from the repo root):
    python gui/server.py            # or: uvicorn gui.server:app --port 8765
Then open http://127.0.0.1:8765
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Make the repo-root modules importable when run as `python gui/server.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

import core

load_dotenv()

app = FastAPI(title="Sentinel", version="1.0")
WEB_DIR = Path(__file__).resolve().parent / "web"

# Credentials set at runtime from the UI (in-memory only; never persisted).
_RUNTIME_KEYS: dict[str, dict[str, str]] = {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _spec(provider: str) -> core.ProviderSpec:
    spec = core.PROVIDERS.get(provider)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"Unknown provider: {provider}")
    return spec


def _resolve(spec: core.ProviderSpec) -> core.Credentials:
    """Resolve credentials from the runtime store, then the environment."""
    saved = _RUNTIME_KEYS.get(spec.key, {})
    base_url = saved.get("base_url") or spec.base_url

    if spec.keyless:
        if spec.key == "ollama":
            base_url = saved.get("base_url") or os.getenv("OLLAMA_HOST") or spec.base_url
        return core.Credentials(api_key="", base_url=base_url)

    if spec.runtime_config:
        base_url = saved.get("base_url") or os.getenv("CUSTOM_BASE_URL")
        if not base_url:
            raise HTTPException(400, "Custom provider needs a base URL (set it in Settings).")
        api_key = saved.get("api_key") or os.getenv("CUSTOM_API_KEY", "")
        return core.Credentials(api_key=api_key, base_url=base_url)

    api_key = saved.get("api_key") or os.getenv(spec.api_key_env or "", "")
    if not api_key:
        raise HTTPException(400, f"No API key for {spec.label}. Add one in Settings.")
    return core.Credentials(api_key=api_key, base_url=base_url)


def _engine(provider: str, model: str | None) -> core.Engine:
    spec = _spec(provider)
    creds = _resolve(spec)
    try:
        return core.create_engine(spec, model, creds.api_key, creds.base_url)
    except core.TranslationError as exc:
        raise HTTPException(502, str(exc)) from exc


def _ready(spec: core.ProviderSpec) -> bool:
    """Whether the provider is usable, considering runtime-entered keys too."""
    saved = _RUNTIME_KEYS.get(spec.key)
    if saved:
        if spec.runtime_config:
            return bool(saved.get("base_url") or os.getenv("CUSTOM_BASE_URL"))
        if not spec.keyless:
            return bool(saved.get("api_key"))
    return core.provider_is_ready(spec)


def _profile() -> core.UserProfile:
    return core.load_profile() or core.UserProfile()


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class TranslateReq(BaseModel):
    provider: str
    model: str | None = None
    request: str


class ExplainReq(BaseModel):
    provider: str
    model: str | None = None
    command: str


class AskReq(BaseModel):
    provider: str
    model: str | None = None
    messages: list[dict[str, str]]


class ExecuteReq(BaseModel):
    command: str


class SummarizeReq(BaseModel):
    provider: str
    model: str | None = None
    request: str
    command: str
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0


class KeysReq(BaseModel):
    provider: str
    api_key: str | None = None
    base_url: str | None = None


class ProfileReq(BaseModel):
    experience: str
    explain: bool


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/")
def index() -> FileResponse:
    path = WEB_DIR / "index.html"
    if not path.exists():
        raise HTTPException(404, "Frontend not built yet (gui/web/index.html missing).")
    return FileResponse(path)


@app.get("/providers")
def providers() -> list[dict]:
    return [
        {"key": s.key, "label": s.label, "ready": _ready(s), "notes": s.notes}
        for s in core.PROVIDERS.values()
    ]


@app.get("/providers/{key}/models")
def models(key: str, refresh: bool = False) -> dict:
    spec = _spec(key)
    if refresh:
        engine = _engine(key, None)
        try:
            return {"models": engine.list_models(), "source": "live"}
        except core.TranslationError as exc:
            raise HTTPException(502, str(exc)) from exc
    return {"models": list(spec.models), "source": "curated"}


@app.post("/translate")
def translate(req: TranslateReq) -> dict:
    engine = _engine(req.provider, req.model)
    try:
        command = engine.translate(req.request)
    except core.TranslationError as exc:
        raise HTTPException(502, str(exc)) from exc
    if not command or command == core.UNSUPPORTED_SENTINEL:
        return {"command": "", "safe": False, "block_reason": "", "unsupported": True}
    verdict = core.screen_command(command)
    return {
        "command": command,
        "safe": verdict.allowed,
        "block_reason": verdict.reason or None,
        "unsupported": False,
    }


@app.post("/explain")
def explain(req: ExplainReq) -> dict:
    engine = _engine(req.provider, req.model)
    try:
        return {"explanation": engine.explain(req.command, _profile())}
    except core.TranslationError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.post("/ask")
def ask(req: AskReq) -> dict:
    engine = _engine(req.provider, req.model)
    try:
        return {"answer": engine.ask(req.messages, _profile())}
    except core.TranslationError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.post("/execute")
def execute(req: ExecuteReq) -> dict:
    # Re-screen server-side: never trust the client to have honored the verdict.
    verdict = core.screen_command(req.command)
    if not verdict.allowed:
        raise HTTPException(400, f"Refused by safety filter: {verdict.reason}")
    result = core.run_command(req.command)
    return {"stdout": result.stdout, "stderr": result.stderr, "exit_code": result.exit_code}


@app.post("/summarize")
def summarize(req: SummarizeReq) -> dict:
    engine = _engine(req.provider, req.model)
    result = core.CommandResult(exit_code=req.exit_code, stdout=req.stdout, stderr=req.stderr)
    try:
        return {"summary": engine.summarize(req.request, req.command, result, _profile())}
    except core.TranslationError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.get("/profile")
def get_profile() -> dict:
    return _profile().to_dict()


@app.post("/profile")
def set_profile(req: ProfileReq) -> dict:
    if req.experience not in core.EXPERIENCE_LEVELS:
        raise HTTPException(400, f"experience must be one of {core.EXPERIENCE_LEVELS}")
    ok = core.save_profile(core.UserProfile(experience=req.experience, explain=req.explain))
    return {"ok": ok}


@app.post("/settings/keys")
def set_keys(req: KeysReq) -> dict:
    _spec(req.provider)
    entry = _RUNTIME_KEYS.setdefault(req.provider, {})
    if req.api_key is not None:
        entry["api_key"] = req.api_key
    if req.base_url is not None:
        entry["base_url"] = req.base_url
    return {"ok": True}


@app.exception_handler(core.TranslationError)
def _translation_error(_request, exc: core.TranslationError) -> JSONResponse:
    return JSONResponse(status_code=502, content={"detail": str(exc)})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8765)
