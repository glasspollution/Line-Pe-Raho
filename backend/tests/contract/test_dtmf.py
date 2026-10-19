"""Contract: app.audio.dtmf (CLAUDE.md §6.4). Tasks T-102 (+ T-103 for the noisy cases).

Pinned interface:
    generate(digits: str, sample_rate: int = 8000) -> np.ndarray[int16]
        120 ms tone + 80 ms gap per digit (trailing gap optional), each tone at -8 dBFS.
    DTMFDetector(sample_rate: int = 8000).feed(frame: bytes) -> list[str]
        Returns each newly detected key once per press. Valid: both tones over threshold,
        twist < 8 dB, sustained >= 60 ms.
"""

from __future__ import annotations

from types import ModuleType

import numpy as np
import pytest

from tests.contract.helpers import SR, frames, require, sine, tone_level_dbfs

pytestmark = pytest.mark.milestone("M1")

KEYS = "0123456789*#"
LOW = {"1": 697, "2": 697, "3": 697, "4": 770, "5": 770, "6": 770, "7": 852, "8": 852, "9": 852,
       "*": 941, "0": 941, "#": 941}
HIGH = {"1": 1209, "2": 1336, "3": 1477, "4": 1209, "5": 1336, "6": 1477, "7": 1209, "8": 1336, "9": 1477,
        "*": 1209, "0": 1336, "#": 1477}


@pytest.fixture(scope="module")
def dtmf() -> ModuleType:
    return require("app.audio.dtmf", "T-102")


def detect(dtmf: ModuleType, pcm: np.ndarray) -> str:
    det = dtmf.DTMFDetector(8000)
    out: list[str] = []
    for f in frames(np.concatenate([pcm, np.zeros(SR // 5, dtype=np.int16)])):
        out += det.feed(f)
    return "".join(out)


def test_timing(dtmf: ModuleType) -> None:
    for n in (1, 4, 11):
        x = dtmf.generate("5" * n)
        assert x.dtype == np.int16
        ms = len(x) / SR * 1000
        assert ms in (pytest.approx(n * 200, abs=2), pytest.approx(n * 200 - 80, abs=2)), ms


@pytest.mark.parametrize("key", list(KEYS))
def test_tone_frequencies_and_level(dtmf: ModuleType, key: str) -> None:
    x = dtmf.generate(key)[: int(0.12 * SR)]
    for f in (LOW[key], HIGH[key]):
        assert tone_level_dbfs(x, f) == pytest.approx(-8.0, abs=1.0)
    assert int(np.max(np.abs(x))) < 32767  # no clipping


def test_roundtrip_clean_all_keys(dtmf: ModuleType) -> None:
    assert detect(dtmf, dtmf.generate(KEYS)) == KEYS


def test_roundtrip_mobile_number_with_terminator(dtmf: ModuleType) -> None:
    assert detect(dtmf, dtmf.generate("9000000001#")) == "9000000001#"


def test_short_tone_rejected(dtmf: ModuleType) -> None:
    x = dtmf.generate("7")[: int(0.040 * SR)]  # 40 ms < 60 ms minimum
    assert detect(dtmf, x) == ""


def test_twist_over_8db_rejected(dtmf: ModuleType) -> None:
    t = np.arange(int(0.12 * SR)) / SR
    lo = 0.4 * 32767 * np.sin(2 * np.pi * 697 * t)
    hi = 0.4 * 32767 * 10 ** (-12 / 20) * np.sin(2 * np.pi * 1209 * t)  # 12 dB twist
    assert detect(dtmf, (lo + hi).astype(np.int16)) == ""


@pytest.mark.parametrize("signal", ["silence", "white_noise", "single_tone_440", "speechlike_am"])
def test_no_false_digits(dtmf: ModuleType, signal: str) -> None:
    rng = np.random.default_rng(1)
    n = 5 * SR
    if signal == "silence":
        x = np.zeros(n, dtype=np.int16)
    elif signal == "white_noise":
        x = (rng.normal(0, 3000, n)).astype(np.int16)
    elif signal == "single_tone_440":
        x = sine(440, 5.0, -10)
    else:
        t = np.arange(n) / SR  # voiced-speech-like: 130 Hz harmonic series, 4 Hz syllable envelope
        carrier = sum(np.sin(2 * np.pi * 130 * k * t) / k for k in range(1, 25))
        x = (4000 * carrier * (0.5 + 0.5 * np.sin(2 * np.pi * 4 * t))).astype(np.int16)
    assert detect(dtmf, x) == ""


@pytest.mark.parametrize("profile", ["clean", "snr20", "snr10"])
@pytest.mark.parametrize("loss", [0.0, 0.01])
def test_roundtrip_through_pstn(dtmf: ModuleType, profile: str, loss: float) -> None:
    sim_mod = require("app.audio.pstn_sim", "T-103")
    sim = sim_mod.PSTNSimulator(sim_mod.PSTNConfig(profile=profile, noise_type="street", packet_loss=loss,
                                                   gain_db=0.0, seed=7))
    digits = "9000000001#"
    det = dtmf.DTMFDetector(8000)
    got: list[str] = []
    for f in frames(np.concatenate([dtmf.generate(digits), np.zeros(SR // 5, dtype=np.int16)])):
        got += det.feed(sim.process(f))
    assert "".join(got) == digits
