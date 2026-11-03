# Integration tidy-up — worker notes (vz-tidy, 2026-11-03)

This finishes the wiring the round's workers listed as "wanted" or "outside my
fence" (verify.md, words-backend.md, words-ui.md, words-sky.md), and sweeps the
frontend's copy against `docs/vocabulary.md`. Frontend only. `backend/web/static/`
is **not** rebuilt; the integrator rebuilds it once.

## Verify on the route — the wiring

| Wanted (verify.md) | Done |
|---|---|
| Done on a Verify stop parked on you is refused (409 `answer_behaviors`) | **`lib/flightsApi.stepDone`** handles it everywhere: a 409 `answer_behaviors` does not open an error card. It shows a toast ("Answer its behaviors (or Skip it) — the route goes on once each one has an answer") and calls `openBehaviors` for the flight's session (`flightBehaviorsTarget`: title + branch from the flights cache, else `{ next: true }`). New helpers: `isAnswerBehaviors(err)`, `answerBehaviors(flightId)`, `stepFix(flightId, stepId)` |
| Stop ⋯ menus (`useChainMenu`, `FlywayCanvas`) | `lib/stopEditing.menuItemsFor`: on a perched Verify stop, **Behaviors** (key `behaviors`) replaces Done. On a halted Verify stop, **Let the agent fix it** (key `fix`) is added after Retry. Both hosts handle the keys (`answerBehaviors` / `stepFix`). `StopMenuKey` gains `behaviors` and `fix` |
| `useSessionLine` (the stop dialog's Done) | On a Verify stop the dialog's Done button reads **Behaviors** and opens the card. `StopDialog` gains `doneLabel` / `doneTitle`. A halted Verify stop's dialog gets **Let the agent fix it** (`onFix`) |
| `outbox/WaitingRow` (the bell's waiting row) | On a Verify stop the row shows **Behaviors** (`openBehaviors({ title })`) in Done's place |
| `FlightStrip` `YouStepActions`, halted | A halted Verify stop shows **Retry · Let the agent fix it · Skip** (`POST …/steps/{sid}/op/fix`). Parked on you, it already rendered `VerifyStopActions` |
| verifyActions toasts "in Verify" | Session: "…already has behaviors — they're on its route in the Sky" / "they'll appear on its route in the Sky (up to three minutes)". Branch with no session: "…they're on the Behaviors board in the Sky" / "they'll appear on the Behaviors board" |
| barDefs / SidebarBarsPicker "verify" bar | Removed from `SIDEBAR_BARS` and the picker's notes. A saved `bar_order` / `hidden_bars` that still names `verify` keeps working, because `orderedSections` drops unknown keys (pinned in `barDefs.test.ts`) |
| replayTour `verify` surface | Replaying the Verify tour calls `openBehaviors({ next: true })`, which opens the Sky, and no longer opens the retired dialog name |
| Dead code | Deleted `VERIFY_BLURB` (`lib/vocab.ts`) and `components/dialogs/verify/emptyChecklist.ts`. Removed `topBarQuiet`'s `verify` flag and its `checklists` input (`lib/calm.ts`, `TopBar.tsx`; TopBar still calls `useTestPlans()` to keep the poll warm). `DialogName "verify"` and `installVerifyRedirect` are **kept** as a catch-all for extensions and old links |
| The Verify tour no longer opens by itself | **Decided: it opens once, the first time a Behaviors stop shows on a route in the Sky.** `SkyView` calls `useTourOnFirstSight("verify", behaviorsOnARoute(testPlans, rows, titles))`. This is the new hook in `onboarding/useAutoTour.ts`, using the first-use rule: any hour, after the first prefs pull, hints on, not already in `tours_seen`. If another tour or the welcome card is up, it waits until that one closes. Once it has opened it counts as seen, like every other tour |

### For the integrator: the halted face (keep-out files)

`verifyStopFace(short, state)` is in `lib/behaviors.ts`. The one-line hookup
belongs in **`lib/flightDiagram.ts` `withBehaviors`**, which was keep-out for this
task:

```ts
// import { verifyStopFace } from "./behaviors";
if (verify) return { ...d, nodes: d.nodes.map((n) => (n.id === verify.id ? { ...n, behaviors: { ...mark, short: verifyStopFace(mark.short, n.state) } } : n)) };
```

Today a halted Verify stop's face says "Failed · 2" next to the halt badge. With
the hookup it says "Failed · 2 — halted".

## Words (words-backend.md "frontend pins")

- Fixtures now match the server's copy:
  - `r18Sky.test.ts`: "Wait for it to finish". The label "Start a route" was already updated.
  - `skyOnramp.test.ts`: "Route on me".
  - `skyLooks`, `skyVisibility`, `skyResize`, `stopGroups`: ship stops are titled "Merge when green".
- `lib/stage.ts` ⏩ tooltip: "Goes as far as Open PR …", "Shipping halted: <reason>", "Goes as far as Agent only. Click to choose…", and "Click to change how far it goes (Ctrl+K F)". Pinned in `autopilot.test.ts` and `fastTrackSurfaces.test.ts`.
- `NotificationsBell`, `agentMessages`: the vz-verify and vz-words-ui workers had already changed these. No edit needed.

## Copy sweep (frontend/src outside the keep-out files)

| Old | New | Where |
|---|---|---|
| templates and scheduled chains | templates and saved routes | `TopBar.tsx` (Schedule tooltip) |
| Saved chains… | Saved routes… ("route" keywords added; "chain" kept as a keyword) | `CommandPalette.tsx` |
| built-in chain | built-in route | `lib/skyIntake.ts` |
| Fast-track (ladder head) · Fast-track: <state> | Goes as far as · Goes as far as: <state> | `SessionFlight.tsx`, `useSessionLine.tsx` |
| its checklist is ready in Verify · Couldn't write the checklist for · New/Rewrote the checklist for … N steps · "a checklist" · checked — N steps failed / need you · everything passed | its behaviors are ready on its route in the Sky · Couldn't write the behaviors for · New/Rewrote the behaviors for … N behaviors · "these behaviors" · N behaviors failed / need you · every behavior verified | `state/queries.ts` toasts |
| Rewrite the checklist · Writing the checklist… · couldn't write a checklist · has a checklist · no steps · add the steps · Add a step · the steps below · "Checklist…" (verdict short) | … the behaviors · behaviors · Add a behavior · "Writing…" | `components/dialogs/verify.ts` (read by the Behaviors card / settings) |

**Deliberate keeps:** "Setup checklist", including SetupChip's "click for the
checklist". Search keywords that include the old words. Code values
(`"perched"`, `"chains"`, `flyway` keys, `landing` ids, CSS classes).

### Retired words left in keep-out files (for the integrator)

- `lib/flightDiagram.ts:1050`: a rung's sub line `"fast-track" + …` is shown on the box. Wanted: "goes as far as".
- `lib/flights.ts:777` `SHIP_WORD.merge = "Merge"`: the backend ship stop is titled "Merge when green" now. Words-sky kept it on purpose because it must match the backend title; rename both together.
- `lib/flights.ts:713`: the `#flights/checklists` deep-link word. Code, kept.
- No user-visible retired word was found in `skyGraph.ts`, `SkyChain.tsx`, `StopParts.tsx`, `SkyWires.tsx`, `FlightChain.tsx` or `docs/flights.md` (only identifiers, classes and comments).

## Open / not done

- When a Verify stop's *failure* is handed to you (`on_fail: you`), it still takes Done on the server. The menus offer Behaviors there anyway, because the diagram node does not carry `failed_reason`. The card answers both cases.
- `state/store.ts` `DialogName "verify"` and the redirect stay until nothing outside calls `openDialogFor("verify")`.

## Tests

- New: `__tests__/verifyStopTidy.test.ts` (menus, 409 handling, fix op, waiting row, Verify tour rule and replay).
- Updated pins: `barDefs`, `tours`, `firstRunVision` (the Verify tour is now vocabulary-checked too), `autopilot`, `fastTrackSurfaces`, `flightsUi`, `flatTopBar`, `scheduleSurface`, `firstHourShell`, `verifyAnotherBranch`, `verifyDialogUi`, `shellVerify`, `testPlans`, `behaviorsSettings`, `r18Sky`, `skyOnramp`, `skyLooks`, `skyVisibility`, `skyResize`, `stopGroups`.
