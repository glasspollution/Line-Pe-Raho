"""Mock company IVR runtime (CLAUDE.md §11). Implements the VirtualLine FarEnd interface.

Node kinds (see trees/*.yaml):
  menu      say + options      play prompt, accept a key at any time (barge-in), timeout per on_timeout
  collect   collect + next     play prompt, gather digits until terminator / digit count
  ask       ask (+ routes)     play prompt, record the caller until they stop, transcribe, route by keyword
  hold      hold + then        music with periodic announcements, then the agent leg
  say       say + next         play, then continue ("hangup" ends the call)
Special targets: "agent" (connect the agent leg), "hangup".

Everything the IVR does is appended to `log` with line time: the ground truth for eval (prompt text for
WER, when the agent connected, what was collected).
"""

from __future__ import annotations

import asyncio
import collections
import random
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import numpy as np
import yaml

from app.audio.dtmf import DTMFDetector
from app.audio.frames import FRAME_BYTES, bytes_to_pcm
from app.company.hold_music import HoldMusic
from app.company.voice import PromptAudio
from app.config import Settings, get_settings

TREES_DIR = Path(__file__).resolve().parent / "trees"
SILENCE = b"\x00" * FRAME_BYTES
DEFAULT_MESSAGES = {
    "invalid": "Sorry, that is not a valid option.",
    "goodbye": "Thank you for calling. Goodbye.",
}

Recognizer = Callable[[bytes], Coroutine[Any, Any, str]]


class AgentLeg(Protocol):
    """Who answers after hold: the human console (T-109) or the auto-agent (T-208)."""

    def on_connect(self, t: float) -> None: ...

    def receive(self, frame: bytes, t: float) -> None: ...

    def next_frame(self, t: float) -> bytes: ...

    @property
    def ended(self) -> bool: ...


class SilentAgentLeg:
    """Placeholder agent: connects and says nothing (also models the 'silent agent' scenario)."""

    def __init__(self) -> None:
        self.connected_at: float | None = None

    def on_connect(self, t: float) -> None:
        self.connected_at = t

    def receive(self, frame: bytes, t: float) -> None:
        pass

    def next_frame(self, t: float) -> bytes:
        return SILENCE

    @property
    def ended(self) -> bool:
        return False


@dataclass
class Scenario:
    hold_s: float | None = None  # overrides the tree's hold duration range
    seed: int = 0
    answer_after_s: float | None = None


def list_companies() -> list[dict[str, str]]:
    out = []
    for f in sorted(TREES_DIR.glob("*.yaml")):
        t = yaml.safe_load(f.read_text(encoding="utf-8"))
        out.append({"id": t["id"], "display_name": t["display_name"]})
    return out


def load_tree(company_id: str) -> dict[str, Any]:
    path = TREES_DIR / f"{company_id}.yaml"
    if not path.exists():
        raise KeyError(f"unknown company {company_id!r}")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@dataclass
class _Hold:
    music: HoldMusic
    end_t: float
    every_s: float
    announcements: list[str]
    next_ann_t: float
    ann_idx: int = 0


@dataclass
class _Ask:
    buf: bytearray = field(default_factory=bytearray)
    speech_s: float = 0.0
    silence_s: float = 0.0
    started_t: float = 0.0
    task: asyncio.Task[str] | None = None


class IVREngine:
    def __init__(
        self,
        tree: dict[str, Any],
        voice: PromptAudio,
        agent_leg: AgentLeg | None = None,
        recognizer: Recognizer | None = None,
        scenario: Scenario | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.tree = tree
        self.voice = voice
        self.agent = agent_leg or SilentAgentLeg()
        self.recognizer = recognizer
        self.scenario = scenario or Scenario()
        self.s = settings or get_settings()
        self.answer_after_s = (self.scenario.answer_after_s if self.scenario.answer_after_s is not None
                               else self.s.ivr_answer_after_s)
        self.messages = {**DEFAULT_MESSAGES, **(tree.get("messages") or {})}
        self.rng = random.Random(self.scenario.seed)
        self.log: list[dict[str, Any]] = []
        self.collected: dict[str, str] = {}
        self.current_node: str | None = None
        self.mode = "idle"  # idle | play | listen | collect | ask | hold | agent | ended
        self._ended = False
        self._play: collections.deque[bytes] = collections.deque()
        self._after_play: Callable[[float], None] | None = None
        self._deadline: float | None = None
        self._repeats = 0
        self._digits = ""
        self._hold: _Hold | None = None
        self._ask: _Ask | None = None
        self._detector = DTMFDetector()
        self._t = 0.0

    # ---- FarEnd interface ----
    @property
    def ended(self) -> bool:
        return self._ended

    def on_connect(self, t: float) -> None:
        self._t = t
        self._event("connected")
        self._goto(self.tree["root"], t)

    def on_hangup(self, t: float) -> None:
        self._event("caller_hangup", t=t)
        self.mode = "ended"
        self._ended = True

    def receive(self, frame: bytes, t: float) -> None:
        self._t = t
        if self.mode == "agent":
            self.agent.receive(frame, t)
            return
        for d in self._detector.feed(frame):
            self._on_digit(d, t)
        if self.mode == "ask" and self._ask is not None and self._ask.task is None:
            self._ask_audio(frame, t)

    def next_frame(self, t: float) -> bytes:
        self._t = t
        if self.mode == "agent":
            if self.agent.ended:
                self._end(t, "agent_hangup")
            return self.agent.next_frame(t)
        if self.mode == "hold":
            return self._hold_frame(t)
        if self._play:
            frame = self._play.popleft()
            if not self._play and self._after_play is not None:
                cb, self._after_play = self._after_play, None
                cb(t)
            return frame
        if self._deadline is not None and t >= self._deadline:
            self._deadline = None
            self._on_timeout(t)
        if self.mode == "ask" and self._ask is not None:
            self._ask_poll(t)
        return SILENCE

    # ---- internals ----
    def _event(self, kind: str, t: float | None = None, **data: Any) -> None:
        self.log.append({"t": round(self._t if t is None else t, 3), "kind": kind, "node": self.current_node, **data})

    def _say(self, text: str, t: float, then: Callable[[float], None] | None) -> None:
        pcm = self.voice.render(text)
        if len(pcm) % FRAME_BYTES:
            pcm += b"\x00" * (FRAME_BYTES - len(pcm) % FRAME_BYTES)
        self._play.extend(pcm[i : i + FRAME_BYTES] for i in range(0, len(pcm), FRAME_BYTES))
        dur = len(pcm) / 2 / 8000
        self._event("say", t=t, text=text, duration_s=round(dur, 3))
        self._after_play = then
        self._deadline = None

    def _stop_playback(self) -> None:
        if self._play:  # barge-in: the rest of the prompt is never heard (ground truth for STT/WER)
            self._event("say_cut", remaining_s=round(len(self._play) * 0.02, 3))
        self._play.clear()
        self._after_play = None

    def _goto(self, name: str, t: float, repeat: bool = False) -> None:
        self._stop_playback()
        self._deadline = None
        if name == "hangup":
            self.mode = "play"
            self.current_node = None  # keys pressed during the goodbye must not act on the old menu
            self._say(self.messages["goodbye"], t, lambda tt: self._end(tt, "ivr_hangup"))
            return
        if name == "agent":
            self.mode = "agent"
            self._event("agent_connect", t=t)
            self.agent.on_connect(t)
            return
        if not repeat:
            self._repeats = 0
        self.current_node = name
        node = self.tree["nodes"][name]
        self._event("node_enter", t=t, repeat=repeat)
        self._digits = ""
        if "hold" in node:
            self._start_hold(node, t)
        elif "collect" in node:
            self.mode = "collect"
            self._say(node["collect"]["prompt"], t, self._listen_collect)
        elif "ask" in node:
            self.mode = "ask"
            self._ask = None
            self._say(node["ask"]["prompt"], t, self._start_ask)
        elif "options" in node:
            self.mode = "play"
            self._say(node["say"], t, self._listen_menu)
        else:  # say + next
            self.mode = "play"
            self._say(node.get("say", ""), t, lambda tt: self._goto(node["next"], tt))

    def _node(self) -> dict[str, Any]:
        assert self.current_node is not None
        node: dict[str, Any] = self.tree["nodes"][self.current_node]
        return node

    def _listen_menu(self, t: float) -> None:
        self.mode = "listen"
        self._deadline = t + float(self._node().get("timeout_s", self.s.ivr_default_timeout_s))

    def _listen_collect(self, t: float) -> None:
        self._deadline = t + self.s.collect_interdigit_timeout_s

    def _on_timeout(self, t: float) -> None:
        node = self._node()
        self._event("timeout", t=t)
        if "collect" in node:
            self._retry(t)
            return
        action = str(node.get("on_timeout", "repeat"))
        if action.startswith("goto:"):
            self._goto(action[5:], t)
        elif action == "hangup":
            self._goto("hangup", t)
        else:
            self._retry(t)

    def _retry(self, t: float) -> None:
        self._repeats += 1
        max_rep = int(self._node().get("max_repeats", self.s.ivr_default_max_repeats))
        if self._repeats > max_rep:
            self._goto("hangup", t)
        else:
            self._goto(self.current_node or self.tree["root"], t, repeat=True)

    def _on_digit(self, d: str, t: float) -> None:
        self._event("dtmf", t=t, digit=d)
        if self.mode in ("play", "listen") and self.current_node is not None:
            node = self._node()
            options = {str(k): v for k, v in (node.get("options") or {}).items()}
            if not options:
                return
            if d in options:
                self._event("option_selected", t=t, digit=d, target=options[d])
                self._goto(options[d], t)
            else:
                self._event("invalid_option", t=t, digit=d)
                self._stop_playback()
                self.mode = "play"
                self._say(self.messages["invalid"], t, lambda tt: self._retry(tt))
        elif self.mode == "collect":
            spec = self._node()["collect"]
            self._stop_playback()
            term, want = spec.get("terminator"), int(spec["digits"])
            if term is not None and d == term:
                self._finish_collect(t)
                return
            self._digits += d
            self._deadline = t + self.s.collect_interdigit_timeout_s
            if term is None and len(self._digits) >= want:
                self._finish_collect(t)

    def _finish_collect(self, t: float) -> None:
        spec = self._node()["collect"]
        if len(self._digits) == int(spec["digits"]) and self._digits.isdigit():
            self.collected[spec["field"]] = self._digits
            self._event("collected", t=t, field=spec["field"], n_digits=len(self._digits))
            self._goto(self._node()["next"], t)
        else:
            self._event("collect_invalid", t=t, n_digits=len(self._digits))
            self._digits = ""
            self._say(self.messages["invalid"], t, lambda tt: self._retry(tt))

    # -- ask (voice prompt) --
    def _start_ask(self, t: float) -> None:
        self._ask = _Ask(started_t=t)

    def _ask_audio(self, frame: bytes, t: float) -> None:
        a = self._ask
        assert a is not None
        x = bytes_to_pcm(frame).astype(np.float64)
        level = 20 * np.log10(max(float(np.sqrt(np.mean(x * x))), 1e-9) / 32768)
        dt = len(x) / 8000
        if level > self.s.ask_speech_dbfs:
            a.speech_s += dt
            a.silence_s = 0.0
            a.buf += frame
        elif a.speech_s > 0:
            a.silence_s += dt
            a.buf += frame
        done = (a.speech_s >= 0.3 and a.silence_s >= self.s.ask_end_silence_s) or (t - a.started_t >= self.s.ask_max_s)
        if done:
            self._event("ask_recorded", t=t, speech_s=round(a.speech_s, 2))
            if self.recognizer is None or a.speech_s < 0.3:
                self._route_ask("", t)
            else:
                a.task = asyncio.get_running_loop().create_task(self.recognizer(bytes(a.buf)))

    def _ask_poll(self, t: float) -> None:
        a = self._ask
        if a is not None and a.task is not None and a.task.done():
            try:
                text = a.task.result()
            except Exception as e:  # recognizer failure routes to fallback, like a real IVR
                self._event("ask_error", t=t, error=type(e).__name__)
                text = ""
            self._route_ask(text, t)

    def _route_ask(self, text: str, t: float) -> None:
        spec = self._node()["ask"]
        low = text.lower()
        target = next((dest for kw, dest in (spec.get("routes") or {}).items() if kw.lower() in low), None)
        self._event("ask_routed", t=t, text=text, target=target or spec.get("fallback"), matched=target is not None)
        self._ask = None
        self._goto(target or spec.get("fallback") or "hangup", t)

    # -- hold --
    def _start_hold(self, node: dict[str, Any], t: float) -> None:
        h = node["hold"]
        if self.scenario.hold_s is not None:
            dur = float(self.scenario.hold_s)
        else:
            rng = h.get("duration_s") or {"min": 60, "max": 600}
            dur = self.rng.uniform(float(rng["min"]), float(rng["max"]))
        every = float(h.get("announcement_every_s", 35))
        self._hold = _Hold(HoldMusic.from_name(h.get("music", "procedural_seed_1")), t + dur, every,
                           list(h.get("announcements") or []), next_ann_t=t + every)
        self.mode = "hold"
        self._event("hold_start", t=t, duration_s=round(dur, 2), music=h.get("music"))

    def _hold_frame(self, t: float) -> bytes:
        h = self._hold
        assert h is not None
        if self._play:  # announcement in progress (music paused)
            return self._play.popleft()
        if t >= h.end_t:
            self._event("hold_end", t=t)
            self._goto(self._node()["then"], t)
            return SILENCE
        if h.announcements and t >= h.next_ann_t:
            text = h.announcements[h.ann_idx % len(h.announcements)]
            h.ann_idx += 1
            self._say(text, t, None)
            self.log[-1]["kind"] = "announcement"
            h.next_ann_t = t + h.every_s
            return self._play.popleft()
        return h.music.next_frame()

    def _end(self, t: float, why: str) -> None:
        self._event(why, t=t)
        self.mode = "ended"
        self._ended = True
