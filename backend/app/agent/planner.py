"""CallPlan builder (CLAUDE.md §9.1): the user's problem -> a CallPlan.

LLM path first (prompt llm/prompts/plan.md, output validated as CallPlan, then sanity-checked against
the company and profile); on any failure or with no LLM, a rule-built plan from a Hinglish/English/Hindi
topic lexicon. Both paths return (plan, method) so the caller can journal how the plan was made.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal, Protocol

from app.agent.ivr_decider import RISKY_RE
from app.company.engine import load_tree
from app.config import get_settings
from app.llm.schemas import CallPlan

PROMPTS = Path(__file__).resolve().parents[1] / "llm" / "prompts"
Method = Literal["llm", "rule"]

# topic -> trigger phrases (lowercase, Latin; Devanagari is handled by language detection only)
TOPICS: dict[str, list[str]] = {
    "recharge": ["recharge", "rechaarge", "top up", "topup", "pack"],
    "refund": ["refund", "paisa wapas", "paise wapas", "money back", "return my money", "wapas chahiye"],
    "billing": ["bill", "billing", "charged", "overcharged", "zyada paisa"],
    "duplicate": ["do baar", "double", "twice", "duplicate", "dobara kat", "two times"],
    "data": ["data", "internet", "net nahi", "network", "slow speed"],
    "card": ["card", "debit card", "credit card", "atm"],
    "transaction": ["transaction", "payment failed", "payment fail", "txn", "debit hua", "paisa kat"],
    "dispute": ["dispute", "fraud", "unauthorised", "unauthorized", "galat transaction"],
    "power cut": ["bijli", "light nahi", "light chali", "power cut", "power outage", "current nahi", "outage"],
    "connection": ["new connection", "naya connection", "naye connection"],
    "loan": ["loan", "emi"],
    "balance": ["balance"],
    "complaint": ["complaint", "shikayat", "problem", "issue", "samasya"],
}
_DEDUCTION = ["kaat liya", "kat gaya", "kaata", "kat liya", "deducted", "cut from", "kat gaye"]
_HINGLISH = {"ne", "mera", "meri", "mere", "hai", "hain", "chahiye", "kar", "karo", "nahi", "ko", "ka", "ki",
             "liya", "gaya", "gayi", "baar", "aur", "se", "mujhe", "kya", "bhi", "raha", "rahi", "ho"}
_DEVANAGARI = re.compile(r"[ऀ-ॿ]")


class JsonLLM(Protocol):
    async def complete_json(self, system: str, user: str, model: type[Any], realtime: bool = True,
                            effort: str | None = None, max_tokens: int | None = None,
                            timeout_s: float | None = None) -> Any: ...


def detect_language(text: str) -> Literal["en-IN", "hi-IN", "hi-en"]:
    if _DEVANAGARI.search(text):
        return "hi-IN"
    words = re.findall(r"[a-z]+", text.lower())
    return "hi-en" if sum(w in _HINGLISH for w in words) >= 2 else "en-IN"


def tree_fields(tree: dict[str, Any]) -> list[str]:
    return sorted({n["collect"]["field"] for n in tree["nodes"].values() if "collect" in n})


def menu_wording(tree: dict[str, Any]) -> list[str]:
    return [n["say"] for n in tree["nodes"].values() if n.get("options") and n.get("say")]


def rule_plan(text: str, company_id: str, profile: dict[str, str]) -> CallPlan:
    low = text.lower()
    topics = [t for t, triggers in TOPICS.items() if any(tr in low for tr in triggers)]
    if any(d in low for d in _DEDUCTION) and "refund" not in topics:
        topics.append("refund")
    if not topics:
        topics = ["complaint"]
    if "complaint" not in topics:
        topics.append("complaint")
    keywords = [k for k in topics if k != "duplicate"]
    risky = RISKY_RE.search(low)
    if risky:
        keywords.insert(0, risky.group(0))
    tree = load_tree(company_id)
    fields = [f for f in tree_fields(tree) if f in profile]
    main = [k for k in keywords if k != "complaint"][:3] or ["complaint"]
    intent = " ".join(main) + " issue" if main != ["complaint"] else "I have a complaint"
    if "duplicate" in topics:
        intent = "duplicate " + intent
    summary = " ".join(text.split())
    return CallPlan(
        company_id=company_id,
        goal=summary[:160],
        goal_keywords=keywords,
        needs_human=True,
        spoken_intent={"en-IN": intent, "hi-en": intent},
        profile_fields_needed=fields,
        issue_summary_for_agent=summary[:240],
        user_language=detect_language(text),
    )


def _sanitize(plan: CallPlan, rules: CallPlan, company_id: str, profile: dict[str, str], text: str) -> CallPlan:
    """Keep the LLM's plan but enforce what the LLM must not decide."""
    kws = [k.strip().lower() for k in plan.goal_keywords if k and k.strip()]
    if not RISKY_RE.search(text.lower()):
        kws = [k for k in kws if not RISKY_RE.search(k)]  # never smuggle in destructive actions
    if not kws:
        kws = rules.goal_keywords
    intents = {k: v for k, v in plan.spoken_intent.items() if v and not re.search(r"\d", v)} or rules.spoken_intent
    return plan.model_copy(update={
        "company_id": company_id,
        "goal_keywords": kws[:8],
        "spoken_intent": intents,
        "profile_fields_needed": [f for f in plan.profile_fields_needed if f in profile] or rules.profile_fields_needed,
        "user_language": detect_language(text),
    })


async def build_plan(text: str, company_id: str, profile: dict[str, str],
                     llm: JsonLLM | None = None) -> tuple[CallPlan, Method]:
    if not text.strip():
        raise ValueError("empty problem description")
    rules = rule_plan(text, company_id, profile)
    if llm is None:
        return rules, "rule"
    tree = load_tree(company_id)
    user = json.dumps({
        "customer_text": text,
        "company": {"id": company_id, "name": tree["display_name"], "menu_wording": menu_wording(tree)},
        "profile_fields_available": sorted(profile),
    }, ensure_ascii=False)
    try:
        s = get_settings()
        plan = await llm.complete_json((PROMPTS / "plan.md").read_text(encoding="utf-8"), user, CallPlan,
                                       realtime=False, effort=s.llm_reasoning_effort_plan,
                                       max_tokens=s.llm_max_tokens_plan, timeout_s=s.llm_timeout_plan_s)
    except Exception:  # noqa: BLE001 - any LLM failure falls back to rules (§9.1)
        return rules, "rule"
    return _sanitize(plan, rules, company_id, profile, text), "llm"
