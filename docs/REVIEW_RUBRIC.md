# Review rubric (L3 critic)

Used by `/critic` on every PR and by authors for self-review. Output format at the bottom.

Severity:
- **blocker**: must fix before merge
- **major**: fix in this PR unless the author files a BOARD task and gives a reason
- **minor**: suggestion

## Blockers
- [ ] **§0 hard constraints.**
  - No non-Gnani speech path, including hidden ones: browser speech APIs, a "temporary" fallback, test helpers that hit real APIs.
  - LLM endpoint comes from config.
  - Fallback LLM use is journaled.
- [ ] **AI disclosure.** No new code path lets the agent speak to a possible human before the disclosure (journal invariant J4). This includes probes, voice replies and S1 agent-speaks.
- [ ] **Synthetic data only.**
  - Fictional brands only.
  - Mobile numbers only from `scripts/synthetic_allowlist.txt`.
  - No real recordings.
  - Profile data is sent only for the field the IVR asked for. PIN, OTP and CVV are never sent.
- [ ] **No secrets** in code, logs, journals, fixtures or PR text. Keys are `SecretStr` and never logged.
- [ ] **Gate honesty.**
  - check.py passes.
  - No edited thresholds, contract expectations, golden fixtures or ratchet removals without cited evidence.
  - No deleted, skipped or xfailed tests that were passing.
  - Any `guard: allow` suppression has a real reason.
- [ ] **Credits.** No new code runs live calls in the default test path. Live code paths are metered (§7.5).

## Majors
- [ ] **Spec conformance.** The code does what the cited CLAUDE.md section says, or the deviation is in the Decisions log with a reason.
- [ ] **Async hygiene.**
  - No blocking I/O or heavy DSP on the event loop; use `asyncio.to_thread` for windows over 50 ms.
  - No `time.sleep`.
  - Real-time cadence for STT (1024-byte frames at 64 ms, never burst).
- [ ] **Config.** Thresholds and timeouts live in `app/config.py`, not inline literals.
- [ ] **Errors.**
  - External calls have timeouts and retries per §7.5, and never retry a 400.
  - Failures emit an `error` event and leave the state machine in a legal state (J3).
- [ ] **Tests.**
  - New behaviour has tests.
  - Bug fixes have a regression test that fails without the fix.
  - Tests are deterministic (seeded RNG, no wall-clock sleeps; use fake clocks).
- [ ] **Events.** Every transition and decision emits a typed §14.2 event with the fields the UI needs, such as `method`, `reason` and `latency_ms`.
- [ ] **Scope.** One task. No drive-by rewrites. No new top-level directories without updating CLAUDE.md §4.
- [ ] **Frontend.**
  - Works at 375 px.
  - The join-call CTA is above the fold.
  - No speech-provider SDKs; visual components only.
  - THIRD_PARTY.md is updated for copied components.

## Minors
- Naming and structure match the neighbouring code. Comments explain *why*, not *what*.
- Logs are useful for `scripts/replay.py`.
- Copy and microcopy are consistent (Hinglish where the spec uses it).

## Output format
```
VERDICT: approve | request-changes
BLOCKERS:
- <file:line> <what> -> <fix>
MAJORS:
- ...
MINORS:
- ...
NEW BOARD ITEMS:
- T-??? <title> — <why>   (only for real follow-ups, not restatements of the above)
```
