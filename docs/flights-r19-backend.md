# Flights round 19 backend — notes for the integrator

The backend half of docs/flights.md "Round 19: a chain starts when you say
so". Everything below is live on the server. The frontend types are in place
(`lib/flights.ts`, `lib/flightsApi.ts`); no UI was touched.

## State `ready`

- `F.STATES` gains `"ready"` (after `grounded`). `READY = "ready"`.
- `UNFINISHED` and `HOLDING` include it; `ACTIVE` does not (the driver never
  steps it). One unfinished chain per session, as before: a second
  `POST /api/flights/start` or `/attach` on that session is 409 `in_flight`.
- A ready flight is "on the ground" for editing (`_on_ground`): `_first_open`
  is 0, so tack on (`at: 0` too), edit, move, drop, group, ungroup, layout,
  halt and split_off all work on every stop. Dropping the first stop never
  pauses it. Stop configs are validated **strictly**, as on a flying flight
  (only a `grounded` chain takes incomplete blocks).
- Flight ops that need a flying flight (`done`, `skip`, `retry`, `jump`,
  `pause`, `resume`) are **409 `not_running`** on a ready flight ("this
  chain has not started yet — press Run first"). `cancel` and rename work.
- `DELETE /api/flights/{id}` removes a ready flight at once, with no cancel
  needed: nothing has started. (Running, waiting and paused still need a
  cancel first.)
- A ready flight whose session goes away is pruned and cancelled like any
  other unfinished flight.
- `flight.*` event `detail`: `"<session>: ready · runs on your next prompt"`
  or `"<session>: ready · runs when you press Run"`.

## New flight fields (stored, and in every flight dto)

| field | type | meaning |
|---|---|---|
| `start_on` | `"prompt" \| "run" \| "now"` | how it takes off. `"now"` on every flight started at once, and on older stored flights. A ready flight keeps its value after take-off. |
| `ready_since` | epoch float | when it became ready; `0.0` if it never was. A turn whose `activity_since` is not later than this does not count. |

The dto is the stored flight as it was before (`GET /api/flights`,
`GET /api/flights/{id}`, `{"flight"}` in every route response), plus those two
keys:

```json
{
  "id": "f_ab12", "plan_id": "adhoc", "plan_name": "Flight on s",
  "steps": [ ... ], "subject": {...}, "session": "s",
  "repo_root": "/repo", "branch": "feat",
  "state": "ready", "cursor": 0,
  "step_states": {"s1": {"state": "pending", "since": 0.0, "note": "", "detail": {}, "rounds": 0}},
  "reason": "", "history": [],
  "started": 1800000000.0, "updated": 1800000000.0, "finished": 0.0,
  "by": "user", "gone_since": 0.0, "announced": [], "boot": "", "owner": "", "owner_at": 0.0,
  "start_on": "prompt",
  "ready_since": 1800000000.0
}
```

## History

- New event **`took_off`**. Its entry carries an extra key, `by`:
  `{"at", "step", "event": "took_off", "note", "by": "run" | "prompt"}`.
  `note` is `"Run pressed"` or `"on your next prompt"`. `step` is the first
  stop's id.
- Changing `start_on` on a ready flight writes an `edited` event:
  `"starts on your next prompt"` or `"starts when you press Run"`.
- Attaching a grounded chain as ready writes `attached`, as before.

## Routes

| route | body | result |
|---|---|---|
| `POST /api/flights/start` | as before, plus optional `start_on` | absent / `null` / `""` = `"now"`, today's behaviour unchanged. `"prompt"` or `"run"`: 202 `{flight, absorbed}` with the flight `ready` at cursor 0. Leading agent stops are **not** skipped; nothing is started or seeded. A fast-track absorb still happens: its ship rungs become the leading stops at index 0, and the fast-track run is disarmed as today. With no `title`, the session is created with **no prompt** (`body.prompt` is ignored). A bad value is 400 `bad_start_on`. |
| `POST /api/flights/{id}/attach` | as before, plus optional `start_on` | same rule: ready at cursor 0, `seeded` ignored, and a session created here gets no prompt. |
| `POST /api/flights/{id}/run` | — | ready → `running` at cursor 0, with history `took_off {by: "run"}`; the first stop starts on the next driver pass. A halt point on the first stop pauses it there. Errors: 409 `not_ready` (any other state), 404 `unknown_flight`, 400 `bad_step` with `field` (a stop not ready to fly). |
| `PATCH /api/flights/{id}` | `{start_on}` (optionally with `plan_name`) | ready only, else 409 `not_ready`. `"prompt"` / `"run"` change it. `"now"` runs the flight, the same as `/run`. A bad value, `null` or `""` is 400 `bad_start_on`. `{plan_name}` alone is the rename, unchanged. With both keys, the rename goes first. → `{"flight"}` |

## Taking off on the next prompt (`start_on: "prompt"`)

`flight_driver.on_prompt(title)` (store half: `flights.on_prompt(title, now)`
→ `(before, after) | None`) takes the session's ready `start_on: "prompt"`
flight off **by prompt**. It announces, wakes the loop, and never raises.
Call sites:

1. `server.instance_send` (`POST /api/instances/{title}/send`, which is also
   the voice path: `/m` and the desktop mic send through it). Called after
   a **submitted** text was typed, or an `enter`. A non-submitted paste or a
   dictation batch does not count. A send that was queued instead counts when
   the drain types it.
2. `queue_driver._send_queued_item`: the prompt queue's drain, including the
   usage-limit auto-resume of a queued item.
3. `queue_driver._mail_type`: the mailbox lane and `delivery: "now"`, which
   is the path MCP `send_message` takes into a session.
4. The driver tick (`run_pass` → `_catch_typed_turn`) catches a turn typed
   straight into the terminal: `flights.ready_turn_started(flight, row)` is
   true when the row's `activity == "working"` and its
   `activity_since > ready_since`. A turn that was already under way when the
   chain became ready does not count; the next one does.

A `start_on: "run"` flight ignores all four.

### After that turn (`detail.after_turn`)

A take-off by prompt sets `running`, cursor 0, and on the **first stop's**
step state `detail = {"after_turn": true, "turn_from": <take-off epoch>}`.
Run never sets it. The planner gates that pending stop (`_after_turn`, after
the halt point check). It uses the same settle logic as an agent step
(`_settle` with need-work):

- `"after your turn — waiting for the agent to start"`: the prompt has not
  shown up as work yet. After `AFTER_TURN_START_S` (600 s) with no turn,
  the gate lets go.
- `"after your turn — the agent is working"`, `"… confirming the agent is
  done"` and so on: the turn is still going, or idle has not lasted
  `IDLE_SETTLE_S` (30 s). The queue still holding prompts, or a background
  job waiting, also holds the gate.
- The turn is over when the agent has been idle and settled, or is in
  `clarify` (waiting on you) after it worked. Then the stop runs normally.
- If the first stop is an **Agent stop with no prompt of its own** (an empty
  prompt after filling, and not `fresh`), that turn **is** the stop: it is
  marked `done`, with `note` `"your prompt's turn settled"` and
  `detail.receipt = "your prompt"` (`F.YOUR_PROMPT`), and the flight goes on.

While gated, `step_states[first].state` stays `"pending"` and the flight is
`running`. The gate's wait sentence is in `step_states[first].note`, which
the board can show as "after your turn — …".

## MCP

- `control_flight` gains op **`run`** (a flight op: `POST
  /api/flights/{id}/run`). `FLIGHT_OPS = ("pause", "resume", "cancel",
  "run")`. Its description now leads with `run (a ready flight takes off
  now)`. The tool count is unchanged (no new tool).
- `flight_status` describes `ready`. `compact_flight` adds `start_on` to the
  head of a ready flight.
- `start_flight` sends no `start_on`, so it still starts at once.
  `mcp.flights.UNFINISHED` includes `ready`.
- docs/mcp.md (outside my fence) still lists the old verbs. Add `run` there
  when you integrate.

## Defaults unchanged

None of these pass `start_on`, so they fly at once as before: the Intake Fly
button, schedules (`template_schedule`), triggers (`flight_triggers`), the
handoff kind (`attach` with no `start_on`), intake `arm_pending`, and MCP
`start_flight`.

## Frontend (types only)

- `lib/flights.ts`: `FlightState` gains `"ready"`. New
  `type FlightStartOn = "prompt" | "run" | "now"`. `Flight` gains
  `start_on?: FlightStartOn` and `ready_since?: number`.
  `FlightHistoryEntry.event` gains `"looped" | "took_off"` and
  `by?: "run" | "prompt"`.
- `lib/flightsApi.ts`:
  - `controlFlight(id, "run")` (toast "Chain started")
  - `runFlight(id)`
  - `setStartOn(id, startOn)`, which is `PATCH {start_on}`
  - `tackOnNew(title, steps, startOn?)` passes `start_on` only when given
  - `attachGrounded(id, {..., start_on?})`

  Every existing caller compiles unchanged. tsc and `vitest
  src/__tests__/flight*` were run from a scratch copy.

For the Sky's "build a chain by hand" paths, pass `"prompt"`: a stop added to
a session with no flight (`tackOnNew(title, steps, "prompt")`), and a
stops-only chain wired onto a session (`attachGrounded(id, {title,
start_on: "prompt"})`).
