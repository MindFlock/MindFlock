# Round 18 backend: notes for the integrator

Everything here is live on branch `emandel2630/r18-backend`. Tests:
`tests/unit/test_flights_r18_backend.py` (48 tests).

## 1. Step `pos`

- **Shape.** `step.pos = {x: int, y: int}`, chain-local plane px. Omitted when
  unset. The normaliser (`flights._normalize_pos`) keeps two finite numbers,
  rounds them and clamps them to ±100000 (`flights.MAX_POS`). Anything else
  (a string, a bool, NaN/Inf, a missing axis, a list) is **dropped silently**:
  it is never a 400, strict mode included.
- **Where it is kept.** `normalize_step` keeps it, so these all keep it:
  saved plans (`POST /api/flights/plans`), take-off, grounded flights, the
  flight dto (`GET /api/flights`, `GET /api/flights/{id}`, SSE
  `flight.changed`), `POST /steps` with `{step}` / `{steps}` / `{plan_id}`
  (tackOn / tackOnSteps / a saved stop dropped back), PATCH edits of other
  fields, and split_off.
- **`PATCH /api/flights/{id}/steps/{sid}` `{pos: {x,y} | null}`.** Layout
  only. It is allowed on ANY step: flown, current, ahead, on any flight
  (landed/cancelled ones too). It does not go through `_editable`, writes no
  history and does not bump `updated`. `null`, or a bad value, clears it.
  404 `unknown_step`. `pos` can share a body with other fields; those fields
  keep their own rules (a `title` on a flown step is still 409).
- **`POST /api/flights/{id}/layout` `{positions: {<sid>: {x,y} | null}}`** →
  `200 {flight}`. Bulk version of the above: `null` (or a bad value) clears,
  unknown ids are ignored. 400 `bad_layout` when `positions` is not an
  object; 404 `unknown_flight`.

## 2. Split off

`POST /api/flights/{id}/steps/{sid}/split_off` `{pos?: {x,y}}` (no body is
fine) → `200 {flight, chain}`.

- `flight` = the source flight without `sid…end`. Each moved step gets a
  `removed` history event ("<title>, split off").
- `chain` = a new **grounded** flight (`state: "grounded"`, `session: ""`,
  `plan_id: "adhoc"`), stored under a fresh `f_xxxxxx` id. Its name is
  `"<source plan_name> (from <first moved step title>)"`. Its steps are
  renumbered `s1..sN` and all pending. A Loop among them whose `back_to`
  named a moved step follows it to that step's new id (`_follow_loop_targets`).
- `pos`: every moved step keeps its own. When the body has `pos`, the first
  step's `pos` becomes the drop's.
- Refusals: 409 `not_pending` when any moved step has started or is at or
  before the cursor (the current step can never split off), 409
  `whole_chain` when `sid` is the flight's first step (nothing would be
  left: move the chain instead), 409 `finished`, 404 `unknown_step` /
  `unknown_flight`. Nothing is written on a refusal.
- Two `flight.changed` events go out: one for the source, one for the
  chain.
- Python: `flights.op_split_off(flight, sid, pos=None, now=None) -> chain`
  (pure, does not store the chain), `flights.split_off(fid, sid, pos)` →
  `(flight, chain)` (atomic under the store lock), and
  `flight_driver.split_off` (announces and wakes the driver).

## 3. The `handoff` kind ("Start a chain")

`core/flight_kinds/handoff.py`. `GET /api/flights/kinds` row:
`kind: "handoff"`, `label: "Start a chain"`, `family: "flow"` (new;
`flights.FAMILIES` now ends with `"flow"`), `actor: "robot"`,
`default_on_fail: "halt"`, `risk: []`. The backend sends no icon: the
frontend maps `handoff` → ↗.

Fields:

| name | type | default | notes |
|---|---|---|---|
| `target` | select | `""` | `options: []`, `dynamic: "chains"`. Value is `"flight:<id>"` (a grounded chain) or `"plan:<id>"` (a saved stop/flyway). |
| `session` | select | `"new"` | `options: ["new"]`, `dynamic: "sessions"`. Value is a session title or `"new"`; the UI labels `"new"` as "a new session". |
| `wait` | bool | `false` | |

- **Config.** Strict mode (take-off, a step tacked onto a flight in the air)
  requires `target` in one of the two forms, else `PlanError` with field
  `…config.target` (e.g. `step.config.target` from `POST /steps`). On the
  ground it may be empty, and `validate_for_takeoff` refuses it at attach.
  `session` is case-folded to `"new"`; empty means `"new"`.
- **Default title.** `"Start a chain"`, plus `" on <session>"` for a named
  session and `" and wait"` when `wait` is on.
- **Run.** It resolves the target:
  - `flight:<id>` must still be grounded and pass `validate_for_takeoff`.
  - `plan:<id>` → `flights.ground(plan)` makes a fresh grounded copy each
    time, its steps grouped under the plan's name as a dropped saved stop
    is. The plan itself is never consumed.

  A named session must exist and must not be on a flight. `"new"` →
  `flight_driver.spawn_worker(flight, step, prompt, report_back=False,
  title_prefix=<chain name>)`: a child of this flight's session, forked from
  its HEAD, on its CLI. The chain's first agent stop's prompt becomes the
  worker's initial prompt (seeded, so the driver does not type it again).
  The target is then attached with
  `flights.attach(..., triggered_by={flight, step, session})`.
- **Detail on the stop.** `child` (the started flight id), `child_name`,
  `session` (where it runs) and `receipt` = `"started <name> on <session>"`
  (≤ 80 chars).
- **`wait: false`.** The stop is `done` as soon as the target has taken off.
- **`wait: true`.** The stop stays `running` (note "waiting for <name> to
  land") until the child flight is:
  - `done`: the stop is `done`, receipt `"<name> landed"`;
  - `halted`: the stop fails with "<name> halted: <reason>";
  - `cancelled`: the stop fails with "<name> was cancelled: <reason>";
  - deleted: the stop fails with "<name> is gone".

  A fail goes through `on_fail` as usual (default halt).
- **Refusals** fail the stop with one of these sentences:
  - "the chain to start is gone"
  - "the saved stop <id> is gone"
  - "<name> is already flying" / "<name> is already over"
  - "no session <title>"
  - "<title> is already on a flight"
  - "<name>: <takeoff validation message>"

  A failed spawn deletes the plan copy it had made.
- **Retry or restart.** If the target flight is already flying and its
  `triggered_by` names this flight and stop, the stop adopts it instead of
  failing.
- `needs_session: true` (the flight's own session must be up: a new worker
  is its child).

### `triggered_by` on the flight dto

A flight started by a Handoff carries
`triggered_by: {flight: <source flight id>, step: <handoff step id>, session: <the SOURCE flight's session>}`.
The session the child runs in is the child's own `session`. The key is
**omitted** on every other flight, so don't expect `null`. It persists in
flights.json.

## 4. MCP

`backend/mcp/flights.py` is generic: it reads kinds from
`GET /api/flights/kinds` and passes step dicts through untouched, so
`handoff` shows up in `describe_flight`'s kinds and `pos` survives
`start_flight` / `edit_flight` / `save_flight_plan`. No MCP change was made,
and split_off is **not** in `edit_flight`.

## 5. For the frontend

- `FlightStep.pos?: {x: number; y: number}`,
  `Flight.triggered_by?: {flight: string; step: string; session: string}`.
- `saveStopPositions(fid, positions)` → `POST /api/flights/{fid}/layout
  {positions}`; one drag → `PATCH …/steps/{sid} {pos}` works too.
- `splitOffChain(fid, sid, pos?)` → `POST …/steps/{sid}/split_off {pos}` →
  `{flight, chain}`.
- The kinds editor must fill selects with `dynamic: "chains"` (grounded
  flights as `flight:<id>` + saved plans as `plan:<id>`) and
  `dynamic: "sessions"` (live titles + `"new"`).
- Draw the handoff wire to the started flight with
  `step_states[sid].detail.child`. Before it starts, use `config.target`.

## Open issue (outside my fence)

`tests/unit/test_flights.py::test_kinds_registry_drives_the_editor`
(line ~85) asserts that every select with empty `options` has
`dynamic == "stops_before"`. The handoff's `target` (`dynamic: "chains"`)
fails it. One-line fix: `assert fld["options"] or fld.get("dynamic") in
("stops_before", "chains", "sessions")`.
