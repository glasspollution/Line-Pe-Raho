"""Callee-track audio classifier (CLAUDE.md §10.1): silence | music | speech | tone per 500 ms window.

Features per window: RMS dBFS, webrtcvad speech ratio (informational), spectral flatness (300–3400 Hz),
spectral-flux variance over 20 ms frames, and the energy share of the two strongest spectral peaks.

Rules (thresholds in config, tuned 2026-10-20 on procedural music, synthetic and Timbre speech,
DTMF and ringback, clean through snr5):
  rms < silence_dbfs                      -> silence
  top-2 peak share >= tone_min_top2       -> tone       (DTMF ~0.87, ringback ~0.95; music <= 0.66)
  flux variance >= speech_min_flux_var    -> speech     (speech >= ~0.008; music ~0.002)
  flatness >= noise_min_flatness          -> silence    (line noise only)
  otherwise                               -> music
Deviations from the spec text: webrtcvad cannot separate music from speech on this audio, and the
autocorrelation harmonic ratio added no separation, so neither drives a decision.

`sustained_music` is true when >= sustain_min_music of the last sustain_windows windows are music
(the spec's ">= 2 s"); single stray windows at chord changes or drum hits don't flip it.
"""

from __future__ import annotations

import collections
from dataclasses import dataclass
from typing import Literal

import numpy as np

from app.audio.frames import FRAME_BYTES, LINE_RATE
from app.audio.vad import VAD
from app.config import Settings, get_settings

Label = Literal["silence", "music", "speech", "tone"]
_BAND = (300.0, 3400.0)


@dataclass(frozen=True)
class WindowFeatures:
    rms_dbfs: float
    vad_ratio: float
    flatness: float
    flux_var: float
    top2: float


@dataclass(frozen=True)
class WindowResult:
    t_start: float
    t_end: float
    label: Label
    sustained_music: bool
    features: WindowFeatures


def _top2_share(p: np.ndarray) -> float:
    total = float(p.sum())
    if total <= 0:
        return 0.0
    used = np.zeros(len(p), dtype=bool)
    acc = 0.0
    found = 0
    for i in np.argsort(p)[::-1]:
        lo, hi = max(0, i - 3), i + 4
        if used[lo:hi].any():
            continue
        used[lo:hi] = True
        acc += float(p[lo:hi].sum())
        found += 1
        if found == 2:
            break
    return acc / total


def window_features(x: np.ndarray, vad: VAD | None = None) -> WindowFeatures:
    xf = x.astype(np.float64)
    n = len(xf)
    rms = float(np.sqrt(np.mean(xf * xf))) if n else 0.0
    rms_dbfs = 20 * np.log10(max(rms, 1e-9) / 32768.0)
    spec = np.abs(np.fft.rfft(xf * np.hanning(n))) ** 2
    f = np.fft.rfftfreq(n, 1 / LINE_RATE)
    p = spec[(f >= _BAND[0]) & (f <= _BAND[1])] + 1e-12
    flatness = float(np.exp(np.mean(np.log(p))) / np.mean(p))
    frames = xf[: n - n % 160].reshape(-1, 160)
    mags = np.abs(np.fft.rfft(frames * np.hanning(160), axis=1))
    if len(mags) > 2:
        diff = np.clip(mags[1:] - mags[:-1], 0, None) ** 2
        flux = diff.sum(axis=1) / (np.sum(mags[1:] ** 2, axis=1) + 1e-9)
        flux_var = float(np.var(flux))
    else:
        flux_var = 0.0
    vr = vad.speech_ratio(x.astype("<i2").tobytes()) if vad is not None else 0.0
    return WindowFeatures(float(rms_dbfs), vr, flatness, flux_var, _top2_share(p))


def label_window(ft: WindowFeatures, s: Settings) -> Label:
    if ft.rms_dbfs < s.classifier_silence_dbfs:
        return "silence"
    if ft.top2 >= s.classifier_tone_min_top2:
        return "tone"
    if ft.flux_var >= s.classifier_speech_min_flux_var:
        return "speech"
    if ft.flatness >= s.classifier_noise_min_flatness:
        return "silence"
    if ft.rms_dbfs > s.music_min_dbfs:
        return "music"
    return "silence"


class AudioClassifier:
    """Streaming: feed 20 ms frames, get a WindowResult every classifier_window_ms."""

    def __init__(self, settings: Settings | None = None, use_vad: bool = True) -> None:
        self.s = settings or get_settings()
        self._vad = VAD(self.s.vad_mode) if use_vad else None
        self._win_frames = max(1, self.s.classifier_window_ms // 20)
        self._buf: list[bytes] = []
        self._t0: float | None = None
        self._recent: collections.deque[Label] = collections.deque(maxlen=self.s.classifier_sustain_windows)
        self.history: collections.deque[WindowResult] = collections.deque(maxlen=240)  # last 2 minutes

    def feed(self, frame: bytes, t: float) -> WindowResult | None:
        if len(frame) != FRAME_BYTES:
            raise ValueError(f"expected {FRAME_BYTES}-byte frames")
        if self._t0 is None:
            self._t0 = t
        self._buf.append(frame)
        if len(self._buf) < self._win_frames:
            return None
        x = np.frombuffer(b"".join(self._buf), dtype="<i2")
        ft = window_features(x, self._vad)
        label = label_window(ft, self.s)
        self._recent.append(label)
        sustained = (len(self._recent) == self._recent.maxlen
                     and sum(lab == "music" for lab in self._recent) >= self.s.classifier_sustain_min_music)
        res = WindowResult(self._t0, t + 0.02, label, sustained, ft)
        self.history.append(res)
        self._buf = []
        self._t0 = None
        return res

    @property
    def sustained_music(self) -> bool:
        return bool(self.history) and self.history[-1].sustained_music

    def music_stopped_before(self, t: float) -> bool:
        """True if the last window that ended by t (±50 ms) was not music: the music had stopped before t."""
        before = [w for w in self.history if w.t_end <= t + 0.05]
        return bool(before) and before[-1].label != "music"

    def music_fraction(self, t_from: float, t_to: float) -> float:
        """Share of windows overlapping [t_from, t_to] labelled music (for music_under / no-music-before)."""
        ws = [w for w in self.history if w.t_end > t_from and w.t_start < t_to]
        return sum(w.label == "music" for w in ws) / len(ws) if ws else 0.0
