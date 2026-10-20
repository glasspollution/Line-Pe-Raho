"""User bridge (N-4): WS loopback, join/hangup semantics, and a full bridged conversation in simulation."""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import soundfile as sf
from fastapi.testclient import TestClient

from app.agent.bridge import Bridge
from app.audio.frames import FRAME_BYTES
from app.main import app
from app.sim.harness import SimConfig, run_sim_call
from eval import journal_check


def browser_sine(ms: int = 20) -> bytes:
    t = np.arange(int(48000 * ms / 1000)) / 48000
    return (0.3 * np.sin(2 * np.pi * 800 * t)).astype("<f4").tobytes()


def wait(cond, timeout: float = 2.0) -> None:  # type: ignore[no-untyped-def]
    end = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < end, "timed out"
        time.sleep(0.01)


def test_bridge_join_and_hangup_callbacks() -> None:
    joined, hung = [], []
    b = Bridge(on_connect=lambda: joined.append(1), on_hangup=lambda: hung.append(1))
    b.push_user_pcm(b"\x01\x00" * 160)  # ignored before connect
    assert b.take_uplink() == []
    b.connect()
    b.connect()
    assert joined == [1]
    b.push_user_pcm(b"\x01\x00" * 400)  # 800 bytes -> two 320-byte frames, 160 bytes held back
    assert [len(f) for f in b.take_uplink()] == [320, 320]
    b.to_user(b"\x02\x00" * 160)  # not active yet (orchestrator sets active while BRIDGED)
    assert b.pop_for_user() is None
    b.active = True
    b.to_user(b"\x02\x00" * 160)
    assert b.pop_for_user() == b"\x02\x00" * 160
    b.hangup()
    b.hangup()
    assert hung == [1]


def test_ws_user_audio_loopback() -> None:
    joined, hung = [], []
    bridge = Bridge(on_connect=lambda: joined.append(1), on_hangup=lambda: hung.append(1))
    app.state.bridges["call-u1"] = bridge
    try:
        with TestClient(app) as client, client.websocket_connect("/ws/user-audio/call-u1") as ws:
            assert ws.receive_json() == {"type": "status", "state": "joined"}
            assert joined == [1]
            for _ in range(5):
                ws.send_bytes(browser_sine())
            wait(lambda: len(bridge._uplink) >= 4)
            x = np.frombuffer(bridge.take_uplink()[1], dtype="<i2").astype(float)
            assert np.sqrt(np.mean(x**2)) > 3000
            bridge.active = True
            bridge.to_user(b"\x05\x00" * 160)
            got = ws.receive_bytes()
            assert len(got) == FRAME_BYTES
            ws.send_json({"type": "hangup"})
            wait(lambda: hung == [1])
    finally:
        app.state.bridges.pop("call-u1", None)


def test_ws_unknown_call() -> None:
    with TestClient(app) as client, client.websocket_connect("/ws/user-audio/nope") as ws:
        assert ws.receive_json()["type"] == "error"


async def test_full_bridged_conversation(tmp_path: Path) -> None:
    sim = await run_sim_call(SimConfig("nimbus_telecom", out_dir=tmp_path))
    assert journal_check.check_journal(sim.events, complete=True) == []
    assert sim.agent.truth.stages == ["greeting", "ack", "ask_mobile", "ticket", "goodbye"]  # type: ignore[union-attr]
    assert len(sim.user.spoken) == 3 and "refund" in sim.user.spoken[0].text
    ended = [e for e in sim.events if e["type"] == "call_ended"][0]
    assert ended["by"] == "callee"  # the company agent said goodbye and hung up
    # the user's voice is on user.wav, not caller.wav
    rec = tmp_path / "recordings"
    user = sf.read(rec / "user.wav", dtype="int16")[0].astype(float)
    caller = sf.read(rec / "caller.wav", dtype="int16")[0].astype(float)
    u0, u1 = sim.user.spoken[0].start, sim.user.spoken[0].end
    seg = slice(int(u0 * 8000), int(u1 * 8000))
    assert np.sqrt(np.mean(user[seg] ** 2)) > 300
    assert np.sqrt(np.mean(caller[seg] ** 2)) < 100
