# Behaviors board + camera on request — as built (vs-board, 2026-11-03)

Covers docs/behaviors.md §3 (the board) and the Sky side of §4 (a request
flies the camera). Everything lives in
`frontend/src/components/flights/BehaviorsBoard.tsx` (+ `.css`); SkyView
carries a few lines.

## The board

- A Sky object, layout key `behaviors` (`BEHAVIORS_KEY`), registered in
  `lib/skyObjects.ts` as `BEHAVIORS`: placed in the **right column, above the
  Finished strip** (the registry's order is now intake, palette, behaviors,
  landing, device, person, note). Same calm look as the Finished strip: one
  hairline, radius 14px, a 30px title bar it drags by (`data-sky-head`), a
  one-line "what this is", quiet rows, a resize corner.
- **Rows** = `orphanPlans(plans, framed)`: `plans` from `useTestPlans()`,
  `framed` = the instances whose session the Sky draws as a line — its own
  line, or its lead's line for a worker drawn inside it (the same rule as
  SkyView's `focusLine`).
- **Groups**, empty ones left out: Needs you · Failed · Checking · Not checked
  · Verified today (tone `you`, `bad`, `busy`, `quiet`, `ok`). "Verified
  today" = verified behaviors whose newest run (`runs[].at`) is under 24 h
  old; older ones leave the board (it is not an archive). "Checking" also
  holds "Writing…" (both are the `busy` tone).
- A row: tone dot, `planName(plan)`, `behaviorSummary(plan).short`, the
  branch under it. Click → it opens into `<BehaviorsCard plan compact />`
  (one open row at a time; the card's × folds it).
- The bar: "Behaviors", a small count of rows that need you or failed, ⋯
  (opens `<BehaviorsSettings />` in a portal popover; Esc / a click away
  closes it) and ×.
- **Shown when**: it holds a row that needs you or failed (not yet put
  away by ×), OR it was turned on from the Sky's ⋯ → "Show the Behaviors
  board" (per device, `localStorage["mf_sky_behaviors"]`, every access in
  try/catch), OR `useBehaviorsFocus().boardRequested`. Never on a phone or in
  This session. Hidden → not in `skyObjects()` at all (no spot, no room).
- **×**: turns the switch off, clears `boardRequested`, closes the open row
  and puts away the urgent rows it had (in-memory, this visit) — a NEW plan
  that needs you or fails brings it back. The ⋯ toggle while shown does the
  same as ×; hidden, it turns the switch on.

## Camera on request (`useBehaviorsRequest`)

- Waits until the plans have loaded (or failed) and the Sky is placed
  (`vp` measured, instances in, `settled`; always ready on a phone). A
  request made before SkyView mounted (openBehaviors switches the view
  first) waits in the store and is taken then.
- `aimFor(target, …)`: `board` → the board; `next` → `nextNeedingYou(plans)`
  else the board; planId / title / branch → `findPlan`; a plan whose session
  has a frame → that line; a plan without one → the board with that row
  open; a title with a frame but no plan yet → its line (no card); nothing
  found → the board.
- `applyRequest`: line → `setOpenPlan(plan.id)` (vs-tile opens the card in
  the tile); board → `setBoardRequested(true)` + the row expanded. Then
  `take()`.
- SkyView's `flyToBehaviors(id)` glides the camera with the minimap's jump
  (`centerOn`, zoom ≥ 0.85) and flashes the line. Retried each render for up
  to 3 s while the target has no rect (the board's first render). ~320 ms
  later the open card (`.sky-line-slot[data-slot=…] [data-plan=…]`) or the
  board row (`[data-bh-row=…]`) gets the coach tour's `mf:tour-reveal` event,
  so the existing reveal listener fits it whole into view (never zooming
  in). If the card markup lacks `data-plan`, that step is a silent no-op.

## Drawer

SkyDrawer's "Needs you" rows (`ChecklistRow`, name kept) now call
`openBehaviors({ planId })` with titles "Show the behaviors that need you" /
"…that failed"; the unsupported-server line says "Waiting items and
behaviors still work."

## For the integrator

Connected by behaviors-connect (2026-11-03):

- `flatTopBar.test.ts` now pins the drawer's
  `onOpen={(planId) => openBehaviors({ planId })}` ("…the drawer opens
  behaviors / Intake").
- The reveal step's `[data-plan="<plan id>"]` is on the real card: both the
  tile's open box (`.sky-bh-open`, SkyChain) and `BehaviorsCard`'s root carry
  it. Seen rendered: `#behaviors/<plan id>` flies to the frame and opens the
  card; `#flights/checklists` lands on `#behaviors` with the board in view.
- `BehaviorsCard`'s `onClose` folds a row and `BehaviorsSettings`'s `onClose`
  closes its popover (both wired).
- The "an open row draws the card" render test stubs `document` for that
  render alone (the card's instances query reads `document.hidden`; the
  `pollInterval` line itself is pinned by `uxRound.test.ts`).
- A board row's card is `compact`, which now reads at the board's own type
  size (`--bh-s: 1`) instead of the Sky zoom's larger type, and its head lets
  the count ellipsize so ⋯ and × stay inside a narrow board.
- Still true: in This session (mode "one") and on a phone the board never
  shows, so a request for an orphan plan there only takes the request.
