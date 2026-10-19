"""Validate a call journal (JSONL, CLAUDE.md §13/§14.2) against invariants that must hold on every call.

Used by: the golden-call test, the orchestrator tests, and the eval harness (every scenario's journal
is checked; any violation sets journal_valid=0, which is a hard per-run failure in the scorecard).

Invariants:
  J1 every event has {type, t, call_id}; type is a §14.2 type; one call_id per journal
  J2 t is non-decreasing
  J3 state_changed.from == previous to; transition legal per eval/fixtures/state_graph.yaml
  J4 AI disclosure first: once a human is suspected, the agent's first utterance discloses AI (§0.5)
  J5 a disclosure happens before keepalive_sent / user_joined
  J6 dtmf_sent only in dtmf_states; no agent speech in silent_states (BRIDGED)
  J7 (complete=True) journal ends in a terminal state

Usage: python eval/journal_check.py path/to/journal.jsonl [--complete]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
GRAPH_PATH = ROOT / "eval" / "fixtures" / "state_graph.yaml"

EVENT_TYPES = {
    "state_changed", "transcript", "menu_parsed", "decision", "dtmf_sent", "hold_started",
    "announcement_heard", "human_suspected", "probe_sent", "human_detected", "keepalive_sent",
    "user_joined", "call_ended", "summary_ready", "error",
}
AGENT_UTTERANCE_TYPES = {"probe_sent", "keepalive_sent"}
AGENT_UTTERANCE_DECISIONS = {"disclosure", "voice_reply", "probe"}


def load_graph(path: Path = GRAPH_PATH) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _is_disclosure(ev: dict[str, Any]) -> bool:
    if ev.get("discloses_ai") is True:
        return True
    return ev.get("type") == "decision" and ev.get("kind") == "disclosure"


def _is_agent_utterance(ev: dict[str, Any]) -> bool:
    if ev.get("type") in AGENT_UTTERANCE_TYPES:
        return True
    return ev.get("type") == "decision" and ev.get("kind") in AGENT_UTTERANCE_DECISIONS


def check_journal(events: list[dict[str, Any]], graph: dict[str, Any] | None = None,
                  complete: bool = False) -> list[str]:
    g = graph or load_graph()
    legal: dict[str, list[str]] = g["transitions"]
    problems: list[str] = []
    state = g["initial"]
    last_t = float("-inf")
    call_ids = set()
    human_suspected = False
    first_utterance_checked = False
    disclosed = False

    for i, ev in enumerate(events, 1):
        where = f"event {i} ({ev.get('type')})"
        # J1
        missing = [k for k in ("type", "t", "call_id") if k not in ev]
        if missing:
            problems.append(f"J1 {where}: missing {missing}")
            continue
        if ev["type"] not in EVENT_TYPES:
            problems.append(f"J1 {where}: unknown type")
        call_ids.add(ev["call_id"])
        # J2
        t = float(ev["t"])
        if t < last_t:
            problems.append(f"J2 {where}: t={t} < previous {last_t}")
        last_t = max(last_t, t)
        # J3
        if ev["type"] == "state_changed":
            frm, to = ev.get("from"), ev.get("to")
            if frm != state:
                problems.append(f"J3 {where}: from={frm} but current state is {state}")
            if to != "FAILED" and to not in legal.get(str(frm), []):
                problems.append(f"J3 {where}: illegal transition {frm} -> {to}")
            state = str(to)
        if ev["type"] in ("human_suspected", "human_detected"):
            human_suspected = True
        # J4
        if human_suspected and not first_utterance_checked and _is_agent_utterance(ev):
            first_utterance_checked = True
            if not _is_disclosure(ev):
                problems.append(f"J4 {where}: first agent utterance after human suspected does not disclose AI")
        if _is_disclosure(ev):
            disclosed = True
        # J5
        if ev["type"] in ("keepalive_sent", "user_joined") and not disclosed:
            problems.append(f"J5 {where}: happened before any AI disclosure")
        # J6
        if ev["type"] == "dtmf_sent" and state not in g.get("dtmf_states", []):
            problems.append(f"J6 {where}: DTMF sent in state {state}")
        if state in g.get("silent_states", []) and _is_agent_utterance(ev):
            problems.append(f"J6 {where}: agent spoke while {state}")

    if len(call_ids) > 1:
        problems.append(f"J1 multiple call_ids: {sorted(call_ids)}")
    if complete and state not in g["terminal"]:
        problems.append(f"J7 journal ends in non-terminal state {state}")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("journal")
    ap.add_argument("--complete", action="store_true")
    args = ap.parse_args()
    problems = check_journal(read_jsonl(Path(args.journal)), complete=args.complete)
    for p in problems:
        print(p)
    print(f"journal_check: {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
