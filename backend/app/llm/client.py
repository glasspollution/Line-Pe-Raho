"""OpenAI-compatible LLM client returning pydantic models (CLAUDE.md §9.4). Default: Kimi K3 on Bedrock.

complete_json(system, user, Model, realtime) -> Model
  - sends a JSON schema derived from the pydantic model (response_format json_schema, non-strict)
  - validates with pydantic; on invalid output retries once with the validation error appended
  - realtime=True uses llm_reasoning_effort_realtime / llm_max_tokens_realtime / 4 s timeout;
    post-call uses the *_postcall settings and a 60 s timeout. max_tokens is always explicit (Bedrock
    reserves quota for the maximum when it is unset).
  - transient errors retry per §7.5; if the primary endpoint still fails and a fallback is configured,
    the fallback is used and reported through `on_event` (the caller journals it, §0.2)
Every call records model, endpoint, latency, prompt hash and token usage in `last_call` and the meter.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

import httpx
from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI
from pydantic import BaseModel, ValidationError

from app.config import Settings, get_settings
from app.gnani.errors import GLOBAL_METER, APIHTTPError, RetryPolicy, UsageMeter, with_retries
from app.llm.auth import BearerTokenSource

M = TypeVar("M", bound=BaseModel)


class LLMError(Exception):
    pass


@dataclass
class CallInfo:
    model: str
    endpoint: str
    latency_ms: int
    prompt_hash: str
    tokens_in: int
    tokens_out: int
    attempts: int
    fallback: bool


class LLMClient:
    def __init__(self, settings: Settings | None = None, meter: UsageMeter | None = None,
                 http_client: httpx.AsyncClient | None = None, tokens: BearerTokenSource | None = None,
                 on_event: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.s = settings or get_settings()
        self.meter = meter or GLOBAL_METER
        self._http = http_client
        self._tokens = tokens or BearerTokenSource(self.s)
        self.on_event = on_event
        self.last_call: CallInfo | None = None
        self._clients: dict[tuple[str, str], AsyncOpenAI] = {}

    def _client(self, base_url: str, key: str) -> AsyncOpenAI:
        ck = (base_url, key)
        if ck not in self._clients:  # a new token (hourly) gets a new client
            # the SDK annotates its own bundled httpx; a plain httpx.AsyncClient works at runtime (tested)
            self._clients = {ck: AsyncOpenAI(base_url=base_url, api_key=key, max_retries=0,
                                             http_client=self._http)}  # type: ignore[arg-type]
        return self._clients[ck]

    @staticmethod
    def schema_for(model: type[BaseModel]) -> dict[str, Any]:
        return {"type": "json_schema", "json_schema": {"name": model.__name__, "schema": model.model_json_schema(),
                                                        "strict": False}}

    async def complete_json(self, system: str, user: str, model: type[M], realtime: bool = True,
                            effort: str | None = None, max_tokens: int | None = None,
                            timeout_s: float | None = None) -> M:
        """realtime picks the default budget; effort/max_tokens/timeout_s override it per call."""
        s = self.s
        if effort is None:
            effort = s.llm_reasoning_effort_realtime if realtime else s.llm_reasoning_effort_postcall
        if max_tokens is None:
            max_tokens = s.llm_max_tokens_realtime if realtime else s.llm_max_tokens_postcall
        timeout = timeout_s if timeout_s is not None else (
            s.llm_timeout_realtime_s if realtime else s.llm_timeout_postcall_s)
        system = system.rstrip() + "\nOutput JSON only, matching the provided schema."
        messages: list[dict[str, str]] = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        prompt_hash = hashlib.sha1(json.dumps(messages, ensure_ascii=False).encode()).hexdigest()[:12]
        endpoints = [(s.llm_base_url, s.llm_model, False)]
        if s.llm_fallback_base_url and s.llm_fallback_model:
            endpoints.append((s.llm_fallback_base_url, s.llm_fallback_model, True))

        last_exc: Exception | None = None
        for base_url, model_name, is_fallback in endpoints:
            if is_fallback and self.on_event:
                self.on_event({"type": "error", "message": f"LLM primary failed ({type(last_exc).__name__}); "
                                                           f"using fallback model {model_name}", "llm_fallback": True})
            try:
                return await self._call(base_url, model_name, messages, model, effort, max_tokens, timeout,
                                        prompt_hash, is_fallback)
            except (LLMError, APIHTTPError, httpx.TransportError, TimeoutError) as e:
                last_exc = e
        raise LLMError(f"all LLM endpoints failed: {last_exc}") from last_exc

    async def _call(self, base_url: str, model_name: str, messages: list[dict[str, str]], model: type[M],
                    effort: str, max_tokens: int, request_timeout_s: float, prompt_hash: str,
                    is_fallback: bool) -> M:
        key = self._tokens.get() if not is_fallback else self.s.llm_api_key.get_secret_value() or "none"
        client = self._client(base_url, key)
        extra: dict[str, Any] = {"reasoning_effort": effort} if effort else {}
        msgs = list(messages)
        t0 = time.perf_counter()
        tokens_in = tokens_out = 0
        for attempt in (1, 2):
            kwargs: dict[str, Any] = {
                "model": model_name, "messages": list(msgs), "temperature": self.s.llm_temperature,
                "top_p": self.s.llm_top_p, "max_tokens": max_tokens, "timeout": request_timeout_s,
                "response_format": self.schema_for(model), **extra,
            }

            async def once(kw: dict[str, Any] = kwargs) -> Any:
                try:
                    return await client.chat.completions.create(**kw)
                except APIStatusError as e:
                    raise APIHTTPError(e.status_code, str(e)) from e
                except (APITimeoutError, APIConnectionError) as e:
                    raise httpx.TransportError(str(e)) from e

            r = await with_retries(once, RetryPolicy.from_settings(self.s))
            if r.usage:
                tokens_in += r.usage.prompt_tokens or 0
                tokens_out += r.usage.completion_tokens or 0
            text = (r.choices[0].message.content or "").strip()
            try:
                out = model.model_validate_json(_strip_fences(text))
            except ValidationError as e:
                if attempt == 2:
                    self._record(model_name, base_url, t0, prompt_hash, tokens_in, tokens_out, attempt, is_fallback)
                    raise LLMError(f"invalid JSON after retry: {e.errors()[:2]}") from e
                msgs = msgs + [{"role": "assistant", "content": text[:2000]},
                               {"role": "user", "content": f"That did not match the schema: {e.errors()[:3]}. "
                                                           "Reply again with valid JSON only."}]
                continue
            self._record(model_name, base_url, t0, prompt_hash, tokens_in, tokens_out, attempt, is_fallback)
            return out
        raise AssertionError("unreachable")

    def _record(self, model: str, base_url: str, t0: float, prompt_hash: str, tin: int, tout: int,
                attempts: int, fallback: bool) -> None:
        self.last_call = CallInfo(model, base_url, int((time.perf_counter() - t0) * 1000), prompt_hash, tin, tout,
                                  attempts, fallback)
        self.meter.add(llm_calls=attempts, llm_tokens_in=tin, llm_tokens_out=tout)


def _strip_fences(text: str) -> str:
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):] if "{" in text else text
    return text
