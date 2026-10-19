# Frontend brief

Stack per CLAUDE.md §5 and §15: Vite, React 18, TypeScript, Tailwind, and AudioWorklets. Mobile-first: everything must work at 375 px.

## Product feel
The user hands off a boring phone chore and goes back to their day. The UI should feel **calm, trustworthy and glanceable**.
- **One status line answers "what's happening right now?"** (Dialing → In IVR → On hold 12:40 → Agent found). The timeline sits underneath it as supporting detail.
- **The one moment that matters is "Agent mil gaya".** Make it loud: full screen, ringtone, Notification API, and a Join button at least 56 px tall, above the fold at 375 px.
- **Show proof, not promises.** Every decision shows *why* (reason, rule or LLM badge). Every summary field has ▶ evidence and a ⚠ when unverified.
- **Honest about being an AI.** The live screen shows the disclosure line the agent spoke. Profile inputs are labelled "demo data".
- **Microcopy** is Hinglish where the spec uses it ("Agent mil gaya — Join call") and plain English elsewhere. Never mix scripts in one label.

## Design tokens (starting point; adjust to the owner's inspirations)
- Neutral base (zinc/slate), with one accent for primary actions.
- Semantic colours: success green for "agent found" and "verified", amber ⚠ for "unverified", red only for errors and hang-up.
- Type: Inter plus Noto Sans Devanagari as fallback. Tabular numerals for timers.
- Support light and dark mode via CSS variables (shadcn theming).
- Motion only for state changes: status pill transitions and the join screen entrance. Respect `prefers-reduced-motion`.

## Component sources (open source; record each in THIRD_PARTY.md with its license)
| Need | Use | Notes |
|---|---|---|
| Base primitives: button, card, dialog, sheet, badge, tabs, toast, form | **shadcn/ui** (Radix + Tailwind, copied into `src/components/ui`) | MIT. Copy in, don't depend on it as a package |
| Icons | **lucide-react** | ISC |
| Toasts | **sonner** (shadcn's toast) | MIT |
| Evidence clips, waveform, transcript playback | **wavesurfer.js** with the Regions plugin | BSD-3. The region covers `[start-0.5, end+0.5]`; ▶ plays only that region |
| Join screen and status transitions | **motion** (formerly framer-motion) | MIT |
| Live audio level meter while bridged | Own small canvas or SVG component reading an `AnalyserNode` | Keep it tiny |
| Timeline | Own component built from shadcn Card and Badge | Vertical list; sticky status pill on top |

**Voice UI components (D-5, allowed):** visual components from **ElevenLabs UI** (shadcn-registry components such as waveform, bar visualizer, orb and voice button) and **LiveKit Components** (audio visualizers) may be **copied into `src/components/voice/`**. Rules:
- Copy the component source only. Remove any import of `@elevenlabs/*`, `livekit-client` or `@livekit/*`, and feed the component our own data: an `AnalyserNode` from our playback or capture worklet, or volume arrays.
- Record each copied component in THIRD_PARTY.md with its source URL and license (check the repo's LICENSE at copy time).
- Never use their hooks for conversation, agents, STT or TTS.

**Not allowed:** speech-provider or WebRTC SDK packages (`@elevenlabs/*`, `@deepgram/*`, `livekit-client`, `@livekit/*`, …), the browser `SpeechRecognition` or `speechSynthesis` APIs (guard-enforced), and UI kits that require a hosted account.

## Inspirations
The owner drops references into `docs/design/inspiration/` (see the README there). Before any page work:
1. Read `docs/design/inspiration/inspirations.md`.
2. In the PR, name which reference each page borrows from and what exactly (layout, density, colour, motion).
3. Never copy brand assets, logos or exact screens from a real company (§0.4).

## Pages and required e2e specs (Playwright, run in mock mode)
In mock mode (`npm run dev:mock`, `VITE_MOCK=1`):
- `src/lib/ws.ts` replays `eval/fixtures/golden_call.jsonl` at `?speed=` (default 20×).
- `src/lib/api.ts` serves `eval/fixtures/golden_summary.json` and a fake company directory.

Every page works without a backend in this mode.

| Page | e2e specs (file `e2e/<page>.spec.ts`) |
|---|---|
| NewTask | <ul><li>Fill text, pick Nimbus; profile is pre-filled and labelled "demo data"</li><li>Plan preview shows goal and keywords</li><li>Start call navigates to LiveCall</li><li>Mic button exists; recording UI shows a 30 s cap</li></ul> |
| LiveCall | <ul><li>Status pill passes through IVR → On hold (timer ticks) → Agent found</li><li>Each `decision` shows reason plus a rule/LLM badge, and DTMF digits; a masked mobile number is never shown in full</li><li>On `human_detected`, a full-screen join prompt shows with the Join button visible without scrolling at 375×667</li><li>Join opens the audio UI (worklets mocked); Mute and Hang up are present</li></ul> |
| Summary | <ul><li>Outcome badge reads `ticket_raised`</li><li>The ticket card shows `NMB48213377` with ▶; clicking ▶ seeks the audio element to `start - 0.5`</li><li>`next_steps[0]` shows ⚠ unverified with its reason</li><li>The transcript is colour-coded by track; the usage panel renders</li></ul> |
| CompanyConsole | <ul><li>Incoming call card with Accept and End</li><li>Scenario controls (§11.4) post to `/api/company/scenario` (mocked)</li><li>The current IVR node is shown</li></ul> |
| EvalReport | <ul><li>Renders charts from `eval/reports/latest/*.png` (fixture PNGs in mock)</li><li>The metrics table shows a scorecard status column</li></ul> |
| All pages | <ul><li>No horizontal scroll at 375 px</li><li>axe-core: zero serious or critical violations</li><li>Keyboard reachable: Tab to Join and Hang up</li></ul> |

## Definition of done for frontend PRs (L4)
1. `package.json` has scripts `typecheck`, `lint`, `test` (vitest), `build`, `e2e` (Playwright, starts the mock server itself) and `dev:mock`. `scripts/check.py` runs them.
2. e2e passes at the 375×667 and 1280×800 projects.
3. The `/ui-check` skill:
   - saves screenshots of each page and each key state to `reports/ui/<page>-<state>-<w>.png`
   - reviews them against this brief and the inspirations
   - fixes all P0 and P1 issues
   - lists P2 issues in the PR
4. Approved screens get a Playwright `toHaveScreenshot` baseline in `e2e/__screenshots__/`. Updating a baseline needs a before/after pair in the PR.
5. Bundle budget: initial JS ≤ 300 KB gzip. Report it from `build`.

### UI review rubric (used by /ui-check)
- **P0:**
  - the join CTA is not visible or not obvious
  - the current state is unclear within 2 s of looking
  - a broken layout at 375 px
  - unverified data shown as verified
  - the real-looking full mobile number shown
- **P1:**
  - weak hierarchy (status not the most prominent thing)
  - decisions without a reason
  - ▶ clip missing on an evidenced field
  - contrast below WCAG AA
  - touch targets under 44 px
- **P2:** spacing and alignment inconsistencies, copy tone, motion polish, drift from the inspirations.
