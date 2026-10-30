# The Sky: room and semantic zoom

> To fold into `docs/flights.md` ("The Sky") and `docs/web-ui.md` ("Two views:
> the grid and the sky"). Source: `lib/skyLayout.ts` (the rules),
> `components/flights/SkyView.tsx` (the camera, the toolbar, the drawer),
> `SkyLine.tsx` + `SkyLine.css` (the level of detail),
> `Sky.css`, `IntakeLane.css`; tests in `skyZoom.test.ts`.

## Room on the screen

- **The drawer starts folded** to its vertical tab, which carries the count
  of what is perched on you and wears the attention tone while that count is
  above zero. A press opens it, and the choice is remembered per device
  (`mf_sky_drawer`: `"1"` open, `"0"` folded; `skyDrawerOpen(pref)`). A first
  visit no longer loses 360px to it.
- **The toolbar is one slim row** (36px at most): the views, Find, + New
  session, Tidy, the zoom group. Find gives up width first. When the main
  area is under 860px wide (a 1000px window beside the rail), the 100%
  button and the tour's ? go (the readout still shows the zoom) and the
  chips tighten. Under 640px the row wraps.
- **Spread out**: `SKY_GAP` 64 (was 36) between lines; the intake lane
  `INTAKE_W` 240 (was 300) with a 48px gap before the first line (`LINES_LEFT
  = SKY_MARGIN + INTAKE_W + 48`); a fit keeps `FIT_PAD` 56 (was 40) round
  what it shows. `placeLines` still flows rows newest first; **Tidy** (T)
  forgets the pins and re-places at the new gap.
- **Lines sit where they are placed**: each line's slot is translated to its
  `placeLines` spot (or where it was dropped). Before this, the slots sat in
  plain flow, one under the other, so the gap, Tidy and dragging never
  showed. Lines glide to a new spot. The one being dragged follows the
  pointer, and an open box lifts its line over its neighbours.

## Semantic zoom

**The rule: the geometry never depends on the zoom.** A box keeps the
layout's plane width and height, a line its measured size, the title bar its
34px. If they changed with the camera, the fit (which reads measured sizes)
would chase itself. What changes is what is drawn inside the same geometry
and the size of the type on screen.

`lodFor(z): Lod` (`"far" | "mid" | "near"`):

| Band | Zoom | A box shows | The type |
|---|---|---|---|
| **far** | below `LOD_MID` = 0.62 | the icon and the title (two lines, bold, ellipsis); no summary, chip, detail or in-box parts | counter-scaled: `--sky-far = clamp(1, 0.84 / z, 2.1)`, about 10.5px on screen down to the 40% floor; icons, glyphs and the bar's bird and ⋯ by `--sky-far-ui = clamp(1, 0.6 / z, 1.4)`; the ◧ and 📌 hidden (too small to aim at) |
| **mid** | 0.62 up to 1.15 | today's box | titles and the line's name only, by `--sky-mid = clamp(1, 0.8 / z, 1.3)`, so they read at about 10px from 62% up. That is the first picture's floor. From 80% they are at their normal size. |
| **near** | from `LOD_NEAR` = 1.15 | the title on two lines, the summary, the detail and the reason wrapping to `--sky-lines` lines each (`nearLines(z)` = ⌊2z⌋, from 2 to 8), the perch's checklist and buttons, and the session's task as a second row of the line's bar | counter-scaled back to about 12.5px on screen: `--sky-near = clamp(1, z, 3)`, text at `12.5px / --sky-near`, and padding, icons, chips and perch items `zoom: 1 / --sky-near`. A box at 200% holds about four times the words (twice per line, twice the lines). |

- `SkyLine` puts `data-lod`, `--sky-z` and `--sky-lines` on `.skl` and passes
  `lod` to `FlywayCanvas`. The canvas drops parts at far and adds them at
  near. Until the canvas's `lod` prop lands, it goes through a cast spread.
  The CSS hides the far-only parts too, so the bands hold either way.
- An open box (a card holding the session's pane) is never restyled.
- The intake lane has the same far band (`.sky-intake[data-lod]`): its
  sources' names and counts, counter-scaled; its item rows wait for mid. It
  keeps its 240px width.
- **The ◧ size control** (compact / normal / wide) stays a manual override
  of the geometry. The level of detail is about the camera. Both compose: a
  wide line seen near shows the most.
- `ZOOM_MAX` is 3 (was 2), since near has more to show; `ZOOM_MIN` stays
  0.4. The wheel, the pinch, + / − and the readout follow.

## The first picture

`firstFit(bounds, vw, vh, maxZ = 1, pad = FIT_PAD)`: if fitting everything
(`fitCamera`) is legible (zoom ≥ `LOD_MID`), it does that, centred. If not,
it fits the drawn content's width at no less than 0.62, anchored at the top
left: the intake lane and the newest lines are on screen, and the owner pans
or scrolls down to the rest. It drives the automatic fit (a first visit, a
new view, the lines changing shape until the owner moves the camera) and the
`0` key.

The toolbar's **Fit** is the true fit (`fitCamera`): everything in the
window, however small. Its tooltip says so, and says that `0` is the legible
fit.
