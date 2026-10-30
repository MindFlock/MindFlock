# Editing stops on a line (n8n style)

The owner: "In sky mode I can't drag in new boxes or delete boxes from flows …
boxes should be easier to delete or alter." The stops on a line now edit the
way n8n's nodes do: no ports, no manual wiring, the order is still the
sequence. Every gesture works on a session's line (a line in the Sky, the
pane's Flight tab: `useSessionLine`) and in the flyway editor (`FlywayCard`)
through the same `FlywayCanvas` props. The rules are pure, in
`frontend/src/lib/stopEditing.ts`, and tested in
`frontend/src/__tests__/stopEditing.test.ts`.

## The gestures

| Gesture | What it does | Canvas prop |
|---|---|---|
| Hover a stop (or Tab to it) | A toolbar floats above it: **✎ Edit · ⇤ Earlier · ⇥ Later · ⧉ Duplicate · 🗑 Remove**. It stays a moment after the pointer leaves so you can reach it. | `onSelect`, `onMove`, `onDuplicate`, `onRemove` |
| Right-click any box | A menu at the pointer (`StopMenu`, `role="menu"`, ↑ ↓ Home End, Enter, Esc / Tab / a press elsewhere closes it) | — |
| Delete / Backspace on a selected stop | Removes it | `onRemove` |
| ← / → (↑ / ↓ on a phone) on a selected stop | Moves it one place | `onMove` |
| Drag a stop along the line | Moves it there (never before the flight's current stop) | `onMove` |
| The + between two boxes (shows on hover of the whole line) | The stop palette, to put one in there | `onInsert` |
| The trailing + | Tack on a stop (palette and "Fly a flyway") | `onOpen` |
| Drag a stop chip from the shelf onto the line | The gap it would land in lights up ("+ Tack on here"), or the end + reads "Tack on <Kind>"; dropping tacks the stop on there | `onDropStop`, `dragStop` |

Only a stop that can change (`DiagramNode.editable`: in the editor every
stop; in a live flight the pending stops after the current one) wears the
toolbar, drags, or takes Earlier / Later / Duplicate / Remove. ⇤ / ⇥ are
disabled at the ends and next to a stop that can't change (`canShift`).

**The right-click menu, by box** (`menuItemsFor`):

- a stop that can change: Edit, Earlier, Later, Duplicate, Remove (only the
  ones the host handles);
- the flight's current stop: what its own parts offer (Done on a perch,
  Retry when halted, Skip; the same `stepDone` / `stepRetry` / `stepSkip`
  routes) and a muted line: "The flight is here — it can't change now";
- a stop that has flown (done, skipped, failed): "This stop has flown";
- the trailing +: Tack on a stop; a session, parent, child or branch box:
  Open; a fast-track rung: a note that it moves as the session ships.

In the Sky the line is zoomed with a transform. The toolbar, the menu and
the gap + buttons scale themselves back by the host's `--sky-z` (set on
`.skl`; 1 everywhere else): `scale(calc(1 / var(--sky-z, 1)))`, so they stay
screen-sized at 50% or 200%. With a toolbar to show (or the bird) the stage
keeps 26px of headroom above the top row, so neither is clipped where the
canvas scrolls (the Flight tab, the editor).

## Remove is immediate and undoable

No confirm (the desktop app has no `window.confirm`). On a session's line
the toolbar, the menu, Delete and the inspector's **Remove stop** all go
through one path: `DELETE /api/flights/{id}/steps/{sid}`, then a toast
`Removed “<title>” from the line` with **Undo**. Undo tacks the same stop
(kind, title, config, on_fail) back: `POST /api/flights/{id}/steps {step,
at}` at its old index while that is still ahead of the flight (after its
current stop, not past the end; read from the flight as it is when Undo is
pressed), else at the end (`undoPayload`). In the flyway editor nothing is
saved until Save, so Remove there needs no undo.

## Duplicate

On a session's line: the same stop tacked on right after it (`POST
/api/flights/{id}/steps {step, at: index + 1}`, appended when it is the last
one). In the flyway editor: `duplicateStep` (lib/flights.ts), as the
inspector's Duplicate already did.

## The move route

`POST /api/flights/{id}/steps/{sid}/move` with body `{"at": <index in the
final list>}` answers `{flight}`. 409 `in_progress` when the stop, or the
place it would go, is not strictly after the current stop; 404
`unknown_flight` / `unknown_step`. The client is `flightsApi.moveStop(flightId,
stepId, at)` (through `flightAction`: a refusal opens the error card and the
line stays as it was, since nothing moves until the refetch). The canvas's
`onMove(from, to)` speaks indexes among the line's stops, which on a session
line are the flight's own; `useSessionLine` posts only for a stop that can
change and a `to` ≥ `diagram.insertFrom`.

## The drop contract

A stop chip on the Sky's shelf is dragged with `dataTransfer` type
`STOP_MIME` (`"application/x-mindflock-stop"`, lib/flights.ts), its value the
stop kind id (`"ship"`, `"you"`, …). The canvas takes it only when the host
passes `onDropStop`:

- `dragover` with that type: `preventDefault`, and `dropGapFor(boxes,
  point, insertFrom, vertical)` picks the gap: of the gaps a + can sit in
  (`insertGaps`: before each stop at an index ≥ `insertFrom`, never right
  after the trailing +, never between a stop with branches and its join
  point), the one nearest the pointer along the line, unless the end (the
  trailing +) is nearer, which gives `null`.
- `drop`: `onDropStop(kind, at)` (`at` null = the end); the event stops
  there, so the Sky's own flyway drop never sees it. `dragleave` / `drop`
  clear the highlight.
- During `dragover` a browser hides the dragged value, so the canvas can't
  read the kind's name: the host passes it as `dragStop` (the way it passes
  `dropLabel` for a flyway), and without it the end reads "Tack on a stop".
- `useSessionLine` wires `onDropStop` to its `tackOne(kindOf.get(kind), at)`:
  on a live flight `POST /api/flights/{id}/steps {step, at?}`, on a session
  with no flight an ad-hoc one (`POST /api/flights/start {title, steps,
  absorb_fasttrack: true}`).

## Level of detail (`lod`)

`FlywayCanvas` takes `lod?: "far" | "mid" | "near"` (default `"mid"`) for the
Sky's semantic zoom and puts `fl-lod-<lod>` on `.fl-canvas`. It changes only
what a box draws, never its geometry (sizes still come from `sizedBox` /
`sizeOf`):

- `far`: the head alone (the icon, or a session's state dot, and the title):
  no summary, chip, detail, reason or in-box parts;
- `mid`: as today (what the box's `size` says);
- `near`: the title, the summary, the detail line (`boxDetail(n) ||
  detailOf(n)`), the reason and the in-box parts, whatever the size.

The type sizes per level live with the Sky (`.skl[data-lod=…]`).

## What the hosts pass

- `SkyLine` and `SessionFlight` spread `useSessionLine`'s `canvas`, which now
  carries `onMove`, `onDuplicate` and `onDropStop`: nothing to change there.
  For the end + to name the kind before the drop, the Sky passes the dragged
  chip's name down: `dragStop={…}` on the line's `FlywayCanvas`.
- `FlywayCard` (the flyway editor) already passes `onSelect`, `onMove`,
  `onRemove` and `onInsert`; for Duplicate and drops it adds
  `onDuplicate={(n) => setSteps(duplicateStep(draft.steps, nodeIndex(n)))}`
  and `onDropStop={(kind, at) => { const def = kindOf.get(kind); if (def)
  insert(at ?? draft.steps.length, def); }}`.
