"""PSTN degradation simulator (CLAUDE.md §6.2), applied per direction on VirtualLine.

Chain per 20 ms frame: gain -> additive noise -> 300–3400 Hz bandpass -> G.711 mu-law -> packet loss.

SNR is defined against NOMINAL_SIGNAL_DBFS (a typical speech level): the noise is calibrated so its
level *at the output* (after the bandpass) is NOMINAL_SIGNAL_DBFS - snr. Noise is procedural by
default (offline, deterministic, no licensing); a looped WAV clip can be supplied instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
from scipy.signal import butter, lfilter, lfilter_zi, sosfilt, sosfilt_zi

from app.audio.frames import FRAME_BYTES, LINE_RATE, bytes_to_pcm, mulaw_decode, mulaw_encode

NOMINAL_SIGNAL_DBFS = -20.0
PROFILES: dict[str, float | None] = {"clean": None, "snr20": 20.0, "snr10": 10.0, "snr5": 5.0}
NOISE_TYPES = ("street", "babble", "fan")
_BAND = (300.0, 3400.0)
_CALIBRATION_S = 4.0


@dataclass
class PSTNConfig:
    profile: str = "clean"
    noise_type: str = "street"
    packet_loss: float = 0.0
    gain_db: float | None = None  # None -> random uniform ±3 dB, drawn once per simulator
    seed: int = 0
    noise_clip: str | None = None  # optional 8 kHz mono WAV, looped

    def __post_init__(self) -> None:
        if self.profile not in PROFILES:
            raise ValueError(f"unknown profile {self.profile!r}; expected one of {list(PROFILES)}")
        if self.noise_type not in NOISE_TYPES:
            raise ValueError(f"unknown noise_type {self.noise_type!r}; expected one of {NOISE_TYPES}")
        if not 0.0 <= self.packet_loss < 1.0:
            raise ValueError("packet_loss must be in [0, 1)")


class _NoiseSource:
    """Streaming procedural noise; phase/filter state carries across calls."""

    def __init__(self, kind: str, rng: np.random.Generator, clip: str | None = None) -> None:
        self.kind = kind
        self.rng = rng
        self.n = 0  # global sample counter for phase continuity
        self.clip: np.ndarray | None = None
        if clip:
            import soundfile as sf

            data, sr = sf.read(Path(clip), dtype="float64", always_2d=True)
            if sr != LINE_RATE:
                raise ValueError(f"noise clip must be {LINE_RATE} Hz, got {sr}")
            self.clip = data[:, 0]
        # street: Paul Kellet-style pink filter (approx. 1/f) on white noise
        self._pink_b = np.array([0.049922035, -0.095993537, 0.050612699, -0.004408786])
        self._pink_a = np.array([1.0, -2.494956002, 2.017265875, -0.522189400])
        self._pink_zi = lfilter_zi(self._pink_b, self._pink_a) * 0.0
        # fan: leaky-integrated (brown) noise + 50 Hz mains hum harmonics
        self._brown_b, self._brown_a = np.array([0.05]), np.array([1.0, -0.995])
        self._brown_zi = lfilter_zi(self._brown_b, self._brown_a) * 0.0
        # babble: a handful of synthetic "voices" (harmonic series with syllable-rate envelopes)
        k = 6
        self._f0 = rng.uniform(95, 240, k)
        self._syl = rng.uniform(3.0, 6.0, k)
        self._ph = rng.uniform(0, 2 * np.pi, k)

    def next(self, n: int) -> np.ndarray:
        if self.clip is not None:
            idx = (np.arange(self.n, self.n + n)) % len(self.clip)
            self.n += n
            return self.clip[idx]
        t = (np.arange(self.n, self.n + n)) / LINE_RATE
        self.n += n
        if self.kind == "street":
            w = self.rng.normal(0, 1, n)
            y, self._pink_zi = lfilter(self._pink_b, self._pink_a, w, zi=self._pink_zi)
            rumble = 0.5 * np.sin(2 * np.pi * 0.3 * t) * self.rng.normal(0, 0.3, n)
            return np.asarray(y + rumble)
        if self.kind == "fan":
            w = self.rng.normal(0, 1, n)
            out, self._brown_zi = lfilter(self._brown_b, self._brown_a, w, zi=self._brown_zi)
            hum = sum(np.sin(2 * np.pi * 50 * h * t) / h for h in range(2, 9))
            return out + 0.15 * hum + 0.05 * w
        # babble
        total = np.zeros(n)
        for f0, syl, ph in zip(self._f0, self._syl, self._ph, strict=True):
            env = np.clip(np.sin(2 * np.pi * syl * t + ph), 0, None) ** 2
            harmonics = sum(np.sin(2 * np.pi * f0 * h * t + h * ph) / h for h in range(1, int(3400 // f0)))
            total += env * harmonics
        return total + 0.05 * self.rng.normal(0, 1, n)


class PSTNSimulator:
    """Stateful one-direction channel. `process` takes and returns 320-byte 8 kHz frames."""

    def __init__(self, cfg: PSTNConfig) -> None:
        self.cfg = cfg
        root = np.random.default_rng(cfg.seed)
        gain_rng, noise_rng, cal_rng, loss_rng = root.spawn(4)
        gain_db = cfg.gain_db if cfg.gain_db is not None else float(gain_rng.uniform(-3.0, 3.0))
        self.gain = 10 ** (gain_db / 20)
        self._loss_rng = loss_rng
        self._sos = butter(4, _BAND, btype="band", fs=LINE_RATE, output="sos")
        self._zi = sosfilt_zi(self._sos) * 0.0
        self._prev = b"\x00" * FRAME_BYTES
        snr = PROFILES[cfg.profile]
        self._noise: _NoiseSource | None = None
        self._noise_scale = 0.0
        if snr is not None:
            self._noise = _NoiseSource(cfg.noise_type, noise_rng, cfg.noise_clip)
            self._noise_scale = self._calibrate(cfg, cal_rng, snr)

    def _calibrate(self, cfg: PSTNConfig, rng: np.random.Generator, snr: float) -> float:
        """Scale so the noise RMS *after the bandpass* hits NOMINAL_SIGNAL_DBFS - snr."""
        probe = _NoiseSource(cfg.noise_type, rng, cfg.noise_clip)
        x = probe.next(int(_CALIBRATION_S * LINE_RATE))
        y = sosfilt(self._sos, x)[LINE_RATE // 2 :]  # drop filter settling
        rms = float(np.sqrt(np.mean(y**2))) or 1e-12
        target = 32768.0 * 10 ** ((NOMINAL_SIGNAL_DBFS - snr) / 20)
        return target / rms

    def process(self, frame: bytes) -> bytes:
        if len(frame) != FRAME_BYTES:
            raise ValueError(f"expected {FRAME_BYTES}-byte frames, got {len(frame)}")
        x = bytes_to_pcm(frame).astype(np.float64) * self.gain
        if self._noise is not None:
            x = x + self._noise.next(len(x)) * self._noise_scale
        y, self._zi = sosfilt(self._sos, x, zi=self._zi)
        pcm = np.clip(np.round(y), -32768, 32767).astype(np.int16)
        out = mulaw_decode(mulaw_encode(pcm)).astype("<i2").tobytes()
        if self.cfg.packet_loss and self._loss_rng.random() < self.cfg.packet_loss:
            out = self._prev  # conceal a lost frame by repeating the previous one
        self._prev = out
        return out
