# Flights — one route from arrival to merge

> DESIGN CONTRACT (branch `emandel2630/flights`). The sections marked *contract*
> are what the backend, the frontend and the integration work are built
> against in parallel; change them only by editing this file first.

## The idea

MindFlock had three top-bar surfaces that were really one thing seen from
three sides:

- **Intake** — work *arrives* (tickets, pull requests, issues) and becomes a
  session.
- **Verify** — a *human or agent checks* the work on a checklist.
- **Schedule** — work *starts by itself* on a timer.

Each of them was one step of the same journey, and the journey itself was
nowhere: once a ticket had become a session, nothing in the app knew whether
it was meant to be tested in the sandbox, wait for reviewers, answer the
CodeRabbit comments, or stop and ask before merging.

A **flight** is that journey, drawn as a row of blocks:

```
Ticket → Agent works → Commit → Push → Verify → Open PR → Reviews settle → Checks → You: approve → Merge → Ticket: Done
```

A **flight plan** is the row of blocks itself (a template of steps). A
**flight** is one run through a plan — one ticket, one PR, one scheduled job,
or something you started by hand. A **step** is one block: either a *robot*
step the server carries out by itself, or a *you* step where the flight parks,
pings you, and waits until you tick it off.

Every flight has exactly one session (a worktree + an agent) and the Board
shows where in its plan each flight is. Clicking a flight opens its session.

Why "Flight": a flight is a series of steps (a flight of stairs), a flight
plan is a route with waypoints that everybody understands, "in flight" is what
engineers already call work that is under way, and MindFlock is a flock.

### What stays, what folds in

Nothing is deleted. The three old surfaces become tabs of the Flights dialog
and their stores are untouched:

| Old surface | In Flights | Store (unchanged) |
|---|---|---|
| Intake lists (tickets / PRs / issues ready to start) | **Board → Arrivals**: each eligible item is a flight that has not taken off, with a Start button and a plan picker | pipeline `state.json`, settings sources |
| Intake source cards + Auto-start switches | **Sources** tab (the existing tab components, mounted as they are) | settings |
| Verify checklists | a **Verify** step inside a plan; the checklists list is the **Checklists** tab; open checklist items are *you* steps on the Board | `test_plans.json` |
| Schedule (templates) | **Schedule** tab (the existing templates view); a plan may also carry its own schedule trigger | `session_templates.json` |
| Fast-track depth (off→commit→push→pr→merge) | the **ship** steps of a plan; a per-source depth still works and maps to the built-in plan cut at that rung | `autopilot.json` |

The top bar shows **Flights** where Intake / Verify / Schedule were. `Alt+I`
opens Flights (Board), `Alt+V` opens it on Checklists, `Alt+S` on Schedule.
The badge counts what waits on you: flights parked on a *you* step, plus
Verify's due/failing checklists, plus arrivals that will not auto-start.

## Contract: the data model

Stored under the config dir (`~/.mindflock/flights/`): `plans.json` (synced
across devices like templates, via `sync_stores.register_store("flight_plans")`)
and `flights.json` (local, never synced, like `autopilot.json`). Writes are
atomic under a module lock; the file is re-read under the lock on every
accessor (the pattern of `core/autopilot.py`).

### Plan

```json
{
  "id": "p_7f3a2c",               // server-assigned; built-ins are "builtin:ticket", "builtin:pr", "builtin:schedule", "builtin:verify"
  "name": "Ticket to merge",
  "description": "",
  "builtin": true,                // built-ins are editable; "Reset to default" restores them
  "trigger": {                    // where flights of this plan come from (informational for manual/ticket/pr/issue; live for schedule)
    "kind": "manual" | "ticket" | "pr" | "issue" | "schedule",
    "schedule": null | {"at":"HH:MM","days":"daily"|"weekdays"} | {"cron":"* * * * *"} | {"once":"YYYY-MM-DDTHH:MM"},   // template_schedule.normalize_schedule shape
    "paused": false
  },
  "session": {                    // only used when the flight has to CREATE its session (manual / schedule). Ticket/PR/issue flights get their session from Intake's start path.
    "repo_path": "",              // "" = ask / last repo
    "program": "",                // "" = default CLI
    "model": "",                  // "" = CLI default
    "in_place": false
  },
  "steps": [ Step, ... ],         // 1..40
  "updated": 1793286000.0
}
```

### Step (a block)

```json
{"id": "s1", "kind": "agent", "title": "Work the ticket", "config": {...}, "on_fail": "halt" | "agent" | "skip" | "you"}
```

`title` is the user-visible block label (defaults per kind). `on_fail` says
what the driver does when the step fails: `halt` parks the flight with the
reason (default), `agent` hands the failure text to the session's agent and
retries the step once it settles (≤2 rounds), `skip` moves on, `you` turns it
into a *you* step with the failure as its text.

Step kinds and their `config` (the registry `STEP_KINDS` in
`backend/web/core/flights.py` is also served by `GET /api/flights/kinds` so the
editor's palette and forms are driven by the server):

| kind | robot/you | config | done when |
|---|---|---|---|
| `agent` | robot | `prompt` (text; placeholders `{ticket}`, `{pr}`, `{branch}`, `{subject}` filled by the server), `fresh` (bool: start a new session for this step instead of reusing the flight's) | the session's turn ended and settled (same gates as autopilot's `_agent_settled`: idle ≥30s, nothing queued, no background job, not usage-limited; plus `worked_at` evidence) |
| `ship` | robot | `lane`: `commit` \| `push` \| `pr` \| `merge`; `ask_first` (bool); `message` (optional commit message hint) | the autopilot record armed with `source="flight"` reaches `done`; a `halted` record fails the step with its reason (the autopilot's own fix hand-off runs first) |
| `verify` | robot (+you) | `who`: `agent` \| `you` \| `both` (default both); `wait_live` (bool: wait until the branch is on the live branch + deploy delay, default false) | the checklist exists for (repo, branch) and every step is settled; open *you* items park the flight as waiting on you with a deep link to the checklist |
| `you` | you | `text` (what to do), `items` (list of strings, optional checklist), `notify` (bool, default true) | you press **Done** (or **Skip**) on the Board, the pane strip, or the bell |
| `wait` | robot | `minutes` (int) or `until` ("HH:MM" local) | the time has passed |
| `reviews` | robot | `quiet_minutes` (default 10), `max_rounds` (default 5), `include_bots` (default true) | the PR has had no new unresolved review comments for `quiet_minutes`; each batch of new comments is sent to the agent (the pipeline's consolidated PR prompt) followed by an automatic push; `max_rounds` reached → fail |
| `checks` | robot | `timeout_minutes` (default 60) | the PR's checks are all passing; a failing check → fail (so `on_fail: agent` hands the failing check names to the agent and re-waits) |
| `command` | robot | `command` (shell, run in the worktree with the session's env), `timeout_minutes` (default 30) | exit 0; non-zero → fail with the tail of the output |
| `ticket` | robot | `state` (target state name or id, optional), `comment` (text, optional) | the tracker call returned; a flight without a ticket subject skips it |
| `notify` | robot | `text` | the ntfy/bell notification was sent (never blocks) |

A plan's **first** `agent` step of a ticket / PR / issue / schedule flight is
special: its prompt is the session's *initial* prompt. For ticket, PR and
issue flights the server-built intake prompt is used and the step's own
`prompt` text (if any) is appended as extra instructions.

### Flight

```json
{
  "id": "f_1a2b3c",
  "plan_id": "builtin:ticket",
  "plan_name": "Ticket to merge",
  "steps": [ Step, ... ],         // a SNAPSHOT of the plan's steps at take-off; editing the plan never changes a flight in the air
  "subject": {"kind": "ticket"|"pr"|"issue"|"schedule"|"manual", "source": "<source id or repo>", "id": "<ticket id / PR number / issue number>", "title": "<headline>", "url": ""},
  "session": "<session title>",   // the flight's session; set before the session exists for intake flights (keyed like autopilot)
  "repo_root": "", "branch": "",
  "state": "running" | "waiting_you" | "halted" | "done" | "cancelled",
  "cursor": 0,                    // index into steps of the current step
  "step_states": {"s1": {"state": "pending"|"running"|"waiting_you"|"done"|"skipped"|"failed", "since": 0.0, "note": "", "detail": {}, "rounds": 0}},
  "reason": "",                   // why halted / cancelled (human-readable)
  "history": [{"at": 0.0, "step": "s1", "event": "started"|"done"|"failed"|"skipped"|"you"|"halted"|"resumed"|"jumped", "note": ""}],  // ≤200
  "started": 0.0, "updated": 0.0, "finished": 0.0,
  "by": "user" | "intake" | "schedule" | "agent:<title>",
  "boot": "", "owner": "", "owner_at": 0.0   // lease, as autopilot.json
}
```

Rules:

- One flight per session at a time (`POST /api/flights/start` on a title
  that already has a running flight → 409 `in_flight`).
- A flight's position **is** stored (`cursor`), unlike autopilot: the steps
  are a script the owner drew, not a target. What is *re-derived* on every
  pass is whether the current step is done (git, GitHub, the checklist store,
  the autopilot record, the clock). Restart-safe: the driver reconciles from
  the stored cursor and fresh observation.
- Deleting the session cancels the flight (session-removal hook). Killing a
  flight never deletes the session.
- `prune` releases flights whose session is gone for >30 min, with the same
  `ARM_GRACE_S` idea as autopilot for intake flights armed before the session
  exists.
- The driver (`backend/web/core/flight_driver.py`) is a 5 s loop registered
  in the lifespan next to the autopilot loop, plus `BUS.subscribe` taps on
  `session.turn_ended`, `session.autopilot_changed`, `session.deleted`,
  `session.pushed`, `session.test_plan_checked`, `session.pr_review_changed`
  that `wake()` it. Its planner is a **pure function**
  `next_action(flight, obs, now)` in `flights.py` returning one of
  `wait | run | done | fail | you | advance | finish`, unit-tested without I/O.
- A `ship` step arms `lanes.arm_session(title, lane, ask_first, source="flight", item=<flight id>)`; `autopilot.SOURCES` gains `"flight"`. While a flight drives a session, its ⏩ fast-track button is the flight's (the pane strip replaces the guided next-step button).

### Events (added to `core/events.py` `EVENT_NAMES`)

- `flight.changed` — any state/cursor change, data: `{id, plan_name, session, state, cursor, step: {id, kind, title, state}, reason}`.
- `flight.needs_you` — a flight parked on a *you* step (or a verify step with open human items), data as above plus `text`, `items`. This also makes a bell row (`outbox.WAITING_ACTIONS` gains `flight_you`) and an ntfy push via a notify rule `flight_needs_you`.
- `flight.finished` — done, halted or cancelled, data as `flight.changed`.

### Describe a step / a plan (one-shot model)

`POST /api/flights/describe` `{"text": "send it to the PR reviewer, wait 5 minutes, then merge if checks pass", "want": "step"|"plan"}` →
`{"steps": [Step, ...], "model": "<id>"}` (a step request may still return
several blocks when the sentence describes several). Uses the one-shot
headless CLI plumbing of `core/commit_message.py` (`pick_argv` / `_run`,
≤60 s) with a prompt that lists `STEP_KINDS` and demands a `<flight>` JSON
block; the result is normalised through the same validator as a POSTed plan,
so a bad answer is a 422 `{"error": "could_not_describe"}` never a crash.
No model installed → 409 `no_model`.

## Contract: the HTTP API (`backend/web/routes/flights.py`, prefix `/api/flights`)

| Method and path | Body / query | Returns |
|---|---|---|
| `GET /api/flights/kinds` | | `{"kinds": [{kind, label, actor: "robot"|"you", summary, fields: [{name, type: "text"|"textarea"|"int"|"bool"|"select", label, options?, default?, placeholder?}], default_title}]}` |
| `GET /api/flights/plans` | | `{"plans": [Plan]}` (built-ins first) |
| `POST /api/flights/plans` | Plan (no `id` = create; `id` = replace) | `{"plan": Plan}`; 400 with `{"error", "field"}` on a bad step |
| `DELETE /api/flights/plans/{id}` | | 204; a built-in is reset to its default instead of removed |
| `POST /api/flights/plans/{id}/reset` | | `{"plan": Plan}` (built-ins only) |
| `POST /api/flights/describe` | `{text, want}` | `{"steps": [Step]}` |
| `GET /api/flights` | `?all=1` to include finished (default: unfinished + last 20 finished) | `{"flights": [Flight]}` |
| `GET /api/flights/{id}` | | `{"flight": Flight}` |
| `POST /api/flights/start` | `{"plan_id", "title"?: "<existing session to attach to>", "subject"?: {kind, source, id}, "repo_path"?, "prompt"?, "start_at"?: "<step id>"}` | 202 `{"flight": Flight}`. Attaching to an existing session (`title`) starts at `start_at` or the first non-`agent` step. Without `title` the server creates the session from `plan.session` (+ `repo_path` / `prompt` overrides) through `session_create.create_result` — the same path as the New dialog. A ticket/PR/issue `subject` goes through the Intake start path (`ticket_start.launch` / the PR and issue force-start bodies) with `plan_id`, which arms the flight on the deterministic title before the session exists. |
| `POST /api/flights/{id}/steps/{sid}/done` | `{"note"?}` | `{"flight"}` — only for a `you` step (or a `verify` step waiting on you: it marks the open human checklist items as passed) |
| `POST /api/flights/{id}/steps/{sid}/skip` | | `{"flight"}` |
| `POST /api/flights/{id}/steps/{sid}/retry` | | `{"flight"}` — a failed/halted step runs again |
| `POST /api/flights/{id}/jump` | `{"step": "<id>"}` | `{"flight"}` — move the cursor (a human "start at the PR stage" / "go back") |
| `POST /api/flights/{id}/pause` · `/resume` · `/cancel` | | `{"flight"}` |
| `DELETE /api/flights/{id}` | | 204 (finished flights only; a running one must be cancelled first) |

The session row (`/api/instances` payload) gains a `flight` field:
`{"id", "plan_name", "cursor", "total", "state", "step": {id, kind, title, state, actor}}`
or `null` — the pane strip and the rail read only this.

Intake's start routes (`POST /api/tickets/start`, `/api/github/prs/review`,
`/api/github/issues/start`) accept an optional `plan` (plan id); the pipeline's
auto-start reads a per-source `plan` setting (`TicketingSource.plan`,
`github.repo_settings[repo].plan`, `issue_repo_settings[repo].plan`; empty =
the built-in plan for that kind cut at the source's `depth`, which keeps every
existing setup behaving exactly as before).

## Contract: the UI

**Top bar.** One entry, **Flights** (replaces Intake / Verify / Schedule; the
palette keeps "Intake…", "Verify…", "Schedule…" entries that open the matching
tab). Badge = flights waiting on you + Verify due/failing + arrivals that will
not auto-start. Tooltip lists the three counts.

**Dialog** `#flights-dialog` (`frontend/src/components/flights/`, CSS prefix
`fl-`), tabs:

1. **Board** — the live view. Three groups, loudest first:
   - *Needs you*: flights parked on a *you* step (the step's text + checklist
     items with tick boxes, **Done** / **Skip**), Verify checklists due or
     failing (the existing row component, or a link into Checklists).
   - *In flight*: one strip per flight — the subject headline, then the blocks
     as a row of chips (done ✓ / current ● pulsing / pending ○ / failed ✗ /
     skipped –), the current step's name and how long it has been there,
     and a ⋯ menu (Open session, Pause/Resume, Jump to…, Cancel). Clicking
     the strip **opens the session** (`selectSession`, dialog closes).
   - *Arrivals*: the Intake work items that are eligible and have no session,
     grouped by source, each with **Start** and a plan picker (default = the
     source's plan). Starting selects the new session as soon as it exists.
   - *Landed* (collapsed): finished flights, last 20.
2. **Plans** — the editor. Left: the plan list (built-ins marked). Right: the
   plan as a horizontal row of blocks you can drag to reorder, with **+** between
   blocks that opens the palette (kinds from `/api/flights/kinds`, robot and
   you kinds visually distinct), a form per block (fields from the registry),
   `on_fail` picker, the trigger (manual / ticket / PR / issue / schedule with
   the existing `ScheduleWhen` editor), and the session defaults for
   manual/schedule plans. A **Describe a step…** box at the end of the row sends
   a sentence to `/api/flights/describe` and appends the result. **Take off**
   starts a manual flight of the plan right away (opens the New-session
   picker for the repo when the plan has none).
3. **Sources** — the existing Intake tab components (Tickets / Pull requests /
   Issues source cards and switches), mounted unchanged.
4. **Schedule** — the existing templates view, mounted unchanged.
5. **Checklists** — the existing Verify list, mounted unchanged.

**Pane strip.** A session in a flight shows, above its pane header's guided
next-step area, a one-line strip: `✈ Ticket to merge · ●●●○○○ · Reviews settle
(4m)` with the *you* step's **Done** / **Skip** inline and a ⋯ for Jump /
Cancel. It replaces the guided next-step button while the flight is running
and gives it back when the flight finishes.

**Rail.** The session row's chip shows `You: <step title>` in the needs-you
gold when a flight waits on you (via `deriveSessionState`: a flight *you* step
is a Needs-you state, in the bell).

**Starting opens the session.** Every start (Board Arrivals, Take off, a
template run, a manual flight) ends in `selectSession(title)` as soon as the
row exists, the way `templateRun.ts` already does.

**Tours.** A short `flights` CoachTour (`lib/tours/flights.ts`): Board, a
strip, Plans, Describe a step.

### Frontend notes (not contract: what the UI does where the contract is open)

- **Pause / Resume.** The ⋯ offers **Pause** while a flight is `running` or
  `waiting_you`, and **Resume** while it is `paused` or `halted`. A `paused`
  flight stays under *In flight* with a "Paused" note (its current block
  marked, not pulsing), is not in *Needs you* or the badge, its rail chip is
  the calm idle-style **Paused flight** (out of the bell), and its pane strip
  reads `✈ plan · ●●○○ · Paused · <step>`.
- **Halted flights** sit under *Needs you* with **Retry** (`/steps/{sid}/retry`)
  and **Skip** for the current step, the `reason`, and **Resume** in the ⋯.
- **The pane while a flight drives it** (`running`, `waiting_you`, `paused`):
  the guided next-step button AND the ⏩ fast-track toggle are hidden — the
  flight owns the ship ladder — and both come back once it is `halted`,
  `done` or `cancelled`.
- **"You:" once.** A *you* step is labelled `You: <title>` on the rail, the
  bell and the Board, unless its title already starts with "You:" (any case).
  The rail chip is capped in width with an ellipsis; its tooltip has the whole
  text.
- **Take off** on a plan with no `session.repo_path` (or a ticket / PR / issue
  plan) opens an inline row in the editor — a repository field with Browse…
  and an "on a session that is open" picker — rather than the New dialog; it
  posts `repo_path` or `title`.
- **Arrivals' plan picker** sends `plan` only when one is picked: the empty
  choice ("<built-in> (default)", or "Source's plan" when the source names
  one) leaves the server's own resolution — and so the depth cut — in charge.
  The built-in it names is `builtin:<kind>` (ticket / pr / issue) when the
  plans list has it, else `builtin:ticket`.
- **Select fields** in `GET /api/flights/kinds` may list `options` as plain
  strings or `{value, label}`; both are read.
- **Badge.** A flight parked on a `verify` step is counted once, as Verify's
  due checklist, not again as a flight waiting on you.
- **Bell.** Until the outbox lists a `flight_you` row for a session, the UI
  makes one from the session row's `flight` field (Done / Skip post the step
  routes with that row's `flight.id` / `flight.step.id`).

## Built-in plans (seeded on first read, editable, resettable)

- **Ticket to merge** (`builtin:ticket`, trigger ticket): Agent works the ticket → Commit → Push → Verify (agent) → Open PR → Reviews settle (10 min) → Checks → You: approve the merge → Merge → Ticket: comment "Merged by MindFlock" (state left alone unless the source has a done state configured).
- **Pull request** (`builtin:pr`, trigger pr): Agent addresses the review comments → Commit → Push → Reviews settle → Checks.
- **Scheduled task** (`builtin:schedule`, trigger schedule): Agent → Commit → Push → Open PR.
- **Verify on live** (`builtin:verify`, trigger manual): Verify (both, wait_live).

A per-source `depth` of `commit|push|pr|merge` with no `plan` set uses the
built-in for that kind cut after the matching ship step (so `pr` =
agent→commit→push→verify→open PR).

## Where it lives

| What | Where |
|---|---|
| Store, pure planner, step registry, built-ins | `backend/web/core/flights.py` |
| Driver loop, step executors, bus taps, events | `backend/web/core/flight_driver.py` |
| Routes | `backend/web/routes/flights.py` |
| Describe-a-step one-shot | `backend/web/core/flight_describe.py` |
| Intake / schedule / verify hooks | `ticket_start.py`, `server.py` (PR/issue start), `template_schedule.py`, `test_plans_driver.py` |
| Frontend | `frontend/src/components/flights/`, `frontend/src/lib/flights.ts`, `flightsApi.ts`, `tours/flights.ts` |
| Tests | `tests/unit/test_flights*.py`, `frontend/src/__tests__/flights*.test.ts` |
| CLI | `mindflock flights` (list), `mindflock flight start <plan> [--repo …]`, `mindflock flight done <id>` |
