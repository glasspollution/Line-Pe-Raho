"""Prisma REST STT (CLAUDE.md §7.2): short clips (<= 60 s) -> text.

Used for the user's spoken task input and the post-call ITN pass on number-bearing slices.
Clips over 60 s are rejected before any network call. M0 finding: `format=transcribe` keeps number
words ("press one"), so callers that need digits must normalise themselves.
"""

from __future__ import annotations

import io
import wave
from dataclasses import dataclass
from typing import Any

import httpx

from app.audio.frames import LINE_RATE
from app.config import Settings, get_settings
from app.gnani.errors import GLOBAL_METER, APIHTTPError, RetryPolicy, UsageMeter, raise_for_status, with_retries

MAX_CLIP_S = 60.0


class ClipTooLong(ValueError):
    pass


@dataclass(frozen=True)
class RestTranscript:
    text: str
    request_id: str | None
    raw: dict[str, Any]


def pcm_to_wav(pcm: bytes, rate: int = LINE_RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


class PrismaREST:
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

    async def transcribe(self, pcm: bytes, language: str = "en-IN", fmt: str = "transcribe",
                         rate: int = LINE_RATE) -> RestTranscript:
        seconds = len(pcm) / 2 / rate
        if seconds > MAX_CLIP_S:
            raise ClipTooLong(f"clip is {seconds:.1f}s; REST STT accepts <= {MAX_CLIP_S:.0f}s")
        wav = pcm_to_wav(pcm, rate)
        url = f"{self.s.gnani_base_url.rstrip('/')}/stt/v3"
        headers = {"X-API-Key-ID": self.s.gnani_api_key.get_secret_value()}

        async def call() -> dict[str, Any]:
            r = await self._http().post(url, headers=headers, files={"audio_file": ("clip.wav", wav, "audio/wav")},
                                        data={"language_code": language, "format": fmt})
            raise_for_status(r)
            body: dict[str, Any] = r.json()
            if body.get("success") is False:
                raise APIHTTPError(502, f"STT reported failure: {body}")
            return body

        body = await with_retries(call, RetryPolicy.from_settings(self.s))
        self.meter.add(stt_rest_seconds=seconds)
        text = body.get("transcript") or (body.get("output") or {}).get("literal") or ""
        return RestTranscript(str(text), body.get("request_id"), body)
