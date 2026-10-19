"""Procedural hold music (CLAUDE.md §11.2). No samples, so no copyright questions.

A seeded chord progression of soft pads plus light percussion is rendered once into a seamless
32 s loop at 8 kHz, then streamed frame by frame. The "vocal" variant adds a hummed, vibrato melody
with its energy in the speech band, to stress the hold/human detectors.

Names used in IVR trees: "procedural_seed_<n>" and "procedural_vocal_<n>".
"""

from __future__ import annotations

import re

import numpy as np

from app.audio.frames import FRAME_SAMPLES, LINE_RATE, pcm_to_bytes

LOOP_S = 32.0
LEVEL_DBFS = -24.0
_PROGRESSIONS = (  # scale degrees (semitones from the key root) of each chord's root, with quality
    ((0, "maj"), (7, "maj"), (9, "min"), (5, "maj")),  # I V vi IV
    ((0, "maj"), (5, "maj"), (7, "maj"), (5, "maj")),  # I IV V IV
    ((9, "min"), (5, "maj"), (0, "maj"), (7, "maj")),  # vi IV I V
    ((0, "maj"), (9, "min"), (5, "maj"), (7, "maj")),  # I vi IV V
)
_NAME_RE = re.compile(r"^procedural_(seed|vocal)_(\d+)$")


def _midi_hz(m: float) -> float:
    return float(440.0 * 2 ** ((m - 69) / 12))


def _envelope(n: int, attack: int, release: int) -> np.ndarray:
    env = np.ones(n)
    a, r = min(attack, n // 2), min(release, n // 2)
    env[:a] = np.linspace(0, 1, a)
    env[n - r :] = np.linspace(1, 0, r)
    return env


def render_loop(seed: int, vocal: bool = False, seconds: float = LOOP_S, sr: int = LINE_RATE) -> np.ndarray:
    """Deterministic loop for (seed, vocal). Returns int16 PCM normalised to LEVEL_DBFS RMS."""
    rng = np.random.default_rng(seed)
    n_total = int(seconds * sr)
    out = np.zeros(n_total)
    root = 48 + int(rng.integers(0, 12))  # C3..B3
    prog = _PROGRESSIONS[int(rng.integers(0, len(_PROGRESSIONS)))]
    bpm = float(rng.uniform(80, 104))
    beat = 60.0 / bpm
    chord_s = 4 * beat
    n_chords = max(1, int(round(seconds / chord_s)))
    chord_s = seconds / n_chords  # fit an integer number of chords so the loop is seamless
    t_chord = np.arange(int(chord_s * sr)) / sr

    melody_notes: list[float] = []
    for i in range(n_chords):
        deg, quality = prog[i % len(prog)]
        third = 3 if quality == "min" else 4
        notes = [root + deg, root + deg + third, root + deg + 7, root + deg + 12]
        pad = np.zeros_like(t_chord)
        for m in notes:
            f = _midi_hz(m)
            # soft "saw-ish" pad: a few decaying harmonics, slight detune for warmth
            for h in range(1, 5):
                if f * h < 3400:
                    pad += np.sin(2 * np.pi * f * h * t_chord * (1 + 0.0015 * h)) / (h * 1.6)
        env = _envelope(len(t_chord), int(0.35 * sr), int(0.35 * sr))
        start = int(i * chord_s * sr)
        seg = pad * env
        out[start : start + len(seg)] += seg[: n_total - start]
        melody_notes += [root + deg + 12 + int(rng.choice([0, third, 7, 12])) for _ in range(4)]

    # light percussion: a short filtered-noise tick on each beat, softer on off-beats
    n_beats = int(seconds / beat)
    tick_len = int(0.06 * sr)
    decay = np.exp(-np.arange(tick_len) / (0.012 * sr))
    for b in range(n_beats):
        s = int(b * beat * sr)
        noise = rng.normal(0, 1, tick_len)
        noise = np.convolve(noise, np.ones(4) / 4, mode="same")  # take the edge off
        gain = 0.9 if b % 2 == 0 else 0.45
        out[s : s + tick_len] += gain * noise[: n_total - s] * decay[: n_total - s]

    if vocal:
        # hummed melody: harmonic-rich voice-like tone, 5.5 Hz vibrato, formant emphasis ~500/1000 Hz
        note_s = seconds / len(melody_notes)
        t = np.arange(n_total) / sr
        f0 = np.repeat([_midi_hz(m) for m in melody_notes], int(np.ceil(note_s * sr)))[:n_total]
        f0 = np.convolve(f0, np.ones(400) / 400, mode="same")  # glide between notes
        f0 *= 1 + 0.012 * np.sin(2 * np.pi * 5.5 * t)
        phase = 2 * np.pi * np.cumsum(f0) / sr
        voice = np.zeros(n_total)
        for h in range(1, 12):
            fh = f0 * h
            formant = np.exp(-(((fh - 520) / 260) ** 2)) + 0.6 * np.exp(-(((fh - 1050) / 380) ** 2)) + 0.05
            voice += np.where(fh < 3400, formant * np.sin(h * phase), 0.0)
        syll = 0.6 + 0.4 * np.clip(np.sin(2 * np.pi * (1 / note_s) * t), 0, None)
        out += 1.4 * voice * syll

    # seamless loop: short crossfade of the tail into the head
    xf = int(0.05 * sr)
    out[:xf] = out[:xf] * np.linspace(0, 1, xf) + out[-xf:] * np.linspace(1, 0, xf)
    out = out[:-xf] if n_total > 4 * xf else out
    rms = float(np.sqrt(np.mean(out**2))) or 1.0
    out *= (32768.0 * 10 ** (LEVEL_DBFS / 20)) / rms
    return np.clip(np.round(out), -32768, 32767).astype(np.int16)


def parse_name(name: str) -> tuple[int, bool]:
    m = _NAME_RE.match(name)
    if not m:
        raise ValueError(f"unknown hold music {name!r}; use procedural_seed_<n> or procedural_vocal_<n>")
    return int(m.group(2)), m.group(1) == "vocal"


class HoldMusic:
    """Streams a rendered loop as 20 ms frames, wrapping seamlessly."""

    _cache: dict[tuple[int, bool], np.ndarray] = {}

    def __init__(self, seed: int, vocal: bool = False) -> None:
        key = (seed, vocal)
        if key not in self._cache:
            self._cache[key] = render_loop(seed, vocal)
        self._loop = self._cache[key]
        self._pos = 0

    @classmethod
    def from_name(cls, name: str) -> HoldMusic:
        seed, vocal = parse_name(name)
        return cls(seed, vocal)

    def next_frame(self) -> bytes:
        n = len(self._loop)
        idx = (np.arange(self._pos, self._pos + FRAME_SAMPLES)) % n
        self._pos = (self._pos + FRAME_SAMPLES) % n
        return pcm_to_bytes(self._loop[idx])
