"""Contract: app.agent.menu_parser (CLAUDE.md §9.2). Task T-201.

Pinned interface:
    normalize(text: str) -> str
    parse_segment(text: str, segment_id: str = "s0") -> SegmentParse
        .options: list[MenuOption]          # MenuOption(digit: str, label: str, segment_id: str), unique digits
        .data_prompt: DataPrompt | None     # DataPrompt(field: str, terminator: str | None)
        .voice_prompt: bool
        .repeat_hint: bool
        .needs_llm: bool                    # rules found 0 options but text has press|dabaiye|option|select
    MenuBuffer().add(parse: SegmentParse, t_end: float) ; .is_complete(now: float) -> bool ; .options
        complete when: >= 2.5 s since the last segment that added options, OR first option repeats,
        OR a repeat hint was heard. Never complete with zero options.
Corpus: backend/tests/fixtures/menu_prompts.yaml
"""

from __future__ import annotations

from types import ModuleType
from typing import Any

import pytest
import yaml

from tests.contract.helpers import FIXTURES, require

pytestmark = pytest.mark.milestone("M2")

CORPUS = yaml.safe_load((FIXTURES / "menu_prompts.yaml").read_text(encoding="utf-8"))
CASES: list[dict[str, Any]] = CORPUS["cases"]


@pytest.fixture(scope="module")
def mp() -> ModuleType:
    return require("app.agent.menu_parser", "T-201")


def test_corpus_is_big_enough() -> None:
    assert len(CASES) >= 60
    assert len({c["id"] for c in CASES}) == len(CASES)


@pytest.mark.parametrize("raw,expected", CORPUS["normalize"], ids=[n[0] for n in CORPUS["normalize"]])
def test_normalize(mp: ModuleType, raw: str, expected: str) -> None:
    assert mp.normalize(raw) == expected


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_parse_segment(mp: ModuleType, case: dict[str, Any]) -> None:
    res = mp.parse_segment(case["text"], segment_id=case["id"])
    got = {o.digit: o.label for o in res.options}
    expected: dict[str, list[str]] = {str(k): v for k, v in (case.get("options") or {}).items()}

    if case.get("or_needs_llm") and res.needs_llm and not got:
        return
    if case.get("exact", True):
        assert set(got) == set(expected), f"digits {sorted(got)} != {sorted(expected)}; labels={got}"
    else:
        assert set(expected) <= set(got), f"missing {set(expected) - set(got)}; labels={got}"
    for digit, keywords in expected.items():
        label = mp.normalize(got[digit])
        for kw in keywords:
            assert kw in label, f"option {digit}: {kw!r} not in label {label!r}"
    assert all(o.segment_id == case["id"] for o in res.options)

    dp = case.get("data_prompt")
    if dp is None:
        assert res.data_prompt is None, f"unexpected data prompt {res.data_prompt}"
    else:
        assert res.data_prompt is not None, "data prompt not detected"
        assert (res.data_prompt.field, res.data_prompt.terminator) == (dp["field"], dp["terminator"])

    assert res.voice_prompt is bool(case.get("voice_prompt", False))
    assert res.repeat_hint is bool(case.get("repeat_hint", False))
    if expected:
        assert res.needs_llm is False
    if "needs_llm" in case:
        assert res.needs_llm is case["needs_llm"]


def _buf(mp: ModuleType, *segs: tuple[str, float]) -> Any:
    b = mp.MenuBuffer()
    for i, (text, t) in enumerate(segs):
        b.add(mp.parse_segment(text, segment_id=f"s{i}"), t)
    return b


def test_buffer_complete_after_silence(mp: ModuleType) -> None:
    b = _buf(mp, ("press 1 for billing press 2 for data", 10.0))
    assert not b.is_complete(11.0)
    assert b.is_complete(12.6)


def test_buffer_complete_when_first_option_repeats(mp: ModuleType) -> None:
    b = _buf(mp, ("press 1 for billing press 2 for data", 10.0), ("press 1 for billing", 11.0))
    assert b.is_complete(11.0)
    assert sorted(o.digit for o in b.options) == ["1", "2"]


def test_buffer_complete_on_repeat_hint(mp: ModuleType) -> None:
    assert _buf(mp, ("press 1 for billing press 9 to repeat", 10.0)).is_complete(10.0)


def test_buffer_never_complete_without_options(mp: ModuleType) -> None:
    assert not _buf(mp, ("your call is important to us", 10.0)).is_complete(30.0)


def test_buffer_accumulates_across_segments(mp: ModuleType) -> None:
    b = _buf(mp, ("press 1 for billing", 10.0), ("press 2 for data", 12.0))
    assert not b.is_complete(13.0)
    assert b.is_complete(14.6)
    assert sorted(o.digit for o in b.options) == ["1", "2"]
