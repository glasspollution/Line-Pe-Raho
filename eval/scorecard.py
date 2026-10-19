"""L2 scorecard: metrics.csv + thresholds.yaml (+ baseline) -> scorecard.json / scorecard.md.

Exit 1 if any gate fails, any hard per-run rule is violated, any rule regressed beyond tol vs
baseline, or required columns are missing. The markdown lists problems worst-first so an agent
picking "what to improve next" starts at the top.

Usage:
  python eval/scorecard.py eval/reports/<ts>/metrics.csv [--baseline eval/reports/baseline/scorecard.json]
                           [--thresholds eval/thresholds.yaml] [--out-dir <dir of metrics.csv>] [--full]
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import operator
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
OPS: dict[str, Callable[[float, float], bool]] = {
    ">=": operator.ge, "<=": operator.le, ">": operator.gt, "<": operator.lt, "==": operator.eq,
}


@dataclass
class RuleResult:
    id: str
    metric: str
    agg: str
    n: int
    value: float | None
    gate: str | None
    target: str | None
    gate_ok: bool | None
    target_ok: bool | None
    baseline: float | None = None
    regression: bool = False
    worst_rows: list[str] = field(default_factory=list)


def norm(v: Any) -> str:
    s = str(v).strip()
    low = s.lower()
    if low in ("true", "yes"):
        return "1"
    if low in ("false", "no"):
        return "0"
    try:
        f = float(s)
        return str(int(f)) if f.is_integer() else s
    except ValueError:
        return s


def parse_cond(cond: str) -> tuple[Callable[[float, float], bool], float, str]:
    sym, num = cond.split()
    return OPS[sym], float(num), sym


def percentile(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    if len(xs) == 1:
        return xs[0]
    k = (len(xs) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def to_float(v: str) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def aggregate(rows: list[dict[str, str]], rule: dict[str, Any]) -> tuple[float | None, int]:
    metric, agg = rule["metric"], rule["agg"]
    vals = [x for r in rows if (x := to_float(r.get(metric, ""))) is not None]
    if agg == "per_hour":
        per = [x for r in rows if (x := to_float(r.get(rule["per"], ""))) is not None]
        total_s = sum(per)
        return (sum(vals) / total_s * 3600.0 if total_s > 0 else None), len(vals)
    if not vals:
        return None, 0
    if agg == "mean":
        return sum(vals) / len(vals), len(vals)
    if agg == "p50":
        return percentile(vals, 0.5), len(vals)
    if agg == "p90":
        return percentile(vals, 0.9), len(vals)
    if agg == "max":
        return max(vals), len(vals)
    raise ValueError(f"unknown agg {agg}")


def select(rows: list[dict[str, str]], where: dict[str, list[Any]] | None) -> list[dict[str, str]]:
    if not where:
        return rows
    allowed = {k: {norm(v) for v in vs} for k, vs in where.items()}
    return [r for r in rows if all(norm(r.get(k, "")) in vs for k, vs in allowed.items())]


def worst(rows: list[dict[str, str]], rule: dict[str, Any], higher_better: bool) -> list[str]:
    scored: list[tuple[float, str]] = []
    for r in rows:
        v = to_float(r.get(rule["metric"], ""))
        if v is not None:
            scored.append((v, r.get("scenario_id", "?")))
    scored.sort(key=lambda x: x[0], reverse=not higher_better)
    return [f"{s}={v:g}" for v, s in scored[:5]]


def evaluate(rows: list[dict[str, str]], cfg: dict[str, Any],
             baseline: dict[str, Any] | None = None, full: bool = False) -> dict[str, Any]:
    """full=True (complete matrix run): a rule with no matching rows is a failure (coverage gap)."""
    columns = set(rows[0].keys()) if rows else set()
    missing_cols = [c for c in cfg.get("required_columns", []) if c not in columns]

    hard: list[str] = []
    for h in cfg.get("hard_per_run", []):
        fn = OPS[h["op"]]
        for r in rows:
            v = to_float(r.get(h["metric"], ""))
            if v is None or not fn(v, float(h["value"])):
                hard.append(f"{r.get('scenario_id', '?')}: {h['metric']}={r.get(h['metric'])!r} ({h['why']})")

    base_vals = {b["id"]: b.get("value") for b in (baseline or {}).get("rules", [])}
    results: list[RuleResult] = []
    for rule in cfg.get("rules", []):
        sel = select(rows, rule.get("where"))
        value, n = aggregate(sel, rule)
        gate_ok = target_ok = None
        higher_better = True
        if rule.get("gate"):
            fn, thr, sym = parse_cond(rule["gate"])
            higher_better = sym.startswith(">")
            gate_ok = None if value is None else fn(value, thr)
        if rule.get("target"):
            fn, thr, sym = parse_cond(rule["target"])
            higher_better = sym.startswith(">")
            target_ok = None if value is None else fn(value, thr)
        res = RuleResult(rule["id"], rule["metric"], rule["agg"], n, value, rule.get("gate"),
                         rule.get("target"), gate_ok, target_ok)
        b = base_vals.get(rule["id"])
        if b is not None and value is not None:
            res.baseline = b
            delta = (b - value) if higher_better else (value - b)
            res.regression = delta > float(rule.get("tol", 0))
        if gate_ok is False or target_ok is False or res.regression:
            res.worst_rows = worst(sel, rule, higher_better)
        results.append(res)

    gate_fail = [r for r in results if r.gate_ok is False]
    regress = [r for r in results if r.regression]
    no_data = [r.id for r in results if r.value is None]
    ok = not (missing_cols or hard or gate_fail or regress or (full and no_data)) and bool(rows)
    return {
        "ok": ok,
        "n_runs": len(rows),
        "missing_columns": missing_cols,
        "hard_violations": hard,
        "no_data": no_data,
        "full": full,
        "rules": [asdict(r) for r in results],
    }


def fmt(v: float | None) -> str:
    return "-" if v is None else f"{v:.3g}"


def to_markdown(card: dict[str, Any]) -> str:
    lines = [f"# Eval scorecard - {'PASS' if card['ok'] else 'FAIL'}", "", f"Runs: {card['n_runs']}", ""]
    if card["missing_columns"]:
        lines += ["## Missing required columns", *[f"- `{c}`" for c in card["missing_columns"]], ""]
    if card["hard_violations"]:
        lines += ["## Hard per-run violations (fix first)", *[f"- {h}" for h in card["hard_violations"]], ""]
    rules = card["rules"]

    def rank(r: dict[str, Any]) -> int:
        if r["gate_ok"] is False:
            return 0
        if r["regression"]:
            return 1
        if r["value"] is None:
            return 2
        if r["target_ok"] is False:
            return 3
        return 4

    lines += ["## Rules (worst first)", "",
              "| status | id | value | gate | target | baseline | n | worst scenarios |",
              "|---|---|---|---|---|---|---|---|"]
    labels = {0: "GATE FAIL", 1: "REGRESSED", 2: "NO DATA", 3: "below target", 4: "ok"}
    for r in sorted(rules, key=rank):
        lines.append(f"| {labels[rank(r)]} | {r['id']} | {fmt(r['value'])} | {r['gate'] or ''} | "
                     f"{r['target'] or ''} | {fmt(r['baseline'])} | {r['n']} | {', '.join(r['worst_rows'])} |")
    lines += ["", "Improvement loop: take the first non-ok row, form a hypothesis, change one thing, "
              "re-run the affected slice, compare against this scorecard."]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("metrics")
    ap.add_argument("--thresholds", default=str(ROOT / "eval" / "thresholds.yaml"))
    ap.add_argument("--baseline")
    ap.add_argument("--out-dir")
    ap.add_argument("--full", action="store_true", help="complete matrix: rules without data fail")
    args = ap.parse_args()

    with open(args.metrics, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    cfg = yaml.safe_load(Path(args.thresholds).read_text(encoding="utf-8"))
    baseline = None
    if args.baseline and Path(args.baseline).exists():
        baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
    card = evaluate(rows, cfg, baseline, full=args.full)
    out = Path(args.out_dir or Path(args.metrics).parent)
    out.mkdir(parents=True, exist_ok=True)
    (out / "scorecard.json").write_text(json.dumps(card, indent=2), encoding="utf-8")
    md = to_markdown(card)
    (out / "scorecard.md").write_text(md, encoding="utf-8")
    print(md)
    return 0 if card["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
