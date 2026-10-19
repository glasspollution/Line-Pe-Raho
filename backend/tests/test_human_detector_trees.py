"""Every hold announcement in our mock companies must be rejected the first time it is heard, and
connecting prompts must be recognised (they feed after_connecting_prompt)."""

from __future__ import annotations

import pytest

from app.agent.human_detector import HumanDetector, SegmentEvidence, is_connecting_prompt
from app.company.engine import list_companies, load_tree

ANNOUNCEMENTS = [
    (c["id"], a)
    for c in list_companies()
    for n in load_tree(c["id"])["nodes"].values()
    for a in (n.get("hold") or {}).get("announcements", [])
]


@pytest.mark.parametrize("company,text", ANNOUNCEMENTS)
def test_tree_announcements_rejected_first_time(company: str, text: str) -> None:
    det = HumanDetector(load_tree(company).get("known_phrases", []))
    score = det.score(SegmentEvidence(text=text, speech_no_music_before=True))
    assert det.bucket(score) == "reject", (score, det.explain(SegmentEvidence(text=text, speech_no_music_before=True)))


def test_connecting_prompts() -> None:
    assert is_connecting_prompt("Please hold, we are connecting you to our customer care executive.")
    assert is_connecting_prompt("Kripya pratiksha karein, hum aapko hamare pratinidhi se jod rahe hain.")
    assert not is_connecting_prompt("Press 1 for recharge and billing.")
