# A line in the Sky (SkyLine)

> To fold into `docs/flights.md` ("The Sky"). Source:
> `frontend/src/components/flights/SkyLine.tsx` (+ `SkyLine.css`), the
> `size` / `openBox` / `dropLabel` / `detailOf` props of `FlywayCanvas`, and
> `Pane`'s `embedded` prop. Tests: `frontend/src/__tests__/skyLine.test.ts`.

The owner's ask: "make boxes smaller and bigger to reveal more text … click
on a box and see a claude code session there working or a terminal … an
isomorphism really of the normal flow." A line is one session drawn as its
diagram, and any session box on it opens into the very pane the grid shows.

## Anatomy

```
┌ 🐦 me  [Working]  📌  ◧  ⋯ ───────────────────────────────┐  ← title bar (drag = move the line)
│  [session] → [stop] → [stop] → … → ( + Tack on a stop )    │  ← FlywayCanvas, live mode, the bird
│     └─ [worker]                                            │
└────────────────────────────────────────────────────────────┘
```

- **Title bar** — the bird, the session's name, its state chip
  (`deriveSessionState`), 📌 when the line is pinned (`layout.pinned`: the
  host's auto-layout leaves it where it was put), **◧** the size control, and
  **⋯**: Open session (`selectSession`; the host switches to the grid), Show
  only this line (`openDialogFor("flights", "sky/<title>")`), Fly a flyway…
  (opens the +'s palette), Close session. A press on the bar (not on a
  button) calls the host's `onDragStart`; the host moves the line.
- **The diagram** — `sessionDiagram(rows, title)` with the family (parent
  above, workers below), the bird from `birdPerch`, and everything
  `useSessionLine` gives a line: the + palette, the + between pending stops,
  the stop inspector, a live flight's ⋯ and perch parts in its boxes. The
  palette/inspector hang under the line (they don't count toward its size).
- **Focus** — a press anywhere on the line calls `onFocus`; `focused` draws
  the ring.
- **A flyway chip dragged over** (`dropFlyway`) — the + lights up and reads
  "Drop to fly <name>" (the host handles the drop).

## Box sizes (◧)

◧ cycles **compact → normal → wide** through `onLayout({size})`; the host
remembers it per line. `FlywayCanvas`'s `size` prop does the drawing (default
normal, so the Flyways editor and the pane's Flight tab are unchanged):

| size    | box         | shows |
|---------|-------------|-------|
| compact | 110 × 62 px | the title alone, one line (a session/worker box: a tone dot for its state); the + is a bare circle |
| normal  | 178 × 76 px | today's box: icon, title, state chip, the config summary, a halted reason |
| wide    | 260 px, taller | the full title (two lines), the summary (two lines) and a **detail line**: the agent stop's prompt (first line), a perch's text (or its items), a ship stop's commit message, a command, a ping's text; on a session box, what it was last asked |

The layout grows a wide box only by what it shows, so connectors stay
straight. Detail comes from `boxDetail(node)` (the stop's own config) and the
canvas's `detailOf` (the line passes a session's `last_prompt`).

## The open card = the pane

Pressing the **session box** toggles `onLayout({open})`. Open, the box grows
into a card of `layout.paneW × paneH` plane px (default **720 × 460**, floor
**360 × 240**) under a slim head (the name, **▣** to fold it back into a
box). Inside is the grid's own `Pane` for that title with `embedded`:

- dropped: the grip, the drag wiring, hide −; a press focuses the session
  (`setFocused`) instead of `selectSession`, so the grid behind the sky is
  not rearranged;
- kept: the header with its tab strip (Agent · Terminal · Diff · Queue · Map ·
  Flight · Thread) and every tab body. The tab is the same per-title store key
  (`lastTab[title]`) the grid uses, so switching views keeps it; the
  terminals are the registry's (`getTerm`) — typing works as in the grid.
- one pane per title holds the terminals: while a card is open, the grid's
  pane for the same session leaves them alone (`holdTerms` in Pane.tsx — its
  ref re-runs every render and would otherwise pull them back), and adopts
  them again the moment the card closes. The card's Pane is keyed by the
  title.

The card's corner (**⤡**) resizes it: pointer travel ÷ `zoom` = plane px,
drawn live, persisted once on release (`onLayout({paneW, paneH})`). The card
is a node of variable size in `FlywayCanvas` (`openBox`): it starts at the
size it asks for and is then measured (ResizeObserver, layout px), so the
connectors and the boxes after it follow the card. Wheel inside the card
scrolls the pane (`sky-own-scroll`), not the sky; clicks inside it never fold
it (only the box itself, Enter on it, or ▣ do).

## Workers

A **worker's box** opens the same way. The host keeps one layout per line,
so a worker's open state (and its card size) lives in the line itself, keyed
by the worker's title — not persisted; a worker that leaves the family takes
its card with it. The host sees it as the line growing (`onMeasure`).

## What the host provides

`SkyLine({title, rows, plans, layout, onLayout, focused, onFocus, zoom,
onDragStart?, onMeasure?, dropFlyway?})`. The line renders in flow
(`position: relative; width: max-content`), so the host wraps it in its
absolutely positioned box at `layout.x/y`. `onMeasure(w, h)` reports the
line's size in plane px (offset sizes — the zoom is a transform) whenever it
changes: a size change, a card opening, closing or being resized.

## Known limits

- xterm under the sky's CSS `scale()`: typing and scrolling work; a mouse
  selection in the terminal is off by the zoom factor away from 100%.
- An open card's terminal is fitted to the card (tmux resizes); back in the
  grid it refits to the grid's pane.
