"""VirtualLine (CLAUDE.md §6.3): in-process duplex line to a FarEnd, through a PSTN simulator each way.

A pump task ticks every 20 ms (on the injected clock). Each tick moves exactly one frame in each
direction, which gives natural real-time pacing for anything queued with send_audio. Before the far
end answers, the caller hears Indian ringback (400+450 Hz, 0.4 on / 0.2 off / 0.4 on / 2.0 off).
"""

from __future__ import annotations

import asyncio
import collections
from collections.abc import AsyncIterator, Callable

import numpy as np

from app.audio import dtmf
from app.audio.frames import FRAME_BYTES, FRAME_SAMPLES, LINE_RATE, pcm_to_bytes
from app.audio.pstn_sim import PSTNSimulator
from app.line.base import FarEnd, LineState
from app.line.clock import Clock, RealClock

TICK_S = FRAME_SAMPLES / LINE_RATE  # 0.02
SILENCE = b"\x00" * FRAME_BYTES
_RINGBACK_CADENCE = ((0.4, True), (0.2, False), (0.4, True), (2.0, False))
_RINGBACK_DBFS = -19.0


def ringback_frame(t: float) -> bytes:
    """Indian ringback at line time t (seconds since ringing started)."""
    period = sum(d for d, _ in _RINGBACK_CADENCE)
    pos, acc = t % period, 0.0
    on = False
    for dur, is_on in _RINGBACK_CADENCE:
        if pos < acc + dur:
            on = is_on
            break
        acc += dur
    if not on:
        return SILENCE
    n = np.arange(FRAME_SAMPLES) / LINE_RATE + t
    amp = 32768.0 * 10 ** (_RINGBACK_DBFS / 20)
    x = amp * (np.sin(2 * np.pi * 400 * n) + np.sin(2 * np.pi * 450 * n)) / 2
    return pcm_to_bytes(np.round(x).astype(np.int16))


class VirtualLine:
    def __init__(
        self,
        far_end_factory: Callable[[str], FarEnd],
        uplink: PSTNSimulator | None = None,
        downlink: PSTNSimulator | None = None,
        clock: Clock | None = None,
        tap: Callable[[float, bytes, bytes], None] | None = None,
    ) -> None:
        self._factory = far_end_factory
        self._up = uplink
        self._down = downlink
        self.clock: Clock = clock or RealClock()
        self.tap = tap  # called every tick with (t, frame we sent, frame we heard) for aligned recording
        self._state: LineState = "idle"
        self._far: FarEnd | None = None
        self._out: collections.deque[bytes] = collections.deque()
        self._in: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._pump_task: asyncio.Task[None] | None = None
        self._drained = asyncio.Event()
        self._drained.set()
        self._connected = asyncio.Event()
        self.t0 = 0.0
        self.ticks = 0

    @property
    def state(self) -> LineState:
        return self._state

    @property
    def far_end(self) -> FarEnd | None:
        return self._far

    def line_time(self) -> float:
        """Seconds of line audio since dial (exact, tick-based)."""
        return self.ticks * TICK_S

    async def dial(self, target: str) -> None:
        if self._state != "idle":
            raise RuntimeError(f"cannot dial from state {self._state}")
        self._far = self._factory(target)
        self._state = "ringing"
        self.t0 = self.clock.now()
        self._pump_task = asyncio.create_task(self._pump(), name=f"virtual-line-{target}")

    async def wait_connected(self) -> None:
        await self._connected.wait()

    async def send_audio(self, frame: bytes) -> None:
        if len(frame) != FRAME_BYTES:
            raise ValueError(f"expected {FRAME_BYTES}-byte frames, got {len(frame)}")
        if self._state == "ended":
            return
        self._out.append(frame)
        self._drained.clear()

    async def send_dtmf(self, digits: str) -> None:
        pcm = pcm_to_bytes(dtmf.generate(digits, LINE_RATE))
        if len(pcm) % FRAME_BYTES:
            pcm += b"\x00" * (FRAME_BYTES - len(pcm) % FRAME_BYTES)
        for i in range(0, len(pcm), FRAME_BYTES):
            await self.send_audio(pcm[i : i + FRAME_BYTES])

    async def drain(self) -> None:
        await self._drained.wait()

    async def inbound(self) -> AsyncIterator[bytes]:
        while True:
            frame = await self._in.get()
            if frame is None:
                return
            yield frame

    async def hangup(self) -> None:
        if self._state == "ended":
            return
        self._end()
        if self._far is not None:
            self._far.on_hangup(self.line_time())
        if self._pump_task is not None and self._pump_task is not asyncio.current_task():
            self._pump_task.cancel()
            await asyncio.gather(self._pump_task, return_exceptions=True)

    def _end(self) -> None:
        self._state = "ended"
        self._out.clear()
        self._drained.set()
        self._connected.set()  # release waiters; they must check state
        self._in.put_nowait(None)

    async def _pump(self) -> None:
        assert self._far is not None
        far = self._far
        next_t = self.t0
        try:
            while self._state != "ended":
                next_t += TICK_S
                await self.clock.sleep_until(next_t)
                t = self.line_time()
                self.ticks += 1
                if self._state == "ringing":
                    if t >= far.answer_after_s:
                        self._state = "connected"
                        far.on_connect(t)
                        self._connected.set()
                    else:
                        ring = ringback_frame(t)
                        if self.tap:
                            self.tap(t, SILENCE, ring)
                        self._in.put_nowait(ring)
                        continue
                # caller -> callee
                up = self._out.popleft() if self._out else SILENCE
                if not self._out:
                    self._drained.set()
                far.receive(self._up.process(up) if self._up else up, t)
                # callee -> caller
                down = far.next_frame(t)
                heard = self._down.process(down) if self._down else down
                if self.tap:
                    self.tap(t, up, heard)
                self._in.put_nowait(heard)
                if far.ended:
                    self._end()
        except asyncio.CancelledError:
            pass
