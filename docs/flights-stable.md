# The still sky

> Worker notes (round 10, `sky-stable`), to fold into docs/flights.md "The
> Sky" and "Room and the semantic zoom" at merge. Source:
> `lib/skyLayout.ts` (`assignSpots`, `NEW_SESSION_MIME`),
> `components/flights/SkyView.tsx`, `Sky.css`, `NewSessionBlock.tsx` +
> `.css`; tests in `skyStable.test.ts`.

The owner: "I DON'T like how the boxes move around. I also don't like that I
can't drag in a new session block." **Nothing on the sky moves unless the
owner moves it.**

## Where a line sits

- **A line gets a spot once.** When it first appears, after it has been drawn
  and measured (and the window, the sessions and the flights have loaded),
  `assignSpots(existing, newcomers, area)` gives it the first free place by
  the old rule (rows from the intake lane rightwards, newest first, wrapping
  at the sky's width), clear of every line already placed at that line's size
  now. That spot is kept in SkyView's `autoPos` and passed to the placement as
  saved, merged with the pins (`{ ...autoPos, ...pinnedPositions(layout) }`).
- **It keeps that spot** while it grows or shrinks: a flight loading, a perch
  opening wide, the hover toolbar, its box opening into a pane card. A
  neighbour arriving or leaving doesn't move it either.
- **Two lines may grow into each other.** The sky doesn't shove them apart.
  The focused or open line is lifted over the other (z-index), and the owner
  drags one away or presses Tidy.
- **A line that leaves frees its spot**: the session is closed, or it becomes
  a hidden helper. A line hidden by a filter (Find, Mine vs All) keeps its
  spot, and a newcomer keeps clear of it at its last measured size. A dragged
  line is pinned where it was dropped. Its auto spot is dropped, so "Tidy this
  line" places it afresh.
- **Tidy** (the button, `T`) forgets every auto spot and every pin. Every line
  is placed again, in rows. It is **the only glide**: SkyView sets
  `data-tidying` on `.sky-plane` for 350 ms (not under reduced motion), and
  only `.sky-plane[data-tidying] .sky-line-slot` has a `transform` transition.
  No other rule eases a line's or a slot's transform. A dragged line follows
  the pointer live.
- This session (one line) still sits at the plane's corner. It neither reads
  nor changes the auto spots.

## The camera fits once

The automatic fit (`firstFit`, or the focused line when the grid was on "1")
runs once, on the sky's first show, after the lines have their spots (drawn
and measured, or 0.9 s after the sessions load, whichever comes first). It
runs again only when the view changes (All / Mine / Perched / This session,
or This session's line). The effect's dependencies have no line sizes in
them. A line growing, a new session arriving, or the window resizing never
refits. A newcomer off screen is brought in by the rail's selection and the
flash, as before. `Fit`, `0`, `1` and the zoom buttons work on demand. Back
from the grid in the same view, the camera is where it was left.

## Dragging a new-session block in

- The toolbar's **+ New session** is draggable (`dataTransfer`
  `application/x-mindflock-new-session` = `"session"`, effect *copy*; tooltip
  "Click, or drag onto the sky, …"). Over empty sky the viewport washes
  softly (`.sky-viewport.is-new-over`) and takes the drop. Over a line it
  refuses. Dropped, the block opens at the drop point (plane coordinates
  through `toPlane`), as a double-click there would. A click, a double-click
  and Ctrl/Alt+N work as before.
- **The block is a block on the sky**: a card with a line's bar (the bird,
  "New session", × to cancel) and a dashed accent edge until it flies. In
  light and dark. **Drag it by its bar** (`onMove(pos)` in plane px; the sky's
  `zoom` turns pointer px into plane px). The session it makes is pinned
  where the block sat. Its fields are unchanged: repository chips and the
  path field, the task, the flyway pick, the agent.
