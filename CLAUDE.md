# Line Pe Raho — Project Spec (context file for Claude Code)

An AI agent that calls a company's customer-care line for the user. It navigates the IVR, waits on hold, detects when a live human agent picks up, discloses itself, and bridges the user into the call. Afterwards it produces a call summary in which every field links to the audio clip it came from.

Built for the Gnani AI "Great Indian AI Internship Challenge".
- Final submission deadline: Nov 10, 2026, 23:59 IST.
- The public demo post must be live around Nov 2, so engagement can accrue before the cutoff.

---

## 0. Hard constraints (never violate)

1. **Gnani models carry all speech.**
   - All speech-to-text uses **Gnani Prisma v2.5**.
   - All text-to-speech uses **Gnani Timbre** (v3.0 preferred, v2.5 allowed).
   - No other STT/TTS provider anywhere in the call path.
2. **Reasoning uses an OpenAI-compatible LLM chosen in config** (D-4).
   - Current choice: **Kimi K3 on Amazon Bedrock** (`us.moonshotai.kimi-k3`, us-east-1, Chat Completions). Auth uses short-lived bearer tokens minted from a local AWS profile (`LLM_AUTH=bedrock_profile`, `AWS_PROFILE` in `.env`).
   - The challenge only requires at least one Gnani model or API; Prisma (STT) and Timbre (TTS) carry all speech. Gnani Evon v3.3 stays an optional drop-in if hosting becomes available.
   - The LLM client must be provider-agnostic: base_url, model and key come from config.
   - Any fallback endpoint must be logged in the call journal.
3. **Synthetic data only.**
   - Never call real companies during development or demo.
   - Never use real phone numbers, account numbers, Aadhaar, PAN, or recordings of real people.
   - All demo calls go to the **mock company simulator** (§11).
4. **Use fictional brands only.** Examples: `Nimbus Telecom`, `Sahyog Bank`, `Prakash Bijli Board`. Never imitate a real company's name, IVR script, or branding.
5. **AI disclosure.** The agent must say it is an AI assistant calling on someone's behalf before saying anything else to a human.
6. **Secrets.** API keys come from `.env` only. Never commit or log keys.

---

## 1. Scope

### MVP (must ship)
- **Task creation.** The user describes the problem in text or voice, in Hinglish, Hindi or English. The system builds a `CallPlan`.
- **Outbound call over the virtual phone line** (§6) to the mock company, with PSTN degradation applied.
- **IVR navigation.** Transcribe prompts, parse menu options, choose an option, and send in-band DTMF. Also handle "say your query" voice prompts and enter synthetic account data via DTMF.
- **Hold handling.** Detect hold music and recorded announcements, and stay on the line.
- **Human detection**, including a liveness probe, to separate a live agent from recordings.
- **Disclosure and keep-alive.** Pre-synthesized disclosure line, then keep-alive phrases until the user joins.
- **User notification and bridge.** The user joins from the browser with two-way audio.
- **Post-call summary with receipts.** Every extracted field links to `[start, end]` in the recording and is checked by a verifier.
- **Mock company simulator.** YAML IVR trees, hold music, announcements, a human-agent console, and an LLM auto-agent mode.
- **Evaluation harness.** Scenario suite × noise levels, producing a metrics CSV and charts.

### Stretch (only after MVP passes acceptance)
- **S1 Agent-speaks mode.** If the user doesn't join within the timeout, the agent explains the issue itself.
  - It never agrees to payments and never shares data beyond the task profile.
  - It asks for a ticket number and callback.
- **S2 Real telephony adapter (Exotel).** Same `PhoneLine` interface (§6.3). Must not be used for the demo.
- **S3 Summary read-back.** Spoken summary to the user in their language via Timbre.

### Non-goals
- Multiple concurrent calls.
- User accounts or auth beyond a single local user.
- Native mobile apps.
- Payments.
- Calling real businesses.

---

## 2. User flow

1. **New task.** The user types or speaks, e.g. "Nimbus ne mera recharge do baar kaat liya, refund chahiye". The user also picks a company from the mock directory and adds synthetic profile fields (registered mobile, account ID).
2. **Plan.** The LLM produces a `CallPlan` (§9.1). The UI shows it, the user confirms, and the call starts.
3. **Live call screen.** A timeline of events:
   - dialing
   - IVR prompt heard (with transcript)
   - option chosen, with reason and DTMF
   - on hold (timer, announcements count)
   - human detected
4. **Human detected.**
   - The agent plays the cached disclosure.
   - The UI rings loudly and shows a full-screen "Agent mil gaya — Join call" button.
   - The browser sends a notification if the tab is hidden.
5. **User joins.** Two-way audio is bridged. The agent goes silent and keeps transcribing.
6. **Call ends.** The summary page shows the complaint/ticket ID, what was promised, deadlines, agent name, and next steps.
   - Each field has a ▶ button that plays the source clip.
   - Unverified fields carry a ⚠ badge.

---

## 3. Architecture

```
┌────────────── Browser (React) ──────────────┐   ┌──── Browser: /company console ────┐
│ Task UI · Live timeline · Join call · Summary│   │ Agent desk (teammate) · Scenario  │
└───────▲───────────────┬──────────────────────┘   └───────▲──────────────┬────────────┘
        │ WS events      │ WS audio (user leg)              │ WS audio      │ REST
┌───────┴───────────────▼──────────────────────────────────┴──────────────▼────────────┐
│ FastAPI backend (Python 3.11, asyncio)                                                │
│                                                                                        │
│  CallOrchestrator ── state machine (§8)                                                │
│     ├─ STTStream (Prisma realtime WS, 8 kHz)  ◄── inbound audio (callee track)         │
│     ├─ AudioClassifier (VAD + music/hold heuristics) ◄── inbound audio                 │
│     ├─ MenuParser + IVRDecider (rules → LLM)                                           │
│     ├─ DTMFGenerator (in-band tones) ──► outbound audio                                │
│     ├─ TTSClient (Timbre WS, raw PCM 8 kHz) + PhraseCache ──► outbound audio           │
│     ├─ HumanDetector (+ liveness probe)                                                │
│     ├─ Bridge (user leg ⇄ line)                                                        │
│     └─ Recorder (caller.wav, callee.wav, user.wav, mixed.wav) + EventJournal (JSONL)   │
│                                                                                        │
│  PhoneLine interface ── VirtualLine (MVP) | ExotelLine (S2)                            │
│     VirtualLine ⇄ PSTNSimulator ⇄ MockCompany (IVR engine, hold, agent leg)            │
│                                                                                        │
│  PostCall: Prisma Batch (diarization+denoise) → REST ITN on slices → LLM extract       │
│            → Verifier → Summary                                                        │
│  Eval: scenario runner (headless, auto-agent) → metrics.csv + charts                   │
└────────────────────────────────────────────────────────────────────────────────────────┘
        │                                │
   Gnani APIs (api.vachana.ai)      LLM: Kimi K3 on Bedrock (OpenAI-compatible; Evon optional)
```

**Key design choices:**
- **Separate tracks.** We own the line, so the caller (agent) and callee (company) tracks are recorded separately. STT and classifiers run on the **callee track only**, which means the agent's own speech never reaches them.
- **Telephony-shaped audio.** The internal line audio format matches real telephony streams (8 kHz s16le mono). Swapping VirtualLine for ExotelLine is therefore an adapter, not a rewrite.
- **In-band DTMF everywhere.** Works on the virtual line and in Exotel bidirectional streams.

---

## 4. Repo layout

```
line-pe-raho/
  CLAUDE.md                 # this file
  .env.example
  pyproject.toml            # backend deps, ruff, pytest config
  backend/
    app/
      main.py               # FastAPI app, routers, startup (phrase cache warmup)
      config.py             # pydantic-settings, reads .env
      models.py             # SQLModel tables (§13)
      api/
        tasks.py            # REST: tasks, plans, calls, summaries
        ws.py               # WS: /ws/events/{call_id}, /ws/user-audio/{call_id}, /ws/company-agent/{call_id}
        company.py          # REST: mock company scenarios, console
      audio/
        frames.py           # AudioFrame, rechunker, resample (8k⇄16k⇄48k), mu-law codec
        dtmf.py             # generator + Goertzel detector
        vad.py              # webrtcvad wrapper
        classifier.py       # music/silence/speech/announcement heuristics
        recorder.py
        pstn_sim.py         # bandpass, mu-law, noise, packet loss
      gnani/
        stt_realtime.py     # raw websockets client (VAD headers, reconnect at 14 min)
        stt_rest.py         # ≤60 s clips, ITN
        stt_batch.py        # create/start/poll/files/download
        tts.py              # WS realtime + REST, PhraseCache
        errors.py           # retry/backoff policy
      llm/
        client.py           # OpenAI-compatible, JSON-schema output, provider fallback
        prompts/            # *.md prompt templates (§9)
        schemas.py          # pydantic models for every LLM output
      agent/
        orchestrator.py     # state machine (§8)
        menu_parser.py      # rules (§9.2) + LLM fallback
        ivr_decider.py
        human_detector.py
        bridge.py
        phrases.py          # disclosure/keep-alive text per language
      line/
        base.py             # PhoneLine protocol (§6.3)
        virtual.py
        exotel.py           # S2 only
      company/
        engine.py           # IVR tree runtime
        trees/*.yaml        # mock companies
        hold_music.py       # procedural music generator
        auto_agent.py       # LLM + Timbre simulated human agent
      postcall/
        pipeline.py
        extract.py
        verify.py
      journal.py            # JSONL event journal
      sim/                  # offline simulation: oracle STT, scripted human agent, full-call harness (FakeClock)
    tests/
      contract/             # spec-pinning tests; skip as NOT IMPLEMENTED until the module exists (docs/EVAL_LOOPS.md)
      fixtures/             # menu_prompts.yaml, human_detector_cases.yaml, gnani/ (scrubbed real responses)
  frontend/                 # Vite + React + TS + Tailwind
    src/
      pages/ NewTask.tsx LiveCall.tsx Summary.tsx CompanyConsole.tsx EvalReport.tsx
      audio/ capture-worklet.ts playback-worklet.ts
      lib/ ws.ts api.ts
  eval/
    scenarios/*.yaml
    noise/                  # noise clips (licensed for reuse; record license in eval/noise/LICENSES.md)
    run_eval.py
    reports/                # reports/baseline/ = scorecard all changes are compared against
    thresholds.yaml         # L2 gates/targets
    scorecard.py            # metrics.csv -> pass/fail + worst-first improvement list
    journal_check.py        # journal invariants J1–J7 (incl. AI-disclosure-first)
    fixtures/               # golden_call.jsonl, golden_summary.json, state_graph.yaml
    tests/
  scripts/
    check.py                # L0 gate — the one command before every commit
    guard_constraints.py    # §0 static guard (+ guard_rules.py, synthetic_allowlist.txt)
    ratchet.py              # passing tests must keep passing (eval/ratchet.json)
    scoreboard.py           # per-milestone pass / fail / not-implemented
    spikes/                 # M0 live probes (spend credits; never in default tests)
    tests/
  docs/                     # EVAL_LOOPS, AGENT_LOOP, BOARD, REVIEW_RUBRIC, FRONTEND, design/inspiration/
  .claude/skills/           # iterate, critic, ui-check
  .github/workflows/ci.yml  # runs scripts/check.py on every PR
  conftest.py               # blocks network in tests; live marker; milestone tagging
  THIRD_PARTY.md
  data/                     # gitignored: sqlite db, recordings, journals, tts cache
```

---

## 5. Tech stack

**Backend:**
- Python 3.11, FastAPI, uvicorn, `websockets` (≥13; use `additional_headers`), httpx
- pydantic v2, pydantic-settings, SQLModel + SQLite
- numpy, scipy (filters, resample_poly), soundfile, webrtcvad
- `rapidfuzz` (fuzzy matching), `openai` SDK (for the OpenAI-compatible LLM endpoint)
- `gnani-vachana` only for REST/batch convenience. **Do not** use it for realtime STT: the SDK doesn't expose the VAD headers.

**Frontend:** Vite, React 18, TypeScript, Tailwind. AudioWorklet for capture and playback.

**LLM:** Kimi K3 on Amazon Bedrock (D-4). Optional Evon self-hosting: vLLM ≥0.12 on an A100/H100, `--trust-remote-code`.

**Tooling:** ruff, mypy (non-strict), pytest + pytest-asyncio, matplotlib (eval charts).

---

## 6. Audio and the phone line

### 6.1 Formats
| Where | Format |
|---|---|
| Line (internal canonical) | PCM s16le, mono, **8000 Hz**, 20 ms frames = 160 samples = **320 bytes** |
| Prisma realtime input | PCM s16le mono 8000 Hz, **exactly 1024-byte frames** (512 samples = 64 ms), sent at real-time cadence. Rechunk 320 → 1024 B; never burst. |
| Timbre output for the line | `audio_config = {sample_rate: 8000, num_channels: 1, sample_width: 2, encoding: "linear_pcm", container: "raw"}` |
| Browser | Capture 48 kHz float32 in a worklet → server resamples to 8 kHz. Playback: server sends 8 kHz s16le → worklet upsamples. `getUserMedia({echoCancellation: true, noiseSuppression: false})` |
| Recordings | WAV 8 kHz per track + mixed |

### 6.2 PSTN simulator (`audio/pstn_sim.py`)
Applied in **both** directions on VirtualLine. Configured per call or scenario:
- **Bandpass:** 300–3400 Hz (4th-order Butterworth).
- **Codec:** G.711 μ-law encode → decode.
- **Additive noise:** a noise clip at the target SNR. Profiles: `clean` (no noise), `snr20`, `snr10`, `snr5`. Noise types: street, babble, fan/AC.
- **Packet loss:** drop 20 ms frames with probability `p ∈ {0, 0.01, 0.03}`. Conceal by repeating the previous frame.
- **Gain:** random ±3 dB per call.

### 6.3 `PhoneLine` protocol (`line/base.py`)
```python
class PhoneLine(Protocol):
    async def dial(self, target: str) -> None            # target = company id (virtual) or E.164 (exotel)
    async def send_audio(self, frame: bytes) -> None     # 320-byte 8 kHz frame, caller → callee
    def inbound(self) -> AsyncIterator[bytes]            # 320-byte frames, callee → caller
    async def send_dtmf(self, digits: str) -> None       # in-band tones via send_audio
    async def hangup(self) -> None
    @property
    def state(self) -> Literal["idle","ringing","connected","ended"]
```
- **VirtualLine:** an in-process duplex queue pair through the PSTN simulator to `company.engine`. Pacing is driven by a 20 ms asyncio clock.
- **ExotelLine (S2):**
  - Placed via Exotel Connect API: `POST https://<key>:<token>@api.in.exotel.com/v1/Accounts/<sid>/Calls/connect` with `StreamUrl=<our wss>` and `StreamType=bidirectional`.
  - Audio is base64 16-bit 8 kHz PCM (or μ-law by default; set MediaFormat `audio/L16;rate=8000`).
  - Events: connected/start/media/dtmf/stop/mark/clear.
  - DTMF is sent in-band.
  - Verify all of this against Exotel docs before implementing.

### 6.4 DTMF (`audio/dtmf.py`)
- **Generator:** standard dual tones; row 697/770/852/941 Hz × column 1209/1336/1477 Hz. Covers keys 0–9, `*` and `#`.
  - Tone 120 ms, gap 80 ms, amplitude −8 dBFS per tone.
  - Multi-digit strings (e.g., mobile number + `#`) are sent sequentially.
- **Detector** (used by the mock company): Goertzel on 8 kHz frames.
  - Valid digit: both tones > threshold and twist < 8 dB, sustained ≥ 60 ms.
  - Must still detect digits after the PSTN simulator at `snr10`.

---

## 7. Gnani API usage (exact)

Base: `https://api.vachana.ai`. Auth header: `X-API-Key-ID: <key>` (header name is case-insensitive on WS).

### 7.1 Prisma realtime STT (`gnani/stt_realtime.py`)
- **Endpoint:** `wss://api.vachana.ai/stt/v3/stream`. All config goes in upgrade headers.
- **Headers we send:**
  - `x-api-key-id`
  - `lang_code` (default `en-IN`; it accepts Hindi-English mixed speech and outputs Latin script)
  - `x-sample-rate: 8000`
  - `x-format: transcribe` (ITN: digits, ₹, dates)
  - **IVR VAD preset:** `x-vad-threshold: 0.7`, `x-min-speech-ms: 200`, `x-min-silence-ms: 500`, `x-min-segment-ms: 120`, `x-max-speech-s: 20`
  - **Word boosting:** `x-bias-list` = JSON array of single letters-only words, with `x-bias-score: 1.0`. Use brand words, `recharge`, `complaint`, `refund`, `dabaiye`, `press`, `agent`, `executive`. Non-ASCII words go in as `\u` escapes.
- **Server messages:** `connected`, `processing`, `transcript {text, audio_duration_ms, segment_id, segment_index, latency}`, `error`, `settings_updated`.
  - **Do not rely on partial transcripts.**
- **Mid-session update:** `{"type":"update_settings","bias":{"list":[...],"score":1.0}}`. Used when the IVR mentions new terms.
- **Session cap:** 15 min. Proactively open a new session at **14:00** and overlap sessions by 2 s; dedupe by text + time.
- **Language switch:** headers are fixed per connection. To switch `en-IN` → `hi-IN`, reconnect.
  - Default policy: choose the English option in IVR language menus. Hindi is only used if no English option exists.
- **Timestamps:** keep a local sample counter. Each transcript segment gets `t_end ≈` the sample time when `processing` arrived and `t_start = t_end - audio_duration_ms`. This is used to align segments with recordings.

### 7.2 Prisma REST STT (`gnani/stt_rest.py`)
- `POST /stt/v3`, multipart.
- Fields: `audio_file` (≤60 s, ideal ≤30 s), `language_code`, `format=transcribe` for ITN, optional `bias_list` + `bias_score` (works with `format=verbatim` only).
- Used for:
  - (a) Voice task input from the user.
  - (b) **ITN pass on number-bearing slices** in post-call, because Batch has no ITN.

### 7.3 Prisma Batch STT (`gnani/stt_batch.py`)
- **Create:** `POST /stt/v3/batch/jobs` multipart.
  - `config` JSON: `{"model":"gnani-prisma-v2.5","language_code":"en-IN,hi-IN","mode":"transcribe","with_diarization":true,"num_speakers":3,"with_denoise":true,"bias_list":[...]}` plus `files`.
- **Start:** `POST /stt/v3/batch/jobs/{id}/start`. Jobs do not start on their own.
- **Poll:** `GET /stt/v3/batch/jobs/{id}` every **≥10 s** until a terminal status.
- **Files:** `GET /stt/v3/batch/jobs/{id}/files?status=COMPLETED` → `transcript_url`, which **expires after 1 h**. Download immediately.
- **Result:** `segments[{start_time, end_time, text, speaker_id}]` and `full_transcript`.
- **Limits:**
  - Upload ≤10 MB per file and ≤100 files.
  - Language ID allows up to 3 codes; the first code is the fallback; the decision is per file.
  - **No ITN.** Gujarati and Punjabi are not supported on batch.

### 7.4 Timbre TTS (`gnani/tts.py`)
- **Realtime:** `wss://api.vachana.ai/api/v1/tts`.
  - Send one request at a time: `{text, voice, model, language, speed, audio_config}`.
  - Receive `start`, `audio {chunk_index, audio(base64), is_final}`, `complete`, `error`.
  - Use for the dynamic agent speech in S1.
- **REST:** `POST /api/v1/tts/inference` → binary audio. Use it to build the PhraseCache.
- **Defaults:** model `timbre-v3.0`; agent voice `Shivani` or `Avinash`; mock IVR voice a *different* v3 voice (e.g., `Riyaa`); auto-agent voices rotate across the remaining voices.
  - `language`: `en-IN` | `hi-IN` | `hi-en` (Hinglish) | `auto`.
  - `speed`: 0.85–1.15.
- **Text rules:**
  - Write numbers, IDs and dates as spoken words. `utils/spoken.py` converts digits to words digit-by-digit for IDs, and to Indian-system words for amounts.
  - Audio tags (`<pauses>`, etc.) are v3.0 only.
- **PhraseCache:** at startup, pre-synthesize every fixed phrase (§10.3) per language and store raw 8 kHz PCM under `data/tts_cache/{sha1(model|voice|lang|text)}.pcm`. The disclosure must play with **0 network latency**.

### 7.5 Reliability (`gnani/errors.py`)
- Retry 429/500/503 with exponential backoff and jitter: 0.5 → 8 s, max 5 tries. Never retry 400.
- **Credit meter:** log audio-seconds sent to STT, characters sent to TTS, and batch minutes per call in the journal. Expose totals at `GET /api/usage`.
- **Tests:** use recorded fixtures (`tests/fixtures/gnani/*.json`) and fake clients. CI must not spend credits.

---

## 8. Call orchestrator state machine (`agent/orchestrator.py`)

```
IDLE → PLANNING → DIALING → IVR ⇄ HOLD → HUMAN_SUSPECTED → PROBING → HUMAN_CONFIRMED
     → DISCLOSING → WAITING_FOR_USER → BRIDGED → ENDED → SUMMARIZING → DONE
                                    ↘ (timeout) AGENT_SPEAKS (S1) → ENDED
any state → FAILED (fatal error / max duration)
```

| State | Entry action | Exit conditions |
|---|---|---|
| DIALING | `line.dial()`; start Recorder, STTStream, Classifier | connected → IVR; no answer 45 s → FAILED |
| IVR | Collect transcript segments into `MenuBuffer` | menu complete (§9.2) → decide & press; voice prompt → speak `spoken_intent`; data prompt → send DTMF from profile; hold indicators → HOLD; human evidence → HUMAN_SUSPECTED |
| HOLD | Start hold timer; count announcements | speech segment not matching known announcements/prompts → HUMAN_SUSPECTED; new menu → IVR |
| HUMAN_SUSPECTED | Score with HumanDetector (§10.2) | score ≥ 0.85 → HUMAN_CONFIRMED; 0.5–0.85 → PROBING; < 0.5 → back to HOLD/IVR |
| PROBING | Play cached probe, which itself discloses AI (§10.3, D-1) | responsive human speech within 4 s → HUMAN_CONFIRMED; none → previous state |
| DISCLOSING | Play cached disclosure in the detected language; notify user (event `human_detected`) | playback done → WAITING_FOR_USER |
| WAITING_FOR_USER | Keep-alive phrase every 12 s, max 6 | user audio connected → BRIDGED; 90 s → AGENT_SPEAKS (S1) or polite request for callback + ticket number then ENDED |
| BRIDGED | Agent outbound muted; user ⇄ line; STT continues on callee track | either side hangs up / user ends → ENDED |
| ENDED | Hang up; finalize recordings | → SUMMARIZING |

**Global limits:**
- IVR phase ≤ 6 min.
- Hold ≤ 45 min (configurable).
- Total call ≤ 60 min.
- Max IVR depth 8.
- Same menu heard 3× → press the configured fallback (`0`, then `*`, then `9`), then FAILED.

**Event emission:** every transition and every decision emits a typed event (§14.2) to the journal and the WebSocket.

---

## 9. Reasoning components

### 9.1 CallPlan (LLM, JSON schema)
```json
{
  "company_id": "nimbus_telecom",
  "goal": "refund for duplicate recharge deduction",
  "goal_keywords": ["recharge","refund","complaint","billing"],
  "needs_human": true,
  "spoken_intent": {"en-IN": "duplicate recharge refund", "hi-IN": "recharge refund"},
  "profile_fields_needed": ["registered_mobile"],
  "issue_summary_for_agent": "<=2 sentences, used in AGENT_SPEAKS and summary>",
  "user_language": "hi-en"
}
```
- **Inputs:** the user text (or REST STT transcript), the company directory entry, and the profile.
- Validate with pydantic. On failure, re-ask once with the error message, then fall back to a rule-built plan.

### 9.2 MenuParser (rules first, LLM fallback)
**Normalize:**
- lowercase
- strip punctuation
- number words → digits: `one…nine, zero, oh, ek, do, teen, char/chaar, paanch, chhe/chah, saat, aath, nau, shunya`
- `star|asterisk` → `*`, `hash|pound` → `#`

**Extraction patterns (all on normalized Latin text):**
- `press (\d|\*|#) (for|to) (?P<label>.+?)`
- `(for|to) (?P<label>.+?),? press (\d|\*|#)`
- `(?P<label>.+?) ke liye (\d|\*|#) (dabaiye|dabayen|press karein)`
- `(\d|\*|#) dabaiye (?P<label>.*)`
- Devanagari input (hi-IN sessions) is transliterated to Latin first (`indic-transliteration`), then the same patterns apply.

**Output:** `MenuOption{digit, label, segment_id}`.

**Menu complete when any of these holds:**
- (a) ≥ 2.5 s with no new transcript after the last option
- (b) the first option repeats
- (c) a phrase matches `to repeat|dobara sunne` ("to hear again")

**LLM fallback:** if rules find 0 options but the text contains `press|dabaiye|option|select`, call the LLM with the menu text. It returns the options as JSON.

**Data prompts:** `enter|darj karein|type` + `mobile|account|number` → `DataPrompt{field, terminator: "#"|None}`.

**Voice prompts:** `tell us|say|boliye|batayein` + `query|problem|reason` → `VoicePrompt`.

### 9.3 IVRDecider
1. **Deterministic first.**
   - If an option label fuzzy-matches (`rapidfuzz.token_set_ratio ≥ 85`) any `goal_keywords`, press it.
   - If `needs_human` and a label matches `agent|executive|customer care|pratinidhi|baat karein`, prefer that.
   - Language menus: press English if present, else Hindi.
2. **Otherwise ask the LLM.**
   - Input: `{goal, options[], path_so_far[]}`.
   - Output: `{action: "press"|"wait"|"repeat", digit?, reason, confidence}`.
   - Press only if confidence ≥ 0.6, else `repeat`.
3. **Latency budget:** decision + first DTMF tone ≤ 1.5 s after menu-complete. Early press is allowed when the deterministic match is ≥ 95.

### 9.4 LLM call conventions (`llm/client.py`)
- **OpenAI-compatible** `chat.completions` with `temperature=0.6`, `top_p=0.95`, and **`max_tokens` always set** (Bedrock reserves quota for the maximum when it is unset).
- **Reasoning:**
  - Kimi K3 reasons at `max` effort by default. Send `reasoning_effort="none"` for all real-time decisions, and `llm_reasoning_effort_postcall` (default `low`) for post-call extraction.
  - With Evon, the equivalent is `extra_body={"chat_template_kwargs":{"enable_thinking": false}}`.
- **Structured output:** use `response_format` with a JSON schema generated from the pydantic model when the provider supports it. Still validate with pydantic.
- **System prompt** always names the output language/script and says "Output JSON only."
- **Output handling:** parse with pydantic. On invalid JSON, retry once with the error; else fall back to rules.
- **Provider:** Amazon Bedrock. `app/llm/auth.py` mints 1-hour bearer tokens from `AWS_PROFILE` (SigV4 presign, local). `LLM_AUTH=api_key` switches to a static key for any other provider.
  - Optional Evon: model repo `gnani/gnani-evon-v3.3-30B-A3B`, run with `vllm serve <repo> --trust-remote-code --max-model-len 32768 --served-model-name evon`; then point `LLM_BASE_URL` / `LLM_MODEL` at it.
- **Timeouts:** 4 s for real-time decisions, 60 s for post-call.
- **Journal:** log model, latency, and prompt hash for every call.

---

## 10. Hold and human detection

### 10.1 AudioClassifier (`audio/classifier.py`), per 500 ms window on the callee track
**Features:**
- RMS dBFS
- webrtcvad speech ratio (mode 2, 20 ms frames)
- spectral flatness
- spectral flux variance
- harmonic ratio via autocorrelation

**Labels:** `silence | music | speech | tone`. `tone` covers DTMF and ringback.
- `music` = sustained energy (> −45 dBFS for ≥ 2 s) **and** speech ratio < 0.3 **and** stable harmonic content.
- Thresholds live in config and are tuned with the eval harness.

### 10.2 HumanDetector (`agent/human_detector.py`)
Score each new callee speech segment from 0 to 1.

**Positive evidence:**
- (+0.35) Greeting or self-introduction patterns: `hello|namaste|good (morning|afternoon|evening)|main .* bol (raha|rahi) hoon|this is \w+|how (may|can) i help|kaise madad`.
- (+0.25) Segment is **not** a fuzzy match (≥ 80) to any prompt or announcement heard earlier in the call, or to the company's known-phrase list.
- (+0.20) Turn-taking: a question ends, then ≥ 1.2 s of silence follows (a human waiting for an answer).
- (+0.10) Classifier shows speech with no music in the 3 s before.
- (+0.10) The segment arrives after an IVR "connecting you to an agent" prompt.

**Negative evidence:**
- (−0.5) Matches a hold/announcement lexicon: `call is important|aapki call humare liye|please stay on the line|kripya line par bane rahein|all our executives are busy|sabhi pratinidhi vyast`.
- (−0.3) Music under speech.

**Escalation:** the LLM is called only for scores in 0.5–0.85 and only when no probe is possible. It returns `{is_live_human, confidence}`.

**Liveness probe:** the most reliable signal. A recording does not answer "Hello?". It never fires during an IVR menu.

**Target:** time from the **end** of the agent's greeting segment to confirmation ≤ 3 s (D-3); false human alarms ≤ 1 per hour of hold.

### 10.3 Fixed phrases (PhraseCache), per language `en-IN`, `hi-IN`, `hi-en`
- **Disclosure:** "Namaste, main ek AI assistant hoon aur {user_first_name} ji ki taraf se call kar raha hoon. Main unhe abhi line par jod raha hoon, kripya ek pal rukiye."
  - Pre-synthesize per user name at task start.
- **Keep-alive (rotate 3):**
  - "Bas ek second, woh line par aa rahe hain."
  - "Thank you for waiting, they are joining now."
  - "Kripya line par bane rahiye."
- **Probe** (discloses AI, so §0.5 holds even if a human answers it; D-1): "Hello? Main ek AI assistant hoon, {user_first_name} ji ki taraf se call kar raha hoon. Kya aap mujhe sun pa rahe hain?"
- **Fallback when the user doesn't join:** "Woh abhi uplabdh nahi hain. Kya aap mujhe complaint number bata sakte hain, taaki woh aapse baad mein sampark kar sakein?"

### 10.4 Credit-saving hold gate (config `HOLD_STT_MODE`)
- `stream_all` (default for MVP): stream everything to Prisma.
- `gated`:
  - While in HOLD with label `music`, close the Prisma session.
  - Reopen it when webrtcvad speech ratio > 0.6 for ≥ 300 ms.
  - Replay a 1.5 s pre-roll ring buffer at real-time cadence.
  - Measure accuracy and latency cost in the eval before enabling.

---

## 11. Mock company simulator (`company/`)

### 11.1 IVR tree YAML
```yaml
id: nimbus_telecom
display_name: Nimbus Telecom
voice: { model: timbre-v3.0, name: Riyaa, language: hi-en }
root: welcome
nodes:
  welcome:
    say: "Nimbus Telecom mein aapka swagat hai. For English press 1. Hindi ke liye 2 dabaiye."
    options: { "1": main_en, "2": main_hi }
    timeout_s: 8
    on_timeout: repeat        # repeat | goto:<node> | hangup
    max_repeats: 2
  main_en:
    say: "Press 1 for recharge and billing. Press 2 for data issues. Press 3 for new connection. Press 9 to repeat."
    options: { "1": billing_en, "2": data_en, "3": newconn_en, "9": main_en }
  billing_en:
    collect: { field: registered_mobile, digits: 10, terminator: "#", prompt: "Please enter your 10 digit registered mobile number followed by hash." }
    next: billing_menu_en
  billing_menu_en:
    say: "For duplicate deduction or refund, press 2. To speak to our customer care executive, press 0."
    options: { "2": queue_refund, "0": queue_refund }
  queue_refund:
    hold:
      duration_s: { min: 60, max: 600 }      # scenario may override
      music: procedural_seed_7
      announcement_every_s: 35
      announcements:
        - "Aapki call humare liye mahatvapurn hai. Kripya line par bane rahein."
        - "All our executives are busy. Please stay on the line."
    then: agent                # agent = human console or auto-agent
```
- `say` audio is pre-synthesized via Timbre into a cache.
- Voice prompts are supported with `ask: {expect: speech, keywords: [...]}`. They transcribe via Prisma REST and route by keyword.
- **Ship 3 companies:** `nimbus_telecom`, `sahyog_bank`, `prakash_bijli_board`. They must vary in:
  - depth (3–6 levels)
  - language order
  - a voice-prompt node
  - an "agent connecting" prompt vs none
  - announcements that begin with "Hello"

### 11.2 Hold music (`hold_music.py`)
- Procedural only, so there are no copyright issues: a seeded chord progression of sine/saw pads plus soft percussion, 8 kHz.
- Include one variant with a hummed/vocal-like melody to stress the speech detector.

### 11.3 Agent leg
- **Human console mode:** `/company` page. A teammate accepts the call and talks via the browser mic. The ringing → accept → greet flow mirrors real agents.
- **Auto-agent mode** (eval + solo testing): the LLM plays the agent; Timbre gives it a different voice.
  - Persona and greeting are sampled from a list. It asks for a mobile number, issues a fictional ticket ID (e.g., `NMB` + 8 digits), and promises a resolution date.
  - **Ground truth** (greeting time, ticket ID, promised date) is written to the scenario result for scoring.

### 11.4 Scenario control
`POST /api/company/scenario` sets:
- `company_id`
- hold duration
- noise profile
- packet loss
- agent mode
- whether the agent greets first or waits silently (the silent agent tests the liveness probe)

---

## 12. Post-call pipeline (`postcall/`)
1. **Batch STT** on `callee.wav` and `user.wav`, with diarization, denoise, and the bias list. Agent speech is already known from our TTS text and timestamps; no STT needed.
2. **Merge** into one timeline: `Segment{id, track, speaker, start, end, text}`, using realtime segments as a fallback if batch fails.
3. **ITN pass.** For segments with number-like content (regex on digits or number words), cut `[start-0.3, end+0.3]` and send to REST STT with `format=transcribe`. Store as `text_itn`.
4. **Extract (LLM):**
```json
{
  "ticket_id": {"value": "NMB48213377", "evidence": [{"segment_id": "s42", "quote": "..."}]},
  "agent_name": {...},
  "resolution": {...},
  "promised_by": {"value": "2026-11-05", "evidence": [...]},
  "amounts": [{"value": 239, "currency": "INR", "evidence": [...]}],
  "next_steps": [{"value": "...", "evidence": [...]}],
  "outcome": "resolved|ticket_raised|callback_promised|unresolved"
}
```
5. **Verify** (`verify.py`, deterministic). Each field gets `verified: bool` and `reason`.
   - Each `quote` must fuzzy-match (≥ 90) its segment text or `text_itn`.
   - IDs and amounts must appear literally in `text_itn` of a cited segment.
   - Dates must parse.
   - Unverified fields show ⚠ in the UI. Never silently drop them.
6. **Clips.** The summary API returns `{start, end}` per evidence. The frontend plays `mixed.wav` from `start-0.5` to `end+0.5`.

---

## 13. Data model (SQLModel / SQLite)
- `Task(id, created_at, raw_text, raw_audio_path?, user_language, company_id, profile_json, plan_json, status)`
- `Call(id, task_id, started_at, ended_at, final_state, line_kind, noise_profile, hold_seconds, ivr_seconds, time_to_human_s?, recording_dir)`
- `Segment(id, call_id, track, source["realtime"|"batch"], speaker?, start, end, text, text_itn?)`
- `Decision(id, call_id, t, kind["menu_press","data_entry","voice_reply","probe","disclosure"], input_json, output_json, method["rule"|"llm"], latency_ms)`
- `Summary(call_id, json, verified_count, unverified_count)`
- `Usage(call_id, stt_rt_seconds, stt_rest_seconds, stt_batch_seconds, tts_chars, llm_calls)`

Journal: `data/calls/{call_id}/journal.jsonl`, append-only, one event per line.

---

## 14. Backend API

### 14.1 REST
| Method | Path | Purpose |
|---|---|---|
| POST | `/api/tasks` | `{text? , audio? (multipart), company_id, profile}` → Task + CallPlan |
| POST | `/api/tasks/{id}/call` | start call → `{call_id}` |
| POST | `/api/calls/{id}/hangup` | end call |
| GET | `/api/calls/{id}` | call state + decisions |
| GET | `/api/calls/{id}/summary` | summary with evidence + verification |
| GET | `/api/calls/{id}/audio/{track}` | wav (`mixed`, `callee`, `caller`, `user`) |
| GET | `/api/companies` | mock directory |
| POST | `/api/company/scenario` | simulator settings |
| GET | `/api/usage` | credit meter totals |

### 14.2 WebSocket
- **`/ws/events/{call_id}`** (server → client JSON). Every event has `{type, t, call_id, ...}`.
  - Types: `state_changed`, `transcript`, `menu_parsed`, `decision`, `dtmf_sent`, `hold_started`, `announcement_heard`, `human_suspected`, `probe_sent`, `human_detected`, `keepalive_sent`, `user_joined`, `call_ended`, `summary_ready`, `error`.
- **`/ws/user-audio/{call_id}`** (binary, bidirectional). Client sends 48 kHz float32 frames; the server sends 8 kHz s16le. Enabled only from WAITING_FOR_USER.
- **`/ws/company-agent/{call_id}`** (binary, bidirectional). For the human console.

---

## 15. Frontend
- **NewTask:**
  - Textarea plus a mic button. The mic records ≤ 30 s and sends it to the backend, which runs Prisma REST.
  - Company picker and synthetic profile fields, pre-filled with fake values and labelled "demo data".
  - Plan preview card, then a Start call button.
- **LiveCall:**
  - Vertical timeline of events with transcript snippets, decision reason and method (rule/LLM), and a hold timer.
  - Big status pill (IVR / On hold mm:ss / Agent found).
  - On `human_detected`: full-screen join prompt, ringtone, and Notification API alert.
  - Join opens the audio worklets. Mute and hang-up controls.
- **Summary:**
  - Outcome badge and field cards, each with value, ▶ clip, ⚠ if unverified, and the evidence quote.
  - Full transcript with track colors.
  - Usage panel.
- **CompanyConsole (`/company`):**
  - Incoming call card with accept, talk via mic, and end.
  - Scenario controls (§11.4).
  - Live view of the IVR node currently active.
- **EvalReport (`/eval`):** renders `eval/reports/latest/*.png` and the metrics table.
- **Styling:** mobile-first; the user flow must work at 375 px width.

---

## 16. Evaluation harness (`eval/`)

**Scenario YAML:**
```yaml
id: nimbus_refund_snr10
company_id: nimbus_telecom
task_text: "Nimbus ne recharge do baar kaata, refund chahiye"
expected_path: ["1", "1", "<mobile>#", "2"]
hold_s: 180
noise: { profile: snr10, type: street, packet_loss: 0.01 }
agent: { mode: auto, greets_first: true }
```

**Matrix:**
- 3 companies × 4 noise profiles (`clean`, `snr20`, `snr10`, `snr5`) × 2 agent greeting modes, with `hold_s` reduced to 60 for speed.
- Plus adversarial cases:
  - announcement starting with "Hello"
  - vocal hold music
  - silent agent
  - IVR with no English option

**Metrics per run (`metrics.csv`):**
- `reached_human`
- `correct_queue` (path vs expected)
- `wrong_presses`
- `ivr_seconds`
- `time_to_human_detect_s` (from the **end** of the auto-agent's ground-truth greeting; D-3)
- `false_human_alarms`
- `probe_used`
- `summary_ticket_correct`, `summary_date_correct`
- `prompt_wer`: Prisma WER on IVR prompts. Ground truth is free because we synthesized the prompts. Compute per noise profile with `jiwer` after the same normalization as §9.2.
- `credits` (from the usage meter)

**Charts:**
- `prompt_wer` vs noise
- `task_success` vs noise
- `time_to_human_detect` distribution
- false alarms per hour

Run: `python eval/run_eval.py --matrix default --parallel 1`. Results go to `eval/reports/{timestamp}/`.

---

## 17. Configuration (`.env.example`)
```
GNANI_API_KEY=
GNANI_BASE_URL=https://api.vachana.ai
STT_LANG_DEFAULT=en-IN
TTS_MODEL=timbre-v3.0
TTS_AGENT_VOICE=Shivani
TTS_IVR_VOICE=Riyaa
LLM_BASE_URL=https://bedrock-runtime.us-east-1.amazonaws.com/openai/v1   # Kimi K3 on Bedrock (D-4)
LLM_MODEL=us.moonshotai.kimi-k3
LLM_AUTH=bedrock_profile                   # bedrock_profile (token from AWS_PROFILE) | api_key
AWS_PROFILE=                               # local AWS CLI profile; never commit
AWS_REGION=us-east-1
LLM_API_KEY=                               # only for LLM_AUTH=api_key
LLM_FALLBACK_BASE_URL=                     # optional; usage is journaled
LLM_FALLBACK_MODEL=
HOLD_STT_MODE=stream_all                   # stream_all | gated
MAX_HOLD_MIN=45
USER_JOIN_TIMEOUT_S=90
AGENT_SPEAKS_ENABLED=false                 # S1
LINE_KIND=virtual                          # virtual | exotel (S2)
EXOTEL_SID= EXOTEL_KEY= EXOTEL_TOKEN= EXOTEL_CALLER_ID=
DATA_DIR=./data
```

---

## 18. Observability and testing
- **Journal:** every event as JSONL. `scripts/replay.py <call_id>` re-renders a call timeline in the terminal.
- **Unit tests (offline, no credits):**
  - DTMF round-trip through `pstn_sim` at every profile
  - menu parser on 60+ Hindi/English/Hinglish prompt strings
  - human-detector scoring tables
  - verifier
  - state machine transitions with fake STT/TTS/LLM
- **Integration tests** (`-m live`, spend credits, run manually):
  - TTS → PSTN sim → realtime STT round-trip
  - batch job lifecycle
  - one full scenario
- **Latency logging:** STT transcript latency, decision latency, DTMF send time, disclosure start delay.

---

## 19. Milestones (today = Oct 18, 2026)
| Milestone | Dates | Deliverable | Acceptance |
|---|---|---|---|
| M0 Spikes | Oct 19–20 | Keys working; TTS→sim→STT round-trip at 8 kHz; batch job lifecycle; LLM decided (Kimi K3 on Bedrock, D-4) | Round-trip WER printed per noise profile; credit cost per audio-minute measured for each API; batch turnaround time measured |
| M1 Line + simulator | Oct 21–23 | VirtualLine, PSTN sim, DTMF gen/detect, IVR engine with 1 company, hold music + announcements, company console (basic) | Press-through of the full Nimbus tree with scripted DTMF; DTMF detected at snr10 |
| M2 Agent | Oct 24–27 | Orchestrator, MenuParser, IVRDecider, Classifier, HumanDetector + probe, PhraseCache, disclosure, notify, bridge | End-to-end: task → human agent → user joined, on Nimbus at snr10, 3/3 runs |
| M3 Summary + UI | Oct 28–30 | Post-call pipeline with verifier; all frontend pages; 3 companies; eval harness v1 | Summary ticket ID correct with ▶ clips on 5 auto-agent runs |
| M4 Eval + demo | Oct 31–Nov 2 | Full matrix run, charts, demo recording | Charts in `eval/reports/`; demo flows without manual fixes |
| M5 Hardening + stretch | Nov 3–9 | Bug fixes; S1 if MVP solid; submission write-up | Submission done by Nov 9 |

---

## 20. Unknowns to resolve in M0 (record answers here)
- [ ] Credit cost per minute for realtime STT, REST STT, batch STT, and TTS characters. Is realtime STT billed for silence or music?
- [x] Does realtime `x-format: transcribe` convert "press two" / "do dabaiye" to digits reliably? **No.** 2026-10-19 spike: realtime and REST both returned "press one for recharge…". Number-word normalization in the menu parser is mandatory.
- [x] Do partial transcripts exist on `/stt/v3/stream`? The `connected` message reports `partials_enabled: false` by default. Not used.
- [x] Realtime sample rate: the server echoes 16000, but 8 kHz input transcribes at 1.6% prompt WER (live run 2026-10-20, snr10). Keep sending 8 kHz. No `processing` message was seen; messages are `connected`, `speech_start`, `transcript{is_final, utterance_id, …}`.
- [ ] TTS realtime and REST rate limits; max text length per request.
- [ ] Batch job turnaround for a 5-minute file.
- [x] Evon hosting: not needed (D-4). Reasoning runs on Kimi K3 via Bedrock.
- [x] Kimi K3 (Bedrock us-east-1, `reasoning_effort=none`, JSON schema): **p50 1.15 s, max 1.86 s** (cold first call) for a 250-token IVR decision from India; valid JSON 4/4. Default reasoning: 2.7 s, 5× output tokens. Fixture: `backend/tests/fixtures/llm/smoke_decision.json`. Cost per eval run still to measure (T-008).
- [ ] Exotel (S2 only): in-band DTMF reaches the far end in bidirectional mode; outbound to mobile allowed from the trial account.

---

## 21. Conventions for Claude Code
- Python: type hints everywhere, async I/O, no blocking calls in the event loop. CPU-heavy DSP goes through `asyncio.to_thread` when a window exceeds 50 ms.
- Keep modules small and single-purpose, matching §4. No new top-level dirs without updating this file.
- Every external call (Gnani, LLM) goes through its client module with retries, timeouts, usage metering and journaling.
- Every LLM output is a pydantic model. No free-text parsing downstream.
- Thresholds and timeouts live in `config.py`, never inline.
- Write tests alongside each module. Default test run must spend zero credits.
- Keep this file updated: tick §20 items, adjust thresholds after eval, note deviations in a short "Decisions log" at the end.

## 22. Autonomous development workflow
Agents work unattended. Before doing anything, read **docs/AGENT_LOOP.md** (how to work), **docs/EVAL_LOOPS.md** (how work is judged) and **docs/BOARD.md** (what to work on).
- `python scripts/check.py` must pass before every commit. CI runs the same command.
- Contract tests in `backend/tests/contract/` are the spec in executable form. Make them pass; don't rewrite them.
- `eval/ratchet.json` only grows. Never loosen `eval/thresholds.yaml` gates without measured evidence.
- Frontend work follows **docs/FRONTEND.md** and the owner's `docs/design/inspiration/`.
- Owner-only items and spec conflicts go to "Owner decisions" in BOARD.md.

## Decisions log
- 2026-10-19: Added the eval/agent workflow (§22), the scripts/ and docs/ dirs, and eval/{thresholds,scorecard,journal_check,fixtures}.
- 2026-10-19: New per-run metrics `disclosure_first` and `journal_valid` (from eval/journal_check.py) are hard gates in the scorecard, enforcing §0.5 on every eval run.
- 2026-10-19: Synthetic mobile numbers must come from scripts/synthetic_allowlist.txt (guard-enforced).
- 2026-10-19: Python deps install via `uv pip install -r pyproject.toml --extra dev`; `webrtcvad-wheels` replaces `webrtcvad` (prebuilt wheels).
- 2026-10-19 (owner): D-1 resolved: the liveness probe itself discloses AI (§10.3); probe events carry `discloses_ai: true`.
- 2026-10-19 (owner): D-2 resolved: single local session on the owner's machine for now; BOARD.md is the task list. Revisit when moving to cloud agents.
- 2026-10-19 (owner): D-3 resolved: detection latency is measured from the END of the greeting segment; gate p50 ≤ 3 s.
- 2026-10-20: First live runs (real Timbre + Prisma realtime, Nimbus, snr10):
  - DONE, path 1-1-2, prompt WER 1.6%.
  - STT end-of-speech estimates run early on real audio, so turn-taking now uses the classifier's speech end (`human_end_slack_s`). Detection went from probe/8–10 s to 2.46 s with no probe.
  - Metered: 1,761 TTS characters (cached) and 234 s realtime STT over 3 runs.
- 2026-10-20: Cloud overnight work follows docs/OVERNIGHT.md. The cloud uses `LLM_AUTH=api_key` with a Bedrock API key; the laptop uses `bedrock_profile`.
- 2026-10-20: AudioClassifier (§10.1), measured on procedural music, synthetic and Timbre speech, DTMF and ringback, clean to snr5:
  - webrtcvad marks hold music and snr10 noise as ~100% speech, so it is informational only.
  - Speech vs music is decided by spectral-flux variance (≥ 0.006); tone by the energy share of the top 2 peaks; noise by flatness.
  - Harmonic ratio dropped (no separation).
  - "Sustained music" = ≥ 3 of the last 4 windows.
  - Babble noise and real recordings are not yet covered; re-tune in eval.
- 2026-10-20: Orchestrator (§8):
  - Tick-driven, with one step per 20 ms inbound frame.
  - In IVR state, only a greeting can raise human suspicion.
  - The turn-taking bonus needs no speech and no music after the segment.
  - "No music before" means the last classifier window before the speech was not music. The 3 s lookback never fired, because agents pick up as music stops.
  - Silent-agent rule: music stopped plus 3 s silence in HOLD triggers the probe.
  - Post-call is skipped until M3 (`summary_ready{skipped}`).
- 2026-10-20: IVRDecider ranks options by goal relevance:
  - keyword hits (word-level fuzzy, so "card" matches "cards") plus goal-word overlap
  - never picks a destructive option (block/cancel/deactivate/close/stop/band) unless the goal asks for it
  - found by the first Sahyog simulation, which pressed "block your card" for a refund goal
- 2026-10-20: Menu parser (§9.2) is token-based, not just the four regexes:
  - Hindi "X dabaiye" mentions claim "<label> ke liye" first.
  - English mentions get label-first or key-first orientation per segment.
  - "do"/"oh" become digits only next to a press verb.
  - Devanagari key phrases are mapped before transliteration.
  - `#`/`*` mentions are dropped when a data prompt is present.
  - PIN/OTP/CVV prompts are `other`, so the agent never sends data to them.
- 2026-10-20: HumanDetector novelty uses whole-string `fuzz.ratio` (not token-set). The announcement lexicon is widened to tolerate variants. Weights live in config (`human_w_*`).
- 2026-10-20: Phrases agree with the agent voice's gender (Shivani → "kar rahi hoon"). hi-IN phrases are in Devanagari with "एआई". User names are sanitised before TTS.
- 2026-10-20: `PhoneLine` gained `drain()` (await playback of queued audio) beyond the §6.3 protocol; VirtualLine is paced by an injectable clock (`line/clock.py`: RealClock, or FakeClock for tests/eval faster than real time) and plays Indian ringback (400+450 Hz) before answer.
- 2026-10-20 (owner): D-4 resolved: Evon is not required by the challenge (it needs at least one Gnani model or API; we use Prisma and Timbre). Reasoning uses Kimi K3 on Amazon Bedrock via OpenAI-compatible Chat Completions (`us.moonshotai.kimi-k3`, us-east-1, owner's existing AWS profile; short-lived tokens, no static key). Evon remains a config-only drop-in.
- 2026-10-19 (owner): D-5 resolved: ElevenLabs UI and LiveKit Components may be used for visual UI components only (copied in); no SDK packages (guard-enforced).
