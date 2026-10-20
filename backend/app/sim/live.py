"""Live run: the full product loop with real Gnani speech and the LLM (spends credits; run manually).

Same wiring as app/sim/harness.py with the fakes swapped out:
  mock IVR voice   -> Timbre (tts_ivr_voice, tree language), pre-synthesised via PhraseCache
  our agent voice  -> Timbre (tts_agent_voice, plan language), pre-synthesised (disclosure, probe, ...)
  human agent      -> Timbre (a third voice), scripted greeting/replies
  hearing          -> Prisma realtime STT on the callee track, through the PSTN simulator
  clock            -> RealClock (Prisma must receive audio at real-time cadence)
  voice prompts    -> the IVR transcribes the caller with Prisma REST
"""

from __future__ import annotations

import datetime as dt
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jiwer
import soundfile as sf

from app.agent import phrases
from app.agent.bridge import Bridge
from app.agent.menu_parser import normalize
from app.agent.orchestrator import CallOrchestrator
from app.agent.planner import build_plan
from app.audio.pstn_sim import PSTNConfig, PSTNSimulator
from app.audio.recorder import CallRecorder
from app.company.auto_agent import AutoAgentLeg
from app.company.engine import DEFAULT_MESSAGES, IVREngine, Scenario, load_tree
from app.config import get_settings
from app.gnani.errors import GLOBAL_METER
from app.gnani.stt_realtime import PrismaRealtime
from app.gnani.stt_rest import PrismaREST
from app.gnani.tts import CachedVoice, PhraseCache, TimbreTTS
from app.journal import Journal
from app.line.clock import RealClock
from app.line.virtual import VirtualLine
from app.llm.client import LLMClient
from app.postcall.pipeline import PostCallInputs, summarize
from app.sim.harness import DEMO_PLANS, DEMO_PROFILE, USER_LINES, SimUser
from app.sim.oracle_stt import engine_utterances


@dataclass
class LiveConfig:
    company_id: str = "nimbus_telecom"
    text: str | None = None  # the user's problem; None = the company's demo plan
    out_dir: Path = Path("data/live/run")
    noise_profile: str = "snr10"
    hold_s: float = 15.0
    user_first_name: str = "Aarav"
    user_join_delay_s: float | None = 3.0
    max_talk_s: float = 120.0
    greets_first: bool = True
    use_llm: bool = True  # Kimi for the plan, unfamiliar menus and the summary
    dry_run: bool = False  # only report what would be synthesised


@dataclass
class LiveReport:
    final_state: str = ""
    plan_method: str = ""
    plan: dict[str, Any] = field(default_factory=dict)
    path: list[str] = field(default_factory=list)
    time_to_detect_s: float | None = None
    probe_used: bool = False
    call_seconds: float = 0.0
    wall_seconds: float = 0.0
    tts_new_phrases: int = 0
    tts_new_chars: int = 0
    usage: dict[str, Any] = field(default_factory=dict)
    prompt_wer: float | None = None
    truth: dict[str, Any] = field(default_factory=dict)
    summary_ok: dict[str, bool] = field(default_factory=dict)
    summary_fields: dict[str, Any] = field(default_factory=dict)
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
    rep = LiveReport()
    cfg.out_dir.mkdir(parents=True, exist_ok=True)
    meter = GLOBAL_METER.child()
    llm = LLMClient(s, meter=meter) if cfg.use_llm and not cfg.dry_run else None
    call_date = dt.date.today()

    if cfg.text:
        plan, rep.plan_method = await build_plan(cfg.text, cfg.company_id, DEMO_PROFILE, llm)
    else:
        plan, rep.plan_method = DEMO_PLANS[cfg.company_id], "demo"
    rep.plan = plan.model_dump()

    tts = TimbreTTS(s, meter=meter)
    cache = PhraseCache(tts)
    ivr_voice = CachedVoice(cache, tree["voice"].get("name", s.tts_ivr_voice), tree["voice"].get("language", "hi-en"))
    agent_voice = CachedVoice(cache, s.tts_agent_voice, plan.user_language)
    company_voice = CachedVoice(cache, s.tts_company_agent_voice, "en-IN")
    user_voice = CachedVoice(cache, s.tts_sim_user_voice, "en-IN")
    rest = PrismaREST(s, meter=meter)

    async def hear(pcm: bytes, t0: float, t1: float) -> str:  # the company agent hears the caller via Prisma
        return (await rest.transcribe(pcm[-55 * 16000:], language="en-IN")).text

    company = AutoAgentLeg(cfg.company_id, tree["display_name"], company_voice, hear=hear,
                           greets_first=cfg.greets_first, seed=int(time.time()) % 1000, today=call_date)
    user_lines = USER_LINES[cfg.company_id]
    agent_texts = (phrases.per_user_phrases(plan.user_language, cfg.user_first_name)
                   + phrases.fixed_phrases(plan.user_language) + list(plan.spoken_intent.values()))
    plan_sets = [(ivr_voice, tree_texts(tree)), (agent_voice, agent_texts),
                 (company_voice, company.lines()), (user_voice, user_lines)]
    for voice, texts in plan_sets:
        missing = [t for t in texts if cache.get_cached(t, voice.voice, voice.language) is None]
        rep.tts_new_phrases += len(missing)
        rep.tts_new_chars += sum(len(t) for t in missing)
    if cfg.dry_run:
        await tts.aclose()
        await rest.aclose()
        return rep
    for voice, texts in plan_sets:
        await voice.prewarm(texts)

    async def recognizer(pcm: bytes) -> str:
        return (await rest.transcribe(pcm, language="en-IN")).text

    engine = IVREngine(tree, ivr_voice, agent_leg=company, recognizer=recognizer,
                       scenario=Scenario(hold_s=cfg.hold_s, answer_after_s=2.0))
    rec = CallRecorder(cfg.out_dir / "recordings")
    up = PSTNSimulator(PSTNConfig(profile=cfg.noise_profile, packet_loss=0.01, seed=11))
    down = PSTNSimulator(PSTNConfig(profile=cfg.noise_profile, packet_loss=0.01, seed=12))
    holder: dict[str, Any] = {}

    def tap(t: float, sent: bytes, heard: bytes) -> None:
        bridged = "o" in holder and holder["o"].state == "BRIDGED"
        rec.write("user" if bridged else "caller", sent, t)
        rec.write("callee", heard, t)

    line = VirtualLine(lambda _: engine, uplink=up, downlink=down, clock=RealClock(), tap=tap)
    brand_words = [w for w in tree["display_name"].split() if w.isalpha()]
    stt = PrismaRealtime(s, meter=meter, lang="en-IN", bias=brand_words + plan.goal_keywords)
    journal = Journal(f"live-{cfg.company_id}-{int(time.time())}", cfg.out_dir / "journal.jsonl",
                      clock=lambda: holder["o"].t if "o" in holder else 0.0)

    async def summarizer(o: CallOrchestrator) -> dict[str, Any]:
        rec.close()  # flush user.wav before transcribing it
        user_pcm, _ = sf.read(cfg.out_dir / "recordings" / "user.wav", dtype="int16")
        inp = PostCallInputs(call_id=journal.call_id, call_date=call_date, events=list(journal.events),
                             agent_spoken=list(o.spoken), user_pcm=user_pcm.astype("<i2").tobytes(),
                             out_dir=cfg.out_dir, usage=meter.snapshot(), ticket_prefix=tree.get("ticket_prefix"))
        return await summarize(inp, llm=llm, rest=rest)

    bridge = Bridge()
    orch = CallOrchestrator(plan, DEMO_PROFILE, cfg.user_first_name, line, stt, agent_voice, journal, settings=s,
                            bridge=bridge, summarizer=summarizer, llm=llm)
    bridge.on_connect, bridge.on_hangup = orch.user_join, orch.end_call
    holder["o"] = orch
    user = SimUser(orch, bridge, user_voice, user_lines, cfg.user_join_delay_s, cfg.max_talk_s)
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
    rep.stt_errors = stt.errors
    rep.transcripts = [{"t": e["t"], "start": e["start"], "end": e["end"], "text": e["text"]}
                       for e in journal.events if e["type"] == "transcript"]
    rep.truth = {"ticket_id": company.truth.ticket_id, "promised_by": company.truth.promised_by,
                 "agent_name": company.truth.agent_name, "stages": company.truth.stages}
    if orch.summary:
        f = orch.summary["fields"]
        rep.summary_fields = {k: {"value": v["value"], "verified": v["verified"]} for k, v in f.items()}
        for key in ("ticket_id", "promised_by", "agent_name"):
            rep.summary_ok[key] = bool(f[key]["verified"] and str(f[key]["value"]) == str(rep.truth[key]))
    agent_at = next((e["t"] for e in engine.log if e["kind"] == "agent_connect"), None)
    ref = " ".join(normalize(u.text) for u in engine_utterances(engine.log) if agent_at is None or u.start < agent_at)
    hyp = " ".join(normalize(tr["text"]) for tr in rep.transcripts if agent_at is None or tr["start"] < agent_at)
    if ref.strip():
        rep.prompt_wer = round(float(jiwer.wer(ref, hyp or "-")), 3)
    (cfg.out_dir / "report.json").write_text(json.dumps(rep.__dict__, indent=2, ensure_ascii=False), encoding="utf-8")
    return rep
