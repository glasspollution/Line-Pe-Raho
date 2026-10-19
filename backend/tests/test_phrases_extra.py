"""Phrases beyond the contract: voice-gender agreement, name sanitising, cache lists."""

from __future__ import annotations

import pytest

from app.agent import phrases


def test_gender_follows_voice() -> None:
    assert "kar rahi hoon" in phrases.disclosure("hi-en", "Aarav", gender="f")
    assert "kar raha hoon" in phrases.disclosure("hi-en", "Aarav", gender="m")
    assert "कर रही हूँ" in phrases.probe("hi-IN", "Aarav", gender="f")
    assert phrases.agent_gender() == "f"  # default voice Shivani


@pytest.mark.parametrize("raw,clean", [("Aarav", "Aarav"), ("  Priya  Sharma ", "Priya Sharma"),
                                       ("<break time='2s'/>Ravi", "Ravi"), ("R2D2", "R D"), ("", "user")])
def test_clean_name(raw: str, clean: str) -> None:
    assert phrases.clean_name(raw) == clean


def test_cache_lists_and_unknown_lang() -> None:
    assert len(phrases.fixed_phrases("en-IN")) == 4
    assert all("Aarav" in p for p in phrases.per_user_phrases("hi-en", "Aarav"))
    with pytest.raises(ValueError):
        phrases.disclosure("ta-IN", "Aarav")
