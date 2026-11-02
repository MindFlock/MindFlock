# Round 18 — Sky integration notes (r18-sky)

Spec: `docs/flights.md` "Round 18: free-floating chains (the n8n look) —
contract". Built against the pre-committed `skyGraph.ts` / `SkyChain.tsx`
seams. Neither file is edited here. Checked against r18-graph's real versions
(merged on `emandel2630/flights`, f0e0f420) in a scratch copy: tsc is clean
and vitest is 5661/5662 (the one failure is the skySnap "snaps the pressed
box" timeout, which also fails on the base). Your commit 0ecc6d65 (the card's
double-click fold, the `mf:tour-reveal` camera glide) merges into SkyLine.tsx
and SkyView.tsx with **no conflicts** (`git merge-file` checked).

## What changed

### Lines are chains
- **SkyLine** draws a small `.skl-label` (Bird, name, where, age, pin, ⋯) and
  then `<SkyChain>` from `chainGraph(line.graphDiagram, {head, key: title,
  flightId})`. The head is `{kind: "session", title: windowName, sub: where ||
  task, session: title, color: chainColor(title), node: <the session node>}`.
  `renderHead` = the state chip. The head tile is the chain's drag handle (its
  press bubbles up to the label's `onDragStart`; a press that moved is not a
  click). A click on the head opens the session card. Cards (the session's,
  and each opened worker's) sit in a `.skl-cards` row under the chain.
  **PaneCard is untouched**, so your double-click fold is kept as-is. The ◧,
  the box-size ⋯ items, the corner resize and the scaled sizer are gone (a
  chain's tiles have one size). `dragBox`, `presetPatch`, `nextSize` and
  `flownCanvas` are still exported for the classic row.
- **GroundedLine** has no "No session yet" head box and no card frame. It
  draws a `.skl-label` (name / rename, chip, ⋯; still the drag handle) and
  then a chain. The new `head` prop takes a trigger head (from SkyView's
  `triggerHeads`), else `NO_HEAD` (headless, with SkyChain's socket).
- `.skl.skl-chainline` (SkyLine.css) drops the card look: no border,
  background, shadow or padding. Focus rings the label only. A chain the
  marquee took whole keeps the sky's one solid selection ring.
- Slot size = what the line root measures (label + `SkyChain` root sized by
  `bbox` + cards). Before measuring, SkyView estimates with `chainSize(diagram,
  head)`.

### Hooks (useSessionLine / useGroundedLine)
- New on both: `graphDiagram` (the line in the **flight's** indexes, which
  chainGraph / wireIntent need; `diagram` / `canvas` stay drawn-place-counted
  for FlywayCanvas), `graphCanvas` (the raw handlers), `paletteAfter(node |
  null, pos?)`, `anchor` / `anchorPos` (where the palette or dialog floats).
- A stop picked from a palette opened at a drop carries `pos`. `wireStep` now
  sends `pos` (the backend keeps it on `POST /steps`).

### Wiring (useSkyWiring.ts, SkyWires.tsx)
- `useChainRegistry`: each line calls `registerChain(entry)` (graph, the
  chain's offset in its slot, its `paletteAfter`) from a layout effect
  (`useChainEntry`). The registry always stores the latest entry. It bumps
  its `version` (which redraws the cross-chain wires) only when `entrySig`
  changes (ids and spots). **A render loop was found and fixed:** SkyView's
  `kinds || []` / `plans || []` made new arrays while loading, which remade
  every graph, which re-registered, which re-rendered. The fallbacks are
  stable consts now, and the signature check guards against it either way.
- `useSkyWiring`: `startWire(key, node, side, e)` from each chain's
  `onPortDown`. It uses window pointer listeners and draws the band in plane
  px. Hits are found in the DOM (`elementFromPoint` → `[data-node]` inside
  `.sky-chain[data-chain]`, never inside `.skl-card`). Targets come from
  `wireIntent` node by node (`wireTargetsOf`; this is equivalent to r18-graph's
  optional `wireTargets`, used here so the branch compiles against the stubs).
  Shift re-lights the targets, Esc cancels, and the click after a drag is
  eaten. SkyView's `onDownCapture` leaves `.sky-chain [data-port]` alone in
  both tools. The old `startConnect` / `.fl-box-add` gesture is removed: the
  `+` stub and the session head's out port now do it as `attach` / `merge`.
- `runWireIntent(intent, ctx, deps)` (pure, tested):
  - move → `moveStop(f, sid, loopSlotAfter(after))`
  - loop → `tackOn(loopStepTo(...), loopSlotAfter(from))`
  - merge / attach → SkyView's `mergeGrounded(from, into, was, at?)`, which
    now takes a flight index and a stops-only target, and strips the merged
    stops' `pos`
  - handoff → `tackOn(handoffStepFor(def, ids, "flight:<id>"),
    loopSlotAfter(from))` (`tackOnNew` with no flight; a toast if the server
    lacks the kind)
  - palette → the chain's `paletteAfter(after, drop - tile centre, snapped to
    8 px)`
  - split → `splitOffChain(f, sid, {x: 0, y: 0})`. The new chain's slot is
    pinned at the drop. Its other stops' saved spots are re-based onto the
    new origin (`saveStopPositions`).
- Node moves: `useNodeMoves` keeps an optimistic spot map, cleared once the
  server pos matches (or on failure), and `queueStopPos` (flightsApi) batches
  them into **one `POST …/layout` per flight** 250 ms after the last drop.
- Cross-chain wires: `handoffLinks(flights)` (groundedLines) lists every
  Handoff stop with a `flight:` target or a started `detail.child`.
  `handoffWires` draws each one from the Handoff tile's out port to the target
  chain's head (or first tile) in port: dashed, in the source session's
  colour, dimmer once started. These are drawn in `<SkyWires>` (`.sky-xwires`,
  plane coordinates, `pointer-events: none`), with the band.
- `triggerHeadFor(flight, flights, windowName)`: "When <session> reaches
  <stop>", "runs on <session | a new session>", `chainColor(source session)`.

### Panels
- `ChainPanels`: the palette or the stop dialog is absolutely positioned
  inside `.skl-chain-wrap`, right of its tile (or 24 px right of a wire's drop
  spot). It never changes the slot's measured size.

### StopDialog / StopParts
- `dynamic: "chains"` → `BoardChoiceField`: chips grouped "On the board" (the
  stops-only chains, `flight:<id>`) and "Saved stops — a fresh copy each time"
  (`plan:<id>`). `dynamic: "sessions"` → "a new session" (`new`) and then
  every session title (`windowName` labels). The options are read from the
  query cache (`queryClient.getQueryData`), so StopForm needs no provider. A
  value no longer on the board stays visible as its own chip.
  `lib/flights.ts`'s `StepKindField.dynamic` type is still `"stops_before"`
  (outside my fence), so StopParts reads it as a string (`boardDynamic`).
  **Integrator:** widen it to `"stops_before" | "chains" | "sessions"`.

### Tour hooks (kept on the element itself)
- `sky-line`: the session line's slot (SkyView, unchanged).
- `sky-line-bar`: the chain's label.
- `sky-session-box`: the head tile, and `sky-stop`: the first stop tile.
  Both are set on SkyChain's own elements by `useTourHooks` (a DOM effect),
  since the seam has no prop for them. If r18-graph adds a `tourHooks` /
  `headProps` prop, switch over.
- `sky-grounded`: the stops-only chain's label, and its `.sky-socket`.
- `flights-bird`, `sky-selbar`, `sky-shelf` are untouched.

### SkyGuide
- "Chains, tiles and wires" (ports, dragging a tile, out-port drops, `+`,
  pulling an in port off to split) and "Stops on their own, and handoffs"
  (the socket, Shift-drop = Start a chain, the trigger tile, the dashed wire)
  replace the old "No session yet" copy.

## Still to do (for the integrator)

- **Props r18-graph added that are not passed yet** (the stubs don't have
  them): `kinds` on `<SkyChain>` (family colours from the server) and
  `onPillDown` (re-aiming a loop pill in the graph). `onLoopPill` is wired:
  it opens the Loop's dialog.
- **Right-click menu:** `onMenu` is not wired (the classic StopMenu was
  FlywayCanvas's).
- **Worker birds in formation:** FlywayCanvas flew these; SkyChain draws one
  bird.
- **Negative bbox:** a tile dragged left of or above the chain's origin
  overflows its slot (SkyChain sizes its root `bbox.x + bbox.w`). Tidy,
  marquee and snapping use the slot rect.
- **Existing layout behaviour, not new:** lines are placed under the intake
  lane at its first measured height. At a nearer zoom the lane draws its
  cards and grows over them. The base does the same.
- Rebuild `backend/web/static` after the merge (I never build into the
  worktree).
- **Outside my fence:** `frontend/src/__tests__/flightsUi.test.ts` ("the sky
  view: … every session a line with the bird on its active box") still asserts
  the row's `>Tack on a stop<` + and `.fl-bird-marker`. It fails on this
  branch. The fix, a 5-line change asserting `data-chain` on each line and
  the `bird={birdPerch(moves.diagram)}` source, is in the scratch patch
  `/tmp/claude-1000/-home-emandel2630--mindflock-worktrees-emandel2630-r18-sky-18e43fd578908858/7d51475c-55e3-42ff-874f-de5068097847/scratchpad/flightsUi-test.patch`.
  It passes against both the stubs and the real SkyChain.
- **Outside my fence:** `lib/flights.ts` `StepKindField.dynamic` should
  widen to `"stops_before" | "chains" | "sessions"`.

## Tests

- New: `r18Sky.test.ts` (29 tests):
  - heads and handoff links, the trigger head, `handoffStepFor`
  - handoff wire geometry, `nodeOfStep`, `panelSpot`
  - `wireTargetsOf` / `nodeUnder` / `entrySig` (loop-proof) / `withLocalPos`
  - every `runWireIntent` op against mocked flight routes
  - `queueStopPos` batching against the mocked client, `wireStep` pos
  - the chains / sessions choices and their chips
  - the host wiring and tour hooks pinned in the source
- Rewritten to the chain behaviour: skyLine, skyResize, skyVisibility,
  skyZoom, skyLooks, skySelect, skyMerge, skyView, grounded, flightsUi,
  skyGuide. They assert only markup that the stub SkyChain and the real one
  both draw (`data-chain`, the label, cards), plus the source.
- `skyGraph.test.ts` is untouched (r18-graph's).

## Screens (scratch, merged picture with mocks)

The scratch directory is
`/tmp/claude-1000/-home-emandel2630--mindflock-worktrees-emandel2630-r18-sky-18e43fd578908858/7d51475c-55e3-42ff-874f-de5068097847/scratchpad/`:

- `sky.png`: the board after Fit (four session chains, the trigger head, the
  dashed handoff wire)
- `sky-zoom.png`: close-up of a session chain
- `sky-palette.png`: a wire dropped on empty sky; the palette opens at the
  drop
- `sky-attach.png`: a session tile's out port dragged to a socket, targets
  lit (on release: `POST /api/flights/f_live/steps` with the group, then
  `DELETE` of the stops-only flight)
