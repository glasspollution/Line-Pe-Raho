"""WebSocket endpoints (CLAUDE.md §14.2): company console, user audio. /ws/events arrives with T-304."""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.agent.bridge import Bridge
from app.company.console import ConsoleRegistry

router = APIRouter()
_SEND_POLL_S = 0.02


def consoles(ws: WebSocket) -> ConsoleRegistry:
    reg: ConsoleRegistry = ws.app.state.consoles
    return reg


def bridges(ws: WebSocket) -> dict[str, Bridge]:
    reg: dict[str, Bridge] = ws.app.state.bridges
    return reg


@router.websocket("/ws/events/{call_id}")
async def events(ws: WebSocket, call_id: str) -> None:
    """Server -> client JSON events (§14.2): the backlog first, then live, closing after DONE/FAILED.
    A finished call from an earlier server run is replayed from its journal file."""
    await ws.accept()
    m = ws.app.state.manager
    rt = m.get(call_id)
    if rt is None:
        path = m.s.data_dir / "calls" / call_id / "journal.jsonl"
        if not path.exists():
            await ws.send_json({"type": "error", "message": f"unknown call {call_id}"})
            await ws.close(code=4404)
            return
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                await ws.send_text(line)
        await ws.close()
        return
    q = rt.journal.subscribe()
    try:
        while True:
            ev = await q.get()
            await ws.send_json(ev)
            if ev["type"] == "state_changed" and ev.get("to") in ("DONE", "FAILED"):
                break
        await ws.close()
    except WebSocketDisconnect:
        pass
    finally:
        rt.journal.unsubscribe(q)


@router.websocket("/ws/user-audio/{call_id}")
async def user_audio(ws: WebSocket, call_id: str) -> None:
    """Binary both ways: browser sends 48 kHz float32 mic audio, server sends 8 kHz s16le call audio.
    Connecting is the join (WAITING_FOR_USER -> BRIDGED). {"type":"hangup"} or closing the socket ends it."""
    bridge = bridges(ws).get(call_id)
    await ws.accept()
    if bridge is None:
        await ws.send_json({"type": "error", "message": f"no call {call_id} waiting for the user"})
        await ws.close(code=4404)
        return
    bridge.connect()
    await ws.send_json({"type": "status", "state": "joined"})

    async def sender() -> None:
        while not bridge.hung_up:
            frame = bridge.pop_for_user()
            if frame is not None:
                await ws.send_bytes(frame)
            else:
                await asyncio.sleep(_SEND_POLL_S)

    send_task = asyncio.create_task(sender())
    try:
        while not bridge.hung_up:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if msg.get("bytes") is not None:
                bridge.push_browser_audio(msg["bytes"])
            elif msg.get("text") is not None and json.loads(msg["text"]).get("type") == "hangup":
                break
    except WebSocketDisconnect:
        pass
    finally:
        bridge.hangup()
        bridge.disconnect()
        send_task.cancel()
        await asyncio.gather(send_task, return_exceptions=True)


@router.websocket("/ws/company-agent/{call_id}")
async def company_agent(ws: WebSocket, call_id: str) -> None:
    leg = consoles(ws).get(call_id)
    await ws.accept()
    if leg is None:
        await ws.send_json({"type": "error", "message": f"no call {call_id} waiting for an agent"})
        await ws.close(code=4404)
        return
    await ws.send_json({"type": "status", "state": leg.state})

    async def sender() -> None:
        last_state = leg.state
        while not leg.ended:
            frame = leg.pop_for_console()
            if frame is not None:
                await ws.send_bytes(frame)
                continue
            if leg.state != last_state:
                last_state = leg.state
                await ws.send_json({"type": "status", "state": leg.state})
            await asyncio.sleep(_SEND_POLL_S)
        await ws.send_json({"type": "status", "state": "ended"})

    send_task = asyncio.create_task(sender())
    try:
        while not leg.ended:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if msg.get("bytes") is not None:
                leg.push_browser_audio(msg["bytes"])
            elif msg.get("text") is not None:
                ctrl = json.loads(msg["text"])
                if ctrl.get("type") == "accept":
                    leg.accept()
                elif ctrl.get("type") == "end":
                    leg.end()
    except WebSocketDisconnect:
        pass
    finally:
        leg.end()  # a closed console means the agent hung up
        await asyncio.gather(send_task, return_exceptions=True)
