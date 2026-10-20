"""CallPlan builder (N-1): rules on Hinglish/English/Hindi problems, LLM path with a fake LLM, sanitising."""

from __future__ import annotations

from typing import Any

import pytest

from app.agent.ivr_decider import IVRDecider
from app.agent.menu_parser import parse_segment
from app.agent.planner import build_plan, detect_language, rule_plan
from app.llm.schemas import CallPlan
from app.sim.harness import DEMO_PROFILE

CASES = [
    ("Nimbus ne mera recharge do baar kaat liya, refund chahiye", "nimbus_telecom", {"recharge", "refund"}, "hi-en"),
    ("My recharge was deducted twice, I want a refund", "nimbus_telecom", {"recharge", "refund"}, "en-IN"),
    ("internet bahut slow hai, data kaam nahi kar raha", "nimbus_telecom", {"data"}, "hi-en"),
    ("mera bill zyada aaya hai", "nimbus_telecom", {"billing"}, "hi-en"),
    ("My card transaction failed but money was debited", "sahyog_bank", {"card", "transaction"}, "en-IN"),
    ("card se galat transaction hua, dispute karna hai", "sahyog_bank", {"card", "dispute"}, "hi-en"),
    ("Please block my lost card", "sahyog_bank", {"block", "card"}, "en-IN"),
    ("hamare area mein bijli nahi aa rahi subah se", "prakash_bijli_board", {"power cut"}, "hi-en"),
    ("There is a power cut in my area since morning", "prakash_bijli_board", {"power cut"}, "en-IN"),
    ("बिजली नहीं आ रही है", "prakash_bijli_board", {"complaint"}, "hi-IN"),
]


@pytest.mark.parametrize("text,company,must,lang", CASES, ids=[c[0][:30] for c in CASES])
def test_rule_plans(text: str, company: str, must: set[str], lang: str) -> None:
    plan = rule_plan(text, company, DEMO_PROFILE)
    assert must <= set(plan.goal_keywords), plan.goal_keywords
    assert plan.user_language == lang and plan.company_id == company
    assert all(not any(ch.isdigit() for ch in v) for v in plan.spoken_intent.values())


def test_rule_plan_never_adds_destructive_keywords_unasked() -> None:
    plan = rule_plan("my card payment failed", "sahyog_bank", DEMO_PROFILE)
    assert "block" not in plan.goal_keywords


def test_profile_fields_come_from_the_tree() -> None:
    assert rule_plan("bijli nahi hai", "prakash_bijli_board", DEMO_PROFILE).profile_fields_needed == ["account_id"]
    assert rule_plan("recharge issue", "nimbus_telecom", DEMO_PROFILE).profile_fields_needed == ["registered_mobile"]


def test_rule_plan_drives_the_decider_through_nimbus() -> None:
    plan = rule_plan("Nimbus ne mera recharge do baar kaat liya, refund chahiye", "nimbus_telecom", DEMO_PROFILE)
    d = IVRDecider()
    main = parse_segment("press 1 for recharge and billing press 2 for data issues press 3 for new connection").options
    refund = parse_segment("for duplicate deduction or refund press 2 to speak to our customer care executive press 0").options
    assert d.decide(plan, main, []).digit == "1"
    assert d.decide(plan, refund, ["1"]).digit == "2"


class FakeLLM:
    def __init__(self, out: Any = None, fail: bool = False) -> None:
        self.out, self.fail, self.calls = out, fail, 0

    async def complete_json(self, system: str, user: str, model: type[Any], realtime: bool = True,
                            effort: str | None = None, max_tokens: int | None = None,
                            timeout_s: float | None = None) -> Any:
        self.calls += 1
        assert "Output JSON only" in system and effort == "none" and "menu_wording" in user
        if self.fail:
            raise RuntimeError("boom")
        return self.out


async def test_llm_plan_is_sanitised() -> None:
    raw = CallPlan(company_id="WRONG", goal="refund for duplicate recharge",
                   goal_keywords=["Recharge", "refund", "cancel subscription"],
                   spoken_intent={"en-IN": "refund of 2 recharges"},
                   profile_fields_needed=["registered_mobile", "aadhaar"], user_language="en-IN")
    plan, method = await build_plan("Nimbus ne mera recharge do baar kaat liya, refund chahiye", "nimbus_telecom",
                                    DEMO_PROFILE, FakeLLM(raw))
    assert method == "llm" and plan.company_id == "nimbus_telecom"
    assert plan.goal_keywords == ["recharge", "refund"]  # lowercased, destructive one dropped
    assert plan.profile_fields_needed == ["registered_mobile"]  # only fields we actually have
    assert not any(ch.isdigit() for v in plan.spoken_intent.values() for ch in v)  # digit intent replaced
    assert plan.user_language == "hi-en"  # detected from the text, not trusted from the LLM


async def test_llm_failure_falls_back_to_rules() -> None:
    llm = FakeLLM(fail=True)
    plan, method = await build_plan("recharge do baar kat gaya", "nimbus_telecom", DEMO_PROFILE, llm)
    assert method == "rule" and llm.calls == 1 and "recharge" in plan.goal_keywords


def test_language_detection() -> None:
    assert detect_language("मेरा रिचार्ज") == "hi-IN"
    assert detect_language("mera recharge kat gaya hai") == "hi-en"
    assert detect_language("my recharge failed") == "en-IN"
