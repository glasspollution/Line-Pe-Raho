"""Auto-agent conversation flow, driven tick by tick with a fake 'hear' (N-3)."""

from __future__ import annotations

import asyncio
import datetime as dt

from app.audio.frames import FRAME_BYTES
from app.company.auto_agent import AutoAgentLeg, has_number, spell_id, spoken_date
from app.company.voice import SyntheticVoice

CALLER = SyntheticVoice(f0=140)
LINES_FULL = ["Namaste, main ek AI assistant hoon aur Aarav ji ki taraf se call kar rahi hoon",
              "Bas ek second, woh line par aa rahe hain",
              "Hi, my recharge was deducted twice and I need a refund",
              "nine zero zero zero zero zero zero zero zero one",
              "No, that is all, thank you very much"]


async def converse(agent: AutoAgentLeg, caller_lines: list[str], max_s: float = 200.0) -> float:
    """Speak each caller line once the agent has gone quiet; return when the agent hangs up."""
    t, queue = 0.0, list(caller_lines)
    agent.on_connect(0.0)
    out: list[bytes] = []
    quiet_since = 0.0
    caller_done = 0.0
    while t < max_s and not agent.ended:
        frame = agent.next_frame(t)
        agent_talking = frame != b"\x00" * FRAME_BYTES or agent._out
        quiet_since = quiet_since if not agent_talking else t
        if out and len(out) == 1:
            caller_done = t  # the caller finishes its line on this tick
        if (not out and queue and not agent_talking and agent._pending is None
                and t - max(quiet_since, caller_done) > 1.5):  # a real caller waits for a reply
            pcm = CALLER.render(queue.pop(0))
            pcm += b"\x00" * ((-len(pcm)) % FRAME_BYTES)
            out = [pcm[i : i + FRAME_BYTES] for i in range(0, len(pcm), FRAME_BYTES)]
        agent.receive(out.pop(0) if out else b"\x00" * FRAME_BYTES, t)
        await asyncio.sleep(0)
        t += 0.02
    return t


def scripted_hear(texts: dict[int, str]):  # type: ignore[no-untyped-def]
    """Hear = return the i-th caller line (what an exact transcriber would return)."""
    calls = {"n": 0}

    async def hear(pcm: bytes, t0: float, t1: float) -> str:
        i = calls["n"]
        calls["n"] += 1
        return texts.get(i, "")

    return hear


async def test_full_conversation() -> None:
    hear = scripted_hear(dict(enumerate(LINES_FULL)))
    agent = AutoAgentLeg("nimbus_telecom", "Nimbus Telecom", SyntheticVoice(f0=170), hear=hear, seed=3)
    t = await converse(agent, LINES_FULL)
    assert agent.ended and t < 200
    assert agent.truth.stages == ["greeting", "ack", "ask_mobile", "ticket", "goodbye"]
    tr = agent.truth
    assert tr.ticket_id.startswith("NMB") and len(tr.ticket_id) == 11
    assert spell_id(tr.ticket_id) in agent.text["ticket"] and not any(c.isdigit() for c in agent.text["ticket"])
    assert dt.date.fromisoformat(tr.promised_by) > dt.date(2026, 10, 21)
    assert tr.greeting_start is not None and tr.greeting_end is not None and tr.greeting_end > tr.greeting_start


async def test_reasks_mobile_once_then_proceeds() -> None:
    lines = ["Namaste, main ek AI assistant hoon aur Aarav ji ki taraf se call kar rahi hoon",
             "My problem is that my card payment failed yesterday", "I am not sure what to say here",
             "Sorry I still do not have it with me", "No that is all thank you very much"]
    hear = scripted_hear(dict(enumerate(lines)))
    agent = AutoAgentLeg("sahyog_bank", "Sahyog Bank", SyntheticVoice(), hear=hear, seed=1)
    await converse(agent, lines)
    assert agent.truth.stages == ["greeting", "ack", "ask_mobile", "repeat_mobile", "ticket", "goodbye"]
    assert agent.truth.ticket_id.startswith("SHB")


async def test_silent_agent_answers_probe_first() -> None:
    lines = ["Hello? Main ek AI assistant hoon, kya aap mujhe sun pa rahe hain?",
             "Namaste, main ek AI assistant hoon aur Aarav ji ki taraf se call kar rahi hoon",
             "There is no electricity in our area since this morning", "My number is 9000000001 thank you",
             "No that is all thank you very much"]
    hear = scripted_hear(dict(enumerate(lines)))
    agent = AutoAgentLeg("prakash_bijli_board", "Prakash Bijli Board", SyntheticVoice(), hear=hear,
                         greets_first=False)
    await converse(agent, lines)
    assert agent.truth.stages[0] == "probe_reply" and agent.truth.greeting_start is None
    assert "ticket" in agent.truth.stages and agent.truth.ticket_id.startswith("PBB")


def test_helpers() -> None:
    assert spell_id("NMB12") == "N for Nashik, M for Mumbai, B for Bangalore, one two"
    assert spoken_date(dt.date(2026, 11, 5)) == "the fifth of November"
    assert has_number("nine zero zero zero zero zero zero zero zero one") and has_number("9000000001")
    assert not has_number("refund for my recharge")


def test_lines_are_known_upfront_and_seeded() -> None:
    a = AutoAgentLeg("nimbus_telecom", "Nimbus Telecom", SyntheticVoice(), seed=7)
    b = AutoAgentLeg("nimbus_telecom", "Nimbus Telecom", SyntheticVoice(), seed=7)
    assert a.lines() == b.lines() and a.truth.ticket_id == b.truth.ticket_id
    assert len(a.lines()) == 7
