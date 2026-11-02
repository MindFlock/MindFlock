# Round 20 A — a session is a frame, the agent is a tile (as built)

Worker `r20-frames`. Spec: docs/flights.md "Round 20" part A. The owner:
"we are weirdly conflating agent with session — I often click on session to
edit what the agent is doing". This round draws the two as two things.

## What changed, by file

All rendering-layer: no graph model change. `lib/skyGraph.ts`,
`lib/flightDiagram.ts`, `lib/flights*.ts` and `useSkyWiring.ts` are untouched
(r20-parallel owns them).

- **SkyLine.tsx / SkyLine.css**: the line root (`.skl.skl-chainline.skl-frame`)
  is the session's frame.
  - `.skl-frame-box` is the frame itself: an absolutely positioned box behind
    the chain, 1px session-colour border at ~38% alpha, ~5% fill, radius 14,
    `pointer-events: none` (empty frame area still pans / marquees as empty sky
    did). Its left/top go negative when a tile is dragged left of / above the
    chain's origin (`graph.bbox.x/y < 0`), so the frame always holds every
    tile. The bar follows with `margin-left` (widens it) and `translateY` (no
    reflow, so `useChainEntry`'s dx/dy stay right).
  - The bar (`.skl-bar.skl-label.skl-frame-bar`, still `data-tour="sky-line-bar"`,
    still the drag handle) shows name · branch · state chip · where · age ·
    📌 · ⋯. Double-click renames: an alias via `setAlias`, a name another
    session has is refused (`nameTakenBy`), as the rail's row does. The Bird
    glyph left the bar (the bird is on the agent tile).
  - The head is the agent: `title` = `agentTitle(provider)` ("Claude",
    "Codex", "Agent" when unknown); `sub` = the task headline, else the create
    task, else the last prompt (or round 19's "ready · …"). "Where the flight
    is" moved to the bar.
  - Right-click on the agent tile (or a sub-session's agent) opens the agent
    menu (`useAgentMenu`): Open terminal / Fold the terminal, Send a prompt…,
    Stop (Resume while paused), then a "Session “name”" label line and the ⋯'s
    items (minus Open, which Open terminal already is). Stops keep their menu.
  - The ⋯ keeps "Open session" / "Fold the session back" (skyVisibility.test
    pins those labels — outside this worker's fence).
  - Sub-sessions: `subFramesOf(rows, title, flights, kinds)` → each child's
    agent name and its own chain's stops; `subFrameBoxes` feeds every child
    node's box to `chainGraph`'s `big` (an open card's box when open).
    `hangFromAgent(diagram)` re-hangs every `below` child from the session
    node (so the dashed bus leaves the agent tile) and restores the straight
    wire across the stop they were hung at. `branch` workers (spawned at one
    of the flight's stops) keep their stop: that is where they came from.
  - CSS fix: the "card open → hide the head tile" rule was a descendant
    selector, so the open card's own Flight tab lost its head tile. Now
    `.skl-chain-wrap.has-card > .sky-chain > .sky-head …`.
- **SkyChain.tsx / SkyChain.css**: the head of a session chain gets
  `sky-agent` + `data-agent`, the sparkle icon (`TileIcon kind="agent"`), a
  plain rounded tile (no session "pill" shape), aria "Agent: Claude — …".
  A `child` / `branch` node with a session draws as `.sky-subframe`: a bar
  with its name, its agent tile (sparkle in its own `chainColor`), agent
  name, summary, chip, its bird on the tile, and a `MiniChain` strip (one
  18px square per stop, state-coloured, capped at 8 + "+n") when it flies
  one. Sub-frame tiles draw no ports. The bus (`fan` into a sub-session) gets
  `sky-wire-bus` (dashed). New exports: `agentTitle`, `agentDoing`,
  `subFramesOf`, `subFrameSize`, `subFrameBoxes`, `hangFromAgent`,
  `isSubSession`, `SubFrame`, `MiniStop`, size constants.
- **useChainMenu.tsx**: `agentMenuItems`, `useAgentMenu` (StopMenu in a
  portal + a `PromptComposer`: textarea, Enter sends, Shift+Enter new line,
  Esc/press outside closes, clamped to the viewport), `sendPrompt` →
  `POST /api/instances/{title}/send {text, dialog_safe: true}`. Stop is
  `pauseSession` (the app's "stops the agent, the session stays"); there is
  no interrupt-only route on the server.
- **FlightChain.tsx / Flights.css**: the Flight tab draws the same — a
  `.fl-chain-frame` with its bar (name · branch · state · where) round the
  chain, the agent head, sub-frames (`useInstances` + `useFlights` for
  `subFramesOf`), `hangFromAgent`. A click on the agent (or Open terminal)
  switches the pane to its Agent tab (`setLastTab(title, "agent")`); a
  sub-session's selects it. Tiles dragged to negative spots shift the chain
  in by margin (no host bookkeeping here).
- **lib/tours/sky.ts, SkyGuide.tsx**: "A session is a frame" / "The agent
  starts the chain" / stops-only chains "with no frame"; the guide's Lines
  section and sketch describe frame, bar, agent tile, sub-session frames.

## Kept working

ReadyHead on the agent tile; the Loop self-wire on the head (head id stays
`"head"`); wire drags from the agent's out port; merge targets (the drop
target is still the line's slot rect = the frame; the frame box lights);
node drags (sub-frames drag as before: `isBranchRow`); the in-place own card
and sub-session cards (`renderOpen`); double-click fold of an open card; tour
hooks (`sky-session-box` = the agent tile, `sky-line-bar` = the frame bar,
`sky-stop` now skips sub-frames, `sky-grounded`).

## Tests

- New `src/__tests__/r20Frames.test.ts` (21 tests).
- Rewritten to the new behaviour: skyLine.test (frame classes, agent head,
  `hangFromAgent`, headSub, focus ring), skyChain.test (head classes),
  r18Sky.test (tour hook selector), tours.test (word list: "frame", "agent").
- Full run from a scratch copy: 311 files, 5845 tests passed; `tsc --noEmit`
  clean.

## For the integrator

- **Rebuild the committed bundle** (`npm run build` in `frontend/`) — this
  worker was fenced out of `backend/web/static`.
- Source-text tests outside the fence pinned some strings, so they were kept
  rather than reworded: SkyLine's `aria-label={name + "'s chain"}`, the ⋯
  labels "Open session" / "Fold the session back"
  (skyVisibility/skyResize), and the literal
  `firstLine(row.task_headline || row.task || "")` (skyZoom).
- `endSession.test` forbids any `prompt(` call under src/, hence
  `compose` (not `prompt`) for the composer opener.
- Merging with r20-parallel: `hangFromAgent` reads `branch.from/to` and the
  `branch` edges only; parallel groups don't touch those. If skyGraph's
  `autoPlace` changes how branches lay out under the head, sub-frame sizes
  come in through `big` as before.
- Screenshots (scratch): sky.png (whole sky), c-close.png (one frame up
  close), sky-menu.png, sky-prompt.png, sky-open.png (terminal in the agent
  tile's place), c-flighttab.png (the Flight tab), m-merge.png (a stops-only
  chain dragged onto a frame).
