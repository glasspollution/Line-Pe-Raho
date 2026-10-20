"""Run a complete simulated call offline: orchestrator + virtual line + mock company + scripted human agent,
on a FakeClock (a 3-minute call takes seconds). Used by tests now and by the eval harness's fast mode.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from app.agent.bridge import Bridge
from app.agent.orchestrator import CallOrchestrator, CallResult
from app.audio.pstn_sim import PSTNConfig, PSTNSimulator
from app.audio.recorder import CallRecorder
from app.company.auto_agent import AutoAgentLeg
from app.company.engine import IVREngine, Scenario, load_tree
from app.company.voice import PromptAudio, SyntheticVoice
from app.journal import Journal
from app.line.clock import FakeClock
from app.line.virtual import VirtualLine
from app.llm.schemas import CallPlan
from app.postcall.pipeline import PostCallInputs, summarize
from app.sim.oracle_stt import OracleSTT, Utterance, engine_utterances
from app.sim.scripted_agent import ScriptedAgentLeg

DEMO_PROFILE = {"registered_mobile": "9000000001", "account_id": "100200300400"}

DEMO_PLANS: dict[str, CallPlan] = {
    "nimbus_telecom": CallPlan(
        company_id="nimbus_telecom", goal="refund for duplicate recharge deduction",
        goal_keywords=["recharge", "refund", "complaint", "billing"],
        spoken_intent={"en-IN": "duplicate recharge refund", "hi-en": "recharge refund"},
        profile_fields_needed=["registered_mobile"], user_language="hi-en"),
    "sahyog_bank": CallPlan(
        company_id="sahyog_bank", goal="refund for a failed card transaction",
        goal_keywords=["card", "transaction", "dispute", "refund"],
        spoken_intent={"en-IN": "my card transaction failed and I need a refund",
                       "hi-en": "my card transaction failed and I need a refund"},
        profile_fields_needed=["registered_mobile"], user_language="hi-en"),
    "prakash_bijli_board": CallPlan(
        company_id="prakash_bijli_board", goal="report a power cut in my area",
        goal_keywords=["power cut", "outage", "bijli", "complaint"],
        spoken_intent={"en-IN": "power cut complaint", "hi-en": "bijli kati hai"},
        profile_fields_needed=["account_id"], user_language="hi-en"),
}


USER_LINES: dict[str, list[str]] = {
    "nimbus_telecom": ["Hi, my recharge was deducted twice and I need a refund for the extra amount.",
                       "Sure, it is nine zero zero zero zero zero zero zero zero one.",
                       "No, that is all. Thank you very much."],
    "sahyog_bank": ["Hi, my card payment failed yesterday but the money was debited from my account.",
                    "Sure, it is nine zero zero zero zero zero zero zero zero one.",
                    "No, that is all. Thank you very much."],
    "prakash_bijli_board": ["Hi, there has been no electricity in our area since this morning.",
                            "Sure, it is nine zero zero zero zero zero zero zero zero one.",
                            "No, that is all. Thank you very much."],
}


@dataclass
class SimConfig:
    company_id: str = "nimbus_telecom"
    plan: CallPlan | None = None
    profile: dict[str, str] = field(default_factory=lambda: dict(DEMO_PROFILE))
    user_first_name: str = "Aarav"
    noise_profile: str = "snr10"
    noise_type: str = "street"
    packet_loss: float = 0.01
    hold_s: float = 20.0
    greets_first: bool = True
    agent_mode: str = "auto"  # auto = AutoAgentLeg (full conversation); scripted = greeting-only ScriptedAgentLeg
    user_join_delay_s: float | None = 4.0  # None = the user never joins
    max_talk_s: float = 90.0  # the user hangs up after this long bridged, whatever happens
    call_date: dt.date = field(default_factory=dt.date.today)
    summarize: bool = True  # run the post-call pipeline (rules unless an llm is given)
    llm: Any = None  # optional LLMClient-like object for post-call extraction
    ivr_llm: Any = None  # optional LLMClient-like object for IVR menus the rules can't decide
    user_lines: list[str] | None = None
    seed: int = 0
    out_dir: Path | None = None  # journal + recordings are written here (required)


class SimUser:
    """The person who asked for the call. Joins a little after the agent announces a human, then talks
    through the bridge: says the problem, answers what the company agent asks, and says goodbye.
    Stepped once per 20 ms tick (deterministic). What it says goes to `spoken` (ground truth)."""

    def __init__(self, orch: CallOrchestrator, bridge: Bridge, voice: PromptAudio, lines: list[str],
                 join_delay_s: float | None, max_talk_s: float) -> None:
        self.orch, self.bridge, self.voice = orch, bridge, voice
        self.lines = list(lines)
        self.join_delay_s, self.max_talk_s = join_delay_s, max_talk_s
        self.spoken: list[Utterance] = []
        self._waiting_since: float | None = None
        self._bridged_at: float | None = None
        self._speaking_until = 0.0
        self._floor = 0.0
        self._heard_speech_s = 0.0
        self._silence_s = 0.0
        self._agent_turn_done = False
        self._voiced_run = 0.0

    def utterances(self) -> list[Utterance]:
        return list(self.spoken)

    def _say(self, text: str, t: float) -> None:
        pcm = self.voice.render(text)
        self.bridge.push_user_pcm(pcm + b"\x00" * ((-len(pcm)) % 320))
        start = max(t + 0.04, self.orch.line.line_time())
        self._speaking_until = start + len(pcm) / 16000
        self.spoken.append(Utterance(text, round(start, 3), round(self._speaking_until, 3)))
        self._agent_turn_done = False  # wait for the agent's reply to THIS line
        self._heard_speech_s = self._silence_s = 0.0

    def _listen(self, t: float) -> None:
        while (frame := self.bridge.pop_for_user()) is not None:
            if t < self._speaking_until + 0.3:
                continue  # what you hear while you're talking doesn't count as the agent's turn
            x = np.frombuffer(frame, dtype="<i2").astype(np.float64)
            level = 20 * np.log10(max(float(np.sqrt(np.mean(x * x))), 1e-9) / 32768)
            self._floor = min(level, self._floor + 0.05)
            if level > self._floor + 8.0:
                self._voiced_run += 0.02
                if self._voiced_run >= 0.1:  # >= 100 ms: speech, not a noise spike
                    self._heard_speech_s += 0.02
                    self._silence_s = 0.0
                    continue
            else:
                self._voiced_run = 0.0
            if self._voiced_run < 0.1:
                self._silence_s += 0.02
                if self._heard_speech_s >= 0.5 and self._silence_s >= 1.5:  # spelled IDs pause at commas
                    self._agent_turn_done = True
                    self._heard_speech_s = 0.0

    def step(self, t: float) -> None:
        st = self.orch.state
        if st == "WAITING_FOR_USER" and self.join_delay_s is not None:
            if self._waiting_since is None:
                self._waiting_since = t
            elif t >= self._waiting_since + self.join_delay_s:
                self.bridge.connect()  # opening the audio channel is the join
        elif st == "BRIDGED":
            if self._bridged_at is None:
                self._bridged_at = t
            self._listen(t)
            if t >= self._bridged_at + self.max_talk_s:
                self.orch.end_call()
                return
            if t < self._speaking_until or not self.lines:
                return
            first = not self.spoken and t >= self._bridged_at + 1.0 and self._silence_s >= 1.5
            if first or self._agent_turn_done:
                self._agent_turn_done = False
                self._say(self.lines.pop(0), t)


@dataclass
class SimResult:
    result: CallResult
    events: list[dict[str, Any]]
    engine: IVREngine
    agent: AutoAgentLeg | ScriptedAgentLeg
    user: SimUser
    orch: CallOrchestrator
    line_seconds: float


def caller_hearing(sources: list[Any]):  # type: ignore[no-untyped-def]
    """Exact 'transcription' of what the caller side said in [t0, t1] (sim stand-in for Prisma REST)."""

    async def hear(pcm: bytes, t0: float, t1: float) -> str:
        said = [u for src in sources for u in src() if u.end > t0 - 0.3 and u.start < t1 + 0.3]
        return " ".join(u.text for u in sorted(said, key=lambda u: u.start))

    return hear


async def run_sim_call(cfg: SimConfig) -> SimResult:
    assert cfg.out_dir is not None, "out_dir is required (journal and recordings are written there)"
    tree = load_tree(cfg.company_id)
    plan = cfg.plan or DEMO_PLANS[cfg.company_id]
    holder: dict[str, Any] = {}
    agent: AutoAgentLeg | ScriptedAgentLeg
    if cfg.agent_mode == "auto":
        agent = AutoAgentLeg(cfg.company_id, tree["display_name"], SyntheticVoice(f0=175),
                             hear=caller_hearing([lambda: holder["orch"].spoken, lambda: holder["user"].spoken]),
                             greets_first=cfg.greets_first, seed=cfg.seed, today=cfg.call_date)
    else:
        agent = ScriptedAgentLeg(SyntheticVoice(f0=175), greets_first=cfg.greets_first,
                                 greeting=f"Hello, this is Priya from {tree['display_name']}. How may I help you?")

    async def recognizer(pcm: bytes) -> str:  # the IVR "hears" whatever our agent last said as a voice reply
        orch: CallOrchestrator = holder["orch"]
        replies = [e["text"] for e in orch.j.events if e["type"] == "decision" and e.get("kind") == "voice_reply"]
        return replies[-1] if replies else ""

    engine = IVREngine(tree, SyntheticVoice(f0=215), agent_leg=agent, recognizer=recognizer,
                       scenario=Scenario(hold_s=cfg.hold_s, seed=cfg.seed, answer_after_s=2.0))
    rec = CallRecorder(cfg.out_dir / "recordings")
    up = PSTNSimulator(PSTNConfig(profile=cfg.noise_profile, noise_type=cfg.noise_type,
                                  packet_loss=cfg.packet_loss, seed=cfg.seed * 2 + 1))
    down = PSTNSimulator(PSTNConfig(profile=cfg.noise_profile, noise_type=cfg.noise_type,
                                    packet_loss=cfg.packet_loss, seed=cfg.seed * 2 + 2))

    def tap(t: float, sent: bytes, heard: bytes) -> None:
        bridged = "orch" in holder and holder["orch"].state == "BRIDGED"
        rec.write("user" if bridged else "caller", sent, t)
        rec.write("callee", heard, t)

    line = VirtualLine(lambda _: engine, uplink=up, downlink=down, clock=FakeClock(), tap=tap)
    stt = OracleSTT([lambda: engine_utterances(engine.log), agent.utterances])
    journal = Journal(f"sim-{cfg.company_id}-{cfg.seed}", cfg.out_dir / "journal.jsonl", clock=lambda: holder["orch"].t)
    bridge = Bridge()
    async def summarizer(o: CallOrchestrator) -> dict[str, Any]:
        inp = PostCallInputs(call_id=journal.call_id, call_date=cfg.call_date, events=list(journal.events),
                             agent_spoken=list(o.spoken), user_spoken=list(holder["user"].spoken),
                             out_dir=cfg.out_dir, ticket_prefix=tree.get("ticket_prefix"))
        return await summarize(inp, llm=cfg.llm)

    orch = CallOrchestrator(plan, cfg.profile, cfg.user_first_name, line, stt, SyntheticVoice(f0=245), journal,
                            bridge=bridge, summarizer=summarizer if cfg.summarize else None,
                            llm=cfg.ivr_llm)
    bridge.on_connect = orch.user_join
    bridge.on_hangup = orch.end_call
    holder["orch"] = orch
    user = SimUser(orch, bridge, SyntheticVoice(f0=135), cfg.user_lines or USER_LINES[cfg.company_id],
                   cfg.user_join_delay_s, cfg.max_talk_s)
    holder["user"] = user
    orch.on_frame = lambda t, frame: user.step(t)
    result = await orch.run()
    rec.close()
    journal.close()
    return SimResult(result, list(journal.events), engine, agent, user, orch, line.line_time())
