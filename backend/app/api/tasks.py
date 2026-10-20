"""REST: tasks, calls, summaries, audio, usage, voice input (CLAUDE.md §14.1)."""

from __future__ import annotations

import io
import json
import uuid
from typing import Any, Literal

import numpy as np
import soundfile as sf
from fastapi import APIRouter, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.agent.planner import build_plan
from app.audio.frames import resample
from app.company.engine import load_tree
from app.gnani.errors import GLOBAL_METER
from app.gnani.stt_rest import PrismaREST
from app.llm.client import LLMClient
from app.llm.schemas import CallPlan
from app.models import Task, session
from app.runtime import CallManager
from app.sim.harness import DEMO_PROFILE

router = APIRouter(prefix="/api")
TRACKS = ("mixed", "callee", "caller", "user")


def manager(request: Request) -> CallManager:
    m: CallManager = request.app.state.manager
    return m


class TaskIn(BaseModel):
    text: str = Field(min_length=3, max_length=1000)
    company_id: str
    profile: dict[str, str] = Field(default_factory=lambda: dict(DEMO_PROFILE))
    user_first_name: str = Field(default="Aarav", max_length=40)


class CallIn(BaseModel):
    mode: Literal["sim", "live"] = "sim"
    speed: Literal["realtime", "fast"] = "realtime"


@router.post("/tasks")
async def create_task(body: TaskIn, request: Request) -> dict[str, Any]:
    try:
        load_tree(body.company_id)
    except KeyError:
        raise HTTPException(404, f"unknown company {body.company_id!r}") from None
    m = manager(request)
    llm = LLMClient(m.s) if m.s.api_use_llm else None
    plan, method = await build_plan(body.text, body.company_id, body.profile, llm)
    task_id = uuid.uuid4().hex[:12]
    with session(m.db) as db:
        db.add(Task(id=task_id, raw_text=body.text, user_first_name=body.user_first_name,
                    user_language=plan.user_language, company_id=body.company_id,
                    profile_json=json.dumps(body.profile), plan_json=plan.model_dump_json(), plan_method=method))
        db.commit()
    return {"task_id": task_id, "plan": plan.model_dump(), "plan_method": method, "profile": body.profile,
            "user_first_name": body.user_first_name, "demo_data": True}


@router.post("/tasks/{task_id}/call")
async def start_call(task_id: str, request: Request, body: CallIn | None = None) -> dict[str, Any]:
    body = body or CallIn()
    m = manager(request)
    with session(m.db) as db:
        task = db.get(Task, task_id)
        if task is None:
            raise HTTPException(404, "unknown task")
        plan = CallPlan.model_validate_json(task.plan_json)
        profile = json.loads(task.profile_json)
        name = task.user_first_name
        task.status = "calling"
        db.add(task)
        db.commit()
    rt = await m.start_call(task_id, plan, profile, name, mode=body.mode, speed=body.speed)
    return {"call_id": rt.call_id, "mode": rt.mode, "scenario": rt.scenario.__dict__}


def _runtime(request: Request, call_id: str) -> Any:
    rt = manager(request).get(call_id)
    if rt is None:
        raise HTTPException(404, "unknown call")
    return rt


@router.get("/calls/{call_id}")
async def get_call(call_id: str, request: Request) -> dict[str, Any]:
    rt = _runtime(request, call_id)
    ev = rt.journal.events
    return {
        "call_id": call_id, "task_id": rt.task_id, "mode": rt.mode, "state": rt.state, "error": rt.error,
        "path": rt.orch.result.path, "time_to_detect_s": rt.orch.result.time_to_detect_s,
        "probe_used": rt.orch.result.probe_used, "company_id": rt.orch.plan.company_id,
        "agent_mode": rt.scenario.agent_mode, "events": len(ev),
        "decisions": [e for e in ev if e["type"] == "decision"],
        "summary_ready": any(e["type"] == "summary_ready" and not e.get("skipped") for e in ev),
        "usage": rt.meter.snapshot(),
    }


@router.post("/calls/{call_id}/hangup")
async def hangup(call_id: str, request: Request) -> dict[str, Any]:
    _runtime(request, call_id)
    return {"ok": await manager(request).hangup(call_id)}


@router.get("/calls/{call_id}/summary")
async def get_summary(call_id: str, request: Request) -> dict[str, Any]:
    s = manager(request).summary(call_id)
    if s is None:
        raise HTTPException(404, "summary not ready")
    return s


@router.get("/calls/{call_id}/audio/{track}")
async def get_audio(call_id: str, track: str, request: Request) -> FileResponse:
    if track not in TRACKS:
        raise HTTPException(404, f"track must be one of {TRACKS}")
    m = manager(request)
    rt = m.get(call_id)
    if rt is not None and rt.state not in ("DONE", "FAILED"):
        raise HTTPException(409, "recordings are available after the call ends")
    path = m.s.data_dir / "calls" / call_id / "recordings" / f"{track}.wav"
    if not path.exists():
        raise HTTPException(404, "no recording")
    return FileResponse(path, media_type="audio/wav")


@router.get("/usage")
async def usage() -> dict[str, Any]:
    return GLOBAL_METER.snapshot()


@router.post("/transcribe")
async def transcribe(audio: UploadFile, request: Request, language: str = "en-IN") -> dict[str, Any]:
    """Voice task input (§2.1): a WAV recorded in the browser (<= transcribe_max_s) -> text via Prisma REST."""
    m = manager(request)
    raw = await audio.read()
    try:
        data, rate = sf.read(io.BytesIO(raw), dtype="int16", always_2d=True)
    except Exception:  # noqa: BLE001 - anything soundfile can't read is a client error
        raise HTTPException(415, "send a WAV file (PCM)") from None
    mono = data.mean(axis=1).astype(np.int16)
    if len(mono) / rate > m.s.transcribe_max_s:
        raise HTTPException(413, f"recording longer than {m.s.transcribe_max_s:.0f} s")
    pcm = resample(mono, rate, 16000) if rate != 16000 else mono
    rest = PrismaREST(m.s)
    try:
        out = await rest.transcribe(pcm.astype("<i2").tobytes(), language=language, rate=16000)
    finally:
        await rest.aclose()
    return {"text": out.text, "language": language}
