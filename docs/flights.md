# Flights — one route from waiting to landed

> How Flights work — for people using them (start at [Using Flights](#using-flights))
> and for code. The sections marked *contract* are what the backend, the
> frontend and the integration are built against; change them only by editing
> this file first.

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

A **flight** is that journey, drawn as a line of stops:

```
Ticket → Agent works → Commit → Push → Verify → Open PR → Reviews settle → Checks → Perch: approve → Merge → Ticket: Done
```

The words are birds', never planes' — MindFlock is a flock:

| Word | Is | On the wire |
|---|---|---|
| **flyway** | the route a flock follows: a line of stops (a template) | a *plan* (`plans`, `plan_id`, `FlightPlan`) |
| **flight** | one run of a flyway — one ticket, one PR, one scheduled job, or something you started by hand | a *flight* |
| **stop** | one box on the line: a *robot* stop the server carries out by itself | a *step* |
| **perch** | a stop that waits on you: the flight perches, pings you, and flies on when you press **Done** | a step of kind `you` |
| **sky** | the open space where every flight is drawn: the main area's second view beside the grid (the same sessions as lines), a pannable, zoomable plane | (UI only) |
| **line** | one session's row of blocks in the sky: its session box, its workers, its stops or rungs, and the trailing + | (UI only) |

Glossary in one line: *flyway* = the route, *flight* = one run, *stop* = one
box, *perch* = a stop that waits on you, *sky* = the open space where every
flight is drawn, *line* = one session's row of blocks. "Take off", "In flight" and "Landed"
are what they sound like. The glyph is a small bird, never ✈. Only the words
changed: every field and route keeps its name (`plans`, `steps`,
`/api/flights/...`).

Every flight has exactly one session (a worktree + an agent) and the Sky
draws every session as a line, a bird on the stop each flight is at.

### What stays, what folds in

Nothing is deleted, and nothing is nested: Intake, Verify and Schedule stay
the flat top-bar dialogs they always were, their stores untouched, and each
carries its side of the journey. Flights are drawn in the Sky.

| Surface | Its part in a flight | Store (unchanged) |
|---|---|---|
| Intake lists (tickets / PRs / issues ready to start) | **Sky → the intake lane and the drawer's Waiting to fly**: each eligible item is a flight that has not taken off, under its kind's heading with a kind badge, a **Fly** button and the flyway as chips | pipeline `state.json`, settings sources |
| Intake source cards + Auto-start switches | the **Intake** dialog, each source card with its **Flyway** chips | settings |
| Verify checklists | a **Verify** stop on a flyway; the checklists stay in the **Verify** dialog; open checklist items perch the flight (the Sky's drawer lists it) | `test_plans.json` |
| Schedule (templates) | the **Schedule** dialog; a flyway may carry its own schedule trigger, and Schedule lists those under **Scheduled flyways** | `session_templates.json` |
| Fast-track depth (off→commit→push→pr→merge) | the **ship** stops of a flyway; a per-source depth still works and maps to the plain ship ladder at that rung (the agent, then commit → … → the depth — nothing else); an armed ⏩ is drawn as rung boxes on the session's line ([The session diagram](#the-session-diagram)) | `autopilot.json` |

The top bar is **New · Grid / Sky · Intake · Verify · Schedule · Settings**:
`Alt+I` / `Alt+V` / `Alt+S` open the three dialogs, `Alt+Y` switches to the
sky (the main area's other view). The Sky half of the toggle counts the
flights perched on you. Flyways are made, edited and flown from the Sky's
**flyway shelf**. (Sky rounds 4–7 put Intake / Verify / Schedule behind one
Flights button as tabs of a Flights dialog; round 8 retired it — see
[Round 8: flat again](#round-8-flat-again).)

## Using Flights

### How a ticket becomes a flight

Every way work arrives ends in the same start path, and every one of them can
put the new session on a flight:

- **Intake / the Sky's Waiting to fly** (its intake lane and drawer) — **Fly** on a ticket, pull
  request or issue (`POST /api/tickets/start`, `/api/github/prs/review`,
  `/api/github/issues/start`, or `POST /api/flights/start` with a `subject`,
  which runs the very same code). The flyway chips send `plan`; leaving them
  on the default leaves the choice to the server.
- **The pipeline** — a ticket, PR or issue it auto-starts.
- **An agent** — `spawn_ticket_session` from another session.

Which flyway (`plan`), in order:

1. the start's own `plan`;
2. a fast-track **depth picked for this start** (the row's ⏩ pick, a team
   run's "off", an agent's `autopilot=`) — the built-in plan cut at that rung;
3. the **source's plan** — Intake's source card (`TicketingSource.plan`) or the
   repo card (`github.repo_settings[repo].plan` for PRs,
   `github.issue_repo_settings[repo].plan` for issues), while that plan exists;
4. the built-in plan for the kind **cut at the source's depth** — so a source
   set to "carry it to a PR" flies *Agent works → Commit → Push → Verify → Open
   PR*;
5. **no flight** — a source with no plan and a depth of off/agent starts its
   sessions exactly as it always did.

The flight is armed on the session's title **before the session exists** (an
intake title is deterministic), so the item is "in flight" in the Sky and in
the sidebar's provisioning row from the first second, and survives a server
restart mid-launch. Its first agent step is the session's first prompt — the
ticket / PR / issue prompt Intake always built — with the step's own text (if
any) appended. The flight's ship steps arm the fast-track lanes themselves, so
the source's depth is not armed beside it (unless the plan has no ship steps).
A start whose session can't be created cancels its flight with the reason.

### Scheduled plans

A plan whose trigger is **Schedule** starts itself, like a template: daily or
weekdays at a time, a cron expression, or once at a date and time — the same
editor (and the same `GET /api/templates/cron-preview` previews) as
Schedule, and the same rules (`core/template_schedule.py`): a schedule
counts from when it was set, after downtime only the most recent missed run
fires, a paused trigger fires nothing, and in a group of devices only the
automation device fires. Each run takes off like **Take off** — a new session
from the plan's session defaults — recorded with `by: "schedule"`. Scheduled
plans fire from the **templates addon's schedule loop** (`template_schedule.
run_loop`, beside the templates' own schedules), so they stop with it: a server
with that addon disabled fires neither.

A run is **skipped** (and says so in the plan's history, with a refetch of the
Sky) while the plan's previous flight is still in the air or its session is
working, asking a question or at a usage limit — a schedule never stacks
flights. A run that can't take off (no folder set, the agent not installed) is
a failed run with the reason. The plans list (`GET /api/flights/plans`) shows a
scheduled plan's next run, last run and history under `runs`; the record is
`~/.mindflock/flights/schedule_runs.json`, on this device only (the plans
themselves sync). The Schedule dialog lists templates and, under **Scheduled
flyways**, these plans — with that record (next run, last result, history),
**Fly now** and **Edit in the Sky**.

### Verify steps

A verify step asks for the session's checklist through Verify's own button
path and parks the flight on you while human items are open. Ticking them off
in the Verify dialog moves the flight on at once (`session.test_plan_settled`
wakes the driver); **Done** on the step records them as passed. A checklist the
push watcher makes for the same branch is the same checklist — Verify keys
them by (repo, branch).

### The CLI

```
mindflock flights [--all]                 # what is in the air: flyway, subject, session, state, stop
mindflock flight flyways                  # the flyways (scheduled ones say when next); `flight plans` = the same
mindflock flight start FLYWAY [--repo PATH] [--title SESSION]
mindflock flight done FLIGHT [--note …]   # the perch waiting on you; also skip / cancel
```

`FLYWAY` is an id or a name (a unique prefix is enough); `FLIGHT` is a flight
id (prefix) or its session's title. See [cli.md](cli.md).

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
{"id": "s1", "kind": "agent", "title": "Work the ticket", "config": {...}, "on_fail": "halt" | "agent" | "skip" | "you", "halt": true}
```

`halt` (optional, omitted when false) makes the step a **halt point**: the
flight stops before it ([Halt points](#halt-points)). A plan keeps it (the
per-plan editor saves it with the plan) and a flight's snapshot carries it.

`title` is the user-visible stop label (defaults per kind). A *you* step — a
**perch** in the UI — carries no prefix in its title (the UI adds "Perch:"):
"Approve the merge". `on_fail` says
what the driver does when the step fails: `halt` parks the flight with the
reason (default), `agent` hands the failure text to the session's agent and
retries the step once it settles (≤2 rounds), `skip` moves on, `you` turns it
into a *you* step with the failure as its text.

Step kinds and their `config` (the registry `STEP_KINDS` in
`backend/web/core/flights.py` is also served by `GET /api/flights/kinds` so the
editor's palette and forms are driven by the server):

| kind | robot/you | config | done when |
|---|---|---|---|
| `agent` | robot | `prompt` (text; placeholders `{ticket}`, `{pr}`, `{branch}`, `{subject}` filled by the server), `fresh` (bool: start a new session for this step instead of reusing the flight's), `join` (bool, default true, "Wait for its workers") | the session's turn ended and settled (same gates as autopilot's `_agent_settled`: idle ≥30s, nothing queued, no background job, not usage-limited; plus `worked_at` evidence) — and, with `join`, every live child of the session has finished ([Fan-out and join](#fan-out-and-join)) |
| `split` | robot | `prompts` (textarea: one worker per non-empty line; `{index}` / `{n}` and the flight's placeholders filled), `count` (int, default 1: a single line is spawned this many times), `model` (text, optional: the workers' `profile_model`), `fence_only` (text, optional: comma-separated paths each worker may change → its fence's `only`), `report_back` (bool, default true: the `spawn_session` report footer) | every worker was created as a child of the session (`parent`, `spawned`, forked from the session's HEAD); their titles are in `step_states[sid].detail.children`. Follow it with a `join` |
| `join` | robot | `of`: `all` (every live child, default) \| `spawned` (only the workers the nearest `split` before it made); `on_failed`: `halt` (default: a worker whose report is `failed` fails the step with its summary) \| `continue` | every worker in the set has finished ([Fan-out and join](#fan-out-and-join)) |
| `ship` | robot | `lane`: `commit` \| `push` \| `pr` \| `merge`; `ask_first` (bool); `message` (optional commit message hint) | the autopilot record armed with `source="flight"` reaches `done`; a `halted` record fails the step with its reason (the autopilot's own fix hand-off runs first) |
| `verify` | robot (+you) | `who`: `agent` \| `you` \| `both` (default both); `wait_live` (bool: wait until the branch is on the live branch + deploy delay, default false) | the checklist exists for (repo, branch) and every step is settled; open *you* items park the flight as waiting on you with a deep link to the checklist |
| `you` | you | `text` (what to do), `items` (list of strings, optional checklist), `notify` (bool, default true) | you press **Done** (or **Skip**) in the Sky (its box, or the drawer), the pane strip, or the bell |
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
  "state": "running" | "waiting_you" | "paused" | "halted" | "done" | "cancelled",   // paused = held by you (Halt now) or at a halt point (reason "halt point"); Resume carries on. halted = stopped on a failure (retry / skip / resume)
  "cursor": 0,                    // index into steps of the current step
  "step_states": {"s1": {"state": "pending"|"running"|"waiting_you"|"done"|"skipped"|"failed", "since": 0.0, "note": "", "detail": {}, "rounds": 0}},
  "reason": "",                   // why halted / cancelled (human-readable)
  "history": [{"at": 0.0, "step": "s1", "event": "started"|"done"|"failed"|"skipped"|"you"|"halted"|"paused"|"resumed"|"jumped"|"added"|"edited"|"removed"|"moved"|"halted_at", "note": ""}],  // ≤200
  "started": 0.0, "updated": 0.0, "finished": 0.0,
  "by": "user" | "intake" | "schedule" | "agent:<title>",
  "boot": "", "owner": "", "owner_at": 0.0   // lease, as autopilot.json
}
```

Rules:

- One flight per session at a time (`POST /api/flights/start` on a title
  that already has a running flight → 409 `in_flight`).
- A flight's `steps` snapshot is never changed by its plan, but **can be
  changed in the air** through the step routes: a step can be added after the
  current one, and a step still ahead (pending) edited or removed. The planner
  lands a flight only when its cursor has passed every step, so a step added to
  a flight whose last step is finishing is flown.
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
  `session.pushed`, `session.test_plan_checked`, `session.test_plan_settled`,
  `session.pr_review_changed` that `wake()` it. Its planner is a **pure function**
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
| `POST /api/flights/start` | `{"plan_id"?, "title"?: "<existing session to attach to>", "subject"?: {kind, source, id}, "repo_path"?, "prompt"?, "start_at"?: "<step id>", "steps"?: [Step], "absorb_fasttrack"?: bool}` | 202 `{"flight": Flight, "absorbed": [lane]}`. **`steps`** (1..40, validated like a plan's; 400 `bad_step` with `field`) draws the flight here: alone, the flight is `plan_id: "adhoc"`, `plan_name` = the subject's headline or "Flight on <title>", its steps numbered `s1..sN`; with `plan_id`, the plan's steps then the given ones (next free `sN`). An ad-hoc flight needs `title` or `repo_path` (400 `needs_title`). **`absorb_fasttrack`** (default true with `title`): the session's own fast-track run (autopilot record `running` or `halted`, `source != "flight"`) becomes the flight's leading `ship` steps — the rungs after the one the session's stage (`committed`/`pushed`/`pr`) proves, up to the run's lane, in `autopilot.DEPTH_ORDER` order — placed where the flight starts, and the record is disarmed so the ladder carries on under the flight; `absorbed` lists their lanes. A `done` run is left alone; a `halted` one is absorbed (its first step starts `pending`, the halt reason goes into the flight's history). Attaching to an existing session (`title`) starts at `start_at` or the first non-`agent` step. Without `title` the server creates the session from `plan.session` (+ `repo_path` / `prompt` overrides) through `session_create.create_result` — the same path as the New dialog. A ticket/PR/issue `subject` goes through the Intake start path (`ticket_start.launch` / the PR and issue force-start bodies) with `plan_id`, which arms the flight on the deterministic title before the session exists. |
| `POST /api/flights/{id}/steps` | `{"step": Step, "at"?: int}` | `{"flight"}` — appended, or inserted at index `at`, which must be after the current step (409 `in_progress`); the flight must be `running`/`waiting_you`/`paused` (409 `finished`). The new step takes the next free `sN`. History `added`. |
| `PATCH /api/flights/{id}/steps/{sid}` | `{title?, config?, on_fail?, halt?}` | `{"flight"}` — only a pending step after the current one (409 `in_progress`); validated like a plan step. `{halt}` alone may also mark / clear the **current** step ([Halt points](#halt-points)). History `edited`. |
| `DELETE /api/flights/{id}/steps/{sid}` | | `{"flight"}` — only a pending step after the current one (409 `in_progress`). History `removed`. |
| `POST /api/flights/{id}/steps/{sid}/move` | `{"at": int}` | `{"flight"}` — a pending step after the current one moved to index `at` of the final list (pop then insert), clamped to the last place; `at` at/before the current step is 409 `in_progress`; 400 `bad_step` (`field: "at"`) when `at` is not an integer. Its `step_states` record moves with it; the same place changes nothing. History `moved`. |
| `POST /api/flights/{id}/steps/{sid}/done` | `{"note"?}` | `{"flight"}` — only for a `you` step (or a `verify` step waiting on you: it marks the open human checklist items as passed) |
| `POST /api/flights/{id}/steps/{sid}/skip` | | `{"flight"}` |
| `POST /api/flights/{id}/steps/{sid}/retry` | | `{"flight"}` — a failed/halted step runs again |
| `POST /api/flights/{id}/jump` | `{"step": "<id>"}` | `{"flight"}` — move the cursor (a human "start at the PR stage" / "go back") |
| `POST /api/flights/{id}/pause` · `/resume` · `/cancel` | | `{"flight"}` |
| `DELETE /api/flights/{id}` | | 204 (finished flights only; a running one must be cancelled first) |

The session row (`/api/instances` payload) gains a `flight` field:
`{"id", "plan_name", "cursor", "total", "state", "step": {id, kind, title, state, actor}, "join", "branch"}`
or `null` — the pane strip and the rail read only this. `join` is `{waiting,
total}` while the current step (an `agent` step with `join`, or a `join` step)
has workers outstanding, else `null`. `branch` is `{parent, step}` on a
worker's row while its parent is in a flight: the parent's step during which it
was spawned (the `split` that names it, else the step whose `since` ≤ the
worker's creation < the next step's `since`, else the current step).

Intake's start routes (`POST /api/tickets/start`, `/api/github/prs/review`,
`/api/github/issues/start`) accept an optional `plan` (plan id); the pipeline's
auto-start reads a per-source `plan` setting (`TicketingSource.plan`,
`github.repo_settings[repo].plan`, `issue_repo_settings[repo].plan`; empty =
the built-in plan for that kind cut at the source's `depth`, which keeps every
existing setup behaving exactly as before). A per-start `depth` outranks the
source's `plan` (a team run's `"off"` must stay off) — the full order is in
[How a ticket becomes a flight](#how-a-ticket-becomes-a-flight). An unknown
`plan` is a 400.

## Contract: the UI

**Words.** Birds, never planes (see [The idea](#the-idea)): flyway, flight,
stop, perch; the glyph is a small bird (`components/flights/Bird.tsx`). The
wire keeps its names; only what a person reads changed.

**Top bar.** Flat: **New · Grid / Sky · Intake · Verify · Schedule ·
Settings**, each with its own badge, tooltip, calm-hour rule and shortcut
(`Alt+I` / `Alt+V` / `Alt+S`, ids `intake` / `verify` / `schedule`). No
Flights entry. The Grid / Sky toggle's Sky half carries the badge of flights
perched on you (a flight perched on a Verify stop is Verify's count, not this
one; red when one halted — `perchBadge`). The palette has "Intake…",
"Verify…", "Schedule…", "Switch to the Sky / grid" and "Flyways…" (the Sky
with its shelf open).

**The flyway canvas** (`components/flights/FlywayCanvas.tsx`, model
`lib/flightDiagram.ts`). Stops are boxes on a canvas laid left to right with
auto-drawn SVG connectors (curved, with an arrowhead) between consecutive
stops — no ports, no manual wiring: the order is the sequence. A box: the
kind's icon, the title, one line of config (`stopSummary`: "push", "10 min
quiet", "who: agent"). A perch is rounder, outlined gold, with a branch motif.
Editing: drag to reorder (pointer events, mouse and touch: the picked box
lifts, the others slide, the drop snaps, the connectors redraw); ← / → move the
selected box, Delete removes it, Enter opens it; **+** between boxes on hover
and at the end opens the stop palette ("The flock does" / "You do", each kind
with its summary), inserting there. Live: the same canvas draws a flight's
snapshot steps with their `step_states` — done dimmed with ✓, current glowing
(gold perched on you, green while the flock works, red halted with the
reason), skipped struck through, pending outlined; a perch waiting on you
shows its checklist and **Done** / **Skip** inside its box; a ⋯ on every box
(**Jump here** / **Retry** / **Pause** / **Resume** / **Cancel flight**). It
pans sideways (scroll, or drag empty space); below ~520px it stacks top to
bottom with the connectors running down.

**Controls.** No native `<select>` and no bare `<input>` / `<textarea>`
anywhere in Flights: enum choices (lane, who, on_fail, trigger, the flyway)
are the New dialog's segmented control or a chip row, free text the New
dialog's field look, a prompt its prompt box, a number a field with its unit
("10 min"), a yes/no a switch chip (`components/flights/controls.tsx`).

**No dialog.** There is no Flights dialog (`DialogName "flights"` is only
an alias the store routes, below). Where each part lives:

1. **Flyways** — on the Sky's **flyway shelf** (`FlywayShelf`): every saved
   flyway as a chip; a chip opens its **card** (`FlywayCard`): the flyway on
   the canvas, the stop palette, the inspector for the selected stop (its
   fields in the controls above, `on_fail` as chips), **Describe a stop…**
   (`/api/flights/describe`, highlighted until saved), the trigger under
   **Takes off** (by hand / ticket / PR / issue / schedule with its days as
   chips and its time, date or cron as fields, Paused) and the session
   defaults for by-hand and scheduled flyways (`FlywayInspector`). Save /
   Save & fly on… / Cancel; its ⋯ holds **Reset to default** (built-ins) and
   **Delete flyway…** (yours, asked inline). **+ New flyway** opens a fresh
   card; a chip's **Fly** or a drag onto a line flies it.
2. **Sources** — the **Intake** dialog, each source card with its **Flyway**
   chips (`PlanSelect`).
3. **Scheduled flyways** — listed in the **Schedule** dialog under their own
   heading (name, schedule line, next run, last result, **Fly now**, **Edit in
   the Sky** → the card).
4. **Checklists** — the **Verify** dialog.

**Addresses.** The retired dialog's links still land
(`lib/flights.ts` `legacyFlightsRoute`, routed by the store's
`openDialogFor("flights", …)` and by `lib/surfaceHash.ts`, which rewrites the
URL in place):

| Old link / target | Lands on |
|---|---|
| `#flights`, `sky`, `sky/<title>`, `board` | the Sky (`#sky`, `#sky/<title>`) |
| `flyways[/<id>]`, `plans[/<id>]` | the Sky with the shelf open (and that flyway's card) — `useUi.showFlyways(id)` |
| `sources[/<x>]` | Intake, target `<x>` (`#intake/<x>`) |
| `checklists[/<branch>]` | Verify, that checklist (`#verify/<branch>`) |
| `schedule[/<x>]` | Schedule, target `<x>` (`#schedule`) |
| anything else | the Sky |

**The sky is a view, not a tab.** It is the main area's second view, beside
the grid ([The Sky](#the-sky), docs/web-ui.md "Two views: the grid and the
sky"); `#sky` / `#sky/<title>` are its addresses.

**Pane strip.** A session in a flight shows, above its pane body, a one-line
strip: the bird · `Ticket to merge · ●●●○○○ · Reviews settle (4m)` — a button
that opens the session's **Flight** tab — with a perch's **Done** / **Skip**
inline and a ⋯ for Pause / Resume / Cancel. It replaces the guided next-step
button while the flight is running and gives it back when the flight
finishes.

**Pane Flight tab.** A tab **Flight** next to Agent / Terminal / Diff / Queue /
Map (header item `flight`), drawing [the session diagram](#the-session-diagram)
full width. It shows once the session has anything to draw (a flight, workers,
a parent, an armed fast-track), badged **!** while perched on you or halted;
the session ⋯ menu's **Fly a flyway…** opens it on any session, **Show in the
Sky** switches to the sky on its line alone, and the tab's own **Open in the
Sky** does the same (`showInSky`, `#sky/<title>`). The bird stands on the line's active box. With
nothing on the line it also offers the flyways as cards (**Fly this flyway**,
built-ins first, the stops as tiny boxes) and **Describe a flyway…** (describe
`want=plan` → `POST /api/flights/plans` → `POST /api/flights/start` with this
session's `title`).

**Rail.** The session row's chip shows `Perch: <stop title>` in the needs-you
gold when a flight is perched on you (via `deriveSessionState`: a perch is a
Needs-you state, in the bell, where its row reads `Perched on you: <stop>`).

**Starting opens the session.** Every start (Waiting to fly's Fly, a flyway
flown from the shelf or Schedule's Fly now, a template run, a manual flight)
ends in `selectSession(title)` as
soon as the row exists, the way `templateRun.ts` already does.

**Phone.** No canvas: a session perched on you gets a card with the bird,
`<flyway> · Perch: <stop>`, and **Done** / **Skip**.

**Tours.** A short `sky` CoachTour (`lib/tours/sky.ts`; it was the `flights`
tour): the top bar's Grid · Sky toggle (`data-tour="view-sky"`), a line
(`sky-line`), the bird (`flights-bird`), the shelf (`sky-shelf`), each
falling back to the toggle. Its **?** ends the Sky's toolbar; it opens by
itself the first time the Sky is shown in the calm first hour. The Intake,
Verify and Schedule tours walk their own dialogs again.

### The Sky

The main area's second view, beside the grid (`components/flights/SkyView.tsx`,
the switch in `lib/viewMode.ts`, the pure rules in `lib/skyLayout.ts`): the
same sessions as the grid, each drawn as a line on one open expanse. The rail,
top bar, bell and dialogs stay; App renders it in the grid's place while
`useUi.view === "sky"` (`mf_view`, per device).

- **An isomorphism of the grid**: one selection (`useUi.focused`) — a press
  on a line selects its session, a selection anywhere brings its line into
  view; a line's session box opens into the session's real `Pane`, which reads
  the same `lastTab[title]` as the grid's; terminals come from the
  `lib/terminals` registry, never re-attached; the grid's View 1 is the sky's
  "fit the focused line" and This session, 2 / 4 / 9 its Fit; the bell's
  needs-you rows are the drawer's Perched on you; New is a new-session card on
  the sky. The full table is in docs/web-ui.md.
- **Switch points**: the top bar's Grid · Sky toggle (`data-tour="view-sky"`),
  the sidebar View row's **Sky** chip (a number returns to the grid), `Alt+Y`,
  the palette, `#sky` / `#sky/<title>`. Switching keeps the selection, the
  camera, the drawer and the shelf; each pane's ghost flies to its line and
  back (~250 ms; none under `prefers-reduced-motion`).
- **One plane**, panned and zoomed as a whole: one `translate(x, y)
  scale(z)` container, no library. Drag on empty sky pans (mouse and touch),
  the wheel scrolls, Ctrl/⌘+wheel and a pinch zoom (0.4×–2×) about the
  pointer, Space+drag pans from anywhere; keys: arrows pan, + / − zoom, 0
  fits all, 1 the focused line, T tidies, Enter opens the focused line's box,
  Esc closes it. A faint dot grid scales with it, in both themes, the flock
  (`SkyFlock {width, height, zoom, paused?, density?}`) drifting behind.
- **Every session is a line** (`SkyLine {title, rows, plans, layout,
  onLayout, focused, onFocus, zoom, onDragStart?, onMeasure?, dropFlyway?}`):
  its title bar (drag handle, size control, ⋯), its
  [session diagram](#the-session-diagram) (the same canvas, live, with its
  + / palette / inspector — `useSessionLine`, shared with the pane's Flight
  tab) and its session box, which expands into the real pane when
  `layout.open`. A new session appears on the next poll; a closed one's line
  fades out. Hidden helpers (`hidden_by`) are never drawn.
- **The layout store** (`localStorage` `mf_sky_layout`, `{title:
  SkyLineLayout}` where `SkyLineLayout = {x, y, size: compact | normal |
  wide, open, paneW?, paneH?, pinned?}`; the old `mf_sky_pos` positions are
  read once as pinned lines). A dragged line is pinned where it was dropped;
  the rest are auto-placed by `placeLines(lines, pinnedPositions(layout),
  area)` at their measured size (`onMeasure`, else `estimateLine`) — rows,
  newest at the top, never overlapping. **Tidy** (toolbar, `T`) clears every
  pin and the lines glide back; a line's `onLayout({pinned: false})` is "Tidy
  this line".
- **The bird** (`birdPerch(diagram) -> {nodeId, tone} | null`, the canvas's
  `bird` prop): on the flight's current stop, else the fast-track's current
  rung, else the session box while its agent works (or asks you); none when
  it idles with nothing armed. It bobs while working, sits gold and still
  perched on you, red halted, and hops (a left/top transition) to the next
  box.
- **Build from the space**: a double-click on empty sky, **+ New session**
  or `Ctrl/Alt+N` opens a `NewSessionBlock` card there (`{x, y, plans,
  onCreated, onCancel}`) that creates the session through the New dialog's
  path, then — a flyway picked — `POST /api/flights/start {plan_id, title}`;
  the new line takes the card's spot, pinned.
- **Flyways from the sky**: the folding shelf at the foot (`FlywayShelf
  {plans, onNew, onFly, onEdit}`, remembered as `mf_sky_shelf`). `onFly(plan,
  title?)` → `POST /api/flights/start {plan_id, title}` on that line, else the
  focused one, else a chip row to pick a line; a chip dragged onto a line
  (`dataTransfer` `application/x-mindflock-flyway` = the plan id; the host
  passes `dropFlyway` to the line under the pointer) flies there on drop.
  `onNew` / `onEdit(plan)` open a `FlywayCard {x, y, plans, initial?, onSaved,
  onCancel}` mid-view.
- **Views** (toolbar chips): **All** (every session, workers on their own
  lines), **Mine** (default: top-level sessions, workers folded into their
  parent's line), **Perched on you**, **This session** (the focused session's
  line alone, fit — `#sky/<title>`); a find field filters by name or task.
- **The intake lane** (`IntakeLane {rows, plans, onFly, width?}`) pinned at
  the plane's left edge: the sources as blocks with their waiting items and
  arrows to the lines they started.
- **The drawer** (`SkyDrawer.tsx`) at the right edge, foldable: *Perched on
  you* (each perched or halted flight with its perch block — checklist,
  **Done** / **Skip**, or **Retry** — and Verify's due or failing checklists),
  *Waiting to fly* (folded; kind badges, flyway chips, **Fly**) and *Landed*
  (folded).
- **Phone**: no pan or zoom — the lines as a list, each canvas top to bottom.

### The session diagram

A session's canvas is a systems diagram of what it is and what it is about to
do — so a person can pilot it and see what is running. `sessionDiagram(rows,
title)` (`lib/flightDiagram.ts`, pure, unit-tested) composes it from the
`/api/instances` rows alone (no read route of its own; the full flight from
the shared flights query when it is cached, else the row's `flight` field):

```
   [parent]                                 above — click = go up
      │
   [session] → [rung | stop] → … → [+ Tack on a stop]
      ├─ [child]                            below, on a branch — click = open it
      └─ [child]
```

- **The session box**: the agent — its state chip (Working, Idle, Perch: …,
  Needs you) and its headline.
- **Its parent** above (`row.parent`), when it has one, and **its children**
  (rows whose `parent` is this session, one level) as a vertical branch below,
  each with its own state chip and a mini ladder (its flight's dots, else its
  fast-track's).
- **The fast-track ladder** as boxes when the row's `autopilot` is armed and
  there is no flight: commit → push → pr → merge up to its depth, a rung done
  once the row's git `stage` has reached it (committed / pushed / pr /
  merged), the first rung not reached glowing, a halted run's rung red with
  the reason — the same boxes as ship stops.
- **The flight's stops** when the row has a live `flight` (live mode above).
- **Always a trailing + ("Tack on a stop")** at the end of the line. On a
  session with no flight it starts an ad-hoc flight of the picked stop: `POST
  /api/flights/start {title, steps: [Step], absorb_fasttrack: true}` (no saved
  flyway; `absorb_fasttrack` turns the armed fast-track's remaining rungs into
  the leading ship stops and disarms it, so the ladder's boxes become flight
  boxes seamlessly). On a live flight: `POST /api/flights/{id}/steps {step,
  at?}` (appended; a **+** between two pending stops inserts there — only after
  the current stop), `PATCH /api/flights/{id}/steps/{sid}` (a pending stop's
  title / config / on_fail, from the inspector) and `DELETE
  /api/flights/{id}/steps/{sid}` (a pending stop). Done and current stops are
  read-only. The + also offers **Fly a flyway**: all of a flyway's stops,
  appended to the live flight one by one, or — no flight yet — a flight of
  that flyway on this session (`POST /api/flights/start {plan_id, title}`).

Toggling ⏩ fast-track on a session therefore shows up as rung boxes appearing
on its line (the ⏩ code path itself is unchanged), and adding a ship stop
through + when no fast-track is armed just works through the flight. The same
diagram, compact and on one row (no family; the session box counts its
workers), is each card under the Sky drawer's *Landed*.

### Frontend notes (not contract: what the UI does where the contract is open)

- **The sky's parts, as built in round 7.** The view host (`SkyView.tsx`)
  imports `SkyLine`, `SkyFlock`, `FlywayShelf` and `FlywayCard` from one seam,
  `components/flights/skyContracts.tsx`, which carries their contracts as
  placeholders (the line as the Sky drew it before, plus the opened box as the
  real `Pane`; no flock; no shelf; a card that only closes) until their own
  files land — then that module becomes four `export *` re-exports. The host
  keeps the camera (across a trip to the grid), the layout store, the drag
  (`onDragStart` → window pointer listeners, a 4px threshold, a plain press
  selects), the fit, the keys and the flyway drop; `SkyLine` draws.

- **Pause / Resume.** The ⋯ offers **Pause** while a flight is `running` or
  `waiting_you`, and **Resume** while it is `paused` or `halted`. A `paused`
  flight stays under *In flight* with a "Paused" note (its current box marked,
  not glowing), is not in *Needs you* or the badge, its rail chip is the calm
  idle-style **Paused flight** (out of the bell), and its pane strip reads
  `<flyway> · ●●○○ · Paused · <stop>`.
- **Halted flights** sit under *Needs you* with **Retry** (`/steps/{sid}/retry`)
  and **Skip** for the current stop, the `reason` in its red box, and
  **Resume** in the ⋯.
- **The pane while a flight drives it** (`running`, `waiting_you`, `paused`):
  the guided next-step button AND the ⏩ fast-track toggle are hidden — the
  flight owns the ship ladder — and both come back once it is `halted`,
  `done` or `cancelled`.
- **"Perch:" once.** A perch is labelled `Perch: <title>` on the rail and the
  boxes and `Perched on you: <title>` in the bell, unless its title already
  starts with "Perch:" (or the old "You:", any case), which is read without
  it. The rail chip is capped in width with an ellipsis; its tooltip has the
  whole text.
- **Flying a flyway from the shelf** with no line focused shows the line
  picker (a row of chips) and posts `title`; Schedule's **Fly now** posts
  `{plan_id}` alone, so the server creates the session from the flyway's
  session defaults.
- **A waiting item's flyway chips** send `plan` only when one is picked: the
  default chip ("Default · <built-in>", which the server cuts at the source's
  depth) leaves the server's own resolution in charge. The built-in it names
  is `builtin:<kind>` (ticket / pr / issue) when the list has it, else
  `builtin:ticket`. A row's chips start on its source's `plan` when one is
  set; picking the default chip there then sends the built-in's id, since ""
  would mean the source's flyway.
- **The source cards' Flyway chips** (Intake's source cards) write `plan` with the
  card's existing save: on the ticketing source via `PUT
  /api/settings/ticketing/sources`, under `github.repo_settings[repo]` /
  `github.issue_repo_settings[repo]` via `POST /api/settings`. The default chip
  clears the key. A stored flyway id the list no longer has stays as its own
  chip, "<id> (not found)". The Fast-track picker stays beside them (it is the
  default flyway's cut).
- **Registry fields** in `GET /api/flights/kinds` become designed controls: a
  `select` is a segmented control (its `options` read as plain strings or
  `{value, label}`; lane and who in the app's words), a `bool` a switch chip,
  an `int` a field with its unit (`*minutes` → "min", `max_rounds` →
  "rounds"), a `textarea` the prompt box (a perch's `items` one per line,
  stored as a list), a `text` the field look.
- **Badge.** A flight perched on a `verify` stop is counted once, by Verify's
  badge as its due checklist, not again on the Sky toggle's perch count.
- **Bell.** Until the outbox lists a `flight_you` row for a session, the UI
  makes one from the session row's `flight` field (Done / Skip post the step
  routes with that row's `flight.id` / `flight.step.id`).
- **The canvas's layout** is computed, not measured (`layoutDiagram`: fixed
  box sizes, a perch opened wide declares its own), so a server render draws
  the same boxes and connectors a browser does, and the tests pin both.

## Built-in flyways (seeded on first read, editable, resettable)

- **Ticket to merge** (`builtin:ticket`, trigger ticket): Agent works the ticket → Commit → Push → Verify (agent) → Open PR → Reviews settle (10 min) → Checks → Perch: Approve the merge (step title "Approve the merge") → Merge → Ticket: comment (step title "Ticket: comment"; it posts "Merged by MindFlock" and moves the ticket nowhere — set the step's `state` to move it).
- **Pull request** (`builtin:pr`, trigger pr): Agent addresses the review comments → Commit → Push → Reviews settle → Checks.
- **Scheduled task** (`builtin:schedule`, trigger schedule): Agent → Commit → Push → Open PR.
- **Verify on live** (`builtin:verify`, trigger manual): Verify (both, wait_live).

Every built-in's agent step keeps `join` on (the default), so a ticket agent
that spawns workers is waited for automatically: its next stop starts only
once the agent and all its workers are done. None of them uses `split` /
`join` stops; those are for flyways you draw.

A per-source `depth` of `commit|push|pr|merge` with no `plan` set is the
**ship ladder only** — exactly what that fast-track did before Flights: the
kind's agent step, then commit → push → pr → merge cut at the depth, with no
verify / reviews / checks / *you* / ticket step and nothing asking first (so
`pr` = agent→commit→push→open PR, and `merge` is a plain merge with no approve
step). The full built-in ("Ticket to merge" …) is opt-in: pick it as the
source's `plan` or at start.

## As built: where the backend differs from (or adds to) the contract

Everything above holds; these are the additions and the places the backend
had to choose. Field and route names are unchanged.

- **Flight record extras.** `gone_since` (prune's clock) and `announced` (the
  needs-you / finished keys already said, so a restart never repeats a push).
- **Paused vs halted.** A flight is `paused` for one of two causes: Pause
  (**Halt now** in the UI) sets it (`reason` stays empty; history event
  `paused`) on a running or waiting flight — a halted one stays halted; or
  the flight reached a [halt point](#halt-points) (`reason: "halt point"`,
  history `halted_at`). A paused flight is not driven. **Resume** goes back to `running`, or
  to `waiting_you` when the step is parked; on a halted flight it runs the
  failed step again. Done / Skip on a paused flight tick the step off and the
  flight stays paused; Retry and Jump are a "go" and set it running.
  `flight.finished` is never said for a pause.
- **Which flights hold their session.** `POST /api/flights/start` on a session
  whose flight is `running`/`waiting_you`/`paused` is 409 `in_flight`; a
  `halted` one is cancelled ("replaced by a new flight") and the new one takes
  off. `DELETE /api/flights/{id}` refuses the same three states (cancel first).
- **A side effect happens once.** A step's effect (a prompt, an armed lane,
  a command, a tracker call, a fix note) is first recorded as an intent
  (`step_states[sid].detail.performing = {token, at, action, phase}`, the
  step set running) under the store lock, only if no route moved the flight
  since it was read; then carried out; then its outcome recorded against that
  token. A pass that finds a mark waits ("carrying out …") instead of
  repeating it; a mark older than 10 minutes (a server that died mid-effect)
  no longer blocks a retry. A pause landing mid-effect keeps the outcome and
  stays paused; a skip / jump / cancel drops it. A pass that changes nothing
  but the clock writes nothing and announces nothing.
- **A ship step reads only its own record.** It (and the reviews step's
  push) judges the autopilot record only when `source == "flight"`, `item`
  is this flight's id and the lane is the step's; any other record on the
  session (your own ⏩ run) is a visible wait: "Fast-track is busy with your
  own run".
- **Start refuses before it creates.** `start_at`, the plan and the folder are
  checked before a session is created, so a refusal leaves nothing behind. A
  halted flight a start replaces is announced (`flight.finished`, reason
  "replaced by a new flight"); the driver's pass also announces, once, any
  finish from the last hour nobody announced (intake's `arm_pending` replacing
  one).
- **An empty `command`** (a synced plan's lenient normalisation lets one
  through) fails its step at start with "no command".
- **Errors** are `{"error": "<word>", "detail": "<sentence>"}` (plan
  validation keeps `{"error", "field"}`).
- **`GET /api/flights/kinds`** also returns `on_fail: [halt, agent, skip, you]`.
  The `you` step's `items` field is a `textarea` (one item per line); the
  stored config is a list either way.
- **Row `flight`** also carries `reason` (why it halted).
- **Event data** also carries `detail` (the human sentence the ntfy body
  uses). `flight.needs_you` is said once per (step, fix round); a `you` step
  with `notify: false` still parks the flight and makes the bell row, but
  emits no `flight.needs_you`.
- **Bell row** (`GET /api/outbox` `waiting`): `kind: "flight_you"`, `key:
  "flight::<id>::<sid>"`, `reason`/`text` = what to do, `actions: ["done",
  "skip", "open"]`, `flight: {id, plan_name, step, step_title, kind, items}`.
- **Depth ladders are virtual plans.** `flights.plan_for_depth(kind, depth)`
  returns `builtin:<kind>@<depth>` for every ship depth (e.g.
  `builtin:ticket@pr`, `builtin:ticket@merge`); `get_plan` and `/start`
  resolve it to "Fast-track to <rung>": the built-in's first agent step (as
  edited) then the ship steps up to the depth (`ask_first` false). It is never
  listed and can't be saved. An issue uses `builtin:ticket`'s agent step. The
  built-ins themselves are unchanged.
- **Sleeping sessions don't hold a flight.** The sleep sweep only stops an
  idle agent, so a sleeping session counts as idle for the settle gate, and a
  step that types into it wakes it first (`sleep.ensure_awake`). Paused /
  missing / provisioning sessions are visible waits as written.
- **`agent` step with an empty prompt** sends nothing and is done when the
  current turn settles (no work evidence needed). `fresh: true` creates a new
  session (plan `session` defaults, the flight's repo) and moves the flight to
  it.
- **`ticket` step** whose source is not configured on this machine is
  skipped (not failed); a provider error fails it. `state` is what a person
  types — the state's exact id, or its name / unqualified label (any case) —
  resolved through the provider's `list_states()` to the id `set_state`
  takes; no match fails the step naming the states the tracker has. With no
  `state` it only comments (there is no per-source done state, so the
  built-in's last step is "Ticket: comment").
- **`command` step** runs `/bin/sh -c` in the session's folder with the
  server's environment plus `MINDFLOCK_FLIGHT` and `MINDFLOCK_SESSION_TITLE`;
  a server restart mid-command runs it again.
- **`reviews` step**: comments already on the PR when the step starts are
  treated as seen; each batch of new ones goes to the agent as the pipeline's
  consolidated prompt (which leaves the changes uncommitted), then a `push`
  lane is armed. `include_bots: false` drops `*[bot]` and CodeRabbit authors.
- **`checks` step**: `none` (nothing reports) counts as passed only after 2
  minutes; the failing names come from the check-runs API.
- **`verify` step** asks for the checklist through Verify's own manual path
  (`_ensure_test_plan(title, manual=True)`) and starts the agent's run through
  the `POST /api/test-plans/{id}/run` route body. `who: agent` is done once
  the agent's items are answered (human items stay on the checklist); open
  agent items left after a run become yours under `both`.

- **Steps changed in the air.** The four step routes are pure operations
  (`flights.op_add_step` / `op_edit_step` / `op_remove_step` /
  `op_move_step`) run through the
  store's `mutate`, checked against the flight *as stored* under the lock (a
  driver pass that moved the cursor meanwhile wins and the route gets its
  409); each emits `flight.changed` (even when the cursor did not move) and
  wakes the driver. A step "still ahead" is one after the cursor whose state is
  `pending` with nothing recorded on it — a later step already marked
  `skipped` is not. PATCH's `config` **replaces** the step's config (send the
  whole thing, as the plan editor does); `kind` and `id` never change; an
  empty `title` falls back to the kind's default. The new step's id is one
  past the highest `sN` the flight's steps *or history* name, so a removed
  step's id is never handed out again. `at` is 400 `bad_at` when it is not an
  index `0..len(steps)`; a flight already at 40 steps is 409 `too_many_steps`.
  A bad step is 400 `{"error": "bad_step", "detail", "field"}` with `field`
  like `step.config.lane` (`steps[i]…` on `/start`). The planner side: an
  `apply(finish)` that meets a current step (one added after the pass read the
  flight) advances instead of landing. A move never touches the current
  step or anything before it (both the step and its new place are after the
  cursor), so the planner, which reads `steps[cursor]`, carries on where it
  was; a `join` of `spawned` and a `join`'s `since` follow the new order (the
  nearest `split` / the step before it *after* the move).
- **Absorbing a fast-track.** The stage is read the way the autopilot driver
  reads it (`server._session_stage(inst)`, uncached), else the published row's
  `stage`. The absorbed steps go where the flight *starts* — after an attach's
  skipped agent step(s), before the plan's own steps from there — and the
  flight starts on the first of them; their ids follow the plan's (`s3, s4`
  after `s1, s2`; for a steps-only flight the given steps are `s1..sN` and the
  absorbed ones follow on, though they fly first). `ask_first` lands on the
  one step the run would have asked before — the commit for a commit lane,
  else the push — and only while that ask is still ahead (the record held
  short of its lane), so a run that asks once still asks once. The run's
  commit `message` rides along as the steps' `message` unless it was a
  placeholder (`message_auto` / `message_written`). A run whose rungs are all
  behind it (or a `leave` run) is still disarmed, with nothing absorbed. The
  history gets one `added` event on the first absorbed step ("taken over from
  Fast-track: Push → Open PR", plus "(it had stopped: <reason>)" for a halted
  run). The disarm happens after the flight is stored and skips a record the
  flight's own first step has armed by then.

### Fan-out and join

The owner: *"if an agent spawns multiple agents in its flow it should have 5
branches in the flow and then they converge at the next step … have process
join only when they all are done. agents can spin up and down."*

- **The set.** An `agent` step with `join` (and a `join` of `all`) waits on
  every live child of the flight's session (`lineage.children_of`, stored
  `Parent == title`), hidden helpers (`hidden_by`, Verify's sessions) left
  out. It is re-read on every pass: a worker spawned mid-step joins the set
  (nothing resets), a deleted one leaves it. A `join` of `spawned` waits on
  the titles the nearest `split` before it recorded; one deleted since reads
  `gone`.
- **Finished** (`flights.child_state`, pure): a `kind == "result"` report
  (`thread.last_report`) newer than the step's `since` — `failed` when its
  status says so; or idle-settled by `wait_for_session`'s own gates (idle ≥
  8 s for claude / 30 s for other CLIs, nothing queued, no waiting background
  job, no message pending for it, not held by worker order, and at least 60 s
  old); or offline ≥ 30 s (asleep counts as idle); or closed (`paused`) /
  deleted (`gone`). A worker in a dialog (`clarify`), or whose newest report
  is `blocked`, is `asking` and holds the join with a note ("worker X is
  asking") until it works again. A report followed by new work is superseded.
  A `join` step counts reports from the `since` of the step before it (the
  agent or split that set the workers going).
- **The agent step** is done only when the agent has settled *and* no worker
  is open. Workers' reports are typed into the agent, so the agent waits while
  a message for it is still pending, and once the last worker finished after
  the agent went idle it gets one more settle window to pick the report up. A
  `failed` worker counts as finished here — the agent decides what to do.
- **Detail.** The step's `detail` carries `{waiting, total, children: [{title,
  state: working|asking|done|failed|gone, report?: {id, status, summary,
  ts}}]}`, written only when it changed; an agent step with no workers writes
  none. The row's `flight.join` mirrors `{waiting, total}`.
- **The split executor** builds `spawn_session`'s payload server-side
  (`flight_driver._split_base`): `parent` = the session, `spawned`, the
  session's CLI, `repo_path` = its canonical repo with `base_ref` = its HEAD
  and `base_branch` (a provisioned session's workers are provisioned from the
  same source instead), `profile_model`, the fence (`fence_only` kept inside
  the session's own fence, as `spawn_session` does), titles `<slug>-w<N>` (the
  lowest free one: no live or starting title, tmux name or leftover branch),
  and the report footer when `report_back` and the MCP is attached to the
  workers' CLI. The workers go through `session_create.create_result`, so the
  spawn limits and the memory guard are the create path's own; the children
  cap is also checked for the whole split before the first create. A refusal
  fails the step with its sentence ("… (after 1 of 3 workers)"), keeping the
  workers already made in `detail.children`; a split run again (Retry, or a
  restart mid-spawn) adopts its live `<slug>-w<N>` children made since take-off
  that no other split claims, and spawns only the rest.
- **Worker rows.** A worker of a flying parent with no flight of its own gets a
  branch-only `flight` — `{id: "", state: "", step: null, total: 0, join:
  null, branch, plan_name: <the parent's>}` — so `flight.branch` is always
  there to draw; `isLive` never matches it.
- **MCP.** `list_sessions`, `get_session` and `whoami` rows carry the row's
  `flight` (docs/mcp.md "Flights and workers"). No new tools.

### Hand-off hooks for the integration work

- `flights.arm_pending(title, plan_id, subject, by="intake", replaced=None) -> Flight` —
  a `running` flight on `title` before the session exists, its first agent
  step recorded as already running (the intake prompt is that step). Raises
  `flights.FlightError` (`unknown_plan` 404, `in_flight` 409). A halted flight
  it replaces on that title is appended to `replaced` (pass a list to
  announce it right away; otherwise the driver's next pass does).
- `flights.plan_for_depth(kind, depth) -> str` — see "Depth cuts" above;
  `""` for `off`/`agent`.
- `flight_driver.on_session_created(title)` — already subscribed to
  `session.created`; attaches the pending flight (repo root, gone clock) and
  wakes the loop.
- `POST /api/flights/start` with a ticket/PR/issue `subject` (`{kind, source,
  id}`) runs that kind's Intake start body (`ticket_start.launch` /
  `pr_review.launch` / `issue_start.launch`) with `plan` = `plan_id` and
  answers 202 `{flight, title}`. The Intake routes' `plan` resolution lives in
  `server._intake_plan` / `_arm_intake` (the pipeline's twin is
  `SessionRunner._arm` / `_plan_for`).
- `flight_driver.start` records whatever `subject` it is given (the 501
  `not_wired` branch is gone); routing a ticket/PR/issue subject through the
  Intake start paths is the route's job.

## Where it lives

| What | Where |
|---|---|
| Store, pure planner, step registry, built-ins | `backend/web/core/flights.py` |
| Driver loop, step executors, bus taps, events | `backend/web/core/flight_driver.py` |
| Routes | `backend/web/routes/flights.py` |
| Describe-a-step one-shot | `backend/web/core/flight_describe.py` |
| Intake / schedule / verify hooks | `ticket_start.launch`, `pr_review.launch`, `issue_start.launch`, `server.py` (`_intake_plan`, `_arm_intake`, the result route's `session.test_plan_settled`), `ticket_ingestion/session_runner.py` (`_arm`), `template_schedule.py` (`tick_flights`), `pending.py` (the provisioning row's `flight`) |
| Frontend | `frontend/src/components/flights/` (SkyView + SkyDrawer = the sky view, with SkyLine / SkyFlock / FlywayShelf / FlywayCard, IntakeLane, NewSessionBlock, FlywayCanvas, SessionFlight, useSessionLine, FlywayInspector = the card's trigger, StopParts, controls, Bird, useWaiting; CSS `Flights.css` + `FlywayInspector.css`), `frontend/src/lib/flights.ts`, `flightDiagram.ts` (the canvas's model, the session diagram and `birdPerch`), `skyLayout.ts` (the sky's placement, camera, views, layout store), `viewMode.ts` (grid ⇄ sky), `flightsApi.ts`, `tours/sky.ts`; Schedule's scheduled flyways in `components/dialogs/TemplatesDialog.tsx`; the pane's Flight tab in `components/grid/Pane.tsx` |
| Tests | `tests/unit/test_flights*.py`, `frontend/src/__tests__/flights*.test.ts`, `flightDiagram.test.ts` |
| CLI | `mindflock flights` (list), `mindflock flight flyways` (alias `plans`), `mindflock flight start <flyway> [--repo …] [--title …]`, `mindflock flight done\|skip\|cancel <id>` (`backend/cli.py`) |


## The Sky: the intake lane and the new-session block

Two pieces of **the Sky**, the main area's canvas view where every session
is a line and work starts from the white space. The owner's ask:

> "I want to be able to see all agents/sessions that exist and be able to
> start and build agent sessions using this white space. … The ticket
> ingestion is an example of this sort of flow already i just want it
> formalized into the block diagram thing"

- **The intake lane** (`components/flights/IntakeLane.tsx`) draws the ticket
  ingestion pipeline as blocks, pinned to the sky's left edge.
- **The new-session block** (`components/flights/NewSessionBlock.tsx`) is the
  card that opens where you double-click empty sky.

Words as in [flights.md](flights.md): a *flyway* is a route, a *flight* is one
run of it bound to a session, a *stop* is one box, a *perch* is a stop that
waits on you.

## The intake lane

### The pipeline, as blocks

The ingestion pipeline ([ingestion-pipeline.md](ingestion-pipeline.md), "Story
flow") does four things for every item: **poll** a source, decide whether the
item is **eligible**, **fly** it (start a session for it), and from then on
it is a **session**. The lane draws each stage where you can see it:

| Pipeline | On the lane |
|---|---|
| **Poll** — a ticket source, a PR repo monitor, an issue repo monitor | one **source block** each, with an auto-start chip saying whether it polls by itself and how often |
| **Eligible** — the server's `eligible` (the pipeline's own skip filters) and no session yet | the block's **waiting items**: the same list as the Board's *Waiting to fly* (`useWaiting.ts`, `intake/queue.ts collectQueued`), oldest first |
| **Fly** — `POST /api/tickets/start`, `/api/github/prs/review`, `/api/github/issues/start` | each waiting item's **Fly** button: the Board's own request (`flightsApi.flyRequest`), with the source's flyway |
| **Session** — the started session | an **arrow stub** out of the block, labelled with the session; the Sky draws the arrow on to that session's line |

So a ticket moves left to right: it appears as a chip in its source's block,
leaves the block when it takes off (by Fly or by the auto-start sweep), and
from then on hangs off the block as a stub pointing at its line.

### A source block

- **The kind badge** — Ticket / PR / Issue, the badges *Waiting to fly* uses.
- **The name** — a ticket source as *provider · label* ("Shortcut ·
  quickpay"; the provider alone when there is no label or the label already
  names it), a repo monitor as "PRs · owner/name" / "Issues · owner/name".
- **The auto-start chip** — "auto-start on · every 20 s" or "manual". *On*
  is the Board's rule (`flights.ts waitingAuto`, from `GET
  /api/mindflock/status`): tickets when the pipeline runs, PRs and issues on
  their own switches. The interval is the source's `poll_interval_seconds`
  (tickets) or `github.poll_interval_seconds` / `issue_poll_interval_seconds`,
  falling back to the pipeline's defaults (20 s / 60 s / 60 s).
- **The flyway chip** — the source's own flyway (`TicketingSource.plan`,
  `github.repo_settings[repo].plan`, `github.issue_repo_settings[repo].plan`,
  highlighted) or else the kind's built-in (`builtinPlanFor`).
- **Waiting items** — up to six chips (reference + headline + **Fly**), then
  "n more". A source with nothing waiting says *Nothing waiting*. A refused
  Fly shows the server's sentence under its chip.
- **⋯** — opens the source in Intake
  (`openDialogFor("intake", "<kind>:<id>")`).

An item from a source the settings document does not name (an older server,
a source removed between polls) still gets a block, so nothing waiting is ever
hidden.

Folded, the lane is a rail of one icon per source (T / PR / I) with its
waiting count; the fold is remembered per browser.

### Arrow semantics

Each started session is an `<li class="sky-lane-stub"
data-lane-target="<title>">` at the right edge of its block. The lane draws
only the stub; **the Sky** finds the stubs by that attribute and draws the
arrow from each to the line of the session with that title. A block's
started sessions are the live rows (`/api/instances`, not hidden by Verify,
not a guest's locked row) that:

1. the panel names for one of the block's items — every ticket / PR / issue
   row carries the `session` title it starts as (the server's
   `session_title`), so a row with that title is its session; or, for items
   the panel no longer lists,
2. carry the title the server gives that kind — `pr-<repo name>-<n>` /
   `issue-<repo name>-<n>` (`pr_slug` / `issue_slug`) — or, for a ticket, a
   ticket-slug title on a branch cut from `<source id>-…`.

A row belongs to one block at most. A session another of your devices started
matches under its bare title and keeps its `<device>::` title on the stub.

### Where it lives

- `lib/skyIntake.ts` — `sourceBlocks(settings, waiting, rows, {plans, status,
  providers, panels})`: the blocks, their counts, default flyways and started
  sessions; pure, tested in `__tests__/skyIntake.test.ts`.
- `components/flights/IntakeLane.tsx` — `IntakeLane({rows, plans, onFly,
  width = 260})`; `flyLaneItem` is the Fly request. After a start, `onFly`
  gets the title the start returned.

## The new-session block

Double-click empty sky and a card opens at that spot:

- **Repository** — the New dialog's suggestions (`/api/repos/suggest`,
  recent first) as chips, and a path field with **Browse…** (the shared
  folder picker window). The first suggestion is preselected, as in the New dialog. Fly is
  disabled with no repository, or with a bare name rather than a path.
- **Task** — the prompt box; **Enter** flies, **Shift+Enter** is a new line.
- **Flyway** — **No flyway** (the default), then the built-in flyways, then
  yours.
- **Agent** — chips, only when more than one CLI is installed; your default
  is preselected.
- **Fly** and **Cancel**; **Escape** cancels too.

Fly builds the create body with the New dialog's own builder
(`newSessionSubmit(…).buildBody`, so the prompt, worktree rules and the
agent's default launch flags are what the dialog would send), posts `POST
/api/instances`, and, when a flyway was picked, `POST /api/flights/start
{plan_id, title}`. Then it calls `onCreated(title)`. A refusal stays in the
card; if the session was created but the flyway refused to take off, the card
says so and its button becomes **Open session**.


## A line in the Sky (SkyLine)

> Source notes from the round-9 worker:
> `frontend/src/components/flights/SkyLine.tsx` (+ `SkyLine.css`), the
> `size` / `openBox` / `dropLabel` / `detailOf` props of `FlywayCanvas`, and
> `Pane`'s `embedded` prop. Tests: `frontend/src/__tests__/skyLine.test.ts`.

The owner's ask: "make boxes smaller and bigger to reveal more text … click
on a box and see a claude code session there working or a terminal … an
isomorphism really of the normal flow." A line is one session drawn as its
diagram, and any session box on it opens into the very pane the grid shows.

## Anatomy

```
┌ 🐦 me  [Working]  📌  ◧  ⋯ ───────────────────────────────┐  ← title bar (drag = move the line)
│  [session] → [stop] → [stop] → … → ( + Tack on a stop )    │  ← FlywayCanvas, live mode, the bird
│     └─ [worker]                                            │
└────────────────────────────────────────────────────────────┘
```

- **Title bar** — the bird, the session's name, its state chip
  (`deriveSessionState`), 📌 when the line is pinned (`layout.pinned`: the
  host's auto-layout leaves it where it was put), **◧** the size control, and
  **⋯**: Open session (`selectSession`; the host switches to the grid), Show
  only this line (`openDialogFor("flights", "sky/<title>")`), Fly a flyway…
  (opens the +'s palette), Close session. A press on the bar (not on a
  button) calls the host's `onDragStart`; the host moves the line.
- **The diagram** — `sessionDiagram(rows, title)` with the family (parent
  above, workers below), the bird from `birdPerch`, and everything
  `useSessionLine` gives a line: the + palette, the + between pending stops,
  the stop inspector, a live flight's ⋯ and perch parts in its boxes. The
  palette/inspector hang under the line (they don't count toward its size).
- **Focus** — a press anywhere on the line calls `onFocus`; `focused` draws
  the ring.
- **A flyway chip dragged over** (`dropFlyway`) — the + lights up and reads
  "Drop to fly <name>" (the host handles the drop).

## Box sizes (◧)

◧ cycles **compact → normal → wide** through `onLayout({size})`; the host
remembers it per line. `FlywayCanvas`'s `size` prop does the drawing (default
normal, so the Flyways editor and the pane's Flight tab are unchanged):

| size    | box         | shows |
|---------|-------------|-------|
| compact | 110 × 62 px | the title alone, one line (a session/worker box: a tone dot for its state); the + is a bare circle |
| normal  | 178 × 76 px | today's box: icon, title, state chip, the config summary, a halted reason |
| wide    | 260 px, taller | the full title (two lines), the summary (two lines) and a **detail line**: the agent stop's prompt (first line), a perch's text (or its items), a ship stop's commit message, a command, a ping's text; on a session box, what it was last asked |

The layout grows a wide box only by what it shows, so connectors stay
straight. Detail comes from `boxDetail(node)` (the stop's own config) and the
canvas's `detailOf` (the line passes a session's `last_prompt`).

## The open card = the pane

Pressing the **session box** toggles `onLayout({open})`. Open, the box grows
into a card of `layout.paneW × paneH` plane px (default **720 × 460**, floor
**360 × 240**) under a slim head (the name, **▣** to fold it back into a
box). Inside is the grid's own `Pane` for that title with `embedded`:

- dropped: the grip, the drag wiring, hide −; a press focuses the session
  (`setFocused`) instead of `selectSession`, so the grid behind the sky is
  not rearranged;
- kept: the header with its tab strip (Agent · Terminal · Diff · Queue · Map ·
  Flight · Thread) and every tab body. The tab is the same per-title store key
  (`lastTab[title]`) the grid uses, so switching views keeps it; the
  terminals are the registry's (`getTerm`) — typing works as in the grid.
- one pane per title holds the terminals: while a card is open, the grid's
  pane for the same session leaves them alone (`holdTerms` in Pane.tsx — its
  ref re-runs every render and would otherwise pull them back), and adopts
  them again the moment the card closes. The card's Pane is keyed by the
  title.

The card's corner (**⤡**) resizes it: pointer travel ÷ `zoom` = plane px,
drawn live, persisted once on release (`onLayout({paneW, paneH})`). The card
is a node of variable size in `FlywayCanvas` (`openBox`): it starts at the
size it asks for and is then measured (ResizeObserver, layout px), so the
connectors and the boxes after it follow the card. Wheel inside the card
scrolls the pane (`sky-own-scroll`), not the sky; clicks inside it never fold
it (only the box itself, Enter on it, or ▣ do).

## Workers

A **worker's box** opens the same way. The host keeps one layout per line,
so a worker's open state (and its card size) lives in the line itself, keyed
by the worker's title — not persisted; a worker that leaves the family takes
its card with it. The host sees it as the line growing (`onMeasure`).

## What the host provides

`SkyLine({title, rows, plans, layout, onLayout, focused, onFocus, zoom,
onDragStart?, onMeasure?, dropFlyway?})`. The line renders in flow
(`position: relative; width: max-content`), so the host wraps it in its
absolutely positioned box at `layout.x/y`. `onMeasure(w, h)` reports the
line's size in plane px (offset sizes — the zoom is a transform) whenever it
changes: a size change, a card opening, closing or being resized.

## Known limits

- xterm under the sky's CSS `scale()`: typing and scrolling work; a mouse
  selection in the terminal is off by the zoom factor away from 100%.
- An open card's terminal is fitted to the card (tmux resizes); back in the
  grid it refits to the grid's pane.


## The Sky: birds, the flyway shelf, flyways made on the sky

Round 7 of the Sky (to fold into `docs/flights.md`). Owner's ask: the
MindFlock birds flying instead of a drawn one, no bird on the pane's Flight
tab, and flyways made from the Sky itself.

## The birds

**One bird everywhere: the MindFlock raven.** `components/flights/Bird.tsx`
draws `backend/web/static/bird.png` — the sprite the app's flock
(`lib/flock.ts`) and the website's hero fly — instead of the old hand-drawn
gull. The sprite is a single 216×160 frame with white stripped to alpha, so it
is used as a **CSS mask over `currentColor`**: the bird takes the colour of
wherever it sits, exactly as the old stroke glyph did. No new assets, no
canvas per glyph.

- `Bird {size?, className?}` — the still glyph (~16px) for the top bar, the
  pane strip, the Sky's line bars, the Flyways editor. Still an
  `<svg class="fl-bird">` element (callers and their CSS are unchanged); the
  mask is its inline style.
- `BirdMarker {tone, x, y, label, size?}` — the bird standing on a line's
  active box (`FlywayCanvas`'s `bird`). ~24px, drawn as two masked bands of the
  sprite split at the wing hinge (56%, flock.ts's `HINGE`): the body below,
  the wings above. **While the flock works the wings beat** — the wing band
  scales about its bottom edge (the hinge), full spread down to a quarter,
  about once a second, the same beat flock.ts paints. Perched on you it sits
  still and gold, halted red, paused grey (the tints are FlywayCanvas.css's
  `.fl-bird-marker.tone-*`). The hop to the next box is still the
  `left`/`top` transition. Reduced motion: no flapping (no `fl-flapping`
  class, and the stylesheet turns the animation off as well).
- Pure helpers, pinned by `__tests__/skyBirds.test.ts`: `spriteBox(width)`
  (height, wing band, body band), `spriteMask(size, position)`,
  `birdLook(tone, reduced)` (the marker class, the sprite class, flaps or not),
  `prefersReducedMotion()`.

**The ambient flock** — `SkyFlock {width, height, zoom, paused?, density?}`
(`components/flights/SkyFlock.tsx`): a `<canvas>` the Sky mounts behind its
plane with a few birds crossing the empty sky — ~8, times `density` (capped
at 40). Same sprite (flock.ts's `tinted()`), same 12-species plumage (the dark
column, the light one in the light theme), same wing beat and belly-up mirror
as the app's flock. They flock casually, each heads for a wandering target,
they **ease out of the middle of the sky** where the lines are, and they wrap
at the edges so they cross the sky rather than turn back. Bird size follows
the Sky's zoom a little (√zoom, 0.7–1.3).

Cheap by construction: one rAF, at most 60 frames a second (a 120 Hz frame is
skipped), no work while the tab is hidden, `paused` asks for no frame at all
(the last one stays painted), DPR-aware (capped at 2). Under
`prefers-reduced-motion` it renders **nothing**. It is decoration:
`pointer-events: none`, 0.55 opacity (0.32 in the light theme). The simulation
(`flockSize`, `seedBirds`, `fitBirds`, `stepBirds`) and the frame loop
(`skyLoop`) are pure and tested without a DOM.

**The pane's Flight tab** (`components/grid/TabIcon.tsx`) is a route now —
three stops joined by arrows, like the connectors — not a bird.

## The flyway shelf

`FlywayShelf {plans, onNew, onFly, onEdit, collapsed?, onCollapse?}`: the saved
flyways as a strip of chips along the bottom of the Sky, built-ins first. A
chip shows the flyway's name and a tiny row of stop dots (a perch's dot gold;
past ten, "+N").

- **Click** the chip → `onEdit(plan)` (the host opens a card on it).
- **Fly** → `onFly(plan)` (the host asks which line).
- **Drag** the chip onto a line: it carries the flyway's id as
  `application/x-mindflock-flyway` (and its name as `text/plain`); the host
  owns the drop targets. `FLYWAY_MIME` and `flywayDragData(plan)` are exported.
- **+ New flyway** → `onNew()`.
- The head folds the strip away; pass `collapsed` / `onCollapse` to remember
  it (without them the shelf keeps it itself).

## The flyway card

`FlywayCard {x, y, plans, initial?, onSaved, onCancel}`: a flyway made or
edited on the sky, floating at (x, y) (in the list on a phone). Since round 8
it is THE flyway editor (the Flights dialog's Flyways tab is retired):

- the **name** (a fresh "New flyway", "New flyway 2"… that the shelf doesn't
  have yet, unless `initial` names it);
- the stops on `FlywayCanvas` in editor mode — drag to reorder, ← → / Delete,
  `+` between boxes or at the end for the stop palette — with the selected
  stop in the shared `StopInspector` (Earlier / Later / Duplicate / Remove)
  and **Describe a stop…** under the canvas (all from `StopParts.tsx`);
- **Takes off: <how>** — the trigger and, for by-hand and scheduled flyways,
  the session defaults (`FlywayTrigger` in `FlywayInspector.tsx`), folded to
  that line; open from the start on a scheduled flyway;
- its **⋯** — **Reset to default** on a built-in (`POST
  /api/flights/plans/{id}/reset`, the card reloads on it) or **Delete
  flyway…** on one of yours (asked inline; `DELETE /api/flights/plans/{id}`,
  then the card closes); a new, unsaved card has no ⋯;
- **Save & fly on…** — saves, then `onSaved(plan)`: the host offers the line
  picker and flies it (`startFlightOn(title, planId)` in `lib/flightsApi.ts`);
- **Save** — `POST /api/flights/plans` (`savePlan`; an `id` in `initial`, or
  from a first save, makes it an update) and keeps the card open on the saved
  flyway;
- **Cancel** — with unsaved stops it asks inline (Discard / Keep editing; the
  desktop app has no `confirm()`); Escape does the same.

A refusal shows the server's sentence and selects the stop its `field` names.
A new card makes a manual flyway unless its trigger is changed. `saveCard(draft,
andFly, onSaved)` is the card's save, exported for the tests.

The host (`SkyView`) also opens the shelf — and a card — on request:
`useUi.showFlyways(planId?)` sets `skyShelf {plan, n}` and switches to the
sky; the sky opens the shelf, then (once the flyways have loaded) the card of
`plan` (`"new"` = a fresh one) and clears the request (`takeSkyShelf`).

## Save as flyway

A live line's ⋯ → **Save as flyway** opens a card seeded with
`planFromSteps(name, steps, trigger?)` (`lib/flights.ts`): a flyway body
(no `id` / `updated`) with only what each stop **is** — kind, title, config,
on_fail — re-numbered `s1..sN`, a deep copy of each config with the driver's
`_`-prefixed keys dropped, and a manual trigger unless one is passed. Runtime
state never rides along (`step_states`, `detail.performing`, the cursor live
beside the steps, not in them).

## Round 8: flat again

The owner, after round 7: *"i dont know how much i love sources (intake)
schedule and verify tabs within the flight thing … menus within menus within
menus can get very tricky very fast."* So:

- **The top bar is flat**: New · Grid / Sky · Intake · Verify · Schedule ·
  Settings — the three buttons with their own badges, tooltips, calm-hour
  rule (`lib/calm.ts` `topBarShows`: Verify after the first checklist,
  Schedule after the first template), fold order (Settings, Schedule, Verify,
  then the toggle) and shortcuts (`Alt+I` / `Alt+V` / `Alt+S`, the old ids).
  The Flights button is gone; the Sky toggle counts the flights perched on
  you (`perchBadge`).
- **The Flights dialog is retired** (`FlightsDialog.tsx`, `PlanEditor.tsx`,
  `FlightsDialog.css` deleted; the kit CSS lives on as `Flights.css`, the
  palette / inspector / trigger CSS as `FlywayInspector.css`). The store
  opens Intake / Verify / Schedule directly again (`"templates"` →
  Schedule). `"flights"` stays in `DialogName` only as an alias the store
  routes (`legacyFlightsRoute`; the table under [Contract: the UI](#contract-the-ui))
  — `SkyLine`'s **Show only this line** still calls
  `openDialogFor("flights", "sky/<title>")`, which lands on `#sky/<title>`.
- **Flyways live in the Sky**: the shelf lists, makes and flies them; the
  card edits, resets and deletes them and carries the trigger and session
  defaults that were the Flyways tab's.
- **Scheduled flyways show in Schedule** (`scheduledFlyways`,
  `flywayNextLine` in `TemplatesDialog.tsx`), with **Fly now** and **Edit in
  the Sky**; nothing scheduled, no section.
- **The drawer** keeps Perched on you / Waiting to fly / Landed; its
  checklist rows open Verify, its *Intake ›* opens Intake. The intake lane's
  ⋯ opens the source in Intake (`<kind>:<id>`, Intake's own deep link).
- **Tours**: `flights` became `sky` (toggle, a line, the bird, the shelf);
  Intake / Verify / Schedule walk their dialogs.


## Fan-out and join: branches on a session's line

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

## Room and the semantic zoom

> Source notes from the round-9 worker:
> the grid and the sky"). Source: `lib/skyLayout.ts` (the rules),
> `components/flights/SkyView.tsx` (the camera, the toolbar, the drawer),
> `SkyLine.tsx` + `SkyLine.css` (the level of detail),
> `Sky.css`, `IntakeLane.css`; tests in `skyZoom.test.ts`.

### Room on the screen

- **The drawer starts folded** to its vertical tab, which carries the count
  of what is perched on you and wears the attention tone while that count is
  above zero. A press opens it, and the choice is remembered per device
  (`mf_sky_drawer`: `"1"` open, `"0"` folded; `skyDrawerOpen(pref)`). A first
  visit no longer loses 360px to it.
- **The toolbar is one slim row** (36px at most): the views, Find, + New
  session, Tidy, the zoom group. Find gives up width first. When the main
  area is under 860px wide (a 1000px window beside the rail), the 100%
  button and the tour's ? go (the readout still shows the zoom) and the
  chips tighten. Under 640px the row wraps.
- **Spread out**: `SKY_GAP` 64 (was 36) between lines; the intake lane
  `INTAKE_W` 240 (was 300) with a 48px gap before the first line (`LINES_LEFT
  = SKY_MARGIN + INTAKE_W + 48`); a fit keeps `FIT_PAD` 56 (was 40) round
  what it shows. `placeLines` still flows rows newest first; **Tidy** (T)
  forgets the pins and re-places at the new gap.
- **Lines sit where they are placed**: each line's slot is translated to its
  `placeLines` spot (or where it was dropped). Before this, the slots sat in
  plain flow, one under the other, so the gap, Tidy and dragging never
  showed. Lines glide to a new spot. The one being dragged follows the
  pointer, and an open box lifts its line over its neighbours.

### Semantic zoom

**The rule: the geometry never depends on the zoom.** A box keeps the
layout's plane width and height, a line its measured size, the title bar its
34px. If they changed with the camera, the fit (which reads measured sizes)
would chase itself. What changes is what is drawn inside the same geometry
and the size of the type on screen.

`lodFor(z): Lod` (`"far" | "mid" | "near"`):

| Band | Zoom | A box shows | The type |
|---|---|---|---|
| **far** | below `LOD_MID` = 0.62 | the icon and the title (two lines, bold, ellipsis); no summary, chip, detail or in-box parts | counter-scaled: `--sky-far = clamp(1, 0.84 / z, 2.1)`, about 10.5px on screen down to the 40% floor; icons, glyphs and the bar's bird and ⋯ by `--sky-far-ui = clamp(1, 0.6 / z, 1.4)`; the ◧ and 📌 hidden (too small to aim at) |
| **mid** | 0.62 up to 1.15 | today's box | titles and the line's name only, by `--sky-mid = clamp(1, 0.8 / z, 1.3)`, so they read at about 10px from 62% up. That is the first picture's floor. From 80% they are at their normal size. |
| **near** | from `LOD_NEAR` = 1.15 | the title on two lines, the summary, the detail and the reason wrapping to `--sky-lines` lines each (`nearLines(z)` = ⌊2z⌋, from 2 to 8), the perch's checklist and buttons, and the session's task as a second row of the line's bar | counter-scaled back to about 12.5px on screen: `--sky-near = clamp(1, z, 3)`, text at `12.5px / --sky-near`, and padding, icons, chips and perch items `zoom: 1 / --sky-near`. A box at 200% holds about four times the words (twice per line, twice the lines). |

- `SkyLine` puts `data-lod`, `--sky-z` and `--sky-lines` on `.skl` and passes
  `lod` to `FlywayCanvas`. The canvas drops parts at far and adds them at
  near. Until the canvas's `lod` prop lands, it goes through a cast spread.
  The CSS hides the far-only parts too, so the bands hold either way.
- An open box (a card holding the session's pane) is never restyled.
- The intake lane has the same far band (`.sky-intake[data-lod]`): its
  sources' names and counts, counter-scaled; its item rows wait for mid. It
  keeps its 240px width.
- **The ◧ size control** (compact / normal / wide) stays a manual override
  of the geometry. The level of detail is about the camera. Both compose: a
  wide line seen near shows the most.
- `ZOOM_MAX` is 3 (was 2), since near has more to show; `ZOOM_MIN` stays
  0.4. The wheel, the pinch, + / − and the readout follow.

### The first picture

`firstFit(bounds, vw, vh, maxZ = 1, pad = FIT_PAD)`: if fitting everything
(`fitCamera`) is legible (zoom ≥ `LOD_MID`), it does that, centred. If not,
it fits the drawn content's width at no less than 0.62, anchored at the top
left: the intake lane and the newest lines are on screen, and the owner pans
or scrolls down to the rest. It drives the automatic fit (a first visit, a
new view, the lines changing shape until the owner moves the camera) and the
`0` key.

The toolbar's **Fit** is the true fit (`fitCamera`): everything in the
window, however small. Its tooltip says so, and says that `0` is the legible
fit.

## Halt points

The owner: "I want to be able to halt steps whenever." Two ways:

- **Halt now** stops the flight where it is: the pause route
  (`POST /api/flights/{id}/pause`), on the current stop's hover toolbar and
  right-click menu (⏹ Halt now while it runs or waits on you, ▶ Carry on
  while paused) and in the pane strip's ⋯ (Halt now / Carry on / Cancel).
- **Halt here** marks a stop not yet done as a **halt point** (`halt: true`
  on the step): the flight pauses when it *reaches* it, before doing it,
  like a breakpoint. ⏸ Halt here on the stop's toolbar and menu sets it;
  Carry on past here clears it. The box wears a pause badge in its corner
  while it is set.

The planner: when the cursor lands on a pending step with `halt` (`_advance`
after a step finishes, or `next_action`'s `hold` verdict for a flight that
takes off on one, is jumped onto, or gets one set on its current pending
step), the flight goes `paused` with `reason: "halt point"`, history
`halted_at` ("halted before <title>") and `step_states[sid].detail.halt_hit
= true`, before its executor starts. `halt_hit` lets it through after that:
Resume carries on into the step and does not stop there again; a retry keeps
it. Any "go" on the current step counts as passing its halt point (Resume,
Retry, a Jump onto it); a jump back re-arms the halt points after its target.
Done / Skip on a paused flight that lands on a halt point keeps it paused
there.

Marking: `PATCH /api/flights/{id}/steps/{sid}` with `{"halt": true|false}`
on any stop not yet done — a pending one ahead, or the current one in any
state, on a flight running, waiting, paused or halted. Clearing the halt on
the current stop while paused at it un-marks it without resuming (the
flight stays paused; Carry on resumes). A stop behind the flight is 409
`in_progress`; a non-bool is 400 `bad_step` (`field: "step.halt"`).
`title` / `config` / `on_fail` keep the pending-only rule. In the flyway
editor the toggle edits the draft (`toggleHalt`, lib/flights.ts) and Save
stores it with the plan.

The UI (pure rules in `lib/stopEditing.ts`: `canHalt`, `haltToggle`,
`flightHold`, `haltMark`; tested in `stopEditing.test.ts`): `FlywayCanvas`
takes `onToggleHalt?(n)`. With it, every stop not yet flown that can change,
and the flight's current stop, offers ⏸ Halt here (pressed, `aria-pressed`,
reading Carry on past here on a halt point) on its toolbar and menu; the
current stop's own toolbar (⏹ Halt now / ▶ Carry on, then ⏸) shows only on
such a line, so a line without it asks for no headroom. The current stop's
right-click menu offers Halt now / Carry on on any live line
(`controlFlight`). A halt point wears `.fl-halt-point` and a ⏸ badge
(`.fl-halt-mark`, top-left, the paused tone; `StopEditing.css`).
`useSessionLine` wires `onToggleHalt` to `flightsApi.setHalt(flightId,
stepId, !halt)`. The pane strip says "Paused at a halt point" for one, and
its ⋯ reads Halt now / Carry on / Cancel.

## Editing stops on a line

The owner: "In sky mode I can't drag in new boxes or delete boxes from flows …
boxes should be easier to delete or alter." The stops on a line now edit the
way n8n's nodes do: no ports, no manual wiring, the order is still the
sequence. Every gesture works on a session's line (a line in the Sky, the
pane's Flight tab: `useSessionLine`) and in the flyway editor (`FlywayCard`)
through the same `FlywayCanvas` props. The rules are pure, in
`frontend/src/lib/stopEditing.ts`, and tested in
`frontend/src/__tests__/stopEditing.test.ts`.

### The gestures

| Gesture | What it does | Canvas prop |
|---|---|---|
| Hover a stop (or Tab to it) | A toolbar floats above it: **✎ Edit · ⇤ Earlier · ⇥ Later · ⧉ Duplicate · 🗑 Remove**. It stays a moment after the pointer leaves so you can reach it. | `onSelect`, `onMove`, `onDuplicate`, `onRemove` |
| Right-click any box | A menu at the pointer (`StopMenu`, `role="menu"`, ↑ ↓ Home End, Enter, Esc / Tab / a press elsewhere closes it) | — |
| Delete / Backspace on a selected stop | Removes it | `onRemove` |
| ← / → (↑ / ↓ on a phone) on a selected stop | Moves it one place | `onMove` |
| Drag a stop along the line | Moves it there (never before the flight's current stop) | `onMove` |
| The + between two boxes (shows on hover of the whole line) | The stop palette, to put one in there | `onInsert` |
| The trailing + | Tack on a stop (palette and "Fly a flyway") | `onOpen` |
| ⏸ on a stop's toolbar or menu | Halt here / Carry on past here: a halt point ([Halt points](#halt-points)) | `onToggleHalt` |
| ⏹ / ▶ on the current stop's toolbar or menu | Halt now / Carry on (pause / resume the flight) | — (`controlFlight`) |
| Drag a stop chip from the shelf onto the line | The gap it would land in lights up ("+ Tack on here"), or the end + reads "Tack on <Kind>"; dropping tacks the stop on there | `onDropStop`, `dragStop` |

Only a stop that can change (`DiagramNode.editable`: in the editor every
stop; in a live flight the pending stops after the current one) wears the
toolbar, drags, or takes Earlier / Later / Duplicate / Remove. ⇤ / ⇥ are
disabled at the ends and next to a stop that can't change (`canShift`).

**The right-click menu, by box** (`menuItemsFor`):

- a stop that can change: Edit, Earlier, Later, Duplicate, Remove, Halt here
  / Carry on past here (only the ones the host handles);
- the flight's current stop: what its own parts offer (Done on a perch,
  Retry when halted, Skip; the same `stepDone` / `stepRetry` / `stepSkip`
  routes), Halt now / Carry on, Halt here / Carry on past here, and a muted
  line: "The flight is here — it can't change now";
- a stop that has flown (done, skipped, failed): "This stop has flown";
- the trailing +: Tack on a stop; a session, parent, child or branch box:
  Open; a fast-track rung: a note that it moves as the session ships.

In the Sky the line is zoomed with a transform. The toolbar, the menu and
the gap + buttons scale themselves back by the host's `--sky-z` (set on
`.skl`; 1 everywhere else): `scale(calc(1 / var(--sky-z, 1)))`, so they stay
screen-sized at 50% or 200%. With a toolbar to show (or the bird) the stage
keeps 26px of headroom above the top row, so neither is clipped where the
canvas scrolls (the Flight tab, the editor).

### Remove is immediate and undoable

No confirm (the desktop app has no `window.confirm`). On a session's line
the toolbar, the menu, Delete and the inspector's **Remove stop** all go
through one path: `DELETE /api/flights/{id}/steps/{sid}`, then a toast
`Removed “<title>” from the line` with **Undo**. Undo tacks the same stop
(kind, title, config, on_fail) back: `POST /api/flights/{id}/steps {step,
at}` at its old index while that is still ahead of the flight (after its
current stop, not past the end; read from the flight as it is when Undo is
pressed), else at the end (`undoPayload`). In the flyway editor nothing is
saved until Save, so Remove there needs no undo.

### Duplicate

On a session's line: the same stop tacked on right after it (`POST
/api/flights/{id}/steps {step, at: index + 1}`, appended when it is the last
one). In the flyway editor: `duplicateStep` (lib/flights.ts), as the
inspector's Duplicate already did.

### The move route

`POST /api/flights/{id}/steps/{sid}/move` with body `{"at": <index in the
final list>}` answers `{flight}`. 409 `in_progress` when the stop, or the
place it would go, is not strictly after the current stop; 404
`unknown_flight` / `unknown_step`. The client is `flightsApi.moveStop(flightId,
stepId, at)` (through `flightAction`: a refusal opens the error card and the
line stays as it was, since nothing moves until the refetch). The canvas's
`onMove(from, to)` speaks indexes among the line's stops, which on a session
line are the flight's own; `useSessionLine` posts only for a stop that can
change and a `to` ≥ `diagram.insertFrom`.

### The drop contract

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

### Level of detail (`lod`)

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

### What the hosts pass

- `SkyLine` and `SessionFlight` spread `useSessionLine`'s `canvas`, which now
  carries `onMove`, `onDuplicate` and `onDropStop`: nothing to change there.
  For the end + to name the kind before the drop, the Sky passes the dragged
  chip's name down: `dragStop={…}` on the line's `FlywayCanvas`.
- `FlywayCard` (the flyway editor) already passes `onSelect`, `onMove`,
  `onRemove` and `onInsert`; for Duplicate and drops it adds
  `onDuplicate={(n) => setSteps(duplicateStep(draft.steps, nodeIndex(n)))}`
  and `onDropStop={(kind, at) => { const def = kindOf.get(kind); if (def)
  insert(at ?? draft.steps.length, def); }}`; for halt points it adds
  `onToggleHalt={(n) => setSteps(toggleHalt(draft.steps, nodeIndex(n)))}`.

## The shelf: flyways and stops

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

### The drag contract

A stop chip is `draggable`. On dragstart it sets (`stopDragData(def)`):

| type | value |
|---|---|
| `application/x-mindflock-stop` (`STOP_MIME`, `lib/flights.ts`) | the kind id, e.g. `ship` |
| `text/plain` | the name, e.g. `Ship` / `Perch` |

and `effectAllowed = "copy"`. The drag image is the chip. The sky owns the drop: the gap
the stop lands in on a line lights up and the stop is tacked on there (at the end when it
lands on the line but not in a gap).

### `onTack`

`FlywayShelf {…, onTack?(kind)}` passes it to `StopShelf`. A chip that is clicked (or
tapped on a phone, where nothing drags) calls `onTack(kind)`, and the host tacks that stop
onto the focused line. Without `onTack` a chip is drag-only: it is a plain span (not a
button), its cursor is grab and its tooltip says "Drag onto a line". On a phone
(`@media (hover: none)`) the "drag onto a line" hint under the row's label is hidden.

## A still sky

> Worker notes (round 10, `sky-stable`), to fold into docs/flights.md "The
> Sky" and "Room and the semantic zoom" at merge. Source:
> `lib/skyLayout.ts` (`assignSpots`, `NEW_SESSION_MIME`),
> `components/flights/SkyView.tsx`, `Sky.css`, `NewSessionBlock.tsx` +
> `.css`; tests in `skyStable.test.ts`.

The owner: "I DON'T like how the boxes move around. I also don't like that I
can't drag in a new session block." **Nothing on the sky moves unless the
owner moves it.**

### Where a line sits

- **A line gets a spot once.** When it first appears, after it has been drawn
  and measured (and the window, the sessions and the flights have loaded),
  `assignSpots(existing, newcomers, area)` gives it the first free place by
  the old rule (rows from the intake lane rightwards, newest first, wrapping
  at the sky's width), clear of every line already placed at that line's size
  now. That spot is kept in SkyView's `autoPos` and passed to the placement as
  saved, merged with the pins (`{ ...autoPos, ...pinnedPositions(layout) }`).
- **It keeps that spot** while it grows or shrinks: a flight loading, a perch
  opening wide, the hover toolbar, its box opening into a pane card. A
  neighbour arriving or leaving doesn't move it either.
- **Two lines may grow into each other.** The sky doesn't shove them apart.
  The focused or open line is lifted over the other (z-index), and the owner
  drags one away or presses Tidy.
- **A line that leaves frees its spot**: the session is closed, or it becomes
  a hidden helper. A line hidden by a filter (Find, Mine vs All) keeps its
  spot, and a newcomer keeps clear of it at its last measured size. A dragged
  line is pinned where it was dropped. Its auto spot is dropped, so "Tidy this
  line" places it afresh.
- **Tidy** (the button, `T`) forgets every auto spot and every pin. Every line
  is placed again, in rows. It is **the only glide**: SkyView sets
  `data-tidying` on `.sky-plane` for 350 ms (not under reduced motion), and
  only `.sky-plane[data-tidying] .sky-line-slot` has a `transform` transition.
  No other rule eases a line's or a slot's transform. A dragged line follows
  the pointer live.
- This session (one line) still sits at the plane's corner. It neither reads
  nor changes the auto spots.

### The camera fits once

The automatic fit (`firstFit`, or the focused line when the grid was on "1")
runs once, on the sky's first show, after the lines have their spots (drawn
and measured, or 0.9 s after the sessions load, whichever comes first). It
runs again only when the view changes (All / Mine / Perched / This session,
or This session's line). The effect's dependencies have no line sizes in
them. A line growing, a new session arriving, or the window resizing never
refits. A newcomer off screen is brought in by the rail's selection and the
flash, as before. `Fit`, `0`, `1` and the zoom buttons work on demand. Back
from the grid in the same view, the camera is where it was left.

### Dragging a new-session block in

- The toolbar's **+ New session** is draggable (`dataTransfer`
  `application/x-mindflock-new-session` = `"session"`, effect *copy*; tooltip
  "Click, or drag onto the sky, …"). Over empty sky the viewport washes
  softly (`.sky-viewport.is-new-over`) and takes the drop. Over a line it
  refuses. Dropped, the block opens at the drop point (plane coordinates
  through `toPlane`), as a double-click there would. A click, a double-click
  and Ctrl/Alt+N work as before.
- **The block is a block on the sky**: a card with a line's bar (the bird,
  "New session", × to cancel) and a dashed accent edge until it flies. In
  light and dark. **Drag it by its bar** (`onMove(pos)` in plane px; the sky's
  `zoom` turns pointer px into plane px). The session it makes is pinned
  where the block sat. Its fields are unchanged: repository chips and the
  path field, the task, the flyway pick, the agent.

## Readability: sizes, flown stops, the menus

### The Sky, readable

> Sky round 10, the readability sweep. To be folded into `docs/flights.md`
> under "The Sky" and "Room and the semantic zoom". Code:
> `frontend/src/lib/flightDiagram.ts` (`boxMetrics`, `sizedBoxFor`,
> `collapseFlown`, `drawnIndexes`, `lineWhere`), `components/flights/SkyLine.tsx`
> (+ `SkyLine.css`, `SkyVisibility.css`), `FlywayCanvas.css`, `IntakeLane.css`,
> `Bird.tsx`. Tests: `frontend/src/__tests__/skyVisibility.test.ts`.

The owner: "Everything is still just too hard to read." At the first fit (62%)
a stop's title was ~10px on screen and its summary ~7px; a 9-stop line was
~2000 plane px wide, so the sky zoomed out to fit it. Two levers: bigger boxes
and type in the sky, and shorter lines.

#### Sizes

The sky asks the canvas for a **roomy** tier (`LayoutOpts.roomy`,
`boxMetrics(compact, roomy)`). The pane's Flight tab and the flyway editor keep
the base tier. The geometry still never depends on the zoom.

| | base | roomy (the sky) |
|---|---|---|
| box | 178 × 76 | **236 × 100** |
| + (add) | 148 × 40 | **180 × 48** |
| branch | 156 × 52 | **200 × 64** |
| flown pill | 120 × 44 | **148 × 56** |
| gap / vgap / anchor | 46 / 30 / 38 | **56 / 36 / 50** |
| chip / branch sub / dots add | 18 / 16 / 14 | 22 / 20 / 16 |
| ◧ compact | 110 × 62 | 150 × 64 |
| ◧ wide | 260 wide | 330 wide |

`sizedBoxFor(size, node, base, detail, roomy)` is the ◧ sizing that
`FlywayCanvas`'s `sizedBox` used to hard-code. At the base tier it gives the
same numbers.

| Type in the sky | before | now |
|---|---|---|
| box title | 12.5px | **15px / 600** (a stop's title may take two lines) |
| summary, detail, halted reason | 11.5px | **13px** |
| chips | 10.5px | **12px** |
| icon tile | 22px | **26px** (17px glyph) |
| line bar | 34px, name 12.5px | **42px, name 15px, chip 12.5px** |
| where the flight is (new, in the bar) | — | **13px**, muted ink |
| ⋯ menu items | 12px, ~24px rows | **13.5px, 32px rows**, screen-sized at any zoom |
| bird marker | 24px | ×1.35 |
| intake lane cards | 11.5–12px | **13px**, the ticket id bold, Fly a 28px button, source heads 13px caps |
| drawer perch card | 12–13px | **14px** body, 15px name, **36px** Done / Skip / Retry; "Waiting to fly" alike |
| controls (ChipRow, Seg, Toggle, field labels) | 12–12.5px | **13px**, 28–30px tall (the toolbar's chips too) |

**The zoom bands, re-tuned for the 15px base** (`SkyLine.css`):

- **far** (< 62%): `--sky-far = clamp(1, 0.76 / z, 1.9)`. A title reads
  15 × 0.76 = **11.4px** on screen from 62% down to the 40% floor. The bar's
  where-line, ◧ and 📌 wait for mid.
- **mid**: `--sky-mid = clamp(1, 0.8 / z, 1.3)` now grows the titles **and**
  the summaries, chips, the join pill and the bar's where-line. At 62% a title
  is ~12px on screen and a summary ~10.4px (they were ~10px and ~7px).
- **near** (≥ 115%): counter-scaled back to 15px titles and 13px summaries on
  screen. Titles wrap to three lines, summaries to `--sky-lines`.

#### What has flown: one pill

`sessionDiagram` folds the stops a live flight has already flown into **one
node** (`collapseFlown`):

- **The rule.** Take the leading run of `done` / `skipped` stops, up to the
  first stop that is neither (the current one). If the run has
  **`FLOWN_MIN` = 2** or more stops, it becomes `{ id: "flown", kind: "flown",
  row: "main", title: "<N> flown", sub: "<first> … <last>", state: "done",
  count: N, flown: [titles] }`. A single flown stop stays as it is. A `failed`
  stop breaks the run, so it stays in sight.
- **Edges and branches.** The line's edges chain through the pill. A column of
  branches that fanned out of a folded stop fans out of the pill instead, and
  joins at the box after it. The direct pill → next edge is dropped, so the line
  goes through the branches.
- **Numbering.** `insertFrom` and every stop's `index` stay the flight's own.
- **Expanding.** `SessionDiagramOpts.expandFlown` turns the fold off. The line
  keeps that as its own `useState` (not persisted). A press on the pill
  (`onOpen` with `kind === "flown"`) or the ⋯'s **Show every flown stop** /
  **Fold the flown stops** toggles it.
- **Layout.** `layoutDiagram` sizes the pill from `boxMetrics().flown` and
  never calls `sizeOf` for it. It sits centred on the line's anchor, the way
  the + does. The canvas styles `.fl-box-flown` as a green-edged pill with a
  double tick (`StopIcon` `flown`).
- **The Flight tab.** `useSessionLine` passes no option, so it folds too.
- **The canvas counts DRAWN stops.** A move's from / to, an insert's gap and
  `insertFrom` comparisons all count the stops on screen. With a pill folding N
  stops, those numbers are N lower than the flight's. `drawnIndexes(d)` lowers
  every stop's `index` and `insertFrom` by N for the canvas. `flownCanvas(d,
  canvas)` (SkyLine.tsx) adds N back on the way out (`onMove`, `onInsert`,
  `onDropStop`) and hands back the original nodes (`onSelect`, `onRemove`,
  `onDuplicate`, `renderInBox`, `sizeOf`). Without that, a drag on a folded line
  would move the wrong stop.

#### The line's bar and its ⋯

The bar shows the bird, the name, the state chip, and **where the flight is,
in words**. This comes from `lineWhere(diagram)`, which follows `birdPerch`:

- "at Reviews settle · 10 min quiet"
- "waiting on you at Perch: approve the merge · 2 items to tick"
- "halted at Push · <reason>"
- "at Checks · waiting for 2 of 3"
- "fast-track at Push"

Nothing shows while the bird sits on the session box, because the chip says
that already. After that come 📌, then **◧**: the glyph, a three-bar meter
lit to the current size (compact 1, normal 2, wide 3), and the size in words
on hover or focus.

**⋯ menu groups** (a rule above each group's first item; items 13.5px in 32px
rows; the list scales itself back to screen size; the line whose menu is open
is lifted over its neighbours):

1. **Open session** (opens the card on the line; **Fold the session back**
   when it is open) · **Open in the grid** (`selectSession` + `setView("grid")`)
   · **Show only this line**
2. **Compact boxes / Normal boxes / Wide boxes** (✓ on the current one) ·
   **Show every flown stop** (only when something is folded) · **Pin here**
   (pins at the line's current spot), or **Tidy this line** when it is already
   pinned (Unpin and Tidy are the same act, so there is one item)
3. **Fly a flyway…** · **Tack on a stop**
4. **Close session** (danger)

#### Contrast

**States** (`FlywayCanvas.css`, every home):

- **current**: a 2px working outline and the pulse.
- **perched**: a 2px gold outline and a 14% gold wash.
- **halted / failed**: a 2px red outline. The reason uses a red ink.
- **done / skipped**: no element opacity. The title is `text` at 78% and the
  summary is the muted ink at 85%, on the panel.
- **pending**: a dashed outline, with the summary in the muted ink.

Wires are 2px at 88% (fan / join 75%), with solid arrowheads. The selected
ring is 3px.

**Text inks** (`--fl-ink-working`, `--fl-ink-red`, `--fl-ink-muted`;
`--sky-ink-gold` for the drawer's head). Light mode overrides them; text on
solid gold is `--tone-attention-ink`.

| text on its background | dark | light |
|---|---|---|
| title on a box (`--text` / `--panel-2`) | 11.35 | 13.96 |
| summary, muted ink on a box | 6.53 | 6.24 |
| done title (text 78%) on the panel | 8.02 | 7.91 |
| done summary (muted ink 85%) on the panel | 5.52 | 4.96 |
| pending summary on the panel | 7.15 | 7.24 |
| perched title on the gold wash | 8.07 | 13.04 |
| perched summary on the gold wash | 4.64 | 5.83 |
| halted reason (red ink) on a box | 5.53 | 5.15 (raw `--red` was 3.82) |
| "Working" chip (working ink) on a box | 6.71 | 4.63 (raw green was 2.95) |
| chip ink on solid gold | 12.41 | 9.34 |
| bar's where line (muted ink) on the panel | 7.15 | 7.24 |
| drawer's "Perched on you" head | 12.39 (gold on the panel) | 6.37 (#7a5a10; raw gold was 1.86) |

All body text is ≥ 4.5:1 in both themes. The numbers are WCAG relative
luminance from the tokens in `styles/tokens.css`, with `color-mix` resolved
(script: the round's scratch `contrast.py`).

#### What the merge has to wire (outside this change's files)

- `FlywayCanvas.tsx` needs a `roomy?: boolean` prop. Pass it into
  `layoutDiagram`'s opts (`{ compact, vertical, sizeOf, roomy: !!p.roomy }`,
  and add it to the memo's deps), and make `sizedBox(size, n, base, detail,
  roomy)` return `sizedBoxFor(size, n, base, detail, roomy)`. SkyLine already
  passes `roomy` through a cast spread. Until then the sky draws 15px type in
  base-size boxes.
- `SessionFlight.tsx` should use the same `flownCanvas(diagram, line.canvas)`
  wrapper and its own `expandFlown` toggle on a `flown` press, as SkyLine
  does. Until then the Flight tab folds the pill, but drags and the ⇤ / ⇥
  moves there count drawn stops while the flight counts its own.
- `FlightStrip` (the Board's strip) also gets the pill from
  `sessionDiagram(…, family: false)`. A press on it does nothing there.

#### Screenshots

Before (the round-9 bundle) and after (this change with the FlywayCanvas
wiring above applied), 1400 × 900, mocked API, in the round's scratchpad
`shots/`: `before-{fit,far,near,menu,light,drawer,lightdrawer,tab}.png` and
`a2-{fit,far,near,menu,light,drawer,lightdrawer,tab}.png`.

## Flyways as rows of stops

*(To be folded into `docs/flights.md` → "The flyway shelf" and "The shelf: flyways
and stops"; it replaces the chips described there.)*

The owner: "I want to be able to delete steps from a flyway. I feel like the flyway
concept is not really that clear. I think it should be drag and drop."

**A flyway is a saved route of stops** — a reusable sequence (ticket → agent → commit
→ push → PR → reviews → merge) you fly a session along.

### The shelf

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

### A row

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

### The empty row

"Drop stops here to start a flyway". A stop chip dropped on it (or **Pick a stop** →
the palette) creates the flyway: `savePlan` of a manual `blankPlan` with that one stop
(`flywayFromDrop`), named what was typed, else a fresh "New flyway", "New flyway 2"…
(`freshFlywayName`). It then is an ordinary row, and if it was not named yet its name
field takes the focus. **Editor…** opens a fresh FlywayCard (`onNew`) — the way to
describe a whole flyway in a sentence.

**+ New flyway** puts a name field in the empty row (focused); Enter there opens the
palette for the first stop, Esc drops the name. The server refuses a flyway with no
stops, so nothing is saved until the first stop lands.

### Built-ins

Rows like the rest, with the tag. Editing one saves an override (the server keeps it
under the built-in's id); **Reset to default** (`resetPlan`) puts it back, and that is
also what "delete" means for a built-in.

### Renames (`checkRename`)

Trimmed; empty is refused ("A flyway needs a name"); over 120 characters is refused
(the server's cap); another flyway's name (any case) is refused so two rows can't look
the same. A refused name keeps the field open on Enter (the reason under it); leaving
the field puts the old name back and says why in a toast.

### The FlywayCard

Still the details editor (opened from a row's ⋯ or ✎), unchanged except `initialStop`
(the stop to open on). It keeps its own canvas, palette and inspector as a fallback,
and **Save & fly on…**.

### Host changes this needs (outside this change)

- `SkyView.tsx`: `onEdit={(plan, stop) => setCard({ at: middle(), initial: plan,
  stop })}` and `<FlywayCard … initialStop={card.stop} />`, so ✎ on a row's stop
  opens the card on that stop (without it the card opens on the first stop).
- `flightsUi.test.ts` "carries the flyway shelf at its foot": the chip pin
  `class="sky-shelf-chip builtin" data-plan="builtin:ticket"` becomes
  `class="sky-flyway-row builtin" data-plan="builtin:ticket"`.

### Where it lives

`frontend/src/components/flights/FlywayRow.tsx` (+ `.css`), `FlywayShelf.tsx` (+
`.css`), `StopShelf.tsx`, `frontend/src/lib/flywayEdit.ts`; tests in
`__tests__/flywayRow.test.ts`, `skyFlyways.test.ts`, `stopShelf.test.ts`.

## Round 11: stops all the way down (contract)

The owner, after round 10: flyways are not intuitive; "get rid of the concept
of a flyway entirely and just have stops that have multiple sub-steps in them,
so Ticket to merge is just a series of blocks I can drag onto the board";
a dialog to edit a box; resize a box by its corner; the New session button
should be a stop; delete a step that is running → the flight halts and sits
right before where it was; a join (or any flown stop) must not vanish when
done; a renamed session shows its new name everywhere; new boxes must appear
at once; the Tidy and other buttons in the app's button style; drag to select
an area, move and mass-delete; add stops with no session (they do nothing
until one is attached); select a series of boxes and make them one stop.

Words: **stop** = a block. A **saved stop** = what the store still calls a
plan (`plans.json`, `/api/flights/plans`): a named series of stops you can
drag onto the board. The word *flyway* leaves the UI, the docs' new text and
the code's new identifiers (old identifiers stay until their files go).

### Groups — a stop with sub-steps

A step may carry `group`:

```json
{"id": "s4", "kind": "ship", "title": "Open the PR", "config": {}, "on_fail": "halt",
 "group": {"id": "g_3fa2c1", "name": "Ticket to merge", "plan_id": "builtin:ticket"}}
```

Consecutive steps with the same `group.id` are ONE stop on the board — a
compound box named `group.name` that shows its sub-steps inside (and "k of n"
while it flies) — and the driver flies them one by one exactly as before: the
flight's `steps` stay flat, the cursor is still an index, nothing in the
planner changes. `plan_id` is `""` for a group made by hand. The normaliser
drops a `group` whose `id` or `name` is empty; `id` ≤ 40 chars, `name` ≤ 80.
Non-consecutive steps sharing an id are split into separate boxes by the UI
(the server does not forbid it; `op_move_step` leaves groups alone).

Routes (all answer `{flight}` unless said; the usual 404 `unknown_flight` /
`unknown_step`, 409 `in_progress` for a step that cannot change):

- `POST /api/flights/{id}/steps` — as before `{step, at?}`; **new**
  `{plan_id, at?}` appends that saved stop's steps as one fresh group (new
  step ids, `group.name` = the plan's name, `group.plan_id` = its id; 404
  `unknown_plan`); **new** `{steps: [Step…], at?, group?: {name}}` adds
  several at once, grouped under a fresh id when `group` is given.
- `PATCH /api/flights/{id}/steps/{sid}` — **new** `group: {name} | null` on
  a step that can change: renames its group (every step of the group) or, with
  `null`, takes this one step out of it.
- `POST /api/flights/{id}/group {steps: [sid…], name}` → `{flight, group}`:
  the named steps (consecutive, each able to change) become one new group;
  409 `in_progress` otherwise, 400 `bad_group` when not consecutive.
- `POST /api/flights/{id}/ungroup {group: gid}` → `{flight}`: every step of
  the group loses it.
- `DELETE /api/flights/{id}/steps/{sid}?group=1` removes every step of the
  step's group (rules below apply to each).

### Removing the stop that is flying

`DELETE /api/flights/{id}/steps/{sid}` now also takes **the current step**
of a flight that is `running`, `waiting_you`, `paused` or `halted`: the
step's side effects stop (a `ship` step disarms its autopilot lane; an
`agent` step is left to the session — nothing is typed; `split` children are
left alive), the step is removed, and the flight goes `paused` with
`reason: "stop removed"` and a history `removed` entry whose note names the
stop. The cursor keeps its index, which now points at the step that followed,
so the flight **sits right before that step**; Resume carries on into it. When
nothing followed, the flight stays `paused` at the end and Resume lands it.
A flown step (done / skipped / failed, behind the cursor) may be removed too:
the cursor shifts down with it and nothing else changes. Grounded flights
(below) have no current step: every step can change.

### Grounded flights — stops with no session yet

A series of stops can sit on the board with no session; it does nothing until
one is attached.

- `POST /api/flights/start {grounded: true, plan_id? | steps?, name?}` →
  202 `{flight}` with `session: ""`, `state: "grounded"` (a new state),
  `cursor: 0`, every step `pending`. `plan_id` copies the saved stop's steps as
  one group (as the add route does); `steps` are taken as given (grouped under
  `name` when it is set, else ungrouped, `plan_id: "adhoc"`, `plan_name`
  = `name` or "Stops"). No `title`, no `repo_path`, no session is made.
- The driver skips `grounded` flights (`next_action` is never called on
  one), `prune` never releases them, `GET /api/flights/flights` lists them
  with the unfinished ones, and `DELETE /api/flights/{id}` is allowed on one.
- `POST /api/flights/{id}/attach {title}` → `{flight}`: binds the grounded
  flight to that live session (404 `unknown_session`; 409 `in_flight` when the
  session already has a flight in the air; 409 `not_grounded` on any other
  flight) and takes off — the flight behaves from here exactly as one started
  with `POST /start {plan_id, title}` would (state `running`, or `paused` at a
  halt point on its first step; history `attached`; `repo_root`/`branch`
  filled from the session). `{title: "", repo_path, program?, model?,
  in_place?, prompt?}` instead creates the session the way `start` does for a
  saved plan, then attaches.
- Step routes (add / edit / remove / move / group / ungroup) all work on a
  grounded flight, every step counting as able to change.
- Events: `flight.changed` on create and attach.

### Names

A session's shown name is `windowName(title)` (`lib/windowName.ts`; the
server twin is `aliases.display_name`) — on the box, the bar, the card, the
drawer, the strip, every toast. Never `inst.title` or `row.title` raw. Layout
stays keyed by the raw title (a rename is an alias, the title does not
change).

### The board, the shelf, selection (frontend)

- The shelf is ONE row, **Stops**: a **Session** chip first (drop it on empty
  sky → the new-session block at that spot; drop it on a grounded line → a new
  session is made and attached), then the kind chips, then the saved stops
  (name + a row of dots, one per sub-step). Drop any chip on empty sky → a
  grounded flight (one step, or the saved stop's group) pinned at that spot;
  drop it on a line → tacked on there (a saved stop as one group). The toolbar's
  "+ New session" button is gone (the Session chip, double-click, Ctrl/Alt+N
  remain).
- A grounded flight draws as its own line (layout key `flight:<id>`), with a
  dashed "No session yet" head box and its ⋯: **Fly on a session…** (the line
  picker → attach), **Save to the shelf** (`savePlan` from its steps; a line
  that came from a saved stop updates that saved stop), **Remove**.
- Editing a saved stop = drag it onto the board, edit the blocks there, Save
  to the shelf. Right-click a saved-stop chip: Rename, Reset to default (a
  built-in), Delete. The schedule / session defaults of a saved stop ("Takes
  off") live in a small dialog from the chip's menu.
- Every box edits in a **dialog** (`Modal`), not an inline panel: ✎, Enter,
  double-click, the menu's Edit. A group's dialog lists its sub-steps (each
  opens its own), renames the group, offers Ungroup and Save to the shelf.
- Flown stops stay as boxes (dimmed, ✓); the "N flown" fold is OFF by default
  and the ⋯ item reads "Fold flown stops".
- A line's boxes resize by a corner handle on the line (stored `boxW`/`boxH`
  in `SkyLineLayout`, 140–420 × 60–220, clamped); the ◧ presets set the same
  two numbers.
- Drag on empty sky = marquee selection (pan: wheel/trackpad, middle button,
  Space+drag, arrows). The selection is lines and/or stop boxes; the selection
  bar offers Move (drag any selected line moves them all), Remove…
  (inline-confirmed; a session line closes its session, a grounded line is
  deleted), and for stops of one line: Group into a stop, Save to the shelf,
  Remove.

### As built: round 11 backend

Everything in the round 11 contract holds; these are the names, the error
words and the places the backend had to choose.

**Groups.**

- `flights._normalize_group` is the one normaliser (`normalize_step` calls
  it, so plans, take-off snapshots, `adhoc_plan`, the stored flight and every
  dto carry `group` unchanged). It never refuses, strict or lenient: a group
  with an empty id/name is dropped, `id` is cut to 40, `name` to 80,
  `plan_id` is coerced to a string. A step with no group has no `group` key.
- Pure operations: `op_add_plan` (`{plan_id}`), `op_add_steps` (`{steps,
  group?}`), `op_group` (returns the new group), `op_ungroup`,
  `op_remove_group` (`?group=1`), and `group` in `op_edit_step`. Group ids are
  `g_<6hex>`, unique within the flight. All step adds share one placement
  rule (`_insert_index`) and give each new step the next free `sN` plus its
  own history `added`.
- `{steps}` without `group` keeps whatever `group` each step came with (so a
  client can put a removed group back as it was); `group` that is not an
  object is 400 `bad_group`; an empty name is 400 `bad_group`. A bad step is
  400 `bad_step` with `field: "steps[i]…"`. Too many is 409
  `too_many_steps`.
- **Rename is a label**: `PATCH {group: {name}}` renames the whole group from
  ANY of its steps on a flight not yet over — flown and flying sub-steps too
  (the group dialog may send its first sub-step's id). `{group: null}` (take
  out) needs a step that can change. `{group: {name}}` on a step with no group
  makes it a stop of its own (it must be able to change). `group` can come
  with `title`/`config`/… in one PATCH; a `group` that is neither an object
  nor null is 400 `bad_step` (`field: "step.group"`).
- `POST /{id}/ungroup` is also a label change: allowed on any flight not yet
  over (flown sub-steps lose the group too); 404 `unknown_group`.
- `POST /{id}/group`: 400 `bad_group` for no steps / no name / not side by
  side, 404 `unknown_step`, 409 `in_progress` for a step that can't change
  (409 `finished` on a halted/finished flight, like the other edits). One
  history `edited` ("N stops grouped as <name>"); no new history event.

**Removing a stop.** `op_remove_step` / `_remove_at`:

- Any step of a flight not yet over (halted included): one ahead just goes
  (a `skipped`-ahead one too); one behind shifts the cursor down; the current
  one pauses the flight (`reason` = `flights.STOP_REMOVED` = `"stop
  removed"`, history `removed` "removed <title> while it flew"). On a
  grounded flight nothing pauses.
- **The last stop stays**: removing the only step left (or a group that is
  every step) is 409 `last_step` — delete the flight instead.
- Side effects (driver `_stop_removed`, after the store write): the removed
  current step's lane is disarmed when it is a `ship` step or a `reviews`
  step (its push), through `_disarm_ours` = `autopilot.disarm(title)` only
  when the record is this flight's (`source == "flight"`, `item` = the
  flight id) and not `done` — your own ⏩ run is left alone. Any removed
  step's command job is forgotten (the process itself is not killed). An
  agent's queued prompt and a split's workers are left. A lane armed by a
  pass that was mid-effect when the route landed is disarmed when that pass
  records its outcome (`_disarm_if_removed`).
- Resume of a flight paused at the end (`cursor == len(steps)`) lands it
  (`op_resume` → `done`, `flight.finished`). Resume after a removal lets
  the next stop's halt point through, as Resume always does — the flight
  already sat right before it.
- **Undo**: `POST /steps {step, at}` also takes `at == cursor` on a `paused`
  flight whose current step has not started (or `cursor == len(steps)`), so
  a removed flying stop can be put back right where it was and flies first
  on Resume. A running flight still needs `at` after the cursor.

**Grounded flights.**

- `flights.GROUNDED = "grounded"` is first in `STATES` and in `UNFINISHED`
  (not in `ACTIVE` / `HOLDING`), so the driver never steps it, `prune` and
  `for_session` never see it (its session is `""`), `GET /api/flights`
  lists it with the unfinished, and `DELETE` takes it. No history entry at
  creation.
- `flights.ground(plan, steps, name)`: the plan's steps as one group, then
  the given ones (grouped under `name` when set); steps renumbered `s1..sN`.
  `plan_id` = the plan's id (a virtual `builtin:<kind>@<lane>` works) or
  `"adhoc"`; `plan_name` = `name`, else the plan's name, else "Stops".
  Neither → 400 `needs_steps`; 404 `unknown_plan`; a bad step 400 `bad_step`.
- Done / Skip / Retry / Jump / Pause / Resume on a grounded flight are 409
  `grounded`; Cancel works. All step routes work, every step able to change
  (`at` 0 included; `{halt}` on any step).
- Attach: `flights.attach(id, title, repo_root, branch, seeded)` checks under
  the store lock (404 `unknown_flight`, 409 `not_grounded`, 409 `in_flight`
  when the title has ANY unfinished flight, halted included — attach never
  replaces one). `op_attach` sets `running`, `started` = now, history
  `attached` ("attached to <title>"), then the existing halt-point rule
  (`_hit_halt_point`) may pause it on step 0. `repo_root` comes from the
  session's worktree (`_repo_root`, as `start` does), `branch` from
  `inst.Branch` (the driver keeps it fresh after). `"attached"` is in
  `HISTORY_EVENTS`. The route emits `flight.changed` and wakes the loop.
- **The cursor stays at 0 on attach**: unlike `POST /start {plan_id,
  title}`, which starts an existing session at its first non-`agent` step
  (the work so far counts as the agent step), a grounded flight's stops were
  drawn on purpose, so a leading `agent` step is flown: the driver's normal
  `run` → `_run_agent` → `_deliver` (the prompt queue) types it — the path a
  start with `start_at` on an agent step takes today. No fast-track run is
  absorbed on attach.
- `{title: "", repo_path?, program?, model?, in_place?, prompt?}`: the plan's
  `session` defaults (by the flight's `plan_id`), overridden by the body,
  through `session_create.create_result`; a first `agent` step (not a halt
  point) gives the initial prompt (`prompt` outranks it) and is recorded
  running (`seeded`), else `no_prompt` as `start` does. No folder → 400
  `no_repo`; every refusal comes before the session is made.

**Also.** A flight step's ntfy push is tagged `bird` (🐦), not `airplane`.
### As built: round 11 line

> Code: `frontend/src/components/flights/SkyLine.tsx` (+ `SkyLine.css`),
> `lib/skyLayout.ts` (the store, `boxScale`), `state/queries.ts`
> (`mergePending`, `notePendingCreated`, `landPendingRow`, `dropPendingRow`,
> `bridgeSessionSetEvents`), `lib/sessionActions.ts`. Tests:
> `skyResize.test.ts`, `skyFresh.test.ts`.

**Resizing a line's boxes.** `SkyLineLayout` gains `boxW` / `boxH` (plane px
of one stop box), clamped to 140–420 × 60–220. The store keeps both or
neither, and neither when they are just the preset's (`cleanLine`); an old
layout's `size` alone stands for its preset's numbers (`lineBox`).
`estimateLine` scales the line's width by `boxW / 236` and the part under
the 42px bar by `boxH / 100` when they are set (the old size factors
otherwise).

The canvas still draws the three tiers, so a box keeps showing what its tier
shows (compact the title, wide the detail line). `boxScale(w)` picks the tier
nearest the width (compact under 193, wide from 283) and a uniform scale
`k = w / tierW`. That is the round's "boxW / 236" generalised: every preset is
its own tier at `k = 1`, pixel-identical to round 10. The line draws the
tier's canvas inside a sizer:

- `.skl-scale` (the sizer) is set to the canvas's own size × `k`, read by a
  ResizeObserver, so the line's offset size, and so `onMeasure`, is what it
  draws.
- `.skl-scale-in` carries `transform: scale(k)` (origin 0 0) and
  `--sky-z = zoom × k`, so everything that counter-scales by `--sky-z`
  stays screen-sized: a box's toolbar, the gap +, a stop's menu. The level
  of detail's type factors (`--sky-far`, `--sky-far-ui`, `--sky-mid`,
  `--sky-near`) are declared again on it, because custom properties on `.skl`
  resolve against the zoom alone. The bar and its ⋯ keep the plain zoom.
  The band (`lod`) still follows the camera.
- Drops and picks need no change: the canvas's `stagePoint` divides by its
  rendered width over its layout width, which includes `k`.
- An open card asks the canvas for `size / k` and draws scaled by `1 / k`
  with `--sky-z` back at the zoom, so the pane keeps its size and its
  corner's maths.

The handle is `.skl-box-resize`, the bottom-right corner of the line's frame.
It is screen-sized and shows on hover or focus. A drag works in plane px
(pointer travel ÷ zoom). The drawn canvas follows the pointer along the axis
it moved most, and the box width follows the canvas (`dragBox`). The height
is the tier's at the same scale. The drag is drawn live and persisted once on
release: `onLayout({size: tier, boxW, boxH})`. ◧ and the ⋯'s Compact / Normal
/ Wide are presets of the same numbers (`presetPatch`): compact 150 × 64,
normal 236 × 100, wide 330 × 120.

**Flown stops stay.** The line asks `sessionDiagram` for both pictures
explicitly (`expandFlown: false` for the folded one it counts flown stops
on, `expandFlown: true` for the shown one unless folded), so it reads the
same whatever the diagram's own default is. The ⋯ item is "Fold flown stops"
(then "Show flown stops"), and the pill still unfolds. `flownCanvas` remaps
indexes as before when folded.

**Names.** The bar, the line's aria label, the ⋯'s label, the canvas's label,
the open card's name and its two labels go through `windowName(title)`. A
rename redraws the line (it subscribes to its alias). `data-line`, keys and
the layout stay on the raw title. The ⋯'s "Fly a flyway…" reads **"Add a
stop…"** ("A saved stop, tacked on here").

**New boxes at once.** `GET /api/instances` serves the server's tick
snapshot, so a session made a moment ago is missing from it for a tick or two,
and the optimistic row the dialog added used to be wiped by the very
next fetch. Now:

- An optimistic row is `pending_create: true` with `pending_at` (its clock)
  and, while its real title is unknown, `pending_base` (the title it was
  numbered from). Every fetch runs `mergePending`. It keeps the cached
  pending rows the fetch does not list, after the fetched rows, until the
  fetch lists the title, or a title new in this fetch numbered from the base
  (`base` or `base-N`; each new title claims one row), or `PENDING_TTL_MS`
  (20 s) passes.
- `bridgeSessionSetEvents` (installed once, from `useInstances`) subscribes
  to the following:
  - `session.created` adds a row at once (with `parent` / `program` from the
    event; not for a `verify-` helper). If an optimistic guess is waiting on
    the same base, that guess takes the real title instead, so there is one
    box, not two.
  - `session.create_failed` drops the pending row.
  - `session.created`, `session.create_failed`, `session.deleted` and
    `session.renamed` each refetch, debounced to one refetch per 150 ms burst.
  - `flight.changed` already refetches the flights (`bridgeFlightEvents`).
- `addPendingSession` stamps `pending_at` / `pending_base`. Copy, Branch and
  Reopen (Ctrl+Shift+T, now with a pending row too, keeping the session's
  rename) land the create's answer with `landPendingSession`: the guess
  becomes the real row, still waiting for the snapshot. A bare answer from
  another device is not landed; its namespaced guess waits for the poll.
  The New dialog (`lib/newSessionSubmit.ts`) and the new-session block
  (`NewSessionBlock.tsx`) already add a pending row. Their guess is
  reconciled by the event or the poll, so they need no change.
- One query feeds the grid, the rail and the Sky, so all three get the box at
  the same moment. No surface reads `pending_create` specially.
### As built: round 11 stops

> The stops worker's half of round 11. Code: `frontend/src/components/flights/StopDialog.tsx`
> (+ `.css`), `useSessionLine.tsx`, `FlywayCanvas.tsx` (+ `.css`), `StopParts.tsx`
> (`StopForm`), `lib/flightDiagram.ts`, `lib/stopEditing.ts`; tests in
> `__tests__/stopDialog.test.ts` and `__tests__/stopGroups.test.ts`.

**The dialog (`StopDialog`).** Every box on a session's line edits in a `Modal`
(`#stop-dialog`, rendered into `document.body` so the Sky's zoom transform can't
carry the backdrop), never the inline inspector. `FlywayCanvas` gained
`onEdit?(n)`: ✎ on the toolbar, Enter / Space on a focused box, a double-click
and the menu's Edit call it, while a click only selects (`onSelect`). A host
without `onEdit` (the flyway editor) keeps the old behaviour (everything calls
`onSelect`). Contents:

- a **stop that can change**: its kind (icon, name, summary, read-only), Title,
  the kind's fields and If it fails (`StopForm`, the inspector's own renderer,
  now exported from `StopParts`), the halt-point toggle (saved with the rest:
  `patchStop {title, config, on_fail, halt?}`); Remove · Duplicate, Cancel ·
  Save (Save disabled until something changed; Enter submits);
- a **stop that can't** (flown, or the current one): read-only, its state chip
  and why (plus the flight's note or reason); Remove, and on the current stop
  Done (a perch) / Retry (halted) / Skip and its halt point set at once
  (`setHalt`); Close;
- a **group**: its name (Save renames: `patchStopGroup` on its first sub-step
  that can change; no rename when none can), a row per sub-step (icon, title,
  state, ✎ opens that sub-step's own dialog — closing it goes back to the
  group's — and 🗑 removes it), **Ungroup** (`ungroupStops`), **Save to the shelf**
  (`savePlan(shelfPlanFor(name, steps, group.plan_id, plans))`: the saved stop
  the group came from when the plans query still has it — same id, trigger,
  session and `builtin` — else a new one; a toast says which), **Remove**
  (`dropGroup`).

`useSessionLine`'s `inspector` output is that dialog (or null); it no longer
renders `StopInspector` (still exported, with `DescribeField`, for its other
caller). No `window.confirm/prompt/alert` anywhere.

**Groups on the line.** `sessionDiagram` folds each run of consecutive steps
sharing `step.group.id` into ONE main-row node (`collapseGroups`, after the
branches are placed): `kind: "group"`, `id: "group:<gid>"` (a later run of the
same id: `group:<gid>@<index>`), `title` = the group's name, `index` = its first
sub-step's, `editable` when every sub-step can change, and `group: {id, name,
plan_id, steps, nodes, done, total, current?}` (`nodes` are the sub-steps' own
stop nodes with their live states). Its `state`: `done` when every sub-step is
done or skipped; else the flight's state at a sub-step it is in (current /
perched / halted / paused); else `failed` when one failed; else `pending`
(`edit` in an editor). While it flies `sub` is the current sub-step's title and
the box shows "k of n"; otherwise `sub` is "3 stops: Push → Open the PR → …".
The halt mark and ⏸ act on its first sub-step not yet flown (`groupHaltStep`).
Branches spawned at a sub-step fan out of the group box and join at the box
after it. The flown fold never swallows a group. `SessionDiagramOpts.expandedGroups:
Set<gid>` draws a group open: its sub-steps stay stop boxes marked `inGroup:
{id, name, first, last}`; the canvas draws a dashed outline around them
(`groupFrames`) labelled with the name and a ▾. `useSessionLine` keeps the set
(`expandedGroups`, `toggleGroup`) and the canvas calls `onToggleGroup(gid)` from
▸ / ▾ and the menu's Show / Fold its sub-steps.

On the canvas the group box is `.fl-box-group` — a stack (a second outline
offset behind it), ▸ in its head, a dot per sub-step. Its toolbar: ⏹/▶ while the
flight is in it, ✎, ⇤ ⇥ **disabled this round** ("A stop with sub-steps doesn't
move yet"; it never drags either — a stop may still trade places with a group
that can change), ⏸, 🗑 (the whole group). Its menu: Halt now / Carry on (in it),
Edit (Details once it flies), Show / Fold its sub-steps, Halt here, Remove.

**Index mapping (the remap helper).** The canvas counts *places*: a collapsed
group (or the flown pill) is one. `lib/flightDiagram.ts` exports:

- `lineUnits(d): {node, first, size}[]` — the line's places (stops and group boxes);
- `drawnIndexes(d): Diagram` — every place's `index` and `insertFrom` renumbered
  to places; the diagram itself (`===`) when nothing needs it;
- `flightIndexAt(d, p): number` — place `p` (an insert gap, a drop) → the
  flight's index (the place's first step; past the last, the end);
- `flightMove(d, from, to): {from, to}` — a move between places → the move
  route's `{from, at}` (later: right after the target's last step; earlier: right
  before its first);
- `drawnCanvas(d, canvas): {diagram, canvas}` — both at once, the generic
  successor of SkyLine's `flownCanvas` (maps `onMove`, `onInsert`, `onDropStop`
  and hands the line's own nodes back to `onSelect`, `onEdit`, `onOpen`,
  `onRemove`, `onDuplicate`, `onToggleHalt`, `renderInBox`, `sizeOf`); a
  no-op on a line already counted that way, so it is safe to apply twice;
- `nodeAtIndex(d, i)` — the stop, or the group box, the flight's step `i` is
  drawn in (`currentNodeId` uses it, so the canvas reveals a group box).

`useSessionLine` now returns `diagram` and `canvas` **already drawn** (it runs
`drawnCanvas` itself), so a host spreads them as they are; SkyLine's
`flownCanvas(line.diagram, line.canvas)` is a pass-through on them (nothing
folds by default). A host that builds its own `sessionDiagram` should pass
`expandedGroups: line.expandedGroups` and `foldFlown: line.foldFlown` and wrap
with `drawnCanvas` (and stop using `flownCanvas`).

**Drops (`onDropStop`).** The prop is now `onDropStop?(drop: {kind} | {planId},
at)` (`StopDrop`, lib/stopEditing). `isStopDrag` takes `STOP_MIME` (a kind chip)
and `FLYWAY_MIME` (lib/skyLayout: a saved stop, its value the plan id);
`stopDropOf(dataTransfer)` reads the payload (a kind first). The canvas stops a
saved-stop drop on a line from reaching the Sky's own drop. `useSessionLine`
wires `{kind}` to `tackOne` as before and `{planId}` to `tackOnPlan(flightId,
planId, at)`; on a line with **no flight** it takes off as that saved stop's
flight (`takeOff({plan_id, title})`, the "Fly a flyway" path — the simplest that
works; its stops are not grouped). The + palette's extra row is now **Saved
stops** (under the kinds, also for a + between stops), each tacked on as a group
(`tackOnPlan`); the old "Fly a flyway — all of its stops" row is gone and
`flyFlyway` tacks a saved stop on as one group too.

**Remove, everywhere.** Edit / move / duplicate stay pending-only; 🗑 shows on
every stop of a live line: still ahead "Remove", the current stop "Remove — the
flight halts here", a flown one "Remove from the line" (lib/stopEditing
`removeTool`; the menu says the same; Delete / Backspace stays pending-only). The
current stop's ⏹ / ▶ show only with `onToggleHalt`, as before. `useSessionLine`
toasts `Removed “X” — the flight is paused before “Y”` (or `… paused at the
end`) for the current stop (and a group holding it), `Removed “X” from the line`
otherwise, each with Undo (`undoPayload`; a group comes back through
`tackOnSteps(…, name)`).

**Flown stops stay (the fold flag).** `SessionDiagramOpts.foldFlown?: boolean`
folds only when true; `expandFlown` is still accepted as its inverse (`fold =
foldFlown === true || expandFlown === false`). `useSessionLine` keeps `foldFlown`
(false) with `setFoldFlown`, and a press on a pill unfolds. A done join keeps its
box (no pill once `waiting` is 0).

**Names.** Session, parent, child and branch boxes carry `windowName(title)` as
their `title` (their `id` and `session` stay the raw title); FlightStrip's card
and SessionFlight's label use it too.

**Outside this change (for the merge):**

- `FlywayCard.tsx:257` and `FlywayRow.tsx:291` still pass the old `(kind, at)`
  shape: `onDropStop={(drop, at) => { if (!("kind" in drop)) return; const def =
  kindOf.get(drop.kind); … }}` (FlywayRow: `"kind" in drop &&
  insert(kindOf.get(drop.kind), at)`).
- SkyLine: drop `flownCanvas` and its own `expandFlown`; the ⋯ item becomes "Fold
  flown stops" → `line.setFoldFlown((x) => !x)` (checked by `line.foldFlown`); the
  `onOpen` override for the pill can go (the hook unfolds).
### As built: round 11 board

The word *flyway* has left the Sky's UI; the board is the only editor of a
series of stops. Source: `components/flights/StopShelf.tsx` (+ `.css`),
`GroundedLine.tsx` (+ `.css`), `useGroundedLine.tsx`, `TakeoffDialog.tsx`
(+ `.css`), `SkyView.tsx`, `NewSessionBlock.tsx`, `lib/groundedLines.ts`;
tests in `stopShelf.test.ts`, `stopShelfSaved.test.ts`, `grounded.test.ts`,
`skyStable.test.ts`.

**The shelf** (`StopShelf`) is one row along the sky's foot, shown unless
folded to its handle (**Stops ▾**, remembered per device as
`mf_sky_stops_folded`; the old `mf_sky_shelf` folded-by-default is gone). It
carries `data-tour="sky-shelf"`. Left to right:

| Chip | Drag carries | Dropped on empty sky | Dropped on a line | Click |
|---|---|---|---|---|
| **Session** (`data-tour="flights-newblock"`) | `NEW_SESSION_MIME` = `session` | the new-session block at that spot | a grounded line: a block that makes a session for its stops (`attachGrounded(id, {title})` once made); a session line refuses | the block in the sky's middle |
| a stop kind | `STOP_MIME` = the kind | `startGrounded({steps: [defaultStepFor(kind)]})` | tacked on at the gap (the canvas), else at the end | tacked onto the focused line (or the line picker) |
| a saved stop (name + a dot per step, a perch's gold, "+N" past ten) | `FLYWAY_MIME` = the plan id, `text/plain` = its name | `startGrounded({plan_id})` | tacked on as one group (`tackOnPlan`), or flown on a line with no flight (`startFlightOn`) | the same, on the focused line |

A new grounded line is pinned where the chip fell. The toolbar's
**+ New session** is gone; double-click on empty sky and Ctrl/Alt+N still
open the block. Right-click a saved stop (or the context-menu key /
Shift+F10): **Rename** (a field inside the chip — Enter saves, Esc cancels;
`checkRename`: never empty, ≤ 120, not another saved stop's name),
**Takes off…** (the TakeoffDialog), **Reset to default** (a built-in,
`resetPlan`), **Delete** (yours, `deletePlan`, confirmed inline with
`InlineConfirm` under the row).

**Grounded lines** (`GroundedLine` + `useGroundedLine`): every flight with
`state === "grounded"` is its own line, laid out under `flight:<id>`, after
the session lines, newest first (not in This session or Perched on you; Find
matches its name). Its bar: the bird, the name (`plan_name`, read-only), a
"No session" chip, a ⋯. Then a dashed head box ("No session yet — drop a
Session chip here, or ⋯ Fly on a session") and its stops on the canvas, every
one editable — the same flight routes a session's line uses (`tackOn`,
`tackOnPlan`, `dropStop` with Undo, `moveStop`, `setHalt`, `patchStop`). The
⋯: **Add a stop…**, **Fly on a session…** (the line picker, sessions with no
flight in the air → `attachGrounded`), **Save to the shelf** (`planToSave`:
a line whose every step shares one existing `group.plan_id` updates that
saved stop, keeping its trigger and session defaults; else a new saved stop
named after the line), **Remove** (confirmed inline → `deleteFlight`).
Delete / Backspace with the line itself focused asks the same. The canvas's
`onDropStop` is handed a function taking either a kind string (the old
canvas) or `{kind} | {planId}` (the round-11 canvas).

**TakeoffDialog** is the old flyway card's trigger section in a `Modal`:
Takes off (by hand / ticket / PR / issue / schedule), the schedule's days,
time and cron with the server's preview, Paused, and the repository, agent
and model its sessions start in; Save → `savePlan`, a refusal stays in the
dialog. `useUi.showFlyways(id)` (Schedule's Edit in the Sky, the palette, an
old `#flights/flyways/<id>` link) unfolds the shelf and opens this dialog
for that saved stop.

**A new session is on the sky at once.** Fly in the block writes the
optimistic `pending_create` row (`addPendingSession`) and calls
`onPending(guess)`: the sky pins that row's line where the block sat and
hides the block. The real title gets the same spot (the guess's entry is
forgotten if the server picked another title); a refusal brings the block
back with the error.

**Names**: the line picker, SkyView's messages, the drawer's perch rows and
the intake lane's started stubs show `windowName(title)`.

**Deleted**: `FlywayShelf.tsx/.css`, `FlywayCard.tsx/.css`,
`FlywayRow.tsx/.css`, `FlywayInspector.tsx`, `lib/flywayEdit.ts`,
`skyFlyways.test.ts`, `flywayRow.test.ts`. `FlywayInspector.css` stays: it is
the stop palette's and inspector's stylesheet, imported by `StopParts.tsx`.
The still-needed helpers moved to `lib/groundedLines.ts` (`checkRename`,
`NAME_MAX`, `savedStopOrder`).
### As built: round 11 selection

> Code: `frontend/src/lib/skySelect.ts` (the geometry and the rules),
> `components/flights/SkySelection.tsx` (+ `.css`: the bar, and every rule
> the selection draws), `SkyView.tsx` (the marquee, the keys, the group drag),
> `SkyLine.tsx` / `GroundedLine.tsx` (`selected`, `highlighted`),
> `FlywayCanvas.tsx` (`highlighted`). Tests: `skySelect.test.ts`.

**Pointer.** A left drag (mouse or pen) on empty sky draws the marquee
(`.sky-marquee`, viewport px, drawn after a 4px threshold, `MARQUEE_MIN`).
Panning is the wheel / trackpad, the middle button, a touch drag (a pinch still
zooms), Space+drag from anywhere and the arrows. A press on a line is the
line's, as before. HTML5 drags from the shelf never send a pointerdown to the
sky, so shelf drops are unchanged. A plain click on empty sky clears the
selection (Shift+click keeps it), and a double-click still opens the
new-session block. On `sky-narrow` there is no viewport, so no marquee.

**What is touched.** All measuring happens at release, in plane px
(`screenToPlane`: the viewport's corner, the camera's pan and zoom taken off):

- a line is its layout rect (`posOf` + its measured size) plus its title bar
  (`.skl-bar`'s rect). The line counts as **whole** when the marquee crosses
  its bar or covers at least half of it (`WHOLE_LINE = 0.5`). A session box and
  a grounded line's "No session yet" box are not heads: a sweep over the first
  stops clips them too easily;
- a stop box is any `[data-node]` with a `stop:` or `group:` id inside the
  line's slot (`.sky-line-slot[data-slot=<line id>]`), never one inside an
  open card's pane (`.skl-card`). Any overlap counts.

**The rule** (`select`): stop boxes of exactly ONE line and no whole line →
those stops (`stops: Map<line, Set<node id>>`). Anything else → lines (every
whole line plus every line a touched stop sits on; stops dropped). Never both.
Shift+drag adds under the same rule (`addSelection`: more stops of the same
line stay stops, another line's turn it into lines). Shift+click on a line's bar
toggles that line (`toggleLine`). Ctrl/⌘+A takes every line the sky shows (the
current view and Find filter). Esc clears the bar's question first, then the
selection. A line that leaves the board drops out (`pruneSelection`).

**Stops → steps.** `stop:<sid>` is that step. `group:<gid>` (or
`group:<gid>@<index>` for a later run of the same id) is its consecutive run
of sub-steps (`stepIndexesOf`). `selectedIndexes` sorts them and counts each
once. A group selected with loose stops therefore groups as all their steps.
`consecutive` is what Group needs.

**Showing it.** A selected line wears `.is-selected` (a dashed accent outline
5px outside its border; the focus ring stays on the border). A selected box
wears `.fl-box-selected` (FlywayCanvas's new `highlighted?: ReadonlySet<string>`
prop, beside the single `selected`). The sky passes both only to lines drawn
on the plane.

**The bar** (`SkySelectionBar`, `.sky-selbar`, screen-sized, inside the
viewport). It sits above selected lines (below them at the window's top) and
under selected stops (above them at the window's foot), so it is off the
line's own bar. It is hidden while lines are dragged. No
`window.confirm/prompt/alert`.

- **Lines**: "N lines selected", a ✥ hint (*drag a bar to move them*), **Tidy
  these** (each line's spot forgotten with `tidySkyLayout(l, id)` and its given
  spot freed, then the glide), **Remove…** (an `InlineConfirm` that says what
  happens to each: a session line closes its session with `closeSession`, as
  the ⋯ Close session does; a grounded line is deleted with `deleteFlight`; one
  toast for the grounded ones), **Clear**.
- **Stops of one line**: "N stops selected", **Group into a stop** and **Save
  to the shelf** (each asks a name inline, defaulting to "New stops", focused
  and selected; Enter submits) → `groupStops(flight, step ids, name)` /
  `savePlan(planFromSteps(name, steps))` with the steps in flight order,
  **Remove** (each box through `dropStop`, a group box through `dropGroup` on
  its first step; one toast "Removed N stops"; at once for up to 3 boxes, an
  inline confirm past that), **Clear**. Group is disabled with a hint
  (`groupHint`) for stops not side by side ("Pick stops that sit next to each
  other"), a stop that has flown or is flying, or a selection that already is
  exactly one group. The flight is a grounded line's own, or a session line's
  live flight from the flights query.
- After any action the selection clears; the API helpers refetch.

**Moving many.** The line drag keeps `{from: {id: Pos}, dx, dy}`: a drag
started on a selected line's bar carries every selected line, and on release
each is pinned where it lands (`onLayout(id, {x, y, pinned: true})`).

**Delete / Backspace** with a selection (focus on the sky, not a field, a box
or a pane) asks the bar to remove. The sky takes the key in the WINDOW's
capture phase, ahead of the app keymap's document-capture dispatcher, where
Delete is "close the focused session" (`lib/keymap.ts`). Without that,
selecting stops and pressing Delete closed the session instead. With no
selection, Delete on the sky still closes the focused session, as before.

### As built: round 11 style

The owner: "the tidy button and a few other buttons are not in the correct
style." Three were unstyled: **Tidy** (`.fl-btn sky-tidy`, and `.fl-btn` only
sets `white-space`), the line picker's **`.fl-chip`** (no rule anywhere), and
NewSessionBlock's **Cancel** (`btn-secondary`, a class that exists nowhere).
Each fell back to the browser's grey button. Every `<button` in
`components/flights` now wears one of the app's tiers, pinned by
`flightsButtons.test.ts`.

**The tiers** (the app's own rules; flights add only the focus ring):

| Tier | House rule | Rule |
|---|---|---|
| primary | `.btn-primary` (settings/screens/screens.css) | accent fill and border, `#fff` text (4.66:1), 12px/600, `5px 12px`, radius 6px; hover `brightness(1.08)`; disabled opacity .45. The ONE action a surface is for. |
| secondary | `.test-btn` (dialogs/SetupDialog.css) | `var(--panel-2)` fill, `1px solid var(--border)`, `var(--text)`, 12px, `5px 12px`, radius 6px; hover: the border turns `var(--accent)`; disabled opacity .4. |
| quiet-icon | `.tb-item` (TopBar.css), `.vf-menu > summary` | no fill, `1px solid transparent`, muted glyph (`--fl-ink-muted`), radius 6px; hover: `var(--panel-2)` + `var(--border)` + `var(--text)`; disabled .4. |

Shared by all three: `:focus-visible` = `outline: 2px solid var(--accent)`,
offset 1px (−2px inside a clipped group). Two forms of the secondary tier:
the **chip** (`.fl-pick`: pill, `var(--border)`, accent-mixed border on hover,
`.on` = accent border + 16% wash) and the **segmented group** (the top bar's
`.tb-views`: one bordered group, keys bare and muted, the chosen key
`var(--panel-2)` with a border). Menu rows are `.vf-menu-item`'s
(`5px 9px`, radius 5px, 12px/1.5, panel-2 under the pointer); links are
`button.linklike`. Dialog actions sit in `.modal-actions`, whose house rule
(NewSessionDialog.css: `8px 14px`, 13px, radius 7px) outranks the tier's size,
so a dialog's buttons keep the dialog size and take the tier's colours.

Tokens (FlywayCanvas.css, `:root` / `.light`): `--fl-btn-radius: 6px`,
`--fl-ctl-h: 28px` (the Sky toolbar's controls), and `--fl-ink-accent`: the
accent as TEXT, `color-mix(accent 62%, #fff)` on dark (≈6.7:1; the raw accent
is 3.4–4:1 on the dark panels) and `var(--accent-deep)` on light (7.2:1).

**What moved where:**

- Sky toolbar (Sky.css; SkyView.tsx unchanged): Tidy = secondary at
  `--fl-ctl-h`; the zoom group = a segmented secondary (Tidy's fill, border,
  radius and height around the group, 1px dividers, the readout a muted
  segment); All / Mine / Perched on you / This session = the `.tb-views` pair
  at the same height; Find = `.fl-input` at that height and radius. The sizes
  that SkyVisibility.css used to override live with the rules in Sky.css now.
  The line picker's `.fl-chip` = the chip; its Cancel `linklike` in
  `--fl-ink-accent`.
- Stops shelf (StopShelf.css/.tsx): every chip = secondary (panel-2, border,
  radius 6, 28px; accent border on hover); the **Session** chip is
  primary-tinted (accent border, 16% accent wash, 600 weight, the icon tile
  filled accent); a perch stays round and gold; a built-in stays dashed. The
  fold handle = quiet (a tab). The rename field is `.fl-input` at chip size
  (red border + ring when invalid). The saved-stop menu opens UP from the
  pointer (it fell off the window's foot).
- The line (SkyLine.css): ◧, the card's fold, the ⋯ summary = quiet with the
  ring; the corner handles take the panel-2 hover (they are `<span>`s with no
  `tabIndex`, so the ring rule waits for SkyLine.tsx to make them focusable);
  a box's right-click menu reads at the ⋯'s size inside a line.
- The canvas (FlywayCanvas.css, StopEditing.css): the hover toolbar's
  `.fl-tool` = quiet (26px, transparent border); remove / pause / resume
  hovers use the `--fl-ink-*` inks; the halt toggle's pressed state is a
  border, not an inset shadow; the gap + and the trailing + use
  `--fl-ink-accent` and ring on focus; the join pill is a status chip, not a
  button (unchanged). StopMenu.css = `.vf-menu-item`'s rows.
- Dialogs (StopDialog, TakeoffDialog, StopParts, NewSessionBlock): every action
  names its tier — `test-btn` (Remove, Duplicate, Done, Retry, Skip, Ungroup,
  Save to the shelf, Cancel / Close) or `btn-primary` (Save, Fly); Remove is a
  secondary with the red ink; the sub-step ✎ / 🗑 are `.fl-icon-btn`
  (🗑 red on hover); `.fl-icon-btn` itself gained the transparent border,
  the hover border and the ring; `.fl-toggle`'s label aligns left when it
  wraps.
- FlightStrip / SessionFlight / SkyDrawer: already `btn-primary` / `test-btn`
  (Done / Skip / Retry / Fly these stops / Fly); `.fl-btn`, `.fl-check-row`,
  `.fl-strip-line`, `.sky-perch-name` and the drawer tab gained the ring; the
  flights' `linklike`s (Open in the Sky, Intake ›, Refresh) use
  `--fl-ink-accent`.
