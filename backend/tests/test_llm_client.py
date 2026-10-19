"""LLMClient against a fake OpenAI-compatible transport (T-007). No network, no AWS."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.config import Settings
from app.gnani.errors import UsageMeter
from app.llm.client import LLMClient, LLMError
from app.llm.schemas import IVRDecisionOut


class Tokens:
    def get(self) -> str:
        return "bearer-test"


def completion(content: str, pt: int = 100, ct: int = 20) -> dict[str, Any]:
    return {"id": "x", "object": "chat.completion", "created": 0, "model": "m",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct}}


def make(handler, **settings: Any) -> tuple[LLMClient, list[dict[str, Any]], list[httpx.Request]]:  # type: ignore[no-untyped-def]
    seen: list[httpx.Request] = []

    def wrapped(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        out: httpx.Response = handler(req)
        return out

    events: list[dict[str, Any]] = []
    s = Settings(_env_file=None, gnani_api_key="k", retry_base_s=0.0, retry_max_s=0.0,
                 llm_base_url="https://primary.test/v1", llm_model="kimi", **settings)
    c = LLMClient(s, meter=UsageMeter(), http_client=httpx.AsyncClient(transport=httpx.MockTransport(wrapped)),
                  tokens=Tokens(), on_event=events.append)  # type: ignore[arg-type]
    return c, events, seen


GOOD = json.dumps({"action": "press", "digit": "1", "reason": "billing", "confidence": 0.9})


async def test_valid_json_and_request_shape() -> None:
    c, _, seen = make(lambda r: httpx.Response(200, json=completion(GOOD)))
    out = await c.complete_json("You navigate IVRs.", "{}", IVRDecisionOut, realtime=True)
    assert out.digit == "1" and out.action == "press"
    body = json.loads(seen[0].content)
    assert body["model"] == "kimi" and body["max_tokens"] == 400 and body["reasoning_effort"] == "none"
    assert body["response_format"]["type"] == "json_schema"
    assert "Output JSON only" in body["messages"][0]["content"]
    assert seen[0].headers["authorization"] == "Bearer bearer-test"
    assert c.last_call and c.last_call.tokens_in == 100 and c.meter.snapshot()["llm_calls"] == 1


async def test_postcall_uses_postcall_budget() -> None:
    c, _, seen = make(lambda r: httpx.Response(200, json=completion(GOOD)))
    await c.complete_json("s", "u", IVRDecisionOut, realtime=False)
    body = json.loads(seen[0].content)
    assert body["max_tokens"] == 3000 and body["reasoning_effort"] == "low"


async def test_invalid_then_valid_retries_once_with_error() -> None:
    replies = iter(["not json at all", GOOD])
    c, _, seen = make(lambda r: httpx.Response(200, json=completion(next(replies))))
    out = await c.complete_json("s", "u", IVRDecisionOut)
    assert out.digit == "1" and len(seen) == 2
    second = json.loads(seen[1].content)["messages"]
    assert "did not match the schema" in second[-1]["content"]


async def test_invalid_twice_raises() -> None:
    c, _, _ = make(lambda r: httpx.Response(200, json=completion('{"action": "dance"}')))
    with pytest.raises(LLMError):
        await c.complete_json("s", "u", IVRDecisionOut)


async def test_fallback_used_and_reported() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "primary.test":
            return httpx.Response(503, json={"error": "down"})
        return httpx.Response(200, json=completion(GOOD))

    c, events, _ = make(handler, llm_fallback_base_url="https://fallback.test/v1", llm_fallback_model="backup")
    out = await c.complete_json("s", "u", IVRDecisionOut)
    assert out.digit == "1" and c.last_call and c.last_call.fallback and c.last_call.model == "backup"
    assert events and events[0]["llm_fallback"] is True


async def test_400_is_not_retried_and_no_fallback_means_error() -> None:
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, json={"error": "bad"})

    c, _, _ = make(handler)
    with pytest.raises(LLMError):
        await c.complete_json("s", "u", IVRDecisionOut)
    assert calls["n"] == 1
