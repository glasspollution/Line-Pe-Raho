# Eval loops

The owner is not in the loop day to day. "Done" therefore never means "the agent thinks it works".
It means a command exited 0. This file defines those commands, what they check, and what to do when they fail.

| Loop | When | Command | Fails when | Output |
|---|---|---|---|---|
| **L0 gate** | before every commit | `python scripts/check.py` (`--fast` while iterating) | any step fails | `reports/check.json` |
| **L1 contracts + ratchet** | inside L0 | pytest + `scripts/ratchet.py` | a test that ever passed now fails, skips or vanishes | `reports/junit.xml`, scoreboard |
| **L2 system eval** | after M3; on any change to agent/, audio/, postcall/ | `python eval/run_eval.py ...` then `python eval/scorecard.py <metrics.csv> --baseline ...` | a gate, a hard per-run rule, or a regression beyond `tol` | `eval/reports/<ts>/scorecard.md` |
| **L3 critic** | every PR | `/critic` skill, using `docs/REVIEW_RUBRIC.md` | any blocker | PR review + new BOARD items |
| **L4 UI** | any frontend PR | `/ui-check` skill (`npm run e2e` + screenshots + rubric) | e2e or axe fail, or a P0/P1 visual issue | `reports/ui/*.png`, review notes |
| **L5 owner** | milestone acceptance, live/credit spend, spec conflicts | the owner | — | `docs/BOARD.md` "Owner decisions" |

---

## L0: the gate (`scripts/check.py`)

Steps run in order. A step is skipped only while its target doesn't exist yet. Once it exists, it is required.

1. **guard**: `scripts/guard_constraints.py` turns CLAUDE.md §0 into a scan. Hard errors:
   - a non-Gnani STT/TTS SDK, host, or browser Web Speech API (code files only; docs may discuss them)
   - an LLM host written into code instead of read from config
   - a real company name (deny-list in `scripts/guard_rules.py`)
   - an API key, token or private key literal
   - `.env` tracked by git or not ignored
   - a mobile-like number missing from `scripts/synthetic_allowlist.txt`

   Aadhaar- and PAN-shaped strings are warnings. To suppress one line deliberately, add `# guard: allow <rule> -- <reason>`. The critic reviews every suppression.
2. **ruff**: lint for `backend/`, `scripts/`, `eval/`.
3. **mypy** (full mode): non-strict; `check_untyped_defs`.
4. **pytest**: offline only. The root `conftest.py` blocks every non-loopback socket, so a test that would spend credits fails loudly. Live tests are `@pytest.mark.live` and never run here.
5. **ratchet**: see L1.
6. **journal**: every `eval/fixtures/*.jsonl` must pass `eval/journal_check.py --complete`.
7. **frontend** (once `frontend/package.json` exists): `npm run typecheck`, `lint` and `test`; full mode adds `build` and `e2e`. Missing any of these scripts fails the step.

## L1: contract tests and the ratchet

**Contract tests** live in `backend/tests/contract/`. They pin spec behaviour before the code exists, so agents build against a fixed target rather than inventing one.
- Until a module exists, its tests SKIP with `NOT IMPLEMENTED: <module> [<task>]`.
- `python scripts/scoreboard.py` groups results by milestone and lists the missing modules. That list is the to-do list.

**Ratchet** (`eval/ratchet.json`): the list of test ids that have passed at least once.
- `scripts/ratchet.py` fails if any of them now fails, skips or is missing.
- After making tests pass, run `python scripts/ratchet.py --update` and commit the file.
- The list only grows. CI compares against `origin/main`'s copy and fails if ids were removed.
- Removing an id requires an owner decision in BOARD.md.

### Interfaces pinned by contract tests
Each test file's docstring holds the exact signatures. Summary:

| Module | Task | Pinned |
|---|---|---|
| `app.config` | T-001 | `Settings` (pydantic-settings), `gnani_api_key: SecretStr`, lower-cased env names |
| `app.audio.frames` | T-101 | `Rechunker(size).push/flush`, `resample(x, src, dst)`, `mulaw_encode/decode` |
| `app.audio.dtmf` | T-102 | `generate(digits)`, `DTMFDetector().feed(frame) -> list[str]`; survives the PSTN sim at snr10 and 1 % loss |
| `app.audio.pstn_sim` | T-103 | `NOMINAL_SIGNAL_DBFS=-20`, `PSTNConfig(...)`, `PSTNSimulator(cfg).process(frame)`; chain: gain → noise → bandpass → μ-law → loss |
| company trees YAML | T-105 | references, reachability, depth 3–6 with variety, ask node, connecting prompt in some trees, a "Hello…" announcement, language order varies |
| `app.agent.menu_parser` | T-201 | `normalize`, `parse_segment`, `MenuBuffer`; 68-case corpus in `fixtures/menu_prompts.yaml` |
| `app.agent.human_detector` | T-204 | `SegmentEvidence`, `HumanDetector(known).remember/score`; bucket table in `fixtures/human_detector_cases.yaml` |
| `app.agent.phrases` | T-205 | `disclosure/keepalives/probe/no_join_fallback(lang)`; AI disclosure in the opening sentence, no digits |
| `app.postcall.verify` | T-303 | `verify(extraction, segments) -> {field: FieldVerdict}`; cases built from `eval/fixtures/golden_summary.json` |
| journals | all | `eval/journal_check.py` invariants J1–J7 (below) |

Before handing these over, the DSP, detector and verifier contracts were each checked against a throwaway reference implementation, so they are known to be satisfiable.

**Changing a contract.**
- Adding cases is encouraged.
- Changing an existing expectation requires a PR note citing the spec section or eval evidence. The critic treats a silent expectation change as a blocker.
- If the spec itself is wrong, open an owner decision in BOARD.md instead of editing the test.

### Journal invariants (`eval/journal_check.py`)
Checked on the golden journal, in orchestrator tests, and on every eval run (`journal_valid`):
- **J1:** known event types; required `{type, t, call_id}`; one `call_id` per journal.
- **J2:** `t` never decreases.
- **J3:** every `state_changed` follows `eval/fixtures/state_graph.yaml`; `→ FAILED` is always legal.
- **J4:** **AI disclosure first.** After a human is suspected, the agent's first utterance must disclose AI (§0.5).
- **J5:** no keep-alive or user join before a disclosure.
- **J6:** DTMF only in IVR; no agent speech while BRIDGED.
- **J7:** with `--complete`, the journal ends in DONE or FAILED.

## L2: system eval and the improvement loop

`eval/run_eval.py` (built in T-311) runs the scenario matrix headless with the auto-agent. It must write `metrics.csv` with every column listed in `eval/thresholds.yaml: required_columns`, and run `journal_check` on each journal to fill `journal_valid` and `disclosure_first`.

`eval/scorecard.py` turns that CSV into a verdict:
- **hard_per_run:** a single violating run fails everything (disclosure, journal validity).
- **gate:** failure blocks the milestone.
- **target:** a miss becomes an improvement candidate, not a failure.
- **regression:** worse than the baseline by more than `tol`; fails.
- **no data:** reported; fails only with `--full` (a complete matrix must cover every rule).

The baseline is `eval/reports/baseline/scorecard.json`. It is replaced only by a full-matrix run that passes, in its own PR titled `eval: new baseline`.

**Improvement loop.** Used when the board has no higher-priority work, or a task says "improve X".
1. Open the latest `scorecard.md` and take the first row that isn't ok: GATE FAIL, then REGRESSED, then NO DATA, then below target.
2. Read its worst scenarios' journals (`scripts/replay.py <call_id>` once T-108 lands) and write a one-line hypothesis.
3. Change **one** thing. Thresholds and weights live in `app/config.py`.
4. Re-run only the affected slice (`--only company=... noise=...`) and compare with `scorecard.py --baseline`.
5. Keep the change if the target metric improves and nothing else regresses. Otherwise revert it and record the negative result in the PR or BOARD so nobody repeats it.
6. Re-run the full matrix before merging any change that touches detection or decision logic.

Never loosen a gate to get green. Gates marked PROVISIONAL get real values after the M0 and M4 measurements, in a PR that cites those measurements.

## Live tests and credits

- Credits are real: about 5k on the current key. Live tests (`-m live`, `RUN_LIVE=1`) run only when a BOARD task says so **and** `LIVE_BUDGET_CREDITS` is set in the environment.
- Each live run logs its usage to the PR.
- `scripts/spikes/gnani_smoke.py` is the smallest live check (one TTS + one STT call). It writes scrubbed responses to `backend/tests/fixtures/gnani/` for the offline fakes.

## Recorded API facts (M0 spike, 2026-10-19)
From `backend/tests/fixtures/gnani/smoke_roundtrip.json`:
- TTS REST: an 8 kHz raw PCM request returns `application/octet-stream`. 58 characters gave 4.0 s of audio in about 2.2 s.
- STT REST `format=transcribe` returned `"press one for recharge…"`. **Number words are not converted to digits**, so the menu parser must normalise them.
- Realtime STT:
  - The `connected` message echoes `config.sample_rate: 16000` even with `x-sample-rate: 8000`. `audio_duration_ms` is consistent with 8 kHz input. Confirm in T-005.
  - Messages seen: `connected`, `speech_start`, `transcript {is_final, utterance_id, segment_id, audio_duration_ms, latency, output.literal}`. No `processing` message arrived, so §7.1 timestamp alignment should key on `speech_start` and `transcript` instead.
- LLM (2026-10-20, `scripts/spikes/llm_smoke.py`):
  - Kimi K3 on Bedrock us-east-1, Chat Completions, `reasoning_effort=none` with a strict JSON schema: p50 1.15 s, max 1.86 s, 4/4 valid, correct choice.
  - Leaving reasoning on (the provider default) costs 2.7 s and 5× the output tokens, so real-time calls must always send `reasoning_effort`.
