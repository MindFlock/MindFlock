# Return wires explain themselves (the fix wire)

Branch `emandel2630/vz-fixwire`. What the owner saw (sc-25250, route "Agent
works the ticket → Commit → Push → Open PR"): the Commit stop's pre-commit
hook failed, the commit lane handed the failure to the agent, as it should,
"but in the flow I saw no arrow looping back" — and then: "we should have an
arrow if we are gonna loop back and a little text on the line explaining the
case why it happens."

## What it does now

- **Every stop that can go back to the agent draws a return wire**, with an
  arrowhead into the stop it goes back to (the nearest Agent stop before it,
  else the session's agent tile, as a Loop back to the agent does) and its
  case on the wire: "if pre-commit fails", "if the checks fail", "if CI checks
  fail", "if reviewers ask for changes". Quiet by default: thin, dashed,
  muted.
- **When it actually happens** the wire turns the warning tone (amber:
  `var(--warn, var(--tone-attention))`, no new colour), solid and flowing; its
  label is the real reason and the round ("pre-commit failed at ruff · fixing
  1/2", the whole reason in the tooltip); the fixing stop's face says "Agent
  fixing · round 1" and wears the same tone; the bird sits on the agent.
- **After**: "fixed after 1 round", faint green, until the route passes the
  next stop, then the quiet case again. Out of rounds: "halted after 2
  rounds", red.
- **Loops explain themselves too**: the idle pill says the case in words
  ("↻ repeat ×3 · every 30 min", "↻ until checks green"); while it runs, the
  "2 / 3" pill gets a caption for what the count does not say ("until checks
  green", "every 30 min"). Every Loop wire has the arrowhead as well.

## Which stops return, and why (`flights.returns_of`, pure)

| Stop | Returns when | Case |
|---|---|---|
| Ship | always, for what its lane really hands back on the rungs it climbs (from past the Ship stop before it up to its own lane): commit → a failed pre-commit hook; push → a failed verification check; PR → nothing; merge → red CI, a conflicting branch | "if pre-commit fails", "if the checks fail", "if CI fails or the branch conflicts" (joined with "or" when one stop climbs several rungs) |
| Reviews | always (new comments go to the agent) | "if reviewers ask for changes", of = `max_rounds` |
| anything else | only with **If it fails: Let the agent fix it** | "if CI checks fail" (checks), "if the command fails", "if the tests fail", "if the sync conflicts", else "if it fails" |

None for Agent / Loop / Split / Join / Wait-for-me stops, none for a route
running in a folder (a free route with no session), and none for Ship stops
when the owner turned the failure notes off (`repository.fasttrack_failure_notes`,
read by `flight_driver.lane_notes`). The Fast-track route's Open PR stop has
no return: the lane never hands anything back while opening a PR.

`returns` rides on the flight **only in the API** (`GET /api/flights`,
`GET /api/flights/{id}` via `flight_driver.flight_dto` / `list_dto`) — the
store never carries it, so it can't go stale.

## The live fix state (`step_states[id].detail.fix`)

`{active, round, rounds, of, reason (one short line), since, to, kind?,
outcome?}`:

- **Ship stops**: `_judge_ship` reads the lane record (`fixing`,
  `fix_rounds`, `fix_reason`, `fix_since`; `of` = the lane's 2 rounds) and
  hands it to `apply` as `action["fix"]`. The lane's record is fresh per arm,
  so the round count is per stop.
- **`on_fail: agent`**: `apply` sets it at the hand-off (round = the step's
  `rounds`, `of` = `MAX_FIX_ROUNDS`); the retry after the fix keeps it with
  `active: false`.
- **Reviews**: from `review_rounds` and `phase == "agent"`.
- `_track_fix` keeps it on the step when the fix is over (`outcome` "fixed"
  when the stop passes, "halted" when it fails), and writes one `fixing`
  history event per round ("pre-commit failed at ruff — the agent is fixing it
  (round 1 of 2)") and one `fixed` ("fixed after 1 round") — never one per
  driver pass.

## Drawing (frontend)

- `lib/flightDiagram`: `fixOf`, `returnMark` (what the wire says now: `case`
  / `fixing` / `fixed` / `halted`), `returnLabel` / `returnTitle`,
  `returnEdges` (edge kind `fix`), the stop's `ret` and its "Agent fixing ·
  round N" face; `birdPerch` puts the bird on the fix's target while it
  fixes. The classic canvas (`layoutDiagram`) arcs a `fix` edge like a Loop.
- `lib/skyGraph`: `fix` wires route exactly as Loop wires (`loopWirePathG`)
  and share their lanes, so a fix wire and a Loop on the same span stack a
  lane (20 px) apart; `arrowAt` gives every return wire its arrowhead;
  `wireLabel` puts the words on the run — plane-sized (10 px, they zoom with
  the route), **always inside the wire's own run** (cut with "…", dropped
  when nothing fits; a fixing label cuts the reason and keeps "fixing 1/2"
  whole). Wires on overlapping spans are a lane apart and wires on disjoint
  spans keep their words inside their own runs, so labels never overlap.
- `SkyChain` draws it all, so the Sky and the pane's Route tab (FlightChain)
  both get it; labels hide at far zoom (`[data-lod="far"]`).

## Choices (and why)

- **"Fixed ×1" stays only until the route passes the next stop**, then the
  wire goes back to its quiet case. Calmer: a long route that fixed three
  things early doesn't stay lit green; the history (and the step's
  `detail.fix`) still has it.
- **Plane-sized labels, not screen-sized like the Loop pill**: a
  screen-sized label can't promise to fit its run at every zoom; a plane
  label can, and at far zoom it is hidden anyway.
- **The case is computed on the server**, from what the lane really does
  (`LANE_RUNG_FIXES` mirrors autopilot `_ladder` / `_interrupt_action` /
  `_blocker_fix`) — the UI never claims a case the lane doesn't handle.

## Done by the agent (a Ship stop the agent already did)

The owner: "if the agents do some of the steps in the route I'd still like it
reflected in the route — like this case where the agent PR'd even though it
could have been done by the route." Real case f_a993cc (sc-25355): the agent
committed, pushed and opened the PR itself; the Commit stop armed its lane,
the lane wrote "already at pr — waiting for new work" and the stop sat
`running` forever, Push and Open PR pending, the bird stuck on Commit.

Now a Ship stop whose rung git already proves is done, **done by the agent**:

- **The truth is git's, not the lane's note**: `flights.agent_did(step, obs)`
  reads `obs["git"]` = the session's stage as the lane climbs by
  (`agent_state._session_stage`: a dirty tree is `agent`; a clean tree with
  commits ahead of its base is `committed`; origin holding HEAD is `pushed`;
  an open PR for the branch is `pr`) plus the commits ahead
  (`flight_driver._ship_git_obs`). `STAGE_RUNG` maps a stage to the rung it
  proves (autopilot `_STAGE_ACHIEVED`). Uncommitted work after a commit reads
  `agent`, so Commit still has work. Merge is never proven by a stage.
- Never while the agent is mid-turn (its turn may add work: the lane waits for
  it), and never once the lane has acted itself (`commits` / `step` on its
  record — that is the lane's own done).
- **Where**: a pending Ship stop, before it arms its lane; a running one whose
  lane has not acted. The driver's pass already carries on while each step
  finishes (`_CHAIN` = 4), so Commit, Push and Open PR all go done in ONE
  pass, each marked, up to the first with real work left. A lane armed for a
  stop done this way is disarmed (`_disarm_ours`: it had nothing left to do).
- **Marked**: `detail.by = "agent"`, `detail.receipt` "already committed · 3
  commits" / "already pushed" / "PR #123" (and `detail.url`); one history
  `done` event "done by the agent — <receipt>".
- **Drawn**: the stop's done tick, its receipt, and under it the agent glyph
  and "by the agent" (`DiagramNode.by`, `.sky-node-by`; hidden at far zoom).
  The screen reader hears "Commit — done by the agent, already committed · 3
  commits".

## Leftovers for the integrator

- `LANE_FIX_ROUNDS` (flights.py) mirrors `autopilot.MAX_FIX_ROUNDS`, and
  `LANE_RUNG_FIXES` mirrors the autopilot ladder — if those change, change
  these.
- `--warn` is used with a fallback across the flights CSS but is defined
  nowhere; the wire falls back to `--tone-attention` (gold/amber).
- The bundle (`backend/web/static/`) is not rebuilt here.
- `STAGE_RUNG` mirrors autopilot `_STAGE_ACHIEVED`.
- The PR receipt is "PR #123" (the link rides in `detail.url`; the face does
  not draw an ↗ link yet).
- Pins updated deliberately: `loopIdlePill` "↻ ×3" → "↻ repeat ×3"
  (flightLoop.test, skyChain.test); a retry after an `on_fail: agent` fix
  keeps `detail.fix` (test_flights.py).
