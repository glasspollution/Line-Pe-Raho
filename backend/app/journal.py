"""Append-only call journal (CLAUDE.md §13, §14.2): one JSON event per line, plus live fan-out.

Every event is {type, t, call_id, ...}. `t` is call time in seconds (line time, so it aligns with the
recordings). Unknown event types are rejected. Use `mask_digits` before logging anything typed into an IVR.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

EVENT_TYPES = frozenset({
    "state_changed", "transcript", "menu_parsed", "decision", "dtmf_sent", "hold_started",
    "announcement_heard", "human_suspected", "probe_sent", "human_detected", "keepalive_sent",
    "user_joined", "call_ended", "summary_ready", "error",
})


def mask_digits(digits: str, keep_last: int = 4) -> str:
    """'9000000001#' -> '******0001#'. Single menu presses (1–2 chars) are shown as-is."""
    if len(digits) <= 2:
        return digits
    body, tail = (digits[:-1], digits[-1]) if digits[-1] in "#*" else (digits, "")
    return "*" * max(0, len(body) - keep_last) + body[-keep_last:] + tail


class Journal:
    def __init__(self, call_id: str, path: Path, clock: Callable[[], float]) -> None:
        self.call_id = call_id
        self.path = path
        self._clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a", encoding="utf-8", newline="\n")
        self._subscribers: list[asyncio.Queue[dict[str, Any]]] = []
        self.events: list[dict[str, Any]] = []

    @classmethod
    def for_call(cls, data_dir: Path, call_id: str, clock: Callable[[], float]) -> Journal:
        return cls(call_id, data_dir / "calls" / call_id / "journal.jsonl", clock)

    def emit(self, type_: str, t: float | None = None, **fields: Any) -> dict[str, Any]:
        if type_ not in EVENT_TYPES:
            raise ValueError(f"unknown journal event type {type_!r}")
        reserved = {"type", "t", "call_id"} & fields.keys()
        if reserved:
            raise ValueError(f"reserved fields {sorted(reserved)}")
        ev = {"type": type_, "t": round(self._clock() if t is None else t, 3), "call_id": self.call_id, **fields}
        self._fh.write(json.dumps(ev, ensure_ascii=False, separators=(",", ":")) + "\n")
        self._fh.flush()
        self.events.append(ev)
        for q in self._subscribers:
            q.put_nowait(ev)
        return ev

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        for ev in self.events:  # late joiners get the backlog first
            q.put_nowait(ev)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        if q in self._subscribers:
            self._subscribers.remove(q)

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.close()


def read_journal(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
