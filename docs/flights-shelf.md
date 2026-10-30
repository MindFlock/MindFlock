# The shelf's two rows: flyways and stops

*(To be folded into `docs/flights.md` → "The flyway shelf".)*

The shelf at the foot of the Sky (`FlywayShelf`) has two rows, their labels in one
column:

1. **Flyways**: the saved flyways as chips (unchanged; see "The flyway shelf").
2. **Stops** (`StopShelf.tsx`, rendered by `FlywayShelf`): one chip per stop kind the
   server lists (`useFlightKinds()` → `GET /api/flights/kinds`). Each chip is the kind's
   `StopIcon` plus its name (`kindName`, so a `you` stop reads **Perch**), and its tooltip
   is the kind's `summary`. The flock's stops come first in registry order, then a thin
   divider, then the perches in the gold perch tone (`stopChipOrder(kinds)`). Nothing is
   hardcoded: Split and Join show up only when the server lists them. With zero kinds the
   row says, muted, that the stop list didn't load.

Folding the shelf's head folds both rows. Each row scrolls sideways instead of wrapping,
and the chips are 26px, so the shelf stays about 80px tall.

## The drag contract

A stop chip is `draggable`. On dragstart it sets (`stopDragData(def)`):

| type | value |
|---|---|
| `application/x-mindflock-stop` (`STOP_MIME`, `lib/flights.ts`) | the kind id, e.g. `ship` |
| `text/plain` | the name, e.g. `Ship` / `Perch` |

and `effectAllowed = "copy"`. The drag image is the chip. The sky owns the drop: the gap
the stop lands in on a line lights up and the stop is tacked on there (at the end when it
lands on the line but not in a gap).

## `onTack`

`FlywayShelf {…, onTack?(kind)}` passes it to `StopShelf`. A chip that is clicked (or
tapped on a phone, where nothing drags) calls `onTack(kind)`, and the host tacks that stop
onto the focused line. Without `onTack` a chip is drag-only: it is a plain span (not a
button), its cursor is grab and its tooltip says "Drag onto a line". On a phone
(`@media (hover: none)`) the "drag onto a line" hint under the row's label is hidden.
