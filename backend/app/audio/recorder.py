"""Time-aligned call recorder (CLAUDE.md §3, §12.6).

Tracks: caller (our agent's outbound), callee (company inbound), user (browser leg). Each write carries
the call time of the frame; gaps are filled with silence so sample N means the same instant in every
track. `close()` writes caller.wav, callee.wav, user.wav (8 kHz mono) and mixed.wav (sum, clipped).
Frames are streamed to disk, so long holds don't grow memory.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import soundfile as sf

from app.audio.frames import LINE_RATE

TRACKS = ("caller", "callee", "user")
_CHUNK = LINE_RATE * 10


class CallRecorder:
    def __init__(self, out_dir: Path) -> None:
        self.dir = out_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self._files = {t: sf.SoundFile(self.dir / f"{t}.wav", "w", LINE_RATE, 1, "PCM_16") for t in TRACKS}
        self._written = dict.fromkeys(TRACKS, 0)  # samples per track
        self.closed = False

    def write(self, track: str, frame: bytes, t: float) -> None:
        """Write a frame that STARTS at call time t (seconds)."""
        if self.closed:
            return
        if track not in self._files:
            raise ValueError(f"unknown track {track!r}")
        start = int(round(t * LINE_RATE))
        pos = self._written[track]
        x = np.frombuffer(frame, dtype="<i2")
        if start > pos:  # gap -> silence
            self._files[track].write(np.zeros(start - pos, dtype=np.int16))
            pos = start
        elif start < pos:  # overlap (late/duplicate frame) -> keep only the new tail
            x = x[pos - start :]
        if len(x):
            self._files[track].write(x)
            pos += len(x)
        self._written[track] = pos

    def duration_s(self) -> float:
        return max(self._written.values()) / LINE_RATE

    def close(self) -> None:
        if self.closed:
            return
        n = max(self._written.values())
        for t, f in self._files.items():  # pad every track to the same length
            if self._written[t] < n:
                f.write(np.zeros(n - self._written[t], dtype=np.int16))
            f.close()
        readers = [sf.SoundFile(self.dir / f"{t}.wav") for t in TRACKS]
        with sf.SoundFile(self.dir / "mixed.wav", "w", LINE_RATE, 1, "PCM_16") as mixed:
            while True:
                parts = [r.read(_CHUNK, dtype="int16") for r in readers]
                if not len(parts[0]):
                    break
                total = np.sum([p.astype(np.int32) for p in parts], axis=0)
                mixed.write(np.clip(total, -32768, 32767).astype(np.int16))
        for r in readers:
            r.close()
        self.closed = True
