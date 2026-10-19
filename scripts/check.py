"""L0 gate: the single command every agent runs before committing. Exit 0 = mergeable.

Steps (skipped when their target does not exist yet, failed when a required tool is missing):
  guard      scripts/guard_constraints.py        hard constraints (§0)
  ruff       ruff check                           lint
  mypy       mypy (full mode only)                types
  pytest     offline tests -> reports/junit.xml   network is blocked by conftest.py
  ratchet    scripts/ratchet.py                   previously-passing tests still pass
  journal    eval/journal_check.py on fixtures    golden journals satisfy invariants
  fe:*       npm run typecheck|lint|test|build (+ e2e in full mode) when frontend/package.json exists

Writes reports/check.json and prints a summary table.

Usage:
  python scripts/check.py            # full (before PR)
  python scripts/check.py --fast     # inner loop: skips mypy, frontend build/e2e
  python scripts/check.py --base-ratchet path   # CI: also fail if ratchet ids were removed vs base
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports"
PY = sys.executable
FRONTEND_SCRIPTS_REQUIRED = ["typecheck", "lint", "test", "build"]


@dataclass
class Step:
    name: str
    status: str  # pass | fail | skip
    seconds: float
    detail: str


def run(name: str, cmd: list[str], cwd: Path = ROOT) -> Step:
    t0 = time.perf_counter()
    exe = shutil.which(cmd[0]) or cmd[0]
    try:
        p = subprocess.run([exe, *cmd[1:]], cwd=cwd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return Step(name, "fail", 0.0, f"tool not found: {cmd[0]} (install dev deps, see docs/AGENT_LOOP.md)")
    out = (p.stdout + p.stderr).strip()
    tail = "\n".join(out.splitlines()[-25:])
    return Step(name, "pass" if p.returncode == 0 else "fail", round(time.perf_counter() - t0, 2), tail)


def skip(name: str, why: str) -> Step:
    return Step(name, "skip", 0.0, why)


def py_targets() -> list[str]:
    return [d for d in ("backend", "scripts", "eval", "conftest.py") if (ROOT / d).exists()]


def frontend_steps(full: bool) -> list[Step]:
    pkg = ROOT / "frontend" / "package.json"
    if not pkg.exists():
        return [skip("frontend", "frontend/package.json not present yet")]
    scripts = json.loads(pkg.read_text(encoding="utf-8")).get("scripts", {})
    missing = [s for s in FRONTEND_SCRIPTS_REQUIRED + (["e2e"] if full else []) if s not in scripts]
    if missing:
        return [Step("frontend", "fail", 0.0, f"package.json missing required scripts: {missing} (docs/FRONTEND.md)")]
    steps: list[Step] = []
    if not (ROOT / "frontend" / "node_modules").exists():
        steps.append(run("fe:install", ["npm", "ci"], ROOT / "frontend"))
    names = ["typecheck", "lint", "test"] + (["build", "e2e"] if full else [])
    for s in names:
        steps.append(run(f"fe:{s}", ["npm", "run", s], ROOT / "frontend"))
    return steps


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fast", action="store_true")
    ap.add_argument("--base-ratchet")
    ap.add_argument("--no-frontend", action="store_true")
    args = ap.parse_args()
    full = not args.fast
    REPORTS.mkdir(exist_ok=True)
    env_note = "RUN_LIVE ignored: check.py never runs live tests"
    os.environ["RUN_LIVE"] = "0"

    steps: list[Step] = [run("guard", [PY, "scripts/guard_constraints.py"])]
    steps.append(run("ruff", [PY, "-m", "ruff", "check", *py_targets()]))
    if full:
        mypy_targets = [t for t in ("backend/app", "scripts", "eval") if (ROOT / t).exists()]
        steps.append(run("mypy", [PY, "-m", "mypy", *mypy_targets]))
    steps.append(run("pytest", [PY, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                                f"--junitxml={REPORTS / 'junit.xml'}", "-m", "not live"]))
    ratchet_cmd = [PY, "scripts/ratchet.py"]
    if args.base_ratchet:
        ratchet_cmd += ["--base", args.base_ratchet]
    steps.append(run("ratchet", ratchet_cmd))
    for j in sorted((ROOT / "eval" / "fixtures").glob("*.jsonl")):
        steps.append(run(f"journal:{j.stem}", [PY, "eval/journal_check.py", str(j), "--complete"]))
    if not args.no_frontend:
        steps.extend(frontend_steps(full))

    ok = all(s.status != "fail" for s in steps)
    (REPORTS / "check.json").write_text(json.dumps(
        {"ok": ok, "mode": "full" if full else "fast", "note": env_note, "steps": [asdict(s) for s in steps]},
        indent=2), encoding="utf-8")

    print(f"\n{'step':22} {'status':6} {'secs':>6}")
    for s in steps:
        print(f"{s.name:22} {s.status:6} {s.seconds:>6}")
    for s in steps:
        if s.status == "fail":
            print(f"\n--- {s.name} ---\n{s.detail}")
    print(f"\ncheck: {'PASS' if ok else 'FAIL'}  (reports/check.json)")
    if (REPORTS / "junit.xml").exists():
        subprocess.run([PY, "scripts/scoreboard.py"], cwd=ROOT)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
