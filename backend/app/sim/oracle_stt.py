"""Oracle STT: a stand-in for Prisma realtime that 'hears' exactly what the far end said.

Utterance sources (callables returning the current list) are the IVR engine's ground-truth log and the
scripted agent's speech log. A transcript is emitted once line time passes the utterance end plus the
end-of-speech silence (stt_min_silence_ms) plus a simulated latency, mirroring how Prisma finalises a
segment after VAD silence. Prompts cut short by barge-in are truncated proportionally. Output text looks
like Prisma's: lowercase, no punctuation.

Same interface the orchestrator expects from a real STT stream: feed(frame, t) -> list[Transcript].
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.config import Settings, get_settings


@dataclass(frozen=True)
class Transcript:
    segment_id: str
    text: str
    start: float
    end: float
    track: str = "callee"


@dataclass(frozen=True)
class Utterance:
    text: str
    start: float
    end: float


def engine_utterances(log: list[dict[str, Any]]) -> list[Utterance]:
    """Spoken prompts/announcements from an IVREngine log, with barge-in cuts applied."""
    out: list[Utterance] = []
    for i, e in enumerate(log):
        if e["kind"] not in ("say", "announcement") or not e.get("text"):
            continue
        start, end = float(e["t"]), float(e["t"]) + float(e["duration_s"])
        text = e["text"]
        cut = next((c for c in log[i + 1 :] if c["kind"] == "say_cut" and start <= c["t"] < end), None)
        if cut is not None:
            frac = (float(cut["t"]) - start) / max(end - start, 1e-6)
            words = text.split()
            text = " ".join(words[: int(len(words) * frac)])
            end = float(cut["t"])
        if text:
            out.append(Utterance(text, start, end))
    return out


_CLEAN = re.compile(r"[^\w\s]")


def prisma_style(text: str) -> str:
    return " ".join(_CLEAN.sub(" ", text.lower()).split())


class OracleSTT:
    def __init__(self, sources: list[Callable[[], list[Utterance]]], settings: Settings | None = None) -> None:
        self.sources = sources
        s = settings or get_settings()
        self.delay = s.stt_min_silence_ms / 1000 + s.stt_oracle_latency_s
        self._emitted: set[tuple[float, str]] = set()
        self._n = 0

    def feed(self, frame: bytes, t: float) -> list[Transcript]:
        out: list[Transcript] = []
        for src in self.sources:
            for u in src():
                key = (round(u.start, 3), u.text)
                if key in self._emitted or t < u.end + self.delay:
                    continue
                self._emitted.add(key)
                self._n += 1
                out.append(Transcript(f"s{self._n}", prisma_style(u.text), round(u.start, 3), round(u.end, 3)))
        out.sort(key=lambda tr: tr.start)
        return out
