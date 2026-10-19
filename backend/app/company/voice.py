"""Prompt audio for the mock company.

PromptAudio renders a sentence to 8 kHz PCM. TimbrePromptAudio (pre-synthesised cache, T-003) is the
real one. SyntheticVoice is an offline, deterministic stand-in: speech-like audio (voiced harmonics,
formants, syllable envelope) whose duration tracks the word count. Tests and fake-STT eval use it;
the text itself travels in the engine's ground-truth log.
"""

from __future__ import annotations

import hashlib
from typing import Protocol

import numpy as np

from app.audio.frames import LINE_RATE, pcm_to_bytes

WORD_S = 0.32
LEVEL_DBFS = -20.0


class PromptAudio(Protocol):
    def render(self, text: str) -> bytes: ...  # 8 kHz s16le mono PCM


class SyntheticVoice:
    def __init__(self, f0: float = 210.0) -> None:
        self.f0 = f0
        self._cache: dict[str, bytes] = {}

    def render(self, text: str) -> bytes:
        if text in self._cache:
            return self._cache[text]
        seed = int.from_bytes(hashlib.sha1(text.encode("utf-8")).digest()[:4], "little")
        rng = np.random.default_rng(seed)
        words = max(1, len(text.split()))
        n = int((words * WORD_S + 0.2) * LINE_RATE)
        t = np.arange(n) / LINE_RATE
        f0 = self.f0 * (1 + 0.08 * np.sin(2 * np.pi * 0.7 * t + rng.uniform(0, 6.28)))  # intonation
        phase = 2 * np.pi * np.cumsum(f0) / LINE_RATE
        x = np.zeros(n)
        for h in range(1, 16):
            fh = f0 * h
            formants = (np.exp(-(((fh - 700) / 220) ** 2)) + 0.7 * np.exp(-(((fh - 1200) / 300) ** 2))
                        + 0.4 * np.exp(-(((fh - 2500) / 400) ** 2)) + 0.03)
            x += np.where(fh < 3400, formants * np.sin(h * phase), 0.0)
        syll_rate = 1 / (WORD_S / 1.6)
        env = np.clip(np.sin(2 * np.pi * syll_rate * t + rng.uniform(0, 6.28)), 0, None) ** 0.6
        x *= env
        x += rng.normal(0, 0.02, n)  # breath/friction
        fade = int(0.02 * LINE_RATE)
        x[:fade] *= np.linspace(0, 1, fade)
        x[-fade:] *= np.linspace(1, 0, fade)
        rms = float(np.sqrt(np.mean(x**2))) or 1.0
        x *= 32768.0 * 10 ** (LEVEL_DBFS / 20) / rms
        pcm = pcm_to_bytes(np.clip(np.round(x), -32768, 32767).astype(np.int16))
        self._cache[text] = pcm
        return pcm
