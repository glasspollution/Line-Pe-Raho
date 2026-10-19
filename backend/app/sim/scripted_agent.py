"""Scripted human agent for offline simulation (an AgentLeg for the IVR engine).

greets_first=True : says a greeting shortly after pickup, then waits like a human expecting a reply.
greets_first=False: stays silent until it hears the caller speak (e.g. the liveness probe), then answers.
After it hears the caller finish a longer turn (the disclosure), it acknowledges once.
Everything it says goes to `spoken` (ground truth for the oracle STT and for detection latency).
"""

from __future__ import annotations

import collections

import numpy as np

from app.audio.frames import FRAME_BYTES
from app.company.voice import PromptAudio
from app.sim.oracle_stt import Utterance

SILENCE = b"\x00" * FRAME_BYTES
_HEAR_ABOVE_FLOOR_DB = 8.0


class ScriptedAgentLeg:
    def __init__(self, voice: PromptAudio, greets_first: bool = True,
                 greeting: str = "Hello, this is Priya from Nimbus Telecom. How may I help you?",
                 probe_reply: str = "Haan ji, boliye, main sun rahi hoon.",
                 ack: str = "Ji theek hai, main line par hoon.",
                 pickup_delay_s: float = 0.8, end_after_s: float | None = None) -> None:
        self.voice = voice
        self.greets_first = greets_first
        self.greeting, self.probe_reply, self.ack = greeting, probe_reply, ack
        self.pickup_delay_s = pickup_delay_s
        self.end_after_s = end_after_s
        self.spoken: list[Utterance] = []
        self.connected_at: float | None = None
        self._out: collections.deque[bytes] = collections.deque()
        self._caller_speech_s = 0.0
        self._caller_silence_s = 0.0
        self._replied_probe = False
        self._acked = False
        self._ended = False
        self._floor = 0.0  # tracked noise floor (dBFS); starts high and settles within ~1 s

    def utterances(self) -> list[Utterance]:
        return list(self.spoken)

    @property
    def greeting_end(self) -> float | None:
        return self.spoken[0].end if self.spoken and self.greets_first else None

    def _say(self, text: str, t: float) -> None:
        pcm = self.voice.render(text)
        pcm += b"\x00" * ((-len(pcm)) % FRAME_BYTES)
        self._out.extend(pcm[i : i + FRAME_BYTES] for i in range(0, len(pcm), FRAME_BYTES))
        start = t + 0.02
        self.spoken.append(Utterance(text, start, start + len(pcm) / 16000))

    # AgentLeg interface
    def on_connect(self, t: float) -> None:
        self.connected_at = t

    def receive(self, frame: bytes, t: float) -> None:
        x = np.frombuffer(frame, dtype="<i2").astype(np.float64)
        level = 20 * np.log10(max(float(np.sqrt(np.mean(x * x))), 1e-9) / 32768)
        self._floor = min(level, self._floor + 0.05)  # follows dips instantly, rises slowly
        if level > self._floor + _HEAR_ABOVE_FLOOR_DB:
            self._caller_speech_s += 0.02
            self._caller_silence_s = 0.0
            return
        self._caller_silence_s += 0.02
        if self._caller_speech_s >= 0.3 and self._caller_silence_s >= 0.6 and not self._out:
            if not self.greets_first and not self._replied_probe:
                self._replied_probe = True
                self._say(self.probe_reply, t)
            elif self._caller_speech_s >= 2.0 and not self._acked:
                self._acked = True
                self._say(self.ack, t)
            self._caller_speech_s = 0.0

    def next_frame(self, t: float) -> bytes:
        if self.connected_at is None:
            return SILENCE
        if self.greets_first and not self.spoken and t >= self.connected_at + self.pickup_delay_s:
            self._say(self.greeting, t)
        if self.end_after_s is not None and t >= self.connected_at + self.end_after_s and not self._out:
            self._ended = True
        return self._out.popleft() if self._out else SILENCE

    @property
    def ended(self) -> bool:
        return self._ended
