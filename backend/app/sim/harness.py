"""Run a complete simulated call offline: orchestrator + virtual line + mock company + scripted human agent,
on a FakeClock (a 3-minute call takes seconds). Used by tests now and by the eval harness's fast mode.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.agent.orchestrator import CallOrchestrator, CallResult
from app.audio.pstn_sim import PSTNConfig, PSTNSimulator
from app.audio.recorder import CallRecorder
from app.company.engine import IVREngine, Scenario, load_tree
from app.company.voice import SyntheticVoice
from app.journal import Journal
from app.line.clock import FakeClock
from app.line.virtual import VirtualLine
from app.llm.schemas import CallPlan
from app.sim.oracle_stt import OracleSTT, engine_utterances
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
    user_join_delay_s: float | None = 4.0  # None = the user never joins
    talk_s: float = 6.0  # how long the bridged conversation lasts before the user hangs up
    seed: int = 0
    out_dir: Path | None = None  # journal + recordings; None = in-memory journal file in a temp dir is required


class SimUser:
    """The person who asked for the call: joins a little after the agent says a human is on the line,
    talks for a while, then hangs up. Stepped once per 20 ms tick (deterministic, no polling)."""

    def __init__(self, orch: CallOrchestrator, join_delay_s: float | None, talk_s: float) -> None:
        self.orch, self.join_delay_s, self.talk_s = orch, join_delay_s, talk_s
        self._waiting_since: float | None = None
        self._bridged_at: float | None = None

    def step(self, t: float) -> None:
        st = self.orch.state
        if st == "WAITING_FOR_USER" and self.join_delay_s is not None:
            if self._waiting_since is None:
                self._waiting_since = t
            elif t >= self._waiting_since + self.join_delay_s:
                self.orch.user_join()
        elif st == "BRIDGED":
            if self._bridged_at is None:
                self._bridged_at = t
            elif t >= self._bridged_at + self.talk_s:
                self.orch.end_call()


@dataclass
class SimResult:
    result: CallResult
    events: list[dict[str, Any]]
    engine: IVREngine
    agent: ScriptedAgentLeg
    line_seconds: float


async def run_sim_call(cfg: SimConfig) -> SimResult:
    assert cfg.out_dir is not None, "out_dir is required (journal and recordings are written there)"
    tree = load_tree(cfg.company_id)
    plan = cfg.plan or DEMO_PLANS[cfg.company_id]
    agent = ScriptedAgentLeg(SyntheticVoice(f0=175), greets_first=cfg.greets_first,
                             greeting=f"Hello, this is Priya from {tree['display_name']}. How may I help you?")
    holder: dict[str, Any] = {}

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
        rec.write("caller", sent, t)
        rec.write("callee", heard, t)

    line = VirtualLine(lambda _: engine, uplink=up, downlink=down, clock=FakeClock(), tap=tap)
    stt = OracleSTT([lambda: engine_utterances(engine.log), agent.utterances])
    journal = Journal(f"sim-{cfg.company_id}-{cfg.seed}", cfg.out_dir / "journal.jsonl", clock=lambda: holder["orch"].t)
    orch = CallOrchestrator(plan, cfg.profile, cfg.user_first_name, line, stt, SyntheticVoice(f0=245), journal)
    holder["orch"] = orch

    user = SimUser(orch, cfg.user_join_delay_s, cfg.talk_s)
    orch.on_frame = lambda t, frame: user.step(t)
    result = await orch.run()
    rec.close()
    journal.close()
    return SimResult(result, list(journal.events), engine, agent, line.line_time())
