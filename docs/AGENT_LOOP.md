# Agent operating manual

You are building Line Pe Raho.

**Current mode (D-2): one local session on the owner's machine.**
- The owner wants to follow along. Post a short update before each task (what and why) and after it (what changed, and the gate result).
- Surface any decision that is the owner's, with a recommendation.
- Commits need the owner's go-ahead.

The steps below still apply. When the project moves to cloud agents, the branch/PR flow applies as written. Read this file at the start of every session, together with CLAUDE.md (the spec) and docs/EVAL_LOOPS.md (how work is judged).

## Setup (once per machine)
```bash
uv venv .venv --python 3.11 && uv pip install --python .venv/Scripts/python.exe -r pyproject.toml --extra dev
.venv/Scripts/python.exe scripts/check.py --fast   # (Linux/macOS: .venv/bin/python) must pass on a clean main
```
Put secrets in `.env` (copy `.env.example`). Never print them, commit them, or paste them into PRs.

## One iteration (`/iterate` runs this)

1. **Orient**
   - `git pull`
   - `python scripts/check.py --fast`
   - `python scripts/scoreboard.py`
   - read docs/BOARD.md
2. **Pick exactly one task**, in this priority order:
   1. main is red (check fails on main), so fix that first
   2. a critic "request changes" on your own open PR
   3. a failing L2 gate or regression in the latest `eval/reports/*/scorecard.md`
   4. the first unclaimed BOARD task in the lowest unfinished milestone whose deps are done

   Skip tasks marked `[owner]` and anything under "Owner decisions".
3. **Claim**
   - Create branch `agent/<task-id>-<slug>`.
   - In your first commit, change the task's line in BOARD.md to `[~] T-xxx … (claimed: <branch>, <YYYY-MM-DD>)`.
   - If a claim is more than 48 h old with no commits, you may take it over; say so in your PR.
4. **Tests first**
   - If contract tests exist for the task, they are your target. Read their docstrings for the pinned interface.
   - Add unit tests for anything else you build. Tests go next to the module's tests (`backend/tests/...`).
5. **Implement** within the spec (CLAUDE.md §4 layout, §21 conventions).
   - Thresholds go in `app/config.py`.
   - External calls go through their client module.
   - Every LLM output is a pydantic model.
6. **Gate**
   - Iterate on `python scripts/check.py --fast` until green, then run the full `python scripts/check.py`.
   - When newly passing tests appear, run `python scripts/ratchet.py --update` and commit `eval/ratchet.json`.
   - For frontend work, also follow `/ui-check`.
   - For agent, audio or postcall changes after M3, also run the affected L2 eval slice.
7. **Self-review** against docs/REVIEW_RUBRIC.md. Fix every blocker yourself before opening the PR.
8. **PR**: one task per PR, ideally under 400 changed lines excluding fixtures. The body has these sections:
   - **Task**: T-xxx, spec §
   - **What changed**
   - **Evidence**: the check summary table, scoreboard before/after, eval scorecard rows if relevant, screenshots for UI
   - **Deviations from spec**: also appended to CLAUDE.md "Decisions log"
   - **Follow-ups**: added to BOARD.md as new tasks
9. **Close out**: mark the task `[x]` in BOARD.md (in the PR) and tick any CLAUDE.md §20 item you answered.

## Hard rules
- CLAUDE.md §0 always wins. If a task seems to need breaking it, stop and add an owner decision.
- Never edit `eval/thresholds.yaml` gates, existing contract-test expectations, fixtures' expected values, or remove ratchet ids just to get green. Each of these needs cited evidence in the PR, and the critic blocks silent changes.
- Never delete or skip a failing test to pass the gate. Never use `--no-verify`.
- Never run `-m live` tests or spikes unless the task says so and `LIVE_BUDGET_CREDITS` is set. Report the credits used in the PR.
- Never call a real phone number or company. VirtualLine only (§0.3).
- Never introduce a new top-level directory without updating CLAUDE.md §4.
- Never add a dependency without a reason in the PR. For frontend code copied from open source, add it to THIRD_PARTY.md with its license.

## When stuck
- If the same gate step fails after 3 genuinely different attempts, stop. Add a `BLOCKED:` note under the task in BOARD.md with what you tried and what you suspect, then move to another task.
- If the spec is contradictory or ambiguous in a way that changes behaviour, add an entry under "Owner decisions" in BOARD.md with options and your recommendation, then pick other work. Don't guess on §0 matters.
- If you need something only the owner can provide (keys, GPU account, Exotel trial, a human for the console), mark it `[owner]` and move on.
