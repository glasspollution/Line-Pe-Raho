"""Human console agent leg (CLAUDE.md §11.3): a teammate answers the call from the /company page.

Flow: the IVR connects the leg after hold -> the desk "rings" (caller hears silence) -> the console sends
{"type":"accept"} -> two-way audio -> {"type":"end"} (or the WS closes) hangs up from the agent side.

Audio: browser mic arrives as 48 kHz float32 and is resampled to 8 kHz s16le 20 ms frames; caller audio
goes to the browser as 8 kHz s16le. Buffers are capped (~10 s) so a vanished tab can't grow memory.
The leg uses plain deques because it is fed by the line pump and drained by the WS handler.
"""

from __future__ import annotations

import collections
from typing import Literal

import numpy as np

from app.audio.frames import FRAME_BYTES, Rechunker, pcm_to_bytes, resample

SILENCE = b"\x00" * FRAME_BYTES
_MAX_FRAMES = 500  # 10 s of 20 ms frames
BROWSER_RATE = 48000

LegState = Literal["waiting", "ringing", "talking", "ended"]


class ConsoleAgentLeg:
    def __init__(self, call_id: str) -> None:
        self.call_id = call_id
        self.state: LegState = "waiting"
        self.connected_at: float | None = None
        self.accepted_at: float | None = None
        self._from_console: collections.deque[bytes] = collections.deque(maxlen=_MAX_FRAMES)
        self._to_console: collections.deque[bytes] = collections.deque(maxlen=_MAX_FRAMES)
        self._rechunk = Rechunker(FRAME_BYTES)
        self._t = 0.0

    # -- AgentLeg (called by the IVR engine on the line pump) --
    def on_connect(self, t: float) -> None:
        self.connected_at = t
        self.state = "ringing"

    def receive(self, frame: bytes, t: float) -> None:
        self._t = t
        if self.state == "talking":
            self._to_console.append(frame)

    def next_frame(self, t: float) -> bytes:
        self._t = t
        if self.state == "talking" and self._from_console:
            return self._from_console.popleft()
        return SILENCE

    @property
    def ended(self) -> bool:
        return self.state == "ended"

    # -- console side (called by the WS handler) --
    def accept(self) -> None:
        if self.state == "ringing":
            self.state = "talking"
            self.accepted_at = self._t

    def end(self) -> None:
        self.state = "ended"

    def push_browser_audio(self, data: bytes) -> None:
        """48 kHz float32 little-endian samples from the browser worklet."""
        if self.state != "talking" or not data:
            return
        x = np.frombuffer(data[: len(data) - len(data) % 4], dtype="<f4")
        pcm = resample(np.clip(np.round(x * 32767), -32768, 32767).astype(np.int16), BROWSER_RATE, 8000)
        for f in self._rechunk.push(pcm_to_bytes(pcm)):
            self._from_console.append(f)

    def pop_for_console(self) -> bytes | None:
        return self._to_console.popleft() if self._to_console else None


class ConsoleRegistry:
    """Active console legs by call id (one call at a time in the MVP, but keyed for clarity)."""

    def __init__(self) -> None:
        self._legs: dict[str, ConsoleAgentLeg] = {}

    def create(self, call_id: str) -> ConsoleAgentLeg:
        leg = ConsoleAgentLeg(call_id)
        self._legs[call_id] = leg
        return leg

    def get(self, call_id: str) -> ConsoleAgentLeg | None:
        return self._legs.get(call_id)

    def remove(self, call_id: str) -> None:
        self._legs.pop(call_id, None)
