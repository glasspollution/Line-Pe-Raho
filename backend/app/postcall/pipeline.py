"""Post-call pipeline (CLAUDE.md §12): timeline -> ITN -> extract -> verify -> summary with clip times.

Timeline sources:
  company  : realtime Prisma transcripts from the journal (callee track)
  ai_caller: our agent's own lines (exact text and line times from the orchestrator; no STT needed, §12.1)
  user     : exact text in simulation, or Prisma REST on speech spans of user.wav when live
ITN is local (postcall/itn.py): Prisma's transcribe mode keeps number words (M0), so a REST ITN pass adds
nothing. Batch STT (gnani/stt_batch.py) is optional (it polls every >= 10 s); realtime segments are used.
Extraction: LLM when configured, rules otherwise or on failure; then the deterministic verifier.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from app.config import get_settings
from app.gnani.stt_rest import PrismaREST
from app.postcall.extract import JsonLLM, extract_llm, extract_rules
from app.postcall.itn import itn
from app.postcall.verify import FieldVerdict, verify
from app.sim.oracle_stt import Utterance

log = logging.getLogger(__name__)
FIELD_ORDER = ("ticket_id", "agent_name", "resolution", "promised_by")


@dataclass
class PostCallInputs:
    call_id: str
    call_date: dt.date
    events: list[dict[str, Any]]
    agent_spoken: list[Utterance]
    user_spoken: list[Utterance] | None = None  # simulation: exact user text
    user_pcm: bytes | None = None  # live: user.wav samples (8 kHz s16le)
    out_dir: Path | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    ticket_prefix: str | None = None  # the company's known ticket format (trees/*.yaml), for a mishearing check


def speech_spans(pcm: bytes, rate: int = 8000, min_gap_s: float = 0.6, min_len_s: float = 0.4,
                 max_len_s: float = 55.0) -> list[tuple[float, float]]:
    """Energy-based speech spans in a clean user track (browser mic), each <= max_len_s for REST STT."""
    x = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
    frame = rate // 50
    n = len(x) // frame
    if n == 0:
        return []
    rms = np.sqrt(np.mean(x[: n * frame].reshape(n, frame) ** 2, axis=1))
    db = 20 * np.log10(np.maximum(rms, 1e-9) / 32768)
    active = db > max(-45.0, float(np.percentile(db, 20)) + 10)
    spans: list[tuple[float, float]] = []
    start, last = None, -1e9
    for i, on in enumerate(active):
        t = i / 50
        if on:
            if start is None or t - last > min_gap_s or t - start > max_len_s:
                if start is not None and last - start >= min_len_s:
                    spans.append((start, last + 0.02))
                start = t
            last = t
    if start is not None and last - start >= min_len_s:
        spans.append((start, last + 0.02))
    return spans


async def build_segments(inp: PostCallInputs, rest: PrismaREST | None = None) -> list[dict[str, Any]]:
    segs: list[dict[str, Any]] = []
    for e in inp.events:
        if e["type"] == "transcript" and e.get("track", "callee") == "callee":
            segs.append({"id": e["segment_id"], "speaker": "company", "start": e["start"], "end": e["end"],
                         "text": e["text"]})
    for i, u in enumerate(inp.agent_spoken, 1):
        segs.append({"id": f"a{i}", "speaker": "ai_caller", "start": u.start, "end": u.end, "text": u.text})
    if inp.user_spoken is not None:
        for i, u in enumerate(inp.user_spoken, 1):
            segs.append({"id": f"u{i}", "speaker": "user", "start": u.start, "end": u.end, "text": u.text})
    elif inp.user_pcm and rest is not None:
        for i, (a, b) in enumerate(speech_spans(inp.user_pcm), 1):
            clip = inp.user_pcm[int(a * 8000) * 2 : int(b * 8000) * 2]
            try:
                text = (await rest.transcribe(clip, language="en-IN")).text
            except Exception as e:  # noqa: BLE001 - a failed slice is skipped, not fatal
                log.warning("user-track STT failed for %.1f-%.1f: %s", a, b, e)
                continue
            if text.strip():
                segs.append({"id": f"u{i}", "speaker": "user", "start": round(a, 3), "end": round(b, 3), "text": text})
    for s in segs:
        s["text_itn"] = itn(s["text"])
    segs.sort(key=lambda s: (s["start"], s["id"]))
    return segs


async def summarize(inp: PostCallInputs, llm: JsonLLM | None = None, rest: PrismaREST | None = None) -> dict[str, Any]:
    segs = await build_segments(inp, rest)
    by_id = {s["id"]: s for s in segs}
    method = "rules"
    extraction = None
    if llm is not None:
        try:
            extraction = await extract_llm(segs, inp.call_date, llm)
            method = "llm"
        except Exception as e:  # noqa: BLE001 - fall back to rules (§9.4)
            log.warning("LLM extraction failed (%s); using rules", type(e).__name__)
    if extraction is None:
        extraction = extract_rules(segs, inp.call_date)
    ex = extraction.model_dump()
    for key in ("ticket_id",):
        if ex[key].get("value"):
            ex[key]["value"] = re.sub(r"\s+", "", str(ex[key]["value"])).upper()
    if ex["agent_name"].get("value"):
        ex["agent_name"]["value"] = str(ex["agent_name"]["value"]).strip().title()
    verdicts = verify(ex, by_id)
    tid = ex["ticket_id"].get("value")
    if tid and inp.ticket_prefix and not str(tid).startswith(inp.ticket_prefix):
        verdicts["ticket_id"] = FieldVerdict(False, f"possibly misheard: heard {tid}, {inp.ticket_prefix} tickets "
                                                    f"start with {inp.ticket_prefix}; play the clip to confirm")

    def enrich(item: dict[str, Any], key: str) -> dict[str, Any]:
        v = verdicts.get(key)
        return {
            "value": item.get("value"),
            **({"currency": item["currency"]} if "currency" in item else {}),
            "verified": bool(v and v.verified),
            "reason": v.reason if v else "not mentioned in the call",
            "evidence": [{**ev, **({k: by_id[ev["segment_id"]][k] for k in ("start", "end", "speaker")}
                                   if ev["segment_id"] in by_id else {})} for ev in item.get("evidence") or []],
        }

    fields: dict[str, Any] = {k: enrich(ex[k], k) for k in FIELD_ORDER}
    for i, a in enumerate(ex["amounts"]):
        fields[f"amounts[{i}]"] = enrich(a, f"amounts[{i}]")
    for i, n in enumerate(ex["next_steps"]):
        fields[f"next_steps[{i}]"] = enrich(n, f"next_steps[{i}]")
    present = [f for f in fields.values() if f["value"] not in (None, "")]
    summary = {
        "call_id": inp.call_id,
        "call_date": inp.call_date.isoformat(),
        "outcome": ex["outcome"],
        "method": method,
        "fields": fields,
        "verified_count": sum(f["verified"] for f in present),
        "unverified_count": sum(not f["verified"] for f in present),
        "clip_pad_s": get_settings().clip_pad_s,
        "segments": segs,
        "usage": inp.usage,
    }
    if inp.out_dir is not None:
        inp.out_dir.mkdir(parents=True, exist_ok=True)
        (inp.out_dir / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    return summary
