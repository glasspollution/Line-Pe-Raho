"""Runs calls for the API (CLAUDE.md §14): builds the pipeline per call and drives it in the background.

mode "sim"  : synthetic voices + exact transcription (no credits); real-time pace so a UI can follow it
              (speed="fast" uses a FakeClock, for tests).
mode "live" : Timbre voices (pre-synthesised before dialling), Prisma realtime STT, Prisma REST for the
              user track and the IVR voice prompts, and the LLM (plan was made earlier) for menus + summary.
Company side (scenario.agent_mode): "auto" (AutoAgentLeg), "silent" (auto-agent that waits to be spoken
to), "console" (a teammate on /company; always uses Prisma STT since a human's words can't be faked).
User side: the browser via /ws/user-audio (Bridge registered in `bridges`), or scenario.auto_user for a
simulated user. Note: harness.py and live.py wire the same pieces for tests/spikes (refactor later).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

import soundfile as sf
from sqlalchemy.engine import Engine

from app.agent import phrases
from app.agent.bridge import Bridge
from app.agent.orchestrator import CallOrchestrator
from app.audio.pstn_sim import PSTNConfig, PSTNSimulator
from app.audio.recorder import CallRecorder
from app.company.auto_agent import AutoAgentLeg
from app.company.console import ConsoleAgentLeg, ConsoleRegistry
from app.company.engine import IVREngine, Scenario, load_tree
from app.company.voice import PromptAudio, SyntheticVoice
from app.config import Settings
from app.gnani.errors import GLOBAL_METER, UsageMeter
from app.gnani.stt_realtime import PrismaRealtime
from app.gnani.stt_rest import PrismaREST
from app.gnani.tts import CachedVoice, PhraseCache, TimbreTTS
from app.journal import Journal
from app.line.clock import Clock, FakeClock, RealClock
from app.line.virtual import VirtualLine
from app.llm.schemas import CallPlan
from app.models import Call, session
from app.postcall.pipeline import PostCallInputs, summarize
from app.sim.harness import USER_LINES, SimUser
from app.sim.live import tree_texts
from app.sim.oracle_stt import OracleSTT, engine_utterances

log = logging.getLogger(__name__)
Mode = Literal["sim", "live"]


@dataclass
class CallScenario:
    """§11.4 simulator settings; applied to the next call started."""

    hold_s: float = 20.0
    noise_profile: str = "snr10"
    noise_type: str = "street"
    packet_loss: float = 0.01
    agent_mode: Literal["auto", "silent", "console"] = "auto"
    auto_user: bool = False
    user_join_delay_s: float = 3.0


@dataclass
class CallRuntime:
    call_id: str
    task_id: str
    mode: Mode
    out_dir: Path
    journal: Journal
    orch: CallOrchestrator
    bridge: Bridge
    engine: IVREngine
    company: Any
    meter: UsageMeter
    scenario: CallScenario
    task: asyncio.Task[None] | None = None
    error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def state(self) -> str:
        return self.orch.state


class CallManager:
    def __init__(self, settings: Settings, db: Engine, bridges: dict[str, Bridge], consoles: ConsoleRegistry,
                 llm_factory: Callable[[UsageMeter], Any] | None = None) -> None:
        self.s, self.db, self.bridges, self.consoles = settings, db, bridges, consoles
        self.llm_factory = llm_factory
        self.calls: dict[str, CallRuntime] = {}
        self.scenario = CallScenario()

    def get(self, call_id: str) -> CallRuntime | None:
        return self.calls.get(call_id)

    async def start_call(self, task_id: str, plan: CallPlan, profile: dict[str, str], user_first_name: str,
                         mode: Mode = "sim", speed: Literal["realtime", "fast"] = "realtime") -> CallRuntime:
        sc = CallScenario(**asdict(self.scenario))
        call_id = uuid.uuid4().hex[:12]
        out_dir = self.s.data_dir / "calls" / call_id
        tree = load_tree(plan.company_id)
        meter = GLOBAL_METER.child()
        live = mode == "live"
        real_speech = live or sc.agent_mode == "console"
        holder: dict[str, Any] = {}

        tts = TimbreTTS(self.s, meter=meter) if live else None
        cache = PhraseCache(tts) if tts else None
        ivr_voice: PromptAudio
        agent_voice: PromptAudio
        company_voice: PromptAudio
        user_voice: PromptAudio
        if cache:
            ivr_voice = CachedVoice(cache, tree["voice"].get("name", self.s.tts_ivr_voice),
                                    tree["voice"].get("language", "hi-en"))
            agent_voice = CachedVoice(cache, self.s.tts_agent_voice, plan.user_language)
            company_voice = CachedVoice(cache, self.s.tts_company_agent_voice, "en-IN")
            user_voice = CachedVoice(cache, self.s.tts_sim_user_voice, "en-IN")
        else:
            ivr_voice, agent_voice = SyntheticVoice(f0=215), SyntheticVoice(f0=245)
            company_voice, user_voice = SyntheticVoice(f0=175), SyntheticVoice(f0=135)
        rest = PrismaREST(self.s, meter=meter) if real_speech else None
        llm = self.llm_factory(meter) if (live and self.llm_factory) else None

        async def hear(pcm: bytes, t0: float, t1: float) -> str:
            if rest is not None:
                return (await rest.transcribe(pcm[-55 * 16000:], language="en-IN")).text
            said = [u for src in (holder["orch"].spoken, holder["user"].spoken if "user" in holder else [])
                    for u in src if u.end > t0 - 0.3 and u.start < t1 + 0.3]
            text = " ".join(u.text for u in said)
            if text or "user" in holder:
                return text
            # a real browser user in sim mode can't be transcribed for free: count it as a turn only if the
            # bridge actually received voiced audio from them since the last turn (line noise never counts)
            voiced, seen = bridge.voiced_frames, holder.get("voiced_seen", 0)
            holder["voiced_seen"] = voiced
            return "(the caller spoke)" if voiced - seen >= 15 else ""

        company: Any
        if sc.agent_mode == "console":
            company = self.consoles.create(call_id)
        else:
            company = AutoAgentLeg(plan.company_id, tree["display_name"], company_voice, hear=hear,
                                   greets_first=sc.agent_mode == "auto", seed=int(call_id[:6], 16) % 1000,
                                   today=dt.date.today())

        async def recognizer(pcm: bytes) -> str:
            if rest is not None:
                return (await rest.transcribe(pcm, language="en-IN")).text
            replies = [e["text"] for e in holder["orch"].j.events if e["type"] == "decision" and e.get("kind") == "voice_reply"]
            return replies[-1] if replies else ""

        engine = IVREngine(tree, ivr_voice, agent_leg=company, recognizer=recognizer,
                           scenario=Scenario(hold_s=sc.hold_s, answer_after_s=2.0))
        rec = CallRecorder(out_dir / "recordings")
        up = PSTNSimulator(PSTNConfig(profile=sc.noise_profile, noise_type=sc.noise_type, packet_loss=sc.packet_loss))
        down = PSTNSimulator(PSTNConfig(profile=sc.noise_profile, noise_type=sc.noise_type, packet_loss=sc.packet_loss,
                                        seed=1))

        def tap(t: float, sent: bytes, heard: bytes) -> None:
            bridged = "orch" in holder and holder["orch"].state == "BRIDGED"
            rec.write("user" if bridged else "caller", sent, t)
            rec.write("callee", heard, t)

        clock: Clock = FakeClock() if speed == "fast" else RealClock()
        line = VirtualLine(lambda _: engine, uplink=up, downlink=down, clock=clock, tap=tap)
        stt: Any
        if real_speech:
            brand = [w for w in tree["display_name"].split() if w.isalpha()]
            stt = PrismaRealtime(self.s, meter=meter, lang="en-IN", bias=brand + plan.goal_keywords)
        else:
            utt: Callable[[], list[Any]] = getattr(company, "utterances", lambda: [])
            stt = OracleSTT([lambda: engine_utterances(engine.log), utt])
        journal = Journal.for_call(self.s.data_dir, call_id, clock=lambda: holder["orch"].t if "orch" in holder else 0.0)
        bridge = Bridge()

        async def summarizer(o: CallOrchestrator) -> dict[str, Any]:
            rec.close()
            user_pcm = None
            if rest is not None and "user" not in holder:
                pcm, _ = sf.read(out_dir / "recordings" / "user.wav", dtype="int16")
                user_pcm = pcm.astype("<i2").tobytes()
            inp = PostCallInputs(call_id=call_id, call_date=dt.date.today(), events=list(journal.events),
                                 agent_spoken=list(o.spoken),
                                 user_spoken=list(holder["user"].spoken) if "user" in holder else None,
                                 user_pcm=user_pcm, out_dir=out_dir, usage=meter.snapshot(),
                                 ticket_prefix=tree.get("ticket_prefix"))
            return await summarize(inp, llm=llm, rest=rest)

        orch = CallOrchestrator(plan, profile, user_first_name, line, stt, agent_voice, journal, settings=self.s,
                                bridge=bridge, summarizer=summarizer, llm=llm)
        bridge.on_connect, bridge.on_hangup = orch.user_join, orch.end_call
        holder["orch"] = orch
        self.bridges[call_id] = bridge
        if sc.auto_user:
            user = SimUser(orch, bridge, user_voice, USER_LINES[plan.company_id], sc.user_join_delay_s, 120.0)
            holder["user"] = user
            orch.on_frame = lambda t, frame: user.step(t)

        rt = CallRuntime(call_id, task_id, mode, out_dir, journal, orch, bridge, engine, company, meter, sc)
        with session(self.db) as db:
            db.add(Call(id=call_id, task_id=task_id, mode=mode, noise_profile=sc.noise_profile,
                        recording_dir=str(out_dir / "recordings")))
            db.commit()
        self.calls[call_id] = rt

        async def warm() -> None:
            if not cache:
                return
            texts = {ivr_voice: tree_texts(tree),
                     agent_voice: (phrases.per_user_phrases(plan.user_language, user_first_name)
                                   + phrases.fixed_phrases(plan.user_language) + list(plan.spoken_intent.values())),
                     company_voice: company.lines() if isinstance(company, AutoAgentLeg) else [],
                     user_voice: USER_LINES[plan.company_id] if sc.auto_user else []}
            for v, ts in texts.items():
                assert isinstance(v, CachedVoice)
                await v.prewarm(ts)

        async def run() -> None:
            try:
                await warm()
                if isinstance(stt, PrismaRealtime):
                    await stt.start()
                await orch.run()
            except Exception as e:  # noqa: BLE001 - recorded on the call; the API reports it
                log.exception("call %s crashed", call_id)
                rt.error = f"{type(e).__name__}: {e}"
                if orch.state not in ("DONE", "FAILED"):
                    journal.emit("error", message=rt.error)
                    transition: dict[str, Any] = {"from": orch.state, "to": "FAILED"}
                    journal.emit("state_changed", **transition)
                    orch.state = "FAILED"
            finally:
                if isinstance(stt, PrismaRealtime):
                    await stt.close()
                for client in (rest, tts):
                    if client is not None:
                        await client.aclose()
                rec.close()
                journal.close()
                self.bridges.pop(call_id, None)
                if isinstance(company, ConsoleAgentLeg):
                    self.consoles.remove(call_id)
                self._finish_row(rt)

        rt.task = asyncio.get_running_loop().create_task(run(), name=f"call-{call_id}")
        return rt

    def _finish_row(self, rt: CallRuntime) -> None:
        ev = rt.journal.events
        ended = next((e for e in ev if e["type"] == "call_ended"), None)
        with session(self.db) as db:
            row = db.get(Call, rt.call_id)
            if row is None:
                return
            row.ended_at = dt.datetime.now(dt.UTC)
            row.final_state = rt.orch.state
            row.time_to_human_s = rt.orch.result.time_to_detect_s
            if ended:
                row.hold_seconds = ended.get("hold_seconds")
                row.ivr_seconds = ended.get("ivr_seconds")
            db.add(row)
            db.commit()

    async def hangup(self, call_id: str) -> bool:
        rt = self.calls.get(call_id)
        if rt is None:
            return False
        if rt.orch.state == "BRIDGED":
            rt.orch.end_call()
        elif rt.orch.state not in ("DONE", "FAILED", "ENDED", "SUMMARIZING"):
            await rt.orch.line.hangup()  # the inbound loop ends; the orchestrator records FAILED/ENDED
        return True

    def summary(self, call_id: str) -> dict[str, Any] | None:
        p = self.s.data_dir / "calls" / call_id / "summary.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
