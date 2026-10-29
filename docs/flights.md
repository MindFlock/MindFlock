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
{"id": "s1", "kind": "agent", "title": "Work the ticket", "config": {...}, "on_fail": "halt" | "agent" | "skip" | "you"}
```

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
| `agent` | robot | `prompt` (text; placeholders `{ticket}`, `{pr}`, `{branch}`, `{subject}` filled by the server), `fresh` (bool: start a new session for this step instead of reusing the flight's) | the session's turn ended and settled (same gates as autopilot's `_agent_settled`: idle ≥30s, nothing queued, no background job, not usage-limited; plus `worked_at` evidence) |
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
  "state": "running" | "waiting_you" | "paused" | "halted" | "done" | "cancelled",   // paused = held by you (Resume carries on); halted = stopped on a failure (retry / skip / resume)
  "cursor": 0,                    // index into steps of the current step
  "step_states": {"s1": {"state": "pending"|"running"|"waiting_you"|"done"|"skipped"|"failed", "since": 0.0, "note": "", "detail": {}, "rounds": 0}},
  "reason": "",                   // why halted / cancelled (human-readable)
  "history": [{"at": 0.0, "step": "s1", "event": "started"|"done"|"failed"|"skipped"|"you"|"halted"|"paused"|"resumed"|"jumped"|"added"|"edited"|"removed", "note": ""}],  // ≤200
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
| `PATCH /api/flights/{id}/steps/{sid}` | `{title?, config?, on_fail?}` | `{"flight"}` — only a pending step after the current one (409 `in_progress`); validated like a plan step. History `edited`. |
| `DELETE /api/flights/{id}/steps/{sid}` | | `{"flight"}` — only a pending step after the current one (409 `in_progress`). History `removed`. |
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
- **Paused vs halted.** Pause sets `state: "paused"` (`reason` stays empty;
  history event `paused`) on a running or waiting flight — a halted one stays
  halted. A paused flight is not driven. **Resume** goes back to `running`, or
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

- **Steps changed in the air.** The three step routes are pure operations
  (`flights.op_add_step` / `op_edit_step` / `op_remove_step`) run through the
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
  flight) advances instead of landing.
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

> To fold into `docs/flights.md` ("The Sky"). Source:
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
