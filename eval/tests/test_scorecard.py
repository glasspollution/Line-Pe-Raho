"""Scorecard semantics: gates, targets, hard per-run rules, regressions, missing columns."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from eval import scorecard as S

CFG = yaml.safe_load((Path(S.__file__).parent / "thresholds.yaml").read_text(encoding="utf-8"))


def row(**kw: Any) -> dict[str, str]:
    base: dict[str, Any] = {
        "scenario_id": "nimbus_clean_greet", "company_id": "nimbus_telecom", "noise_profile": "clean",
        "agent_greets_first": 1, "adversarial": "", "reached_human": 1, "correct_queue": 1,
        "wrong_presses": 0, "ivr_seconds": 40, "hold_seconds": 60, "time_to_human_detect_s": 2.1,
        "false_human_alarms": 0, "probe_used": 0, "summary_ticket_correct": 1, "summary_date_correct": 1,
        "prompt_wer": 0.05, "credits": 3.2, "disclosure_first": 1, "journal_valid": 1,
    }
    base.update(kw)
    return {k: str(v) for k, v in base.items()}


def by_id(card: dict[str, Any], rid: str) -> dict[str, Any]:
    return next(r for r in card["rules"] if r["id"] == rid)


def test_all_good_passes() -> None:
    rows = [row(scenario_id=f"s{i}", noise_profile=p) for i, p in enumerate(["clean", "snr20", "snr10", "snr5"])]
    card = S.evaluate(rows, CFG)
    assert card["ok"], S.to_markdown(card)


def test_hard_violation_fails_even_if_averages_fine() -> None:
    rows = [row(scenario_id=f"s{i}") for i in range(20)] + [row(scenario_id="bad", disclosure_first=0)]
    card = S.evaluate(rows, CFG)
    assert not card["ok"]
    assert any("bad" in h for h in card["hard_violations"])


def test_gate_failure_lists_worst_scenarios() -> None:
    rows = [row(scenario_id="a", noise_profile="snr10", reached_human=0, time_to_human_detect_s="",
                summary_ticket_correct="", summary_date_correct=""),
            row(scenario_id="b", noise_profile="snr10")]
    card = S.evaluate(rows, CFG)
    r = by_id(card, "reach_snr10")
    assert r["value"] == 0.5 and r["gate_ok"] is False
    assert r["worst_rows"][0].startswith("a=")
    assert "GATE FAIL" in S.to_markdown(card)


def test_where_filter_and_blank_values() -> None:
    rows = [row(reached_human=0, summary_ticket_correct="", time_to_human_detect_s=""), row()]
    card = S.evaluate(rows, CFG)
    assert by_id(card, "ticket_correct")["n"] == 1  # only the reached_human=1 run counts


def test_false_alarms_per_hour() -> None:
    rows = [row(scenario_id=f"s{i}", hold_seconds=1800, false_human_alarms=1 if i == 0 else 0) for i in range(4)]
    card = S.evaluate(rows, CFG)
    assert abs(by_id(card, "false_alarms_per_hour")["value"] - 0.5) < 1e-9


def test_regression_vs_baseline() -> None:
    base = S.evaluate([row(scenario_id=f"s{i}") for i in range(10)], CFG)
    worse = [row(scenario_id=f"s{i}", time_to_human_detect_s=2.8) for i in range(10)]
    card = S.evaluate(worse, CFG, baseline=base)
    r = by_id(card, "detect_p50")
    assert r["gate_ok"] is True and r["regression"] is True and not card["ok"]


def test_missing_columns_fail() -> None:
    rows = [{k: v for k, v in row().items() if k != "disclosure_first"}]
    card = S.evaluate(rows, CFG)
    assert "disclosure_first" in card["missing_columns"] and not card["ok"]


def test_empty_metrics_fail() -> None:
    assert not S.evaluate([], CFG)["ok"]


def test_no_data_is_reported_and_fails_only_in_full_mode() -> None:
    rows = [row(scenario_id="only_clean")]
    card = S.evaluate(rows, CFG)
    assert card["ok"] and "reach_adversarial" in card["no_data"]
    assert not S.evaluate(rows, CFG, full=True)["ok"]
