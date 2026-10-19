"""Clocks that pace the line. RealClock for live calls; FakeClock runs as fast as the CPU allows
(tests and headless eval) while keeping simulated time exact."""

from __future__ import annotations

import asyncio
from typing import Protocol


class Clock(Protocol):
    def now(self) -> float: ...

    async def sleep_until(self, t: float) -> None: ...


class RealClock:
    def now(self) -> float:
        return asyncio.get_running_loop().time()

    async def sleep_until(self, t: float) -> None:
        delay = t - self.now()
        await asyncio.sleep(delay if delay > 0 else 0)


class FakeClock:
    """Simulated time: sleeping jumps straight to the target time (yielding once to other tasks)."""

    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def now(self) -> float:
        return self.t

    async def sleep_until(self, t: float) -> None:
        if t > self.t:
            self.t = t
        await asyncio.sleep(0)
