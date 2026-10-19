"""Golden journal is valid; each invariant fires on a minimal mutation of it."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from eval import journal_check as J

GOLDEN = Path(J.__file__).parent / "fixtures" / "golden_call.jsonl"


@pytest.fixture
def events() -> list[dict[str, Any]]:
    return J.read_jsonl(GOLDEN)


def codes(problems: list[str]) -> set[str]:
    return {p.split()[0] for p in problems}


def test_golden_is_valid(events: list[dict[str, Any]]) -> None:
    assert J.check_journal(events, complete=True) == []


def test_golden_uses_only_synthetic_brand(events: list[dict[str, Any]]) -> None:
    assert all(e["call_id"] == "golden-nimbus-001" for e in events)


def test_unknown_type_and_missing_fields(events: list[dict[str, Any]]) -> None:
    ev = copy.deepcopy(events)
    ev[3]["type"] = "telepathy"
    del ev[4]["call_id"]
    assert "J1" in codes(J.check_journal(ev))


def test_time_goes_backwards(events: list[dict[str, Any]]) -> None:
    ev = copy.deepcopy(events)
    ev[5]["t"] = 0.1
    assert "J2" in codes(J.check_journal(ev))


def test_illegal_transition(events: list[dict[str, Any]]) -> None:
    ev = [e for e in events if not (e["type"] == "state_changed" and e["to"] in ("HUMAN_CONFIRMED", "DISCLOSING"))]
    assert "J3" in codes(J.check_journal(ev))


def test_failed_is_always_legal(events: list[dict[str, Any]]) -> None:
    ev = events[:10] + [{"type": "state_changed", "t": 23.0, "call_id": "golden-nimbus-001",
                         "from": "IVR", "to": "FAILED"}]
    assert J.check_journal(ev, complete=True) == []


def test_probe_without_disclosure_violates_j4(events: list[dict[str, Any]]) -> None:
    i = next(i for i, e in enumerate(events) if e["type"] == "human_suspected")
    probe = {"type": "probe_sent", "t": events[i]["t"], "call_id": "golden-nimbus-001",
             "text": "Hello? Ji, kya aap mujhe sun pa rahe hain?"}
    ev = events[: i + 1] + [probe] + events[i + 1:]
    assert "J4" in codes(J.check_journal(ev))
    probe["discloses_ai"] = True  # a probe that itself discloses is fine (see BOARD D-1)
    assert "J4" not in codes(J.check_journal(ev))


def test_keepalive_before_disclosure_violates_j5(events: list[dict[str, Any]]) -> None:
    ev = [e for e in events if not (e["type"] == "decision" and e.get("kind") == "disclosure")]
    assert "J5" in codes(J.check_journal(ev))


def test_dtmf_during_hold_and_speech_while_bridged(events: list[dict[str, Any]]) -> None:
    ev = copy.deepcopy(events)
    hold_idx = next(i for i, e in enumerate(ev) if e["type"] == "hold_started")
    ev.insert(hold_idx + 1, {"type": "dtmf_sent", "t": ev[hold_idx]["t"], "call_id": "golden-nimbus-001",
                             "digits": "0"})
    bridged = next(i for i, e in enumerate(ev) if e.get("to") == "BRIDGED")
    ev.insert(bridged + 1, {"type": "keepalive_sent", "t": ev[bridged]["t"], "call_id": "golden-nimbus-001"})
    problems = J.check_journal(ev)
    assert sum(p.startswith("J6") for p in problems) == 2


def test_incomplete_journal(events: list[dict[str, Any]]) -> None:
    assert "J7" in codes(J.check_journal(events[:20], complete=True))
