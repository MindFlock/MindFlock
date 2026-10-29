# The Sky: birds, the flyway shelf, flyways made on the sky

Round 7 of the Sky (to fold into `docs/flights.md`). Owner's ask: the
MindFlock birds flying instead of a drawn one, no bird on the pane's Flight
tab, and flyways made from the Sky itself.

## The birds

**One bird everywhere: the MindFlock raven.** `components/flights/Bird.tsx`
draws `backend/web/static/bird.png` — the sprite the app's flock
(`lib/flock.ts`) and the website's hero fly — instead of the old hand-drawn
gull. The sprite is a single 216×160 frame with white stripped to alpha, so it
is used as a **CSS mask over `currentColor`**: the bird takes the colour of
wherever it sits, exactly as the old stroke glyph did. No new assets, no
canvas per glyph.

- `Bird {size?, className?}` — the still glyph (~16px) for the top bar, the
  pane strip, the Sky's line bars, the Flyways editor. Still an
  `<svg class="fl-bird">` element (callers and their CSS are unchanged); the
  mask is its inline style.
- `BirdMarker {tone, x, y, label, size?}` — the bird standing on a line's
  active box (`FlywayCanvas`'s `bird`). ~24px, drawn as two masked bands of the
  sprite split at the wing hinge (56%, flock.ts's `HINGE`): the body below,
  the wings above. **While the flock works the wings beat** — the wing band
  scales about its bottom edge (the hinge), full spread down to a quarter,
  about once a second, the same beat flock.ts paints. Perched on you it sits
  still and gold, halted red, paused grey (the tints are FlywayCanvas.css's
  `.fl-bird-marker.tone-*`). The hop to the next box is still the
  `left`/`top` transition. Reduced motion: no flapping (no `fl-flapping`
  class, and the stylesheet turns the animation off as well).
- Pure helpers, pinned by `__tests__/skyBirds.test.ts`: `spriteBox(width)`
  (height, wing band, body band), `spriteMask(size, position)`,
  `birdLook(tone, reduced)` (the marker class, the sprite class, flaps or not),
  `prefersReducedMotion()`.

**The ambient flock** — `SkyFlock {width, height, zoom, paused?, density?}`
(`components/flights/SkyFlock.tsx`): a `<canvas>` the Sky mounts behind its
plane with a few birds crossing the empty sky — ~8, times `density` (capped
at 40). Same sprite (flock.ts's `tinted()`), same 12-species plumage (the dark
column, the light one in the light theme), same wing beat and belly-up mirror
as the app's flock. They flock casually, each heads for a wandering target,
they **ease out of the middle of the sky** where the lines are, and they wrap
at the edges so they cross the sky rather than turn back. Bird size follows
the Sky's zoom a little (√zoom, 0.7–1.3).

Cheap by construction: one rAF, at most 60 frames a second (a 120 Hz frame is
skipped), no work while the tab is hidden, `paused` asks for no frame at all
(the last one stays painted), DPR-aware (capped at 2). Under
`prefers-reduced-motion` it renders **nothing**. It is decoration:
`pointer-events: none`, 0.55 opacity (0.32 in the light theme). The simulation
(`flockSize`, `seedBirds`, `fitBirds`, `stepBirds`) and the frame loop
(`skyLoop`) are pure and tested without a DOM.

**The pane's Flight tab** (`components/grid/TabIcon.tsx`) is a route now —
three stops joined by arrows, like the connectors — not a bird.

## The flyway shelf

`FlywayShelf {plans, onNew, onFly, onEdit, collapsed?, onCollapse?}`: the saved
flyways as a strip of chips along the bottom of the Sky, built-ins first. A
chip shows the flyway's name and a tiny row of stop dots (a perch's dot gold;
past ten, "+N").

- **Click** the chip → `onEdit(plan)` (the host opens a card on it).
- **Fly** → `onFly(plan)` (the host asks which line).
- **Drag** the chip onto a line: it carries the flyway's id as
  `application/x-mindflock-flyway` (and its name as `text/plain`); the host
  owns the drop targets. `FLYWAY_MIME` and `flywayDragData(plan)` are exported.
- **+ New flyway** → `onNew()`.
- The head folds the strip away; pass `collapsed` / `onCollapse` to remember
  it (without them the shelf keeps it itself).

## The flyway card

`FlywayCard {x, y, plans, initial?, onSaved, onCancel}`: a flyway made on the
sky, floating at (x, y). The Flyways tab's editor cut to what a card holds:

- the **name** (a fresh "New flyway", "New flyway 2"… that the shelf doesn't
  have yet, unless `initial` names it);
- the stops on `FlywayCanvas` in editor mode — drag to reorder, ← → / Delete,
  `+` between boxes or at the end for the stop palette — with the selected
  stop in the shared `StopInspector` (Earlier / Later / Remove) and
  **Describe a stop…** under the canvas (all from `StopParts.tsx`);
- **Save & fly on…** — saves, then `onSaved(plan)`: the host offers the line
  picker and flies it (`startFlightOn(title, planId)` in `lib/flightsApi.ts`);
- **Save** — `POST /api/flights/plans` (`savePlan`; an `id` in `initial`, or
  from a first save, makes it an update) and keeps the card open on the saved
  flyway;
- **Cancel** — with unsaved stops it asks inline (Discard / Keep editing; the
  desktop app has no `confirm()`); Escape does the same.

A refusal shows the server's sentence and selects the stop its `field` names.
The trigger and the session defaults stay in the Flyways tab: a card makes a
manual flyway (or keeps the trigger it was given). `saveCard(draft, andFly,
onSaved)` is the card's save, exported for the tests.

## Save as flyway

A live line's ⋯ → **Save as flyway** opens a card seeded with
`planFromSteps(name, steps, trigger?)` (`lib/flights.ts`): a flyway body
(no `id` / `updated`) with only what each stop **is** — kind, title, config,
on_fail — re-numbered `s1..sN`, a deep copy of each config with the driver's
`_`-prefixed keys dropped, and a manual trigger unless one is passed. Runtime
state never rides along (`step_states`, `detail.performing`, the cursor live
beside the steps, not in them).
