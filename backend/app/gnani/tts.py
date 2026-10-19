"""Timbre TTS (CLAUDE.md §7.4): REST client, on-disk PhraseCache, and synchronous voices.

- TimbreTTS.synthesize(): POST /api/v1/tts/inference -> raw 8 kHz s16le mono PCM (retries + metering).
- PhraseCache: data/tts_cache/{sha1(model|voice|lang|text)}.pcm. Each phrase is paid for once, ever.
- CachedVoice: the synchronous `render(text)` the IVR engine and orchestrator call on the 20 ms tick. It
  never touches the network; phrases must be pre-warmed (`await voice.prewarm(texts)`), and a missing
  phrase raises instead of stalling the line. This is how the disclosure plays with zero network latency.
The realtime WS endpoint (for S1 dynamic agent speech) is not implemented yet.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import httpx

from app.config import Settings, get_settings
from app.gnani.errors import GLOBAL_METER, APIHTTPError, RetryPolicy, UsageMeter, raise_for_status, with_retries

AUDIO_CONFIG = {"sample_rate": 8000, "num_channels": 1, "sample_width": 2, "encoding": "linear_pcm",
                "container": "raw"}


class TimbreTTS:
    def __init__(self, settings: Settings | None = None, client: httpx.AsyncClient | None = None,
                 meter: UsageMeter | None = None, timeout_s: float = 30.0) -> None:
        self.s = settings or get_settings()
        self.meter = meter or GLOBAL_METER
        self._client = client
        self._timeout = timeout_s

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    async def synthesize(self, text: str, voice: str, language: str, speed: float = 1.0,
                         model: str | None = None) -> bytes:
        text = text.strip()
        if not text:
            return b""
        body = {"text": text, "voice": voice, "model": model or self.s.tts_model, "language": language,
                "speed": speed, "audio_config": AUDIO_CONFIG}
        headers = {"X-API-Key-ID": self.s.gnani_api_key.get_secret_value()}
        url = f"{self.s.gnani_base_url.rstrip('/')}/api/v1/tts/inference"

        async def call() -> bytes:
            r = await self._http().post(url, json=body, headers=headers)
            raise_for_status(r)
            if r.headers.get("content-type", "").startswith("application/json"):
                raise APIHTTPError(502, f"expected audio, got JSON: {r.text[:200]}")
            return r.content

        pcm = await with_retries(call, RetryPolicy.from_settings(self.s))
        self.meter.add(tts_chars=len(text), tts_requests=1)
        if len(pcm) % 2:
            pcm = pcm[:-1]
        return pcm


def cache_key(model: str, voice: str, language: str, text: str) -> str:
    return hashlib.sha1(f"{model}|{voice}|{language}|{text}".encode()).hexdigest()


class PhraseCache:
    def __init__(self, tts: TimbreTTS, cache_dir: Path | None = None) -> None:
        self.tts = tts
        self.dir = cache_dir or (tts.s.data_dir / "tts_cache")
        self.dir.mkdir(parents=True, exist_ok=True)

    def path(self, text: str, voice: str, language: str) -> Path:
        return self.dir / f"{cache_key(self.tts.s.tts_model, voice, language, text.strip())}.pcm"

    def get_cached(self, text: str, voice: str, language: str) -> bytes | None:
        p = self.path(text, voice, language)
        return p.read_bytes() if p.exists() else None

    async def get(self, text: str, voice: str, language: str) -> bytes:
        cached = self.get_cached(text, voice, language)
        if cached is not None:
            self.tts.meter.add(tts_cache_hits=1)
            return cached
        pcm = await self.tts.synthesize(text, voice, language)
        tmp = self.path(text, voice, language).with_suffix(".tmp")
        tmp.write_bytes(pcm)
        tmp.replace(self.path(text, voice, language))
        return pcm

    async def warm(self, texts: list[str], voice: str, language: str) -> int:
        """Synthesise anything missing. Returns how many were newly synthesised (i.e. cost credits)."""
        new = 0
        for text in dict.fromkeys(t.strip() for t in texts if t.strip()):
            if self.get_cached(text, voice, language) is None:
                await self.get(text, voice, language)
                new += 1
        return new


class PhraseNotWarmed(KeyError):
    pass


class CachedVoice:
    """Synchronous render() backed by the PhraseCache (implements PromptAudio / AgentVoice)."""

    def __init__(self, cache: PhraseCache, voice: str, language: str) -> None:
        self.cache, self.voice, self.language = cache, voice, language

    async def prewarm(self, texts: list[str]) -> int:
        return await self.cache.warm(texts, self.voice, self.language)

    def render(self, text: str) -> bytes:
        pcm = self.cache.get_cached(text, self.voice, self.language)
        if pcm is None:
            raise PhraseNotWarmed(f"not pre-synthesised for {self.voice}/{self.language}: {text[:60]!r}")
        return pcm
