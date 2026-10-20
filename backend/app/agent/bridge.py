"""User leg <-> phone line bridge (CLAUDE.md §2.5, §8 BRIDGED, §14.2 /ws/user-audio).

Opening the user's audio channel is the join: `connect()` tells the orchestrator (user audio connected
-> BRIDGED). While bridged, the orchestrator hands every callee frame to `to_user` and flushes queued
user frames into the line each tick; our agent stays muted. Browser audio arrives as 48 kHz float32 and
is resampled to 8 kHz 20 ms frames. Buffers are capped (~10 s) so a stalled client can't grow memory.
"""

from __future__ import annotations

import collections
from collections.abc import Callable

import numpy as np

from app.audio.frames import FRAME_BYTES, Rechunker, pcm_to_bytes, resample

BROWSER_RATE = 48000
_MAX_FRAMES = 500


class Bridge:
    def __init__(self, on_connect: Callable[[], None] | None = None,
                 on_hangup: Callable[[], None] | None = None) -> None:
        self.on_connect = on_connect
        self.on_hangup = on_hangup
        self.connected = False
        self.hung_up = False
        self.voiced_frames = 0  # 20 ms frames of real user speech received (for turn detection without STT)
        self.active = False  # set by the orchestrator while BRIDGED
        self._uplink: collections.deque[bytes] = collections.deque(maxlen=_MAX_FRAMES)
        self._downlink: collections.deque[bytes] = collections.deque(maxlen=_MAX_FRAMES)
        self._rechunk = Rechunker(FRAME_BYTES)

    def connect(self) -> None:
        if not self.connected:
            self.connected = True
            if self.on_connect:
                self.on_connect()

    def disconnect(self) -> None:
        self.connected = False

    def hangup(self) -> None:
        """The user ended the call (hang-up button, or closed the tab while bridged)."""
        if not self.hung_up:
            self.hung_up = True
            if self.on_hangup:
                self.on_hangup()

    # user -> line
    def push_user_pcm(self, frame: bytes) -> None:
        """8 kHz s16le audio from the user (any length; re-chunked to 20 ms)."""
        if not self.connected:
            return
        for f in self._rechunk.push(frame):
            x = np.frombuffer(f, dtype="<i2").astype(np.float64)
            if np.sqrt(np.mean(x * x)) > 32768 * 10 ** (-45 / 20):  # > -45 dBFS: the user is talking
                self.voiced_frames += 1
            self._uplink.append(f)

    def push_browser_audio(self, data: bytes) -> None:
        if not self.connected or not data:
            return
        x = np.frombuffer(data[: len(data) - len(data) % 4], dtype="<f4")
        pcm = resample(np.clip(np.round(x * 32767), -32768, 32767).astype(np.int16), BROWSER_RATE, 8000)
        self.push_user_pcm(pcm_to_bytes(pcm))

    def take_uplink(self) -> list[bytes]:
        out = list(self._uplink)
        self._uplink.clear()
        return out

    # line -> user
    def to_user(self, frame: bytes) -> None:
        if self.connected and self.active:
            self._downlink.append(frame)

    def pop_for_user(self) -> bytes | None:
        return self._downlink.popleft() if self._downlink else None
