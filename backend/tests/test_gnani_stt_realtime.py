"""Prisma realtime client against a local fake WebSocket server (loopback only; no credits). T-005."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from websockets.asyncio.server import ServerConnection, serve

from app.config import Settings
from app.gnani.errors import UsageMeter
from app.gnani.stt_realtime import PrismaRealtime, bias_words

FRAME = b"\x01\x00" * 160  # 20 ms


class FakePrisma:
    """Records headers and chunk sizes; after `speak_after` bytes, emits speech_start then a transcript."""

    def __init__(self, speak_after: int = 8 * 1024, text: str = "press one for recharge") -> None:
        self.headers: list[dict[str, str]] = []
        self.chunks: list[list[int]] = []
        self.arrivals: list[float] = []
        self.speak_after = speak_after
        self.text = text
        self.bias_updates: list[Any] = []

    async def handler(self, ws: ServerConnection) -> None:
        assert ws.request is not None
        self.headers.append({k.lower(): v for k, v in ws.request.headers.raw_items()})
        sizes: list[int] = []
        self.chunks.append(sizes)
        await ws.send(json.dumps({"type": "connected", "config": {"sample_rate": 16000, "language_code": "en-IN"}}))
        total, spoke = 0, False
        async for msg in ws:
            if isinstance(msg, str):
                self.bias_updates.append(json.loads(msg))
                continue
            sizes.append(len(msg))
            self.arrivals.append(time.monotonic())
            total += len(msg)
            if not spoke and total >= self.speak_after:
                spoke = True
                await ws.send(json.dumps({"type": "speech_start", "utterance_id": "u1"}))
                await ws.send(json.dumps({"type": "transcript", "text": self.text, "audio_duration_ms": 900,
                                          "segment_id": "x-seg-1", "utterance_id": "u1", "is_final": True,
                                          "latency": 200}))


async def run_client(fake: FakePrisma, seconds: float, pace: bool = True, **settings: Any) -> tuple[PrismaRealtime, list]:  # type: ignore[type-arg]
    async with serve(fake.handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        s = Settings(_env_file=None, gnani_api_key="test-key", **settings)
        rt = PrismaRealtime(s, meter=UsageMeter(), url=f"ws://127.0.0.1:{port}/stt/v3/stream", pace=pace,
                            bias=["Nimbus", "Sahyog", "9to5", "two words"])
        await rt.start()
        got = []
        n = int(seconds / 0.02)
        for i in range(n):
            got += rt.feed(FRAME, i * 0.02)
            await asyncio.sleep(0.02 if pace else 0)
        for _ in range(100):  # let the last messages arrive
            got += rt.feed(FRAME, n * 0.02)
            await asyncio.sleep(0.01)
        await rt.close()
    return rt, got


async def test_headers_chunks_and_transcript() -> None:
    fake = FakePrisma()
    rt, got = await run_client(fake, 1.2)
    h = fake.headers[0]
    assert h["x-api-key-id"] == "test-key" and h["lang_code"] == "en-IN"
    assert h["x-sample-rate"] == "8000" and h["x-format"] == "transcribe"
    assert (h["x-vad-threshold"], h["x-min-speech-ms"], h["x-min-silence-ms"]) == ("0.7", "200", "500")
    bias = json.loads(h["x-bias-list"])
    assert "nimbus" in bias and "dabaiye" in bias and "9to5" not in bias and "two words" not in bias
    assert all(size == 1024 for size in fake.chunks[0][:-1])  # last chunk may be the zero-padded tail
    assert len(got) == 1 and got[0].text == "press one for recharge"
    assert 0 <= got[0].start <= got[0].end <= 1.3
    assert rt.server_configs[0]["sample_rate"] == 16000
    assert abs(rt.meter.snapshot()["stt_rt_seconds"] - len(fake.chunks[0]) * 0.064) < 1e-6


async def test_never_bursts_faster_than_real_time() -> None:
    fake = FakePrisma(speak_after=10**9)
    t0 = time.monotonic()
    await run_client(fake, 1.0, pace=True)
    span = fake.arrivals[-1] - fake.arrivals[0]
    sent_audio = len(fake.chunks[0]) * 0.064
    assert span >= sent_audio - 0.35, (span, sent_audio)
    assert time.monotonic() - t0 >= 1.0


async def test_rotation_overlaps_and_dedupes() -> None:
    fake = FakePrisma(speak_after=2048)  # each session says the same thing early on
    rt, got = await run_client(fake, 1.6, pace=False, stt_session_rotate_s=0.6, stt_session_overlap_s=0.2)
    assert len(fake.headers) >= 2  # rotated at least once
    assert sum(1 for tr in got if tr.text == "press one for recharge") >= 1
    texts_starts = [(tr.text, round(tr.start)) for tr in got]
    assert len(texts_starts) == len(set(texts_starts))  # overlap duplicates dropped


async def test_update_bias_message() -> None:
    fake = FakePrisma(speak_after=10**9)
    async with serve(fake.handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        rt = PrismaRealtime(Settings(_env_file=None, gnani_api_key="k"), meter=UsageMeter(),
                            url=f"ws://127.0.0.1:{port}/x", pace=False)
        await rt.start()
        await rt.update_bias(["pratinidhi"])
        await asyncio.sleep(0.1)
        await rt.close()
    assert fake.bias_updates[0]["type"] == "update_settings" and "pratinidhi" in fake.bias_updates[0]["bias"]["list"]


def test_bias_words_filter() -> None:
    assert bias_words(["Recharge", "recharge", "दबाइए", "a1", "two words"]) == ["recharge", "दबाइए"]
