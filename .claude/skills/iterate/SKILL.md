---
name: iterate
description: Run one autonomous development iteration on Line Pe Raho — orient, pick one BOARD task, test-first implement, pass the gate, self-review, open a PR. Use when told to "iterate", "work on the next task", or run unattended.
---

Follow docs/AGENT_LOOP.md exactly. In short:

1. Orient:
   - `git pull`
   - `python scripts/check.py --fast`
   - `python scripts/scoreboard.py`
   - read docs/BOARD.md and the newest `eval/reports/*/scorecard.md` if one exists.
2. Pick ONE task by the priority order in AGENT_LOOP.md §2. Skip `[owner]` items and Owner decisions.
3. Claim it: create branch `agent/<task-id>-<slug>` and mark `[~]` with your branch and date in BOARD.md.
4. Read the task's contract tests (docstrings pin the interface) and the CLAUDE.md sections it cites. Write or extend tests first.
5. Implement. Loop on `python scripts/check.py --fast` until green, then run the full `python scripts/check.py`.
6. `python scripts/ratchet.py --update` if new tests pass, and commit `eval/ratchet.json`.
7. Frontend task: run the `ui-check` skill. Agent, audio or postcall change after M3: run the affected eval slice and the scorecard.
8. Self-review against docs/REVIEW_RUBRIC.md and fix every blocker.
9. Open a PR with the body sections from AGENT_LOOP.md §8 (Task, What changed, Evidence, Deviations, Follow-ups). Mark the task `[x]`, add follow-ups to BOARD.md, and log deviations in the CLAUDE.md Decisions log.

Stop and record a BLOCKED note or an Owner decision instead of guessing when:
- a gate fails after 3 genuinely different attempts
- the spec conflicts with itself
- the work needs credits, keys or a human

Never loosen gates, edit contract expectations, or drop ratchet ids to get green.
