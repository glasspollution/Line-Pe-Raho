"""VirtualLine: pacing, ringback, duplex delivery, DTMF through the PSTN sim, drain, hangups."""

from __future__ import annotations

import asyncio

import numpy as np

from app.audio.dtmf import DTMFDetector
from app.audio.frames import FRAME_BYTES
from app.audio.pstn_sim import PSTNConfig, PSTNSimulator
from app.line.clock import FakeClock, RealClock
from app.line.virtual import SILENCE, TICK_S, VirtualLine, ringback_frame


class Recorder:
    """Far-end double: records caller audio, plays a scripted list of frames, can hang up."""

    def __init__(self, answer_after_s: float = 0.0, script: list[bytes] | None = None,
                 hangup_after_ticks: int | None = None) -> None:
        self.answer_after_s = answer_after_s
        self.script = list(script or [])
        self.got: list[bytes] = []
        self.connected_at: float | None = None
        self.hung_up_at: float | None = None
        self._hangup_after = hangup_after_ticks
        self._ended = False
        self.dtmf = DTMFDetector()
        self.keys: list[str] = []

    def on_connect(self, t: float) -> None:
        self.connected_at = t

    def receive(self, frame: bytes, t: float) -> None:
        self.got.append(frame)
        self.keys += self.dtmf.feed(frame)
        if self._hangup_after is not None and len(self.got) >= self._hangup_after:
            self._ended = True

    def next_frame(self, t: float) -> bytes:
        return self.script.pop(0) if self.script else SILENCE

    def on_hangup(self, t: float) -> None:
        self.hung_up_at = t

    @property
    def ended(self) -> bool:
        return self._ended


def frame(v: int) -> bytes:
    return np.full(160, v, dtype="<i2").tobytes()


async def collect(line: VirtualLine, n: int) -> list[bytes]:
    out: list[bytes] = []
    async for f in line.inbound():
        out.append(f)
        if len(out) >= n:
            break
    return out


async def test_ringback_then_connect_and_state() -> None:
    far = Recorder(answer_after_s=1.0)
    line = VirtualLine(lambda _: far, clock=FakeClock())
    assert line.state == "idle"
    await line.dial("nimbus_telecom")
    assert line.state == "ringing"
    ring = await collect(line, 50)  # 1.0 s of ringback
    assert any(f != SILENCE for f in ring[:20]) and ring[25] == SILENCE  # 0.4 on, 0.2 off cadence
    await line.wait_connected()
    assert line.state == "connected" and abs((far.connected_at or 0) - 1.0) < 1e-9
    await line.hangup()
    assert line.state == "ended" and far.hung_up_at is not None


async def test_duplex_exact_without_sim_and_silence_fill() -> None:
    far = Recorder(script=[frame(7), frame(8)])
    line = VirtualLine(lambda _: far, clock=FakeClock())
    await line.dial("x")
    await line.send_audio(frame(1))
    await line.send_audio(frame(2))
    got = await collect(line, 4)
    assert got[:2] == [frame(7), frame(8)] and got[2] == SILENCE
    assert far.got[:3] == [frame(1), frame(2), SILENCE]
    await line.hangup()


async def test_fake_clock_pacing_is_exact() -> None:
    clock = FakeClock()
    line = VirtualLine(lambda _: Recorder(), clock=clock)
    await line.dial("x")
    await collect(line, 100)
    # the pump may be at most a couple of ticks ahead of the reader; line time trails clock time by at most one tick
    assert 100 * TICK_S - 1e-9 <= clock.now() <= 102 * TICK_S + 1e-9
    assert 0 <= clock.now() - line.line_time() <= TICK_S + 1e-9  # fake clock jumps to the next tick, then yields
    await line.hangup()


async def test_real_clock_pacing() -> None:
    line = VirtualLine(lambda _: Recorder(), clock=RealClock())
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await line.dial("x")
    await collect(line, 25)  # 0.5 s
    elapsed = loop.time() - t0
    await line.hangup()
    assert 0.45 <= elapsed <= 0.8, elapsed


async def test_dtmf_reaches_far_end_through_pstn_snr10() -> None:
    far = Recorder()
    up = PSTNSimulator(PSTNConfig(profile="snr10", noise_type="babble", packet_loss=0.01, gain_db=0.0, seed=3))
    down = PSTNSimulator(PSTNConfig(profile="snr10", seed=4))
    line = VirtualLine(lambda _: far, uplink=up, downlink=down, clock=FakeClock())
    await line.dial("x")
    await line.send_dtmf("9000000001#")
    await line.drain()
    await collect(line, 10)  # a few more ticks so the last tone is fully received
    assert "".join(far.keys) == "9000000001#"
    assert all(len(f) == FRAME_BYTES for f in far.got)
    await line.hangup()


async def test_drain_waits_for_playback() -> None:
    clock = FakeClock()
    line = VirtualLine(lambda _: Recorder(), clock=clock)
    await line.dial("x")
    for _ in range(10):
        await line.send_audio(frame(3))
    reader = asyncio.create_task(collect(line, 1000))
    await line.drain()
    assert clock.now() >= 10 * TICK_S - 1e-9
    await line.hangup()
    await reader


async def test_far_end_hangup_ends_line_and_inbound() -> None:
    far = Recorder(hangup_after_ticks=5)
    line = VirtualLine(lambda _: far, clock=FakeClock())
    await line.dial("x")
    got = await collect(line, 1000)
    assert len(got) == 5 and line.state == "ended"
    await line.send_audio(frame(1))  # no-op after end
    await line.hangup()  # idempotent


def test_ringback_frame_shape() -> None:
    assert len(ringback_frame(0.1)) == FRAME_BYTES and ringback_frame(0.5) == SILENCE
