# Words in the Sky and the route UI — worker vz-words-sky (2026-11-03)

Copy only: no identifier, type, CSS class, id, store key, API path or deep link
changed. The binding list is `docs/vocabulary.md`.

Scope: `frontend/src/components/flights/**` (minus `BehaviorsBoard.*`,
`SkyGuide.*` and SkyView's empty-state line "No sessions in the sky yet…"),
`lib/flight*.ts`, `lib/sky*.ts`, `lib/stop*.ts`,
`components/grid/FastTrackMenu.tsx`, `components/grid/WorkersMenu.tsx`
(WorkersMenu needed no change: its mode labels come from `lib/sessionMenu`).

## What changed

### Word swaps (applied only inside string literals and JSX text)

| Old | New | Where (examples) |
|---|---|---|
| chain / chains / Chain / Chains | route / routes / Route / Routes | ~190 strings in 38 files: tooltips, menus, toasts, aria-labels, shortcuts sheet, mode chips, palette, catalog, shelf, stop dialog, halt/rewind/jump copy |
| Stop the chain · End this chain — its session is kept · Stop this chain? | Stop the route · End this route — its session is kept · Stop this route? | FlightStrip, flightsApi `controlFlight` toast ("Route stopped — its session is kept") |
| Open the Chain tab — … · Its Chain tab: add, remove or move its stops | Open the Route tab — … · Its Route tab: … | FlightStrip, FastTrackMenu |
| Saved chains · saved chain · Saved chain · N stops · New chain | Saved routes · saved route · Saved route · N stops · New route | StopShelf, StopCatalog, SessionFlight, SkySelection, lib/stopCatalog `savedChainLabel`, lib/flights `newPlan` |
| Halt the chain here · Chain halted / carrying on / started | Halt the route here · Route halted / carrying on / started | lib/stopEditing, lib/flightsApi |
| Find a chain… · Which chains · Fit the focused chain · Fit all the chains · Chains and objects | Find a route… · Which routes · Fit the focused route · Fit all the routes · Routes and objects | SkyView toolbar, ZoomMenu, lib/skyShortcuts |
| on the board · off the board · back on the board · leaves the board · On the board | on the Sky · off the Sky · back on the Sky · leaves the Sky · On the Sky | GroundedLine, SkyView toasts, StopCatalog, StopShelf, StopParts, SkySelection, lib/skyIntake |
| aria-roledescription="canvas" | aria-roledescription="sky" | SkyView viewport |
| …open the Behaviors tile to answer it · Back to the tile | …open the Behaviors stop to answer it · Back to the stop | FlightChain, SkyLine, SkyChain |

### "If it fails" (lib/flights `ON_FAIL_LABEL`) — Halt is the default wording

| Old | New |
|---|---|
| Stop and tell me | **Halt** |
| Hand it to the agent | Let the agent fix it |
| Skip it | Skip |
| Ask me | Ask me (unchanged) |

### "Goes as far as" (was Fast-track)

| Old | New | Where |
|---|---|---|
| ⏩ Fast-track (picker head) · aria-label `Fast-track — <name>` | ⏩ Goes as far as · `Goes as far as — <name>` | FastTrackMenu |
| When the agent is done, go as far as | When the agent is done, it goes as far as | FastTrackMenu |
| Fast-track is set aside while its stops are on. It comes back once they finish or halt. | Set aside while its stops are on: they decide how far it goes. It comes back once they finish or halt. | FastTrackMenu |
| To do one step yourself, use the button beside ⏩ | To do one of these yourself, use the button beside ⏩ | FastTrackMenu |
| the bird's label "fast-track at Push" | "goes as far as Push" | lib/flightDiagram `lineWhere` |
| A fast-track rung — it moves as the session ships | Goes as far as — this stop moves as the session ships | lib/stopEditing |

### Other

| Old | New | Why |
|---|---|---|
| catalog badge "robot" (beside "you") | "MindFlock" | vocabulary: never "robot"; matches the palette's "MindFlock does" / "You do" headings (StopParts) — CSS class `actor-robot` kept |

## Kept deliberately (and why)

- Every remaining `chain`/`chains`, `flyway(s)`, `perch`/`perched`, `fast-track`
  hit in my files is code: the `"chains"` select-dynamic value, `"perched"`
  node state / Sky mode id, `"perch"` icon kind and class, `flyway` keys and
  types, `/api/hooks/flyway/`, the `#flyways` link, `landing` object ids.
- "takes off" in StopParts' handoff hint ("A route on the Sky takes off (it
  leaves the Sky)…") — the vocabulary allows it as a verb in a sentence.
- "Take off the shelf" (StopShelf) — the plain verb, not a bird noun.
- "the bird flies on" (lib/stopEditing), "The bird: flying …" — bird flavour.
- "Pan a step" (shortcuts) — distance, not a stop.
- "Zoom: fit and presets" (ZoomMenu) — zoom levels, not session templates.
- "Resize X's card", "Close a card" — the open session/behaviors card, not a stop.
- "paused here" / "paused at" bird states — separate from Halted (that is
  the failure halt); "Pause here" as a control is not used.
- lib/flights `SHIP_WORD.merge = "Merge"` — must match the backend ship stop's
  title ("in the words a ship step's title uses"); rename it together with the
  backend stop title if that becomes "Merge when green".
- "Show the Behaviors board", "in a board right of the routes" (SkyView) — the
  BehaviorsBoard object, not the Sky; the behaviors worker owns that name.
- Toasts that quote a backend kind label ("This server has no “Start a route”
  stop yet") now say route; the backend kind label must say "Start a route" too.

## Test pins updated

30 files in `frontend/src/__tests__` (birdDrag, fastTrackSurfaces, flightsUi,
grounded, intakeDefault*, landing, r18Sky, sky*, stop*, uiLanePolish,
zoomMenu): expected words changed, no assertion dropped. One structural
change: stopDialog's "the current one" test now strips the "If it fails" chip
row before listing action buttons, since that row now also says "Skip".

## Retired words OUTSIDE my files (for the integrator's sweep)

`lib/groundedLines.ts` — its error copy is pinned by `stopShelfSaved.test.ts`
(left at "saved chain" so the suite stays green; change both together):
- `:77` default name "New chain"
- `:108` "A saved chain needs a name"
- `:111` "Another saved chain is already called “…”"
- `:151` fallback "Start a chain"; `:165` "a chain"

Route / chain:
- `components/grid/Pane.tsx:993-999` — the **Chain pane tab** label + its tooltip ("Chain — this session's live diagram: its workers, its fast-track or chain…", "The chain halted — it needs you", "The chain needs you")
- `lib/sessionMenu.ts:320` "Open this session's Chain tab: add a stop, or a saved chain, to its chain"
- `lib/sessionState.ts:278` "— chain “…” needs you"
- `components/behaviors/BehaviorsCard.tsx:201` "The chain moves on without these answers"
- `components/TopBar.tsx:171` "…templates and scheduled chains"
- `components/intake/ScheduledTemplates.tsx:157` "templates and saved chains that start by themselves"
- `components/dialogs/TemplatesDialog.tsx:908` "Saved chain · N stops"
- `components/palette/CommandPalette.tsx:182` label "Saved chains…" (+ keywords at :170, :179, :184, :192 — keep "chain" as a search keyword, add "route")
- `lib/paneHeader.ts:60` "its flight or fast-track"; `lib/sessionActions.ts:640` "Its flight or fast-track is running"
- `components/flights/SkyGuide.tsx` (29 hits) and `lib/tours/sky.ts` (4) — first-run worker's

Fast-track → Goes as far as (the control name):
- `lib/autopilot.ts:134,136`; `lib/laneActions.ts:162,165,206,239,252,253,313,396` (the picker's lock/why texts and toasts reach FastTrackMenu from here; `LANE_LABEL` options also live in lib/autopilot)
- `lib/stage.ts:557-568` (the ⏩ tooltip); `lib/paneHeader.ts:67` "Fast-track ⏩"
- `lib/sessionActionTable.ts:190`, `lib/sessionMenu.ts:228,349,353` "Fast-track…"
- `lib/agentMessages.ts:565,583,622`; `lib/splitRun.ts:433`; `lib/newSessionSubmit.ts:139`
- `components/EventToasts.tsx:333`; `components/outbox/outbox.ts:83-84`
- `components/dialogs/FastTrackChoice.tsx:84,113`; `NewList.tsx:382,438,442`; `NewSessionDialog.tsx:1138-1164,1901`; `TemplatesDialog.tsx:220,242`; `newSession/FooterActions.tsx:95,192,236`; `newSession/WorkspaceFold.tsx:334,401`
- `components/intake/kit.tsx:1396-1454`; `components/settings/screens/Workspace.tsx:200,218,256,271`
