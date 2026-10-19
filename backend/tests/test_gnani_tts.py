"""Timbre TTS client and PhraseCache against a fake HTTP transport (T-003). No credits."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.gnani.errors import APIHTTPError, UsageMeter
from app.gnani.tts import CachedVoice, PhraseCache, PhraseNotWarmed, TimbreTTS

FAKE_KEY = "test-key"


def make(handler, tmp_path: Path):  # type: ignore[no-untyped-def]
    s = Settings(_env_file=None, gnani_api_key=FAKE_KEY, data_dir=tmp_path, retry_base_s=0.0, retry_max_s=0.0)
    tts = TimbreTTS(s, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), meter=UsageMeter())
    return tts, PhraseCache(tts)


async def test_request_shape_and_metering(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, content=b"\x01\x00" * 800, headers={"content-type": "application/octet-stream"})

    tts, _ = make(handler, tmp_path)
    pcm = await tts.synthesize("Press 1 for billing.", "Riyaa", "hi-en")
    assert len(pcm) == 1600
    req = seen[0]
    assert req.url.path == "/api/v1/tts/inference" and req.headers["x-api-key-id"] == FAKE_KEY
    body = json.loads(req.content)
    assert body["audio_config"] == {"sample_rate": 8000, "num_channels": 1, "sample_width": 2,
                                    "encoding": "linear_pcm", "container": "raw"}
    assert (body["voice"], body["language"], body["model"]) == ("Riyaa", "hi-en", "timbre-v3.0")
    assert tts.meter.snapshot()["tts_chars"] == len("Press 1 for billing.")


async def test_retry_on_503_but_not_400(tmp_path: Path) -> None:
    calls = {"n": 0}

    def flaky(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, content=b"\x00\x00" * 10, headers={"content-type": "application/octet-stream"})

    tts, _ = make(flaky, tmp_path)
    assert len(await tts.synthesize("hi", "Riyaa", "en-IN")) == 20 and calls["n"] == 2

    tts2, _ = make(lambda r: httpx.Response(400, json={"error": "bad voice"}), tmp_path)
    with pytest.raises(APIHTTPError) as e:
        await tts2.synthesize("hi", "Nobody", "en-IN")
    assert e.value.status == 400


async def test_json_instead_of_audio_is_an_error(tmp_path: Path) -> None:
    tts, _ = make(lambda r: httpx.Response(200, json={"detail": "oops"}), tmp_path)
    with pytest.raises(APIHTTPError):
        await tts.synthesize("hi", "Riyaa", "en-IN")


async def test_phrase_cache_pays_once_and_voice_needs_prewarm(tmp_path: Path) -> None:
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, content=b"\x02\x00" * 100, headers={"content-type": "application/octet-stream"})

    tts, cache = make(handler, tmp_path)
    voice = CachedVoice(cache, "Shivani", "hi-en")
    with pytest.raises(PhraseNotWarmed):
        voice.render("Namaste")
    assert await voice.prewarm(["Namaste", "Namaste", "Bas ek second"]) == 2
    assert await voice.prewarm(["Namaste"]) == 0  # already on disk: free
    assert voice.render("Namaste") == b"\x02\x00" * 100
    assert calls["n"] == 2
    # a new cache object over the same directory still hits (survives restarts)
    assert PhraseCache(tts, cache.dir).get_cached("Namaste", "Shivani", "hi-en") is not None
    assert PhraseCache(tts, cache.dir).get_cached("Namaste", "Avinash", "hi-en") is None  # voice is part of the key
