"""Retry/backoff policy, errors and the credit meter for external calls (CLAUDE.md §7.5).

Retry 429/500/502/503/504 and transport errors with exponential backoff and jitter (0.5 -> 8 s, at most
5 tries). Never retry other 4xx (a 400 is our bug, retrying just burns credits).
UsageMeter counts what we send (audio seconds, TTS characters, LLM calls/tokens); totals feed /api/usage.
"""

from __future__ import annotations

import asyncio
import random
import threading
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from typing import TypeVar

import httpx

from app.config import Settings, get_settings

T = TypeVar("T")
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class GnaniError(Exception):
    """Base for external-API failures (Gnani and LLM)."""


class APIHTTPError(GnaniError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"HTTP {status}: {message[:300]}")
        self.status = status
        self.message = message

    @property
    def retryable(self) -> bool:
        return self.status in RETRYABLE_STATUS


def raise_for_status(r: httpx.Response) -> None:
    if r.status_code >= 400:
        raise APIHTTPError(r.status_code, r.text)


@dataclass
class RetryPolicy:
    base_s: float = 0.5
    max_s: float = 8.0
    max_tries: int = 5

    @classmethod
    def from_settings(cls, s: Settings | None = None) -> RetryPolicy:
        s = s or get_settings()
        return cls(s.retry_base_s, s.retry_max_s, s.retry_max_tries)

    def delays(self, rng: random.Random | None = None) -> list[float]:
        """Backoff before tries 2..max_tries: min(max, base * 2^i), with jitter in [50%, 100%]."""
        r = rng or random.Random()
        return [min(self.max_s, self.base_s * 2**i) * r.uniform(0.5, 1.0) for i in range(self.max_tries - 1)]


def is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, APIHTTPError):
        return exc.retryable
    return isinstance(exc, httpx.TransportError | asyncio.TimeoutError)


async def with_retries(
    fn: Callable[[], Awaitable[T]],
    policy: RetryPolicy | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    rng: random.Random | None = None,
) -> T:
    policy = policy or RetryPolicy.from_settings()
    delays = policy.delays(rng)
    for attempt in range(policy.max_tries):
        try:
            return await fn()
        except Exception as e:  # noqa: BLE001 - classified below
            if not is_retryable(e) or attempt == policy.max_tries - 1:
                raise
            await sleep(delays[attempt])
    raise AssertionError("unreachable")


@dataclass
class Usage:
    stt_rt_seconds: float = 0.0
    stt_rest_seconds: float = 0.0
    stt_batch_seconds: float = 0.0
    tts_chars: int = 0
    tts_requests: int = 0
    tts_cache_hits: int = 0
    llm_calls: int = 0
    llm_tokens_in: int = 0
    llm_tokens_out: int = 0


class UsageMeter:
    """Thread-safe counters. One per call, plus a process-wide total (`GLOBAL_METER`)."""

    def __init__(self, parent: UsageMeter | None = None) -> None:
        self._u = Usage()
        self._lock = threading.Lock()
        self._parent = parent

    def add(self, **amounts: float) -> None:
        with self._lock:
            for k, v in amounts.items():
                setattr(self._u, k, getattr(self._u, k) + v)
        if self._parent is not None:
            self._parent.add(**amounts)

    def snapshot(self) -> dict[str, float]:
        with self._lock:
            d = asdict(self._u)
        return {k: round(v, 3) if isinstance(v, float) else v for k, v in d.items()}

    def child(self) -> UsageMeter:
        return UsageMeter(parent=self)


GLOBAL_METER = UsageMeter()
