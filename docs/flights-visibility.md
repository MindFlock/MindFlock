## The Sky, readable

> Sky round 10, the readability sweep. To be folded into `docs/flights.md`
> under "The Sky" and "Room and the semantic zoom". Code:
> `frontend/src/lib/flightDiagram.ts` (`boxMetrics`, `sizedBoxFor`,
> `collapseFlown`, `drawnIndexes`, `lineWhere`), `components/flights/SkyLine.tsx`
> (+ `SkyLine.css`, `SkyVisibility.css`), `FlywayCanvas.css`, `IntakeLane.css`,
> `Bird.tsx`. Tests: `frontend/src/__tests__/skyVisibility.test.ts`.

The owner: "Everything is still just too hard to read." At the first fit (62%)
a stop's title was ~10px on screen and its summary ~7px; a 9-stop line was
~2000 plane px wide, so the sky zoomed out to fit it. Two levers: bigger boxes
and type in the sky, and shorter lines.

### Sizes

The sky asks the canvas for a **roomy** tier (`LayoutOpts.roomy`,
`boxMetrics(compact, roomy)`). The pane's Flight tab and the flyway editor keep
the base tier. The geometry still never depends on the zoom.

| | base | roomy (the sky) |
|---|---|---|
| box | 178 × 76 | **236 × 100** |
| + (add) | 148 × 40 | **180 × 48** |
| branch | 156 × 52 | **200 × 64** |
| flown pill | 120 × 44 | **148 × 56** |
| gap / vgap / anchor | 46 / 30 / 38 | **56 / 36 / 50** |
| chip / branch sub / dots add | 18 / 16 / 14 | 22 / 20 / 16 |
| ◧ compact | 110 × 62 | 150 × 64 |
| ◧ wide | 260 wide | 330 wide |

`sizedBoxFor(size, node, base, detail, roomy)` is the ◧ sizing that
`FlywayCanvas`'s `sizedBox` used to hard-code. At the base tier it gives the
same numbers.

| Type in the sky | before | now |
|---|---|---|
| box title | 12.5px | **15px / 600** (a stop's title may take two lines) |
| summary, detail, halted reason | 11.5px | **13px** |
| chips | 10.5px | **12px** |
| icon tile | 22px | **26px** (17px glyph) |
| line bar | 34px, name 12.5px | **42px, name 15px, chip 12.5px** |
| where the flight is (new, in the bar) | — | **13px**, muted ink |
| ⋯ menu items | 12px, ~24px rows | **13.5px, 32px rows**, screen-sized at any zoom |
| bird marker | 24px | ×1.35 |
| intake lane cards | 11.5–12px | **13px**, the ticket id bold, Fly a 28px button, source heads 13px caps |
| drawer perch card | 12–13px | **14px** body, 15px name, **36px** Done / Skip / Retry; "Waiting to fly" alike |
| controls (ChipRow, Seg, Toggle, field labels) | 12–12.5px | **13px**, 28–30px tall (the toolbar's chips too) |

**The zoom bands, re-tuned for the 15px base** (`SkyLine.css`):

- **far** (< 62%): `--sky-far = clamp(1, 0.76 / z, 1.9)`. A title reads
  15 × 0.76 = **11.4px** on screen from 62% down to the 40% floor. The bar's
  where-line, ◧ and 📌 wait for mid.
- **mid**: `--sky-mid = clamp(1, 0.8 / z, 1.3)` now grows the titles **and**
  the summaries, chips, the join pill and the bar's where-line. At 62% a title
  is ~12px on screen and a summary ~10.4px (they were ~10px and ~7px).
- **near** (≥ 115%): counter-scaled back to 15px titles and 13px summaries on
  screen. Titles wrap to three lines, summaries to `--sky-lines`.

### What has flown: one pill

`sessionDiagram` folds the stops a live flight has already flown into **one
node** (`collapseFlown`):

- **The rule.** Take the leading run of `done` / `skipped` stops, up to the
  first stop that is neither (the current one). If the run has
  **`FLOWN_MIN` = 2** or more stops, it becomes `{ id: "flown", kind: "flown",
  row: "main", title: "<N> flown", sub: "<first> … <last>", state: "done",
  count: N, flown: [titles] }`. A single flown stop stays as it is. A `failed`
  stop breaks the run, so it stays in sight.
- **Edges and branches.** The line's edges chain through the pill. A column of
  branches that fanned out of a folded stop fans out of the pill instead, and
  joins at the box after it. The direct pill → next edge is dropped, so the line
  goes through the branches.
- **Numbering.** `insertFrom` and every stop's `index` stay the flight's own.
- **Expanding.** `SessionDiagramOpts.expandFlown` turns the fold off. The line
  keeps that as its own `useState` (not persisted). A press on the pill
  (`onOpen` with `kind === "flown"`) or the ⋯'s **Show every flown stop** /
  **Fold the flown stops** toggles it.
- **Layout.** `layoutDiagram` sizes the pill from `boxMetrics().flown` and
  never calls `sizeOf` for it. It sits centred on the line's anchor, the way
  the + does. The canvas styles `.fl-box-flown` as a green-edged pill with a
  double tick (`StopIcon` `flown`).
- **The Flight tab.** `useSessionLine` passes no option, so it folds too.
- **The canvas counts DRAWN stops.** A move's from / to, an insert's gap and
  `insertFrom` comparisons all count the stops on screen. With a pill folding N
  stops, those numbers are N lower than the flight's. `drawnIndexes(d)` lowers
  every stop's `index` and `insertFrom` by N for the canvas. `flownCanvas(d,
  canvas)` (SkyLine.tsx) adds N back on the way out (`onMove`, `onInsert`,
  `onDropStop`) and hands back the original nodes (`onSelect`, `onRemove`,
  `onDuplicate`, `renderInBox`, `sizeOf`). Without that, a drag on a folded line
  would move the wrong stop.

### The line's bar and its ⋯

The bar shows the bird, the name, the state chip, and **where the flight is,
in words**. This comes from `lineWhere(diagram)`, which follows `birdPerch`:

- "at Reviews settle · 10 min quiet"
- "waiting on you at Perch: approve the merge · 2 items to tick"
- "halted at Push · <reason>"
- "at Checks · waiting for 2 of 3"
- "fast-track at Push"

Nothing shows while the bird sits on the session box, because the chip says
that already. After that come 📌, then **◧**: the glyph, a three-bar meter
lit to the current size (compact 1, normal 2, wide 3), and the size in words
on hover or focus.

**⋯ menu groups** (a rule above each group's first item; items 13.5px in 32px
rows; the list scales itself back to screen size; the line whose menu is open
is lifted over its neighbours):

1. **Open session** (opens the card on the line; **Fold the session back**
   when it is open) · **Open in the grid** (`selectSession` + `setView("grid")`)
   · **Show only this line**
2. **Compact boxes / Normal boxes / Wide boxes** (✓ on the current one) ·
   **Show every flown stop** (only when something is folded) · **Pin here**
   (pins at the line's current spot), or **Tidy this line** when it is already
   pinned (Unpin and Tidy are the same act, so there is one item)
3. **Fly a flyway…** · **Tack on a stop**
4. **Close session** (danger)

### Contrast

**States** (`FlywayCanvas.css`, every home):

- **current**: a 2px working outline and the pulse.
- **perched**: a 2px gold outline and a 14% gold wash.
- **halted / failed**: a 2px red outline. The reason uses a red ink.
- **done / skipped**: no element opacity. The title is `text` at 78% and the
  summary is the muted ink at 85%, on the panel.
- **pending**: a dashed outline, with the summary in the muted ink.

Wires are 2px at 88% (fan / join 75%), with solid arrowheads. The selected
ring is 3px.

**Text inks** (`--fl-ink-working`, `--fl-ink-red`, `--fl-ink-muted`;
`--sky-ink-gold` for the drawer's head). Light mode overrides them; text on
solid gold is `--tone-attention-ink`.

| text on its background | dark | light |
|---|---|---|
| title on a box (`--text` / `--panel-2`) | 11.35 | 13.96 |
| summary, muted ink on a box | 6.53 | 6.24 |
| done title (text 78%) on the panel | 8.02 | 7.91 |
| done summary (muted ink 85%) on the panel | 5.52 | 4.96 |
| pending summary on the panel | 7.15 | 7.24 |
| perched title on the gold wash | 8.07 | 13.04 |
| perched summary on the gold wash | 4.64 | 5.83 |
| halted reason (red ink) on a box | 5.53 | 5.15 (raw `--red` was 3.82) |
| "Working" chip (working ink) on a box | 6.71 | 4.63 (raw green was 2.95) |
| chip ink on solid gold | 12.41 | 9.34 |
| bar's where line (muted ink) on the panel | 7.15 | 7.24 |
| drawer's "Perched on you" head | 12.39 (gold on the panel) | 6.37 (#7a5a10; raw gold was 1.86) |

All body text is ≥ 4.5:1 in both themes. The numbers are WCAG relative
luminance from the tokens in `styles/tokens.css`, with `color-mix` resolved
(script: the round's scratch `contrast.py`).

### What the merge has to wire (outside this change's files)

- `FlywayCanvas.tsx` needs a `roomy?: boolean` prop. Pass it into
  `layoutDiagram`'s opts (`{ compact, vertical, sizeOf, roomy: !!p.roomy }`,
  and add it to the memo's deps), and make `sizedBox(size, n, base, detail,
  roomy)` return `sizedBoxFor(size, n, base, detail, roomy)`. SkyLine already
  passes `roomy` through a cast spread. Until then the sky draws 15px type in
  base-size boxes.
- `SessionFlight.tsx` should use the same `flownCanvas(diagram, line.canvas)`
  wrapper and its own `expandFlown` toggle on a `flown` press, as SkyLine
  does. Until then the Flight tab folds the pill, but drags and the ⇤ / ⇥
  moves there count drawn stops while the flight counts its own.
- `FlightStrip` (the Board's strip) also gets the pill from
  `sessionDiagram(…, family: false)`. A press on it does nothing there.

### Screenshots

Before (the round-9 bundle) and after (this change with the FlywayCanvas
wiring above applied), 1400 × 900, mocked API, in the round's scratchpad
`shots/`: `before-{fit,far,near,menu,light,drawer,lightdrawer,tab}.png` and
`a2-{fit,far,near,menu,light,drawer,lightdrawer,tab}.png`.
