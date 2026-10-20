"""One command, the whole product loop: problem text -> plan -> call -> IVR -> hold -> human -> bridge ->
summary with verified evidence.

  python scripts/e2e.py --text "Nimbus ne mera recharge do baar kaat liya, refund chahiye" --company nimbus_telecom
  python scripts/e2e.py ... --live        # real Timbre + Prisma + Kimi (spends credits; ~2 min)

Offline (default): rules plan, synthetic voices, exact transcription, FakeClock: free and takes seconds.
Artifacts: data/e2e/<company>-<ts>/ (journal.jsonl, recordings/*.wav, summary.json[, report.json]).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT)]

from app.agent.planner import build_plan  # noqa: E402
from app.sim.harness import DEMO_PROFILE, SimConfig, run_sim_call  # noqa: E402
from app.sim.live import LiveConfig, run_live_call  # noqa: E402
from scripts.replay import render  # noqa: E402

SHOW = ("state_changed", "menu_parsed", "decision", "dtmf_sent", "hold_started", "human_detected",
        "probe_sent", "user_joined", "call_ended", "summary_ready", "error")


def print_summary(summary: dict[str, Any]) -> None:
    print(f"\n=== summary: outcome={summary['outcome']} (extraction: {summary['method']}) "
          f"verified {summary['verified_count']} / unverified {summary['unverified_count']} ===")
    for key, f in summary["fields"].items():
        if f["value"] in (None, ""):
            continue
        mark = "OK  " if f["verified"] else "WARN"
        ev = f["evidence"][0] if f["evidence"] else {}
        clip = f"  clip {ev.get('start', 0):.1f}-{ev.get('end', 0):.1f}s" if ev else ""
        print(f"  [{mark}] {key:<14} {f['value']}{clip}")
        if not f["verified"]:
            print(f"         reason: {f['reason']}")


async def offline(text: str, company: str, out: Path) -> int:
    plan, method = await build_plan(text, company, DEMO_PROFILE, None)
    print(f"plan ({method}): goal={plan.goal!r} keywords={plan.goal_keywords} language={plan.user_language}")
    t0 = time.perf_counter()
    sim = await run_sim_call(SimConfig(company, plan=plan, out_dir=out))
    print(f"\n=== call: {sim.line_seconds:.0f}s simulated in {time.perf_counter() - t0:.1f}s ===")
    for ev in sim.events:
        if ev["type"] in SHOW:
            print(render(ev)[:170])
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    print_summary(summary)
    truth = sim.agent.truth  # type: ignore[union-attr]
    ok = summary["fields"]["ticket_id"]["value"] == truth.ticket_id and summary["fields"]["ticket_id"]["verified"]
    print(f"\nground truth ticket {truth.ticket_id}, promised {truth.promised_by}: {'MATCH' if ok else 'MISMATCH'}")
    print(f"artifacts: {out}")
    return 0 if ok and sim.result.final_state == "DONE" else 1


async def live(text: str, company: str, out: Path, hold: float) -> int:
    rep = await run_live_call(LiveConfig(company_id=company, text=text, out_dir=out, hold_s=hold))
    print(f"plan ({rep.plan_method}): {json.dumps(rep.plan, ensure_ascii=False)}")
    for line in (out / "journal.jsonl").read_text(encoding="utf-8").splitlines():
        ev = json.loads(line)
        if ev["type"] in SHOW + ("transcript",):
            print(render(ev)[:170])
    summary_path = out / "summary.json"
    if summary_path.exists():
        print_summary(json.loads(summary_path.read_text(encoding="utf-8")))
    print(f"\nground truth: {rep.truth}")
    print(f"summary matches truth: {rep.summary_ok}")
    print(f"detect {rep.time_to_detect_s}s, probe {rep.probe_used}, prompt WER {rep.prompt_wer}, "
          f"call {rep.call_seconds}s (wall {rep.wall_seconds}s)")
    print(f"metered usage: {rep.usage}  (new TTS: {rep.tts_new_phrases} phrases, {rep.tts_new_chars} chars)")
    print(f"artifacts: {out}")
    return 0 if rep.final_state == "DONE" and all(rep.summary_ok.values()) else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", default="Nimbus ne mera recharge do baar kaat liya, refund chahiye")
    ap.add_argument("--company", default="nimbus_telecom")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--hold", type=float, default=15.0)
    args = ap.parse_args()
    out = ROOT / "data" / "e2e" / f"{args.company}-{time.strftime('%Y%m%d-%H%M%S')}"
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # ₹ and Devanagari on Windows consoles
    if args.live:
        return asyncio.run(live(args.text, args.company, out, args.hold))
    return asyncio.run(offline(args.text, args.company, out))


if __name__ == "__main__":
    raise SystemExit(main())
