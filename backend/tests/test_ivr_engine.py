"""IVR engine over VirtualLine (FakeClock). Includes the M1 acceptance: scripted DTMF press-through of the
Nimbus tree at snr10 reaches the hold queue (and the same for the other two companies)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest

from app.audio.frames import FRAME_BYTES
from app.audio.pstn_sim import PSTNConfig, PSTNSimulator
from app.company.engine import IVREngine, Scenario, list_companies, load_tree
from app.company.voice import SyntheticVoice
from app.line.clock import FakeClock
from app.line.virtual import VirtualLine

MOBILE = "9000000001"
ACCOUNT = "100200300400"


async def setup(company: str, profile: str = "snr10", scenario: Scenario | None = None,
                recognizer: Any = None) -> tuple[VirtualLine, IVREngine, asyncio.Task[None]]:
    engine = IVREngine(load_tree(company), SyntheticVoice(), recognizer=recognizer,
                       scenario=scenario or Scenario(hold_s=5.0, answer_after_s=1.0))
    up = PSTNSimulator(PSTNConfig(profile=profile, noise_type="street", packet_loss=0.01, gain_db=0.0, seed=1))
    down = PSTNSimulator(PSTNConfig(profile=profile, seed=2))
    line = VirtualLine(lambda _: engine, uplink=up, downlink=down, clock=FakeClock())

    async def drain_inbound() -> None:
        async for _ in line.inbound():
            pass

    reader = asyncio.create_task(drain_inbound())
    await line.dial(company)
    return line, engine, reader


async def until(cond: Callable[[], bool], line: VirtualLine, max_s: float = 600.0) -> None:
    while not cond():
        if line.line_time() > max_s or line.state == "ended" and not cond():
            raise AssertionError(f"condition not met by t={line.line_time():.1f}s")
        await asyncio.sleep(0)


def listening(engine: IVREngine, node: str) -> Callable[[], bool]:
    return lambda: engine.current_node == node and engine.mode == "listen"


def prompt_done(engine: IVREngine, node: str) -> Callable[[], bool]:
    return lambda: engine.current_node == node and not engine._play


async def press(line: VirtualLine, digits: str) -> None:
    await line.send_dtmf(digits)
    await line.drain()


def kinds(engine: IVREngine, kind: str) -> list[dict[str, Any]]:
    return [e for e in engine.log if e["kind"] == kind]


async def finish(line: VirtualLine, reader: asyncio.Task[None]) -> None:
    await line.hangup()
    await reader


@pytest.mark.milestone("M1")
async def test_m1_acceptance_nimbus_press_through_snr10() -> None:
    line, eng, reader = await setup("nimbus_telecom")
    await until(listening(eng, "welcome"), line)
    await press(line, "1")
    await until(listening(eng, "main_en"), line)
    await press(line, "1")
    await until(prompt_done(eng, "billing_en"), line)
    await press(line, MOBILE + "#")
    await until(listening(eng, "billing_menu_en"), line)
    await press(line, "2")
    await until(lambda: eng.mode == "hold", line)
    assert eng.current_node == "queue_refund"
    assert eng.collected == {"registered_mobile": MOBILE}
    await until(lambda: eng.mode == "agent", line)
    assert [e["target"] for e in kinds(eng, "option_selected")] == ["main_en", "billing_en", "queue_refund"]
    assert not kinds(eng, "invalid_option") and not kinds(eng, "collect_invalid")
    await finish(line, reader)


@pytest.mark.milestone("M1")
async def test_sahyog_press_through_with_voice_prompt() -> None:
    heard: list[int] = []

    async def recognizer(pcm: bytes) -> str:
        heard.append(len(pcm))
        return "my card transaction failed and I need a refund"

    line, eng, reader = await setup("sahyog_bank", recognizer=recognizer)
    await until(listening(eng, "welcome"), line)
    await press(line, "2")
    await until(listening(eng, "main_en"), line)
    await press(line, "1")
    await until(prompt_done(eng, "cards_en"), line)
    await press(line, MOBILE + "#")
    await until(listening(eng, "cards_menu_en"), line)
    await press(line, "2")
    await until(lambda: eng.current_node == "dispute_ask_en" and eng._ask is not None, line)
    speech = SyntheticVoice(f0=140).render("my card transaction failed and I need a refund")
    for i in range(0, len(speech) - FRAME_BYTES + 1, FRAME_BYTES):
        await line.send_audio(speech[i : i + FRAME_BYTES])
    await line.drain()
    await until(lambda: eng.mode == "hold", line)
    routed = kinds(eng, "ask_routed")[0]
    assert routed["matched"] and routed["target"] == "connect_en" and heard
    assert eng.current_node == "queue_main" and eng.collected["registered_mobile"] == MOBILE
    assert any("connecting you" in e["text"].lower() for e in kinds(eng, "say"))
    await finish(line, reader)


@pytest.mark.milestone("M1")
async def test_bijli_press_through_account_number() -> None:
    line, eng, reader = await setup("prakash_bijli_board")
    await until(listening(eng, "welcome"), line)
    await press(line, "2")
    await until(listening(eng, "main_en"), line)
    await press(line, "1")
    await until(prompt_done(eng, "outage_en"), line)
    await press(line, ACCOUNT + "#")
    await until(lambda: eng.mode == "hold", line)
    assert eng.current_node == "queue_outage" and eng.collected == {"account_id": ACCOUNT}
    await finish(line, reader)


async def test_barge_in_during_prompt() -> None:
    line, eng, reader = await setup("nimbus_telecom", profile="clean")
    await until(lambda: eng.current_node == "welcome" and eng.mode == "play" and len(eng._play) > 50, line)
    await press(line, "1")
    await until(lambda: eng.current_node == "main_en", line)
    await finish(line, reader)


async def test_invalid_option_replays_menu() -> None:
    line, eng, reader = await setup("nimbus_telecom", profile="clean")
    await until(listening(eng, "welcome"), line)
    await press(line, "7")
    await until(lambda: any(e["kind"] == "node_enter" and e["repeat"] for e in eng.log), line)
    assert kinds(eng, "invalid_option")[0]["digit"] == "7"
    assert any(e["text"].startswith("Sorry") for e in kinds(eng, "say"))
    await finish(line, reader)


async def test_timeouts_repeat_then_hang_up() -> None:
    line, eng, reader = await setup("nimbus_telecom", profile="clean")
    await until(lambda: eng.ended, line, max_s=120)
    assert sum(1 for e in kinds(eng, "node_enter") if e["repeat"]) == 2  # max_repeats: 2
    assert kinds(eng, "ivr_hangup") and line.state == "ended"
    await reader


async def test_wrong_length_collect_reprompts() -> None:
    line, eng, reader = await setup("nimbus_telecom", profile="clean")
    await until(listening(eng, "welcome"), line)
    await press(line, "1")
    await until(listening(eng, "main_en"), line)
    await press(line, "1")
    await until(prompt_done(eng, "billing_en"), line)
    await press(line, "12345#")
    await until(lambda: bool(kinds(eng, "collect_invalid")), line)
    await until(prompt_done(eng, "billing_en"), line)
    await press(line, MOBILE + "#")
    await until(lambda: eng.current_node == "billing_menu_en", line)
    assert eng.collected["registered_mobile"] == MOBILE
    await finish(line, reader)


async def test_hold_announcements_cadence_then_agent() -> None:
    line, eng, reader = await setup("nimbus_telecom", profile="clean", scenario=Scenario(hold_s=90, answer_after_s=0.5))
    await until(listening(eng, "welcome"), line)
    await press(line, "1")
    await until(listening(eng, "main_en"), line)
    await press(line, "2")  # data issues
    await until(listening(eng, "data_en"), line)
    await press(line, "0")  # executive -> queue_general
    await until(lambda: eng.mode == "agent", line, max_s=400)
    hold = kinds(eng, "hold_start")[0]
    anns = kinds(eng, "announcement")
    assert len(anns) == 2  # every 40 s within a 90 s hold (at 40 and 80)
    assert abs(anns[0]["t"] - hold["t"] - 40) < 0.1
    assert abs(kinds(eng, "agent_connect")[0]["t"] - hold["t"] - 90) < 0.1
    await finish(line, reader)


def test_directory() -> None:
    ids = {c["id"] for c in list_companies()}
    assert {"nimbus_telecom", "sahyog_bank", "prakash_bijli_board"} <= ids
    with pytest.raises(KeyError):
        load_tree("not_a_company")
