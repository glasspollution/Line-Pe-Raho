"""Per-milestone progress from the junit report: passed / failed / not-implemented.

Contract tests skip with "NOT IMPLEMENTED: <module> [<task>]" until their module exists, so
the not-implemented column is the agents' to-do list. Milestone comes from the
`milestone` property that conftest.py records for tests marked @pytest.mark.milestone("M1").

Usage: python scripts/scoreboard.py [--junit reports/junit.xml] [--json]
"""

from __future__ import annotations

import argparse
import json
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TASK_RE = re.compile(r"NOT IMPLEMENTED: ([\w.]+)(?: \[([\w-]+)\])?")


@dataclass
class Row:
    passed: int = 0
    failed: int = 0
    not_implemented: int = 0
    skipped: int = 0
    todo: set[str] = field(default_factory=set)
    failing: list[str] = field(default_factory=list)


def build(junit: Path) -> dict[str, Row]:
    board: dict[str, Row] = defaultdict(Row)
    for case in ET.parse(junit).getroot().iter("testcase"):
        props = {p.get("name"): p.get("value") for p in case.iter("property")}
        row = board[props.get("milestone") or "tooling"]
        tid = f"{case.get('classname')}::{case.get('name')}"
        status = "passed"
        for child in case:
            if child.tag in ("failure", "error"):
                status = "failed"
            elif child.tag == "skipped":
                m = TASK_RE.search(child.get("message", "") + (child.text or ""))
                if m:
                    status = "not_implemented"
                    row.todo.add(m.group(1) + (f" [{m.group(2)}]" if m.group(2) else ""))
                else:
                    status = "skipped"
        setattr(row, status, getattr(row, status) + 1)
        if status == "failed":
            row.failing.append(tid)
    return board


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--junit", default=str(ROOT / "reports" / "junit.xml"))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    path = Path(args.junit)
    if not path.exists():
        print("scoreboard: no junit report; run python scripts/check.py first")
        return 2
    board = build(path)
    if args.json:
        print(json.dumps({k: {**asdict(v), "todo": sorted(v.todo)} for k, v in board.items()}, indent=2))
        return 0
    print(f"{'milestone':10} {'pass':>5} {'fail':>5} {'todo':>5} {'skip':>5}")
    for ms in sorted(board):
        r = board[ms]
        print(f"{ms:10} {r.passed:>5} {r.failed:>5} {r.not_implemented:>5} {r.skipped:>5}")
    for ms in sorted(board):
        r = board[ms]
        if r.failing:
            print(f"\n{ms} failing:")
            for t in r.failing[:15]:
                print(f"  x {t}")
        if r.todo:
            print(f"\n{ms} not implemented yet:")
            for t in sorted(r.todo):
                print(f"  - {t}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
