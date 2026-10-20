"""FastAPI app (CLAUDE.md §14). `create_app(settings)` builds an isolated app (tests use a temp data dir)."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import company, tasks, ws
from app.company.console import ConsoleRegistry
from app.config import Settings, get_settings
from app.gnani.errors import UsageMeter
from app.llm.client import LLMClient
from app.models import make_engine
from app.runtime import CallManager


def create_app(settings: Settings | None = None) -> FastAPI:
    s = settings or get_settings()
    app = FastAPI(title="Line Pe Raho")
    app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
                       allow_methods=["*"], allow_headers=["*"])
    app.state.settings = s
    app.state.consoles = ConsoleRegistry()
    app.state.bridges = {}  # call_id -> Bridge, registered by the CallManager

    def llm_factory(meter: UsageMeter) -> Any:
        return LLMClient(s, meter=meter)

    app.state.manager = CallManager(s, make_engine(s.data_dir), app.state.bridges, app.state.consoles,
                                    llm_factory=llm_factory if s.api_use_llm else None)
    app.include_router(ws.router)
    app.include_router(tasks.router)
    app.include_router(company.router)

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "line_kind": s.line_kind, "llm_model": s.llm_model}

    return app


app = create_app()
