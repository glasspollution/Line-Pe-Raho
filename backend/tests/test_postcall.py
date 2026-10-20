"""Post-call pipeline (N-5): ITN, date parsing, rules extraction, LLM path, and the end-to-end acceptance:
the summary of a simulated call has the auto-agent's real ticket ID and promised date, both verified."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from app.llm.schemas import Evidence, ExtractedField, Extraction
from app.postcall.extract import extract_rules, parse_spoken_date
from app.postcall.itn import itn
from app.postcall.pipeline import PostCallInputs, speech_spans, summarize
from app.sim.harness import SimConfig, run_sim_call
from app.sim.oracle_stt import Utterance
from eval import journal_check

DAY = dt.date(2026, 10, 21)


@pytest.mark.parametrize("raw,expected", [
    ("your complaint number is n m b six six four four eight one six two the refund",
     "your complaint number is NMB66448162 the refund"),
    ("it is nine zero zero zero zero zero zero zero zero one", "it is 9000000001"),
    ("two deductions of rupees 239 on 15th october", "two deductions of ₹239 on 15th october"),
    ("double five triple zero one", "550001"),
    ("press one for billing and two for data", "press one for billing and two for data"),
    ("one of them is a b c", "one of them is a b c"),
])
def test_itn(raw: str, expected: str) -> None:
    assert itn(raw) == expected


@pytest.mark.parametrize("text,iso", [("by the fifth of november", "2026-11-05"), ("by 5th November", "2026-11-05"),
                                      ("by the twenty first of january", "2027-01-21"), ("no date here", None)])
def test_parse_spoken_date(text: str, iso: str | None) -> None:
    got = parse_spoken_date(text, DAY)
    assert (got[0] if got else None) == iso


def seg(i: str, speaker: str, text: str, start: float = 0.0) -> dict[str, Any]:
    return {"id": i, "speaker": speaker, "start": start, "end": start + 3, "text": text, "text_itn": itn(text)}


def test_rules_extraction_ignores_ai_and_user_claims() -> None:
    segs = [seg("a1", "ai_caller", "complaint number ABC12345678 please"),
            seg("u1", "user", "my old complaint number is n m b one one one one one one one one"),
            seg("s9", "company", "this is rohan from nimbus telecom how may i help you"),
            seg("s10", "company", "i have raised a complaint for you your complaint number is n m b six six four four "
                                  "eight one six two the refund will be credited to your account by the fifth of november")]
    ex = extract_rules(segs, DAY)
    assert ex.ticket_id.value == "NMB66448162" and ex.ticket_id.evidence[0].segment_id == "s10"
    assert ex.agent_name.value == "Rohan" and ex.promised_by.value == "2026-11-05"
    assert ex.outcome == "ticket_raised" and ex.next_steps


class FakeLLM:
    def __init__(self, out: Extraction | None = None, fail: bool = False) -> None:
        self.out, self.fail = out, fail

    async def complete_json(self, system: str, user: str, model: type[Any], realtime: bool = True, **_: Any) -> Any:
        assert "2026-10-21" in system and realtime is False
        assert json.loads(user)["segments"][0]["id"]
        if self.fail:
            raise RuntimeError("down")
        return self.out


async def test_llm_extraction_is_still_verified(tmp_path: Path) -> None:
    events = [{"type": "transcript", "segment_id": "s1", "start": 1.0, "end": 4.0, "track": "callee",
               "text": "your complaint number is n m b one two three four five six seven eight"}]
    bad = Extraction(ticket_id=ExtractedField(value="NMB99999999",
                                              evidence=[Evidence(segment_id="s1", quote="your complaint number is")]),
                     outcome="ticket_raised")
    inp = PostCallInputs("c1", DAY, events, agent_spoken=[], user_spoken=[], out_dir=tmp_path)
    summary = await summarize(inp, llm=FakeLLM(bad))
    t = summary["fields"]["ticket_id"]
    assert summary["method"] == "llm" and t["value"] == "NMB99999999" and t["verified"] is False  # hallucination caught
    assert summary["unverified_count"] == 1 and (tmp_path / "summary.json").exists()
    fallback = await summarize(inp, llm=FakeLLM(fail=True))
    assert fallback["method"] == "rules" and fallback["fields"]["ticket_id"]["value"] == "NMB12345678"
    assert fallback["fields"]["ticket_id"]["verified"] is True
    assert fallback["fields"]["ticket_id"]["evidence"][0]["start"] == 1.0  # clip times for the ▶ button


def test_speech_spans_on_user_track() -> None:
    t = np.arange(8000 * 6) / 8000
    x = np.zeros_like(t)
    x[8000:16000] = 0.3 * np.sin(2 * np.pi * 300 * t[8000:16000])
    x[32000:40000] = 0.3 * np.sin(2 * np.pi * 300 * t[32000:40000])
    spans = speech_spans((x * 32767).astype("<i2").tobytes())
    assert len(spans) == 2 and abs(spans[0][0] - 1.0) < 0.05 and abs(spans[1][1] - 5.0) < 0.05


@pytest.mark.milestone("M3")
@pytest.mark.parametrize("company", ["nimbus_telecom", "sahyog_bank", "prakash_bijli_board"])
async def test_summary_matches_ground_truth(tmp_path: Path, company: str) -> None:
    sim = await run_sim_call(SimConfig(company, out_dir=tmp_path, call_date=DAY))
    assert journal_check.check_journal(sim.events, complete=True) == []
    ready = [e for e in sim.events if e["type"] == "summary_ready"][0]
    assert not ready.get("skipped") and ready["outcome"] == "ticket_raised"
    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    truth = sim.agent.truth  # type: ignore[union-attr]
    f = summary["fields"]
    assert f["ticket_id"]["value"] == truth.ticket_id and f["ticket_id"]["verified"]
    assert f["promised_by"]["value"] == truth.promised_by and f["promised_by"]["verified"]
    assert f["agent_name"]["value"] == truth.agent_name and f["agent_name"]["verified"]
    ev = f["ticket_id"]["evidence"][0]
    assert ev["speaker"] == "company" and ev["start"] < ev["end"]
    assert {s["speaker"] for s in summary["segments"]} == {"company", "ai_caller", "user"}


async def test_user_utterances_appear_in_timeline(tmp_path: Path) -> None:
    inp = PostCallInputs("c", DAY, [], agent_spoken=[Utterance("Namaste, main ek AI assistant hoon", 1, 3)],
                         user_spoken=[Utterance("my recharge failed", 5, 7)])
    s = await summarize(inp)
    assert [x["speaker"] for x in s["segments"]] == ["ai_caller", "user"] and s["outcome"] == "unresolved"


async def test_misheard_ticket_prefix_is_flagged_not_verified() -> None:
    events = [{"type": "transcript", "segment_id": "s1", "start": 1.0, "end": 9.0, "track": "callee",
               "text": "your complaint number is nmd 52138691 the refund will be credited"}]
    inp = PostCallInputs("c", DAY, events, agent_spoken=[], user_spoken=[], ticket_prefix="NMB")
    t = (await summarize(inp))["fields"]["ticket_id"]
    assert t["value"] == "NMD52138691" and t["verified"] is False and "possibly misheard" in t["reason"]


def test_phonetic_spelling_survives_a_misheard_letter() -> None:
    heard = "your complaint number is n for nashik, m for mumbai, d for bangalore, five two one three eight six nine one"
    assert "NMB52138691" in itn(heard)


def test_letters_join_a_digit_block_prisma_already_merged() -> None:
    heard = "your complaint number is n for nashik m for mumbai b for bangalore 56363973 the refund"
    assert "NMB56363973" in itn(heard)
    assert itn("we charged 239 for a 2 day pack") == "we charged 239 for a 2 day pack"  # ordinary numbers stay
