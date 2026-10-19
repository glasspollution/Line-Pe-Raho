---
name: ui-check
description: L4 UI evaluation for the Line Pe Raho frontend — run e2e and axe in mock mode, capture screenshots at 375 and 1280 px, review them against docs/FRONTEND.md and the owner's inspirations, fix P0/P1 issues. Use after any frontend change.
---

1. In `frontend/`:
   - `npm run typecheck && npm run lint && npm run test`
   - `npm run e2e` (Playwright starts `dev:mock`, which replays `eval/fixtures/golden_call.jsonl`)
2. Capture screenshots to `reports/ui/<page>-<state>-<width>.png` at 375×667 and 1280×800. Use the e2e helper or a small Playwright script. Required states:
   - NewTask (empty, plan preview)
   - LiveCall (IVR, on hold with timer, agent found / join overlay, bridged)
   - Summary (top, ⚠ field, transcript)
   - CompanyConsole (incoming call)
   - EvalReport
3. Open each screenshot and review it against:
   - the "UI review rubric" in docs/FRONTEND.md (P0, P1, P2)
   - the product feel section of docs/FRONTEND.md
   - `docs/design/inspiration/inspirations.md` and its images: does each page borrow what the owner marked "take", and avoid what they marked "avoid"?
4. Fix every P0 and P1 and repeat from step 1. List P2s in the PR body.
5. When a screen is approved, update its `toHaveScreenshot` baseline and attach the before/after screenshots in the PR.
6. Report: a table of page/state → issues found → fixed?, plus axe results and the bundle size.
