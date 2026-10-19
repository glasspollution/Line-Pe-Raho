"""Shared helpers for contract tests.

Contract tests pin behaviour from CLAUDE.md before the code exists. Until a module is written its
tests SKIP with "NOT IMPLEMENTED: <module> [<task>]" (scripts/scoreboard.py lists these as the
to-do list). Once a test passes it is locked by the ratchet and must keep passing.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from types import ModuleType

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
SR = 8000
FRAME_BYTES = 320


def require(module: str, task: str) -> ModuleType:
    """Import a module under test, or skip as NOT IMPLEMENTED if it (or its package) doesn't exist yet.

    A missing *third-party* dependency inside an existing module is a real error, not a skip.
    """
    try:
        return importlib.import_module(module)
    except ModuleNotFoundError as e:
        missing = e.name or ""
        if module == missing or module.startswith(missing + "."):
            pytest.skip(f"NOT IMPLEMENTED: {module} [{task}]")
        raise


def frames(pcm: np.ndarray) -> list[bytes]:
    """Split int16 PCM into 320-byte line frames (zero-pad the tail)."""
    b = pcm.astype("<i2").tobytes()
    if len(b) % FRAME_BYTES:
        b += b"\x00" * (FRAME_BYTES - len(b) % FRAME_BYTES)
    return [b[i : i + FRAME_BYTES] for i in range(0, len(b), FRAME_BYTES)]


def pcm(b: bytes) -> np.ndarray:
    return np.frombuffer(b, dtype="<i2").astype(np.float64)


def dbfs(x: np.ndarray) -> float:
    rms = float(np.sqrt(np.mean(np.square(x.astype(np.float64))))) if len(x) else 0.0
    return 20 * np.log10(max(rms, 1e-9) / 32768.0)


def sine(freq: float, seconds: float, dbfs_level: float = -20.0, sr: int = SR) -> np.ndarray:
    t = np.arange(int(seconds * sr)) / sr
    amp = 32768.0 * 10 ** (dbfs_level / 20) * np.sqrt(2)  # RMS-referenced
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.int16)


def tone_level_dbfs(x: np.ndarray, freq: float, sr: int = SR) -> float:
    """Peak-referenced level of one sinusoid component via a windowed DFT bin."""
    n = len(x)
    w = np.hanning(n)
    spec = np.fft.rfft(x.astype(np.float64) * w)
    k = int(round(freq * n / sr))
    mag = float(np.max(np.abs(spec[max(k - 2, 0) : k + 3]))) * 2 / np.sum(w)
    return 20 * np.log10(max(mag, 1e-9) / 32768.0)
