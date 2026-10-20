"""Live run of the full loop: plan -> call -> IVR -> hold -> human -> bridge -> summary (spends credits).

Usage:
  python scripts/spikes/live_call.py --dry-run          # show how many TTS characters would be new
  python scripts/spikes/live_call.py [--company nimbus_telecom] [--hold 15] [--noise snr10]
Writes data/live/<company>-<ts>/{journal.jsonl, recordings/, report.json}.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.sim.live import LiveConfig, run_live_call  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--company", default="nimbus_telecom")
    ap.add_argument("--text", help="the user's problem (default: the company demo plan)")
    ap.add_argument("--no-llm", action="store_true", help="rules only (no Kimi)")
    ap.add_argument("--hold", type=float, default=15.0)
    ap.add_argument("--noise", default="snr10")
    ap.add_argument("--silent-agent", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    out = ROOT / "data" / "live" / f"{args.company}-{time.strftime('%Y%m%d-%H%M%S')}"
    cfg = LiveConfig(company_id=args.company, out_dir=out, hold_s=args.hold, noise_profile=args.noise,
                     greets_first=not args.silent_agent, dry_run=args.dry_run, text=args.text,
                     use_llm=not args.no_llm)
    rep = asyncio.run(run_live_call(cfg))
    summary = {k: v for k, v in rep.__dict__.items() if k != "transcripts"}
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    if not args.dry_run:
        print("\ntranscripts (what Prisma heard on the company side):")
        for tr in rep.transcripts:
            print(f"  {tr['start']:7.2f}-{tr['end']:7.2f}  {tr['text']}")
        print(f"\nartifacts: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
