"""AudioClassifier on synthetic silence, DTMF, ringback, procedural hold music, synthetic speech and a real
Timbre TTS clip, clean and through the PSTN simulator (T-203 acceptance)."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from app.audio.classifier import AudioClassifier, WindowResult
from app.audio.dtmf import generate
from app.audio.pstn_sim import PSTNConfig, PSTNSimulator
from app.company.hold_music import render_loop
from app.company.voice import SyntheticVoice
from app.line.virtual import ringback_frame

FIX = Path(__file__).parent / "fixtures" / "audio"


def _timbre() -> np.ndarray:
    x = sf.read(FIX / "timbre_ivr_prompt_8k.wav", dtype="int16")[0]
    return np.tile(x, 3)


def _voice() -> np.ndarray:
    text = "press one for recharge and billing press two for data issues press three for new connection"
    return np.frombuffer(SyntheticVoice().render(text), dtype="<i2")


SOURCES = {
    "silence": lambda: np.zeros(8000 * 6, dtype=np.int16),
    "dtmf": lambda: generate("900000000190000"),
    "music": lambda: render_loop(7)[: 8000 * 12],
    "music_vocal": lambda: render_loop(3, True)[: 8000 * 12],
    "timbre_speech": _timbre,
    "synth_speech": _voice,
}
EXPECT = {"silence": "silence", "dtmf": "tone", "music": "music", "music_vocal": "music",
          "timbre_speech": "speech", "synth_speech": "speech"}
# minimum share of 500 ms windows with the expected raw label
MIN_ACC = {"silence": 1.0, "dtmf": 0.8, "music": 0.7, "music_vocal": 0.7, "timbre_speech": 0.8, "synth_speech": 0.9}


def run(x: np.ndarray, profile: str) -> list[WindowResult]:
    b = x.astype("<i2").tobytes()
    b += b"\x00" * ((-len(b)) % 320)
    sim = PSTNSimulator(PSTNConfig(profile=profile, noise_type="street", gain_db=0.0, seed=1)) if profile != "raw" else None
    clf = AudioClassifier()
    out = []
    for i in range(0, len(b), 320):
        fr = b[i : i + 320]
        r = clf.feed(sim.process(fr) if sim else fr, i / 16000)
        if r:
            out.append(r)
    return out


@pytest.mark.parametrize("profile", ["raw", "snr20", "snr10"])
@pytest.mark.parametrize("name", list(SOURCES))
def test_window_labels(name: str, profile: str) -> None:
    res = run(SOURCES[name](), profile)
    c = Counter(r.label for r in res)
    acc = c[EXPECT[name]] / len(res)
    assert acc >= MIN_ACC[name], dict(c)


@pytest.mark.parametrize("profile", ["raw", "snr10"])
@pytest.mark.parametrize("name", ["music", "music_vocal"])
def test_music_becomes_sustained_within_2_5_s(name: str, profile: str) -> None:
    res = run(SOURCES[name](), profile)
    first = next(r.t_end for r in res if r.sustained_music)
    assert first <= 2.5
    later = [r.sustained_music for r in res if r.t_end > 3.0]
    assert sum(later) / len(later) >= 0.9


@pytest.mark.parametrize("profile", ["raw", "snr10"])
@pytest.mark.parametrize("name", ["timbre_speech", "synth_speech", "silence", "dtmf"])
def test_non_music_never_sustained(name: str, profile: str) -> None:
    assert not any(r.sustained_music for r in run(SOURCES[name](), profile))


def test_ringback_on_windows_are_tone() -> None:
    ring = np.concatenate([np.frombuffer(ringback_frame(i * 0.02), dtype="<i2") for i in range(300)])
    labels = [r.label for r in run(ring, "raw")]
    assert set(labels) <= {"tone", "silence"} and "tone" in labels


def test_music_fraction_window_query() -> None:
    clf = AudioClassifier()
    music = render_loop(7)[: 8000 * 4].astype("<i2").tobytes()
    for i in range(0, len(music), 320):
        clf.feed(music[i : i + 320], i / 16000)
    assert clf.music_fraction(1.0, 4.0) >= 0.6
    assert clf.music_fraction(10.0, 12.0) == 0.0
