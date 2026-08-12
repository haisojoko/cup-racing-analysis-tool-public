"""FastAPI app for the analysis-buddy browser portal.

Local single-user use (binds 127.0.0.1). Serves a static page plus a small JSON
API: history on load, an SSE streaming turn endpoint, and proposal approve/clear.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import AsyncIterator, Optional

from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..session import AnalysisSession

app = FastAPI(title="Cup Racing Analysis Tool")
_session: Optional[AnalysisSession] = None

STATIC = Path(__file__).parent / "static"


@app.middleware("http")
async def _no_store(request, call_next):
    """Local single-user portal: never let the browser cache the shell or assets.
    Prevents another app on the same host from serving stale cached files here, and
    means an updated style.css/app.js always loads fresh."""
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response


def create_app(session: AnalysisSession) -> FastAPI:
    global _session
    _session = session
    return app


class SubmitBody(BaseModel):
    text: str


class ApproveBody(BaseModel):
    id: int
    yes: bool


@app.get("/")
async def root() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/history")
async def api_history() -> dict:
    if _session is None:
        return {"error": "Session not initialized"}
    return _session.history()


@app.post("/api/submit_stream")
async def api_submit_stream(body: SubmitBody) -> StreamingResponse:
    async def event_stream() -> AsyncIterator[bytes]:
        if _session is None:
            payload = {"event": "error", "message": "Session not initialized"}
            yield f"data: {json.dumps(payload)}\n\n".encode("utf-8")
            return
        async for event in _session.submit_stream(body.text):
            yield f"data: {json.dumps(event)}\n\n".encode("utf-8")

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/approve")
async def api_approve(body: ApproveBody) -> dict:
    if _session is None:
        return {"ok": False, "error": "Session not initialized"}
    return _session.approve(body.id, body.yes)


@app.post("/api/clear")
async def api_clear() -> dict:
    if _session is None:
        return {"error": "Session not initialized"}
    return _session.clear()


app.mount("/static", StaticFiles(directory=STATIC), name="static")
