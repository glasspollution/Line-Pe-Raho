"""Journal, recorder and replay. Acceptance for T-108: a recorded simulated call produces the 4 aligned
WAVs and a journal that passes eval/journal_check."""

from __future__ import annotations

import asyncio
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from app.audio.pstn_sim import PSTNConfig, PSTNSimulator
from app.audio.recorder import CallRecorder
from app.company.engine import IVREngine, Scenario, load_tree
from app.company.voice import SyntheticVoice
from app.journal import EVENT_TYPES, Journal, mask_digits, read_journal
from app.line.clock import FakeClock
from app.line.virtual import VirtualLine
from eval import journal_check
from scripts import replay


@pytest.mark.parametrize("raw,masked", [("1", "1"), ("0#", "0#"), ("9000000001#", "******0001#"),
                                        ("100200300400", "********0400")])
def test_mask_digits(raw: str, masked: str) -> None:
    assert mask_digits(raw) == masked


def test_event_types_match_eval_checker() -> None:
    assert set(EVENT_TYPES) == journal_check.EVENT_TYPES


async def test_journal_rejects_bad_events_and_fans_out(tmp_path: Path) -> None:
    j = Journal("c1", tmp_path / "j.jsonl", clock=lambda: 1.5)
    with pytest.raises(ValueError):
        j.emit("telepathy")
    with pytest.raises(ValueError):
        j.emit("decision", call_id="other")
    j.emit("state_changed", **{"from": "IDLE", "to": "PLANNING"})
    q = j.subscribe()  # late subscriber gets the backlog
    j.emit("decision", kind="menu_press", digit="1")
    got = [await q.get(), await q.get()]
    assert [e["type"] for e in got] == ["state_changed", "decision"] and got[1]["t"] == 1.5
    j.close()
    assert len(read_journal(tmp_path / "j.jsonl")) == 2


def test_recorder_aligns_tracks(tmp_path: Path) -> None:
    rec = CallRecorder(tmp_path)
    one = np.full(160, 1000, dtype="<i2").tobytes()
    rec.write("callee", one, 0.0)
    rec.write("caller", one, 1.0)  # starts 1 s later -> 1 s of leading silence
    rec.write("caller", one, 1.0)  # duplicate frame is ignored, not appended
    rec.close()
    data = {t: sf.read(tmp_path / f"{t}.wav", dtype="int16")[0] for t in ("caller", "callee", "user", "mixed")}
    assert len({len(v) for v in data.values()}) == 1  # all tracks the same length
    assert len(data["caller"]) == 8000 + 160
    assert data["caller"][:8000].max() == 0 and data["caller"][8000] == 1000
    assert data["mixed"][0] == 1000 and data["mixed"][8000] == 1000


async def test_recorded_simulated_call_journal_is_valid(tmp_path: Path) -> None:
    clock = FakeClock()
    engine = IVREngine(load_tree("nimbus_telecom"), SyntheticVoice(), scenario=Scenario(hold_s=3.0, answer_after_s=1.0))
    rec = CallRecorder(tmp_path / "rec")
    line = VirtualLine(lambda _: engine, uplink=PSTNSimulator(PSTNConfig(profile="snr10", seed=1)),
                       downlink=PSTNSimulator(PSTNConfig(profile="snr10", seed=2)), clock=clock,
                       tap=lambda t, sent, heard: (rec.write("caller", sent, t), rec.write("callee", heard, t)))
    j = Journal("sim-001", tmp_path / "journal.jsonl", clock=line.line_time)

    async def drain_inbound() -> None:
        async for _ in line.inbound():
            pass

    async def until(cond) -> None:  # type: ignore[no-untyped-def]
        while not cond():
            assert line.line_time() < 300
            await asyncio.sleep(0)

    async def press(digits: str) -> None:
        j.emit("dtmf_sent", digits_masked=mask_digits(digits), n_digits=len(digits))
        await line.send_dtmf(digits)
        await line.drain()

    reader = asyncio.create_task(drain_inbound())
    j.emit("state_changed", **{"from": "IDLE", "to": "PLANNING"})
    j.emit("state_changed", **{"from": "PLANNING", "to": "DIALING"})
    await line.dial("nimbus_telecom")
    await line.wait_connected()
    j.emit("state_changed", **{"from": "DIALING", "to": "IVR"})
    for node, digits in (("welcome", "1"), ("main_en", "1"), ("billing_en", "9000000001#"), ("billing_menu_en", "2")):
        await until(lambda n=node: engine.current_node == n and not engine._play)  # type: ignore[misc]
        await press(digits)
    await until(lambda: engine.mode == "hold")
    j.emit("state_changed", **{"from": "IVR", "to": "HOLD"})
    j.emit("hold_started", reason="ivr queue")
    await until(lambda: engine.mode == "agent")
    j.emit("error", message="test ends before a human is detected")
    j.emit("state_changed", **{"from": "HOLD", "to": "FAILED"})
    await line.hangup()
    await reader
    rec.close()
    j.close()

    events = read_journal(tmp_path / "journal.jsonl")
    assert journal_check.check_journal(events, complete=True) == []
    assert "9000000001" not in (tmp_path / "journal.jsonl").read_text(encoding="utf-8")  # masked
    lens = {t: sf.info(tmp_path / "rec" / f"{t}.wav").frames for t in ("caller", "callee", "user", "mixed")}
    assert len(set(lens.values())) == 1
    assert abs(lens["callee"] / 8000 - line.line_time()) < 0.1
    callee = sf.read(tmp_path / "rec" / "callee.wav", dtype="int16")[0]
    assert np.abs(callee[: 8000]).max() > 1000  # ringback audible in the first second

    rendered = "\n".join(replay.render(e) for e in events)
    assert "IVR -> HOLD" in rendered and "******0001#" in rendered
