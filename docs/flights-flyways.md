# Flyways on the shelf: rows of stops, built by dragging

*(To be folded into `docs/flights.md` → "The flyway shelf" and "The shelf: flyways
and stops"; it replaces the chips described there.)*

The owner: "I want to be able to delete steps from a flyway. I feel like the flyway
concept is not really that clear. I think it should be drag and drop."

**A flyway is a saved route of stops** — a reusable sequence (ticket → agent → commit
→ push → PR → reviews → merge) you fly a session along.

## The shelf

`FlywayShelf {plans, onNew, onFly, onEdit(plan, stop?), collapsed?, onCollapse?,
onTack?}` at the foot of the Sky, top to bottom:

1. The head: the fold button (**▾ Flyways N**), one line that says what a flyway is —
   "**Flyways** — saved routes of stops. Drag one onto a line to fly it; drop stops
   into it to build it." — and **+ New flyway**.
2. The flyways, one ROW each (`FlywayRow.tsx`), built-ins first (`rowOrder`,
   `lib/flywayEdit.ts`), then the **empty row**. The rows scroll inside the shelf
   (`.sky-flyway-rows.sky-own-scroll`, so the Sky's wheel leaves them alone); the
   whole shelf is capped at 40vh, so the head and the stops always show.
3. The stops (`StopShelf.tsx`, unchanged but for its label): "**Stops** — drag onto a
   line, or into a flyway".

## A row

| Part | What it does |
|---|---|
| ⠿ + the name | **Click** the name: a text field (Enter or leaving it saves, Esc cancels). **Drag** the head onto a line: flies the flyway there — it carries `FLYWAY_MIME` = the plan id and its name as `text/plain` (`flywayDragData`); the Sky's existing drop handles it. |
| `built-in` | A small tag on a built-in. |
| The stops | `FlywayCanvas` with `compact`, `lod="far"` (the head of each box alone), `vertical={false}`, 128×40 boxes (`rowBox`). All of the canvas's gestures: drop a stop chip in a gap, hover toolbar (✎ ⇤ ⇥ ⧉ 🗑), right-click menu, Delete / ← → on a selected stop, drag a stop along the row, the + between boxes and at the end (the stop palette, under the row). ✎ (or a click on a stop) → `onEdit(plan, index)`: the details card open on that stop. |
| **Fly** | `onFly(plan)`; the host asks which line when none is focused. |
| ⋯ | **Edit details…** (`onEdit(plan)` → the FlywayCard: trigger, schedule, session, description, Describe, each stop's settings), **Duplicate flyway** (a copy of yours named "<name> copy", its name field focused), then **Reset to default** on a built-in or **Delete flyway** on one of yours. |

**Every change saves at once.** The row shows the change before the server answers
(optimistic), saves go one after another (`savePlan`, the whole flyway each time), a
small **✓ Saved** fades in and out after each, and a refusal puts the row back to what
the server last had, with the server's sentence in an error toast (later changes queued
behind it are dropped). The row drops its own copy when the shelf's list catches up.

**Removing a stop** is immediate, with **Undo** in the toast: the same stop back at its
old place, or the end if the row got shorter, under a fresh id if its own was taken
meanwhile (`restoreStep`). The server keeps a flyway at one stop at least, so the last
stop doesn't come off — the toast points at the ⋯ (Delete, or Reset on a built-in).

**Deleting a flyway** hides the row at once, then `DELETE`s it; the toast's **Undo**
saves it again (it gets a new id). No confirm — the desktop app has none.

## The empty row

"Drop stops here to start a flyway". A stop chip dropped on it (or **Pick a stop** →
the palette) creates the flyway: `savePlan` of a manual `blankPlan` with that one stop
(`flywayFromDrop`), named what was typed, else a fresh "New flyway", "New flyway 2"…
(`freshFlywayName`). It then is an ordinary row, and if it was not named yet its name
field takes the focus. **Editor…** opens a fresh FlywayCard (`onNew`) — the way to
describe a whole flyway in a sentence.

**+ New flyway** puts a name field in the empty row (focused); Enter there opens the
palette for the first stop, Esc drops the name. The server refuses a flyway with no
stops, so nothing is saved until the first stop lands.

## Built-ins

Rows like the rest, with the tag. Editing one saves an override (the server keeps it
under the built-in's id); **Reset to default** (`resetPlan`) puts it back, and that is
also what "delete" means for a built-in.

## Renames (`checkRename`)

Trimmed; empty is refused ("A flyway needs a name"); over 120 characters is refused
(the server's cap); another flyway's name (any case) is refused so two rows can't look
the same. A refused name keeps the field open on Enter (the reason under it); leaving
the field puts the old name back and says why in a toast.

## The FlywayCard

Still the details editor (opened from a row's ⋯ or ✎), unchanged except `initialStop`
(the stop to open on). It keeps its own canvas, palette and inspector as a fallback,
and **Save & fly on…**.

## Host changes this needs (outside this change)

- `SkyView.tsx`: `onEdit={(plan, stop) => setCard({ at: middle(), initial: plan,
  stop })}` and `<FlywayCard … initialStop={card.stop} />`, so ✎ on a row's stop
  opens the card on that stop (without it the card opens on the first stop).
- `flightsUi.test.ts` "carries the flyway shelf at its foot": the chip pin
  `class="sky-shelf-chip builtin" data-plan="builtin:ticket"` becomes
  `class="sky-flyway-row builtin" data-plan="builtin:ticket"`.

## Where it lives

`frontend/src/components/flights/FlywayRow.tsx` (+ `.css`), `FlywayShelf.tsx` (+
`.css`), `StopShelf.tsx`, `frontend/src/lib/flywayEdit.ts`; tests in
`__tests__/flywayRow.test.ts`, `skyFlyways.test.ts`, `stopShelf.test.ts`.
