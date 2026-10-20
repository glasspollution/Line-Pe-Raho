"""Auto-agent: the company's customer-care employee in eval and solo testing (CLAUDE.md §11.3).

A scripted-adaptive conversation (deterministic, so eval ground truth is exact):
  greet -> acknowledge the AI disclosure -> hear the customer's problem -> ask for the registered mobile
  -> give a ticket ID and a promised date -> "anything else?" -> goodbye -> hang up.
It reacts to WHAT it hears: keep-alives are ignored, a turn without a number re-asks for the number.
Hearing is pluggable (`hear(pcm, t_start, t_end) -> text`): exact caller text in simulation, Prisma REST
on live calls. Every line it can say is known at construction (`lines()`), so live runs pre-synthesise
them and it answers with zero TTS latency. Ground truth goes to `truth`.
"""

from __future__ import annotations

import asyncio
import collections
import datetime as dt
import random
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import numpy as np

from app.audio.frames import FRAME_BYTES
from app.company.voice import PromptAudio
from app.sim.oracle_stt import Utterance

SILENCE = b"\x00" * FRAME_BYTES
Hear = Callable[[bytes, float, float], Awaitable[str]]

PERSONAS: dict[str, dict[str, object]] = {
    "nimbus_telecom": {"prefix": "NMB", "names": ["Rohan", "Arjun", "Karan"], "fix": "the refund will be credited to your account"},
    "sahyog_bank": {"prefix": "SHB", "names": ["Vikram", "Rahul", "Aditya"], "fix": "the disputed amount will be reversed to your account"},
    "prakash_bijli_board": {"prefix": "PBB", "names": ["Suresh", "Amit", "Manoj"], "fix": "our team will restore the supply and close the complaint"},
}
_DIGIT_WORDS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]
_ORDINALS = ["", "first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth", "tenth",
             "eleventh", "twelfth", "thirteenth", "fourteenth", "fifteenth", "sixteenth", "seventeenth", "eighteenth",
             "nineteenth", "twentieth", "twenty first", "twenty second", "twenty third", "twenty fourth",
             "twenty fifth", "twenty sixth", "twenty seventh", "twenty eighth", "twenty ninth", "thirtieth",
             "thirty first"]
_AI_RE = re.compile(r"\bai\b|\ba i\b|a\.i\.|assistant|artificial intelligence|एआई", re.I)
_KEEPALIVE_RE = re.compile(r"joining|line par aa rahe|bane rahiye|bas ek second|with you shortly|stay on the line"
                           r"|jod rah|pal rukiye|connecting them|hold for a moment", re.I)
_NUMBER_WORDS_RE = re.compile(r"\b(?:" + "|".join(_DIGIT_WORDS + ["oh", "double", "triple"]) + r")\b")


PHONETIC = {"A": "Agra", "B": "Bangalore", "C": "Chennai", "D": "Delhi", "E": "Erode", "F": "Faridabad",
            "G": "Goa", "H": "Hyderabad", "I": "Indore", "J": "Jaipur", "K": "Kolkata", "L": "Lucknow",
            "M": "Mumbai", "N": "Nashik", "O": "Ooty", "P": "Pune", "Q": "Queen", "R": "Ranchi", "S": "Surat",
            "T": "Trichy", "U": "Udaipur", "V": "Vellore", "W": "Warangal", "X": "Xray", "Y": "Yamuna", "Z": "Zebra"}


def spell_id(ticket: str) -> str:
    """'NMB48213377' -> 'N for Nashik, M for Mumbai, B for Bangalore, four eight two one three three seven seven'
    (TTS-safe, no digits). Phone lines blur B/D, M/N: agents spell letters phonetically, so we do too."""
    letters = [f"{c} for {PHONETIC[c]}" for c in ticket if c.isalpha()]
    digits = " ".join(_DIGIT_WORDS[int(c)] for c in ticket if c.isdigit())
    return ", ".join(letters + [digits]) if letters else digits


def spoken_date(d: dt.date) -> str:
    return f"the {_ORDINALS[d.day]} of {d.strftime('%B')}"


def has_number(text: str) -> bool:
    return len(re.findall(r"\d", text)) + len(_NUMBER_WORDS_RE.findall(text.lower())) >= 6


@dataclass
class AgentTruth:
    company_id: str
    agent_name: str
    ticket_id: str
    promised_by: str  # ISO date
    greeting_start: float | None = None
    greeting_end: float | None = None
    ticket_said_at: float | None = None
    stages: list[str] = field(default_factory=list)


class AutoAgentLeg:
    def __init__(self, company_id: str, display_name: str, voice: PromptAudio, hear: Hear | None = None,
                 greets_first: bool = True, seed: int = 0, today: dt.date | None = None,
                 pickup_delay_s: float = 0.8, end_silence_s: float = 1.0) -> None:
        rng = random.Random(seed)
        persona = PERSONAS[company_id]
        names: list[str] = persona["names"]  # type: ignore[assignment]
        name = names[rng.randrange(len(names))]
        ticket = f"{persona['prefix']}{rng.randrange(10**7, 10**8)}"
        promised = (today or dt.date(2026, 10, 21)) + dt.timedelta(days=rng.choice([7, 10, 15]))
        self.truth = AgentTruth(company_id, name, ticket, promised.isoformat())
        self.voice, self.hear, self.greets_first = voice, hear, greets_first
        self.pickup_delay_s, self.end_silence_s = pickup_delay_s, end_silence_s
        fix = persona["fix"]
        self.text = {
            "greeting": f"Hello, this is {name} from {display_name}. How may I help you?",
            "probe_reply": "Haan ji, boliye, main sun raha hoon.",
            "ack": "Ji theek hai, main line par hoon.",
            "ask_mobile": "Sure, I can help you with that. May I have your registered mobile number please?",
            "repeat_mobile": "Sorry, I did not catch that. Could you please repeat your registered mobile number?",
            "ticket": (f"Thank you. I have raised a complaint for you. Your complaint number is {spell_id(ticket)}. "
                       f"{str(fix).capitalize()} by {spoken_date(promised)}. Is there anything else I can help you with?"),
            "goodbye": f"Thank you for calling {display_name}. Have a nice day.",
        }
        self.spoken: list[Utterance] = []
        self.connected_at: float | None = None
        self._out: collections.deque[bytes] = collections.deque()
        self._stage = "greet" if greets_first else "wait_probe"
        self._turn = bytearray()
        self._turn_start: float | None = None
        self._speech_s = 0.0
        self._silence_s = 0.0
        self._floor = 0.0
        self._pending: asyncio.Task[str] | None = None
        self._pending_span = (0.0, 0.0)
        self._mobile_tries = 0
        self._ending = False
        self._ended = False
        self._asked_more_at: float | None = None
        self.no_reply_goodbye_s = 8.0  # nobody answers "anything else?": a real agent closes the call

    def lines(self) -> list[str]:
        return list(self.text.values())

    def utterances(self) -> list[Utterance]:
        return list(self.spoken)

    # ---- speaking ----
    def _say(self, key: str, t: float) -> None:
        text = self.text[key]
        pcm = self.voice.render(text)
        pcm += b"\x00" * ((-len(pcm)) % FRAME_BYTES)
        self._out.extend(pcm[i : i + FRAME_BYTES] for i in range(0, len(pcm), FRAME_BYTES))
        start = t + 0.02
        u = Utterance(text, start, start + len(pcm) / 16000)
        self.spoken.append(u)
        self.truth.stages.append(key)
        if key == "greeting":
            self.truth.greeting_start, self.truth.greeting_end = u.start, u.end
        if key == "ticket":
            self.truth.ticket_said_at = u.start

    # ---- AgentLeg ----
    def on_connect(self, t: float) -> None:
        self.connected_at = t

    def receive(self, frame: bytes, t: float) -> None:
        if self._out or self._ending:  # we are talking (no barge-in handling needed for the demo flow)
            return
        x = np.frombuffer(frame, dtype="<i2").astype(np.float64)
        level = 20 * np.log10(max(float(np.sqrt(np.mean(x * x))), 1e-9) / 32768)
        self._floor = min(level, self._floor + 0.05)
        if level > self._floor + 8.0:
            if self._turn_start is None:
                self._turn_start = t
            self._speech_s += 0.02
            self._silence_s = 0.0
            self._turn += frame
            return
        if self._turn_start is None:
            return
        self._silence_s += 0.02
        self._turn += frame
        if self._silence_s >= self.end_silence_s:
            if self._speech_s >= 0.4 and self._pending is None:
                span = (self._turn_start, t - self._silence_s)
                self._pending_span = span
                self._pending = asyncio.get_running_loop().create_task(self._hear(bytes(self._turn), *span))
            self._turn = bytearray()
            self._turn_start = None
            self._speech_s = self._silence_s = 0.0

    async def _hear(self, pcm: bytes, t0: float, t1: float) -> str:
        return await self.hear(pcm, t0, t1) if self.hear else ""

    def next_frame(self, t: float) -> bytes:
        if self.connected_at is None:
            return SILENCE
        if self._stage == "greet" and t >= self.connected_at + self.pickup_delay_s:
            self._stage = "wait_disclosure"
            self._say("greeting", t)
        if self._pending is not None and self._pending.done():
            try:
                heard = self._pending.result()
            except Exception:  # noqa: BLE001 - a failed transcription is treated as unheard
                heard = ""
            self._pending = None
            self._react(heard, t)
        if self._out:
            return self._out.popleft()
        if self._stage == "wait_more" and self._pending is None:
            if self._asked_more_at is None:
                self._asked_more_at = t
            elif t - self._asked_more_at >= self.no_reply_goodbye_s and self._turn_start is None:
                self._say("goodbye", t)
                self._stage, self._ending = "done", True
        if self._ending:
            self._ended = True
        return SILENCE

    def _react(self, heard: str, t: float) -> None:
        h = heard.lower().strip()
        if not h:
            return  # noise or an unintelligible burst: not a turn (live Prisma returns "" for noise)
        st = self._stage
        if st == "wait_disclosure" and _AI_RE.search(h):
            self._say("ack", t)  # the AI introduced itself: acknowledge and wait for the customer
            self._stage = "wait_problem"
            return
        if _KEEPALIVE_RE.search(h) and not has_number(h):
            return  # the AI saying "they are joining / please hold": wait for the customer
        if st == "wait_probe":
            self._say("probe_reply", t)
            self._stage = "wait_disclosure"
        elif st == "wait_disclosure":
            if _AI_RE.search(h):
                self._say("ack", t)
                self._stage = "wait_problem"
            else:  # the customer spoke first
                self._say("ask_mobile", t)
                self._stage = "wait_mobile"
        elif st == "wait_problem":
            if _AI_RE.search(h):
                return
            self._say("ask_mobile", t)
            self._stage = "wait_mobile"
        elif st == "wait_mobile":
            if has_number(h) or self._mobile_tries >= 1:
                self._say("ticket", t)
                self._stage = "wait_more"
            else:
                self._mobile_tries += 1
                self._say("repeat_mobile", t)
        elif st == "wait_more":
            self._say("goodbye", t)
            self._stage = "done"
            self._ending = True

    @property
    def ended(self) -> bool:
        return self._ended
