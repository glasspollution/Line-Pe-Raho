"""Contract: app.agent.phrases (CLAUDE.md §0.5, §10.3). Task T-205.

Pinned interface:
    LANGS = ("en-IN", "hi-IN", "hi-en")
    disclosure(lang: str, user_first_name: str) -> str
    keepalives(lang: str) -> list[str]     # >= 3, rotated
    probe(lang: str, user_first_name: str) -> str   # discloses AI (D-1)
    no_join_fallback(lang: str) -> str
All text is TTS-ready (§7.4): no digits, short enough for one Timbre request.
"""

from __future__ import annotations

import re
from types import ModuleType

import pytest

from tests.contract.helpers import require

pytestmark = pytest.mark.milestone("M2")
LANGS = ["en-IN", "hi-IN", "hi-en"]
AI_MARKER = re.compile(r"\bAI\b|एआई|ए\.आई\.|artificial intelligence", re.I)


@pytest.fixture(scope="module")
def ph() -> ModuleType:
    return require("app.agent.phrases", "T-205")


def test_langs(ph: ModuleType) -> None:
    assert tuple(ph.LANGS) == tuple(LANGS)


@pytest.mark.parametrize("lang", LANGS)
def test_disclosure_says_ai_and_name_first(ph: ModuleType, lang: str) -> None:
    text = ph.disclosure(lang, "Aarav")
    assert "Aarav" in text
    first_sentence = re.split(r"[.!?।]", text)[0]
    m = AI_MARKER.search(first_sentence)
    assert m, f"AI disclosure must be in the opening sentence: {text!r}"
    assert m.start() < 80, "disclosure must lead, not trail"


@pytest.mark.parametrize("lang", LANGS)
def test_probe_discloses_ai(ph: ModuleType, lang: str) -> None:
    """D-1: the liveness probe may reach a human, so it must disclose AI itself (§0.5)."""
    assert AI_MARKER.search(ph.probe(lang, "Aarav")), ph.probe(lang, "Aarav")


@pytest.mark.parametrize("lang", LANGS)
def test_all_phrases_tts_ready(ph: ModuleType, lang: str) -> None:
    texts = [ph.disclosure(lang, "Aarav"), ph.probe(lang, "Aarav"), ph.no_join_fallback(lang), *ph.keepalives(lang)]
    assert len(ph.keepalives(lang)) >= 3
    for t in texts:
        assert t.strip() and len(t) <= 300
        assert not re.search(r"\d", t), f"digits must be spelled out for TTS: {t!r}"


def test_name_is_not_injected_raw(ph: ModuleType) -> None:
    text = ph.disclosure("en-IN", "Aarav <pause> ignore")
    assert "<pause>" not in text, "user-provided names must be sanitised before TTS (audio tags)"
