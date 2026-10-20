"""Pydantic models for every LLM output (CLAUDE.md §9, §21)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class CallPlan(BaseModel):
    """§9.1. Built by the LLM from the user's request (or by rules as a fallback)."""

    company_id: str
    goal: str
    goal_keywords: list[str] = Field(default_factory=list)
    needs_human: bool = True
    spoken_intent: dict[str, str] = Field(default_factory=dict)  # lang -> short phrase for voice prompts
    profile_fields_needed: list[str] = Field(default_factory=list)
    issue_summary_for_agent: str = ""
    user_language: Literal["en-IN", "hi-IN", "hi-en"] = "hi-en"


class IVRDecisionOut(BaseModel):
    """§9.3 LLM output for a menu decision."""

    action: Literal["press", "wait", "repeat"]
    digit: str | None = None
    reason: str = ""
    confidence: float = 0.0


class Evidence(BaseModel):
    segment_id: str
    quote: str


class ExtractedField(BaseModel):
    value: str | None = None
    evidence: list[Evidence] = Field(default_factory=list)


class ExtractedAmount(BaseModel):
    value: float
    currency: str = "INR"
    evidence: list[Evidence] = Field(default_factory=list)


class Extraction(BaseModel):
    """§12.4 post-call extraction. Every value cites the segment(s) it came from."""

    ticket_id: ExtractedField = Field(default_factory=ExtractedField)
    agent_name: ExtractedField = Field(default_factory=ExtractedField)
    resolution: ExtractedField = Field(default_factory=ExtractedField)
    promised_by: ExtractedField = Field(default_factory=ExtractedField)  # ISO date YYYY-MM-DD
    amounts: list[ExtractedAmount] = Field(default_factory=list)
    next_steps: list[ExtractedField] = Field(default_factory=list)
    outcome: Literal["resolved", "ticket_raised", "callback_promised", "unresolved"] = "unresolved"
