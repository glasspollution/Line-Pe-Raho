"""Contract: app.audio.frames (CLAUDE.md §6.1). Task T-101.

Pinned interface:
    Rechunker(size: int).push(data: bytes) -> list[bytes]; .flush() -> bytes
    resample(x: np.ndarray[int16], src_hz: int, dst_hz: int) -> np.ndarray[int16]
    mulaw_encode(x: np.ndarray[int16]) -> bytes ; mulaw_decode(b: bytes) -> np.ndarray[int16]
"""

from __future__ import annotations

from types import ModuleType

import numpy as np
import pytest

from tests.contract.helpers import dbfs, require, sine

pytestmark = pytest.mark.milestone("M1")


@pytest.fixture(scope="module")
def fr() -> ModuleType:
    return require("app.audio.frames", "T-101")


def test_rechunk_320_to_1024_exact(fr: ModuleType) -> None:
    rc = fr.Rechunker(1024)
    out: list[bytes] = []
    for i in range(16):
        out += rc.push(bytes([i]) * 320)
    assert [len(c) for c in out] == [1024] * 5
    assert rc.flush() == b""


def test_rechunk_preserves_order_and_tail(fr: ModuleType) -> None:
    rng = np.random.default_rng(0)
    data = rng.integers(0, 256, 5000, dtype=np.uint8).tobytes()
    rc = fr.Rechunker(1024)
    out: list[bytes] = []
    pos = 0
    for size in [1, 319, 320, 1500, 7, 2853]:
        out += rc.push(data[pos : pos + size])
        pos += size
    tail = rc.flush()
    assert all(len(c) == 1024 for c in out)
    assert b"".join(out) + tail == data
    assert len(tail) == 5000 % 1024


@pytest.mark.parametrize("src,dst", [(8000, 16000), (16000, 8000), (48000, 8000), (8000, 48000)])
def test_resample_length_and_pitch(fr: ModuleType, src: int, dst: int) -> None:
    x = sine(1000, 1.0, -12, sr=src)
    y = fr.resample(x, src, dst)
    assert y.dtype == np.int16
    assert abs(len(y) - dst) <= dst * 0.01
    spec = np.abs(np.fft.rfft(y.astype(np.float64)))
    peak_hz = np.argmax(spec) * dst / len(y)
    assert abs(peak_hz - 1000) < 10
    assert abs(dbfs(y) - dbfs(x)) < 1.0


def test_mulaw_roundtrip_quality(fr: ModuleType) -> None:
    x = sine(440, 0.5, -20)
    enc = fr.mulaw_encode(x)
    assert isinstance(enc, bytes | bytearray) and len(enc) == len(x)
    y = fr.mulaw_decode(bytes(enc))
    assert y.dtype == np.int16 and len(y) == len(x)
    err = x.astype(np.float64) - y.astype(np.float64)
    snr = 10 * np.log10(np.mean(x.astype(np.float64) ** 2) / max(np.mean(err**2), 1e-9))
    assert snr > 30


def test_mulaw_silence_stays_quiet(fr: ModuleType) -> None:
    y = fr.mulaw_decode(fr.mulaw_encode(np.zeros(160, dtype=np.int16)))
    assert int(np.max(np.abs(y))) <= 8
