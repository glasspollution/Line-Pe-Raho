"""Retry policy and usage meter (T-002)."""

from __future__ import annotations

import random

import httpx
import pytest

from app.gnani.errors import APIHTTPError, RetryPolicy, UsageMeter, with_retries


class Flaky:
    def __init__(self, failures: list[BaseException]) -> None:
        self.failures = list(failures)
        self.calls = 0

    async def __call__(self) -> str:
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return "ok"


async def run(fn: Flaky) -> tuple[str | None, list[float], BaseException | None]:
    slept: list[float] = []

    async def sleep(d: float) -> None:
        slept.append(d)

    try:
        out = await with_retries(fn, RetryPolicy(), sleep=sleep, rng=random.Random(1))
        return out, slept, None
    except Exception as e:  # noqa: BLE001
        return None, slept, e


def test_backoff_sequence_is_bounded_and_jittered() -> None:
    d = RetryPolicy().delays(random.Random(0))
    caps = [0.5, 1.0, 2.0, 4.0]
    assert len(d) == 4 and all(c / 2 <= x <= c for x, c in zip(d, caps, strict=True))
    assert max(RetryPolicy(max_tries=8).delays(random.Random(0))) <= 8.0


@pytest.mark.parametrize("status", [429, 500, 503])
async def test_retries_transient_then_succeeds(status: int) -> None:
    fn = Flaky([APIHTTPError(status, "busy"), APIHTTPError(status, "busy")])
    out, slept, err = await run(fn)
    assert out == "ok" and fn.calls == 3 and len(slept) == 2 and err is None


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
async def test_never_retries_client_errors(status: int) -> None:
    fn = Flaky([APIHTTPError(status, "bad request")])
    out, slept, err = await run(fn)
    assert isinstance(err, APIHTTPError) and fn.calls == 1 and slept == []


async def test_transport_errors_retry_and_give_up_after_5() -> None:
    fn = Flaky([httpx.ConnectError("down")] * 10)
    out, slept, err = await run(fn)
    assert isinstance(err, httpx.ConnectError) and fn.calls == 5 and len(slept) == 4


def test_usage_meter_child_rolls_up() -> None:
    total = UsageMeter()
    call = total.child()
    call.add(tts_chars=120, tts_requests=1)
    call.add(stt_rt_seconds=1.5)
    assert call.snapshot()["tts_chars"] == 120
    assert total.snapshot()["stt_rt_seconds"] == 1.5
