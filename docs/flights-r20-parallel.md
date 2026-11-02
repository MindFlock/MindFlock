# Flights round 20 B — parallel stops (integrator notes)

Spec: `docs/flights.md` → "Round 20: a session is a frame, the agent is a
tile; parallel stops — contract", part **B**. The owner put two Notify stops
after Open PR and they were forced into a line; now stops can run **side by
side**. This branch (`emandel2630/r20-parallel`) ships the backend and the
graph model. It changes **no UI component**: SkyChain / SkyLine / SkyView /
FlightChain / the palette belong to the frames worker. The last section lists
what they need to call.

## The field

```jsonc
// a step
{ "id": "s2", "kind": "notify", "title": "Tell me", "config": {…}, "on_fail": "halt",
  "parallel": { "id": "p_3fa9c1" } }   // omitted on a step of its own
```

- **A group is a run.** Consecutive steps that share `parallel.id` make up
  one parallel group (`p_<hex>`; the normaliser accepts `p_[A-Za-z0-9]{1,16}`).
  A run of one is not a group, so its `parallel` is dropped. If the same id
  turns up again in a later run, that run gets its own id. The new id is
  derived from the old one, not random, so reading a flight twice gives the
  same id.
- **Every path keeps it:** the step normaliser (`normalize_step`, then
  `_tidy_parallel` in `normalize_steps`), saved plans, `take_off`, `ground`,
  `attach`, tack-on (`/steps` single, `{steps}` and `{plan_id}`) and
  `split_off`. If a split cuts through a group, each side keeps whatever
  group is left on it, which is usually none.
- **Unsafe kinds:** strict normalisation (a POSTed plan or step) refuses a
  kind that isn't parallel-safe with `PlanError(code="not_parallel_safe")`,
  shown as 400 `{"error": "not_parallel_safe", "detail": "Agent can't run in
  parallel — use a Split for agents", "field": "…parallel"}`. Lenient
  normalisation (a stored file) just drops the field.
- **Placement inside a group:** a step inserted or moved strictly between two
  members of a group joins that group. If its kind is unsafe, the request is
  refused with 400 `not_parallel_safe` (field `at`). A step placed at the
  group's edge stays a step of its own.

## Parallel-safe kinds (`parallel_safe` in `GET /api/flights/kinds`)

| allowed (true) | refused (false) |
|---|---|
| notify, wait, command, checks, ticket, you (perch), someone, test, call, listen, handoff, screenshot, preview, budget, checkpoint, review (second-opinion worker) | agent, ship, reviews, verify, split, join, land, sync, loop, judge |

How it is decided: `flights.parallel_safe(kind)`. The kinds in
`NOT_PARALLEL` (agent, ship, reviews, verify, split, join, land, sync, loop)
are always refused. A plug-in is also refused when it has
`Kind.types_to_agent` (Judge does) or sets `spec["parallel_safe"] = False`.
Everything else is allowed. The test `test_parallel_safe_per_kind` lists
every registered kind, so a new kind has to be decided there. Why each
refused kind is refused:

- verify: its agent half prompts the agent.
- loop: its prompt mode re-sends the session's prompt.
- review (the plug-in) is allowed: it spawns its own reviewer worker and
  never touches the agent.

## Driver semantics

- **Cursor:** at a group, the cursor always stands on the group's first
  member. `_normalize_flight` and `_advance` both enforce that.
- **Each pass:** the driver (`flight_driver.step_flight` → `_step_member`)
  takes every unfinished member of the cursor's group. For each one it
  observes, judges (`next_action` on `member_view(flight, i)`, a shallow copy
  with `cursor = i`) and applies, through the existing intent → effect →
  outcome path (`_mark_intent` / `_record_outcome` / `_apply_if_unchanged`
  now take the member index; `flights.apply_at`). Each member keeps its own
  `step_states` entry.
- **Leaving the group:** `_advance` moves past a group only once every member
  is passed: done, skipped, or failed with `on_fail: skip` (`detail.moved_on`).
  When that happens the chain carries on into the next step within the same
  pass.
- **A failing member** fails the group according to its own `on_fail`. The
  first failure wins:
  - `halt` halts the flight. The other members keep their state, and Resume
    (`op_resume`) re-runs every failed member.
  - `skip` marks that member as moved on.
  - `you` parks the member on you.
  - `agent` hands it to the agent.
- **Flight state:** a flight at a group is `waiting_you` while any member
  waits on you, otherwise `running` (`_group_state`). The "needs you"
  notification and `you_text` use the member that is waiting
  (`waiting_step`).
- **Step ops:**
  - Done, Skip and Retry accept any member of the current group.
  - Skipping one member while the group is halted on *another* member's
    failure leaves the flight halted.
  - Jump onto a member jumps to the group's first member.
  - Removing a member while its group is flying leaves the other members
    flying; it does not pause the flight.
  - Take-off on a prompt gates every member of a first group (`after_turn`).
- **Where stops can change:** `_first_open` is the end of the current group,
  so nothing can be inserted inside a group that is flying.
- **`step_view` / `row_dto` / events:**
  - The current step gains `parallel: [{id, kind, title, state, actor,
    note}]` when it is a group.
  - `_view_key` compares every member's state, so a member finishing emits
    `flight.changed`.

## Routes

| call | does | refusals |
|---|---|---|
| `POST /api/flights/{id}/steps {step, parallel_with: sid, at?}` | Puts the new step in `sid`'s group, after its last member (or at `at`, which must be in `lo..hi` of the group). If `sid` has no group, one is made from `sid` and the new step, minting `p_<6hex>` on both. | 400 `not_parallel_safe` (either kind), 400 `bad_at`, 404 `unknown_step`, 409 `in_progress` (making a group needs `sid` still ahead; growing the group in the air is fine), 409 `finished` |
| `PATCH /api/flights/{id}/steps/{sid} {parallel: null}` | The step leaves its group and flies right after it. A group left with one member stops being a group. | 409 `in_progress` (only steps still ahead), 400 `bad_parallel` (anything but `null`) |
| `PATCH /api/flights/{id}/steps/{sid} {parallel_with: other}` | The step joins `other`'s group (making one if needed) and moves to after its last member. This is the Shift-drop. | 400 `not_parallel_safe`, 400 `bad_parallel` (itself / empty), 409 `in_progress` |

These follow the same rule as the other step edits: `_editable` (running,
waiting, paused, halted, ready and grounded flights; not landed or cancelled
ones) plus `_pending_index` (the step must be still ahead). MCP: the
`edit_flight` / `start_flight` / `save_flight_plan` step schema gained an
optional `parallel` object.

## Frontend model

**`lib/flights.ts`**
- `FlightStep.parallel?: {id}` and `StepKindDef.parallel_safe?`.
- `parallelSpan(steps, i) → [lo, hi)` and `parallelGroups(steps)`.
- `PARALLEL_UNSAFE` and `isParallelSafe(kind, kinds?)`: uses the kinds
  list's `parallel_safe` when present, else the client's own list.

**`lib/flightsApi.ts`**
- `wireStep` keeps `parallel`.
- `tackOn(flightId, step, at?, {parallel_with?})`.
- `tackOnParallel(flightId, step, withSid, at?)`.
- `leaveParallel(flightId, sid)`.
- `joinParallel(flightId, sid, withSid)`.

**`lib/flightDiagram.ts`**
- Stop nodes of a group carry `node.parallel = {id, index, size}`
  (`parallelMark`), in `flywayDiagram`, `flightStops`, `flightDiagram` and
  `sessionDiagram`.
- `lineEdges(line)` builds the line's edges:
  - `fan` edges from the place before the group to each member.
  - `join` edges from each member to the place after (`done` once that
    member has flown).
  - The `add` edge comes only from the last member.
  - Two groups in a row are joined all-to-all.
  - `chain()` and the Split-fans path of `sessionDiagram` both use it.
- `lineRuns(line)` splits the line into its places (a group is one place).
- `insertFromOf(f)` is the end of the current group. `flightStops` marks a
  current group's members `current` / `paused`, and the failed member
  `halted`. Members of a flying group are not `editable`.
- `collapseFlown` never ends the flown pill inside a group. `foldRun` turns
  a folded group's fan/join edges back into plain line.
- `layoutDiagram` stacks members in one column (same x, `vgap` apart); the
  line goes on past the widest member. In vertical layout the members are
  indented, like a Split's branches.

**`lib/skyGraph.ts`**
- **Placement** (`autoPlace`): the first member sits at the group's spot; the
  rest go `PITCH_Y` below the member before them, in the first member's
  column, unless pinned. The line goes on from the first member's row.
  Branch rows start under the deepest member.
- **Wires:** fork and join wires are ordinary béziers out→in (`wirePath`),
  with kind `fan` / `join` and ids `a>b:fan` / `a>b:join`. They are not the
  dashed sub-session bus. A first group forks straight out of the head, one
  `fan` per member.
- **The +** hangs off the last member (`plus.after`).
- **Helpers:**
  - `groupLast(chain, node)`: a member's last sibling. "After a member"
    always means after the whole group.
  - `nextOf(chain, node)`
  - `parallelOk(chain, next, kinds?)`
- **`wireIntent`** gains `{op: "parallel", chain, after, with, at?, step?}`:
  - **X's out port dropped on empty sky** while X already has a next that is
    parallel-safe and still changeable (or already in a group that hasn't
    flown) gives `{op:"parallel", after: X, with: next, at}`. Otherwise you
    get today's `palette`.
  - **Shift-drop on a stop Y of the same chain** (editable, parallel-safe,
    not X, not X's next, not already in the next's group) gives
    `{op:"parallel", after: X, with: next, step: Y}`. `wireTargets` lights
    those Y's while Shift is held.
  - **From a member X**, move / loop / merge / handoff / palette all use
    `after` = the group's last member.
  - `WireIntentCtx.kinds` (optional) lets the kinds list decide which kinds
    are safe.

**`components/flights/useSkyWiring.ts`**
- `ChainEntry.paletteParallel?(withNode, pos)` (optional) and a 5th, optional
  argument to `useChainEntry(register, graph, wrap, paletteAfter,
  paletteParallel?)`.
- `SkyWiringOpts.kinds?()` (optional) and `wireTargetsOf(…, shift, kinds?)`.
- `runWireIntent` handles `parallel`:
  - With no `step`, it calls `chain.paletteParallel(with, pos)`. If the chain
    has no hook, it falls back to `paletteAfter(after, pos)`, which is
    today's behaviour.
  - With `step` (Shift), it calls `joinParallel(flight, step, with)` and
    toasts "“Y” runs beside “N”".

## What the UI must do (frames worker / integrator)

1. **SkyLine / GroundedLine:** pass a `paletteParallel` to `useChainEntry`.
   It opens the same palette as `paletteAfter`, but when a stop is picked it
   calls `tackOnParallel(flight.id, step, firstStepOf(withNode).id)` instead
   of `tackOn(…, at)`. The new tile goes at `pos`; save it with
   `queueStopPos` as `paletteAfter` does. A stops-only (grounded) chain uses
   the same call, since the route accepts grounded flights. A session with no
   flight yet has no group to join, so `paletteAfter` is fine there.
2. **SkyView:** pass `kinds: () => kinds` in `useSkyWiring` opts, so
   unsafe kinds never light up and an unsafe next falls back to the palette.
3. **SkyChain (draw):**
   - Members arrive already placed in a column, and their `fan` / `join`
     wires arrive as ordinary bézier `GraphWire`s. Draw `fan` / `join` like
     `line`, with no dash; `done` dims a join.
   - Optionally draw a faint bracket or outline around the members. The
     column is `nodes.filter(n => n.node.parallel?.id === pid)`.
   - Show the + at `graph.plus`, as today.
4. **Stop right-click menu (StopMenu / stopEditing):** add "Run in parallel
   with the next stop" (`joinParallel(f, sid, nextSid)`) and, on a member,
   "Run on its own" (`leaveParallel(f, sid)`). Both only on `editable`
   stops of a parallel-safe kind (`isParallelSafe(step.kind, kinds)`).
5. **FlightChain (pane Flight tab) / FlywayCanvas:** `layoutDiagram` already
   stacks the members and draws the fork and join, so nothing is required.
   An insert gap between two members inserts *into* the group (the server
   adopts it); the canvas may hide those gaps.
6. **Palette error:** a 400 `not_parallel_safe` arrives through
   `flightAction`'s error card, with the sentence "… use a Split for agents".
   No special handling is needed.

## Tests

- `tests/unit/test_flights_r20_backend.py` — 37 tests: the kind table and the
  dto, the normaliser, take-off / ground / attach / split-off, the pure
  planner at a group, the driver passes (`step_flight` with a fake server),
  and the routes.
- `frontend/src/__tests__/r20Parallel.test.ts` — 24 tests: lib/flights,
  flightsApi bodies, the diagram's fork / join / fold / layout, skyGraph's
  column / wires / + / wireIntent, and runWireIntent.
- No existing skyGraph or flightDiagram test had to change.
