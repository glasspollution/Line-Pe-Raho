"""Live-human scoring for callee speech segments (CLAUDE.md §10.2). Contract: test_human_detector.py.

Score in [0, 1] from weighted evidence (weights in config). Buckets: >= human_confirm_threshold confirm,
>= human_probe_threshold probe, else reject. The liveness probe (orchestrator) is the decisive signal
for the middle bucket.

Refinements over the spec text:
- Novelty uses whole-string fuzzy ratio (not token-set), so a bare "hello?" is not mistaken for a
  repeat of "hello, your call is important to us...".
- The announcement lexicon tolerates small variations ("sabhi pratinidhi abhi vyast", "stay on line").
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from rapidfuzz import fuzz

from app.config import Settings, get_settings

GREETING_RE = re.compile(
    r"\bhello\b|\bnamaste\b|\bgood (?:morning|afternoon|evening)\b|\bmain\b.*\bbol (?:raha|rahi) (?:hoon|hun)\b"
    r"|\bthis is \w+|\bhow (?:may|can) i help\b|\bkaise madad\b"
)
ANNOUNCEMENT_RE = re.compile(
    r"call is (?:very )?important|aapki call (?:humare|hamare) liye|stay on (?:the )?line"
    r"|line par bane rah|executives? (?:are|is) (?:all )?(?:currently )?busy|officers are busy"
    r"|sabhi pratinidhi (?:abhi )?vyast|pratinidhi (?:abhi )?vyast hain|will be answered shortly"
    r"|kripya pratiksha karein|please hold\b"
)
CONNECTING_RE = re.compile(r"connecting you|connect you to|transferring|transfer (?:your call|ki ja rahi)"
                           r"|jod rahe|connect kar rahe")
_CLEAN_RE = re.compile(r"[^\w\s]")


def _norm(text: str) -> str:
    return " ".join(_CLEAN_RE.sub(" ", text.lower()).split())


def is_connecting_prompt(text: str) -> bool:
    """IVR text like 'please hold, we are connecting you to an executive' (sets after_connecting_prompt)."""
    return bool(CONNECTING_RE.search(_norm(text)))


@dataclass(frozen=True)
class SegmentEvidence:
    text: str
    music_under: bool = False
    speech_no_music_before: bool = False
    after_connecting_prompt: bool = False
    silence_after_s: float = 0.0


class HumanDetector:
    def __init__(self, known_phrases: list[str] | None = None, settings: Settings | None = None) -> None:
        self.s = settings or get_settings()
        self._heard: list[str] = [_norm(p) for p in (known_phrases or []) if p.strip()]

    def remember(self, text: str) -> None:
        t = _norm(text)
        if t and t not in self._heard:
            self._heard.append(t)

    def is_novel(self, text: str) -> bool:
        t = _norm(text)
        return not any(fuzz.ratio(t, h) >= self.s.human_novelty_fuzzy_min for h in self._heard)

    def explain(self, ev: SegmentEvidence) -> tuple[float, list[str]]:
        s = self.s
        t = _norm(ev.text)
        parts: list[tuple[str, float]] = []
        if GREETING_RE.search(t):
            parts.append(("greeting", s.human_w_greeting))
        if self.is_novel(t):
            parts.append(("novel", s.human_w_novel))
        if ev.silence_after_s >= s.human_turn_silence_s:
            parts.append((f"turn_taking_{s.human_turn_silence_s:g}s", s.human_w_turn_taking))
        if ev.speech_no_music_before:
            parts.append(("no_music_before", s.human_w_no_music_before))
        if ev.after_connecting_prompt:
            parts.append(("after_connecting_prompt", s.human_w_after_connecting))
        if ANNOUNCEMENT_RE.search(t):
            parts.append(("announcement_lexicon", s.human_w_announcement))
        if ev.music_under:
            parts.append(("music_under", s.human_w_music_under))
        score = max(0.0, min(1.0, sum(w for _, w in parts)))
        return score, [f"{name}:{w:+.2f}" for name, w in parts]

    def score(self, ev: SegmentEvidence) -> float:
        return self.explain(ev)[0]

    def bucket(self, score: float) -> str:
        if score >= self.s.human_confirm_threshold:
            return "confirm"
        return "probe" if score >= self.s.human_probe_threshold else "reject"
