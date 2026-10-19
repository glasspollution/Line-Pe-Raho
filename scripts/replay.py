"""Re-render a call timeline in the terminal (CLAUDE.md §18).

Usage: python scripts/replay.py <call_id | path/to/journal.jsonl> [--data-dir data]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

DETAIL_KEYS = {
    "state_changed": ("from", "to"),
    "transcript": ("track", "text"),
    "menu_parsed": ("options",),
    "decision": ("kind", "method", "digit", "field", "reason", "latency_ms"),
    "dtmf_sent": ("digits", "digits_masked"),
    "hold_started": ("reason",),
    "announcement_heard": ("count",),
    "human_suspected": ("score", "evidence"),
    "probe_sent": ("text",),
    "human_detected": ("time_to_detect_s",),
    "keepalive_sent": ("n", "text"),
    "call_ended": ("by", "duration_s", "hold_seconds", "ivr_seconds"),
    "summary_ready": ("outcome", "verified_count", "unverified_count"),
    "error": ("message",),
}


def fmt_t(t: float) -> str:
    m, s = divmod(t, 60)
    return f"{int(m):02d}:{s:05.2f}"


def render(ev: dict[str, Any]) -> str:
    typ = ev["type"]
    if typ == "state_changed":
        return f"{fmt_t(ev['t'])}  ==== {ev.get('from')} -> {ev.get('to')} ===="
    parts = []
    for k in DETAIL_KEYS.get(typ, ()):
        if k in ev and ev[k] not in (None, ""):
            v = ev[k]
            if k == "options":
                v = ", ".join(f"{o['digit']}={o['label']}" for o in v)
            parts.append(f"{k}={v}" if k not in ("text", "reason", "options") else str(v))
    return f"{fmt_t(ev['t'])}  {typ:<18} {' | '.join(map(str, parts))}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("call")
    ap.add_argument("--data-dir", default=str(ROOT / "data"))
    args = ap.parse_args()
    path = Path(args.call)
    if not path.exists():
        path = Path(args.data_dir) / "calls" / args.call / "journal.jsonl"
    if not path.exists():
        print(f"no journal at {path}", file=sys.stderr)
        return 2
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            print(render(json.loads(line)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
