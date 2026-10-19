"""End-to-end simulated calls (T-206): the orchestrator drives a real virtual line through each mock
company's IVR and hold, detects the scripted human, discloses, and bridges. Every journal must pass
eval/journal_check (J1–J7)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.sim.harness import SimConfig, SimResult, run_sim_call
from eval import journal_check


def types(events: list[dict[str, Any]], typ: str) -> list[dict[str, Any]]:
    return [e for e in events if e["type"] == typ]


def states(events: list[dict[str, Any]]) -> list[str]:
    return [e["to"] for e in types(events, "state_changed")]


def check(sim: SimResult) -> None:
    problems = journal_check.check_journal(sim.events, complete=True)
    assert problems == [], problems


@pytest.mark.milestone("M2")
async def test_first_full_call_nimbus_snr10(tmp_path: Path) -> None:
    sim = await run_sim_call(SimConfig("nimbus_telecom", out_dir=tmp_path))
    check(sim)
    st = states(sim.events)
    assert st[-1] == "DONE" and "BRIDGED" in st and "HOLD" in st
    assert sim.result.path == ["1", "1", "2"]
    assert sim.engine.collected == {"registered_mobile": "9000000001"}
    assert not sim.result.probe_used
    assert sim.result.time_to_detect_s is not None and sim.result.time_to_detect_s <= 3.0
    assert types(sim.events, "human_detected") and types(sim.events, "user_joined")
    assert "9000000001" not in (tmp_path / "journal.jsonl").read_text(encoding="utf-8")
    assert (tmp_path / "recordings" / "mixed.wav").exists()


@pytest.mark.milestone("M2")
async def test_sahyog_voice_prompt_and_connecting_prompt(tmp_path: Path) -> None:
    sim = await run_sim_call(SimConfig("sahyog_bank", out_dir=tmp_path))
    check(sim)
    assert states(sim.events)[-1] == "DONE"
    assert any(e.get("kind") == "voice_reply" for e in types(sim.events, "decision"))
    routed = [e for e in sim.engine.log if e["kind"] == "ask_routed"]
    assert routed and routed[0]["matched"]
    assert sim.result.path[0] == "2"  # English is offered second at Sahyog


@pytest.mark.milestone("M2")
async def test_bijli_account_number_path(tmp_path: Path) -> None:
    sim = await run_sim_call(SimConfig("prakash_bijli_board", out_dir=tmp_path))
    check(sim)
    assert states(sim.events)[-1] == "DONE"
    assert sim.engine.collected == {"account_id": "100200300400"}


@pytest.mark.milestone("M2")
async def test_silent_agent_needs_probe(tmp_path: Path) -> None:
    sim = await run_sim_call(SimConfig("nimbus_telecom", greets_first=False, out_dir=tmp_path))
    check(sim)
    assert sim.result.probe_used
    probe = types(sim.events, "probe_sent")[0]
    assert probe["discloses_ai"] and "AI" in probe["text"]
    assert states(sim.events)[-1] == "DONE" and "BRIDGED" in states(sim.events)


@pytest.mark.milestone("M2")
async def test_user_never_joins(tmp_path: Path) -> None:
    sim = await run_sim_call(SimConfig("nimbus_telecom", user_join_delay_s=None, out_dir=tmp_path))
    check(sim)
    assert len(types(sim.events, "keepalive_sent")) == 6
    replies = [e for e in types(sim.events, "decision") if e.get("kind") == "voice_reply"]
    assert replies and "complaint number" in replies[-1]["text"]
    assert "BRIDGED" not in states(sim.events) and states(sim.events)[-1] == "DONE"


@pytest.mark.milestone("M2")
@pytest.mark.parametrize("profile", ["clean", "snr20", "snr5"])
async def test_nimbus_across_noise(tmp_path: Path, profile: str) -> None:
    sim = await run_sim_call(SimConfig("nimbus_telecom", noise_profile=profile, out_dir=tmp_path))
    check(sim)
    assert states(sim.events)[-1] == "DONE" and sim.result.path == ["1", "1", "2"]
