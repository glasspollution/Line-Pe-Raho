"""PhoneLine protocol (CLAUDE.md §6.3) and the far-end interface the virtual line talks to.

Deviation from §6.3 (decisions log): `drain()` was added so callers can await playback of queued
audio (DTMF bursts, the disclosure) before acting on it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Literal, Protocol

LineState = Literal["idle", "ringing", "connected", "ended"]


class PhoneLine(Protocol):
    async def dial(self, target: str) -> None: ...

    async def send_audio(self, frame: bytes) -> None: ...  # 320-byte 8 kHz frame, caller -> callee

    def inbound(self) -> AsyncIterator[bytes]: ...  # 320-byte frames, callee -> caller

    async def send_dtmf(self, digits: str) -> None: ...  # in-band tones via send_audio

    async def drain(self) -> None: ...  # wait until all queued outbound audio has been played

    async def hangup(self) -> None: ...

    @property
    def state(self) -> LineState: ...


class FarEnd(Protocol):
    """The callee side of a VirtualLine (the mock company engine, or a test double).

    The line calls `receive` and `next_frame` once per 20 ms tick, so both must be cheap and non-blocking.
    """

    answer_after_s: float  # ring time before the far end picks up

    def on_connect(self, t: float) -> None: ...

    def receive(self, frame: bytes, t: float) -> None: ...  # caller audio (already degraded by the line)

    def next_frame(self, t: float) -> bytes: ...  # callee audio for this tick (silence if nothing to say)

    def on_hangup(self, t: float) -> None: ...

    @property
    def ended(self) -> bool: ...  # True once the far end has hung up
