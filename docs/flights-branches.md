# Fan-out and join: branches on a session's line

> Sky round 8. To be folded into `docs/flights.md` under "The session
> diagram" (and its Frontend notes). Code: `frontend/src/lib/flightDiagram.ts`
> (`sessionDiagram`, `birdPerch`, `layoutDiagram`),
> `components/flights/FlywayCanvas.tsx/.css`, `SkyLine.tsx`,
> `useSessionLine.tsx`; tests: `__tests__/skyBranches.test.ts`.

When an agent spawns several workers partway through its flight, its line
splits: the workers run side by side as **branches** between the stop that
spawned them and the next box, and the line comes back together at that next
box, the **join point**. The line moves on only when every worker is done.

```
   [session] → [Fan out] ─┬─ [w1 · Working]   ─┐
                          ├─ [w2 · Asking]    ─┤  2 of 5
                          ├─ [w3 · Done]      ─┼─▶ [Push] → [+ Tack on a stop]
                          ├─ [w4 · Done]      ─┤
                          └─ [w5 · Done]      ─┘
      ├─ [child]   (a worker with no branch: below, as before)
```

## What the frontend reads (the dto)

| Where | Field | Meaning |
|---|---|---|
| the parent's row | `flight.join: {waiting, total} \| null` | set on the **current** step while it waits on outstanding children: an `agent` step with `join` true, or a `join` step |
| a child's row | `flight.branch: {parent, step}` | the parent's title and the id of the parent's step it was spawned during (the server falls back to the parent's current step). A child may also have its own flight or fast-track |
| the parent's flight (full, cached) | `step_states[sid].detail.children: [{title, state, report?}]` | on a join step or an agent step that waits; `state` is `working \| asking \| done \| failed \| gone` |
| a child's row | `activity`, `last_report` | its live state |

All of these are optional. An older server sends none of them, and the line
draws as it did before.

## How branches are derived (`sessionDiagram`)

- A child becomes a branch when (a) the parent has a live flight, (b) the
  child's `flight.branch.step` names a stop of **that** flight, and (c)
  `flight.branch.parent` is this session (or is empty). With the full flight
  cached, `step` is matched against the flight's step ids. Drawn from the row
  alone, it matches only the current stop (the row's `flight.step.id`).
- Any other child (no `branch`, a step from an earlier flight, re-parented)
  stays **below** the session box, as before. A child drawn as a branch is
  never also drawn below.
- A branch node is `kind: "branch"`, `row: "branch"`, `id: "branch:<title>"`,
  with `session` = the child's title and `branch: {from, to, state, inner}`:
  `from` is the spawning stop's node, `to` is the next box on the line.
- **State**: the child's live activity comes first (working; a question or a
  perch is `asking`). Next is its `last_report` (`done`, `failed`, and
  `blocked` reads as `asking`), then its entry in the join detail. A child
  with none of these counts as `working`, because the join still waits on it.
  The chip shows Working / Asking / Done / Failed. The node `state` maps to
  current / perched / done / failed, so the canvas's live styles apply.
- **Inner summary** (`innerSummary`): when the child has a flight of its own,
  `k/n stops · <current stop>`. With a fast-track ladder instead, it counts the
  ladder's rungs the same way. The box's sub-line is the inner summary, else
  the child's report.
- **Gone** children leave: a closed child has no row, and a child the join
  detail lists as `gone` is skipped. Nothing keeps their slot, so the host
  animates the change.
- Children that spin up mid-step appear on the next poll.
- `family: false` (the Board's one-row card) draws no branches. The session
  box counts its workers as before.

### Edges

For a stop that has branches, the direct `line` edge to the next box is
dropped, so the line reaches the next box **through** the branches. Each
branch adds one `fan` edge (stop → branch) and one `join` edge (branch →
join point), 2 × children in all. A `join` edge is `done: true` in three
cases: the branch is done, the parent's `join.waiting` is 0, or the join
point has already flown (done or skipped).

`reorderDiagram` (a drag preview) keeps branches on their stop and
re-targets their `join` edges, and `branch.to`, at the stop's new next box.

## The join point

The parent's `flight.join` applies to its **current** step. The join point is
that step itself when it is a `join` step (a Split → Join pair: the workers
were spawned at the Split). Otherwise it is the box right after it (an agent
step with `join`: the workers were spawned during it). That node gets
`join: {waiting, total}`:

- the canvas draws a "2 of 5" pill on its top edge while `waiting > 0`, and
  its accessible name adds "waiting for 2 of 5" (`joinLabel`);
- **the bird** (`birdPerch`) sits on the join point while `waiting > 0`. Its
  tone is `working`, or `you` (gold) when any branch into it is `asking`. A
  halted or paused flight keeps the bird on its current stop (red or still).

## Layout (`layoutDiagram`)

- **Horizontal**: the branch column sits between the spawning stop and the
  join point, which moves right to make room. The column is centred on the
  line's anchor, so one branch sits on the line and many hang down. The
  canvas grows to fit the tallest column, and children below drop under it.
- Branch boxes are smaller than line boxes: 156 × 52 (124 × 46 compact) plus
  16 px for a sub-line. At the line's `size`, compact is 110 × 40 (title
  alone) and wide is 210 px.
- Connectors are curved. Fan-out edges leave the stop's right edge at the
  line's anchor and enter each branch's left edge with **no arrowhead**.
  Fan-in edges leave each branch's right edge and enter the join point's left
  edge **with** an arrowhead. They are dashed while that worker is out, and
  green and solid once the branch is done or the join lets go.
- **Vertical (phone)**: the branches form an indented group under their stop,
  inside the line's column. Fan-out runs down the group's left side, and
  fan-in runs out along the right side into the join point.
- Reading and tab order: the stop, its branches, then the join point.
- The canvas draws no "+" insert gap between a stop and its join point.

## In the Sky (`SkyLine`)

Pressing a branch box opens it into the child's pane card, as a child box
does (it uses the same `kids` state and the same `PaneCard`). Nothing else is
focused or selected. A branch whose worker leaves takes its card with it.

## Split and Join stops (`useSessionLine`)

- The palette and the inspector are driven by the registry
  (`GET /api/flights/kinds`). **Split** (robot: `prompts`, `count`, `model`,
  `fence_only`, `report_back`) and **Join** (robot: `of` all|spawned,
  `on_failed` halt|continue) appear under "The flock does" once the server
  lists them. The `agent` kind's `join` toggle ("Wait for its workers")
  renders as a normal bool field.
- A Split's `prompts` is a textarea. Its inspector adds a "one worker per
  line" hint and hides `count` once `prompts` names more than one worker
  (`inspectorDef`, `promptLines`).
- Their boxes summarize themselves (`stopSummary`): a Split says "3 workers
  in parallel" (one per prompt line, else `count`), and a Join says "waits
  for every worker" or "waits for its split's workers", plus "goes on past a
  failure" when `on_failed` is continue.
- A selected Split that isn't already followed by a Join offers **+ Join**, a
  one-click chip that inserts a `join` step right after it
  (`POST /api/flights/{id}/steps {step, at}`). A Split tacked on through the
  palette opens in the inspector straight away, so the chip shows up
  immediately.

## Known gaps (outside this change)

- `StopIcon` (`components/flights/Bird.tsx`) has no `split` / `join` glyphs.
  Both draw the generic circle.
- The "one worker per line" hint is a note above the form. The field-level
  hint belongs in `StopParts.tsx`'s `StopField` (it has one for `items`). The
  Flyways editor's inspector (`PlanEditor`) doesn't hide `count` yet; it can
  reuse `inspectorDef`.
