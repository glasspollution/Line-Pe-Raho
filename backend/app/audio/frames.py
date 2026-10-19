"""Audio frame plumbing (CLAUDE.md §6.1): rechunking, resampling, G.711 mu-law.

Canonical line audio: PCM s16le mono 8 kHz, 20 ms frames (320 bytes).
Prisma realtime wants exactly 1024-byte (64 ms) chunks, hence Rechunker.
"""

from __future__ import annotations

from math import gcd

import numpy as np
from scipy.signal import resample_poly

LINE_RATE = 8000
FRAME_SAMPLES = 160
FRAME_BYTES = FRAME_SAMPLES * 2
STT_CHUNK_BYTES = 1024


class Rechunker:
    """Re-slices an arbitrary byte stream into fixed-size chunks, preserving order."""

    def __init__(self, size: int) -> None:
        if size <= 0:
            raise ValueError("size must be positive")
        self.size = size
        self._buf = bytearray()

    def push(self, data: bytes) -> list[bytes]:
        self._buf += data
        n = len(self._buf) // self.size
        out = [bytes(self._buf[i * self.size : (i + 1) * self.size]) for i in range(n)]
        del self._buf[: n * self.size]
        return out

    def flush(self) -> bytes:
        """Return and clear whatever is left (shorter than one chunk)."""
        rest = bytes(self._buf)
        self._buf.clear()
        return rest


def resample(x: np.ndarray, src_hz: int, dst_hz: int) -> np.ndarray:
    """Polyphase resample int16 PCM. Applies the anti-aliasing filter built into resample_poly."""
    if src_hz == dst_hz:
        return x.astype(np.int16, copy=True)
    g = gcd(src_hz, dst_hz)
    y = resample_poly(x.astype(np.float64), dst_hz // g, src_hz // g)
    return np.clip(np.round(y), -32768, 32767).astype(np.int16)


def bytes_to_pcm(b: bytes) -> np.ndarray:
    return np.frombuffer(b, dtype="<i2")


def pcm_to_bytes(x: np.ndarray) -> bytes:
    return np.asarray(x, dtype="<i2").tobytes()


# G.711 mu-law (ITU-T reference algorithm: bias 0x84, clip 32635, bit-inverted output).
_BIAS = 0x84
_CLIP = 32635


def mulaw_encode(x: np.ndarray) -> bytes:
    s = x.astype(np.int32)
    sign = (s < 0).astype(np.int32)
    mag = np.minimum(np.abs(s), _CLIP) + _BIAS
    exponent = np.clip(np.floor(np.log2(mag)).astype(np.int32) - 7, 0, 7)
    mantissa = (mag >> (exponent + 3)) & 0x0F
    u = ~((sign << 7) | (exponent << 4) | mantissa) & 0xFF
    return u.astype(np.uint8).tobytes()


def mulaw_decode(b: bytes) -> np.ndarray:
    u = ~np.frombuffer(b, dtype=np.uint8).astype(np.int32) & 0xFF
    sign = u & 0x80
    exponent = (u >> 4) & 0x07
    mantissa = u & 0x0F
    mag = (((mantissa << 3) + _BIAS) << exponent) - _BIAS
    return np.where(sign != 0, -mag, mag).astype(np.int16)
