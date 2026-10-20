"""REST: mock company directory and simulator scenario (CLAUDE.md §11.4, §14.1)."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from app.agent.planner import tree_fields
from app.company.engine import list_companies, load_tree
from app.runtime import CallManager

router = APIRouter(prefix="/api")

SAMPLE_PROBLEMS = {
    "nimbus_telecom": "Nimbus ne mera recharge do baar kaat liya, refund chahiye",
    "sahyog_bank": "Kal mere card se transaction fail hua par paise kat gaye",
    "prakash_bijli_board": "Hamare area mein subah se bijli nahi hai",
}


class ScenarioIn(BaseModel):
    hold_s: float | None = Field(default=None, ge=0, le=45 * 60)
    noise_profile: Literal["clean", "snr20", "snr10", "snr5"] | None = None
    noise_type: Literal["street", "babble", "fan"] | None = None
    packet_loss: float | None = Field(default=None, ge=0, le=0.1)
    agent_mode: Literal["auto", "silent", "console"] | None = None
    auto_user: bool | None = None
    user_join_delay_s: float | None = Field(default=None, ge=0, le=60)


@router.get("/companies")
async def companies() -> list[dict[str, Any]]:
    out = []
    for c in list_companies():
        tree = load_tree(c["id"])
        out.append({**c, "profile_fields": tree_fields(tree), "sample_problem": SAMPLE_PROBLEMS.get(c["id"], "")})
    return out


@router.get("/company/scenario")
async def get_scenario(request: Request) -> dict[str, Any]:
    m: CallManager = request.app.state.manager
    return asdict(m.scenario)


@router.post("/company/scenario")
async def set_scenario(body: ScenarioIn, request: Request) -> dict[str, Any]:
    m: CallManager = request.app.state.manager
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(m.scenario, k, v)
    return asdict(m.scenario)
