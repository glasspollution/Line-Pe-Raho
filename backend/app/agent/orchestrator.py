"""Call orchestrator state machine (CLAUDE.md §8).

Tick-driven: every 20 ms inbound (callee) frame feeds the classifier and the STT stream, then one step of
the state machine runs at that frame's call time `t`. Outbound audio (DTMF, probe, disclosure, keep-alives)
is queued on the line without blocking; `_busy_until` tracks when it finishes playing.
Every transition and decision is journaled (§14.2) and must satisfy eval/journal_check (J1–J7).

Interfaces it needs (fakes in app/sim for offline runs):
  line   : VirtualLine-like (dial, send_audio, send_dtmf, inbound, hangup, state, line_time)
  stt    : feed(frame, t) -> list[Transcript]
  voice  : render(text) -> 8 kHz PCM for the agent's own speech (PhraseCache in production)
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from app.agent import phrases
from app.agent.bridge import Bridge
from app.agent.human_detector import GREETING_RE, HumanDetector, SegmentEvidence, is_connecting_prompt
from app.agent.ivr_decider import RISKY_RE, IVRDecider
from app.agent.menu_parser import MenuBuffer, SegmentParse, parse_segment
from app.audio.classifier import AudioClassifier
from app.audio.frames import FRAME_BYTES
from app.config import Settings, get_settings
from app.journal import Journal, mask_digits
from app.line.virtual import VirtualLine
from app.llm.schemas import CallPlan, IVRDecisionOut
from app.sim.oracle_stt import Transcript, Utterance

TICK = 0.02
PROMPTS = Path(__file__).resolve().parents[1] / "llm" / "prompts"


class STTStream(Protocol):
    def feed(self, frame: bytes, t: float) -> list[Transcript]: ...


class AgentVoice(Protocol):
    def render(self, text: str) -> bytes: ...


@dataclass
class _Suspect:
    seg: Transcript
    first_score: float
    evidence: SegmentEvidence
    decide_at: float  # when the turn-taking window has elapsed


@dataclass
class CallResult:
    final_state: str
    time_to_detect_s: float | None = None
    probe_used: bool = False
    path: list[str] = field(default_factory=list)
    false_alarms: int = 0


class CallOrchestrator:
    def __init__(
        self,
        plan: CallPlan,
        profile: dict[str, str],
        user_first_name: str,
        line: VirtualLine,
        stt: STTStream,
        voice: AgentVoice,
        journal: Journal,
        decider: IVRDecider | None = None,
        detector: HumanDetector | None = None,
        classifier: AudioClassifier | None = None,
        settings: Settings | None = None,
        on_frame: Callable[[float, bytes], None] | None = None,
        bridge: Bridge | None = None,
        summarizer: Callable[[CallOrchestrator], Awaitable[dict[str, Any]]] | None = None,
        llm: Any = None,  # LLMClient-like (complete_json); used when IVR rules can't decide (§9.3)
    ) -> None:
        self.s = settings or get_settings()
        self.plan, self.profile, self.name = plan, profile, user_first_name
        self.lang = plan.user_language
        self.line, self.stt, self.voice, self.j = line, stt, voice, journal
        self.decider = decider or IVRDecider(settings=self.s)
        self.detector = detector or HumanDetector(settings=self.s)
        self.clf = classifier or AudioClassifier(self.s)
        self.on_frame = on_frame
        self.bridge = bridge
        self.summarizer = summarizer
        self.summary: dict[str, Any] | None = None
        self.llm = llm
        self._llm_task: asyncio.Task[Any] | None = None
        self._llm_meta: dict[str, Any] = {}
        self._menu_version = 0
        self.spoken: list[Utterance] = []  # our agent's own speech with line times (post-call timeline)
        self.state = "IDLE"
        self.t = 0.0
        self.result = CallResult("IDLE")
        self._busy_until = 0.0
        self._menu = MenuBuffer(self.s.menu_complete_silence_s)
        self._menu_seen: dict[tuple[str, ...], int] = {}
        self._fallback_idx = 0
        self._ivr_started = 0.0
        self._hold_started = 0.0
        self._announcements = 0
        self._connecting_seen = False
        self._suspect: _Suspect | None = None
        self._probe_end: float | None = None
        self._probe_return = "HOLD"
        self._waiting_since = 0.0
        self._keepalives = 0
        self._last_keepalive = 0.0
        self._user_joined = False
        self._end_requested = False
        self._last_callee_speech_t = -1.0
        self._music_last_t: float | None = None
        self._dial_started = 0.0

    # ---------- public controls ----------
    def user_join(self) -> None:
        self._user_joined = True

    def end_call(self) -> None:
        self._end_requested = True

    # ---------- helpers ----------
    def _goto(self, to: str, **data: Any) -> None:
        self.j.emit("state_changed", t=self.t, **{"from": self.state, "to": to}, **data)
        self.state = to

    async def _speak(self, text: str) -> float:
        pcm = self.voice.render(text)
        pcm += b"\x00" * ((-len(pcm)) % FRAME_BYTES)
        for i in range(0, len(pcm), FRAME_BYTES):
            await self.line.send_audio(pcm[i : i + FRAME_BYTES])
        start = max(self._busy_until, self.line.line_time())
        self._busy_until = start + len(pcm) / 16000
        self.spoken.append(Utterance(text, round(start, 3), round(self._busy_until, 3)))
        return self._busy_until

    async def _press(self, digits: str) -> None:
        await self.line.send_dtmf(digits)
        n_frames = len(digits) * int((self.s.dtmf_tone_ms + self.s.dtmf_gap_ms) / 20)
        start = max(self._busy_until, self.line.line_time())
        self._busy_until = start + n_frames * TICK
        self.j.emit("dtmf_sent", t=self.t, digits_masked=mask_digits(digits), n_digits=len(digits))

    def _fail(self, message: str) -> None:
        self.j.emit("error", t=self.t, message=message)
        self._goto("FAILED")

    # ---------- main loop ----------
    async def run(self) -> CallResult:
        self._goto("PLANNING")
        self._goto("DIALING", company_id=self.plan.company_id)
        await self.line.dial(self.plan.company_id)
        n = 0
        async for frame in self.line.inbound():
            self.t = n * TICK
            n += 1
            if self.on_frame:
                self.on_frame(self.t, frame)
            win = self.clf.feed(frame, self.t)
            if win is not None:
                if win.label == "speech":
                    self._last_callee_speech_t = win.t_end
                if win.label == "music":
                    self._music_last_t = win.t_end
            transcripts = self.stt.feed(frame, self.t)
            for tr in transcripts:
                self.j.emit("transcript", t=self.t, track=tr.track, segment_id=tr.segment_id,
                            start=tr.start, end=tr.end, text=tr.text)
                self._last_callee_speech_t = max(self._last_callee_speech_t, tr.end)
            await self._step(transcripts)
            if self.bridge is not None:
                self.bridge.active = self.state == "BRIDGED"
                if self.bridge.active:  # user <-> line; our agent stays muted
                    self.bridge.to_user(frame)
                    for f in self.bridge.take_uplink():
                        await self.line.send_audio(f)
            if self.state in ("ENDED", "FAILED"):
                break
        if self.state not in ("ENDED", "FAILED"):
            # far end hung up
            if self.state == "BRIDGED" or self.state == "WAITING_FOR_USER":
                self.j.emit("call_ended", t=self.t, by="callee")
                self._goto("ENDED")
            else:
                self._fail(f"far end hung up during {self.state}")
        if self._llm_task is not None:
            self._llm_task.cancel()
        await self.line.hangup()
        if self.state == "ENDED":
            self._goto("SUMMARIZING")
            if self.summarizer is None:
                self.j.emit("summary_ready", t=self.t, skipped=True, reason="no post-call pipeline configured")
            else:
                try:
                    self.summary = await self.summarizer(self)
                    self.j.emit("summary_ready", t=self.t, outcome=self.summary["outcome"],
                                verified_count=self.summary["verified_count"],
                                unverified_count=self.summary["unverified_count"], method=self.summary["method"])
                except Exception as e:  # noqa: BLE001 - a failed summary must not lose the call record
                    self.j.emit("error", t=self.t, message=f"post-call pipeline failed: {type(e).__name__}: {e}")
                    self.j.emit("summary_ready", t=self.t, skipped=True, reason="post-call pipeline failed")
            self._goto("DONE")
        self.result.final_state = self.state
        return self.result

    async def _step(self, transcripts: list[Transcript]) -> None:
        st = self.state
        t = self.t
        if t > self.s.max_call_s:
            self._fail("max call duration")
            return
        if st == "DIALING":
            if self.line.state == "connected":
                self._ivr_started = t
                self._goto("IVR")
            elif t > self.s.dial_timeout_s:
                self._fail("no answer")
            return
        if st == "IVR":
            await self._ivr(transcripts)
        elif st == "HOLD":
            await self._hold(transcripts)
        elif st == "HUMAN_SUSPECTED":
            await self._suspected(transcripts)
        elif st == "PROBING":
            await self._probing(transcripts)
        elif st == "HUMAN_CONFIRMED":
            await self._disclose()
        elif st == "DISCLOSING":
            if t >= self._busy_until:
                self._waiting_since = t
                self._last_keepalive = t
                self._goto("WAITING_FOR_USER")
        elif st == "WAITING_FOR_USER":
            await self._waiting()
        elif st == "BRIDGED":
            if self._end_requested:
                self.j.emit("call_ended", t=t, by="user")
                self._goto("ENDED")

    # ---------- IVR ----------
    async def _ivr(self, transcripts: list[Transcript]) -> None:
        t = self.t
        if t - self._ivr_started > self.s.max_ivr_s:
            self._fail("IVR phase too long")
            return
        for tr in transcripts:
            self.detector.remember(tr.text)
            if is_connecting_prompt(tr.text):
                self._connecting_seen = True
            p = parse_segment(tr.text, tr.segment_id)
            if p.options:
                self._menu.add(p, tr.end)
                self._menu_version += 1
                self.j.emit("menu_parsed", t=t, segment_ids=[tr.segment_id],
                            options=[{"digit": o.digit, "label": o.label} for o in p.options])
                continue
            if p.needs_llm and self.llm is not None and self._llm_task is None:
                self._start_llm([], tr.text)  # "press ..." heard but no options parsed (§9.2 LLM fallback)
                continue
            if p.data_prompt is not None:
                await self._data_prompt(p)
                continue
            if p.voice_prompt:
                text = self.plan.spoken_intent.get(self.lang) or self.plan.spoken_intent.get("en-IN") or self.plan.goal
                self.j.emit("decision", t=t, kind="voice_reply", method="rule", text=text, reason="IVR asked for the query")
                await self._speak(text)
                continue
            if not self._menu.options and GREETING_RE.search(tr.text):  # in IVR, only a greeting suggests a human
                await self._consider_human(tr)
                if self.state != "IVR":
                    return
        if await self._poll_llm():
            return
        if self._menu.is_complete(t) and t >= self._busy_until:
            await self._decide_menu()
            return
        if not self._menu.options and self._llm_task is None and self.clf.sustained_music and t >= self._busy_until:
            self._hold_started = t
            self._goto("HOLD")
            self.j.emit("hold_started", t=t, reason="sustained music")

    async def _data_prompt(self, p: SegmentParse) -> None:
        assert p.data_prompt is not None
        fld = p.data_prompt.field
        value = self.profile.get(fld) if fld != "other" else None
        if not value:
            self.j.emit("decision", t=self.t, kind="data_entry", method="rule", field=fld, action="refuse",
                        reason="sensitive or unavailable field; never sent")
            return
        digits = value + (p.data_prompt.terminator or "")
        self.j.emit("decision", t=self.t, kind="data_entry", method="rule", field=fld,
                    reason=f"data prompt: {fld}" + (f", terminator {p.data_prompt.terminator}" if p.data_prompt.terminator else ""))
        await self._press(digits)

    async def _decide_menu(self) -> None:
        opts = self._menu.options
        sig = tuple(sorted(o.digit + ":" + o.label for o in opts))
        self._menu_seen[sig] = self._menu_seen.get(sig, 0) + 1
        d = self.decider.decide(self.plan, opts, self.result.path)
        if (d.method == "fallback" and d.action != "press" and self.llm is not None and self._llm_task is None
                and self._menu_seen[sig] < self.s.same_menu_max_repeats):
            menu_text = " ".join(seg.text for seg in self._menu.segments)
            self._menu.reset()
            self._start_llm(opts, menu_text)  # rules can't decide: ask the LLM without blocking the call
            return
        digit = d.digit if d.action == "press" else None
        reason, method = d.reason, d.method
        if digit is None and self._menu_seen[sig] >= self.s.same_menu_max_repeats:
            if self._fallback_idx >= len(self.s.fallback_keys):
                self._menu.reset()
                self._fail("same menu repeated; fallback keys exhausted")
                return
            digit = self.s.fallback_keys[self._fallback_idx]
            self._fallback_idx += 1
            reason, method = f"same menu heard {self._menu_seen[sig]}x; fallback key", "fallback"
        self._menu.reset()
        if digit is None:
            self.j.emit("decision", t=self.t, kind="menu_press", method=method, action=d.action, reason=reason,
                        confidence=d.confidence, latency_ms=d.latency_ms)
            return
        self.j.emit("decision", t=self.t, kind="menu_press", method=method, digit=digit, reason=reason,
                    confidence=d.confidence, latency_ms=d.latency_ms)
        self.result.path.append(digit)
        if len(self.result.path) > self.s.max_ivr_depth:
            self._fail("IVR deeper than max_ivr_depth")
            return
        await self._press(digit)

    def _start_llm(self, opts: list[Any], menu_text: str) -> None:
        user = json.dumps({"goal": self.plan.goal, "goal_keywords": self.plan.goal_keywords,
                           "menu_heard": menu_text, "options": [{"digit": o.digit, "label": o.label} for o in opts],
                           "keys_pressed_so_far": self.result.path}, ensure_ascii=False)
        system = (PROMPTS / "ivr_decide.md").read_text(encoding="utf-8")
        self._llm_meta = {"version": self._menu_version, "digits": {o.digit for o in opts},
                          "labels": {o.digit: o.label for o in opts}, "t0": time.perf_counter()}
        self._llm_task = asyncio.get_running_loop().create_task(
            self.llm.complete_json(system, user, IVRDecisionOut, realtime=True))

    async def _poll_llm(self) -> bool:
        task = self._llm_task
        if task is None or not task.done():
            return False
        self._llm_task = None
        meta = self._llm_meta
        latency_ms = int((time.perf_counter() - meta["t0"]) * 1000)
        try:
            out: IVRDecisionOut = task.result()
        except Exception as e:  # noqa: BLE001 - LLM failure: let the menu repeat; fallback keys follow
            self.j.emit("decision", t=self.t, kind="menu_press", method="llm", action="error",
                        reason=f"LLM failed: {type(e).__name__}", latency_ms=latency_ms)
            return False
        if meta["version"] != self._menu_version:
            self.j.emit("decision", t=self.t, kind="menu_press", method="llm", action="stale",
                        reason="a newer menu arrived while the LLM was deciding", latency_ms=latency_ms)
            return False
        digit = out.digit if out.action == "press" else None
        valid = digit is not None and (digit in meta["digits"] or (not meta["digits"] and digit in "0123456789*#"))
        risky = digit is not None and bool(RISKY_RE.search(meta["labels"].get(digit, "")))
        if not valid or risky or out.confidence < self.s.decider_llm_min_confidence:
            self.j.emit("decision", t=self.t, kind="menu_press", method="llm",
                        action="repeat" if out.action == "press" else out.action, reason=out.reason,
                        confidence=out.confidence, latency_ms=latency_ms)
            return False
        assert digit is not None
        self.j.emit("decision", t=self.t, kind="menu_press", method="llm", digit=digit, reason=out.reason,
                    confidence=out.confidence, latency_ms=latency_ms)
        self.result.path.append(digit)
        await self._press(digit)
        return True

    # ---------- HOLD and human detection ----------
    def _evidence(self, tr: Transcript, silence_after: float = 0.0) -> SegmentEvidence:
        music_under = self.clf.music_fraction(tr.start, tr.end) >= 0.5
        # "speech with no music before it": the music had stopped before this person started talking
        no_music_before = not music_under and self.clf.music_stopped_before(tr.start)
        return SegmentEvidence(tr.text, music_under=music_under, speech_no_music_before=no_music_before,
                               after_connecting_prompt=self._connecting_seen, silence_after_s=silence_after)

    async def _consider_human(self, tr: Transcript) -> None:
        ev = self._evidence(tr)
        score, why = self.detector.explain(ev)
        best_case = score + (self.s.human_w_turn_taking if ev.silence_after_s < self.s.human_turn_silence_s else 0)
        if best_case < self.s.human_probe_threshold:
            if self.state == "HOLD":
                self._announcements += 1
                self.j.emit("announcement_heard", t=self.t, segment_id=tr.segment_id, count=self._announcements,
                            score=round(score, 2))
            self.detector.remember(tr.text)
            return
        self._probe_return = self.state
        self._goto("HUMAN_SUSPECTED")
        pending = score < self.s.human_confirm_threshold
        self.j.emit("human_suspected", t=self.t, segment_id=tr.segment_id, score=round(score, 2), evidence=why,
                    **({"pending": "turn_taking"} if pending else {}))
        self._suspect = _Suspect(tr, score, ev, decide_at=tr.end + self.s.human_turn_silence_s)
        if not pending:
            self._confirm(tr.end)

    async def _hold(self, transcripts: list[Transcript]) -> None:
        t = self.t
        if t - self._hold_started > self.s.max_hold_min * 60:
            self._fail("hold too long")
            return
        for tr in transcripts:
            p = parse_segment(tr.text, tr.segment_id)
            if p.options:  # a new menu after hold
                self._menu.add(p, tr.end)
                self.j.emit("menu_parsed", t=t, segment_ids=[tr.segment_id],
                            options=[{"digit": o.digit, "label": o.label} for o in p.options])
                self._goto("IVR")
                return
            if is_connecting_prompt(tr.text):
                self._connecting_seen = True
            await self._consider_human(tr)
            if self.state != "HOLD":
                return
        # silent agent: hold music stopped and nothing has been said for a while
        if (self._music_last_t is not None and not self.clf.sustained_music
                and t - max(self._music_last_t, self._last_callee_speech_t) >= self.s.hold_silence_probe_s):
            self._probe_return = "HOLD"
            self._goto("HUMAN_SUSPECTED")
            self.j.emit("human_suspected", t=t, score=self.s.human_probe_threshold,
                        evidence=["hold_music_stopped_then_silence"])
            self._suspect = None
            await self._start_probe()

    async def _suspected(self, transcripts: list[Transcript]) -> None:
        sus = self._suspect
        if sus is None:
            return
        # Where did the speech really end? STT end estimates run early on real audio (live run 2026-10-20),
        # so extend to the end of classifier speech windows that started around the estimated end.
        tail = [w for w in self.clf.history
                if w.label == "speech" and sus.seg.end - 1.0 <= w.t_start < sus.seg.end + self.s.human_end_slack_s]
        speech_end = max([sus.seg.end] + [w.t_end for w in tail])
        cutoff = speech_end + self.s.human_turn_silence_s
        if transcripts and self.t < cutoff:  # they kept talking before the turn-taking window closed
            cutoff = self.t
        if self.t < cutoff:
            return
        # quiet = no NEW speech after the real end, and no music (a waiting human doesn't play music)
        new_speech = any(w.label == "speech" and w.t_start >= speech_end for w in self.clf.history)
        quiet = not new_speech and not transcripts and self.clf.music_fraction(speech_end, self.t) < 0.25
        ev = self._evidence(sus.seg, silence_after=(self.t - speech_end) if quiet else 0.0)
        score, why = self.detector.explain(ev)
        self.j.emit("human_suspected", t=self.t, segment_id=sus.seg.segment_id, score=round(score, 2), evidence=why)
        if score >= self.s.human_confirm_threshold:
            self._confirm(sus.seg.end)
        elif score >= self.s.human_probe_threshold:
            await self._start_probe()
        else:
            self.detector.remember(sus.seg.text)  # rejected before the agent said anything
            self._suspect = None
            self._goto(self._probe_return)

    async def _start_probe(self) -> None:
        text = phrases.probe(self.lang, self.name)
        self._goto("PROBING")
        self.j.emit("probe_sent", t=self.t, text=text, discloses_ai=True)
        self.result.probe_used = True
        self._probe_end = await self._speak(text)

    async def _probing(self, transcripts: list[Transcript]) -> None:
        assert self._probe_end is not None
        t = self.t
        replied = any(tr.start >= self._probe_end - 0.2 for tr in transcripts) or (
            self._last_callee_speech_t > self._probe_end + 0.3)
        if replied:
            self._confirm(self._suspect.seg.end if self._suspect else self._probe_end)
            return
        if t > self._probe_end + self.s.probe_response_window_s:
            self.result.false_alarms += 1
            self._suspect = None
            self._goto(self._probe_return)

    def _confirm(self, reference_t: float) -> None:
        self.result.time_to_detect_s = round(self.t - reference_t, 3)
        self._goto("HUMAN_CONFIRMED")

    async def _disclose(self) -> None:
        text = phrases.disclosure(self.lang, self.name)
        self._goto("DISCLOSING")
        self.j.emit("decision", t=self.t, kind="disclosure", method="rule", language=self.lang, text=text,
                    cached=True, latency_ms=0)
        self.j.emit("human_detected", t=self.t, time_to_detect_s=self.result.time_to_detect_s,
                    measured_from="greeting_end" if self._suspect else "probe_end")
        await self._speak(text)

    async def _waiting(self) -> None:
        t = self.t
        if self._user_joined:
            self.j.emit("user_joined", t=t)
            self._goto("BRIDGED")
            return
        if t - self._waiting_since >= self.s.user_join_timeout_s:
            if t >= self._busy_until:
                text = phrases.no_join_fallback(self.lang)
                self.j.emit("decision", t=t, kind="voice_reply", method="rule", text=text, reason="user did not join")
                await self._speak(text)
                self._waiting_since = float("inf")  # wait for the fallback to finish, then end
            return
        if self._waiting_since == float("inf"):
            if t >= self._busy_until:
                self.j.emit("call_ended", t=t, by="agent", reason="user did not join")
                self._goto("ENDED")
            return
        if (t - self._last_keepalive >= self.s.keepalive_every_s and self._keepalives < self.s.keepalive_max
                and t >= self._busy_until):
            text = phrases.keepalives(self.lang)[self._keepalives % 3]
            self._keepalives += 1
            self._last_keepalive = t
            self.j.emit("keepalive_sent", t=t, n=self._keepalives, text=text)
            await self._speak(text)
