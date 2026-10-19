"""Contract: app.agent.human_detector (CLAUDE.md §10.2). Task T-204.

Pinned interface:
    SegmentEvidence(text: str, music_under: bool = False, speech_no_music_before: bool = False,
                    after_connecting_prompt: bool = False, silence_after_s: float = 0.0)
    HumanDetector(known_phrases: list[str] | None = None)
        .remember(text: str) -> None      # a prompt/announcement heard earlier in this call
        .score(ev: SegmentEvidence) -> float   # clamped to [0, 1]
Thresholds (confirm 0.85, probe 0.5) come from app.config, but the table below uses the spec defaults.
"""

from __future__ import annotations

from types import ModuleType
from typing import Any

import pytest
import yaml

from tests.contract.helpers import FIXTURES, require

pytestmark = pytest.mark.milestone("M2")

CASES: list[dict[str, Any]] = yaml.safe_load(
    (FIXTURES / "human_detector_cases.yaml").read_text(encoding="utf-8"))["cases"]
EVIDENCE_KEYS = ("music_under", "speech_no_music_before", "after_connecting_prompt", "silence_after_s")


@pytest.fixture(scope="module")
def hd() -> ModuleType:
    return require("app.agent.human_detector", "T-204")


def bucket(score: float) -> str:
    return "confirm" if score >= 0.85 else "probe" if score >= 0.5 else "reject"


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_scoring_table(hd: ModuleType, case: dict[str, Any]) -> None:
    det = hd.HumanDetector(known_phrases=case.get("known", []))
    for t in case.get("remember", []):
        det.remember(t)
    ev = hd.SegmentEvidence(text=case["text"], **{k: case[k] for k in EVIDENCE_KEYS if k in case})
    s = det.score(ev)
    assert 0.0 <= s <= 1.0
    assert bucket(s) == case["bucket"], f"score={s:.2f}"


def test_remembering_lowers_score(hd: ModuleType) -> None:
    text = "thank you for your patience an executive will be with you shortly"
    fresh = hd.HumanDetector().score(hd.SegmentEvidence(text=text))
    det = hd.HumanDetector()
    det.remember(text)
    assert det.score(hd.SegmentEvidence(text=text)) < fresh
