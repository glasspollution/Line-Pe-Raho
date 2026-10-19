"""Choose a key for a complete menu (CLAUDE.md §9.3). Rules first; LLM only when rules can't decide.

Rules, in order:
  1. language menu: English if offered, else Hindi
  2. an option whose label fuzzy-matches a goal keyword (token_set_ratio >= decider_fuzzy_min); best score wins
  3. needs_human and an agent-like option (agent / executive / customer care / officer / pratinidhi / baat)
  4. otherwise ask the LLM (if one is wired in; T-007), pressing only at confidence >= decider_llm_min_confidence
  5. otherwise "wait" (the orchestrator presses fallback keys after the same menu is heard 3 times)
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from rapidfuzz import fuzz

from app.agent.menu_parser import MenuOption
from app.config import Settings, get_settings
from app.llm.schemas import CallPlan, IVRDecisionOut

RISKY_RE = re.compile(r"\b(?:block|cancel|deactivate|close|stop|terminate|surrender|band)\b")
_STOP = {"with", "from", "that", "this", "have", "want", "need", "please", "mera", "meri", "chahiye"}
AGENT_RE = re.compile(r"\b(?:agent|executive|customer care|officer|representative|pratinidhi|adhikari|baat)\b")
LlmDecide = Callable[[CallPlan, list[MenuOption], list[str]], IVRDecisionOut]


def _keyword_score(label: str, keyword: str) -> float:
    """Phrase-level match, plus word-level for single-word keywords so plurals/inflections still match
    ("card" ~ "cards" = 89, "refund" ~ "refunds")."""
    score = float(fuzz.token_set_ratio(label, keyword))
    if " " not in keyword:
        score = max([score] + [float(fuzz.ratio(w, keyword)) for w in label.split() if len(w) >= 3])
    return score


@dataclass(frozen=True)
class MenuDecision:
    action: Literal["press", "wait", "repeat"]
    digit: str | None
    reason: str
    confidence: float
    method: Literal["rule", "llm", "fallback"]
    latency_ms: int = 0


class IVRDecider:
    def __init__(self, llm: LlmDecide | None = None, settings: Settings | None = None) -> None:
        self.llm = llm
        self.s = settings or get_settings()

    def decide(self, plan: CallPlan, options: list[MenuOption], path: list[str]) -> MenuDecision:
        t0 = time.perf_counter()
        d = self._decide(plan, options, path)
        return MenuDecision(d.action, d.digit, d.reason, d.confidence, d.method,
                            int((time.perf_counter() - t0) * 1000))

    def _decide(self, plan: CallPlan, options: list[MenuOption], path: list[str]) -> MenuDecision:
        if not options:
            return MenuDecision("wait", None, "no options", 0.0, "fallback")
        labels = {o.digit: o.label.lower() for o in options}
        # 1. language menu
        for lang in ("english", "angrezi", "hindi"):
            hit = next((d for d, lab in labels.items() if lab.split()[:1] == [lang] or lab == lang), None)
            if hit:
                return MenuDecision("press", hit, f"language menu: {lang.split()[0]} option", 1.0, "rule")
        # 2. goal relevance: keyword hits first, goal-word overlap as a tie-breaker; never a destructive
        #    option (block/cancel/...) unless the goal asks for that action
        goal_words = [w for w in re.findall(r"[a-z]+", plan.goal.lower()) if len(w) >= 4 and w not in _STOP]
        best: tuple[float, float, str, str, str] | None = None  # (relevance, top score, digit, label, keyword)
        for d, lab in labels.items():
            risky = RISKY_RE.search(lab)
            if risky and risky.group(0) not in plan.goal.lower():
                continue
            hits = [(_keyword_score(lab, kw.lower()), kw) for kw in plan.goal_keywords]
            hits = [h for h in hits if h[0] >= self.s.decider_fuzzy_min]
            if not hits:
                continue
            overlap = sum(1 for w in goal_words if any(fuzz.ratio(w, lw) >= 85 for lw in lab.split()))
            relevance = len(hits) + 0.5 * overlap
            top = max(hits)
            if best is None or (relevance, top[0]) > (best[0], best[1]):
                best = (relevance, top[0], d, lab, top[1])
        if best:
            _, score, d, lab, kw = best
            return MenuDecision("press", d, f"'{lab}' matches goal keyword '{kw}' (score {score:.0f})",
                                round(score / 100, 2), "rule")
        # 3. human agent
        if plan.needs_human:
            hit = next((d for d, lab in labels.items() if AGENT_RE.search(lab)), None)
            if hit:
                return MenuDecision("press", hit, f"'{labels[hit]}' reaches a human agent", 0.9, "rule")
        # 4. LLM
        if self.llm is not None:
            out = self.llm(plan, options, path)
            if out.action == "press" and out.digit in labels and out.confidence >= self.s.decider_llm_min_confidence:
                return MenuDecision("press", out.digit, out.reason, out.confidence, "llm")
            return MenuDecision("repeat" if out.action == "press" else out.action, None, out.reason,
                                out.confidence, "llm")
        return MenuDecision("wait", None, "no rule matched and no LLM configured", 0.0, "fallback")
