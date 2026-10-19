"""In-band DTMF (CLAUDE.md §6.4): tone generator for the agent, Goertzel detector for the mock company.

Detector rules (per 20 ms frame): the strongest row and column tones are both above an absolute
floor, together carry most of the frame's energy, each clearly beats the runner-up in its group,
and twist < dtmf_max_twist_db. A key is reported once it has held for >= dtmf_min_ms, and only once
per press (it must drop out before the same key can be reported again).
"""

from __future__ import annotations

import math

import numpy as np

from app.config import get_settings

ROWS = (697.0, 770.0, 852.0, 941.0)
COLS = (1209.0, 1336.0, 1477.0)
KEYPAD = ("123", "456", "789", "*0#")
KEY_FREQS: dict[str, tuple[float, float]] = {
    KEYPAD[r][c]: (ROWS[r], COLS[c]) for r in range(4) for c in range(3)
}

_FLOOR_DBFS = -30.0  # per-tone level floor (generator uses -8 dBFS; leaves room for line loss)
_MIN_TONE_ENERGY_FRACTION = 0.3
_MIN_GROUP_DOMINANCE = 4.0  # strongest tone vs runner-up in its row/column group (power ratio)


def generate(digits: str, sample_rate: int = 8000) -> np.ndarray:
    """Sequential dual tones: tone_ms on, gap_ms off, per digit. Returns int16 PCM."""
    s = get_settings()
    amp = 32768.0 * 10 ** (s.dtmf_level_dbfs / 20)
    t = np.arange(int(s.dtmf_tone_ms * sample_rate / 1000)) / sample_rate
    gap = np.zeros(int(s.dtmf_gap_ms * sample_rate / 1000))
    parts: list[np.ndarray] = []
    for d in digits:
        if d not in KEY_FREQS:
            raise ValueError(f"not a DTMF key: {d!r}")
        lo, hi = KEY_FREQS[d]
        parts += [amp * np.sin(2 * np.pi * lo * t) + amp * np.sin(2 * np.pi * hi * t), gap]
    if not parts:
        return np.zeros(0, dtype=np.int16)
    return np.clip(np.round(np.concatenate(parts)), -32767, 32767).astype(np.int16)


class DTMFDetector:
    """Feed 20 ms frames; returns newly detected keys."""

    def __init__(self, sample_rate: int = 8000) -> None:
        s = get_settings()
        self.sample_rate = sample_rate
        self.max_twist_db = s.dtmf_max_twist_db
        self.min_ms = s.dtmf_min_ms
        self._basis_cache: dict[int, np.ndarray] = {}
        self._floor_power = (32768.0 * 10 ** (_FLOOR_DBFS / 20)) ** 2
        self._key: str | None = None
        self._held_ms = 0.0
        self._reported = False

    def _basis(self, n: int) -> np.ndarray:
        """Complex exponentials for the 7 DTMF frequencies (single-bin Goertzel equivalent)."""
        if n not in self._basis_cache:
            idx = np.arange(n)
            freqs = np.array(ROWS + COLS)
            self._basis_cache[n] = np.exp(-2j * np.pi * np.outer(freqs, idx) / self.sample_rate)
        return self._basis_cache[n]

    def _classify(self, x: np.ndarray) -> str | None:
        n = len(x)
        if n == 0:
            return None
        # power of each sinusoid as amplitude^2 (peak-referenced): |X|^2 * 4 / n^2
        p = np.abs(self._basis(n) @ x) ** 2 * 4 / (n * n)
        rp, cp = p[:4], p[4:]
        ri, ci = int(np.argmax(rp)), int(np.argmax(cp))
        r, c = float(rp[ri]), float(cp[ci])
        if r < self._floor_power or c < self._floor_power:
            return None
        if abs(10 * math.log10(r / c)) >= self.max_twist_db:
            return None
        total = float(np.mean(x * x)) * 2 + 1e-9  # mean power -> peak-referenced sinusoid scale
        if (r + c) / total < _MIN_TONE_ENERGY_FRACTION:
            return None
        if r < _MIN_GROUP_DOMINANCE * float(np.sort(rp)[-2]) or c < _MIN_GROUP_DOMINANCE * float(np.sort(cp)[-2]):
            return None
        return KEYPAD[ri][ci]

    def feed(self, frame: bytes) -> list[str]:
        x = np.frombuffer(frame, dtype="<i2").astype(np.float64)
        key = self._classify(x)
        frame_ms = 1000.0 * len(x) / self.sample_rate
        if key is None or key != self._key:
            self._key, self._held_ms, self._reported = key, 0.0, False
        if key is None:
            return []
        self._held_ms += frame_ms
        if not self._reported and self._held_ms >= self.min_ms:
            self._reported = True
            return [key]
        return []
