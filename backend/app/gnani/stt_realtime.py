"""Prisma realtime STT over raw websockets (CLAUDE.md §7.1). Implements the orchestrator's STTStream.

feed(frame, t) is synchronous (called on the 20 ms tick): it re-chunks 320-byte line frames into exact
1024-byte (64 ms) chunks for a sender task, and returns any transcripts the receiver task has finalised.
The sender never runs ahead of real time (bursting is forbidden), so on a FakeClock line it throttles.

Timestamps (on our line timeline). M0 showed no `processing` message, so:
  start = audio time sent when `speech_start` arrived (minus min_speech)
  end   = audio time sent when the transcript arrived - VAD end silence - reported latency
Sessions rotate before the 15 min cap (stt_session_rotate_s) with stt_session_overlap_s of overlap;
duplicate transcripts from the overlap are dropped. A dropped socket reconnects with backoff.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import json
import logging
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from websockets.asyncio.client import ClientConnection, connect

from app.audio.frames import STT_CHUNK_BYTES, Rechunker
from app.config import Settings, get_settings
from app.gnani.errors import GLOBAL_METER, RetryPolicy, UsageMeter
from app.sim.oracle_stt import Transcript

log = logging.getLogger(__name__)
CHUNK_S = STT_CHUNK_BYTES / 2 / 8000  # 0.064
DEFAULT_BIAS = ["recharge", "complaint", "refund", "dabaiye", "press", "agent", "executive", "billing", "hash"]

Connector = Callable[..., Any]


def _letters_only(word: str) -> bool:
    """Letters and combining marks only (Devanagari vowel signs are marks), no digits/spaces/symbols."""
    return bool(word) and all(unicodedata.category(c)[0] in "LM" for c in word)


def bias_words(words: list[str]) -> list[str]:
    return list(dict.fromkeys(w.lower() for w in words if _letters_only(w)))


@dataclass
class _Session:
    n: int
    ws: ClientConnection
    opened_at: float  # line time
    queue: asyncio.Queue[tuple[bytes, float] | None] = field(default_factory=asyncio.Queue)
    rechunk: Rechunker = field(default_factory=lambda: Rechunker(STT_CHUNK_BYTES))
    sent_t: float = 0.0  # line time at the end of the last chunk sent
    sent_s: float = 0.0  # audio seconds sent
    wall0: float | None = None  # wall time of the first chunk sent (pacing reference)
    speech_start: dict[str, float] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)
    tasks: list[asyncio.Task[None]] = field(default_factory=list)
    closing: bool = False


class PrismaRealtime:
    def __init__(self, settings: Settings | None = None, meter: UsageMeter | None = None, lang: str | None = None,
                 bias: list[str] | None = None, url: str | None = None, connector: Connector | None = None,
                 pace: bool = True) -> None:
        self.s = settings or get_settings()
        self.meter = meter or GLOBAL_METER
        self.lang = lang or self.s.stt_lang_default
        self.bias = bias_words(DEFAULT_BIAS + (bias or []))
        base = self.s.gnani_base_url.replace("https://", "wss://").replace("http://", "ws://").rstrip("/")
        self.url = url or f"{base}/stt/v3/stream"
        self._connect = connector or connect
        self.pace = pace
        self._sessions: list[_Session] = []
        self._ready: collections.deque[Transcript] = collections.deque()
        self._seen: list[tuple[str, float]] = []
        self._n_sessions = 0
        self._n_segments = 0
        self._t = 0.0
        self._rotating = False
        self._overlap_until: float | None = None
        self._overlap_done = asyncio.Event()
        self._closed = False
        self.errors: list[dict[str, Any]] = []
        self.server_configs: list[dict[str, Any]] = []

    # ---------- headers ----------
    def headers(self) -> dict[str, str]:
        s = self.s
        h = {
            "x-api-key-id": s.gnani_api_key.get_secret_value(),
            "lang_code": self.lang,
            "x-sample-rate": "8000",
            "x-format": "transcribe",
            "x-vad-threshold": str(s.stt_vad_threshold),
            "x-min-speech-ms": str(s.stt_min_speech_ms),
            "x-min-silence-ms": str(s.stt_min_silence_ms),
            "x-min-segment-ms": str(s.stt_min_segment_ms),
            "x-max-speech-s": str(s.stt_max_speech_s),
        }
        if self.bias:
            h["x-bias-list"] = json.dumps(self.bias, ensure_ascii=True)  # non-ASCII as \\u escapes
            h["x-bias-score"] = "1.0"
        return h

    # ---------- lifecycle ----------
    async def start(self, t: float = 0.0) -> None:
        self._t = t
        await self._open(t)

    async def _open(self, t: float) -> _Session:
        delays = RetryPolicy.from_settings(self.s).delays()
        for attempt in range(len(delays) + 1):
            try:
                ws = await self._connect(self.url, additional_headers=self.headers(), proxy=None,
                                         open_timeout=10, max_size=2**20)
                break
            except Exception as e:  # noqa: BLE001
                if attempt == len(delays):
                    raise
                log.warning("prisma realtime connect failed (%s); retrying", type(e).__name__)
                await asyncio.sleep(delays[attempt])
        self._n_sessions += 1
        sess = _Session(self._n_sessions, ws, opened_at=t)
        sess.tasks = [asyncio.create_task(self._sender(sess)), asyncio.create_task(self._receiver(sess))]
        self._sessions.append(sess)
        return sess

    async def close(self) -> None:
        self._closed = True
        self._overlap_done.set()
        for sess in list(self._sessions):
            await self._close_session(sess)

    async def _close_session(self, sess: _Session, flush_s: float = 0.0) -> None:
        sess.closing = True
        tail = sess.rechunk.flush()
        if tail:
            sess.queue.put_nowait((tail + b"\x00" * (STT_CHUNK_BYTES - len(tail)), self._t))
        sess.queue.put_nowait(None)
        if flush_s:
            await asyncio.sleep(flush_s)  # let the server finalise the last segment
        with contextlib.suppress(Exception):
            await sess.ws.close()
        for task in sess.tasks:
            task.cancel()
        await asyncio.gather(*sess.tasks, return_exceptions=True)
        if sess in self._sessions:
            self._sessions.remove(sess)

    # ---------- STTStream ----------
    def feed(self, frame: bytes, t: float) -> list[Transcript]:
        self._t = t + 0.02
        for sess in self._sessions:
            if sess.closing:
                continue
            for chunk in sess.rechunk.push(frame):
                sess.queue.put_nowait((chunk, self._t))
        if self._overlap_until is not None and self._t >= self._overlap_until:
            self._overlap_done.set()
        self._maybe_rotate(t)
        out = list(self._ready)
        self._ready.clear()
        return out

    def _maybe_rotate(self, t: float) -> None:
        live = [s for s in self._sessions if not s.closing]
        if not live or self._rotating or self._closed:
            return
        if t - live[-1].opened_at >= self.s.stt_session_rotate_s:
            self._rotating = True
            asyncio.get_running_loop().create_task(self._rotate(live[-1], t))

    async def _rotate(self, old: _Session, t: float) -> None:
        try:
            await self._open(self._t)
            # both sessions get audio for the overlap; feed() sets the event once enough has been sent
            self._overlap_until = self._t + self.s.stt_session_overlap_s
            self._overlap_done.clear()
            await self._overlap_done.wait()
            await self._close_session(old, flush_s=0.5)
        finally:
            self._overlap_until = None
            self._rotating = False

    async def update_bias(self, words: list[str]) -> None:
        self.bias = bias_words(self.bias + words)
        msg = json.dumps({"type": "update_settings", "bias": {"list": self.bias, "score": 1.0}})
        for sess in self._sessions:
            if not sess.closing:
                await sess.ws.send(msg)

    # ---------- tasks ----------
    async def _sender(self, sess: _Session) -> None:
        try:
            while True:
                item = await sess.queue.get()
                if item is None:
                    return
                chunk, t = item
                if sess.wall0 is None:
                    sess.wall0 = time.monotonic()
                if self.pace:  # never run ahead of real time
                    ahead = sess.sent_s - (time.monotonic() - sess.wall0)
                    if ahead > 0.25:
                        await asyncio.sleep(ahead - 0.25)
                await sess.ws.send(chunk)
                sess.sent_s += CHUNK_S
                sess.sent_t = t
                self.meter.add(stt_rt_seconds=CHUNK_S)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("prisma sender stopped: %s", type(e).__name__)

    async def _receiver(self, sess: _Session) -> None:
        try:
            async for raw in sess.ws:
                if isinstance(raw, bytes):
                    continue
                self._on_message(sess, json.loads(raw))
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("prisma receiver stopped: %s", type(e).__name__)
        if not sess.closing and not self._closed:  # unexpected drop -> reconnect
            log.warning("prisma session %d dropped; reconnecting", sess.n)
            if sess in self._sessions:
                self._sessions.remove(sess)
            await self._open(self._t)

    def _on_message(self, sess: _Session, m: dict[str, Any]) -> None:
        typ = m.get("type")
        if typ == "connected":
            sess.config = m.get("config") or {}
            self.server_configs.append(sess.config)
        elif typ == "speech_start":
            utt = str(m.get("utterance_id", ""))
            sess.speech_start[utt] = max(0.0, sess.sent_t - self.s.stt_min_speech_ms / 1000)
        elif typ == "transcript":
            if m.get("is_final") is False:
                return  # never rely on partials (§7.1)
            text = " ".join(str(m.get("text", "")).split())
            if not text:
                return
            latency_s = float(m.get("latency") or 0) / 1000
            end = max(0.0, sess.sent_t - self.s.stt_min_silence_ms / 1000 - latency_s)
            dur = float(m.get("audio_duration_ms") or 0) / 1000
            start = sess.speech_start.pop(str(m.get("utterance_id", "")), max(0.0, end - dur))
            start = min(start, end)
            if any(tx == text and abs(st - start) < 1.5 for tx, st in self._seen):
                return  # duplicate from a rotation overlap
            self._seen.append((text, start))
            self._seen = self._seen[-50:]
            self._n_segments += 1
            self._ready.append(Transcript(f"rt{self._n_segments}", text, round(start, 3), round(end, 3)))
        elif typ == "error":
            self.errors.append(m)
            log.warning("prisma error: %s", m)
