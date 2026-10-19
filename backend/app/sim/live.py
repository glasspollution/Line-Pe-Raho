"""Live run: the simulated call, but with real Gnani speech (spends credits; run manually).

Same wiring as app/sim/harness.py with the fakes swapped out:
  mock IVR voice   -> Timbre (tts_ivr_voice, tree language), pre-synthesised via PhraseCache
  our agent voice  -> Timbre (tts_agent_voice, plan language), pre-synthesised (disclosure, probe, ...)
  human agent      -> Timbre (a third voice), scripted greeting/replies
  hearing          -> Prisma realtime STT on the callee track, through the PSTN simulator
  clock            -> RealClock (Prisma must receive audio at real-time cadence)
  voice prompts    -> the IVR transcribes the caller with Prisma REST
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jiwer

from app.agent import phrases
from app.agent.menu_parser import normalize
from app.agent.orchestrator import CallOrchestrator
from app.audio.pstn_sim import PSTNConfig, PSTNSimulator
from app.audio.recorder import CallRecorder
from app.company.engine import DEFAULT_MESSAGES, IVREngine, Scenario, load_tree
from app.config import get_settings
from app.gnani.errors import GLOBAL_METER
from app.gnani.stt_realtime import PrismaRealtime
from app.gnani.stt_rest import PrismaREST
from app.gnani.tts import CachedVoice, PhraseCache, TimbreTTS
from app.journal import Journal
from app.line.clock import RealClock
from app.line.virtual import VirtualLine
from app.sim.harness import DEMO_PLANS, DEMO_PROFILE, SimUser
from app.sim.oracle_stt import engine_utterances
from app.sim.scripted_agent import ScriptedAgentLeg

HUMAN_VOICE = "Avinash"


@dataclass
class LiveConfig:
    company_id: str = "nimbus_telecom"
    out_dir: Path = Path("data/live/run")
    noise_profile: str = "snr10"
    hold_s: float = 15.0
    user_first_name: str = "Aarav"
    user_join_delay_s: float | None = 3.0
    talk_s: float = 4.0
    greets_first: bool = True
    dry_run: bool = False  # only report what would be synthesised


@dataclass
class LiveReport:
    final_state: str = ""
    path: list[str] = field(default_factory=list)
    time_to_detect_s: float | None = None
    probe_used: bool = False
    call_seconds: float = 0.0
    wall_seconds: float = 0.0
    tts_new_phrases: int = 0
    tts_new_chars: int = 0
    usage: dict[str, Any] = field(default_factory=dict)
    prompt_wer: float | None = None
    stt_server_config: dict[str, Any] = field(default_factory=dict)
    stt_errors: list[dict[str, Any]] = field(default_factory=list)
    transcripts: list[dict[str, Any]] = field(default_factory=list)


def tree_texts(tree: dict[str, Any]) -> list[str]:
    out = list(DEFAULT_MESSAGES.values()) + list((tree.get("messages") or {}).values())
    for node in tree["nodes"].values():
        out += [node.get("say", ""), (node.get("collect") or {}).get("prompt", ""),
                (node.get("ask") or {}).get("prompt", "")]
        out += list((node.get("hold") or {}).get("announcements", []))
    return [t for t in dict.fromkeys(x.strip() for x in out) if t]


async def run_live_call(cfg: LiveConfig) -> LiveReport:
    s = get_settings()
    tree = load_tree(cfg.company_id)
    plan = DEMO_PLANS[cfg.company_id]
    rep = LiveReport()
    cfg.out_dir.mkdir(parents=True, exist_ok=True)
    meter = GLOBAL_METER.child()

    tts = TimbreTTS(s, meter=meter)
    cache = PhraseCache(tts)
    ivr_voice = CachedVoice(cache, tree["voice"].get("name", s.tts_ivr_voice), tree["voice"].get("language", "hi-en"))
    agent_voice = CachedVoice(cache, s.tts_agent_voice, plan.user_language)
    human_voice = CachedVoice(cache, HUMAN_VOICE, "en-IN")
    human = ScriptedAgentLeg(human_voice, greets_first=cfg.greets_first,
                             greeting=f"Hello, this is Rohan from {tree['display_name']}. How may I help you?",
                             probe_reply="Haan ji, boliye, main sun raha hoon.", ack="Ji theek hai, main line par hoon.")
    agent_texts = (phrases.per_user_phrases(plan.user_language, cfg.user_first_name)
                   + phrases.fixed_phrases(plan.user_language) + list(plan.spoken_intent.values()))
    plan_sets = [(ivr_voice, tree_texts(tree)), (agent_voice, agent_texts),
                 (human_voice, [human.greeting, human.probe_reply, human.ack])]
    for voice, texts in plan_sets:
        missing = [t for t in texts if cache.get_cached(t, voice.voice, voice.language) is None]
        rep.tts_new_phrases += len(missing)
        rep.tts_new_chars += sum(len(t) for t in missing)
    if cfg.dry_run:
        await tts.aclose()
        return rep
    for voice, texts in plan_sets:
        await voice.prewarm(texts)

    rest = PrismaREST(s, meter=meter)

    async def recognizer(pcm: bytes) -> str:
        return (await rest.transcribe(pcm, language="en-IN")).text

    engine = IVREngine(tree, ivr_voice, agent_leg=human, recognizer=recognizer,
                       scenario=Scenario(hold_s=cfg.hold_s, answer_after_s=2.0))
    rec = CallRecorder(cfg.out_dir / "recordings")
    up = PSTNSimulator(PSTNConfig(profile=cfg.noise_profile, packet_loss=0.01, seed=11))
    down = PSTNSimulator(PSTNConfig(profile=cfg.noise_profile, packet_loss=0.01, seed=12))

    def tap(t: float, sent: bytes, heard: bytes) -> None:
        rec.write("caller", sent, t)
        rec.write("callee", heard, t)

    line = VirtualLine(lambda _: engine, uplink=up, downlink=down, clock=RealClock(), tap=tap)
    brand_words = [w for w in tree["display_name"].split() if w.isalpha()]
    stt = PrismaRealtime(s, meter=meter, lang="en-IN", bias=brand_words + plan.goal_keywords)
    holder: dict[str, CallOrchestrator] = {}
    journal = Journal(f"live-{cfg.company_id}-{int(time.time())}", cfg.out_dir / "journal.jsonl",
                      clock=lambda: holder["o"].t if "o" in holder else 0.0)
    orch = CallOrchestrator(plan, DEMO_PROFILE, cfg.user_first_name, line, stt, agent_voice, journal, settings=s)
    holder["o"] = orch
    user = SimUser(orch, cfg.user_join_delay_s, cfg.talk_s)
    orch.on_frame = lambda t, frame: user.step(t)

    wall0 = time.monotonic()
    await stt.start()
    try:
        result = await orch.run()
    finally:
        await stt.close()
        await rest.aclose()
        await tts.aclose()
        rec.close()
        journal.close()

    rep.final_state = result.final_state
    rep.path = result.path
    rep.time_to_detect_s = result.time_to_detect_s
    rep.probe_used = result.probe_used
    rep.call_seconds = round(orch.t, 2)
    rep.wall_seconds = round(time.monotonic() - wall0, 2)
    rep.usage = meter.snapshot()
    rep.stt_server_config = stt.server_configs[0] if stt.server_configs else {}
    rep.stt_errors = stt.errors
    rep.transcripts = [{"t": e["t"], "start": e["start"], "end": e["end"], "text": e["text"]}
                       for e in journal.events if e["type"] == "transcript"]
    # prompt WER: everything the IVR said before the human picked up vs what Prisma heard in that span
    agent_at = next((e["t"] for e in engine.log if e["kind"] == "agent_connect"), None)
    ref = " ".join(normalize(u.text) for u in engine_utterances(engine.log) if agent_at is None or u.start < agent_at)
    hyp = " ".join(normalize(tr["text"]) for tr in rep.transcripts if agent_at is None or tr["start"] < agent_at)
    if ref.strip():
        rep.prompt_wer = round(float(jiwer.wer(ref, hyp or "-")), 3)
    (cfg.out_dir / "report.json").write_text(json.dumps(rep.__dict__, indent=2, ensure_ascii=False), encoding="utf-8")
    return rep
