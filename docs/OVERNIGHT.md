# Overnight brief: 2026-10-20 → morning of 2026-10-21

**Goal:** by morning, one command runs the whole product loop on the backend:

> user's problem in text → CallPlan → call the mock company → navigate the IVR → hold → detect the human → disclose → bridge the user → hang up → post-call summary with evidence and verification.

**No frontend tonight.** The owner wants to brainstorm the frontend together. Prepare options (task N-8) but write no UI code.

Read first: CLAUDE.md (spec, especially §0 and §22), docs/AGENT_LOOP.md, docs/EVAL_LOOPS.md, docs/BOARD.md.

## Where things stand (all green: `python scripts/check.py`, 352 tests locked)

**Built and tested:**
- **M1, complete:**
  - audio frames, PSTN simulator, DTMF
  - VirtualLine (RealClock/FakeClock, ringback, `tap`)
  - IVR engine with three company trees
  - hold music, recorder, journal, replay, console WS
- **M2 offline pieces:** menu parser, human detector, phrases, audio classifier, IVR decider (rules), orchestrator.
- **Simulation kit:** `app/sim/` (oracle STT, scripted human agent, `run_sim_call` harness). Eight full simulated calls pass the journal invariants.
- **Clients:**
  - `gnani/errors.py`: retry and usage meter
  - `gnani/tts.py`: Timbre REST, PhraseCache, CachedVoice
  - `gnani/stt_rest.py`
  - `gnani/stt_realtime.py`: Prisma realtime with rotation and pacing
  - `llm/client.py`: Kimi K3 on Bedrock, pydantic JSON, fallback
  - `llm/auth.py`
- **Live:** `scripts/spikes/live_call.py` ran Nimbus end to end with real Timbre and Prisma.
  - DONE, path 1-1-2, prompt WER 1.6 % at snr10.
  - Human confirmed 2.46 s after the greeting, with no probe.

**Not built yet** (tonight's work, in order below):
- the CallPlan builder
- the REST/WS API
- the user audio bridge
- the LLM auto-agent
- the post-call pipeline
- the LLM fallback in the decider
- the end-to-end command

## Working rules for tonight (cloud session)
- Work on branch `overnight/backend-pipeline`.
  - **One commit per task**, with the gate green before each commit.
  - Push after each commit so progress is visible.
  - Never push to `main`. Open a PR at the end, titled "Overnight: backend pipeline", with the summary below.
- `python scripts/check.py` must pass before every commit. Never loosen thresholds, edit contract expectations, or drop ratchet ids (AGENT_LOOP hard rules). Run `python scripts/ratchet.py --update` and commit `eval/ratchet.json`.
- **Credits budget for tonight:**
  - at most **6 live calls** (`live_call.py` or the new e2e live mode), about 8 min of realtime STT
  - TTS synthesis only for new phrases (the cache is per-machine; expect about 2k characters per company the first time)
  - Kimi: at most 200 LLM calls
  - Report metered usage in the PR. If a live call fails twice for the same reason, stop spending and fix it offline.
- Secrets come from the environment, never from files. `.env` does not exist in the cloud; settings read env vars.
  - Gnani: `GNANI_API_KEY`.
  - LLM: `LLM_AUTH=api_key` with `LLM_API_KEY` (a Bedrock API key); base URL and model defaults already point at Kimi K3 in us-east-1.
  - If a secret or network access is missing, do everything offline and note it in the PR.
- Never call real companies or numbers; synthetic data only (§0). Synthetic profile values are in `app/sim/harness.py` (`DEMO_PROFILE`).
- If something in the spec conflicts, add an Owner decision to BOARD.md and pick the reversible option.

## Tasks (in order; each ends with tests and a commit)

**N-1 · CallPlan builder (T-new, §9.1)**: `backend/app/agent/planner.py`
- `async build_plan(text, company_id, profile, llm: LLMClient | None) -> CallPlan`.
  - LLM path: system prompt in `llm/prompts/plan.md`, output is the `CallPlan` pydantic model.
  - Rule fallback when the LLM is absent or fails: keyword table per company (recharge/refund/billing; card/transaction/dispute; power cut/bill), `spoken_intent` from the goal, `user_language` detected from the text (Devanagari → hi-IN, Hinglish markers → hi-en, else en-IN).
- Tests: rules for 10 Hinglish, English and Hindi problem statements; the LLM path with a fake transport (see `tests/test_llm_client.py`). One live Kimi check is optional.
- Done when `build_plan("Nimbus ne mera recharge do baar kaat liya, refund chahiye", "nimbus_telecom", DEMO_PROFILE, None)` yields keywords containing recharge and refund.

**N-2 · LLM fallback in the IVR decider (rest of T-202)**
- `IVRDecider` is synchronous and runs on the tick, so the LLM call must not block it.
  - When the rules return "wait" and an LLM is configured, the orchestrator starts an async task (prompt `llm/prompts/ivr_decide.md`, model `IVRDecisionOut`, `realtime=True`).
  - It keeps ticking, and presses when the result arrives if the menu hasn't changed. Journal the decision with `method="llm"` and `latency_ms`.
- Also wire `needs_llm` menus from the parser (rules found no options) to the same path.
- Tests use a fake LLM: a menu with no keyword match gets pressed via the LLM; low confidence gets "repeat".

**N-3 · Auto-agent (T-208, §11.3)**: `backend/app/company/auto_agent.py`, an AgentLeg
- An LLM plus a Timbre voice play the company's employee.
- Persona and greeting are sampled per company. It asks for the registered mobile number, issues a fictional ticket ID (`NMB` + 8 digits for Nimbus, `SHB…`, `PBB…`), promises a resolution date, and ends politely.
- It **writes ground truth**: greeting start/end, ticket ID, promised date.
- It hears the caller through Prisma REST on utterance end (live), or reads the oracle in sim mode.
- Its speech is pre-synthesised where fixed. Dynamic replies use `TimbreTTS.synthesize` asynchronously (the next frames stay silent until the audio arrives; that's acceptable).
- Offline mode: replace the LLM with a deterministic script, so tests and the eval don't need the network.
- Tests: an offline sim call with the auto-agent produces a ticket ID in ground truth and in the transcripts.

**N-4 · Bridge and user leg (T-207, §14.2)**: `backend/app/agent/bridge.py` and `/ws/user-audio/{call_id}`
- Browser audio is 48 kHz float32 in, 8 kHz s16le out (reuse the `company/console.py` patterns).
- While BRIDGED: user audio → line, line audio → user. Our agent stays muted (journal J6). STT continues on the callee track.
- Record the user track (`CallRecorder` has `user`).
- In sim, a `SimUser` speaks scripted lines (SyntheticVoice) so the auto-agent hears the problem.
- Tests: a WS loopback, plus a sim call where the user speaks and the agent replies.

**N-5 · Post-call pipeline (T-301/302/303, §12)**: `backend/app/postcall/{pipeline,extract,verify}.py`
- `verify.py` must pass `backend/tests/contract/test_verify.py`, which currently skips as "NOT IMPLEMENTED".
- `pipeline.py`:
  - realtime transcripts plus user-track transcripts make the timeline
  - Prisma Batch (`gnani/stt_batch.py`, T-006: create/start/poll/files) when live, with realtime segments as the fallback
  - an ITN pass with Prisma REST on number-bearing slices
  - LLM extract (`llm/prompts/extract.md`, `realtime=False`)
  - verify, then a summary JSON with `{start, end}` per evidence
- The orchestrator calls it in SUMMARIZING, replacing the current `summary_ready{skipped}`.
- Offline: a fake extractor built from ground truth.
- Tests: the golden summary, and an offline sim call whose summary has the auto-agent's ticket ID verified.

**N-6 · REST and WS API, minimal (T-304, §13/§14)**
- SQLModel tables (`models.py`).
- `POST /api/tasks` (text → plan), `POST /api/tasks/{id}/call` (starts a call in the background; virtual line, sim or live by config), `GET /api/calls/{id}`, `GET /api/calls/{id}/summary`, `GET /api/calls/{id}/audio/{track}`, `GET /api/companies`, `POST /api/company/scenario`, `GET /api/usage`.
- `/ws/events/{call_id}` replays the journal backlog, then streams live (`Journal.subscribe`).
- Tests: httpx AsyncClient, the full happy path in sim mode through the API.

**N-7 · One end-to-end command**: `scripts/e2e.py`

```bash
python scripts/e2e.py --text "Nimbus ne mera recharge do baar kaat liya, refund chahiye" --company nimbus_telecom [--live]
```

- Default is offline: FakeClock, oracle STT, scripted or auto-agent, fake extractor. `--live` uses real Timbre, Prisma and Kimi with RealClock.
- Prints the timeline (`scripts/replay.py`), the summary with verified ✓ / unverified ⚠ per field, the usage meter, and the artifact paths.
- Add an offline test that runs it.
- Done when the offline run is green in CI and **one** live run is recorded in the PR (journal and summary attached as text).

**N-8 · Frontend options doc (no code)**: `docs/FRONTEND_OPTIONS.md`

Lay out 2–3 concrete directions for the owner to choose from, each with what the demo video would show, effort, risks, and what it needs from the API:
- (a) a phone-frame "mobile app simulation" in the browser
- (b) a desktop web page with a split view: user app on the left, company console and live call graph on the right
- (c) a hybrid

Note that the demo is judged from a video post, so think about what reads well on screen. Reference docs/FRONTEND.md and decision D-5.

## End-of-night PR summary (template)
- What works end to end (paste the offline e2e output; one live run's summary and usage)
- Tasks done / partial / blocked (with BLOCKED notes)
- Metered credits used tonight (STT seconds, TTS characters, LLM calls and tokens)
- New owner decisions needed
- Ratchet count before → after
