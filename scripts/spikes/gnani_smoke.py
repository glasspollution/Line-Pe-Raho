"""M0 spike: minimal live round-trip against Gnani APIs (spends a few credits).

TTS REST (Timbre, 8 kHz raw PCM) -> STT REST (Prisma) on the same audio.
Saves scrubbed responses to backend/tests/fixtures/gnani/ so offline fakes match reality.

Run:  python scripts/spikes/gnani_smoke.py [--realtime]
Never prints the API key. Requires GNANI_API_KEY in .env.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import sys
import time
import wave
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "backend" / "tests" / "fixtures" / "gnani"
OUT = ROOT / "data" / "spikes"

TEXT = "Press 1 for recharge and billing. Press 2 for data issues."


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    env.update({k: v for k, v in os.environ.items() if k.startswith(("GNANI_", "TTS_"))})
    return env


def scrub(obj: Any, key: str) -> Any:
    text = json.dumps(obj, ensure_ascii=False)
    return json.loads(text.replace(key, "<REDACTED>"))


def pcm_to_wav(pcm: bytes, rate: int = 8000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def tts_rest(client: httpx.Client, base: str, env: dict[str, str]) -> bytes:
    body = {
        "text": TEXT,
        "voice": env.get("TTS_IVR_VOICE", "Riyaa"),
        "model": env.get("TTS_MODEL", "timbre-v3.0"),
        "language": "en-IN",
        "audio_config": {
            "sample_rate": 8000,
            "num_channels": 1,
            "sample_width": 2,
            "encoding": "linear_pcm",
            "container": "raw",
        },
    }
    t0 = time.perf_counter()
    r = client.post(f"{base}/api/v1/tts/inference", json=body)
    dt = time.perf_counter() - t0
    print(f"[tts] status={r.status_code} bytes={len(r.content)} ctype={r.headers.get('content-type')} "
          f"latency={dt:.2f}s chars={len(TEXT)}")
    if r.status_code != 200 or r.headers.get("content-type", "").startswith("application/json"):
        print("[tts] body:", r.text[:800])
        r.raise_for_status()
    return r.content


def stt_rest(client: httpx.Client, base: str, wav: bytes) -> dict[str, Any]:
    t0 = time.perf_counter()
    r = client.post(
        f"{base}/stt/v3",
        files={"audio_file": ("prompt.wav", wav, "audio/wav")},
        data={"language_code": "en-IN", "format": "transcribe"},
    )
    dt = time.perf_counter() - t0
    print(f"[stt-rest] status={r.status_code} latency={dt:.2f}s")
    try:
        payload: dict[str, Any] = r.json()
    except ValueError:
        payload = {"raw": r.text[:2000]}
    print("[stt-rest] body:", json.dumps(payload, ensure_ascii=False)[:800])
    return {"status": r.status_code, "latency_s": round(dt, 3), "body": payload}


async def stt_realtime(base_ws: str, key: str, pcm: bytes) -> list[dict[str, Any]]:
    import websockets

    headers = {
        "x-api-key-id": key,
        "lang_code": "en-IN",
        "x-sample-rate": "8000",
        "x-format": "transcribe",
        "x-vad-threshold": "0.7",
        "x-min-speech-ms": "200",
        "x-min-silence-ms": "500",
        "x-min-segment-ms": "120",
        "x-max-speech-s": "20",
    }
    msgs: list[dict[str, Any]] = []
    t_start = time.perf_counter()
    async with websockets.connect(f"{base_ws}/stt/v3/stream", additional_headers=headers) as ws:

        async def reader() -> None:
            async for raw in ws:
                m = json.loads(raw) if isinstance(raw, str) else {"binary": len(raw)}
                m["_rx_s"] = round(time.perf_counter() - t_start, 3)
                msgs.append(m)
                print("[stt-rt]", json.dumps(m, ensure_ascii=False)[:300])

        task = asyncio.create_task(reader())
        tail = b"\x00\x00" * 8000  # 1 s silence so VAD closes the segment
        audio = pcm + tail
        for i in range(0, len(audio) - 1023, 1024):
            await ws.send(audio[i : i + 1024])
            await asyncio.sleep(0.064)  # real-time cadence: 512 samples @ 8 kHz
        await asyncio.sleep(3.0)
        await ws.close()
        await asyncio.gather(task, return_exceptions=True)
    return msgs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--realtime", action="store_true", help="also stream to realtime STT")
    args = ap.parse_args()

    env = load_env()
    key = env.get("GNANI_API_KEY", "")
    if not key:
        print("GNANI_API_KEY missing in .env", file=sys.stderr)
        return 2
    base = env.get("GNANI_BASE_URL", "https://api.vachana.ai").rstrip("/")
    FIXTURES.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)

    with httpx.Client(headers={"X-API-Key-ID": key}, timeout=60) as client:
        pcm = tts_rest(client, base, env)
        (OUT / "tts_prompt_8k.pcm").write_bytes(pcm)
        wav = pcm_to_wav(pcm)
        (OUT / "tts_prompt_8k.wav").write_bytes(wav)
        print(f"[tts] duration={len(pcm) / 16000:.2f}s (assuming 8 kHz s16le)")
        rest = stt_rest(client, base, wav)

    result: dict[str, Any] = {
        "text_sent": TEXT,
        "tts": {"bytes": len(pcm), "duration_s": round(len(pcm) / 16000, 3)},
        "stt_rest": rest,
    }
    if args.realtime:
        ws_base = base.replace("https://", "wss://").replace("http://", "ws://")
        result["stt_realtime"] = asyncio.run(stt_realtime(ws_base, key, pcm))

    (FIXTURES / "smoke_roundtrip.json").write_text(
        json.dumps(scrub(result, key), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"saved {FIXTURES / 'smoke_roundtrip.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
