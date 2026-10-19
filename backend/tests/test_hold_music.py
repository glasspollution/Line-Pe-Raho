"""Procedural hold music: deterministic, sustained at a sane level, seamless loop, vocal variant voice-like."""

from __future__ import annotations

import numpy as np
import pytest

from app.company.hold_music import HoldMusic, parse_name, render_loop


def dbfs(x: np.ndarray) -> float:
    return float(20 * np.log10(max(np.sqrt(np.mean(x.astype(np.float64) ** 2)), 1e-9) / 32768))


def band_fraction(x: np.ndarray, lo: float, hi: float, sr: int = 8000) -> float:
    spec = np.abs(np.fft.rfft(x.astype(np.float64))) ** 2
    f = np.fft.rfftfreq(len(x), 1 / sr)
    return float(spec[(f >= lo) & (f < hi)].sum() / spec.sum())


def test_deterministic_per_seed_and_distinct_across_seeds() -> None:
    a, b, c = render_loop(7), render_loop(7), render_loop(8)
    assert np.array_equal(a, b)
    assert not np.array_equal(a[: len(c)], c[: len(a)])


@pytest.mark.parametrize("vocal", [False, True])
def test_level_and_sustained_energy(vocal: bool) -> None:
    x = render_loop(3, vocal)
    assert dbfs(x) == pytest.approx(-24.0, abs=0.5)
    assert int(np.max(np.abs(x))) < 32767
    win = 4000  # 500 ms windows, as the classifier uses
    levels = [dbfs(x[i : i + win]) for i in range(0, len(x) - win, win)]
    assert min(levels) > -45.0, "music must stay above the -45 dBFS 'sustained energy' line"


def test_loop_is_seamless() -> None:
    x = render_loop(5).astype(np.float64)
    typical = float(np.median(np.abs(np.diff(x))))
    wrap = abs(x[0] - x[-1])
    assert wrap < 20 * typical + 200


def test_vocal_variant_puts_more_energy_in_speech_band() -> None:
    plain, vocal = render_loop(3, False), render_loop(3, True)
    assert band_fraction(vocal, 300, 1200) > band_fraction(plain, 300, 1200)


def test_streaming_frames_wrap() -> None:
    hm = HoldMusic.from_name("procedural_seed_7")
    frames = [hm.next_frame() for _ in range(1700)]  # 34 s > one loop
    assert all(len(f) == 320 for f in frames)
    assert frames[0] != frames[1]


def test_names() -> None:
    assert parse_name("procedural_seed_7") == (7, False)
    assert parse_name("procedural_vocal_3") == (3, True)
    with pytest.raises(ValueError):
        parse_name("some_real_song.mp3")
