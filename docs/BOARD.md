# Board

Legend:
- `[ ]` open, `[~]` claimed, `[x]` done
- `[owner]` needs the owner (keys, accounts, a human, or a judgment call)
- **Done when**: the tests or commands that must pass, as well as `scripts/check.py`

Claim protocol and priorities: docs/AGENT_LOOP.md. One task per commit or PR. Current mode: the overnight cloud session on branch `overnight/backend-pipeline`. Follow docs/OVERNIGHT.md first.

## Owner decisions
Resolved (2026-10-19):
- **D-1 Probe vs disclosure: (a).** The probe itself discloses AI (CLAUDE.md §10.3). Probe events carry `discloses_ai: true`; journal_check J4 enforces it.
- **D-2 Agents:** a single local session on the owner's machine. This board is the task list. Revisit when moving to cloud agents.
- **D-3 Detection latency:** measured from the END of the greeting segment; `detect_p50` gate ≤ 3 s.
- **D-5 Frontend components:** ElevenLabs UI and LiveKit Components are allowed as **copied-in visual components only**. No `@elevenlabs/*`, `livekit-client` or `@livekit/*` packages (guard-enforced).

- **D-4 Reasoning LLM:** not Evon. The challenge needs at least one Gnani model or API, and Prisma plus Timbre cover that. Use **Kimi K3 on Amazon Bedrock**:
  - endpoint: `us.moonshotai.kimi-k3`, us-east-1, OpenAI-compatible Chat Completions; short-lived tokens minted from the owner's AWS profile (`app/llm/auth.py`)
  - `reasoning_effort="none"` for real-time calls
  - Evon stays a config-only drop-in

## M0: Foundations and spikes (Oct 19–20)
- [x] T-001 Backend skeleton: `backend/app/{__init__,main,config}.py`, FastAPI `/api/health`, pydantic-settings for every §17 var plus all §8 and §10 thresholds. **Done when:** `test_config.py`.
- [x] T-002 `gnani/errors.py`: retry policy (429/500/503, 0.5→8 s, jitter, 5 tries, never 400) and a usage meter. **Done when:** unit tests with a fake transport, including "400 not retried" and the backoff sequence under a seeded RNG.
- [x] T-003 `gnani/tts.py`: REST client and PhraseCache (`data/tts_cache/{sha1}.pcm`), and the WS client. **Done when:** offline tests against fakes shaped like `fixtures/gnani/smoke_roundtrip.json`; a cache hit makes zero network calls.
- [x] T-004 `gnani/stt_rest.py`: multipart, ≤60 s guard, ITN flag. **Done when:** offline fake tests; a clip over 60 s is rejected before the network.
- [x] T-005 `gnani/stt_realtime.py`: raw websockets with §7.1 headers, 320→1024 B rechunk at 64 ms cadence, 14:00 rotation with 2 s overlap and dedupe, `update_settings`, and timestamps from a local sample counter. **Done when:**
  - tests run against a local fake WS server (loopback only)
  - they assert header names, frame sizes and real-time pacing (fake clock), rotation and dedupe
  - the sample-rate question in EVAL_LOOPS "Recorded API facts" is resolved, with the answer recorded there
- [ ] T-006 `gnani/stt_batch.py`: create/start/poll(≥10 s)/files/download, with 1 h URL expiry handling. **Done when:** fake-HTTP lifecycle test, including expiry and retry.
- [x] T-007 `llm/client.py`:
  - OpenAI-compatible (Bedrock Chat Completions)
  - `response_format` JSON schema from pydantic, one retry on invalid JSON
  - explicit `max_tokens`; `reasoning_effort` from config
  - journaled fallback; 4 s / 60 s timeouts **Done when:** tests against a local fake server cover valid, invalid-then-valid, timeout→fallback, and a journal entry on fallback.
- [ ] T-008 `[owner-budget]` M0 spike report. **Done when:** `eval/reports/m0/` holds round-trip WER per noise profile, credits per audio-minute for each API, and batch turnaround; CLAUDE.md §20 items ticked. Needs `LIVE_BUDGET_CREDITS`.
- [x] T-009 Bedrock access: verified via the owner's AWS profile; Kimi K3 inference profiles ACTIVE in us-east-1; tokens minted by `app/llm/auth.py`.

## M1: Line and simulator (Oct 21–23)
- [x] T-101 `audio/frames.py`. **Done when:** `test_frames.py`.
- [x] T-102 `audio/dtmf.py`. **Done when:** `test_dtmf.py` (all cases including PSTN; deps T-103).
- [x] T-103 `audio/pstn_sim.py`, with procedural street/babble/fan noise (WAV clip optional via `noise_clip`); record any clip licenses in `eval/noise/LICENSES.md`. **Done when:** `test_pstn_sim.py`.
- [x] T-104 `line/base.py` and `line/virtual.py` (20 ms asyncio clock, duplex queues through the PSTN sim). **Done when:** tests prove frame pacing with a fake clock, duplex delivery, DTMF via `send_audio`, and state transitions.
- [x] T-105 `company/trees/*.yaml` for nimbus_telecom, sahyog_bank and prakash_bijli_board. **Done when:** `test_ivr_trees.py`.
- [x] T-106 `company/engine.py`: IVR runtime, DTMF collect, timeouts/repeats, and `ask` nodes via REST STT (faked in tests). **Done when:** a scripted DTMF press-through of the full Nimbus tree over VirtualLine at snr10 reaches the hold node (M1 acceptance).
- [x] T-107 `company/hold_music.py`: seeded procedural music plus a vocal-like variant. **Done when:** deterministic output per seed and the expected spectral properties (music-like flatness), tested.
- [x] T-108 `audio/recorder.py`, `journal.py` and `scripts/replay.py`. **Done when:** a recorded fake call produces the 4 WAVs and a journal that passes `eval/journal_check.py`.
- [x] T-109 `/ws/company-agent` and basic console API. **Done when:** a WS loopback test sends console audio into the line.

## M2: Agent (Oct 24–27)
- [x] T-201 `agent/menu_parser.py` (rules; the `needs_llm` flag is set; wiring the actual LLM fallback call happens in T-202 once T-007 exists). **Done when:** `test_menu_parser.py` (68-case corpus and MenuBuffer).
- [~] T-202 (rules done: language, goal relevance, destructive-option guard, agent fallback; the LLM path waits for T-007) `agent/ivr_decider.py`: rules first, then the LLM fallback (faked), confidence ≥ 0.6. **Done when:** tests for the goal-keyword press, the agent preference when `needs_human`, the English language preference, and LLM fallback with a fake that includes low-confidence → repeat.
- [x] T-203 `audio/vad.py` and `audio/classifier.py` (32 tests; babble and real-recording tuning in eval). **Done when:** labels are correct on synthetic silence, tone (DTMF/ringback), procedural music (T-107) and TTS-speech fixtures; thresholds live in config.
- [x] T-204 `agent/human_detector.py` (+ `is_connecting_prompt`; every tree announcement is rejected). **Done when:** `test_human_detector.py`.
- [~] T-205 `agent/phrases.py` done; PhraseCache warmup at startup waits for T-003. **Done when:** `test_phrases.py`; startup warmup is tested with a fake TTS.
- [x] T-206 `agent/orchestrator.py` (+ `app/sim/`: oracle STT, scripted agent, harness; 8 full simulated calls pass J1–J7). Real STT/TTS wiring and the post-call hand-off come later. **Done when:**
  - scripted scenarios covering the happy path, probe path, silent agent, hold timeout, same menu 3× → fallback keys, and user not joining
  - each produces a journal that passes `journal_check --complete`
- [ ] T-207 `agent/bridge.py` and `/ws/user-audio`. **Done when:** a loopback WS test shows two-way audio, agent outbound muted while BRIDGED (J6), and STT continuing on the callee track.
- [ ] T-208 `company/auto_agent.py` (LLM + Timbre persona, ticket `NMB` + 8 digits, promised date) that writes ground truth. Needs T-009 for live use. **Done when:** tests run against fake LLM and TTS.
- [ ] T-209 `[owner-budget]` M2 acceptance: task → human → user joined on Nimbus at snr10, 3 of 3 live runs; the journals pass J1–J7.

## M3: Summary, UI, eval v1 (Oct 28–30)
- [ ] T-301 `postcall/pipeline.py`: batch → merge → ITN slices, with realtime segments as fallback. **Done when:** fake-client tests, including the batch-failure fallback.
- [ ] T-302 `postcall/extract.py`: LLM JSON matching the §12 schema, as a pydantic model. **Done when:** a fake LLM test and a schema-violation retry test.
- [ ] T-303 `postcall/verify.py`. **Done when:** `test_verify.py`.
- [ ] T-304 REST and WS API per §14, with SQLModel tables per §13. **Done when:** httpx AsyncClient tests for every route; `/ws/events` replays a journal.
- [ ] T-305 Frontend scaffold: Vite, React, TS, Tailwind and shadcn/ui; mock mode replaying `eval/fixtures/golden_call.jsonl`; the required npm scripts; Playwright and axe. **Done when:** `scripts/check.py` runs the frontend steps green (docs/FRONTEND.md "Definition of done").
- [ ] T-306 NewTask page. T-307 LiveCall page. T-308 Summary page. T-309 CompanyConsole. T-310 EvalReport. **Done when:** each page's e2e specs in docs/FRONTEND.md pass, and `/ui-check` shows no P0 or P1 issues.
- [ ] T-311 `eval/run_eval.py` v1: the scenario YAMLs and matrix, `metrics.csv` with all required columns, `journal_check` per run, and charts. **Done when:** a `--matrix smoke` run (fake STT and TTS, about 2 min) produces a scorecard that passes the schema check. The M3 acceptance needs 5 auto-agent runs with correct ticket IDs.

## M4: Eval and demo (Oct 31–Nov 2)
- [ ] T-401 `[owner-budget]` Full matrix run; commit `eval/reports/baseline/`; replace PROVISIONAL gates with measured values in their own PR.
- [ ] T-402 Improvement loop on the worst scorecard rows (repeat as needed).
- [ ] T-403 UI polish pass against `docs/design/inspiration/`.
- [ ] T-404 `[owner]` Demo recording and public post.

## M5: Hardening and stretch (Nov 3–9)
- [ ] T-501 Bug bash from the critic, eval and UI notes.
- [ ] T-502 S1 agent-speaks mode (only once the MVP is green). It must never agree to payments, and J4 still applies.
- [ ] T-503 `[owner]` Submission write-up.
