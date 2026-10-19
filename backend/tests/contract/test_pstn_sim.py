"""Contract: app.audio.pstn_sim (CLAUDE.md §6.2). Task T-103.

Pinned interface:
    NOMINAL_SIGNAL_DBFS: float = -20.0      # SNR is defined against this nominal speech level
    PSTNConfig(profile="clean"|"snr20"|"snr10"|"snr5", noise_type="street"|"babble"|"fan",
               packet_loss: float = 0.0, gain_db: float | None = None, seed: int = 0)
        gain_db=None -> random uniform ±3 dB per simulator instance (seeded)
    PSTNSimulator(cfg).process(frame: bytes) -> bytes      # 320-byte frames in, 320-byte frames out
        stateful across frames (filters keep state); works with no noise clips on disk
        (procedural noise fallback) so tests stay offline and deterministic.
    Processing order: gain -> additive noise -> bandpass -> mu-law encode/decode -> packet loss.
    Noise level is measured at the OUTPUT: NOMINAL_SIGNAL_DBFS - snr (calibrate after the bandpass).
"""

from __future__ import annotations

from types import ModuleType

import numpy as np
import pytest

from tests.contract.helpers import dbfs, frames, pcm, require, sine

pytestmark = pytest.mark.milestone("M1")


@pytest.fixture(scope="module")
def ps() -> ModuleType:
    return require("app.audio.pstn_sim", "T-103")


def run(ps: ModuleType, x: np.ndarray, **cfg: object) -> np.ndarray:
    sim = ps.PSTNSimulator(ps.PSTNConfig(**cfg))
    return np.concatenate([pcm(sim.process(f)) for f in frames(x)])


def test_frame_size_preserved(ps: ModuleType) -> None:
    sim = ps.PSTNSimulator(ps.PSTNConfig(profile="snr10", seed=1))
    assert all(len(sim.process(f)) == 320 for f in frames(sine(1000, 0.5)))


def test_nominal_level_constant(ps: ModuleType) -> None:
    assert ps.NOMINAL_SIGNAL_DBFS == pytest.approx(-20.0)


def test_bandpass_passes_voiceband_and_cuts_outside(ps: ModuleType) -> None:
    def level(freq: float) -> float:
        y = run(ps, sine(freq, 2.0, -20), profile="clean", gain_db=0.0, seed=0)
        return dbfs(y[8000:])  # skip filter settling

    assert level(1000) == pytest.approx(-20.0, abs=1.5)
    assert level(1000) - level(100) >= 20
    assert level(1000) - level(3900) >= 6


@pytest.mark.parametrize("profile,snr", [("snr20", 20), ("snr10", 10), ("snr5", 5)])
@pytest.mark.parametrize("noise_type", ["street", "babble", "fan"])
def test_noise_level_matches_profile(ps: ModuleType, profile: str, snr: int, noise_type: str) -> None:
    y = run(ps, np.zeros(8000 * 3, dtype=np.int16), profile=profile, noise_type=noise_type, gain_db=0.0, seed=3)
    assert dbfs(y) == pytest.approx(ps.NOMINAL_SIGNAL_DBFS - snr, abs=2.5)


def test_clean_profile_adds_no_noise(ps: ModuleType) -> None:
    y = run(ps, np.zeros(8000, dtype=np.int16), profile="clean", gain_db=0.0, seed=3)
    assert dbfs(y) < -70


def test_packet_loss_repeats_previous_frame(ps: ModuleType) -> None:
    rng = np.random.default_rng(5)
    x = rng.normal(0, 3000, 8000 * 40).astype(np.int16)  # 2000 frames, no natural repeats
    sim = ps.PSTNSimulator(ps.PSTNConfig(profile="clean", packet_loss=0.03, gain_db=0.0, seed=11))
    outs = [sim.process(f) for f in frames(x)]
    repeats = sum(outs[i] == outs[i - 1] for i in range(1, len(outs)))
    assert 30 <= repeats <= 100  # ~60 expected


def test_no_loss_means_no_repeats(ps: ModuleType) -> None:
    rng = np.random.default_rng(5)
    x = rng.normal(0, 3000, 8000 * 5).astype(np.int16)
    sim = ps.PSTNSimulator(ps.PSTNConfig(profile="clean", packet_loss=0.0, gain_db=0.0, seed=11))
    outs = [sim.process(f) for f in frames(x)]
    assert sum(outs[i] == outs[i - 1] for i in range(1, len(outs))) == 0


def test_random_gain_within_3db_and_seeded(ps: ModuleType) -> None:
    x = sine(1000, 1.0, -20)
    levels = [dbfs(run(ps, x, profile="clean", gain_db=None, seed=s)[4000:]) for s in range(12)]
    assert all(-23.5 <= v <= -16.5 for v in levels)
    assert max(levels) - min(levels) > 1.0  # actually random
    assert dbfs(run(ps, x, profile="clean", gain_db=None, seed=4)[4000:]) == pytest.approx(levels[4], abs=1e-6)


def test_mulaw_applied(ps: ModuleType) -> None:
    """Output values lie on the G.711 mu-law reconstruction grid (only 256 distinct levels)."""
    y = run(ps, sine(1000, 1.0, -10), profile="clean", gain_db=0.0, seed=0)
    assert len(np.unique(y)) <= 256
