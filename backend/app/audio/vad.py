"""webrtcvad wrapper for 8 kHz 20 ms frames.

Measured 2026-10-20: webrtcvad (mode 2) marks hold music and snr10 line noise as ~100% speech, so it is
an informational feature only; the classifier decides speech vs music on spectral flux (see classifier.py).
It remains useful for gating decisions on clean lines and as a cheap "anything there?" signal.
"""

from __future__ import annotations

import webrtcvad

from app.audio.frames import FRAME_BYTES, LINE_RATE


class VAD:
    def __init__(self, mode: int = 2) -> None:
        self._vad = webrtcvad.Vad(mode)

    def is_speech(self, frame: bytes) -> bool:
        if len(frame) != FRAME_BYTES:
            raise ValueError(f"expected {FRAME_BYTES}-byte frames")
        return bool(self._vad.is_speech(frame, LINE_RATE))

    def speech_ratio(self, pcm: bytes) -> float:
        frames = [pcm[i : i + FRAME_BYTES] for i in range(0, len(pcm) - FRAME_BYTES + 1, FRAME_BYTES)]
        if not frames:
            return 0.0
        return sum(self.is_speech(f) for f in frames) / len(frames)
