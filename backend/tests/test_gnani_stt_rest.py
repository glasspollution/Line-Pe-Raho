"""Prisma REST STT client against a fake transport replaying the recorded M0 response (T-004)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.gnani.errors import UsageMeter
from app.gnani.stt_rest import ClipTooLong, PrismaREST

FIX = json.loads((Path(__file__).parent / "fixtures" / "gnani" / "smoke_roundtrip.json").read_text(encoding="utf-8"))


def client(handler) -> PrismaREST:  # type: ignore[no-untyped-def]
    s = Settings(_env_file=None, gnani_api_key="k", retry_base_s=0.0, retry_max_s=0.0)
    return PrismaREST(s, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), meter=UsageMeter())


async def test_transcribe_with_recorded_response() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json=FIX["stt_rest"]["body"])

    stt = client(handler)
    out = await stt.transcribe(b"\x00\x00" * 8000 * 4, language="en-IN")
    assert out.text == "press one for recharge and billing press two for data issues"
    body = seen[0].content
    assert b'name="language_code"' in body and b"en-IN" in body and b'name="format"' in body
    assert b"RIFF" in body and b'filename="clip.wav"' in body
    assert stt.meter.snapshot()["stt_rest_seconds"] == 4.0


async def test_rejects_long_clip_without_network() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError("must not be called")

    with pytest.raises(ClipTooLong):
        await client(handler).transcribe(b"\x00\x00" * 8000 * 61)
