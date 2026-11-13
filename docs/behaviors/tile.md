# Behaviors tile — as built (vs-tile)

docs/behaviors.md §1 and the frontend half of §5. Notes for the integrator.

## The tile on the chain

- **Diagram** (`lib/flightDiagram.ts`): a new node kind `behaviors`
  (`BEHAVIORS_ID = "behaviors"`), and `DiagramNode.behaviors?: BehaviorsMark`
  (`{ planId, short, status, tone }`). `withBehaviors(d, mark)` is a
  rendering view: with no mark it returns `d`; when the line has a Verify
  stop (`verifyStopOf`: the LAST plain `verify` stop) that stop wears the mark
  and no node is added; otherwise a `behaviors` node goes after the last
  main-row node (the session's agent when there are no stops), wired in with
  a `line` edge (a `join` out of every member of a trailing parallel group).
  It has no step, no index, is never editable.
- **Layout** (`lib/skyGraph.ts`): `autoPlace` puts the tile `BEHAVIORS_GAP`
  (40 px) further right than a stop would go, so the trailing + (which still
  hangs off the last STOP: a new stop goes before the tile) has room. A
  main-row node wider than a tile (an open card) now pushes the next one
  along (`max(PITCH_X, prev.w + PITCH_X − NODE_W)`), and one taller than a
  tile pushes the branch rows down. Nothing on the main row was ever wider
  before, so existing chains place exactly as before.
- **Where it's applied**: `useSessionLine` — the one hook both homes use —
  returns `graphDiagram = behaviors.apply(line)` and `behaviors` (the
  `BehaviorsTile`). The line's own handlers keep speaking the raw `line`;
  `useNodeMoves`, `hangFromAgent`, `chainGraph` see the tile. So the Sky
  (SkyLine) and the grid's Flight tab (FlightChain) draw it with no
  extra plumbing.
- **Face** (`SkyChain.tsx` `tile()`): a node with `behaviors` gets the
  ✓-shield (`TileIcon kind="behaviors"`, StopIcons.tsx, family `people`),
  sub line `mark.short`, classes `sky-behaviors is-<tone>`; the derived tile
  also has `sky-node-behaviors`, a ✓ / ! badge when verified / failed, and a
  `.fl-icon-btn` ⋯ (`sky-bh-more`) that opens the same menu as a right-click.
  It is never draggable, has no live ports (it can't be wired off), and
  Delete does nothing to it (`removableBox` is false for it). A click on it,
  or on the Verify stop wearing the mark, goes to `onOpen` (not select);
  Enter / double-click on the Verify stop still opens its stop dialog, and a
  right-click on it is still its stop menu.

## The hook: `useBehaviorsTile(title, row, { flight, listen })` (SkyChain.tsx)

- The plan: `planForSession(useTestPlans().data.plans, row)`; the mark:
  `behaviorsMarkOf(plan)` (`behaviorSummary`'s tone / status / short).
- `onOpen(n)` toggles the card; `renderOpen(n)` draws the BehaviorsCard in
  the tile's place (an opaque `.sky-bh-open` box; Esc or the card's × folds
  it back); `big(d)` gives that node its box (`behaviorsCardBox(plan)`: 380
  wide, 220–560 tall by rows). The open plan is recorded with
  `useBehaviorsFocus.setOpenPlan` (cleared on fold / unmount).
- `flightStep` is passed to the card only when the card is a Verify stop's
  AND the live flight is at that stop.
- ⋯ / right-click on the tile (not on a Verify stop): "Open the behaviors" /
  "Fold the behaviors back", "Delete these behaviors". Delete asks in the
  tile's place (InlineConfirm: Delete these behaviors / Keep them), then
  `removePlan` (lib/verifyActions).
- `listen` (the Sky only: `useSessionLine(..., { listenBehaviors: true })` in
  SkyLine): reacts to each new `useBehaviorsFocus().request.n` that names
  this plan — `findPlan` (plan id, title, branch), Alt+V's `next` through
  `nextNeedingYou`, or the session's own title (`requestNamesPlan`). It never
  calls `take()`.
- The frame: `frameClass` is `is-bh-bad` (a thin red top edge,
  screen-thin via `--sky-z`) or `is-bh-you`; on the Sky's frame bar and the
  Flight tab's, a "Needs you" chip (the existing `tone-attention` state chip)
  shows when a behavior waits on you and the session's own chip doesn't
  already say so.

## The Verify stop (§5)

- `FlightStrip.tsx`: `YouStepActions` on a Verify stop parked on you
  (`waiting_you`) renders `VerifyStopActions`: the BehaviorsCard (`compact`,
  `flightStep={{ flightId, stepId }}`) — **no blanket Done**. The pane's
  one-line strip (`compact`): a primary "Behaviors" button
  (`openBehaviors({ planId })`) and Skip. Before the behaviors are written:
  "Writing the behaviors…" and Skip. A halted Verify stop keeps Retry / Skip.
  Skip stays on the stop's own ⋯ everywhere; next to the full card it is the
  card's ("Skip this stop", vs-card) — there is no second Skip beside it.
- `liveBoxParts(flight, expand, onCancelAsk, { verifyInPlace })`: on the
  chains SkyChain draws (`useSessionLine` passes it) a parked Verify stop
  stays a tile — the card opens in its place instead of growing under the
  tile. The Board card / drawer (`FlightCard`, SkyDrawer) still expand it,
  with room for the card (320 × base + 220, the card scrolls).
- `lib/needsYou.ts` `needsYouBadge`: a flight parked on a Verify stop now counts
  in the Sky's Needs-you badge.
- `lib/flights.ts` `legacyFlightsRoute("checklists[/<b>]")` →
  `{ to: "sky", focus: "", behaviors: { branch: b } | { board: true } }`.
  The `to: "sky"` shape keeps store.ts / surfaceHash.ts compiling and
  landing on the Sky; `behaviors` is the target to hand to `openBehaviors`.
  (Imported as a TYPE only: lib/behaviorsFocus reads the store, which reads
  lib/flights.)

## For the integrator (outside this fence)

Connected by behaviors-connect (2026-11-03):

1. `state/store.ts` `openDialogFor("flights", "checklists[/<b>]")` calls
   `openBehaviors(r.behaviors)` right after `showSky` (a static import; the
   store ↔ behaviorsFocus cycle is safe because each side only calls the
   other inside an action).
2. `lib/surfaceHash.ts`: an old `#flights/checklists` link now reads
   `#behaviors`; `#flights/checklists/<branch>` reads `#sky` (its plan id is
   not known until the plans load). `#behaviors` / `#behaviors/<plan id>` is
   accepted on load and on Back/Forward (`parseBehaviorsLink` →
   `openBehaviors`) and stays the address until the view moves. An old
   `#verify…` link is still the Verify dialog's (see docs/behaviors.md, "Still
   to connect").
3. `flatTopBar.test.ts` pins both: `[null, null]` dialog, the Sky, the
   behaviors request (`{ board: true }` / `{ branch: "feature/x" }`), the
   rewritten address, and a new case for the `#behaviors` links.
4. `behaviorsCardBox` now sizes a row at 100 px (measured 89–108 at
   BH_CARD_W); at 64 the open card showed only its first row of three. Below
   ~0.8 zoom the card's type grows (`--bh-s`), so the rows scroll there.

## Tests

`frontend/src/__tests__/behaviorsTile.test.ts` (38): the diagram gets the
tile (after the last stop / right of the agent / joins out of a trailing
parallel group), the Verify stop doubles as it, layout (gap, +, no ports,
an open card makes room), the face (shield, title, sub, every tone class,
✓ / ! badges, ⋯, not draggable), renderOpen draws the card in place, the
frame classes and CSS, the click / menu / focus wiring in both homes,
`requestNamesPlan`, and the parked Verify stop's card (no Done; compact:
Behaviors + Skip; unwritten: Writing… + Skip; a Wait-for-me keeps Done).
`flights.test.ts`: the badge now counts a Verify stop; the checklists route.
