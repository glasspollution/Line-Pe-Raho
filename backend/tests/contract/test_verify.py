"""Contract: app.postcall.verify (CLAUDE.md §12.5). Task T-303.

Pinned interface:
    verify(extraction: dict, segments: dict[str, dict]) -> dict[str, FieldVerdict]
        segments: {segment_id: {"text": str, "text_itn": str | None, "start": float, "end": float, ...}}
        keys: "ticket_id", "agent_name", "resolution", "promised_by", "amounts[i]", "next_steps[i]"
        FieldVerdict(verified: bool, reason: str)
    Rules: every quote fuzzy-matches (>= 90) its cited segment text or text_itn; IDs and amounts appear
    literally in a cited segment's text_itn; dates parse (ISO); unknown segment ids fail; every non-null
    field in the extraction appears in the output (never silently dropped).
"""

from __future__ import annotations

import copy
import json
from types import ModuleType
from typing import Any

import pytest

from tests.contract.helpers import ROOT, require

pytestmark = pytest.mark.milestone("M3")

GOLDEN = json.loads((ROOT / "eval" / "fixtures" / "golden_summary.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def vf() -> ModuleType:
    return require("app.postcall.verify", "T-303")


def run(vf: ModuleType, extraction: dict[str, Any]) -> dict[str, Any]:
    return vf.verify(extraction, GOLDEN["segments"])


def test_golden_expectations(vf: ModuleType) -> None:
    out = run(vf, GOLDEN["extraction"])
    got = {k: v.verified for k, v in out.items()}
    assert got == GOLDEN["expected_verification"]
    assert all(isinstance(v.reason, str) and v.reason for v in out.values())


def test_ticket_digit_off_by_one_fails(vf: ModuleType) -> None:
    ex = copy.deepcopy(GOLDEN["extraction"])
    ex["ticket_id"]["value"] = "NMB48213378"
    assert run(vf, ex)["ticket_id"].verified is False


def test_amount_not_in_itn_fails(vf: ModuleType) -> None:
    ex = copy.deepcopy(GOLDEN["extraction"])
    ex["amounts"][0]["value"] = 249
    assert run(vf, ex)["amounts[0]"].verified is False


def test_quote_not_in_segment_fails(vf: ModuleType) -> None:
    ex = copy.deepcopy(GOLDEN["extraction"])
    ex["agent_name"]["evidence"][0]["quote"] = "my name is rohan from billing"
    assert run(vf, ex)["agent_name"].verified is False


def test_unparseable_date_fails(vf: ModuleType) -> None:
    ex = copy.deepcopy(GOLDEN["extraction"])
    ex["promised_by"]["value"] = "sometime next week"
    assert run(vf, ex)["promised_by"].verified is False


def test_no_evidence_fails(vf: ModuleType) -> None:
    ex = copy.deepcopy(GOLDEN["extraction"])
    ex["resolution"]["evidence"] = []
    assert run(vf, ex)["resolution"].verified is False


def test_null_fields_skipped_but_nothing_dropped(vf: ModuleType) -> None:
    ex = copy.deepcopy(GOLDEN["extraction"])
    ex["agent_name"] = {"value": None, "evidence": []}
    out = run(vf, ex)
    assert "agent_name" not in out or out["agent_name"].verified is False
    assert {"ticket_id", "resolution", "promised_by", "amounts[0]", "next_steps[0]"} <= set(out)
