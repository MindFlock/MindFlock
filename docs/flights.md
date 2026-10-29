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
| **sky** | the open space where every flight is drawn: Flights' first tab, a pannable, zoomable plane | (UI only) |
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

Nothing is deleted. The three old surfaces become tabs of the Flights dialog
and their stores are untouched:

| Old surface | In Flights | Store (unchanged) |
|---|---|---|
| Intake lists (tickets / PRs / issues ready to start) | **Sky → the intake lane and the drawer's Waiting to fly**: each eligible item is a flight that has not taken off, under its kind's heading with a kind badge, a **Fly** button and the flyway as chips | pipeline `state.json`, settings sources |
| Intake source cards + Auto-start switches | **Sources** tab (the existing tab components, mounted as they are) | settings |
| Verify checklists | a **Verify** stop on a flyway; the checklists list is the **Checklists** tab; open checklist items perch the flight (the Sky's drawer lists it) | `test_plans.json` |
| Schedule (templates) | **Schedule** tab (the existing templates view); a flyway may also carry its own schedule trigger | `session_templates.json` |
| Fast-track depth (off→commit→push→pr→merge) | the **ship** stops of a flyway; a per-source depth still works and maps to the plain ship ladder at that rung (the agent, then commit → … → the depth — nothing else); an armed ⏩ is drawn as rung boxes on the session's line ([The session diagram](#the-session-diagram)) | `autopilot.json` |

The top bar shows **Flights** where Intake / Verify / Schedule were. `Alt+I`
opens Flights (the Sky), `Alt+V` opens it on Checklists, `Alt+S` on Schedule.
The badge counts what waits on you: flights perched on you, plus Verify's
due/failing checklists, plus items waiting to fly that will not auto-start.

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
editor (and the same `GET /api/templates/cron-preview` previews) as the
Schedule tab, and the same rules (`core/template_schedule.py`): a schedule
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
themselves sync). The Schedule tab keeps listing templates.

### Verify steps

A verify step asks for the session's checklist through Verify's own button
path and parks the flight on you while human items are open. Ticking them off
in the Checklists tab moves the flight on at once (`session.test_plan_settled`
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

**Top bar.** One entry, **Flights** (the bird before the word; replaces
Intake / Verify / Schedule; the palette keeps "Intake…", "Verify…",
"Schedule…" entries that open the matching tab). Badge = flights perched on
you + Verify due/failing + items waiting to fly that will not auto-start.
Tooltip lists the three counts.

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

**Dialog** `#flights-dialog` (`frontend/src/components/flights/`, CSS prefix
`fl-`), tabs:

1. **Sky** — the open expanse ([The Sky](#the-sky)); it replaced the Board
   (`board` / `#flights/board` still land here) and the Board's groups live
   on in its drawer. On this tab the dialog takes the whole window
   (`.sky-full`).
2. **Flyways** — the editor (tab id `plans`). Left: the flyway list
   (built-ins marked). Right: the flyway on the canvas, the stop palette, the
   inspector for the selected stop (its fields in the controls above, `on_fail`
   as chips), the trigger (by hand / ticket / PR / issue / schedule with its
   days as chips and its time, date or cron as fields), and the session
   defaults for by-hand and scheduled flyways. **Describe a stop…** under the
   canvas sends a sentence to `/api/flights/describe` and appends the result,
   highlighted until saved. **Fly this flyway** starts a flight of it right
   away (an inline row asks for a repository or an open session when the
   flyway has none).
3. **Sources** — the existing Intake tab components (Tickets / Pull requests /
   Issues source cards and switches), mounted unchanged, each card with its
   **Flyway** chips.
4. **Schedule** — the existing templates view, mounted unchanged.
5. **Checklists** — the existing Verify list, mounted unchanged.

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
Sky** opens the Sky on it, and the tab's own **Open in the Sky** does the same
(`#flights/sky/<title>`). The bird stands on the line's active box. With
nothing on the line it also offers the flyways as cards (**Fly this flyway**,
built-ins first, the stops as tiny boxes) and **Describe a flyway…** (describe
`want=plan` → `POST /api/flights/plans` → `POST /api/flights/start` with this
session's `title`).

**Rail.** The session row's chip shows `Perch: <stop title>` in the needs-you
gold when a flight is perched on you (via `deriveSessionState`: a perch is a
Needs-you state, in the bell, where its row reads `Perched on you: <stop>`).

**Starting opens the session.** Every start (Waiting to fly's Fly, Fly this
flyway, a template run, a manual flight) ends in `selectSession(title)` as
soon as the row exists, the way `templateRun.ts` already does.

**Phone.** No canvas: a session perched on you gets a card with the bird,
`<flyway> · Perch: <stop>`, and **Done** / **Skip**.

**Tours.** A short `flights` CoachTour (`lib/tours/flights.ts`): the sky
(`data-tour="flights-sky"`), the bird (`flights-bird`), + New session
(`flights-newblock`), Flyways, Describe a stop.

### The Sky

Flights' first tab (`components/flights/Sky.tsx`, the pure rules in
`lib/skyLayout.ts`) — the big open expanse where every session floats:

- **One plane**, panned and zoomed as a whole: one `translate(x, y)
  scale(z)` container, no library. Drag on empty sky pans (mouse and touch),
  the wheel scrolls, Ctrl/⌘+wheel and a pinch zoom (0.4×–2×) about the
  pointer, Space+drag pans from anywhere, 0 fits; the toolbar has the zoom
  readout, **Fit** and **100%**. A faint dot grid scales with it, in both
  themes (a white expanse in the light one).
- **Every session is a line**: its [session diagram](#the-session-diagram)
  (the same canvas, live, with its + / palette / inspector — `useSessionLine`,
  shared with the pane's Flight tab) under a title bar (name, state chip, ⋯:
  Open session · Show only this line · Fly a flyway… · Close session) that
  drags it anywhere. A dragged line's spot is remembered per device
  (`localStorage` `mf_sky_pos`, `{title: {x, y}}`); the rest are auto-placed
  by `placeLines(lines, saved, area)` — rows, newest at the top, never
  overlapping. A new session appears on the next poll; a closed one's line
  fades out. Hidden helpers (`hidden_by`) are never drawn.
- **The bird** (`birdPerch(diagram) -> {nodeId, tone} | null`, the canvas's
  `bird` prop): on the flight's current stop, else the fast-track's current
  rung, else the session box while its agent works (or asks you); none when
  it idles with nothing armed. It bobs while working, sits gold and still
  perched on you, red halted, and hops (a left/top transition) to the next
  box.
- **Build from the space**: a double-click on empty sky, or **+ New
  session**, opens a `NewSessionBlock` card there (`{x, y, plans, onCreated,
  onCancel}`) that creates the session through the New dialog's path, then
  — a flyway picked — `POST /api/flights/start {plan_id, title}`; the new
  line takes the card's spot.
- **Views** (toolbar chips): **All** (every session, workers on their own
  lines), **Mine** (default: top-level sessions, workers folded into their
  parent's line), **Perched on you**, **This session** (one line, fit —
  `#flights/sky/<title>`); a find field filters by name or task.
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
- **Fly this flyway** on a flyway with no `session.repo_path` (or a ticket /
  PR / issue flyway) opens an inline row in the editor — a repository field
  with Browse… and the open sessions as chips — rather than the New dialog; it
  posts `repo_path` or `title`.
- **A waiting item's flyway chips** send `plan` only when one is picked: the
  default chip ("Default · <built-in>", which the server cuts at the source's
  depth) leaves the server's own resolution in charge. The built-in it names
  is `builtin:<kind>` (ticket / pr / issue) when the list has it, else
  `builtin:ticket`. A row's chips start on its source's `plan` when one is
  set; picking the default chip there then sends the built-in's id, since ""
  would mean the source's flyway.
- **The source cards' Flyway chips** (Sources tab) write `plan` with the
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
- **Badge.** A flight perched on a `verify` stop is counted once, as Verify's
  due checklist, not again as a flight waiting on you.
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
| Frontend | `frontend/src/components/flights/` (Sky + SkyDrawer = the Sky, IntakeLane, NewSessionBlock, FlywayCanvas, SessionFlight, useSessionLine, PlanEditor = the Flyways editor, StopParts, controls, Bird, useWaiting), `frontend/src/lib/flights.ts`, `flightDiagram.ts` (the canvas's model, the session diagram and `birdPerch`), `skyLayout.ts` (the Sky's placement, camera, views), `flightsApi.ts`, `tours/flights.ts`; the pane's Flight tab in `components/grid/Pane.tsx` |
| Tests | `tests/unit/test_flights*.py`, `frontend/src/__tests__/flights*.test.ts`, `flightDiagram.test.ts` |
| CLI | `mindflock flights` (list), `mindflock flight flyways` (alias `plans`), `mindflock flight start <flyway> [--repo …] [--title …]`, `mindflock flight done\|skip\|cancel <id>` (`backend/cli.py`) |
