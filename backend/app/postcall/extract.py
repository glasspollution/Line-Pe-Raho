"""Post-call extraction (CLAUDE.md §12.4): segments -> Extraction (every value cites a segment + quote).

`extract_llm` asks the LLM (prompt llm/prompts/extract.md, post-call budget). `extract_rules` is a
deterministic extractor for the patterns our mock companies use; it is the offline path and the fallback
when the LLM fails. Both outputs go through the same verifier, so neither is trusted blindly.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
from typing import Any, Protocol

from app.llm.schemas import Evidence, ExtractedAmount, ExtractedField, Extraction

PROMPTS = Path(__file__).resolve().parents[1] / "llm" / "prompts"
TICKET_RE = re.compile(r"\b([A-Z]{2,4}\d{6,10})\b")
AGENT_RE = re.compile(r"\b(?:this is|my name is|main)\s+([a-z]+)\s+(?:from|bol)", re.I)
RESOLUTION_RE = re.compile(r"(?:raised a complaint|refund will|will be (?:credited|reversed|restored)|"
                           r"restore the supply|reversed to your account)[^.]*", re.I)
MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
     "November", "December"], 1)}
_ORD = {w: i for i, w in enumerate(
    ["", "first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth", "tenth", "eleventh",
     "twelfth", "thirteenth", "fourteenth", "fifteenth", "sixteenth", "seventeenth", "eighteenth", "nineteenth",
     "twentieth"])}
_ORD.update({"twenty " + w: 20 + i for w, i in list(_ORD.items()) if 0 < i < 10})
_ORD.update({"thirtieth": 30, "thirty first": 31})
DATE_RE = re.compile(
    r"\b(?:the\s+)?(?P<day>\d{1,2}(?:st|nd|rd|th)?|" + "|".join(sorted((k for k in _ORD if k), key=len, reverse=True))
    + r")\s+(?:of\s+)?(?P<month>" + "|".join(MONTHS) + r")\b", re.I)
AMOUNT_RE = re.compile(r"₹\s?(\d+(?:\.\d+)?)")


class JsonLLM(Protocol):
    async def complete_json(self, system: str, user: str, model: type[Any], realtime: bool = True,
                            effort: str | None = None, max_tokens: int | None = None,
                            timeout_s: float | None = None) -> Any: ...


def parse_spoken_date(text: str, call_date: dt.date) -> tuple[str, str] | None:
    """('the fifth of november', 2026-10-21) -> ('2026-11-05', 'fifth of november')."""
    m = DATE_RE.search(text)
    if not m:
        return None
    raw_day = m.group("day").lower()
    day = int(re.sub(r"\D", "", raw_day)) if raw_day[0].isdigit() else _ORD[raw_day]
    month = MONTHS[m.group("month").lower()]
    year = call_date.year + (1 if month < call_date.month else 0)
    try:
        return dt.date(year, month, day).isoformat(), m.group(0)
    except ValueError:
        return None


def _quote(text: str, needle: str, words: int = 6) -> str:
    """A short verbatim phrase from `text` around `needle` (for evidence)."""
    i = text.lower().find(needle.lower())
    if i < 0:
        return " ".join(text.split()[:words])
    before = text[:i].split()[-2:]
    after = text[i + len(needle):].split()[:2]
    return " ".join(before + needle.split() + after)


def extract_rules(segments: list[dict[str, Any]], call_date: dt.date) -> Extraction:
    ex = Extraction()
    company = [s for s in segments if s["speaker"] == "company"]
    for s in company:
        t = s["text_itn"] or s["text"]
        if ex.ticket_id.value is None and (m := TICKET_RE.search(t)):
            ex.ticket_id = ExtractedField(value=m.group(1), evidence=[Evidence(segment_id=s["id"], quote=_quote(t, m.group(1)))])
        if ex.agent_name.value is None and (m := AGENT_RE.search(t)):
            ex.agent_name = ExtractedField(value=m.group(1).capitalize(),
                                           evidence=[Evidence(segment_id=s["id"], quote=_quote(t, m.group(0), 4))])
        if ex.resolution.value is None and (m := RESOLUTION_RE.search(t)):
            phrase = " ".join(m.group(0).split()[:12])
            ex.resolution = ExtractedField(value=phrase[0].upper() + phrase[1:],
                                           evidence=[Evidence(segment_id=s["id"], quote=phrase)])
        if ex.promised_by.value is None and (d := parse_spoken_date(t, call_date)):
            ex.promised_by = ExtractedField(value=d[0], evidence=[Evidence(segment_id=s["id"], quote=_quote(t, d[1], 4))])
        for m in AMOUNT_RE.finditer(t):
            ex.amounts.append(ExtractedAmount(value=float(m.group(1)),
                                              evidence=[Evidence(segment_id=s["id"], quote=_quote(t, m.group(0), 4))]))
    if ex.ticket_id.value:
        ex.outcome = "ticket_raised"
        step = f"Keep complaint number {ex.ticket_id.value} for follow-up"
        ex.next_steps.append(ExtractedField(value=step, evidence=ex.ticket_id.evidence))
        if ex.promised_by.value:
            ex.next_steps.append(ExtractedField(value=f"Expect the resolution by {ex.promised_by.value}",
                                                evidence=ex.promised_by.evidence))
    return ex


async def extract_llm(segments: list[dict[str, Any]], call_date: dt.date, llm: JsonLLM) -> Extraction:
    system = (PROMPTS / "extract.md").read_text(encoding="utf-8").replace("{call_date}", call_date.isoformat())
    user = json.dumps({"segments": [{"id": s["id"], "speaker": s["speaker"], "text": s["text_itn"] or s["text"]}
                                    for s in segments]}, ensure_ascii=False)
    out: Extraction = await llm.complete_json(system, user, Extraction, realtime=False)
    return out
