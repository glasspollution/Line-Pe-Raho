"""Test ratchet: once a test passes on main it must keep passing.

eval/ratchet.json is a sorted list of test ids ("<classname>::<name>") that have passed.
  check   : every ratcheted test must be PASSED in the junit report (failed/skipped/missing = regression)
  --update: add newly passing tests to the ratchet (never removes)
  --base  : also fail if the ratchet file dropped ids relative to a base copy (e.g. origin/main's)

Usage:
  python scripts/ratchet.py [--junit reports/junit.xml] [--update] [--base path/to/base_ratchet.json]
"""

from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RATCHET = ROOT / "eval" / "ratchet.json"


def read_junit(path: Path) -> dict[str, str]:
    """Return {test_id: 'passed'|'failed'|'skipped'}."""
    results: dict[str, str] = {}
    for case in ET.parse(path).getroot().iter("testcase"):
        tid = f"{case.get('classname', '')}::{case.get('name', '')}"
        status = "passed"
        for child in case:
            if child.tag in ("failure", "error"):
                status = "failed"
                break
            if child.tag == "skipped":
                status = "skipped"
        results[tid] = status
    return results


def load(path: Path) -> list[str]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return list(data.get("tests", []))


def save(path: Path, tests: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"_doc": "Tests that must keep passing. Managed by scripts/ratchet.py --update.",
               "tests": sorted(set(tests))}
    path.write_text(json.dumps(payload, indent=1) + "\n", encoding="utf-8")


def regressions(ratchet: list[str], results: dict[str, str]) -> list[tuple[str, str]]:
    return [(t, results.get(t, "missing")) for t in ratchet if results.get(t) != "passed"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--junit", default=str(ROOT / "reports" / "junit.xml"))
    ap.add_argument("--ratchet", default=str(RATCHET))
    ap.add_argument("--update", action="store_true")
    ap.add_argument("--base", help="base ratchet.json to compare against (dropped ids = failure)")
    args = ap.parse_args()

    rpath = Path(args.ratchet)
    current = load(rpath)
    rc = 0

    if args.base and Path(args.base).exists():
        base = set(load(Path(args.base)))
        dropped = sorted(base - set(current))
        if dropped:
            rc = 1
            print(f"ratchet: {len(dropped)} id(s) removed from ratchet.json vs base:")
            for t in dropped:
                print(f"  - {t}")

    jpath = Path(args.junit)
    if not jpath.exists():
        print(f"ratchet: junit report not found at {jpath}; run pytest --junitxml first")
        return 2
    results = read_junit(jpath)
    regs = regressions(current, results)
    for t, st in regs:
        print(f"REGRESSION {st:8} {t}")
    if regs:
        rc = 1

    passing = {t for t, s in results.items() if s == "passed"}
    new = sorted(passing - set(current))
    if args.update and not regs:
        save(rpath, current + new)
        print(f"ratchet: +{len(new)} newly locked, {len(set(current) | passing)} total")
    elif args.update and regs:
        print("ratchet: not updating while regressions exist")
    else:
        print(f"ratchet: {len(current)} locked, {len(regs)} regression(s), {len(new)} passing-but-unlocked "
              f"(run with --update to lock)")
    return rc


if __name__ == "__main__":
    sys.exit(main())
