# Loop back to the session's agent

A Loop stop on a session's chain can go back to the session's **agent**, the
head of the chain. The agent isn't a stop, so this is a separate kind of
target. Each repeat sends the agent a prompt, waits for that turn to end, and
then flies the chain again from stop 0. It repeats N times or forever, at a
pace.

The owner's case: in the Sky, the chain **Claude → Wait 5 h → Loop** should
repeat. After the wait, Claude runs again, then the wait, and so on.

This builds on the Loop block in `docs/flights.md` ("Round 16: the Loop block").
Code: `backend/web/core/flight_kinds/loop.py` (the kind) and
`backend/web/core/flights.py` (`LOOP_AGENT`, `loops_to_agent`,
`check_agent_loops`, `loop_back(..., to_agent=True)` and the `loop` branch of
`apply`). Tests: `tests/unit/test_flights_loop_agent.py`.

## Contract

### Config

| field | meaning |
|---|---|
| `back_to: "agent"` | The literal string: go back to the session's agent. Any other value keeps its old meaning: `""` is the first stop (or the Loop's own group's first stop), and anything else is a stop id. |
| `prompt` | Optional, at most 4000 characters. On each repeat, this is what the agent gets. Strict validation refuses a longer prompt (`PlanError` on `steps[i].config.prompt`, HTTP 400). Lenient validation (a block just placed) cuts it to 4000. Placeholders such as `{ticket}`, `{pr}` and `{branch}` are filled the way an Agent stop fills them. |
| `times`, `every_seconds` (older: `every_minutes`), `wait_limits`, `fresh`, `until` | Same as for any Loop. |

`GET /api/flights/kinds` describes `prompt` as a `textarea` field labelled
**"Prompt to send the agent"** (placeholder: "empty = the session's last
prompt"). The loop kind's summary now mentions the agent. The `back_to` field
keeps `options: []` / `dynamic: "stops_before"`, so the editor adds the agent
as a choice itself.

`GET /api/flights/{id}` (and every flight DTO) returns the loop step's
`config` unchanged. The frontend reads `config.back_to === "agent"` to draw the
return wire to the agent tile.

### Where it's valid

- **Valid on a flight that has a session.** It also counts toward
  `needs_agent` / `needs_session`. When a grounded chain holding such a Loop
  is attached to a folder (`title: ""`), it gets a new session there instead
  of running as a free chain.
- **Refused on a stops-only chain** (a free chain that runs in a folder with
  no session). The refusal is a `PlanError("Loop back to the agent needs a
  session — this chain runs in a folder", "steps[i].config.back_to")`:
  - when steps are added or edited on a free chain that's in the air (every
    `change_steps` op: add, add_steps, add_plan, edit, …). Nothing is written.
  - when a grounded chain is bound to a folder with no session
    (`flights.attach(id, "")` → `op_attach`).

  A grounded chain isn't refused, because it can still be attached to a
  session. At run time, a Loop to the agent on a flight with no session
  fails with `"Loop back to the agent needs a session"`.
- **A Loop to the agent with no stop before it** (Claude → Loop) behaves like
  the existing prompt mode (`repeats_prompt`). The session's own turn is
  run 1, and each later run re-sends the prompt and waits for the turn. Its
  receipt is "your prompt N times".

### A repeat to the agent

When the flight reaches the Loop again, these steps happen in order:

1. **The judge decides**, as for any Loop:
   - `until` met → `done`
   - `times` used up → `done`, with receipt "looped N times"
   - a usage limit and `wait_limits` → wait "waiting on the usage limit"
   - the pace not yet due → wait "next run in …"

   Otherwise the judge returns `run` with phase `loop` and
   `detail.target: "agent"`, and the note is "run 3 of 5 — back to the
   agent".
2. **`fresh`:** the provider's `clear_context_command()` is delivered first.
   A provider without one gets nothing.
3. **The prompt is delivered** to the session's agent through
   `flight_driver._deliver`, the same path the Agent stop uses (the prompt
   queue, which wakes a sleeping agent). The prompt is the first non-empty
   one of:
   - the Loop's own `prompt`
   - `detail.agent_prompt`: the session's newest prompt, read when the Loop
     first arrived (that is, the prompt the agent's turn before this run ran
     on)
   - the session's newest prompt now.

   If none of these exists, the step fails with **"Loop back to the agent
   needs a prompt"**. With the default `on_fail: halt`, the flight halts with
   that reason and nothing is sent.
4. **The jump** (`loop_back(..., to_agent=True)`):
   - every stop before the Loop goes back to pending
   - the cursor moves to 0
   - the Loop stays `running` with `runs += 1`, `last_start = now`,
     `target: "agent"`
   - history gets `looped` "run 3 of 5 — back to the agent".
5. **Stop 0 waits for the agent's turn.** Stop 0 (every member of its
   parallel group) carries `detail.after_turn: true` and `turn_from: <sent
   at>`. This is the gate a chain uses when it took off on a prompt
   (`start_on: "prompt"`, `flights._after_turn`). The stop shows a visible
   wait ("after your turn — …") until that prompt's turn has started and
   ended, then flies. Two things count as the end: the agent working and
   then going idle, or `session.turn_ended` / `worked_at` after `turn_from`.
   If the turn never starts within `AFTER_TURN_START_S` (10 min), the stop
   flies anyway.

The pace (`every_seconds`) counts from each run's start (`last_start`). Run 1
starts when stop 0 started.

## MCP

`start_flight`'s description (which `edit_flight` / `save_flight_plan` refer
to for step format) explains `back_to: "agent"` and `config.prompt`. The MCP
tools don't validate Loop fields themselves. They pass steps to the HTTP API,
and the kinds list they show comes from `GET /api/flights/kinds`.

## Notes for the integrator

- `"agent"` is now a reserved `back_to` value. A stop whose id is literally
  `agent` can no longer be a Loop target. Ids made by the editor and by the
  server are `s1…sN`, so this doesn't happen in practice.
- `detail.target` on a Loop to the agent is `"agent"`. The older prompt mode
  (a Loop first on the chain) still uses `"session"`.
