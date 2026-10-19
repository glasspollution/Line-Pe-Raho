"""M0 spike: live LLM decision latency (spends a few cents at most).

Sends a realistic IVR menu decision with a JSON schema to the configured LLM (Kimi K3 on Bedrock by
default), N times with reasoning off and once with provider-default reasoning, and records latency,
token usage and outputs to backend/tests/fixtures/llm/smoke_decision.json (no secrets).

Run: python scripts/spikes/llm_smoke.py [--n 4]
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from openai import OpenAI  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.llm.auth import BearerTokenSource  # noqa: E402

SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["press", "wait", "repeat"]},
        "digit": {"type": ["string", "null"]},
        "reason": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": ["action", "digit", "reason", "confidence"],
    "additionalProperties": False,
}
SYSTEM = ("You navigate phone IVR menus for a caller. Output language: English, Latin script. "
          "Output JSON only, matching the schema.")
USER = json.dumps({
    "goal": "refund for duplicate recharge deduction",
    "options": [{"digit": "1", "label": "recharge and billing"}, {"digit": "2", "label": "data issues"},
                {"digit": "3", "label": "new connection"}, {"digit": "9", "label": "repeat"}],
    "path_so_far": ["1"],
})


def call(client: OpenAI, model: str, effort: str | None, max_tokens: int) -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    if effort:
        kwargs["reasoning_effort"] = effort
    t0 = time.perf_counter()
    r = client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": USER}],
        temperature=0.6, top_p=0.95, max_tokens=max_tokens,
        response_format={"type": "json_schema", "json_schema": {"name": "ivr_decision", "schema": SCHEMA,
                                                                "strict": True}},
        **kwargs,
    )
    dt = time.perf_counter() - t0
    text = r.choices[0].message.content or ""
    try:
        parsed: Any = json.loads(text)
    except ValueError:
        parsed = None
    u = r.usage
    return {"effort": effort or "provider-default", "latency_s": round(dt, 3), "valid_json": parsed is not None,
            "output": parsed if parsed is not None else text[:300],
            "prompt_tokens": u.prompt_tokens if u else None, "completion_tokens": u.completion_tokens if u else None,
            "finish_reason": r.choices[0].finish_reason}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=4)
    args = ap.parse_args()
    s = get_settings()
    client = OpenAI(base_url=s.llm_base_url, api_key=BearerTokenSource(s).get(), timeout=60)
    runs = []
    for i in range(args.n):
        res = call(client, s.llm_model, s.llm_reasoning_effort_realtime or None, s.llm_max_tokens_realtime)
        print(f"[effort=none #{i + 1}] {res['latency_s']}s valid={res['valid_json']} "
              f"tokens={res['prompt_tokens']}/{res['completion_tokens']} -> {json.dumps(res['output'])[:160]}")
        runs.append(res)
    default = call(client, s.llm_model, None, 2000)
    print(f"[provider-default reasoning] {default['latency_s']}s tokens={default['prompt_tokens']}/"
          f"{default['completion_tokens']} -> {json.dumps(default['output'])[:160]}")
    lat = [r["latency_s"] for r in runs]
    summary = {"model": s.llm_model, "base_url": s.llm_base_url, "n": len(runs),
               "p50_latency_s": round(statistics.median(lat), 3), "max_latency_s": max(lat),
               "all_valid_json": all(r["valid_json"] for r in runs), "runs": runs, "default_reasoning": default}
    out = ROOT / "backend" / "tests" / "fixtures" / "llm"
    out.mkdir(parents=True, exist_ok=True)
    (out / "smoke_decision.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"p50={summary['p50_latency_s']}s max={summary['max_latency_s']}s all_valid={summary['all_valid_json']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
