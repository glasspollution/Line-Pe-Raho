"""Company console: ring -> accept -> two-way audio over /ws/company-agent -> end. T-109 acceptance:
console audio reaches the line, caller audio reaches the console."""

from __future__ import annotations

import time

import numpy as np
from fastapi.testclient import TestClient

from app.audio.frames import FRAME_BYTES
from app.company.console import SILENCE, ConsoleAgentLeg
from app.main import app


def browser_sine(ms: int = 20, freq: float = 1000.0) -> bytes:
    t = np.arange(int(48000 * ms / 1000)) / 48000
    return (0.3 * np.sin(2 * np.pi * freq * t)).astype("<f4").tobytes()


def caller_frame(v: int = 2000) -> bytes:
    return np.full(160, v, dtype="<i2").tobytes()


def wait(cond, timeout: float = 2.0) -> None:  # type: ignore[no-untyped-def]
    end = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < end, "timed out"
        time.sleep(0.01)


def test_leg_state_machine_offline() -> None:
    leg = ConsoleAgentLeg("c")
    leg.receive(caller_frame(), 0.0)
    assert leg.pop_for_console() is None  # nothing forwarded before the call reaches the desk
    leg.on_connect(10.0)
    assert leg.state == "ringing" and leg.next_frame(10.0) == SILENCE
    leg.push_browser_audio(browser_sine())  # ignored while ringing
    leg.accept()
    assert leg.state == "talking"
    leg.push_browser_audio(browser_sine(40))
    f = leg.next_frame(10.02)
    assert len(f) == FRAME_BYTES and f != SILENCE
    leg.end()
    assert leg.ended


def test_ws_unknown_call() -> None:
    with TestClient(app) as client, client.websocket_connect("/ws/company-agent/nope") as ws:
        assert ws.receive_json()["type"] == "error"


def test_ws_two_way_audio_and_end() -> None:
    leg = app.state.consoles.create("call-1")
    leg.on_connect(5.0)
    try:
        with TestClient(app) as client, client.websocket_connect("/ws/company-agent/call-1") as ws:
            assert ws.receive_json() == {"type": "status", "state": "ringing"}
            ws.send_json({"type": "accept"})
            wait(lambda: leg.state == "talking")
            assert ws.receive_json() == {"type": "status", "state": "talking"}
            # console mic -> line
            for _ in range(5):
                ws.send_bytes(browser_sine())
            wait(lambda: len(leg._from_console) >= 4)
            frame = leg.next_frame(5.1)
            x = np.frombuffer(frame, dtype="<i2").astype(float)
            assert np.sqrt(np.mean(x**2)) > 3000  # ~0.3 FS sine survived resampling
            # caller -> console
            leg.receive(caller_frame(1234), 5.12)
            got = ws.receive_bytes()
            assert len(got) == FRAME_BYTES and np.frombuffer(got, dtype="<i2")[0] == 1234
            ws.send_json({"type": "end"})
            wait(lambda: leg.ended)
    finally:
        app.state.consoles.remove("call-1")


def test_ws_disconnect_counts_as_agent_hangup() -> None:
    leg = app.state.consoles.create("call-2")
    leg.on_connect(1.0)
    try:
        with TestClient(app) as client, client.websocket_connect("/ws/company-agent/call-2") as ws:
            ws.receive_json()
            ws.send_json({"type": "accept"})
            wait(lambda: leg.state == "talking")
        wait(lambda: leg.ended)
    finally:
        app.state.consoles.remove("call-2")
