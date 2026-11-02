# Round 18 — graph worker notes (skyGraph + SkyChain)

What the integrator (SkyView) needs to wire the free-floating chains in. Spec:
`docs/flights.md` "Round 18: free-floating chains (the n8n look) — contract".
Every seam name and signature is unchanged; everything below marked *added*
is optional and new.

## `frontend/src/lib/skyGraph.ts`

Coordinates are **chain-local plane px**: (0, 0) is the head's top-left (no
head: the slot's origin). A node's box is `NODE_W × NODE_H` (132 × 118); its
tile (`TILE` = 64) sits centred at the box's top, the label area under it.

| Export | What |
|---|---|
| `chainGraph(d, {head, key, flightId})` | The chain of a `sessionDiagram` / `groundedDiagram` (collapse groups first, as the Sky does today). The diagram's session node becomes the head (`id === HEAD_ID`, `"head"`); parent rows are dropped; main-row stops / groups / the flown pill / rungs become nodes; branch / below rows (workers, hung children) are nodes too, placed under their split. |
| `autoPlace(nodes)` | Line order in; pinned nodes keep their spot; the rest go `PITCH_X` right of the node before (so an un-positioned tail **follows** a moved node); branches `PITCH_Y` below their split, half a pitch right, stacking. |
| `moveNode(g, id, pos)` *added* | The graph with `id` pinned at `pos` (tail follows, wires re-route, bbox follows). SkyChain uses it for the drag preview; the host can use it for an optimistic update. The head never moves (returns `g`). |
| `snapPos(p, free)` *added* | 8 px grid (`GRID_SNAP`), or rounded when Alt is held. |
| `chainBBox(g)` *added* | What `g.bbox` is: every box, the `+`, each loop's run + pill + hook bulge. `bbox.x/y` go **negative** when a node is dragged left of / above the origin. |
| `outPortOf`, `inPortOf`, `portPos(n, side)` *added* | Port spots (the rubber band starts at `portPos`). |
| `wirePath(a, b)` | Cubic out→in, horizontal tangents. |
| `loopWirePathG(a, b, others, lane = 0)` | Out port → hooks down → runs `LOOP_CLEAR` under the lowest of a, b, `others` (whole boxes, labels included) → hooks up into b's in port. `lane` (*added*, optional) drops nested loops `LOOP_LANE` lower; `chainGraph` assigns lanes (wider span = lower). |
| `chainColor(title)` | One of `CHAIN_HUES` (8), stable per title; `var(--muted)` for `""`, null and `"new"`. |
| `nodeAt(g, p)`, `portAt(g, p, slop)` | Chain-local hit tests (topmost box; nearest *live* port). |
| `wireIntent(src, dst, {shift})` | The Wiring table (below). |
| `wireTargets(src, chains, ctx)` *added* | `Map<chainKey, Set<nodeId>>` of every node a drop means something on — pass each chain's set as its `targets`. |
| `canFollow(n, insertFrom)`, `loopable(n)` *added* | The rules `wireIntent` uses: a stop may go right after `n` (its next slot ≥ `insertFrom`); a node a Loop can leave from / return to. |

`ChainGraph` *added fields*: `insertFrom` (the diagram's) and `plus: {after, at}
| null` (the node the trailing `+` hangs off — `"head"` when there are no
stops — and the button's centre). `GraphWire.lane` *added* (loops).

Ports: head = out only. A stop's `inPort` = it is `editable` (can still
change), or it is the first node of a stops-only chain; `outPort` =
`canFollow` (a live flight's current stop has one; flown stops don't).

### `wireIntent` — chains are named by **key**

All intents name chains by `ChainGraph.key` (a session title, or
`"flight:<id>"` for a grounded chain — the same form as `groundedKey`); a
handoff's `target` is `"flight:<B.flightId>"`. Steps are diagram nodes (flight
indexes in `node.index`).

| src | dst | intent |
|---|---|---|
| X out (X can be followed) | Y of the same chain, `Y.index > X.index`, Y an editable **stop** (groups don't move), not already right after X | `move {chain, step: Y, after: X}` |
| X out | Y of the same chain at or before X (X itself too), both `loopable` | `loop {chain, from: X, to: Y}` → `tackOn` Loop via `loopStepTo` at `loopSlotAfter(X)` |
| X out | first node of a stops-only chain B (no trigger), no Shift | `merge {chain: A, after: X, from: B.key}` |
| X out, A has a session | B's first node **or trigger tile**, with Shift or B already triggered | `handoff {chain: A, from: X, target: "flight:<id>"}` |
| session head out | B's first node / socket (or its trigger tile) | `attach {session, chain: B.key}` |
| X out / session head out / `+` | `{sky}` | `palette {chain, after: X \| null, at}` (`null` = after the head: insert at `insertFrom`) |
| X **in** (editable stop/group; not the first of a stops-only chain) | `{sky}` | `split {chain, step: X, at}` |
| anything else | | `none` |

`dst` for the socket = `{chain: B, node: B's first node, side: "in"}`. The
side of `dst` is ignored (a drop on the node counts).

## `frontend/src/components/flights/SkyChain.tsx`

Props as pre-committed, plus *added* optional:
- `kinds?: StepKindDef[]` — family colours follow the server's `family`
  (else StopIcons' built-in map).
- `onPillDown?(via, e)` — a loop pill pressed (before its click): start a
  re-aim drag here if you want `onLoopTo` in the graph. `onLoopPill` still
  fires on click.

Exported helpers: `tileSub(n)` (group "k of n · current", receipt, else sub),
`tileLabel(n, head?)`.

Behaviour the host relies on:
- **Root** (`.sky-chain`, `role=list`, `data-chain`, `data-flight`,
  `data-head`) is sized `bbox.x + bbox.w` × `bbox.y + bbox.h`, overflow
  visible; position it at the chain's origin. Nodes at negative spots overflow
  left/up — use `graph.bbox` for the real extent (Tidy, marquee, fit).
- **Node drag** is internal: tile pointerdown is `stopPropagation`ed only for
  draggable nodes (stops / groups, when `onNodeMove` is set). Head, flown
  pill, branch tiles let the pointerdown bubble (the host can drag the whole
  chain by its head). Preview = `moveNode`; `onNodeMove(node, pos)` fires on
  release only if the spot changed; Esc cancels.
- **Ports**: `<span class="sky-port sky-port-in|out" data-port="in|out"
  data-node="<id>">` call `onPortDown(node, side, e)` on pointerdown (button
  0, propagation stopped). Passive wire ends on fixed tiles get
  `sky-port-off`, no data attributes.
- **`+`**: `<button class="sky-plus" data-port="out" data-node="<after>"
  data-plus>` — a click → `onPlus(afterNode)`; once the pointer moves 4 px it
  calls `onPortDown(afterNode, "out", <the pointermove event>)` (the host's
  drag starts from the move, so a plain click never starts one).
- **Socket** (headless chain): `<span class="sky-socket" data-port="in"
  data-node="<first>" data-socket>` — a drop target only; lit by
  `targets`/`over` on the first node's id.
- **Clicks**: a stop/group → `onSelect`; head, flown pill, branch/child →
  `onOpen`. Double-click / Enter → `onEdit` (stops); Space = click;
  Delete/Backspace → `onRemove` (stops/groups and a focused loop pill);
  right-click → `onMenu(node, {clientX, clientY})`.
- `selected` may be a node id or the head's diagram id (`session:<title>`);
  `bird.nodeId` likewise maps the session node onto the head. The bird is a
  picture here (no hand); add one in a later round if the graph needs it.
- `--sky-z` must be set on an ancestor (the Sky already does) for the loop
  pill to stay screen-sized.
- Imports `FlywayCanvas.css` (bird marker) and `SkyChain.css`; the built
  bundle needs a rebuild (`npm run build`) by whoever owns `backend/web/static`.

## CSS classes (SkyChain.css, prefix `sky-`)

`.sky-chain` · `.sky-wires` (the one SVG) · `.sky-wire` + `-line|-head|-fan|
-join|-loop|-add|-socket`, `.is-done`, `.is-live` (flowing dash into the
current stop), `.sky-loop-pending|running|done` · `.sky-node` +
`.sky-node-<kind>` / `.sky-head .sky-head-session|trigger`, `.st-<state>`,
`.sky-fam-work|ship|outside|people|flow`, `.tone-<chip tone>` (head),
`.is-group`, `.is-perch`, `.is-selected`, `.is-highlighted`, `.is-target`,
`.is-over`, `.is-dragging`, `.is-draggable`, `.is-fixed` · `.sky-tile`,
`.sky-tile-icon`, `.sky-node-title`, `.sky-node-sub`, `.sky-node-extra`,
`.sky-head-extra`, `.sky-head-dot`, `.sky-head-chip` · `.sky-badge` +
`-done|-failed|-halt|-if` · `.sky-risk` + `.sky-risk-<tag>` · `.sky-port` +
`-in|-out|-off` · `.sky-plus` · `.sky-socket` · `.sky-loop-pill` ·
`.sky-chain-bird`. Tokens are local custom properties on `.sky-chain`
(`--sky-tile-bg`, `--sky-wire`, `--sky-fam-*`, …) over the app's theme
variables; `.light` softens the mixes. The head's colour arrives as
`--sky-head-color` (inline); head/line wires get `stroke: head.color` inline at
0.6 opacity.

## `frontend/src/components/flights/StopIcons.tsx` (new)

`TileIcon({kind, size})` — Bird.tsx's `StopIcon` plus `handoff` (↗ out of a
box) and `group` (stacked layers). `kindFamily(kind, kinds?)`,
`nodeFamily(node, kinds?)`, type `StopFamily` (`work | ship | outside | people
| flow`; Loop and Handoff are `flow`). When the backend adds the `handoff`
kind, the palette's `StopIcon` won't know its glyph — use `TileIcon` there
too, or move the glyph into `StopIcon`.

## Not done here (outside the fence)

- Cross-chain handoff wires (dashed, source session's colour) — the host
  draws them between chains; SkyChain only draws a chain's own wires.
- The rubber band during a wire drag — host.
- Rebuilding `backend/web/static`.
