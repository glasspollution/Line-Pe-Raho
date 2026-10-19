"""FastAPI app entry point. More routers and startup (phrase-cache warmup) arrive with later tasks."""

from __future__ import annotations

from fastapi import FastAPI

from app.api import ws
from app.company.console import ConsoleRegistry
from app.config import get_settings

app = FastAPI(title="Line Pe Raho")
app.state.consoles = ConsoleRegistry()
app.include_router(ws.router)


@app.get("/api/health")
async def health() -> dict[str, str]:
    s = get_settings()
    return {"status": "ok", "line_kind": s.line_kind, "llm_model": s.llm_model}
