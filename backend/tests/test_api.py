"""REST + WS API (N-6): the full happy path through the API in sim mode, plus validation and errors."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from eval import journal_check


@pytest.fixture
def app(tmp_path: Path) -> Any:
    return create_app(Settings(_env_file=None, gnani_api_key="k", data_dir=tmp_path, api_use_llm=False))


def client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def wait_done(c: httpx.AsyncClient, call_id: str, timeout_s: float = 120) -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout_s
    while True:
        body = (await c.get(f"/api/calls/{call_id}")).json()
        if body["state"] in ("DONE", "FAILED"):
            return body
        assert loop.time() < end, body
        await asyncio.sleep(0.05)


async def test_full_api_flow_sim(app: Any) -> None:
    async with client(app) as c:
        comps = (await c.get("/api/companies")).json()
        assert {x["id"] for x in comps} >= {"nimbus_telecom", "sahyog_bank", "prakash_bijli_board"}
        nimbus = next(x for x in comps if x["id"] == "nimbus_telecom")
        assert nimbus["profile_fields"] == ["registered_mobile"] and nimbus["sample_problem"]

        r = await c.post("/api/company/scenario", json={"auto_user": True, "hold_s": 10})
        assert r.json()["auto_user"] is True and r.json()["hold_s"] == 10

        task = (await c.post("/api/tasks", json={"text": nimbus["sample_problem"], "company_id": "nimbus_telecom"})).json()
        assert task["plan_method"] == "rule" and "refund" in task["plan"]["goal_keywords"] and task["demo_data"]

        call = (await c.post(f"/api/tasks/{task['task_id']}/call", json={"mode": "sim", "speed": "fast"})).json()
        cid = call["call_id"]
        assert (await c.get(f"/api/calls/{cid}/audio/mixed")).status_code in (409, 200)
        done = await wait_done(c, cid)
        assert done["state"] == "DONE" and done["path"] == ["1", "1", "2"] and done["summary_ready"]

        summary = (await c.get(f"/api/calls/{cid}/summary")).json()
        f = summary["fields"]
        assert f["ticket_id"]["verified"] and f["ticket_id"]["value"].startswith("NMB")
        assert f["ticket_id"]["evidence"][0]["start"] < f["ticket_id"]["evidence"][0]["end"]
        assert summary["outcome"] == "ticket_raised"

        wav = await c.get(f"/api/calls/{cid}/audio/mixed")
        assert wav.status_code == 200 and wav.content[:4] == b"RIFF"
        assert (await c.get(f"/api/calls/{cid}/audio/secret")).status_code == 404
        assert "tts_chars" in (await c.get("/api/usage")).json()

    # events WS replays the whole journal (the call already finished) and it is valid
    with TestClient(app) as tc, tc.websocket_connect(f"/ws/events/{cid}") as ws:
        events = []
        while True:
            ev = ws.receive_json()
            events.append(ev)
            if ev["type"] == "state_changed" and ev["to"] in ("DONE", "FAILED"):
                break
    assert journal_check.check_journal(events, complete=True) == []


async def test_validation_and_unknowns(app: Any) -> None:
    async with client(app) as c:
        assert (await c.post("/api/tasks", json={"text": "hi there", "company_id": "realco"})).status_code == 404
        assert (await c.post("/api/tasks", json={"text": "", "company_id": "nimbus_telecom"})).status_code == 422
        assert (await c.post("/api/tasks/nope/call", json={})).status_code == 404
        assert (await c.get("/api/calls/nope")).status_code == 404
        assert (await c.get("/api/calls/nope/summary")).status_code == 404
        assert (await c.post("/api/company/scenario", json={"noise_profile": "snr99"})).status_code == 422


async def test_transcribe_rejects_non_wav(app: Any) -> None:
    async with client(app) as c:
        r = await c.post("/api/transcribe", files={"audio": ("x.webm", b"not audio", "audio/webm")})
        assert r.status_code == 415
