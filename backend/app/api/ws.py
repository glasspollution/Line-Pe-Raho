"""WebSocket endpoints (CLAUDE.md §14.2). /ws/events and /ws/user-audio arrive with T-304 / T-207."""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.company.console import ConsoleRegistry

router = APIRouter()
_SEND_POLL_S = 0.02


def consoles(ws: WebSocket) -> ConsoleRegistry:
    reg: ConsoleRegistry = ws.app.state.consoles
    return reg


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
