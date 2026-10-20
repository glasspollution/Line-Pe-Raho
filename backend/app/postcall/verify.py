"""Deterministic verifier for the post-call extraction (CLAUDE.md §12.5). Contract: test_verify.py.

For every non-null field:
  - it must cite evidence, every cited segment must exist, and every quote must fuzzy-match (>= 90,
    partial ratio, case/punctuation-insensitive) its segment's text or text_itn
  - IDs and amounts must appear literally in the text_itn of a cited segment
  - dates must parse as ISO (YYYY-MM-DD)
Unverified fields are reported with a reason, never dropped.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from typing import Any

from rapidfuzz import fuzz

from app.config import get_settings


@dataclass(frozen=True)
class FieldVerdict:
    verified: bool
    reason: str


_PUNCT = re.compile(r"[^\w\s₹]")


def _norm(text: str) -> str:
    return " ".join(_PUNCT.sub(" ", text.lower()).split())


def _fmt_amount(v: Any) -> str:
    f = float(v)
    return str(int(f)) if f.is_integer() else str(f)


def _check(item: dict[str, Any], segments: dict[str, dict[str, Any]], literal: str | None = None,
           is_date: bool = False) -> FieldVerdict:
    min_ratio = get_settings().verify_quote_fuzzy_min
    evidence = item.get("evidence") or []
    if not evidence:
        return FieldVerdict(False, "no evidence cited")
    for ev in evidence:
        seg = segments.get(ev.get("segment_id", ""))
        if seg is None:
            return FieldVerdict(False, f"cited segment {ev.get('segment_id')!r} does not exist")
        quote = _norm(ev.get("quote", ""))
        if not quote:
            return FieldVerdict(False, "empty quote")
        best = max(fuzz.partial_ratio(quote, _norm(seg.get("text") or "")),
                   fuzz.partial_ratio(quote, _norm(seg.get("text_itn") or "")))
        if best < min_ratio:
            return FieldVerdict(False, f"quote not found in segment {ev['segment_id']} (match {best:.0f})")
    if literal is not None:
        cited = [segments[ev["segment_id"]].get("text_itn") or segments[ev["segment_id"]].get("text") or ""
                 for ev in evidence]
        if not any(literal.lower() in c.lower() for c in cited):
            return FieldVerdict(False, f"{literal!r} does not appear in the cited segment")
    if is_date:
        try:
            dt.date.fromisoformat(str(item.get("value")))
        except ValueError:
            return FieldVerdict(False, f"date {item.get('value')!r} does not parse")
    return FieldVerdict(True, "quote and value found in the cited audio")


def verify(extraction: dict[str, Any], segments: dict[str, dict[str, Any]]) -> dict[str, FieldVerdict]:
    out: dict[str, FieldVerdict] = {}
    for key in ("ticket_id", "agent_name", "resolution", "promised_by"):
        item = extraction.get(key) or {}
        if item.get("value") in (None, ""):
            continue
        literal = str(item["value"]) if key == "ticket_id" else None
        out[key] = _check(item, segments, literal=literal, is_date=key == "promised_by")
    for i, item in enumerate(extraction.get("amounts") or []):
        out[f"amounts[{i}]"] = _check(item, segments, literal=_fmt_amount(item.get("value", "")))
    for i, item in enumerate(extraction.get("next_steps") or []):
        if item.get("value"):
            out[f"next_steps[{i}]"] = _check(item, segments)
    return out
