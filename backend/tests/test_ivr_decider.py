"""IVRDecider rules (deterministic half of T-202)."""

from __future__ import annotations

from app.agent.ivr_decider import IVRDecider
from app.agent.menu_parser import parse_segment
from app.llm.schemas import CallPlan, IVRDecisionOut

PLAN = CallPlan(company_id="x", goal="refund for a failed card transaction",
                goal_keywords=["card", "transaction", "refund"], needs_human=True)


def decide(text: str, plan: CallPlan = PLAN, llm=None):  # type: ignore[no-untyped-def]
    return IVRDecider(llm=llm).decide(plan, parse_segment(text).options, [])


def test_language_menu_prefers_english() -> None:
    d = decide("sahyog bank mein aapka swagat hai hindi ke liye 1 dabaiye for english press 2")
    assert (d.action, d.digit, d.method) == ("press", "2", "rule")


def test_hindi_when_no_english() -> None:
    assert decide("hindi ke liye 1 dabaiye marathi ke liye 2 dabaiye").digit == "1"


def test_plural_keyword_match_beats_agent_option() -> None:
    d = decide("for credit and debit cards press 1 for loans press 3 to speak to a banking officer press 0")
    assert d.digit == "1" and "card" in d.reason


def test_goal_keyword_beats_agent_option_golden_menu() -> None:
    plan = CallPlan(company_id="nimbus_telecom", goal="refund", goal_keywords=["recharge", "refund"])
    d = decide("for duplicate deduction or refund press two to speak to our customer care executive press zero", plan)
    assert d.digit == "2"


def test_agent_option_when_no_keyword() -> None:
    plan = CallPlan(company_id="x", goal="talk", goal_keywords=["mortgage"], needs_human=True)
    assert decide("for balance press 1 to speak to our customer care executive press 0", plan).digit == "0"


def test_wait_without_rules_or_llm() -> None:
    plan = CallPlan(company_id="x", goal="?", goal_keywords=["mortgage"], needs_human=False)
    d = decide("for balance press 1 for statements press 2", plan)
    assert d.action == "wait" and d.method == "fallback"


def test_llm_used_only_when_rules_fail_and_confidence_gated() -> None:
    plan = CallPlan(company_id="x", goal="?", goal_keywords=["mortgage"], needs_human=False)
    hi = lambda p, o, path: IVRDecisionOut(action="press", digit="2", reason="statements", confidence=0.8)  # noqa: E731
    lo = lambda p, o, path: IVRDecisionOut(action="press", digit="2", reason="unsure", confidence=0.3)  # noqa: E731
    assert decide("for balance press 1 for statements press 2", plan, hi).digit == "2"
    assert decide("for balance press 1 for statements press 2", plan, lo).action == "repeat"


def test_goal_overlap_breaks_keyword_ties() -> None:
    d = decide("to block your card press 1 for a disputed or failed transaction press 2 to repeat these options press 9")
    assert d.digit == "2"


def test_destructive_option_needs_explicit_goal() -> None:
    plan = CallPlan(company_id="x", goal="card problem", goal_keywords=["card"], needs_human=True)
    d = decide("to block your card press 1 to speak to a banking officer press 0", plan)
    assert d.digit == "0"  # never 'block your card' unless asked
    plan_block = CallPlan(company_id="x", goal="block my lost card", goal_keywords=["card", "block"])
    assert decide("to block your card press 1 to speak to a banking officer press 0", plan_block).digit == "1"
