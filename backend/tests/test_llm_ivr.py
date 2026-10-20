"""LLM fallback for IVR menus the rules can't decide (N-2): async, non-blocking, confidence-gated, journaled."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.llm.schemas import CallPlan, IVRDecisionOut
from app.sim.harness import SimConfig, run_sim_call
from eval import journal_check

PLAN = CallPlan(company_id="nimbus_telecom", goal="port my number to another operator",
                goal_keywords=["port", "mnp"], needs_human=False,
                spoken_intent={"en-IN": "port my number"}, user_language="en-IN")


class MenuLLM:
    """Fake Kimi: prefers 'data', then 'speak'; returns the given confidence."""

    def __init__(self, confidence: float = 0.9) -> None:
        self.confidence = confidence
        self.calls: list[dict[str, Any]] = []

    async def complete_json(self, system: str, user: str, model: type[Any], realtime: bool = True, **_: Any) -> Any:
        assert model is IVRDecisionOut and realtime is True and "phone menu" in system
        req = json.loads(user)
        self.calls.append(req)
        for word in ("data", "speak"):
            for o in req["options"]:
                if word in o["label"]:
                    return IVRDecisionOut(action="press", digit=o["digit"], reason=f"{word} is closest",
                                          confidence=self.confidence)
        return IVRDecisionOut(action="repeat", reason="unclear", confidence=0.2)


def decisions(events: list[dict[str, Any]], method: str) -> list[dict[str, Any]]:
    return [e for e in events if e["type"] == "decision" and e.get("kind") == "menu_press" and e.get("method") == method]


async def test_llm_routes_when_rules_cannot(tmp_path: Path) -> None:
    llm = MenuLLM()
    sim = await run_sim_call(SimConfig("nimbus_telecom", plan=PLAN, ivr_llm=llm, out_dir=tmp_path))
    assert journal_check.check_journal(sim.events, complete=True) == []
    llm_presses = [d for d in decisions(sim.events, "llm") if d.get("digit")]
    assert [d["digit"] for d in llm_presses] == ["2", "0"]
    assert all("latency_ms" in d for d in llm_presses)
    assert sim.result.path == ["1", "2", "0"]  # English by rule, then two LLM presses
    assert llm.calls[0]["keys_pressed_so_far"] == ["1"] and llm.calls[0]["goal"] == PLAN.goal
    assert [e for e in sim.events if e["type"] == "state_changed"][-1]["to"] == "DONE"


async def test_low_confidence_never_presses(tmp_path: Path) -> None:
    sim = await run_sim_call(SimConfig("nimbus_telecom", plan=PLAN, ivr_llm=MenuLLM(confidence=0.3),
                                       out_dir=tmp_path, user_join_delay_s=None))
    assert journal_check.check_journal(sim.events, complete=True) == []
    llm = decisions(sim.events, "llm")
    assert llm and all("digit" not in d for d in llm) and {d["action"] for d in llm} == {"repeat"}
    assert any(d.get("method") == "fallback" for d in sim.events if d["type"] == "decision")


async def test_without_llm_rules_only_path_unchanged(tmp_path: Path) -> None:
    sim = await run_sim_call(SimConfig("nimbus_telecom", out_dir=tmp_path))
    assert not decisions(sim.events, "llm") and sim.result.path == ["1", "1", "2"]
