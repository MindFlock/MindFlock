# Flights — one route from waiting to finished

> How routes work — for people using them (start at [Using Flights](#using-flights))
> and for code. "Flights" is the feature's internal name (the `/api/flights/*`
> routes, `flights.py`); on screen it is a **route**. The sections marked
> *contract* are what the backend, the frontend and the integration are built
> against; change them only by editing this file first.

## Vocabulary

The words a person reads are decided in [vocabulary.md](vocabulary.md)
(2026-11-03), which replaces the table that stood here (2026-11-02). Internal
names stay as they are: types, ids, CSS classes, the `/api/flights/*` routes,
`plans` and `steps` on the wire, store keys, the `#flyways` deep link and the
hook URL `/api/hooks/flyway/`.

The sections after [Using Flights](#using-flights) record each round in the
words of its day. Read them with this key: a *flyway*, *plan* or *saved chain*
is a **saved route**; a *flight*, *line* or *chain* is a **route** (running on
a session, or on the Sky with no session); a *block*, *tile* or *step* is a
**stop**; a *perch* is a **Wait for me** stop and *perched on you* is **Needs
you**; *Fly* / *Take off* is **Run**; *Fast-track* (the control) is **Goes as
far as**; a *checklist*, *test plan* or *behaviors* is the session's **Verify
steps** (its agent's own steps since 2026-11-10);
*Checks* is the **CI checks** stop; *Land* is the **Finish** stop and the
*landing strip* is **Finished**.

## The idea

MindFlock had three top-bar surfaces that were really one thing seen from
three sides:

- **Intake** — work *arrives* (tickets, pull requests, issues) and becomes a
  session.
- **Verify** — the session's own agent *checks its work*, and a person confirms
  what only a person can.
- **Schedule** — work *starts by itself* on a timer.

Each of them was one step of the same journey, and the journey itself was
nowhere: once a ticket had become a session, nothing in the app knew whether
it was meant to be tested in the sandbox, wait for reviewers, answer the
CodeRabbit comments, or stop and ask before merging.

A **route** is that journey, drawn as a row of stops:

```
Ticket → Agent works → Commit → Push → Verify → Open PR → Reviews settle → CI checks → Wait for me: approve → Merge when green → Ticket: Done
```

| Word | Is | On the wire |
|---|---|---|
| **route** | the row of stops one piece of work goes through — one ticket, one PR, one scheduled job, or something you started by hand | a *flight* |
| **saved route** | a route kept on the shelf to reuse (Ticket to merge, Nightly chores, …) | a *plan* (`plans`, `plan_id`, `FlightPlan`) |
| **stop** | one box on a route: a *robot* stop the server carries out by itself, or one that asks a person | a *step* |
| **Wait for me** | a stop that waits on you: the route pings you and carries on when you press **Done** | a step of kind `you` |
| **Needs you** | everything waiting on a person — the bell's word | step state `waiting_you` |
| **Sky** | the open space where every route is drawn: the main area's second view beside the Grid (the same sessions), a pannable, zoomable plane | (UI only) |

The bird flavour stays where it explains itself: the small bird that moves
along a route to show where the work is (never ✈), the Sky, and "takes off"
as a verb in a sentence. Only the words changed: every field and route keeps
its name (`plans`, `steps`, `/api/flights/...`).

Every route that is running has exactly one session (a worktree + an agent),
and the Sky draws every session with its route, a bird on the stop it is at.

### What stays, what folds in

Nothing is deleted, and nothing is nested: Intake and Schedule stay the flat
top-bar dialogs they always were, their stores untouched, and each carries its
side of the journey. Routes are drawn in the Sky.

| Surface | Its part in a route | Store (unchanged) |
|---|---|---|
| Intake lists (tickets / PRs / issues ready to start) | **Sky → the intake lane and the drawer's waiting items**: each eligible item is a route that has not started, under its kind's heading with a kind badge, a **Run** button and the saved route as chips | pipeline `state.json`, settings sources |
| Intake source cards + Auto-start switches | the **Intake** dialog, each source card saying which saved route its items take | settings |
| Verify | a **Verify** stop on a route: the session's agent verifies its own work; steps only you can do make the route **Need you** until you confirm (the Sky's drawer lists it) | `test_plans.json` |
| Schedule (templates) | the **Schedule** dialog; a saved route may carry its own schedule trigger, and Schedule lists those beside the templates | `session_templates.json` |
| Goes as far as (Agent only · Commit · Push · Open PR · Merge when green) | the **ship** stops of a route; a per-source setting still works and maps to the plain ladder at that rung (the agent, then Commit → … → that rung — nothing else); an armed session's rungs are drawn as stops on its route ([The session diagram](#the-session-diagram)) | `autopilot.json` |

The top bar is **New · Grid / Sky · Intake · Schedule · Settings** (the
Grid / Sky toggle only on a phone-narrow window; on desktop it sits on the
sidebar's View row): `Alt+I` / `Alt+S` open the dialogs, `Ctrl+Y` (⌘Y)
switches to the Sky (the main area's other view). The Sky half of the toggle
counts the routes that need you. Saved routes are made, edited and run from
the Sky's shelf. (Sky rounds 4–7 put Intake / Verify / Schedule behind one
Flights button as tabs of a Flights dialog; round 8 retired it — see
[Round 8: flat again](#round-8-flat-again).)

## Using Flights

### On the Sky: build, run, and a stop that fails

- **Build a route from nothing.** Right-click empty sky and type in **Add a
  stop…** (Enter places it where you clicked), drag a chip from the
  **Stops** shelf at the bottom, or open **All stops…** for every kind and the
  **saved routes** (Ticket to merge, Nightly chores, …) at its foot. A route
  with no session yet shows under **All** and **Mine**. **This session** (the
  default) shows one session's route only: with no session at all, placing a
  stop moves the view to All; otherwise a toast says where it went, with
  **Show all**.
- **Run it.** A route on a session runs its stops in order; the bird shows
  where the work is, and a stop with a ✓ is done.
- **Wait for me** stops the route until you press **Done** (or **Skip**) on
  the stop or in the bell; the route then **Needs you**.
- **A stop that fails halts the route** (its *If it fails* option, Halt by
  default): the route is **Halted** — the stop turns red with the reason, and
  offers **Retry**, **Skip** and **Let the agent fix it**.
- **Paused** is a person's hold: **Pause** (the stop's menu, the bird) stops
  the route where it is, and **Carry on** picks it up. A **halt point**
  (**Add halt point** / **Remove halt point** on a stop) pauses the route when
  it reaches that stop, before doing it ([Halt points](#halt-points)). A paused
  route offers Carry on and Skip right on its stop and in the pane strip; one
  paused at a halt point is in Needs you as "Paused at <stop> — Carry on" (a
  pause you made yourself is not).
- **Every stop that waits says why**, in plain words, with an action that
  works: the stop's own reason ("2 steps for you to check, then confirm"),
  "Failed: …" on a failure handed to you (Retry, Let the agent fix it, Skip),
  and the pane's route strip leads with "Halted · <why>", "Needs you · <what>"
  or "Paused". A stop's dialog shows the reason first, then the remedies (the
  one that usually works first), then its settings folded under **Its
  settings**.
- **Edit the stop the route is on** when it failed, or when the route is
  paused before it with nothing run yet: Save, then Retry or Carry on runs it
  with the new settings ([Flow polish](#flow-polish-2026-11-10-as-built)).

### How a ticket gets its route

Every way work arrives ends in the same start path, and every one of them can
put the new session on a route:

- **Intake / the Sky's waiting items** (its intake lane and drawer) — **Run**
  on a ticket, pull request or issue (`POST /api/tickets/start`,
  `/api/github/prs/review`, `/api/github/issues/start`, or
  `POST /api/flights/start` with a `subject`, which runs the very same code).
  The saved-route chips send `plan`; leaving them on the default leaves the
  choice to the server.
- **The pipeline** — a ticket, PR or issue it auto-starts.
- **An agent** — `spawn_ticket_session` from another session.

Which saved route (`plan`), in order:

1. the start's own `plan`;
2. a **Goes as far as picked for this start** (the row's pick, a team run's
   "off", an agent's `autopilot=`) — the built-in saved route cut at that
   rung;
3. the **source's saved route** — Intake's source card
   (`TicketingSource.plan`) or the repo card
   (`github.repo_settings[repo].plan` for PRs,
   `github.issue_repo_settings[repo].plan` for issues), while it exists;
4. the built-in saved route for the kind **cut at the source's Goes as far
   as** — so a source set to go as far as Open PR runs *Agent works → Commit →
   Push → Verify → Open PR*;
5. **no route** — a source with no saved route that goes no further than
   Agent only starts its sessions exactly as it always did.

The route is armed on the session's title **before the session exists** (an
intake title is deterministic), so the item shows on its route in the Sky and
in the sidebar's provisioning row from the first second, and survives a server
restart mid-launch. Its first agent stop is the session's first prompt — the
ticket / PR / issue prompt Intake always built — with the stop's own text (if
any) appended. The route's ship stops arm Goes as far as themselves, so the
source's setting is not armed beside it (unless the route has no ship stops).
A start whose session can't be created stops its route with the reason.

### Scheduled saved routes

A saved route that **Starts: Schedule** starts itself, like a template: daily
or weekdays at a time, a cron expression, or once at a date and time — the same
editor (and the same `GET /api/templates/cron-preview` previews) as
Schedule, and the same rules (`core/template_schedule.py`): a schedule
counts from when it was set, after downtime only the most recent missed run
fires, a paused trigger fires nothing, and in a group of devices only the
automation device fires. Each run starts like **Run** — a new session from the
saved route's session defaults — recorded with `by: "schedule"`. Scheduled
saved routes fire from the **templates addon's schedule loop**
(`template_schedule.run_loop`, beside the templates' own schedules), so they
stop with it: a server with that addon disabled fires neither.

A run is **skipped** (and says so in the saved route's history, with a refetch
of the Sky) while its previous route is still running or its session is
working, asking a question or at a usage limit — a schedule never stacks
routes. A run that can't start (no folder set, the agent not installed) is a
failed run with the reason. The saved-route list (`GET /api/flights/plans`)
shows a scheduled one's next run, last run and history under `runs`; the
record is `~/.mindflock/flights/schedule_runs.json`, on this device only (the
saved routes themselves sync). The Schedule dialog lists templates and these
saved routes — with that record (next run, last result, history), **Run now**,
**Schedule…** (an unscheduled one) and **Edit** (opens it in the Sky).

### Verify stops

A Verify stop is **self-verify** (2026-11-10): it sends the session's own
agent ONE prompt — "verify the work you just did" — and the agent comes up
with its own concrete test steps (real usage, not the unit tests it already
ran), writes them to its results file, puts them on its todo list and works
through them. A step only a person can do, it **tells the person** in its
reply (what to do, what to look for) and records as `actor: "human"`. No
steps are written beforehand: starting the stop finds or makes the session's
plan (an empty one, nothing generated) and starts the check.

The stop follows the check's outcome — the same **Verify pill** the session's
row shows (`verify` on `/api/instances`, docs/web-api.md):

| Outcome | The stop |
|---|---|
| `verifying` | waits ("the agent is verifying its work · 3 of 7 answered"); the stall nudge and the budget close below apply |
| `needs_you` — there are steps only a person can do (or an agent step it could not settle, or `who: you`) | parks the route on you and keeps saying why ("2 steps for you to check, then confirm"; `detail.ask: "confirm"`, `items` = the steps with what to look for, and for a step the agent left, "— the agent couldn't: <why>"). It **never passes silently**: it moves on when a person confirms — `POST /api/test-plans/{id}/confirm`, or **Done** on the stop, which here is **Confirm verified** |
| `passed` — every agent step passed, nothing for a person | done |
| `failed` — an agent step failed, or a person confirmed "it doesn't work" | fails with each failed step and what the agent saw ("Failed · 2: login works — saw: 500 on /login; logout works", evidence clipped to 160 characters) — **halts** the route by default (Retry · Skip · Let the agent fix it) |
| `blocked` — the check stopped before it finished (closed for a stall or the budget, its window gone, no steps written) | parks on you with the reason (`ask: "blocked"`): **Retry** (a new check) or **Skip**; Done there also checks again |

With `ask: "end"` ("Round 21") a `needs_you` outcome does not park the
route: the stop finishes, the checks are owed, and the route asks for them
once, at its end.

Only a check from **this attempt** counts: a Retry (or the agent's fix)
starts a new one, and a check already running when the stop arrives (started
from the session's own Verify) is waited on, never restarted.

The stop carries four plain choices and a checkbox; its face reads them as one
sentence, **"Verify on *where* · *who* · *ready*"** — e.g. "Verify on sandbox ·
agent, then you · 15 min after the stop before", "Verify on this branch · agent
only · right away", "Verify on live · agent, then your sign-off · once on
release". Its form opens with the same choices read back as one plain sentence
("Its agent verifies on this branch as soon as the route gets here; you confirm
the steps only a person can do; …"), which changes as you change them.

- **Where** (`where`) — `branch`: this branch's own code, a fresh checkout of
  the plan's commit, no deployment; or the **name of one of the repo's
  environments** (the editor picks from the repo's list; a saved name the
  repo doesn't have shows as "<name> (unknown)" with a warning) (`repository.verify_repo_settings["owner/name"].environments`:
  `{name, reach, branch, deploy_minutes}` each). The repo's older fields
  (live branch, deploy delay, target) are an implicit environment named
  `live`. A name is lowercased and must match `[a-z0-9_-]{1,40}`. A name
  the repo does not have fails the stop: "no environment named *x* in this
  repo". When the environments **cannot be read** (the lookup raises), the
  stop does **not** fail: it goes on as if nothing were known about the
  environment — `ready_minutes` gets no `deploy_minutes` fallback, and
  `on_branch` waits on the checklist's own `live` mark (the repo's live
  branch), whatever `where` names.
- **Ready when** (`ready`) — when the check may start:
  - `now` — right away.
  - `on_branch` — once the work has **reached the environment's branch**
    (`test_plans.env_reached_at`: the first time the plan's commit is seen on
    `origin/<branch>`), then `ready_minutes` more (0 = the environment's
    `deploy_minutes`). On `where: branch` it reads as `now`.
  - `after_prev` — `ready_minutes` (0 = the environment's `deploy_minutes`)
    after this stop started (`step_states[sid].since`), which is normally
    when the stop before finished. A halt point or a pause in between adds to
    the wait; it does not count towards it. With 0 minutes and no
    `deploy_minutes` (always so on `where: branch`) there is no wait at all,
    although the face still reads "after the stop before, once this branch
    has deployed".

  `ready_minutes` (0..1440) is ignored when `ready` is `now`.

  The gate holds the check itself: the agent is not asked to verify until
  the environment is ready. The face says why it
  waits: "waiting for the work to reach sandbox", "12 min for sandbox to be
  ready".

  *Why `after_prev` exists:* a protected `sandbox` branch that only takes
  squash-merged PRs never carries the plan's own commit — the squash makes a
  new one — so "reached the branch" never comes true. A route like *Push →
  Open PR → Merge into sandbox → Verify on sandbox, ready `after_prev`, 15
  min* checks the sandbox a quarter of an hour after the merge stop finished,
  which is when its deploy has landed. Use `on_branch` when the branch takes
  the commits themselves (a fast-forward or merge-commit release branch).
- **Who checks** (`who`; the ids stay, the words changed 2026-11-10) —
  `both` **"Agent, then me"** (default: the agent does its steps; the route
  waits on you only for the steps a person must do), `agent` **"Agent only"**
  (the run has `walk: "all"`: the agent attempts every step itself with its
  own tools and must give evidence for each answer; the route waits only if
  something ends blocked or failed), `you` **"Agent, then my sign-off"** (the
  agent verifies, then the route always waits for your confirm — the run
  route's `confirm: true` — even when every step passed).
- **Notes** (`notes`) — text for this check only, on top of the repo's
  standing notes; handed to the agent's check.
- **Post the results to the PR** (`post_to_pr`) — when the stop finishes
  (done **or** failed) the driver comments `test_plans.results_markdown(plan)`
  on the session's PR, once per attempt (`detail.pr_comment` says what
  happened: `posted`, `skipped: …`, `failed: …`, and a `noted` history entry
  says it too: "posted the results to the PR" or "results not posted to the
  PR (<reason>)"). No checklist, no PR or no GitHub token is a `skipped`; a
  posting error never fails the stop. The checker itself never posts — it
  only writes its results file.

A bad `who`, `where`, `ready` or `ready_minutes` is a 400 `bad_step` when a
plan is saved; a stored config that is already bad falls back to the
defaults.

**If it fails** on a Verify stop: Halt (red, with the failed steps: Retry,
Skip, Let the agent fix it), **Let the agent fix it** (the failed steps and
their evidence go back to the session's agent, which verifies again — up to
2 rounds, then the route halts), Skip, or Wait for me (the route waits on you,
showing the failed steps).

**Migration from `wait_live`.** An old `wait_live` reads as where/ready (true
→ `live` + `on_branch`; false → `branch` + `now`) and is never written back:
stored plans and flights lose the key the next time they are normalised.
Plan JSON sent through the MCP tools, the CLI or the API should use
`where` / `ready`.


#### Verified in the session that wrote the work

When the session that wrote the work is still open (the plan's own session,
or another window on the same repo + branch), the check runs **in that
session**: the run route hands its own agent the self-verify prompt (it
starts `MindFlock Verify: verify the work you just did …`). The agent writes
`{"plan", "finished", "steps": [{id, text, expect, actor, result?, note?}]}`
to its worktree's git-excluded results file (`.mindflock_verify.json`),
rewriting it after every step; the server adopts its steps as the plan's
(`test_plans.adopt_reported_steps` — the plan's generated steps are dropped
when the check starts, a person's own are kept and handed to it) and folds
its answers in. With `"finished": true` the outcome settles: any agent
`fail` → `failed`; otherwise any step without a `pass` (a person's step, an
agent step it answered `blocked`) → `needs_you`; no steps at all →
`blocked`; else `passed`. Nothing is created and nothing is checked out: for
`where: branch` the code under test is the session's branch as it stands
(its HEAD when the check started, or a descendant — a HEAD that no longer
contains it has its passes downgraded); for an environment the deployment is
under test and the tree is only read.

A session mid-turn gets the prompt on its prompt queue (never typed over a
running turn); a sleeping one is woken through the launch gate, like any wake
nobody asked for (low memory: the prompt waits on the queue and the wake
waits for memory — queued, not refused; the close notice below wakes the
same way); one whose agent is not running is relaunched by the queue's
drain. The stop's stall and budget clocks start when
the agent picks it up, not while it waits on the queue. A paused session
refuses it (409 `reason: "send_failed"` → the stop fails with the sentence).
The run record carries `in_session: true`, `self: true` and `session` = that
session; the stop's detail carries `in_session` / `walk_session`, and the pane
strip says **Its agent is verifying its work — open the session to talk to
it**. A nudge goes
to that agent; the stop closing the check (stall, budget) tells it the check
is over, and nothing that ends a run — that close, Cancel, Delete — **ever
closes the session**.

A check that ends **before the agent's steps were adopted** (the hand-off
refused, Cancel, the two-hour give-up, the session deleted, a close) puts
the plan's previous steps back, so the checker fallback below still has
them. Any such end settles the run `blocked` with its reason, stamped at the
moment it ended (the pill shows for 10 minutes from then). It is never
removed: removing it would bring back the check before it, and a
`needs_you` there would be confirmed against this run's steps. One plan
starts one check at a time: a second Run while one is starting or running
answers 409 `reason: "busy"` (a stop waits on the running check), and the
liveness pass never releases a check in flight.

Only when the plan's session is gone (closed or deleted) does a separate
`verify-<plan>-<sha7>` checker session walk the plan's **written** steps, as
before — the server prepares its checkout, and the memory guard can refuse it
(below). That fallback is the one path that still needs steps written
beforehand (the one-shot author behind Verify's "Write behaviors" button and
`verify_on_push`); it settles the same outcomes.

#### The walkthrough (`walk`, `expect`, `needs`)

*(The fallback checker's steps.)* A written checklist is a
**walkthrough**: ordered steps the checker (the *walker*) performs itself. Each step is `{id, text, actor, expect?, needs?}` —
`text` is what to **do** (the walker triggers the behaviour itself: an API
call, a Slack slash command via the API, an HTTP request, a CLI against the
deployment, a DB query, browser automation — never "after someone has done
X"), `expect` what it must **observe** for a pass, and `needs` the ids of
earlier steps whose action this one relies on. The run prompt prints each step
as its text, then `Expect: …` and `Needs: sN`; the walker goes in order, and a
step whose needs failed or were blocked is answered `blocked` "needs sN".

A run has a **walk** mode (`POST /api/test-plans/{id}/run` body
`{"walk": "agent" | "all"}`, default `agent`, anything else a 400; stored as
`run.walk`):

- `agent` — today's run: only `actor: "agent"` steps are the walker's; any
  answer it gives a person's step is stored `blocked`.
- `all` — the walker also attempts the person's steps with its tools. It may
  settle one **only with evidence**: `pass`/`fail` with a non-empty note
  saying what it observed and how. Such an answer is stored
  `{"result", "note", "at", "by": "agent", "walked": true}`. Without evidence
  it is refused (`blocked`, still yours); a step it cannot do it answers
  `blocked` with `needs you: <why>`, which parks on you exactly as below. A
  plan of person's steps alone is not refused (`human_only`) in this mode,
  and `steps: [...]` may name a person's step.

A Verify stop with `who: "agent"` starts its run with `walk: "all"`; `both`
keeps `agent`, then you.

#### When the check can't go on (never silently)

Found live 2026-11-09: a check the memory guard refused read as running
forever, a checker that ended its turn with a background job and no answers
read as "checking" for two hours, and a run that answered every behavior
"blocked" passed as "checked". A Verify stop now always says why it isn't
moving, and comes to you when only you can move it:

| What happened | What the stop does |
|---|---|
| The memory guard refused the checker's session (the run route answers 409 `reason: "memory"`; only a separate checker — a walk in the session's own agent creates nothing, so it is never refused for memory) | waits — "waiting for memory to start the check (<why>)", plus "· waited 3 min" once a minute has passed — and retries every 30 s; **after 5 min** it parks on you (`ask: "memory"`): free memory then Retry, or Skip |
| It believes a check started, but no run exists 60 s later | fails: "the check never started — Retry to start it again, or Skip"; a run that was released (its window died) and kept its reason parks on you instead (`ask: "blocked"`, Retry / Skip) |
| The check's record can't be read | parks on you at once: "the check couldn't be read: … — Retry to verify again, or Skip" |
| The check's record is gone | fails: "the check's record is gone — Retry starts a new one" |
| A check is running | says progress: "the agent is verifying its work · 3 of 7 answered · quiet 6 min" (the agent rewrites its results file after every step) |
| No new answer for 8 min and the checker's turn is over | nudges it once: write the file, mark the rest blocked, stop background jobs |
| Still nothing 15 min after the nudge, or past the 30-minute budget (+5) | closes the check: answered steps keep their answers, the rest become `blocked` ("not checked: the agent stopped answering" / "not checked: the check ran out of time (30 min)") |
| The check finished with steps only a person can do, or agent steps it answered `blocked` (`needs_you`) | parks on you: "N steps for you to check, then confirm". **Done = Confirm verified** (`test_plans.confirm_verify`); "it doesn't work" goes through the confirm route with `verified: false` and fails the stop. **Never** counted as a pass, whatever `who` says ("Agent only" too) |
| The check was closed, its window died, or it wrote no steps (`blocked`) | parks on you with the reason: Retry or Skip (Done checks again) |

**What Retry and Done do on a parked Verify stop.** Retry is accepted on a
Verify stop parked with `ask: "blocked"` or `"memory"` (it starts a fresh
check), and so is Done, which means "I sorted it — check again". On a check
waiting for your confirm (`ask: "confirm"`) Done is Confirm verified. Done is
refused anywhere else on a Verify stop waiting on you — 409
`answer_behaviors` "<stop> isn't verified yet — Retry to verify again, or
Skip" — because it would pass steps nobody checked. A Verify stop whose
*failure* was handed to you (`on_fail: you`) takes Done: that answers the
failure, not the check.

### The CLI

```
mindflock flights [--all]                 # routes running: saved route, subject, session, state, stop
mindflock flight plans                    # the saved routes (scheduled ones say when next); `flight flyways` = the same
mindflock flight start ROUTE [--repo PATH] [--title SESSION] [--at STOP|resume]
mindflock flight again ROUTE [--at STOP] [--title SESSION]   # an ended route, from where it stopped
mindflock flight done ROUTE [--note …]    # the Wait for me stop that needs you; also skip / cancel (Stop the route)
mindflock flight retry ROUTE              # run the halted stop again; also resume (Carry on) / pause
mindflock flight jump ROUTE STOP          # Jump here: STOP is a stop's id, number or title
```

For `start`, `ROUTE` is a saved route's id or name (a unique prefix is
enough); for the others it is a route id (prefix) or its session's title.
Under its table, `mindflock flights` prints each route that waits on you, is
halted or is paused, with the stop's reason and the commands that work from
there (`mindflock flight retry f_1a2b3c`, …). See [cli.md](cli.md).

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

A Verify stop's config, for example (see [Verify stops](#verify-stops)):

```json
{"id": "s5", "kind": "verify", "title": "Verify on sandbox", "on_fail": "halt",
 "config": {"where": "sandbox", "ready": "after_prev", "ready_minutes": 15,
            "who": "both", "notes": "log in as the qa user", "post_to_pr": true}}
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
| `verify` | robot (+you) | `where` (`branch` or an environment's name, default branch); `ready`: `now` \| `on_branch` \| `after_prev` (default now); `ready_minutes` (0..1440, 0 = the environment's deploy minutes); `who`: `both` \| `agent` \| `you` (default both); `notes` (≤2000 chars); `post_to_pr` (bool, default false) — see [Verify stops](#verify-stops); a legacy `wait_live` true reads as `live` + `on_branch` | the session's self-verify `passed` (or a person confirmed it); `needs_you` parks the flight on you until confirmed, `failed` fails the stop, `blocked` parks on you (Retry / Skip) |
| `you` | you | `text` (what to do), `items` (list of strings, optional checklist), `notify` (bool, default true) | you press **Done** (or **Skip**) in the Sky (its box, or the drawer), the pane strip, or the bell |
| `wait` | robot | `minutes` (int) or `until` ("HH:MM" local) | the time has passed |
| `reviews` | robot | `quiet_minutes` (default 10), `max_rounds` (default 5), `include_bots` (default true), `require_reviewer` (default empty), `reviewer_wait_minutes` (default 10), `reply_declined` (default false), `each_round` (default empty) | the PR has had no new unresolved review comments for `quiet_minutes` (with `require_reviewer`: counted from that reviewer's first post, which it waits for); each batch of new comments is sent to the agent (the pipeline's consolidated PR prompt) followed by an automatic push; `max_rounds` reached → fail "comments still open after N rounds — it needs a human review"; the required reviewer silent for `reviewer_wait_minutes` → fail "<reviewer> never reviewed this PR — it needs a human review" (see "Team PR rules as a route"); `each_round` / `reply_declined` add to every round's prompt (see "Reviews: instructions for every round") |
| `checks` | robot | `timeout_minutes` (default 60) | the PR's checks are all passing; a failing check → fail (so `on_fail: agent` hands the failing check names to the agent and re-waits); checks it can't read for 5 min in a row → fail, "can't read the CI checks: <why> — fix that and Retry, or Skip" (until then it waits saying why) |
| `command` | robot | `command` (shell, run in the worktree with the session's env), `timeout_minutes` (default 30) | exit 0; non-zero → fail with the tail of the output |
| `ticket` | robot | `state` (target state name or id, optional), `comment` (text, optional) | the tracker call returned; a flight without a ticket subject skips it |
| `notify` | robot | `text` | the notification went out (never blocks): the stop's receipt says where — "sent to your phone", "shown in the bell — phone notifications are off" or "not sent: <why>" — and the bell lists its text (`flight.notified`) |

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
  "state": "running" | "waiting_you" | "paused" | "halted" | "done" | "cancelled",   // paused = held by you (Pause) or at a halt point (reason "halt point"); Carry on (resume) goes on. halted = stopped on a failure (retry / skip / resume)
  "cursor": 0,                    // index into steps of the current step
  "step_states": {"s1": {"state": "pending"|"running"|"waiting_you"|"done"|"skipped"|"failed", "since": 0.0, "note": "", "detail": {}, "rounds": 0}},
  "reason": "",                   // why halted / cancelled (human-readable)
  "history": [{"at": 0.0, "step": "s1", "event": "started"|"done"|"failed"|"skipped"|"you"|"halted"|"paused"|"resumed"|"jumped"|"added"|"edited"|"removed"|"moved"|"halted_at"|"noted", "note": ""}],  // ≤200; noted = a remark that changes nothing (a Verify stop's PR comment)
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
- `flight.notified` — a Notify stop's text went out, data as `flight.changed`
  plus `text`, `title`, `phone` (whether the phone got it) and `why` (why not);
  the bell lists it as "<title>: <text>".

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
| `GET /api/flights/kinds` | | `{"kinds": [{kind, label, actor: "robot"|"you", summary, fields: [{name, type: "text"|"textarea"|"int"|"bool"|"select", label, options?, option_labels?, default?, placeholder?}], default_title}]}` (`option_labels`: `{option: words}`, e.g. a Verify stop's `who` → "Agent, then me") |
| `GET /api/flights/plans` | | `{"plans": [Plan]}` (built-ins first) |
| `POST /api/flights/plans` | Plan (no `id` = create; `id` = replace) | `{"plan": Plan}`; 400 with `{"error", "detail", "field"}` on a bad step (strict: see [Flow polish](#flow-polish-2026-11-10-as-built)) |
| `DELETE /api/flights/plans/{id}` | | 204; a built-in is reset to its default instead of removed |
| `POST /api/flights/plans/{id}/reset` | | `{"plan": Plan}` (built-ins only) |
| `POST /api/flights/describe` | `{text, want}` | `{"steps": [Step]}` |
| `GET /api/flights` | `?all=1` to include finished (default: unfinished + last 20 finished) | `{"flights": [Flight]}` |
| `GET /api/flights/{id}` | | `{"flight": Flight}` |
| `POST /api/flights/check` | the body of a start | 200 `{ok, steps, start, skipped, in_flight, problems, warnings, start_on, environments?, absorbed?}` — the start's answer without starting: nothing is created or written. `steps` are the stops strictly normalised with the ids the server gives them, `start` the stop it takes off at, `skipped` `[{id, title, reason}]` the stops before it and why, `in_flight` the session's current route, `problems` `[{field, detail, error?, session?}]` every refusal the start would answer plus the driver's pre-check, `environments` the repo's Verify environments. `ok` = no problems: the real start will be accepted |
| `POST /api/flights/start` | `{"plan_id"?, "title"?: "<existing session to attach to>", "subject"?: {kind, source, id}, "repo_path"?, "prompt"?, "start_at"?: "<step id>", "steps"?: [Step], "absorb_fasttrack"?: bool}` | 202 `{"flight": Flight, "absorbed": [lane]}`. **`steps`** (1..40, validated like a plan's; 400 `bad_step` with `field`) draws the flight here: alone, the flight is `plan_id: "adhoc"`, `plan_name` = the subject's headline or "Flight on <title>", its steps numbered `s1..sN`; with `plan_id`, the plan's steps then the given ones (next free `sN`). An ad-hoc flight needs `title` or `repo_path` (400 `needs_title`). **`absorb_fasttrack`** (default true with `title`): the session's own fast-track run (autopilot record `running` or `halted`, `source != "flight"`) becomes the flight's leading `ship` steps — the rungs after the one the session's stage (`committed`/`pushed`/`pr`) proves, up to the run's lane, in `autopilot.DEPTH_ORDER` order — placed where the flight starts, and the record is disarmed so the ladder carries on under the flight; `absorbed` lists their lanes. A `done` run is left alone; a `halted` one is absorbed (its first step starts `pending`, the halt reason goes into the flight's history). Attaching to an existing session (`title`) starts at `start_at`, else past each **leading Agent stop with no prompt** (the session's own work counts as it; one `skipped` history line each — an Agent stop with a prompt is sent) — unless the session is still on its turn (working, loading, or its task waiting in its prompt queue) and the route opens with an `agent` step: that step then starts `running` on the work in progress (nothing is sent; history `started` "the session's work in progress") and ends when the turn settles. Without `title` the server creates the session from `plan.session` (+ `repo_path` / `prompt` overrides) through `session_create.create_result` — the same path as the New dialog. A ticket/PR/issue `subject` goes through the Intake start path (`ticket_start.launch` / the PR and issue force-start bodies) with `plan_id`, which arms the flight on the deterministic title before the session exists. **Before anything is created** the driver's pre-check (`flight_driver.preflight`) runs: a bad `start_at` or ad-hoc list, a Verify `where` the repo has no environment for (listing the ones it has), a branch a live session already holds ("<s> already has this branch — run the route on that session instead"), a Ship stop on a session sharing its folder or a copy window. Its first problem is the answer — 409 when it names a session, else 400 — `{error, detail, field, session?, problems}`. |
| `POST /api/flights/{id}/steps` | `{"step": Step, "at"?: int}` | `{"flight"}` — appended, or inserted at index `at`, which must be after the current step (409 `in_progress`); the flight must be `running`/`waiting_you`/`paused` (409 `finished`). The new step takes the next free `sN`. History `added`. |
| `PATCH /api/flights/{id}/steps/{sid}` | `{title?, config?, on_fail?, when?, halt?}` | `{"flight"}` — a pending step after the current one, **or the current step** when it failed (halted on it, or its failure handed to you) or the route is paused before it with nothing of it run yet; else 409 `in_progress`. Validated strictly like a plan step (400 `{error, detail, field}`). An edited current step is reset, so the next Retry or Carry on runs it as edited (a failure that was waiting on you becomes a halt). `{halt}` alone may also mark / clear the **current** step ([Halt points](#halt-points)). History `edited`. |
| `DELETE /api/flights/{id}/steps/{sid}` | | `{"flight"}` — only a pending step after the current one (409 `in_progress`). History `removed`. |
| `POST /api/flights/{id}/steps/{sid}/move` | `{"at": int}` | `{"flight"}` — a pending step after the current one moved to index `at` of the final list (pop then insert), clamped to the last place; `at` at/before the current step is 409 `in_progress`; 400 `bad_step` (`field: "at"`) when `at` is not an integer. Its `step_states` record moves with it; the same place changes nothing. History `moved`. |
| `POST /api/flights/{id}/steps/{sid}/done` | `{"note"?}` | `{"flight"}` — a step waiting on you: ticks off a Wait for me stop; on a Verify stop waiting for a confirm it is **Confirm verified**; on a Verify stop parked because the check couldn't finish or memory kept it from starting it runs the stop again; it never passes a Verify stop's open steps (409 `answer_behaviors`); a failure handed to you takes it. 409 `not_waiting` on a stop that does not need you now |
| `POST /api/flights/{id}/steps/{sid}/skip` | | `{"flight"}` |
| `POST /api/flights/{id}/steps/{sid}/retry` | | `{"flight"}` — a failed/halted step runs again; so does a Verify stop parked with `ask` `blocked` or `memory`. 409 `not_failed` otherwise |
| `POST /api/flights/{id}/again` | `{"at"?: "<step id>" \| "resume", "title"?: "<session>"}` | 202 `{"flight", "from", "session", "how"}`: an ended route flies again from the stop it had reached ("Round 21") |
| `POST /api/flights/{id}/jump` | `{"step": "<id>"}` | `{"flight"}` — move the cursor (a human "start at the PR stage" / "go back") |
| `POST /api/flights/{id}/pause` · `/resume` · `/cancel` | | `{"flight"}` |
| `DELETE /api/flights/{id}` | | 204 (finished flights only; a running one must be cancelled first) |

The session row (`/api/instances` payload) gains a `flight` field:
`{"id", "plan_name", "cursor", "total", "state", "reason", "step": {id, kind, title, state, actor, note, failed_reason?}, "join", "branch"}`
or `null` (`step.note` is the stop's own reason — why it waits — and
`failed_reason` is set when its failure was handed to you) — the pane strip and the rail read only this. `join` is `{waiting,
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
[How a ticket gets its route](#how-a-ticket-gets-its-route). An unknown
`plan` is a 400.

## Contract: the UI

**Words.** Birds, never planes (see [The idea](#the-idea)): flyway, flight,
stop, perch; the glyph is a small bird (`components/flights/Bird.tsx`). The
wire keeps its names; only what a person reads changed.

**Top bar.** Flat: **New · Grid / Sky · Intake · Verify · Schedule ·
Settings**, each with its own badge, tooltip, calm-hour rule and shortcut
(`Alt+I` / `Alt+V` / `Alt+S`, ids `intake` / `verify` / `schedule`). No
Flights entry. The Grid / Sky toggle is on the bar only on a phone-narrow
window; on desktop it lives on the sidebar's View row (`Ctrl+Y` and the
palette switch when the sidebar is hidden). Its Sky half carries the badge of flights
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
and at the end opens the stop palette ("MindFlock does" / "You do", each kind
with its summary), inserting there. **Type to filter** (`StopPalette`, owner:
"when i click the plus i should be able to just start typing and it narrows
down the list"): the palette opens with its "Type to filter…" field focused
(an ARIA combobox over a `listbox` of `option` rows); what is typed narrows the
kinds (the label `kindName` says, `label`, the kind id, the summary) and the
saved-chain chips (name, stops) by the floating palette's own rule
(`catalogMatch`: every word, case-blind). Inside each group the matches rank
by `paletteRank` — 0 the label starts with the query, 1 a word of the label
does, 2 a word of another text does, 3 a substring only; ties keep the
server's order — and the groups always draw MindFlock does → You do → saved
chains (`paletteRows`). ↓ / ↑ walk the highlight (wrapping), Enter picks it
(`bestRow`, the best match, until an arrow moves it; a disabled saved chain,
while `busy`, is skipped silently), Esc clears the text and only a second Esc
closes (`paletteKey`, pure; IME composition ignored). The field stops every
key's propagation, the palette's own outer Esc handler included, so the Sky's
keys (T tidies, arrows pan, Delete removes) never see what is typed. Nothing
left → "No stop matches “…”", and a group with nothing left hides its head.
Props: `saved?: PaletteSaved[]` (`id`, `name`, `title` — its stops joined
with " → " — `disabled`, `onPick`), `savedHead`, `initialQuery`; `extra`
still draws anything else under them, **unfiltered and off the arrow keys** —
so a caller with saved chains passes them as `saved`. The Sky's grounded line
(`useGroundedLine`) does; the session diagram's **Fly a flyway** chips
(`useSessionLine`) still come in through `extra`, so typing does not narrow
them. Live: the same canvas draws a flight's
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
| `checklists[/<branch>]` | the behaviors in the Sky (`openBehaviors`): the board (`#behaviors`), or that branch's frame (`#sky`) — docs/behaviors.md §4 |
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
tour): the Grid · Sky toggle (`data-tour="view-sky"` — the sidebar View
row's Sky chip on desktop, the top bar's toggle on a phone-narrow window), a
line (`sky-line`), the bird (`flights-bird`), the shelf (`sky-shelf`). No
step falls back to the toggle; a step whose element is not on screen
(including the toggle step on a desktop with the sidebar hidden) centres its
card (see [the tour table](#as-built-round-12-guide)). Its **?** ends the Sky's toolbar; it opens by
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
- **Switch points**: the sidebar View row's **Sky** chip
  (`data-tour="view-sky"`; a number returns to the grid), the top bar's
  Grid · Sky toggle (phone-narrow only), `Ctrl+Y`,
  the palette, `#sky` / `#sky/<title>`. Switching keeps the selection, the
  camera, the drawer and the shelf; each pane's ghost flies to its line and
  back (~250 ms; none under `prefers-reduced-motion`).
- **One plane**, panned and zoomed as a whole: one `translate(x, y)
  scale(z)` container, no library. Drag on empty sky pans (mouse and touch),
  the wheel scrolls, Ctrl/⌘+wheel and a pinch zoom (0.4×–2×) about the
  pointer, Space+drag pans from anywhere; keys: arrows pan, + / − zoom, 0
  fits all, 1 the focused line, T tidies, Enter opens the focused line's box,
  Esc closes it. A faint dot grid scales with it, in both themes, the flock
  (`SkyFlock {width, height, zoom, paused?, density?, birds?}`) drifting
  behind when you turn it on (Settings → Appearance → **Birds in the Sky**,
  off by default; the weather's clouds and night tint show either way).
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
  this line". (Superseded: rows now wrap at `fillWidth`'s width, not the
  window's, and Tidy ends on a fit — see "As built: Tidy fills the screen
  (2026-11-03)".)
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
  `{value, label}`; lane and who in the app's words; a field's
  `option_labels`, when it has them, are the words to show — not read by the
  editor yet, see [Verify stops](#verify-stops)), a `bool` a switch chip,
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

- **Ticket to merge** (`builtin:ticket`, trigger ticket): Agent works the ticket → Commit → Push → Verify (this branch · agent only · right away) → Open PR → Reviews settle (10 min) → Checks → Perch: Approve the merge (step title "Approve the merge") → Merge → Ticket: comment (step title "Ticket: comment"; it posts "Merged by MindFlock" and moves the ticket nowhere — set the step's `state` to move it).
- **Pull request** (`builtin:pr`, trigger pr): Agent addresses the review comments → Commit → Push → Reviews settle → Checks.
- **Scheduled task** (`builtin:schedule`, trigger schedule): Agent → Commit → Push → Open PR.
- **Verify on live** (`builtin:verify`, trigger manual): "Verify on live · agent, then you · once on the live branch" (`where: live`, `ready: on_branch`, `who: both`).

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
  `require_reviewer` holds the stop until that reviewer has posted (see
  "Team PR rules as a route"). `each_round` and `reply_declined` add to
  every round's prompt (see "Reviews: instructions for every round").
- **`checks` step**: `none` (nothing reports) counts as passed only after 2
  minutes; the failing names come from the check-runs API.
- **`verify` step** finds or makes the session's plan
  (`_ensure_verify_plan(title)`: its own on its current branch, else an empty
  `origin: "manual"` one — nothing is generated) and starts the self-verify
  through the `POST /api/test-plans/{id}/run` route body (`{"environment":
  where, "notes": notes, "walk": …}`, plus `"confirm": true` for `who: you`).
  `_verify_obs` adds `env` (`test_plans.environment(repo_root, where)`, None
  for `branch`), for `ready: on_branch` `env_reached_at`, and the newest
  run's `outcome` / `outcome_note` and `confirm_items` (the steps a person
  checks before confirming); an unknown environment fails the stop. Done on a
  stop parked with `ask: "confirm"` confirms the check (verified). With
  `post_to_pr` the driver comments the results on the PR when the stop is
  done or failed (`_post_verify_results`).

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
  and `base_branch` — a provisioned session (Intake's tickets and PRs) too:
  its workers are plain worktrees of ITS clone (clone strategy) or `_base_`
  clone (worktree strategy), never provisioned twins (its `Path` is the
  server's own folder when Intake cloned a URL, so a twin landed in the wrong
  repository, and a twin forks from the base branch, not the session's
  change); an unreadable checkout fails the stop rather than guess —
  `profile_model`, the fence (`fence_only` kept inside
  the session's own fence, as `spawn_session` does), titles `<slug>-w<N>` (the
  lowest free one: no live or starting title, tmux name or leftover branch —
  `<prefix><title>` by the row's prefix and by the server's own, a
  provisioned session's leftover `<workspace_dir>/mindflock-<title>` clone or
  `mindflock/<title>` branch as `spawn_session`'s `_provisioned_branch_probe`
  sees them, a title whose create failed in the background in the last ten
  minutes, a title the create already refused as existing this spawn),
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

## Round 21: routes on rails (2026-11-12)

Where the owner's own routes needed a person and should not have: review
stops that never settled, routes that could not be picked up after a
disruption, and a Verify stop that parked the route before the unattended
work behind it had begun.

### A review stop converges

- **Round 1 is a full review; every later round is a follow-up.** When a
  review is asked for, the stop records the session's HEAD
  (`detail.reviewed_sha`) and the round (`detail.round`). Findings are kept
  verbatim in `detail.followup` (`{findings, sha, round}`); that record
  survives the hand-off to the agent, a person's Retry and "Let the agent fix
  it". The next reviewer gets `followup_brief()`: the earlier findings, and
  three jobs: (a) check each one is fixed, or answered by a rebuttal in
  `git log <sha>..HEAD`; (b) report only bugs that `git diff <sha>..HEAD`
  introduced; (c) raise nothing new in code an earlier round already read
  unless it is severe (data loss, a security hole, a crash on a common path).
  All three modes. A sha that is missing or no longer an ancestor of HEAD (a
  rebase) falls back to a full review, and says so. A new flight, or a jump
  back to the stop, starts fresh.
- **A reviewer with no verdict is not the agent's failure.** Did not finish,
  gone, ended without a report or a readable reply, could not start (a
  refused create, a stale worktree): the stop asks another reviewer, up to
  `MAX_REASKS` (2) per visit, the launch respawn included. Nothing is typed to
  the agent and no fix round is spent. After that it halts with the plain
  reason, never "(after N fix rounds)".
- **No report is never a silent pass.** A reviewer that ended without
  `report_result` is judged from its last reply (`parse_reply`: "No findings"
  or the last numbered list). Anything else counts as did not finish.
- **Rounds are the stop's own setting.** `config.rounds` (1..10, default 3:
  how many times findings go back to the agent; other kinds keep
  `MAX_FIX_ROUNDS` = 2) and `config.unresolved`: `"ask"` (default) or
  `"continue"`. With `on_fail: agent` and findings still open after the last
  round: `continue` finishes the stop done ("2 findings left open",
  `detail.findings` kept); `ask` parks on the person (`waiting_you`,
  `detail.ask: "unresolved"`, "2 findings still open after 3 rounds"), where
  **Done** carries on with them on record and **Retry** gives the agent
  another set of rounds, as a follow-up.
- Every brief asks only for findings that should block the merge, most
  serious first.

### A Verify stop can ask once, at the end

`config.ask`: `"here"` (default: the stop parks on needs-you, as before) or
`"end"`. In end mode a check that needs a person finishes the stop **done**
("2 checks for you, asked at the end"; `detail.owes`), and the person's
steps, with the ones the agent could not settle, go onto the flight:

```
flight.owed       [{step, title, plan, items: [str], since, run_at, perch}]
flight.owed_perch  the id of the stop the route added to ask, or ""
flight.owed_note   "2 checks owed to you at the end" (dto only, while flying)
```

The agent's own failures still fail the stop right there. The route flies
on, and asks **once**: at the next Wait-for-me (`you`) stop it reaches, whose
ask then carries the owed checks too (`detail.ask: "owed"`, `detail.owed` =
one group per Verify stop); else **before the first stop that cannot be
undone** (`flights.cannot_undo`: a Ship stop that merges, a kind whose
`risk` lists `irreversible`, such as Land), on a "Your checks" stop the
route adds in front of it ("Before this route merges: 2 checks for you");
else on a "Your checks" stop it adds at the end ("Before this route
finishes: 2 checks for you"). So "at the end" is the last safe moment: the
route never merges, lands or finishes with a check owed.
`flight.owed_added` lists the stops the route added this way.

| On the perch | What happens |
|---|---|
| **Done** | every owed plan is confirmed (`test_plans.confirm_verify`, the same event as the confirm route) and the route moves on; 409 `owed_failed` when one was already reported failed |
| "It doesn't work" (`POST /api/test-plans/{plan}/confirm {verified: false}`) | the route goes back to that Verify stop, which fails through its `on_fail`; the stops after it run again; a stop that cannot be undone and already ran is never reset, and the stops the route added are taken off again |
| confirmed elsewhere (the session's Verify pill) | those entries drop; an added perch with nothing left finishes itself |
| **Skip** | the entries drop, the plans stay `needs_you`, the route finishes |

Cancelling the route or deleting the session leaves the plans `needs_you`.
`mindflock flights` shows "· 2 checks owed to you at the end" in the STOP
column and `flight_status` has `owed`.

### A route carries on where it stopped

- **The resume point** (`flights.resume_point`): for a route about to start,
  the newest earlier flight on the same session, the same branch of the same
  repository (never `main` / `master`) or the same ticket / PR / issue that
  did not finish (cancelled, halted, running, waiting, paused), and the first
  stop it had not finished. Stops match by id for the same saved route, else
  by kind and title in order, and then only when every stop before the point
  was flown (done or skipped) by the earlier flight: carrying on never skips
  a stop the earlier run did not fly. A flight cancelled because its session
  was deleted keeps its stop states, so it counts. Shared branches
  (`flights.SHARED_BRANCHES`: main, master, staging, develop, dev, trunk)
  never match by branch. A "Your checks" stop the earlier route added is not
  one of its stops (`own_steps`); a route that stopped there carries on at
  the Verify stop that still owed, which asks again. An Agent stop whose
  prompt had gone out (`in_progress`) is not sent again: the route rides the
  work in progress, or passes the stop when the session is idle.
- **`POST /api/flights/check`** answers `resume`: `{stop, index, title,
  from_flight, session, state, reason, at, in_progress}` or null. It is null
  for a start that makes its own session: a new session always starts at the
  first stop (a ticket / PR / issue start gets the warning
  `carries_on_if_found` when the carry-on below would apply).
- **`start_at: "resume"`** on a start (MCP `start_flight at="resume"`, CLI
  `mindflock flight start ROUTE --at resume`) starts there, or normally when
  there is no resume point. A stop id still starts at that stop. History:
  "carried on where route f_ab12cd stopped (its session was deleted), at
  Push".
- **Fly again**: `POST /api/flights/{id}/again {at?, title?}` → 202 `{flight,
  from, session, how}` starts a new flight with an ended route's stops, at the
  stop it had reached (`at` names another; a route that finished starts at
  stop 1). The session: `title` when given (`how: "chosen"`), its own
  (`same`), the live session that holds its branch (`branch`), or its
  Recently-closed session reopened (`reopened`; `restored` when the reopen
  brought the route itself back, and `flight` is then the old one). A branch
  or closed session only counts inside the route's own repository. Refusals:
  409 `not_flown`, `in_flight`, `session_gone` (the sentence says what to do),
  400 `unknown_step`, and a start's own pre-check. CLI `mindflock flight again
  ROUTE [--at STOP] [--title SESSION]`; MCP `control_flight op="again"`.
- **An Intake start adopts work that is already there.** A ticket whose
  branch is already checked out no longer dies with "the session could not be
  created". `flight_driver.carry_on_intake` moves the armed route onto the
  live session that works in that worktree, or reopens the closed session
  that kept it (or opens a session in place on the folder), at the resume
  point, else past the leading Agent stops ("carried on in sc-42 (already had
  this branch)"). A session opened in place on a folder with uncommitted
  changes no earlier run accounts for, with a Ship or Land stop ahead, starts
  **paused** and says why; Carry on flies it. A holder that is already on a
  route keeps it. Nothing is
  deleted to make room; when nothing can take the folder over, the refusal
  names the session to close.
- **A server restart** changes nothing for a route in flight: a running
  stop, a route waiting on a person and one in the middle of a fix round
  carry on from the stored record, with no prompt sent twice
  (`tests/unit/test_flights_resume.py`).

### In the UI

See docs/web-ui.md: a waiting review stop offers **Give it another round**
and **Carry on**; owed checks show on the route while it flies and are
settled on one perch; starting a route on a session offers **Start at…**
and carries on at the resume point; an ended route offers **Fly again**.

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

**The ambient flock** — `SkyFlock {width, height, zoom, paused?, density?,
birds?}` (`components/flights/SkyFlock.tsx`): a `<canvas>` the Sky mounts
behind its plane with a few birds crossing the empty sky — ~8, times `density` (capped
at 40). Same sprite (flock.ts's `tinted()`), same 12-species plumage (the dark
column, the light one in the light theme), same wing beat and belly-up mirror
as the app's flock. They flock casually, each heads for a wandering target,
they **ease out of the middle of the sky** where the lines are, and they wrap
at the edges so they cross the sky rather than turn back. Bird size follows
the Sky's zoom a little (√zoom, 0.7–1.3).

Cheap by construction: one rAF, at most 60 frames a second (a 120 Hz frame is
skipped), no work while the tab is hidden, `paused` asks for no frame at all
(the last one stays painted), DPR-aware (capped at 2). Under
`prefers-reduced-motion` it flies no birds.

**Off by default.** Settings → Appearance → **Birds in the Sky** turns the
flock on (`skyBirds` in the store, saved under `mf_sky_flock`; the old
`mf_sky_birds` key, saved when the default was on, is deliberately never
read, so everyone starts with it off). It is a per-browser `localStorage`
pref — not in `settings.json` or the synced `ui.*` prefs — so it is turned on
separately on each device, and in the desktop app apart from a browser tab.
The Sky always mounts `SkyFlock` and passes `birds={skyBirds}`: with the birds off, none are seeded or stepped, but
the weather stays — the canvas still exists while there are clouds, the rAF
runs only while they drift, and the night tint still dims the sky. A clear,
bird-less sky renders nothing and asks for no frame (`skyMotion`). The birds
on your chains are not part of it and always stay. It is decoration:
`pointer-events: none`, 0.55 opacity (0.32 in the light theme). The simulation
(`flockSize`, `seedBirds`, `fitBirds`, `stepBirds`), what the sky draws
(`skyMotion`) and the frame loop (`skyLoop`) are pure and tested without a
DOM (`__tests__/skyBirds.test.ts`, `skyFlockOptIn.test.ts`).

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
  `on_failed` halt|continue) appear under "MindFlock does" once the server
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
  forgets the pins and re-places at the new gap. (Superseded for the wrap
  width: see "As built: Tidy fills the screen (2026-11-03)".)
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

The owner: "I want to be able to halt steps whenever." Two ways, both of
which **pause** the route (state `paused`) — **Halted** (red) is kept for a
stop that failed (Retry / Skip / Let the agent fix it):

- **Pause** stops the route where it is: the pause route
  (`POST /api/flights/{id}/pause`), on the current stop's menu and the bird
  (▶ **Carry on** while paused); the toast reads "Route paused — Carry on picks
  it up again". The pane strip's own ⋯ reads Pause / Carry on / Cancel.
- **Add halt point** marks a stop not yet done as a **halt point** (`halt:
  true` on the step): the route pauses when it *reaches* it, before doing it,
  like a breakpoint (state `paused`, reason `"halt point"`). ⏸ **Add halt
  point** on the stop's toolbar and menu sets it; **Remove halt point** clears
  it. The stop dialog's toggle reads "Halt point — the route pauses before
  this stop until you press Carry on". The box wears a pause badge in its
  corner while it is set. Removing the stop the route is on also pauses it.

A route paused at a halt point shows **Carry on** (primary) and **Skip** on
its stop's tile ("Your halt point") and in the pane strip, and is listed in
Needs you as "Paused at <stop> — Carry on" (a row the UI builds from the
session row's `flight`, kind `flight_paused` in `lib/needsYou.ts`); a
route you paused yourself reads "Paused by you", with the same two buttons,
and stays out of the bell.

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
and the flight's current stop, offers ⏸ Add halt point (Remove halt point on a
halt point, `pressed: true`) on its toolbar and menu; the current stop's own
toolbar (Pause / ▶ Carry on, then ⏸) shows only on such a line, so a line
without it asks for no headroom. The current stop's right-click menu offers
Pause / Carry on on any live line (`controlFlight`). The ⏸ tool looks pressed
only on a halt point (`HaltTool` reads `pressed`). A halt point wears `.fl-halt-point` and a ⏸ badge
(`.fl-halt-mark`, top-left, the paused tone; `StopEditing.css`).
`useSessionLine` wires `onToggleHalt` to `flightsApi.setHalt(flightId,
stepId, !halt)`. The pane strip leads with "Paused · " for a paused route,
and its ⋯ reads Pause / Carry on / Cancel (see above).

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
| The trailing + | **Add a stop** (the box's title and its menu item; palette and "Fly a flyway") | `onOpen` |
| ⏸ on a stop's toolbar or menu | Add halt point / Remove halt point ([Halt points](#halt-points)) | `onToggleHalt` |
| Pause / ▶ on the current stop's toolbar or menu | Pause / Carry on (pause / resume the route) | — (`controlFlight`) |
| Drag a stop chip from the shelf onto the line | The gap it would land in lights up ("+ Add here"), or the end + reads "Add <Kind>"; dropping adds the stop there | `onDropStop`, `dragStop` |

Only a stop that can change (`DiagramNode.editable`: in the editor every
stop; in a live flight the pending stops after the current one) wears the
toolbar, drags, or takes Earlier / Later / Duplicate / Remove. ⇤ / ⇥ are
disabled at the ends and next to a stop that can't change (`canShift`).

**The right-click menu, by box** (`menuItemsFor`):

- a stop that can change: Edit, Earlier, Later, Duplicate, Remove, Add halt
  point / Remove halt point (only the ones the host handles), Jump here on a
  live route;
- the flight's current stop: what its own parts offer (Done on a perch,
  Retry when halted, Skip; the same `stepDone` / `stepRetry` / `stepSkip`
  routes), Pause / Carry on, Add halt point / Remove halt point, and a muted
  line: "The flight is here — it can't change now";
- a stop that has flown (done, skipped, failed): "This stop has flown";
- the trailing +: Add a stop; a session, parent, child or branch box:
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
  is placed again, in rows wrapped at `fillWidth`'s width (not a fixed window
  width), and once every line has its spot the camera eases to the whole fit
  (see "As built: Tidy fills the screen (2026-11-03)"). It is **the only
  glide**: SkyView sets `data-tidying` on `.sky-plane` for 350 ms (not under
  reduced motion), and only `.sky-plane[data-tidying] .sky-line-slot` has a
  `transform` transition.
  No other rule eases a line's or a slot's transform. A dragged line follows
  the pointer live.
- This session (one line) still sits at the plane's corner. It neither reads
  nor changes the auto spots.

### The camera fits once

The automatic fit (`firstFit`, or the focused line when the grid was on "1")
runs once, on the sky's first show, after the lines have their spots (drawn
and measured, or 0.9 s after the sessions load, whichever comes first). It
runs again only when the view changes (All / Mine / Perched / This session,
or This session's line), and once after a toolbar Tidy / `T` (armed for 2 s,
fires once, `fitAll(true)`; Tidy these and the lane making way never fit). The
effect's dependencies have no line sizes in them. A line growing, a new
session arriving, or the window resizing never refits. A newcomer off screen
is brought in by the rail's selection and the
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

## Round 12: the bird in hand, a movable intake, the shelf's catalog, tools, a guide, a fuller block, moving sessions (contract)

The owner, after round 11: stop a bird where it works, or pick it up and
drag it to an earlier stop; pick up, drag around and expand the intake menu;
the Stops shelf = the default blocks, then the multi-step blocks, the rest in
a dialog listing every block; a toggle between drag mode and click mode in
the sky; a big tutorial for the Sky; the sky's new-session block with the
full capacity of the grid's New dialog, and "spawn from a session"; move a
session to a new directory; and: click any box in the sky, hit Delete, it
goes.

### The bird (frontend)

- A click on the bird marker (`.fl-bird-marker`) on the current stop = **Halt
  now** (pause) while the flight runs or waits on you; on a paused flight =
  **Carry on** (resume). The bird's title says which.
- The bird is **draggable** (pointer drag, not HTML5 DnD): while dragging, every
  stop that can be jumped to lights up (`.fl-jump-target`); dropping it on a
  stop = `POST /api/flights/{id}/jump {step}` (`jumpFlight`), which re-arms the
  halt points after the target and pauses there when the target is a halt
  point. Any stop of the flight may be a target (earlier or later); the
  current one is a no-op. Dropping elsewhere snaps the bird back. Pure rules
  in `lib/stopEditing.ts` (`canJumpTo`, `jumpTargets`), tested.

### The intake lane (frontend)

- The intake lane is a sky object like a line: layout key `"intake"` in
  `mf_sky_layout` (x / y / pinned, `open` = expanded). Drag it by its head
  (same gesture as a line's bar; pins it). Tidy forgets its spot.
- Folded (default) it is the block row it is today; **expanded** (▸/▾ on its
  head, or double-click) it shows every source block with its waiting items in
  full (titles, Fly buttons, the started stubs) and its own width grows.

### The shelf (frontend)

- Three regions in one row: the **default blocks** (Session, then Agent,
  Perch, Ship, Verify, Wait), the **multi-step blocks** (the saved stops), and
  an **All blocks…** chip that opens `StopCatalog` (a `Modal`): every kind
  with its summary, every saved stop with its sub-steps, each row draggable
  onto the board like a chip and with an **Add** button (tacks onto the focused
  line, or makes a grounded line in the middle of the view).

### Drag mode / click mode (frontend)

- `skyTool: "drag" | "click"` kept in localStorage (`mf_sky_tool`), shown as a
  two-way toggle on the toolbar (✥ Drag · ☝ Click) with a key (V = drag, H =
  click, like design tools; Space held = temporary click/pan in either mode).
- **Drag** (today): a line moves by its bar, empty-sky drag = marquee.
- **Click**: nothing moves by dragging — a drag anywhere pans the sky; a click
  on a line focuses it, on a box selects it (Enter / double-click opens it as
  today). Marquee is Shift+drag.
- `.sky[data-tool]` carries it for CSS (grab cursor in click mode).

### Delete removes the selected box (frontend)

Click any box in the sky and press Delete / Backspace: a stop or group → the
line's Remove (undoable toast); a grounded line's head → Remove the line
(inline confirm); a session box → Close session (inline confirm, as today).
The global keymap's Delete (`lib/keymap.ts`) must yield whenever the focus or
the sky's selection is on a box that is not the session itself.

### The Sky guide (frontend)

- The Sky tour (`lib/tours/sky.ts`) grows to walk every surface: the toggle,
  a line and its bar, the boxes and the bird, the hover toolbar, a dialog, the
  shelf and its catalog, a grounded line, the intake lane, selection, the
  tools, the zoom, the ⋯ menu — one plain sentence each, with targets that
  exist on an empty sky too (`""` centres the card when a target is missing).
- `SkyGuide` (a `Modal`, `components/flights/SkyGuide.tsx`): the long-form
  tutorial — sections with a small inline SVG sketch each: What the sky is ·
  Lines and boxes · Stops and saved stops · The bird · Halt points and Remove ·
  Grounded stops · Selection · Tools and the keyboard · The intake lane ·
  Where things are saved. Opened from the sky's ? (Replay the tour / Open the
  guide) and the palette ("Learn the Sky").

### The new-session block (frontend)

- `NewSessionBlock` gains a **More options** fold with everything the grid's
  New dialog offers — CLI and account, model, workspace (worktree / in place,
  base branch), template, launch arguments, device — built from the New
  dialog's own pieces (`components/dialogs/newSession/*`, `lib/newSessionSubmit`),
  so one body builder serves both.
- **From a session**: a picker of live sessions; choosing one forks from that
  session (the create body's `parent` + its repo; the server forks at the
  parent's HEAD as `spawn_session` does) and suggests a worker-style title.

### Moving a session (backend + frontend)

`POST /api/instances/{title}/move {"dest": "<absolute folder>", "force": false}`
→ 202 `{"title", "from", "to"}`; the move runs in the background and emits
`session.moved {title, from, to}` (added to `EVENT_NAMES`) when done, or
`session.move_failed {title, error}`.

- `dest` is the NEW folder path for the workspace (its parent must exist; the
  folder itself must not — 409 `exists`; 400 `bad_dest` for a relative path or
  one inside the current workspace).
- 409 `working` while the agent is mid-turn unless `force` is true; 409
  `in_flight` while the session's flight or fast-track runs (pause them first,
  or `force`); 404 `unknown_session`; 409 `remote` for another device's row.
- The engine's `MoveWorkspace(title, dest)` (backend/session/instance.py, Go
  style): stop the agent's tmux session (graceful, like Pause does), move the
  folder — `git worktree move` for a linked worktree (then `git worktree
  repair` from the main repo), `shutil.move` + `git worktree repair` for a main
  clone or a non-git folder — update every path the instance keeps (path /
  workdir / worktree), carry the CLI's per-directory state with the provider's
  hook (`carry_workdir(old, new)` on the provider base, default no-op; Claude
  copies `~/.claude/projects/<escaped old>` → `<escaped new>` so `--resume`
  still finds the thread), persist state.json, and resume the agent in the new
  folder through the provider's resume path. The flight / autopilot / aliases
  are keyed by title and need no change; a flight's `repo_root` is refreshed.
- Frontend: the session ⋯ gains **Move to a folder…** → `MoveSessionDialog` (a
  `Modal`: the folder field with Tab completion from `components/PathField`,
  a line saying what will happen, Move) → a toast on `session.moved`, the error
  card on `session.move_failed`. The row shows "moving…" meanwhile.

### As built: round 12 shelf

The owner: "In the stops section I want there to be the default blocks and
then the multi-step blocks, and additional blocks should be in some dialogue
of a list of all the blocks." Source: `components/flights/StopShelf.tsx`
(+ `.css`), `StopCatalog.tsx` (+ `.css`), `lib/stopCatalog.ts`; tests in
`stopShelf.test.ts`, `stopShelfSaved.test.ts`, `stopCatalog.test.ts`, and the
sky's shelf pin in `flightsUi.test.ts`.

**The shelf** is still one row (`Stops — drag onto the sky or a line`, the
fold handle, `data-tour="sky-shelf"`), now in three regions, each a
`.sky-stops-region` (`role="list"`, `data-region`) led by a tiny muted
uppercase label (`.sky-stops-label`, 9.5px, `--fl-ink-muted`), split by the
thin `.sky-stops-divider`:

| Region | What | Notes |
|---|---|---|
| **Blocks** (`data-region="blocks"`) | the Session chip, then `defaultKinds(kinds)`: `DEFAULT_KINDS = ["agent", "you", "ship", "verify", "wait"]` in that order (`you` reads Perch, round and gold) | a default the server does not list is skipped; every other kind (split, join, reviews, checks, command, ticket, notify…) is in the catalog only. The old flock/perch divider and `stopChipOrder` are gone. |
| **Multi-step** (`data-region="multi"`) | the saved stops, built-ins first (`savedStopOrder`) | the region and its divider are left out while there are none (or they haven't loaded) |
| **All blocks…** | `button.sky-stop-chip.sky-catalog-chip` (`data-tour="sky-catalog"`, `aria-haspopup="dialog"`), a muted ⊞ | the secondary chip tier; not draggable; opens `StopCatalog` |

Every chip keeps its round-11 behaviour: the drag MIMEs (`NEW_SESSION_MIME`,
`STOP_MIME`, `FLYWAY_MIME` + `text/plain`), the click (`onSession`, `onTack`,
`onTackPlan`), a saved stop's right-click menu and inline rename. The drag-data
helpers `stopDragData` / `savedDragData` moved to `lib/stopCatalog.ts` (the
catalog's rows use them too); `sessionDragData` stays in StopShelf.

**`StopCatalog`** (`components/flights/StopCatalog.tsx`), rendered by the
shelf while open:

```ts
interface StopCatalogProps {
  kinds: readonly StepKindDef[];   // useFlightKinds(), as listed
  plans: readonly FlightPlan[];    // the saved stops, built-ins first
  onAdd?(drop: CatalogDrop): void; // CatalogDrop = {kind: string} | {planId: string}
  onClose(): void;
  portal?: boolean;                // default true (document.body); false in the tests
}
```

- A `Modal` (`#sky-catalog`, `role="dialog"`, `.sky-catalog-modal`), the
  stop dialog's card (panel, border, radius 12, `min(560px, 100vw − 48px)`,
  ≤ 80vh with the list scrolling). Head: **All blocks** and a quiet ×
  (`.fl-icon-btn`). Then a filter (`.fl-input`, focused on open): every word,
  case-blind, in a kind's label (Perch for `you`), kind or summary, or a saved
  stop's name or description (`catalogMatch`, `filterKinds`, `filterPlans`).
- **Blocks** (`data-section="blocks"`): every kind, `catalogKinds` = the
  defaults first, then the rest in registry order; a row = the kind's icon,
  its name, a `robot` / `you` badge, its one-line summary. **Multi-step**
  (`data-section="multi"`): every saved stop; a row = a tiny box per step (a
  perch's gold, `+N` past ten), the name, a dashed `built-in` badge, its
  sub-steps as `a → b → c` (`planPath`). Nothing matching: "Nothing matches
  “…”."
- Each row is `role="option"`, draggable with its chip's data (`stopDragData`
  / `savedDragData`), and has an **Add** (`test-btn`, shown on the picked row
  and under the pointer, always on a touch screen). The filter keeps the
  focus: ↑ / ↓ move the pick (`aria-activedescendant`, scrolled into view;
  the pointer picks too), Enter adds it. Esc or a press on the backdrop
  closes; keys typed inside don't reach the sky (Escape still does, for the
  Modal).
- **The drag goes through the Modal.** Its backdrop is a full-window fixed
  layer, so a drop would land on it. The catalog stays a Modal (the Escape
  stack, the keymap's overlay guard, the focus hand-back) and, a tick after a
  row's dragstart (changing it inside dragstart can cancel the drag), turns
  `.is-dragging`: the backdrop transparent and `pointer-events: none`, the
  card at 35% opacity, so the lines and the sky under it take the dragover and
  the drop. Dragend clears it; a drop that landed (`dropEffect !== "none"`)
  closes the catalog. The row's dragstart / dragend bubble through the React
  tree (the portal too) to SkyView's `.sky-shelf-host`, which reads the drag
  exactly as it reads a chip's, so SkyView needed nothing for drops.
- **Add** goes through the shelf's own props, the ones a chip's click uses:
  the shelf passes `onAdd` when it has `onTack` or `onTackPlan`, closes the
  catalog, then calls `onTack(kind)` for `{kind}` or `onTackPlan(plan)` (the
  saved stop looked up by id) for `{planId}`. SkyView passes
  `onTack={onTack}` / `onTackPlan={onFly}` already, so Add tacks onto the
  focused line today; with no focused line SkyView shows its line picker
  (`setTackPick` / `setFlyPick`). The contract's "or makes a grounded line in
  the middle of the view" is SkyView's to change in those two handlers (e.g.
  `dropGrounded(d, <the view's centre on the plane>)` when there is no focus
  line) — the shelf's props don't change for it.
### As built: round 12 move UI

- **The menu item** — `lib/sessionMenu.ts`: **Move to a folder…** (`key:
  "move"`) right after Rename… in the Session group, on this machine's own
  rows only (not a remote row, a peer's shared folder, a session still
  starting or one whose folder is gone); greyed with the note *moving…* while
  the row carries `moving: true`. It runs `openMoveSession(title)` →
  `openDialogFor("move", title)` (a new `DialogName` `"move"` in
  `state/store.ts`, `dialogTarget` = the title, the way Rename opens).
- **The dialog** — `components/dialogs/MoveSessionDialog.tsx` (+ `.css`),
  mounted beside `RenameDialog` in `App.tsx`: a `Modal` whose inner form is
  keyed on the title, so every opening starts from the seed and a poll never
  overwrites the typing. *Now in* the row's `path` (else `folder`); **New
  folder** is a `PathField` (Tab completion through `/api/browse`, Browse…)
  seeded with the folder's parent + `/` (`moveDestSeed`); the sentence;
  **Even if it is mid-turn** (→ `force`); Cancel / **Move**, greyed with a
  reason (`moveBlockReason`) for an empty path, the folder itself, or the
  bare parent. No `confirm`/`prompt`/`alert`.
- **The action** — `lib/sessionActions.ts` `moveSession(title, dest, force)`:
  `POST /api/instances/{title}/move {dest, force}` via `instApi`. 202 →
  `markMoving(title)`, toast *Moving "name"…* (names are `windowName`), and
  the dialog closes. A failure is classified by `moveRefusal` (the body's
  `error`, or `code`, among `working` / `exists` / `in_flight` / `remote` /
  `bad_dest` / `unknown_session`); `moveErrorText` shows the server's
  sentence, or ours when it sent only the bare code. 409 `working` without
  `force` makes no card — the dialog shows `MOVE_MID_TURN` (*The agent is
  mid-turn — tick the box to move anyway*) under the box; everything else is
  an error card *Moving "name" failed*.
- **The mark and the events** — `state/queries.ts`: `markMoving` /
  `clearMoving` keep a title → since map (`MOVE_TTL_MS` = 60 s, cleared by
  its own timer) and stamp `moving: true` (`MovingFields`, `isMovingRow`) on
  the cached row; the instances query re-applies it to every fetch
  (`withMoving`, before `mergePending`), so a poll doesn't wipe it.
  `bridgeSessionSetEvents` subscribes `session.moved` → `onSessionMoved`
  (unmark, refetch, toast *Moved "name" to <to>*) and `session.move_failed`
  → `onSessionMoveFailed` (unmark, refetch, error card *Moving "name"
  failed* with `error`); a replay only unmarks and refetches. The title is
  the envelope's `session`, else `data.title`.
- **Not in this round's files** — the rail row and the pane header don't
  draw the *moving…* word yet (`components/sidebar/SidebarRow.tsx`,
  `lib/paneHeader.ts`: read `isMovingRow(inst)` from `state/queries`), and
  the command palette has no *Move to a folder…* entry
  (`components/palette/CommandPalette.tsx`: `openMoveSession(title)` from
  `lib/sessionActions`).
- **Tests** — `moveSession.test.ts` (the request and its error mapping,
  the mark and its TTL, both events and their subscription, the dialog's
  markup), `sessionMenu.test.ts` (the item's place, its dialog, the greyed
  *moving…*, where it is left out).
### As built: round 12 guide

**The tour** (`lib/tours/sky.ts`, id `sky`, title "Sky") is fourteen steps,
one plain sentence each, in the order a newcomer meets the sky:

| # | Step | Target (hook first, today's class on the same element second) |
|---|---|---|
| 1 | The same sessions, as lines | `[data-tour="view-sky"]` |
| 2 | One line per session (drag the bar, ⋯) | `[data-tour="sky-line-bar"], [data-tour="sky-line"] .skl-bar` |
| 3 | Boxes are stops (the session box opens the terminal) | `[data-tour="sky-session-box"], .sky .fl-box-session` |
| 4 | The bird is where the flight is (click halts, drag jumps) | `[data-tour="flights-bird"]` |
| 5 | Hover a stop for its tools | `[data-tour="sky-stop"], .sky .fl-box.fl-has-tools` |
| 6 | Every stop edits in a dialog | `[data-tour="sky-stop"], .sky .fl-box-stop, .sky .fl-box-group` |
| 7 | The shelf holds the blocks (defaults · saved stops · All blocks…) | `[data-tour="sky-shelf"]` |
| 8 | Stops before a session (grounded line, Session chip) | `[data-tour="sky-grounded"], .skl-grounded-head, [data-tour="sky-shelf"]` |
| 9 | Select an area (move, Group into a stop, Save to the shelf) | `[data-tour="sky-selbar"], .sky-selbar` |
| 10 | Drag or Click (V / H / Space) | `[data-tour="sky-tool"]` |
| 11 | Delete removes a box | `""` |
| 12 | The intake lane (drag it, expand it) | `[data-tour="sky-intake"], .sky-intake` |
| 13 | Zoom and fit (0 / 1 / + / −) | `[data-tour="sky-zoom"], .sky-toolbar .sky-zoom` |
| 14 | The guide | `[data-tour="sky-guide"], .sky-toolbar .tour-btn` |

A selector list matches in **document order** and the top bar comes before
the sky, so the old four-step tour's `…, [data-tour="view-sky"]` fallbacks always
ringed the toggle, never the line or the bird. No step past the first names
the toggle now: a missing element (an empty sky, no flight, nothing selected,
a hook not added yet) centres the card. The one fallback, step 8's shelf,
sits after every line in the page. `tours.test.ts` pins the fourteen first
selectors and that no later step names `view-sky`; the tour-length rule
allows the Sky up to fifteen.

**The guide** (`components/flights/SkyGuide.tsx` + `.css`): a wide `Modal`
(`#sky-guide`, `.sky-guide-modal`, portalled) with the section list on the
left and the section on the right, each with an inline SVG sketch (built
from three small parts — a box, a bar, the bird — in `currentColor` and the
theme tokens). Exports:

- `SKY_GUIDE_SECTIONS` — `{id, title}` × 10: `what` What the sky is ·
  `lines` Lines and boxes · `stops` Stops and saved stops · `bird` The bird ·
  `halt` Halt points and Remove · `grounded` Grounded stops · `select`
  Selection · `keys` Tools and the keyboard · `intake` The intake lane ·
  `saved` Where things are saved.
- `SKY_GUIDE_KEYS` — the keyboard table: V, H, Space (held), 0, 1, + / −, T,
  Arrows, Delete, Esc, Ctrl+A, Enter, Ctrl+Y, Ctrl/Alt+N.
- `useSkyGuide` (zustand `create`, like `DoctorList`'s warn store):
  `{section: id | null, open(section?), close()}`; `openSkyGuide(section?)`
  / `closeSkyGuide()` call it. An unknown section opens the first.
- `SkyGuide(props?: {open, onClose, section?, portal?})` — given `open` it is
  controlled; with no props it renders itself from `useSkyGuide`, so a host
  mounts a bare `<SkyGuide />`. Keyed by the asked-for section, so asking
  again while open follows it. Its **Replay the tour** goes through
  `replayTour("sky")` (onboarding.test.ts lists the files allowed to call
  `openCoachTour`).

**Ways in.** The sky's **?** (`TourButton tour="sky"`, unchanged in
SkyView) is now a two-item menu — *Replay the tour* · *Open the guide* —
an inline DOM menu (`role="menu"`, Esc / a press elsewhere folds it; styles
in `SkyGuide.css`); every other tour's ? is the one button it was. The
palette's **Learn the Sky** runs `setView("sky")` then `openSkyGuide()`.

**For the orchestrator — edits outside this worker's fence:**

1. Mount the guide once in `SkyView.tsx`: `import { SkyGuide } from
   "./SkyGuide";` and `<SkyGuide />` inside both `<main id="sky-view">`
   returns (the narrow one too), e.g. right after `{toolbar}`. Without it the
   ? menu's *Open the guide* and the palette's *Learn the Sky* set the store
   but nothing draws.
2. The `data-tour` hooks the tour asks for (each on the element named, so
   the hook and today's class match the same node):
   - `sky-line-bar` — SkyLine.tsx's `.skl-bar` (session lines);
   - `sky-session-box` — FlywayCanvas.tsx's `.fl-box-session` box (the sky's
     lines; harmless on the Flight tab);
   - `sky-stop` — FlywayCanvas.tsx, the first stop box that wears
     `.fl-has-tools` on a line (one per line is fine: the first visible wins);
   - `flights-bird` — keep it on `.fl-bird-marker` (Bird.tsx) through the bird
     drag work;
   - `sky-grounded` — GroundedLine.tsx's `.skl-grounded-head`;
   - `sky-selbar` — SkySelection.tsx's `.sky-selbar`;
   - `sky-tool` — the new ✥ Drag · ☝ Click toggle in SkyView's toolbar (the
     **only** step with no class to fall back on today: it centres until
     this lands);
   - `sky-intake` — SkyView.tsx's `.sky-intake` slot itself (not the lane's
     head inside it: the slot comes first in document order and would win);
   - `sky-zoom` — SkyView.tsx's `.sky-zoom` group;
   - `sky-catalog` — the shelf's **All blocks…** chip (StopShelf.tsx): not
     used by the tour yet (the shelf, its ancestor, would win a list), named
     for a later step.
### As built: round 12 bird

The owner: "I want to be able to stop a bird where it is working or pick it
up and drag it to an earlier stop." The bird (`.fl-bird-marker`) is in hand
on any line that draws one on a live flight's stop (a line in the Sky, the
pane's Flight tab). **No new canvas prop**: `FlywayCanvas` calls the flight
routes itself (`controlFlight`, `stepRetry`, `jumpFlight`, keyed by
`diagram.flightId`), the way the current stop's toolbar and `pick` already
do, so `useSessionLine` and `useGroundedLine` are unchanged (a grounded
line has no bird).

**Click = Halt now / Carry on / Retry.** `BirdMarker` takes an optional
`hand` (`BirdHand`, Bird.tsx: its title and its handlers); with one it is a
`role="button"`, `tabIndex=0` span (not a `<button>`, so flightsButtons'
tier rule has nothing to pin), Enter / Space press it. Its `aria-label`
stays what it was ("The bird: flying Push" — the Sky's tests pin it); what
a press does is its `title` and `aria-description`, from
`birdAction(tone, onFlight)` in lib/stopEditing:

| Bird | Press | Route |
|---|---|---|
| working, perched on you | **Halt the flight here** | `controlFlight(id, "pause")` |
| paused | **Carry on** | `controlFlight(id, "resume")` |
| halted (a stop failed) | **Retry** | `stepRetry(id, <step it is at>)` |
| on the session box / a rung | — (a picture, `role="img"`) | — |

Paused, the bird sits still: `birdLook` flaps (and bobs) only while the
flock works. When the bird can be dragged, the title adds "· Drag the bird
to another stop to jump there".

**Drag = jump.** Pointer events with `setPointerCapture` on the marker (no
HTML5 DnD, so the shelf's drops are untouched); pointerdown stops
propagation, so a box's reorder drag, the stage's pan, the line's bar drag
and the sky's marquee never start. A mouse lifts it after 4px; a touch after
a 380ms hold (`BIRD_HOLD_MS` — a tap still clicks; a touch that moves first
lets go; the marker is `touch-action: none` and eats the long-press's
context menu). In hand it wears `.fl-bird-dragging`: centred on the pointer
(stage px through `stagePoint`, which undoes the Sky's zoom), scaled
`1.15 / --sky-z` so it stays screen-sized, no transition. The canvas wears
`.fl-bird-carried`; every stop it can land on `.fl-jump-target` (a dashed
accent outline), the one under the pointer (6px slack) `.fl-jump-over`.
Released over one → `jumpFlight(flightId, step, title)`, and the bird
stands on that stop until the flight's perch moves (or 6s; a failed jump
flies it back). Released anywhere else, or Esc → it flies home
(`.fl-bird-snap`, 0.22s). A drag never counts as a click.

**The rules** (lib/stopEditing, tested in `birdDrag.test.ts`):

- `jumpStepOf(n)` — the step a box jumps to: a stop's own, a group's first
  sub-step; null for the session, a branch, a rung, the +.
- `jumpFlightOf(d)` — `{id, at}`: the line's live flight and the step it is
  at (a group's by its `current` sub-step); null with no live flight.
- `canJumpTo(flight, n)` — a main-row box with a jump step that is not the
  step the flight is at: any stop, earlier or later (the server re-arms the
  halt points after the target and pauses there when it is one).
- `jumpTargets(d, flight = jumpFlightOf(d))` — those boxes, in line order.

**The menu.** `StopMenuCtx.jump` (the canvas: `canJumpTo(jumpFlightOf(d),
n)`) adds **Jump the bird here** (`key: "jump"`) to a stop's or a group's
right-click menu, above Remove and the notes — the keyboard's and a
phone's path to the drag. The bird itself has no menu.

Tests: `birdDrag.test.ts` (20: the click by tone, the jump rules on plain,
folded and opened groups, the route, the menu item's place, the marker's
markup in each state, the drag's CSS and its pointer-capture wiring);
stopEditing / skyBirds / flightDiagram / skyBranches / skyVisibility /
flightsButtons unchanged and green.

**Every child a parallel row, each with its own bird.** The owner:
"subsessions should all be in the MindFlock block in parallel, each with
their own bird if they are running." (The Sky draws a child's line only
inside its parent's.) In `sessionDiagram` every child that is not a worker
spawned at a stop — the plain ones, one spawned at a stop not in this
flight, one re-parented — now hangs the way workers do: a parallel row off
the line's current place (`hangPoint`: the flight's current stop, else the
fast-track's current or halted rung, else the session box) and converging at
the box after it (the next stop, or the +). It stays a `child` box with its
`child:<title>` id and its chip and mini ladder. It stays in row `below`, so
reading order keeps the children last. What is new is a `branch` link
(`{from, to, state, inner, live}`): the wire out is the family's `branch`
wire, drawn the way a fan-out is, and the wire back in is a `join`, done once
the child's turn ended. The line reaches the next box through them, the
direct wire is dropped, and the flight never waits on them.

- **Layout:** `layoutDiagram` stacks a hung child in the same column as the
  stop's workers, under them (an indented group on a phone).
- **Reordering:** `reorderDiagram` re-targets the child's fan-in along with
  the workers'.
- **States** (`childState`): working, asking, failed (by its report or a
  broken session), else done.
- **The +:** `insertGaps` keeps the + after a stop whose column holds only
  hung children, drawn between the stop and the column. A worker column
  still has none, because the line reaches its join point through the
  workers.

**Birds.** `birdPerches(diagram): BirdPerch[]` returns the main bird
(`birdPerch`, unchanged and still exported) first. After it comes one bird
per branch whose session works (`tone: "working"`) or waits on you (`"you"`),
counting workers and hung children alike; an idle one has none. Each of those
carries `child` (the session's title). `BranchInfo.live` holds it, from
`deriveSessionState`. The canvas draws them on any line whose host passes
`bird` at all (null too: SkyLine and SessionFlight pass `birdPerch(d)`, the
flyway editor passes nothing), so no host changed.

A branch's bird is smaller (18px), `.fl-bird-branch`, and sits centred on
the branch box's left edge, where its wire comes in. With `onOpen` it is a
`role="button"` ("Open <child> — working"): a click or Enter opens that
child, as a click on its box does, and it never drags. Without `onOpen` it is
a picture. `BirdMarker` gained a `className` prop for this. The main bird
keeps its click (halt / carry on / retry) and its drag (jump).

Tests: skyBranches.test.ts (8 more: the hanging rows off a stop, a rung or
the session box, the phone layout, the + kept, reordering, `birdPerches`, the
canvas's branch birds) and skyBirds.test.ts (the marker's class). The tests
of the old "children below" picture (flightDiagram.test.ts,
skyBranches.test.ts) now pin the parallel rows. flightsUi, skyLine and
stopGroups are unchanged and green.
### As built: round 12 move backend

- **Route** (`server.py`, route only): `POST /api/instances/{title}/move
  {dest, force}` → `core/move_session.check` (on a worker thread) → **202**
  `{title, from, to}` and `move_session.start` runs the move as a task on the
  server's loop. Refusals answer `{error, code}`: 404 `unknown_session`, 400
  `bad_dest` / `bad_source` (a repo's sub-folder), 409 `exists` / `working` / `in_flight` / `moving` / `remote` /
  `peer_share` / `not_ready`. `working` reads `_agent_activity` (the probe
  ship-now uses); `in_flight` is `flights.for_session` in `ACTIVE` (running /
  waiting on you — a paused flight does not hold it) or an autopilot record
  with `state == "running"`. `force` skips both. A device row (`dev::title`) is
  proxied to that device before the route runs; `check` refuses one anyway.
- **Background run** (`move_session.run`): the tmux env is rebuilt first (port
  block + auth-profile / local-model overlay, as the resume route does), then
  the engine's `MoveWorkspace(ENGINE.instances, title, dest, save=ENGINE.save)`;
  every unfinished flight whose `repo_root` was the session's old repo path
  (an in-place session or a main clone moved) is pointed at the new one; a
  sleep record is dropped (the agent came back up awake). Then
  `session.moved` (`old`/`new` = the folders, data `{title, from, to}`) or
  `session.move_failed` (data `{title, error, path}`, `path` = where the
  workspace is now). `move_session.moving(title)` → `{from, to}` while it runs
  (not yet on the row JSON: `server._instance_json` is outside this round's
  backend fence — the frontend shows "moving…" from the 202 until the event).
- **Engine** (`session/instance.py`): `Instance.MoveWorkspace(dest)` +
  module-level `MoveWorkspace(instances, title, dest, save)`, `CheckMoveDest`,
  `MoveError(code)`; Resume's current-scheme relaunch became
  `_relaunch_in_worktree()`, shared with the move. Exact steps in
  docs/session-engine.md.
- **Providers**: `carry_workdir(old, new)` on the base (no-op); Claude copies
  `projects/<slug old>` → `projects/<slug new>` in every config root; Codex is
  an explicit no-op (rollouts are keyed by session id); generic inherits the
  no-op.
- **Not handled**: a cross-filesystem move of a folder other windows' agents
  are running in (their cwd goes with a rename, not with a copy); Codex's
  per-session token usage is matched by `cwd`, so turns before the move stop
  counting toward the session.
### As built: round 12 new block

`NewSessionBlock` (components/flights/) keeps its rows — Repository, Task,
Stops, Agent (chips when more than one CLI) — its drag bar, Esc, Enter-flies
and the Cancel / Fly footer as they were. Under the rows sit two folds, their
heads the quiet tier (`fl-icon-btn sky-new-fold`):

- **More options** (`mf_sky_new_more` = "1" remembers it open; folded by
  default). Folded, one muted line says what it holds — `optionsSummary`:
  "claude · opus · worktree from main", then "“Template”" and "on <device>"
  when picked (the model is a key account's pick, else a `--model` / `-m` /
  `--model=` in the launch flags, `modelOf`). Open (`MoreOptions` in
  `newBlockOptions.tsx`), all chips (`ChipRow`) or plain inputs, never a
  `<select>`:
  - **Account** — App default / CLI's own login / each profile (this device
    only, as the dialog). The dialog's `accountSetter` is reused as is, so a
    key account steers the agent and an OpenRouter key fetches its models.
  - **Model** — for a key account (kind ≠ `account`): a text field with the
    fetched models as a datalist (`profile_model`).
  - **Workspace** — Worktree / In place. A template's provisioned workspace
    is carried and shown as a line, not offered.
  - **Base branch** — Default (<the repo's default>) plus up to six branches
    (`branchChoices`: default, checked-out, local, remote) and an "Another
    branch…" field past six. The branches come from the dialog's own
    `useBaseBranches` (re-asked per folder, the pick reset with it).
  - **Template** — None + the server's templates (`ensureTemplates`); a pick
    fills the block through the dialog's `templateFiller` (agent, folder,
    task, workspace); None drops a template's provisioned workspace. Its
    fast-track is ignored: the block flies stops.
  - **Launch flags** — the field (pre-filled with the agent's saved defaults,
    reset when the agent changes) plus the dialog's `FlagChips`.
  - **Device** — This device + each startable device (`startableDevices`,
    `deviceLabel`), only when there are any. Another device drops the account
    and the base, and the POST goes there (`postInstance`: `deviceApi` +
    `deviceTitle`, as the dialog's `postCreate`).
- **From a session** — `ParentPicker`: this device's live sessions
  (`parentChoices`: no `device`, not `pending_create`, `hidden_by` or
  `locked`, with a `path`), by `windowName`, as `fl-pick` chips; a filter
  field past eight (`PARENT_FILTER_AT`, `filterParents`). Picking one closes
  the fold and forks: the Repository row reads "forked from **<name>**" with
  the parent's repo · branch and a **Clear** (`test-btn`) that restores the
  plain flow (the typed folder is kept underneath). The worker runs the
  parent's agent when that is one of the choices. Folded, the summary adds
  "· as <title>".

**One body.** `blockBody(choice)` fills the New dialog's
`newSessionSubmit(ctx).buildBody` ctx from the block (program, launch flags,
account + model, workspace, base, template, device) — the only body shape.
A fork goes through the same builder: `forkOptions` forces a worktree on this
device with `baseBranch` = the parent's branch (so `base_ref` / `base_branch`
cut it at the parent's HEAD and read its diff against it, as `spawn_session`
does), `repo_path` = the parent's `path` (its repository), the title is
`workerTitle(parent, taken)` — the lowest free `<slug(parent)[:40]>-wN`, the
MCP's own rule — and `forkFields` adds `parent` + `spawned: true`:

```json
{"title": "r12-w1", "program": "claude", "repo_path": "/code/app",
 "prompt": "…", "launch_args": [], "init_repo": false, "in_place": false,
 "base_ref": "mf/r12", "base_branch": "mf/r12", "parent": "r12", "spawned": true}
```

It is still `api<Instance>("/api/instances", { json: body })` after
`addPendingSession` (aimed at the device) and `onPending`; a picked saved stop
then takes off (`startFlightOn`), a grounded line's stops attach.

Tests: `newBlock.test.ts` (the pure helpers), `newSessionBlock.test.ts` (the
fold closed / open markup, every option and the fork in the body, the POST
with a mocked api, the picker). The older pins in `intakeLane.test.ts`,
`flightsButtons.test.ts`, `grounded.test.ts` and `skyStable.test.ts` pass
unchanged.
### As built: round 12 sky

> Code: `frontend/src/components/flights/SkyView.tsx` (placement, the press
> handling for both tools, Delete), `lib/skySelect.ts` (`gestureFor`, the tool
> store, `deleteFor`, `nodeById`, `removableBox`), `lib/skyLayout.ts`
> (`INTAKE_KEY`, `INTAKE_OPEN_W`, `intakeSize`), `IntakeLane.tsx` (+ `.css`),
> `SkyLine.tsx`, `GroundedLine.tsx`, `SkySelection.tsx`, `lib/keymap.ts`
> (`skyBoxFocused`), `Sky.css`, `SkyLine.css`. Tests: `skyTool.test.ts`
> (new), `skySelect.test.ts`, `skyIntake.test.ts`, `intakeLane.test.ts`,
> `keymap.test.ts`, `skyView.test.ts`.

**The intake lane is a sky object.** Its layout entry is `"intake"` in
`mf_sky_layout`: `x` / `y` / `pinned` like a line's, and `open` = expanded
(never a pane: `estimateLine` is not used for it; `intakeSize` is its measure,
else 240 × 320 folded or 420 × 520 expanded). A session titled `intake` would
share the entry. That is a known edge, left alone because the contract names the key.

- Placement: the lane is the FIRST item of the `assignSpots` call (`items =
  [intake, ...lines]`, area left `SKY_MARGIN`), so unpinned it takes the
  plane's corner and the lines flow to its right and under it. Its room is
  never under the folded width (`max(measured, INTAKE_W)`), so unfolding the
  rail never runs into a line. No line keeps an auto spot before the lane has
  one (`intakeReady`: its spot kept, pinned, measured, or the 0.9 s wait), so
  the lines are placed around its real size. This session (one line) has no lane.
- It drags by its head (`.sky-lane-head`, or the folded rail's background)
  through SkyView's `onDragStart(INTAKE_KEY, e)`: the line drag's own path,
  generalised to "a sky object with a layout entry". It is pinned where it
  is dropped. A click on its head focuses no session. Tidy and "Tidy these"
  forget its spot and keep `open`. It glides with the slots
  (`.sky-plane[data-tidying] .sky-intake`).
- Expanded (▸ / ▾ `.sky-lane-expand` on its head, or a double-click on the
  head): `width` 420, every waiting item of every source (`shownWaiting(block,
  Infinity)`), headlines and references unclamped (`.sky-lane[data-open]`),
  lifted over the lines (`z-index: 5`). The ‹ fold to the rail stays.
- Measured like a line: a ResizeObserver on the lane's root → `onMeasure`
  (re-armed when the settings load and on the rail fold).
- The marquee takes it as a line (its head is its bar). Selected, it wears a
  dashed outline. It moves and tidies with the selection. `SkySelection`'s
  Remove counts only the other lines (`removable`) and hides when the lane is
  all there is.

`IntakeLane` gained: `open`, `onToggleOpen`, `onHeadDown`, `draggable`
(false = click mode: the head starts no move), `onMeasure`.

**Drag mode / click mode.** `skyTool` in localStorage `mf_sky_tool`
(`loadSkyTool` / `saveSkyTool`, "drag" unless it says "click"). The toolbar's
toggle is a `ChipRow` with `className="sky-modes sky-tools"` (the mode chips'
tier, `.fl-pick`). V = drag and H = click, when no field is focused. Space held is
click mode while held (`liveTool`). `.sky[data-tool]` carries the live tool:
in click mode the hand (`grab`) shows over the lines and boxes, `grabbing`
while it pans (`.sky-viewport.is-panning`).

`gestureFor(tool, target, {shift, space, touch})`. `target` is from
`skyTargetOf`: `control` (a button, a field, `[data-no-drag]`, an open card, a
line's editor, a menu, the selection bar, the new-session block), `box` (a
`[data-node]`, or any other part of a line), `line` (`.skl-bar`, the lane's
head or rail), `ground`.

| press on | drag mode | click mode |
|---|---|---|
| empty sky | marquee (touch: pan) | pan; a still press clears the selection |
| a line's bar / the lane's head | move it (and the other selected lines) | pan; a still press focuses the line |
| a box | the box's own (its click selects it, its drag reorders) | pan; a still press is its own click: selected, and given the keyboard |
| a control | its own | its own |
| Shift+drag | marquee on empty sky; Shift+click on a bar toggles the line | marquee from anywhere; Shift+click on a line toggles it |
| Space held | pan from anywhere | pan from anywhere |

Click mode works in the viewport's CAPTURE phase. Every press but a
control's is stopped there, so no bar, box or resize handle starts a move. The
pointer is captured only once the press has moved 4px (`MARQUEE_MIN`): a still
press keeps its target, so the box's own click and double-click land on it
(select; Enter / double-click opens, as before), and `clickOn` focuses the line
and gives the box (or the line) the keyboard. `SkyLine` and `GroundedLine`
take `draggable` (false in click mode: the bar starts no move), and
SkyView's `onDragStart` checks `gestureFor(liveTool, "line") === "move-line"`
too.

**Delete removes the selected box.** SkyView takes Delete / Backspace in the
window's capture phase (ahead of the keymap) and routes it with
`deleteFor(selection, focus, selectedBox)`:

| where the keyboard is | Delete |
|---|---|
| the sky holds a selection (focus on the sky, not on a box) | the bar's Remove, as in round 11 |
| a stop or group box (focused) | that box leaves its line, through the canvas's own `onRemove`: the undoable toast |
| a session box (`session:`), or a worker's / branch's (`child:` / `branch:`) | "Close “name”?" inline on the line (`.skl-close-confirm`), then `closeSession` with its Undo toast |
| a session line's bar or the line itself | the same close question for the line's session |
| a grounded line (its head, its bar, the line) | its own "Remove “name”?" inline confirm |
| the +, a rung, the flown pill, the intake lane | taken; nothing happens (never the keymap's close) |
| the sky's empty space or the page, with the focused line holding a selected box (`.fl-box.selected`) | as if that box were focused |
| an open card's pane, a field, a menu, a question, outside the sky | not the sky's: the keymap as before |

How the sky reaches a line's box: each line registers a handle
(`registerDelete(fn | null)`, `LineDelete = (node) => boolean`, `""` = the
line itself) that SkyView keeps per line id (`registerFor`, stable per id). I
chose this over lifting "the selected node per line" into SkyView, because the
selection, the flight and the remove path (`useSessionLine` /
`useGroundedLine`'s canvas, the undo toast) already live in the line. The
handle only asks the line to do what its own Delete key or toolbar does. A
press on a line's bar now focuses the line's root (`tabIndex={-1}`), so Delete
has somewhere to land.

The keymap's Delete alias yields to `skyBoxFocused()` (`lib/keymap.ts`). It
yields when the focus is inside `.sky`, not inside an open card's `.pane` /
`.skl-card`, and either on a `[data-node]` that is not `session:` or inside a
`[data-sky-box]`: a session line's root, a grounded line's root, or the
viewport while the selection holds something. Ctrl+W is unchanged.

Checked in a browser (a scratch `vite build` + the demo mocks): the lane
drags, pins and expands to 420; a drag-mode bar drag moves its line; in click
mode the same drag pans and the line stays, a box drag pans, the lane's head
pans, Shift+drag draws the marquee (it takes the lane), a click on a grounded
stop selects it and Delete removes it (`DELETE …/steps/s1` and the Undo toast);
Delete after a click on a session line's bar shows the close question and sends
nothing.

**Workers live inside their lead's line** (the owner: "the subsessions … should
all be in the MindFlock block in parallel, each with their own bird").
`skyLineTitles` no longer gives a worker a line when its parent is itself a
line in that view (All, Mine, Perched on you). It is drawn only inside the
parent's line, as a branch (`sessionDiagram` with `family: true`; SkyLine
always drew the family, and SkyView's size estimate now does too). A click on
its box there opens it as before. The rule is a fixed point: a worker is a line
exactly when its parent is not one. So a worker whose parent is gone, hidden,
filtered out by Find, not perched (in Perched on you), or itself folded (a
worker's worker) keeps its own line, with its parent's box above. This session
still shows a worker's line alone. A folded worker's layout entry is simply
unused. All and Mine now draw the same lines; `lineFamily` is gone (nothing
read it).

**Ctrl+Y switches views** (the owner: "I want Ctrl+Y to go to the sky instead
of Alt+Y"). The keymap's `view` entry is `{key: "y", mod: true}`: Ctrl+Y, or ⌘Y
on a Mac, like Ctrl+P / B / N. Alt+Y is gone. The guard is unchanged: it fires
from a terminal, as Ctrl+P / B / N do, so it takes readline's C-y yank there.
A text field keeps it (redo), and nothing happens behind a dialog.

### Round 12 follow-ups (the owner, live, 2026-10-30)

- **Shift+click on a box** adds it to (or takes it out of) the selection:
  `toggleStop` in `lib/skySelect.ts`; a stop selection is always one line's,
  so a box of another line, or a line selection, gives way. The canvas's
  `onShiftSelect` prop carries it up through SkyLine / GroundedLine.
- **What sits on the shelf is a pick** (`mf_sky_shelf`, `ShelfPick {kinds,
  plans}` in `lib/stopCatalog.ts`): the five default blocks and NO multi-step
  block to begin with. Right-click any chip → **Take off the shelf**; every
  catalog row has a ★ / ☆ **On the shelf** toggle. The Multi-step region shows
  only when a saved stop is picked.
- **The intake lane resizes by its corner** (`w`/`h` on its layout entry,
  `intakeFrame` clamps 200–1200 × 160–1600; the handle is `.sky-lane-resize`,
  the drag divided by the sky's zoom).
- **Removing a flight's last stop cancels the flight** (`_last_ones`, reason
  "its last stop was removed") instead of 409 `last_step`: the box goes.
- **A halted flight's steps can change** (`_editable` takes every UNFINISHED
  state): tack on, edit or move pending stops, then Retry / Skip.
- **The bird follows the work**: a flight halted or paused at a stop while the
  agent is busy puts the bird on the session box (working), not on the stop.
- **A click on a flying or flown stop opens its dialog** (a pending one still
  selects; ✎ / Enter / double-click edit it). The read-only dialog shows
  **What it did**: the flight's history on that stop, its note, a command's
  output tail, and for a ship stop the fast-track's state / step / reason
  (`stopLog` in `useSessionLine.tsx`). Ship lanes run in the server, not in
  the terminal, so this is the place to look.
- **Ctrl+Y** (⌘Y) switches Grid ⇄ Sky; **zoom** goes down to 10%.

## Round 13: a diagram tool's manners (contract)

The owner, after round 12: delete and name the non-built-in blocks I made;
adding a block must NOT land it in the selected session — it goes on the
board and I drag it in (the line's own + is what adds straight in); no
session selected at all in the sky; the All blocks menu larger; the blocks
as a block of their own I can size and drag from, with the bottom bar still
an option. "This should be as simple as any diagram drawing software."

The principles, made explicit (every surface below follows them):

1. **Nothing lands anywhere by itself.** A palette click or Add puts a block
   on the board near the palette, unselected, as a grounded line. The only
   implicit targets are the ones you point at: a drop on a line, a line's own
   +, a drag of a grounded line onto a session's line.
2. **Direct manipulation.** Click selects. Double-click (or ✎ / Enter) edits.
   Drag moves. Delete deletes. Esc clears. Dragging one thing onto another
   combines them. Dropping on empty sky places.
3. **Every object you made has a name and can go.** Saved stops: rename and
   delete anywhere they appear (built-ins: reset). Grounded lines: rename
   their title inline; Delete removes them. Groups: rename in their dialog.
4. **Selection may be empty.** The sky never picks a session for you; a click
   on empty sky, or Esc, leaves nothing focused. The pane strip, the drawer
   and the keyboard actions cope with "nothing".
5. **Palettes dock or float, your choice.** The same blocks are reachable
   from the bottom shelf, from a floating resizable palette on the board, and
   from a large catalog — one pick of what shows, shared by all three.

### Backend

- `PATCH /api/flights/{id} {"plan_name": "<name>"}` → `{flight}`: renames a
  flight (its line's title; a grounded one's especially). ≤120 chars, 400
  `bad_name` when empty; 404 `unknown_flight`; works in any unfinished state.
  History `edited` ("renamed to <name>").

### The palette (frontend: StopShelf / StopCatalog / SkyPalette)

- **Click = place.** A chip click, a catalog row's **Add**, Enter in the
  catalog: `onPlace(drop)` — the host makes a grounded line at a free spot
  near the palette (bottom shelf: above it, centred; floating palette: to its
  right) and selects nothing. No chip click ever tacks onto the focused line.
- **The catalog is big**: a `Modal` sized to the window (≈ 92vw × 86vh), the
  blocks as a grid of cards (icon, name, actor, summary; the saved stops with
  their step boxes), a filter, keyboard as before. Each saved-stop card has a
  ⋯: **Rename** (inline), **Delete** (yours, inline confirm) or **Reset to
  default** (built-in), **On the shelf** (★/☆).
- **Floating palette**: `SkyPalette` — the shelf's content as a sky object
  (layout key `"palette"`: x / y / pinned / w / h), a head to drag it by, a
  corner handle to size it, the regions flowing in a grid inside, every item
  draggable onto lines or the sky exactly as a chip, a ⋯ with **Dock at the
  bottom**. The bottom shelf's ⋯ (or a ⤢ on its handle) offers **Float the
  blocks**. `mf_sky_palette: "shelf" | "float"` remembers the choice; the pick
  (`mf_sky_shelf`) is shared.

### The sky (frontend: SkyView and the lines)

- **Nothing focused** is a state: entering the sky keeps whatever the grid
  had but never picks a line itself; a click on empty sky, or Esc with no
  selection and no dialog, calls `selectSession(null)`-equivalent (the store's
  `setFocused(null)`); no line wears `.is-focused`, the pane strip hides, the
  drawer shows the whole flock, Delete / Enter do nothing. "This session" mode
  with nothing focused shows the mode's empty note.
- **Place near the palette**: `placeGrounded(drop, near)` — a free spot
  (assignSpots-style) just above the shelf or right of the floating palette,
  within the view; the new line is pinned there, unselected, unfocused.
- **Drag a grounded line onto a session's line** (its bar, drag mode): the
  target line lights (`.is-merge-target`) while the pointer is over it; on
  release the grounded flight's steps go onto that session — `attachGrounded`
  when the session has no flight, else `tackOnSteps` as one group named after
  the grounded line — and the grounded flight is deleted. A toast with Undo
  (re-create the grounded flight from the same steps). Dropping elsewhere
  just moves it.
- **Grounded line rename**: a click on its name (or F2) edits inline →
  `renameFlight(id, name)`; Esc cancels.
- The floating palette is a sky object like the intake lane (drag by head,
  corner size, assignSpots, Tidy forgets its spot, marquee ignores it).

### As built: round 13 palette

Source: `components/flights/StopShelf.tsx` (+ `.css`), `StopCatalog.tsx`
(+ `.css`), `SkyPalette.tsx` (+ `.css`), `lib/stopCatalog.ts`; tests in
`stopShelf.test.ts`, `stopShelfSaved.test.ts`, `stopCatalog.test.ts`,
`skyPalette.test.ts`, `flightsButtons.test.ts`.

**What the host passes.** Nothing here changes a drop: every chip and card
still drags with the round-12 MIMEs and SkyView reads them as before.

| Surface | Props the sky passes | What they do |
|---|---|---|
| `StopShelf` | `onPlace(drop)`, `onFloat()` (new, optional), and as before `onTack` / `onTackPlan` / `onSession` / `folded` / `onFold` | With `onPlace`, a chip click and the catalog's Add / Enter / double-click call `onPlace({kind} \| {planId})` — never `onTack` / `onTackPlan` (those stay for the pane's Flight tab, which passes no `onPlace`). `onFloat` shows the ⤢ beside the fold handle. |
| `SkyPalette` | `onPlace`, `onDock`, `onSession`, `onHeadDown` + `draggable`, `onResize(w, h)`, `onMeasure(w, h)`, `width` / `height` (the layout entry's), `zoom` | Exactly the placeholder's interface — no prop was added. Without `onPlace` its chips only drag; without `onDock` no ⋯; without `onResize` no handle. |
| `StopCatalog` | (opened by the two above) `place?: boolean` added | `place` only words the hints ("Add it to the board" / "puts it on the board"). |

`CatalogDrop` is `{kind: string} | {planId: string}` (lib/stopCatalog). The
host places a grounded line near the surface that asked (the contract's
"Place near the palette"); the palette and shelf can't tell the host where
they are beyond `onMeasure` and the layout entry the host already keeps.

**Click = place.** `StopChip` / `SavedChip` take `onClick` and `place`; with
`place` the button's label is "Put “Ship” on the board" and its title "Click
to put it on the board, or drag it onto a line or the sky" (a perch, a saved
stop alike). The shelf picks `clickKind = onPlace ? (kind) => onPlace({kind})
: onTack` (and `clickPlan` likewise); its catalog `add` closes the catalog and
returns `onPlace(d)` first.

**The big catalog** (`StopCatalog`, still a `Modal` `#sky-catalog`, the
drag-through-the-Modal rule unchanged): `.sky-catalog` is `92vw × 86vh`
(held 8px inside the window; a phone ≤ 640px gets the whole screen), the head
is the title, the filter (focused) and ×; the list (`.sky-own-scroll`, so
the sky's wheel leaves it alone) holds **Blocks** and **Multi-step**, each a
`.sky-catalog-grid` (`repeat(auto-fill, minmax(230px, 1fr))`) of
`.sky-catalog-card`s (`role="option"`, draggable, `data-kind` / `data-plan`):

- a kind's card: the icon tile (a perch round and gold), the name, the
  `robot` / `you` badge, ★ / ☆ (with the shelf's pick), the summary (two
  lines), and the **Add** (`test-btn`, on the picked / hovered card);
- a saved stop's card: the name, a dashed `built-in` badge, ★ / ☆, a **⋯**
  (the house `OverflowMenu`, flipping up against `#sky-catalog-list`), a row
  of step boxes joined by a thread (`planDots`: a perch's round and gold, +N
  past ten), the path "a → b → c" (`planPath`), and Add.

The ⋯ (`cardMenu(plan, shelved)`): **Rename** (when `renamable` — every
stored plan, built-ins too: the server keeps a renamed built-in a built-in;
a depth cut `builtin:x@lane` is virtual, so it gets none) → the name turns
into `PlanNameField` on the card; **★ / ☆ On the shelf**; **Reset to
default** (a built-in → `resetPlan`) or **Delete** (yours → an
`InlineConfirm` on the card → `deletePlan`; Esc answers the question before
it closes the catalog). While renaming or asking, the card doesn't drag.

Keys, with the focus in the filter: ↑ / ↓ a row (into the next section's
first row and back, keeping the column), ← / → one card (only from the
filter's edge, else they move the caret) — `gridMove(at, key, [blocks,
multi], cols)`, the column count read from the laid-out grid; Enter adds the
picked card; a double-click on a card adds it. The filter now also matches a
saved stop's step titles ("push" finds the stops that push).

**The card model** (lib/stopCatalog, pure): `catalogCards(kinds, plans,
query)` → `{blocks: KindCard[], multi: PlanCard[], all}` (a card = `section`,
`key` `kind:<k>` / `plan:<id>`, `drop`, `name`, and `perch` + `def` or
`builtin` + `path` + `plan`); `gridMove`; `renamable`; `cardMenu`.

**The floating palette** (`SkyPalette`): a `section.sky-palette`
(`aria-label="Blocks palette"`, `data-tour="sky-palette"`), its frame
`paletteFrame(width, height)` — 360 × 420 when the host keeps none, never
under 240 × 200. The head (`.sky-palette-head`, `data-sky-head`; `.movable`
+ "Drag to move the blocks" when `draggable && onHeadDown`; a press on its
buttons / ⋯ never moves it): **Blocks**, a count (the kinds and saved stops
on it), **All blocks…** (`test-btn`, opens the same catalog with
`place`), a ⋯ → **Dock at the bottom** (`onDock`). The body
(`.sky-palette-body.sky-own-scroll`): the Blocks region (Session, the
picked kinds) and the Multi-step region (the picked saved stops), each a grid
(`minmax(108px, 1fr)`; a saved stop spans two columns) of the shelf's own
chips with `place`, right-click menus and Rename included, then a muted hint.
The corner `.sky-palette-resize` is the intake lane's: pointer capture, the
frame drawn live as `dragFrame(start, dx, dy, zoom)` (the delta over the
zoom, clamped), `onResize(w, h)` on release; the arrow keys on the focused
handle size it 20px a press. A ResizeObserver reports `onMeasure(w, h)`
(plane px, as offsetWidth / offsetHeight). Esc does nothing special.

**What moved where.** The chips (`StopChip`, `SessionChip`, `SavedChip`),
the pick (`useShelfPick`) and a saved stop's right-click menu with its
Rename / Takes off… / Reset / Delete (`useSavedActions` → `confirm`,
`overlays`) are exported from StopShelf.tsx; the rename field is
`PlanNameField` in StopCatalog.tsx (the shelf's chips, the palette's and the
catalog's cards all use it — `checkRename`, then `savePlan`). `useShelfPick`
stores `mf_sky_shelf` and fires `mf:sky-shelf` on window with the new pick
(and listens for it and for `storage`), so the shelf, the palette and an
open catalog always show the same pick. The shelf's fold handle now sits in
`.sky-shelf-tabs` (absolute over the shelf's top edge) next to the ⤢
`.sky-shelf-fold.sky-shelf-float` (the same quiet tab).
### As built: round 13 sky

> Code: `frontend/src/components/flights/SkyView.tsx` (`placeGrounded`,
> `mergeGrounded`, `unfocus`, the floating palette's slot), `GroundedLine.tsx`
> (+ `.css`: the inline rename, `.is-merging`), `SkyLine.tsx` (+ `.css`:
> `.is-merge-target`), `Sky.css` (`.sky-palette-slot`), `lib/skyLayout.ts`
> (`spotNear`, `objectFrame`, `paletteFrame`, `PALETTE_*`, `PLACE_GAP`),
> `lib/skySelect.ts` (`mergeTargetAt`), `lib/sessionActions.ts`
> (`selectSession(null)`). Tests: `skyMerge.test.ts` (new), `sky.test.ts`,
> `skyView.test.ts`, `grounded.test.ts`, and the pins that moved in
> `skyStable` / `skyTool` / `skyZoom`.

**Place, don't tack.** The shelf gets `onPlace={(d) => placeGrounded(d,
"shelf")}` and `onFloat`; `SkyPalette` gets `onPlace={(d) => placeGrounded(d,
"palette")}`. The shelf no longer receives `onTack` / `onTackPlan`, so no chip
click reaches a line. `placeGrounded` makes the grounded flight with
`makeGrounded`, the API half of `dropGrounded`. It then pins the flight at
`spotNear(want, taken, size, view)`:

- `want` is just above the shelf, centred in the view, or for the palette
  `PLACE_GAP` to the right of the floating palette, level with its top.
- `taken` is every line, the intake lane and the palette.
- `view` is the visible plane inset by 16 screen px.

`spotNear` walks rings of `PLACE_GAP` (24 px) outward from `want`. It keeps
the nearest free spot inside the view, else the nearest free spot anywhere.
The spot is computed with an estimate (`lineSize` + the head box). Once the
new line is drawn and measured, `placing` places it once more at its real
size, the same rule `assignSpots` follows. Nothing is selected or focused, and
a drop on empty sky (`dropGrounded`) no longer focuses its line. In This
session or Perched on you, where grounded lines aren't drawn, a toast says
where the block went. The old fallbacks are gone: `onFly` / `onTack` without a
title, `tackPick`, `flyPick` and their rows in the line picker. Both functions
now take a REQUIRED title and serve only a drop on a session's line. The picker
keeps ⋯ Fly on a session….

**Nothing focused.** `selectSession(null)` sets `focused` to null and returns.
It leaves the MRU, the hidden set and the keyboard alone, and the grid's focus
clears too. SkyView's `unfocus()` clears the grounded focus and calls it, and
these presses reach it:

- a still press on empty sky: drag mode's marquee release, or click mode's
  `clickOn` with no object under it;
- Esc, once the sky's cards, the selection and an open box have had their Esc,
  with no dialog and no marquee.

Removed automatic picks:

- `setMode` picking `titles[0]` for This session (it now shows "Pick a line to
  show it alone.");
- the new-session block's `onCreated` focusing its session;
- the intake lane's `onFly`, which focused the session it made and now only
  flashes its line (`flashLine`);
- `dropGrounded`'s `setGroundFocus`.

The explicit picks stay: a bar or box click, the drawer's perch names, Find's
Enter, the line picker. With nothing focused:

- no line wears `.is-focused`;
- Enter does nothing (it needs `focusLine`);
- Delete falls through to the keymap, which needs a focused session, so it
  does nothing;
- the drawer always showed the whole flock and needed no change;
- the pane strip (`PaneFlightStrip`) lives inside a session's own pane, so it
  shows only where a pane is open.

`lastFocus` resets when nothing is focused, so selecting the same line again
from the rail brings it back into view.

**Drag a grounded line onto a session's line.** In drag mode, a grounded line
dragged ALONE (not as part of a selection) hit-tests the other lines' rects on
every move: `mergeTargetAt(planeAt(pointer), others, id)`. The test takes the
topmost session line containing the point, and never a `flight:` line, the
intake lane, the palette or the dragged line itself. The target line wears
`.is-merge-target` (an accent ring and a tint). The dragged line wears
`.is-merging` (see-through, accent edge). A release over a target does not pin
the line. It calls `mergeGrounded`:

1. If the session has no live flight: `attachGrounded(f.id, {title})`.
   Otherwise: `tackOnSteps(live.id, f.steps, undefined, f.plan_name)` (one
   group named after the line), then `deleteFlight(f.id)`.
2. A toast says "Moved N stops onto <windowName(title)>". Its Undo calls
   `startGrounded({steps, name})` and pins the new flight where the line
   started its drag.

A release on a grounded line or on empty sky just moves the line.

**Rename a grounded line.** Its bar's name (`plan_name`, or "Stops") is a
`.skl-name-edit` span. A click on it renames, unless the press had moved 4 px
or more (that was a drag). F2 with the line's root focused also renames. The
name becomes an `fl-input`: Enter or blur saves through `renameFlight` when the
trimmed name (`cleanFlightName`, at most 120 characters) changed, and Esc
cancels. A `named` ref ends the edit once, so the blur that follows Enter or
Esc doesn't save again.

**The floating palette.** `mf_sky_palette` ("shelf" | "float", read and written
in SkyView, default "shelf"; a phone always gets the shelf). When floating:

- The shelf host mounts no `StopShelf`; the TakeoffDialog stays.
- A `.sky-palette-slot` on the plane holds `<SkyPalette>`, with `width` /
  `height` from `paletteFrame`, `zoom`, `onHeadDown → onDragStart(PALETTE_KEY,
  e)`, `draggable`, `onResize → onLayout(PALETTE_KEY, {w, h})`, `onMeasure`,
  `onPlace`, `onDock → "shelf"` and `onSession = newHere`.
- The slot carries the shelf's `onDragStart` / `onDragEnd`, so its blocks drag
  and drop exactly as the shelf's chips do.
- Its layout entry is "palette". It is second in `assignSpots`, after the
  intake lane, and keeps its spot at once, as the lane does. In This session it
  sits under the one line unless pinned.
- Tidy forgets its spot and keeps its `w` / `h`.
- `objectFrame(stored, min, max)` is the generalised clamp. `intakeFrame` wraps
  it with the lane's 200–1200 × 160–1600, and `paletteFrame` with 220–1400 ×
  140–1200.
- The marquee never takes it: `selectables` lists only the lines and the lane.
- It is in `SKY_CONTROLS`, so a press on it is its own and never pans or draws
  a marquee. Keyboard focus inside it is not the sky's for Delete
  (`deleteFocus`).
- A click on its head clears the selection and focuses nothing.

Checked in a browser (a scratch `vite build` of a copy of `frontend/` plus the
demo mocks; in the copy, StopShelf routes a chip click to `onPlace`, as the
palette worker's version will):

- **Nothing focused:** a bar click focused its line, and a click on empty sky
  left none focused. Bar click then Esc left none focused. This session with
  nothing focused showed "Pick a line to show it alone." Delete and Enter with
  nothing focused sent nothing.
- **Placement:** an Agent chip click made `POST /api/flights/start {grounded}`
  and put the line just above the shelf (its bottom 16 px over the shelf's
  top), unfocused. A second click landed directly above the first.
- **Rename:** click the name, type, Enter sent `PATCH /api/flights/f_…
  {plan_name}`; Esc sent nothing.
- **Merge:** dragging "Grounded stops" by its bar over a session line lit that
  line (`.is-merge-target`) and dimmed the dragged one (`.is-merging`). The
  release sent `POST /api/flights/f_g1/attach {title}` and toasted "Moved 2
  stops onto …" with Undo. Undo sent `POST /api/flights/start` with the same
  two steps and pinned the new line where the old one had been.
- **Float:** `mf_sky_palette=float` hid the shelf and placed the palette slot
  (still the placeholder, so empty).

## Round 14: undo, no needless questions, a palette that fills itself (contract)

The owner, using round 13: Ctrl+Z must undo a block deletion; deletion must
not ask unless something is actually running; the floating blocks panel must
hold as many blocks as there is room, not just the starred ones; the All
blocks screen must not take the whole screen (people can't tell they may drag
from it); a double-click on a block adds it to the map as well.

### Undo (frontend)

- One sky-wide undo stack, `lib/skyUndo.ts`: `pushUndo({label, undo: () =>
  Promise<unknown>})` (≤ 20 entries, each good for 10 minutes), `popUndo()`,
  `undoCount()`. Every removal in the sky pushes one — a stop or group taken
  off a line (the toast's Undo payload), a grounded line deleted (re-create
  from its steps and name, pinned where it was), a merge onto a session (the
  grounded line back), a closed session (reopen from Recently closed), a
  selection's Remove (one entry for the lot). The toasts keep their Undo
  button; it pops the same entry.
- **Ctrl+Z / ⌘Z** in the sky, outside a text field and with no dialog open,
  pops the stack (toast "Undid: <label>"); nothing to undo → a quiet toast.
  Redo is not offered (Ctrl+Y is the view toggle).

### Deletion asks only when something runs (frontend)

- Delete / Backspace / the ⋯ Remove / the selection bar's Remove act at once
  with an Undo toast — a grounded line, a stop, a group, a selection of stops
  or grounded lines, a session line whose agent is **idle**.
- They ask (the inline confirm as today) only when something is actually
  running: a session whose agent is working or asking, a stop that is the
  flight's current one while the flight runs, a selection holding any of
  those. The question names what is running.

### The palette fills itself (frontend)

- `SkyPalette` shows EVERY block — the Session chip, every kind, every saved
  stop — as a wrapped grid of chips/cards that fills its frame and scrolls
  past it; its frame (corner drag) decides how many show. A filter field at
  its head. The ★ pick governs only the bottom shelf; in the palette a ★/☆
  on each item still sets that pick.
- **All blocks…** no longer opens a window-sized dialog: it opens the
  floating palette over the board (float mode, a large frame ≈ 60vw × 70vh
  if the owner never sized it, placed to the right of the view), with a ×
  that returns to the shelf. The board stays visible around it so a drag out
  of it is the obvious move. `StopCatalog` becomes the palette's body (the
  cards, the filter, the ⋯ menus) and is no longer a `Modal`.
- **Double-click places too**: a chip or card places on single click (after a
  short pause) OR on double-click — never twice for one double-click
  (`clickOrDouble` helper: a click waits ~250 ms and is cancelled by a second
  click; the double-click places once).

### As built: round 14 palette

Source: `components/flights/SkyPalette.tsx` (+ `.css`), `StopCatalog.tsx`
(+ `.css`), `StopShelf.tsx`, `lib/clickOrDouble.ts`, `lib/stopCatalog.ts`;
tests in `skyPalette.test.ts`, `stopCatalog.test.ts`, `stopShelf.test.ts`,
`clickOrDouble.test.ts`, `flightsButtons.test.ts`.

**What the host passes.** No drop changed: every card still drags with the
round-12 MIMEs (STOP_MIME / FLYWAY_MIME / NEW_SESSION_MIME).

| Surface | New props | What they do |
|---|---|---|
| `StopShelf` | `onCatalog?()` | "All blocks…" calls it (the host floats the palette large) instead of opening a dialog; the chip then drops `aria-haspopup` / `aria-expanded` and says "in a palette on the board". Without it the shelf opens `StopCatalog` (a dialog), as before. |
| `SkyPalette` | `onClose?()`, `focusFilter?` (the round-14 seams, now wired) | `onClose` draws the head's × (`fl-icon-btn sky-palette-close`, "Close — back to the shelf"); `focusFilter` focuses the filter on mount (`focus({preventScroll: true})`, so the sky never scrolls to it). Every round-13 prop is unchanged. |

**The palette is every block.** Its body is the catalog's, compact:
`StopCatalogBody` with `compact`, `idPrefix="sky-palette"` (list
`#sky-palette-list`, rows `sky-palette-row-N`), `listClass="sky-palette-body"`
(so `.sky-palette-body.sky-own-scroll` stays), `place`, `onAdd = onPlace`.
The **Session** card comes first (the body's `lead(query)` slot; it drops out
when the filter doesn't match "Session" / "A new agent session"; it is no row,
so the arrows skip it), then every kind (`catalogKinds`: the defaults first),
then every saved stop (`savedStopOrder`: built-ins first) — whatever the pick.
The grid is `repeat(auto-fill, minmax(132px, 1fr))`; a saved stop spans two
columns; the frame (360 × 420 by default, or the host's) decides how many
show and the list scrolls past it. A compact card: an icon tile, the name, one
line (a kind's summary; a saved stop's step boxes, its path in the title), a
★ / ☆ (shown on the hovered / picked card or when on; always on a touch
screen), and a saved stop's ⋯; no badges, no Add (the click places). The head:
**Blocks**, a count of every block (kinds + saved stops), the filter (`flex:
1`), the ⋯ (**Dock at the bottom**) and the ×. "All blocks…" is gone from the
palette (it IS all the blocks), and so are its round-13 regions, chips,
right-click menus and inline delete question (`useSavedActions` is now the
shelf's alone).

**The ★ governs only the bottom shelf.** A card's ★ / ☆ is
`toggleShelf` on `useShelfPick` (titles "Put it on the bottom shelf" / "On
the bottom shelf — click to take it off"); the palette shows every block either
way.

**`StopCatalogBody`** (StopCatalog.tsx, exported): the filter, handed to the
surface's `head(filter)` render prop, then the list — Blocks and Multi-step
grids of cards, then the hint ("… · a click or a double-click puts it on the
board · the arrows pick, Enter puts it on the board"). It owns the query, the
arrows' pick, Rename (`PlanNameField`), the delete question and **Takes off…**
(`cardMenu` now: Rename, Takes off…, On the shelf, Reset / Delete — so a
palette-only owner still reaches the TakeoffDialog). `onDrag(on, landed)` and
`escRef` are the Modal's hooks; the arrows scroll only the list to the picked
card (`reveal`, from the rects over the list's own scale — `scrollIntoView`
would also scroll the sky the palette floats in).

**`StopCatalog`** is now a thin Modal around the body (the shelf's fallback):
`min(920px, 70vw) × min(680px, 72vh)` instead of 92vw × 86vh, so the board
shows around it; the drag-through (`.is-dragging`), drop-closes and Esc-answers-
the-question rules are as before, through `onDrag` / `escRef`.

**Click or double-click, once** (`lib/clickOrDouble.ts`): `clickOrDouble(place,
{delay = 250, pair = 500}) → {onClick, onDoubleClick, cancel}`. A click arms a
`delay` timer; a second click cancels it; the dblclick then places once. A
keyboard click (`detail` 0: Enter / Space on a button) places at once. Two slow
clicks (past `delay`, inside the OS's double-click time) place on the first and
skip the dblclick (it comes within `pair` ms of the click that placed).
`useClickOrDouble(place?)` keeps one per mount (a waiting click meets its
second click across a re-render), calls the newest `place`, cancels on
unmount. Used by the shelf's kind, saved-stop and Session chips (a drag start
cancels a waiting click), by every catalog / palette card (one per card key,
kept in the body; a press on its ★ / ⋯ / field / Add / question is theirs), and
by the palette's Session card. Seen in the browser (a scratch build, mocked
API): a double-click on a palette card → one `POST /api/flights/start`, a
click → one.

**Seen, for the sky (not changed here).** With the palette hanging past the
viewport's right edge, focusing its filter by a script scrolled the sky's
viewport ~33px (the plane, intake and lines shifted with it): the viewport is
`overflow: hidden` but still scrollable by focus. `focusFilter` uses
`preventScroll`, so opening it is safe; a typed caret may still do it.
### As built: round 14 sky

> Code: `frontend/src/lib/skyUndo.ts` (new), `lib/skySelect.ts`
> (`removalNeedsAsk`, `sessionRuns`, `stopsRun`), `lib/skyLayout.ts`
> (`catalogFrame`), `components/flights/SkyView.tsx` (Ctrl+Z, `removeGrounded`,
> `reground`, `pinAt`, `openCatalog`), `SkyLine.tsx` (`closeLine`,
> `pushCloseUndo`, `reopenClosed`, the close question only while running),
> `useSessionLine.tsx` (undo entries, `askRunning`, `removeAsk`,
> `nodeIndexes`), `GroundedLine.tsx` (Remove at once), `SkySelection.tsx`
> (`takenStops`, `putBackAt`, one entry for the lot). Tests: `skyUndo.test.ts`
> (new), `skySelect.test.ts`, `skyView.test.ts`, `sky.test.ts`,
> `grounded.test.ts`, `skyMerge.test.ts`.

**The undo stack.** `pushUndo({label, undo, valid?})` returns the entry;
`popUndo()` / `peekUndo()` / `undoCount()` pass over entries older than 10
minutes (`UNDO_TTL_MS`) or whose `valid()` says false; at most 20
(`UNDO_MAX`), the oldest dropped. The toast's Undo is `takeUndo(entry)`: it runs
only if the entry is still on the stack. So the toast and Ctrl+Z share one
entry and never apply it twice (`undoAction(entry, aria)` builds the button).
The entries:

| removal | label | undo |
|---|---|---|
| a stop off a line (`removeStop`) | Remove “title” | `tackOn` at `undoPayload`'s index, read against the flight as it is when Undo is pressed |
| a group off a line (`removeGroup`) | Remove “name” | `tackOnSteps(…, name)` the same way |
| a grounded line (⋯ Remove, Delete) | Remove “name” | `reground`: `startGrounded({steps, name})`, pinned where it sat |
| a merge onto a session | Moved N stops onto … | `reground` (the stops stay on the session too, as in round 13) |
| a closed session line (Delete, ⋯ Close session, the close question) | Close “name” | `reopenClosed`: `undoLastClose()` (the Ctrl+Shift+T road) per title still closed, newest first |
| the selection bar's Remove | Remove N lines / Remove N stops | the lot: grounded lines `reground`ed, sessions reopened; stops tacked back in index order (`putBackAt`) |

`useSessionLine` keeps the stop toast's literal shape (`action: {label:
"Undo", …, onClick}`), since `stopDialog.test.ts` pins it. The `onClick` takes
the entry. A closed session's own toast comes from `closeSession`
(`lib/sessionActions`, not the sky's): its Undo reopens directly and cannot
take the sky's entry. That entry's `valid()` is "still closed", so a session
reopened from that toast (or by Ctrl+Shift+T) drops out of the stack.
`reopenClosed` skips a title that is open again. `undoLastClose` reopens the
newest session closed in this page, so a session closed from the grid after
the sky's close would come back first. That edge is left as is.

The selection's stop undo: `takenStops(nodes, steps)` gives each removed box's
first index, its steps and a group's name, first index first.
`putBackAt(taken, {cursor, length})` puts each one back at its old index while
that index is ahead of the flight's cursor and inside the line as it grows
back. Once one item goes to the end, the rest follow it, so none lands inside
another. If the flight went with its last stop, the stops come back as a new
flight on the session (`tackOnNew`), or as a grounded line.

The pane's Flight tab shares `useSessionLine`, so a removal there pushes an
entry too. Ctrl+Z outside the sky is still the keymap's reopen.

**Ctrl+Z / ⌘Z.** SkyView's window-capture key handler (`undoKey`: Ctrl or ⌘,
no Shift, no Alt) pops the stack ahead of the keymap's Ctrl+Z, which reopens
the last closed session. It does nothing in these places:

- a text field, an xterm or anything else `typing`;
- an open card's `.pane`, a `.skl-card`, a menu, a dialog or an inline question;
- while `overlayOpen()`.

During a line drag, a pan or a marquee it takes the key and does nothing.
Otherwise it toasts "Undid: <label>" and runs the undo. An empty stack toasts
"Nothing to undo", and its tooltip points to Ctrl+Shift+T. No redo: Ctrl+Y
stays the view switch. A phone has no key handler.

**Ask only when something runs.** `removalNeedsAsk(target)` is true for:

- `{kind: "session", row}`: the row's `activity` is `working` or `clarify`,
  or it has an `asking`;
- `{kind: "stops", indexes, flight}`: the flight is `running` or
  `waiting_you` and its `cursor` is among `indexes` (`nodeIndexes(n)`: a stop
  its own, a group all its sub-steps);
- `{kind: "selection", items}`: any item is true.

`{kind: "grounded"}` is always false. Where it is applied:

- **GroundedLine**: its InlineConfirm is gone. ⋯ Remove, Delete on the line
  and Delete from the sky call `remove()` at once. Through the host
  (`onRemove={() => removeGrounded(id)}`) that means `deleteFlight`, a
  "Removed “name”" toast with Undo, and an entry. Alone, without a host, it
  deletes and pushes its own entry, without the pin.
- **SkyLine**: Delete on a session's box or on the line, and ⋯ Close session,
  go through `askClose(t)`. An idle session closes at once (`closeLine`:
  `closeSession`, then an entry). A working or asking one gets the inline
  "Close “name”?". Its body names what runs ("Its agent is working right
  now" or "…is asking you something").
- **A stop on a session line**: the canvas's `onRemove` is `requestRemove`.
  With `askRunning` (only SkyLine passes it; the pane's Flight tab draws no
  question and removes at once, as before), the stop the running flight is
  at raises `removeAsk`. That is an inline "Remove “title”? The flight is at
  it right now…", drawn under the bar like the close question. Every other
  stop goes at once. Delete from the sky now reaches the current stop too:
  SkyLine's `deleteBox` takes a non-editable box when it holds the flight's
  cursor. Before, Delete there did nothing, and only the 🗑 removed it.
- **SkySelection**: `REMOVE_AT_ONCE` and the "past 3" question are gone.
  Remove (now without the ellipsis), and Delete through `ask === "remove"`,
  run at once unless the selection asks. The question names the running
  sessions ("Remove 5 lines? a, b are running." / "x is working.") or the
  current stop ("The flight is at “Plan it” right now."). The host passes
  `rowOf`, `posOf` and `restoreGrounded` (`reground`).

**All blocks… floats the palette large.** StopShelf gets
`onCatalog={openCatalog}` (not on a phone). `openCatalog`:

- switches `mf_sky_palette` to "float";
- computes `catalogFrame(view, screen, zoom, stored)`. Without a stored frame,
  that is 60% of the window's width × 70% of its height, divided by the zoom
  (plane px), held to the palette's 220–1400 × 140–1200 and to the view less
  16 screen px. A stored frame keeps its size, and only its spot is chosen;
- pins the palette's layout entry at that spot: its left edge `PLACE_GAP`
  right of the view's centre unless that would push it out (then right-aligned
  in the view), centred top to bottom.

The large size is held in state (`catalogOpen`) for this opening only. It is
never written to the layout, so a later "Float the blocks" opens at the
default size, unless the owner drags the corner (stored as before). The
palette gets these props:

- `focusFilter={!!catalogOpen}`;
- `onClose={() => setPaletteMode("shelf")}`, always;
- `width` / `height` from the stored frame, else from `catalogOpen`.

Esc docks it back to the shelf in either of these cases, after the cards and
the selection have had their Esc and before "nothing focused":

- the focus is on the sky;
- the focus is inside the palette but not in its filter field.

Docking clears `catalogOpen`. It is the palette slot of round 13, not a modal:
the board stays around it, and its blocks drag out as the shelf's do.

Checked in a browser (a scratch `vite build` into the scratchpad plus the demo
mocks; for All blocks…, a scratch copy whose StopShelf calls `onCatalog`, as
the palette worker's will):

- **Grounded line:** a bar click then Delete sent `DELETE /api/flights/f_g1`
  with no question and toasted "Removed “Grounded stops”" with Undo. Ctrl+Z
  sent `POST /api/flights/start {grounded, steps}`, toasted "Undid: Remove
  “Grounded stops”", and the new line sat at the old line's exact transform.
- **A stop ahead:** Delete sent `DELETE …/steps/a2` with no question. Ctrl+Z
  sent `POST …/steps {step, at: 1}`.
- **The current stop of a running flight:** Delete asked "Remove “Plan it”?"
  and sent nothing. Remove sent the DELETE ("…the flight is paused before
  “Review it”"), and Ctrl+Z tacked it back at the end (it was no longer
  ahead).
- **Select all, then Delete:** asked "Remove 5 lines? … jira-PAY-301, …
  sc-17460 are running." and sent nothing.
- **Session lines:** Delete on the working one asked "Close …? Its agent is
  working right now…". Delete on an idle one sent `POST …/close` at once.
- **Empty stack:** Ctrl+Z after draining toasted "Nothing to undo".
- **All blocks…** at 1500 × 900 and 100% opened the palette at 900 × 630
  screen px inside the viewport (16 px from its right edge, centred top to
  bottom). The shelf hid, no modal opened, and the lines stayed visible to its
  left. Esc docked it and brought the shelf back.

### Round 14 follow-ups (the owner, live, 2026-10-30)

- **A block on the ground may be incomplete.** A Split with no prompts (any
  step whose config a plan would refuse) can be placed and edited on the
  board: `normalize_step(…, config_strict=False)` for grounded flights; its
  kind and shape are checked as always. `validate_for_takeoff` runs on
  attach and answers 400 `bad_step` with the field (`steps[i].config.prompts`).
- **A session picked from outside the sky opens its box.** The sidebar row,
  Alt+N, the palette: the focus effect in SkyView sees the pick was not a
  press on the sky (`pressedLine`) and opens the line on its pane, once per
  pick (`openedFor`), then pans it into view as before.
## Round 15: more kinds of block, and the manners of a dev tool (contract)

The owner, after a brainstorm (2026-10-30): every robot stop today either
runs the agent or waits on the outside world; nothing **judges**, nothing
**undoes**, and the Sky says nothing about what a stop touches or what it
did. Round 15 adds the stops a dev reaches for every day, a `when` on every
stop instead of a branching gate, the sky objects that complete the metaphor
(a landing strip on the right, people and devices to drop a line on, notes),
and the looks that carry information (a risk stripe, receipts, time under a
line, ageing, formation, weather). Five principles hold from round 13: nothing
lands by itself, direct manipulation, every object has a name and can go,
selection may be empty, palettes dock or float.

### The seam: kinds as plug-ins (built, in this commit)

`backend/web/core/flight_kinds/` is a package; every module in it registers
one `Kind` (see the package docstring for the full dataclass): `spec` (the
`STEP_KINDS` row + `risk`, `family`, `default_on_fail`), `config(raw, strict,
where)`, `judge(flight, step, ss, obs, now)` (pure), `run(flight, step,
action, now)` (async; raise `StepFailed`), optional `observe(flight, step,
ss, sess, now)` (async → `obs[kind]`), `needs_session`, `types_to_agent`,
`default_title(config)`, `ops` (behind `POST
/api/flights/{id}/steps/{sid}/op/{name}`, via `flight_driver.step_op`) and
`router` (included next to the flights routes). `flights.install_kinds()`
merges them at import; the core falls through to the registry in
`_step_config`, `_default_title`, `judge_for`, the driver's `observe` and
`_perform`. Driver helpers for plug-ins: `run_shell(flight, step, cmd,
timeout_s, folder, env_extra, keep)` + `job_of(flight, step)` (the command
kind's own job machinery), `worktree_of(flight)`, `spawn_worker(flight, step,
prompt, model, program, fence_only, report_back, title_prefix)` (one child of
the session, as a split makes them), `_deliver(title, text)` (a prompt into
the session's queue). `StepFailed` lives in the package;
`flight_driver._StepFailed` is it.

`GET /api/flights/kinds` rows now carry `risk` (list of `remote` — touches the
shared remote, `irreversible` — Ctrl+Z cannot undo it, `spends` — money
outside the agent's own usage), `family` (`work` | `ship` | `outside` |
`people`; built-ins: agent/split/join/command = work, ship/checks/reviews =
ship, wait/notify/ticket = outside, you/verify = people) and
`default_on_fail`. `ship` and `ticket` are `remote`. A step's `on_fail`
defaults to its kind's `default_on_fail`.

**Receipts.** A judge sets `detail.receipt` (≤ 80 chars: "312 passed",
"PR #291", "rebased 4 commits") on `done`; the box shows it once the stop has
flown, and the stop dialog's "What it did" leads with it. Built-in kinds get
theirs in this round too (the `receipts` task): ship "PR #291" / "merged" /
"pushed" / "committed", checks "all green", reviews "2 rounds, quiet", command
"exit 0", agent "settled after 12 min", split "3 workers", join "3 of 3
done", wait "10 min", verify "k of n", ticket "→ Done", notify "sent".

### `when`: a condition on every stop (task `when-judge`, core)

A step may carry `when: "<expression>"` (≤ 300 chars; the normaliser
canonicalises it or raises `PlanError(…, where + ".when")`; lenient mode
drops an unparsable one). A pending step whose `when` is false is **skipped**
by the planner (`step_states[sid].state = skipped`, note "skipped: <expr> —
<the fact>", history `skipped`), never run. The grammar, words a dev would
type, case-insensitive, `and` binding tighter than `or`, `not` prefix:

| condition | true when |
|---|---|
| `diff touches <glob>[, <glob>…]` | a changed file (vs the flight's base) matches any glob (`frontend/**`, `*.sql`, `migrations/`) |
| `diff lines > N` / `< N` | added + removed lines vs the base |
| `files > N` / `< N` | changed files vs the base |
| `weekday` / `weekend` | the server's local day |
| `hour between H and H` | local hour in [H1, H2) (wrapping past midnight allowed) |
| `branch matches <glob>` | the session's branch |
| `label <name>` | the PR / issue carries that label (a flight without one: false) |
| `judge is <value>` / `judge <sid> is <value>` | the nearest earlier Judge stop's value (or that stop's) equals it |
| `always` / `never` | — |

**The base** of a diff is the session's base branch when the row knows one,
else the repo's default branch (`origin/HEAD`); the facts are gathered once
per pass by the driver into `obs["facts"]` (`{files: [...], added, removed,
branch, labels, hour, weekday, judges: {sid: value}}`), only when the
current step has a `when` (cheap otherwise). `GET
/api/flights/when?expr=…` → `{ok, canonical, error, field}` validates for the
editor. `fill_prompt` gains `{preview}` (the latest Preview stop's url in
the flight, "" when none) for the `preview-shots` task to fill.

**Judge** (kind `judge`, family work, robot): config `question` (textarea),
`options` (text: comma-separated answers, default "yes, no"), `by`: `model`
(default: the one-shot describe model, `flight_describe`'s machinery, asked
with the diff stat + the question) | `agent` (the session's agent answers:
the prompt asks for one line `ANSWER: <option>`; the judge waits for the
agent's turn to settle, then reads the last transcript line matching it).
`detail.value` is the answer (one of the options, lower-cased), receipt the
answer. `when` reads it. v1 `by: agent` may be a follow-up if the
transcript read is not at hand: say so in "As built".

Frontend part of this task is in `when-ui` below.

### The new stops (one module each, `flight_kinds/<kind>.py`, tests `tests/unit/test_flight_kind_<kind>.py`)

| kind | family / risk / actor | config | done when | receipt | task |
|---|---|---|---|---|---|
| `test` | work · — · robot | `command` (empty = auto: pytest when pyproject/pytest.ini/setup.cfg, `npm test` when package.json has a test script, `go test ./...`, `cargo test`), `expect` pass\|fail (default pass), `rerun_failures` bool (default true: rerun once when ≤ 3 failures), `timeout_minutes` 30 | the run ends; `expect pass`: exit 0 and no failures; `expect fail`: ≥ 1 failure (a pass "passed unexpectedly" fails the stop). Parses pytest's summary, vitest/jest "Tests: N passed, M failed", go `ok`/`FAIL`, cargo "test result: ok. N passed; M failed". `detail` {passed, failed, failures (≤ 20 names), runs, output tail}; `on_fail: agent` hands the failing names + tail to the agent | "312 passed" / "2 failed" / "failed as expected" | `test` |
| `sync` | ship · — · robot | `with` (branch; empty = the default branch), `how` rebase\|merge (default rebase), `fetch` bool (default true) | HEAD contains `with`'s tip and no rebase/merge is in progress. A conflict is NOT aborted: the stop fails with the conflicting files and the fix note tells the agent to resolve and `git rebase --continue` / commit the merge; the retry re-checks. Refuses (fails) with uncommitted changes in the worktree | "up to date with main" / "rebased 4 commits" / "merged main" | `sync-checkpoint` |
| `checkpoint` | ship · — · robot | `note` (text) | HEAD's sha and, for uncommitted work, a `git stash create` object are recorded (`detail.sha`, `detail.stash`) and kept alive under `refs/mindflock/flights/<fid>/<sid>`; ops `rewind` (`POST …/op/rewind`): the session idle (409 `working` otherwise) → `git reset --hard <sha>`, re-apply the stash, then the flight jumps to the step after the checkpoint (`op_jump`); the ref is dropped when the flight is deleted (`remove`) | "at a1b2c3d" | `sync-checkpoint` |
| `land` | ship · irreversible · robot | `delete_branch` bool (true), `remote_branch` bool (true: delete on origin too), `close` delete\|close (default delete: `kill_session(mode="delete")` removes the worktree) | the branch is merged into the default branch (else fail "not merged"); the flight is finished `done` FIRST, then the session is closed on a thread a second later (the flight must not lose its session mid-step). Must be the LAST step of a plan (strict `PlanError` otherwise). `needs_session` true | "landed" | `land-budget` |
| `budget` | outside · — · robot · `default_on_fail: you` | `max_usd` (float, 0 = none), `max_minutes` (the flight's elapsed, 0 = none), `max_tokens` (0 = none), `scope` here\|flight (default flight) | immediately when within budget; over → fail "over budget: $5.20 of $5" (so the default perches you with Carry on). `scope: flight`: the check is a **guard** — the core re-evaluates every done budget stop of the flight before judging the current step (`Kind.guard(flight, step, obs, now) -> action|None`, added to the registry by this task; the driver observes guard steps into `obs["guard"][sid]`); a guard that trips fails the CURRENT step with the budget's reason and `on_fail` of the budget stop. Cost from the session's usage (`session_stats`/the usage ledger) | "$1.20 of $5" | `land-budget` |
| `review` | work · — · robot | `mode` select (`bugs` · `security` · `standards`, see "Team PR rules as a route"), `program` (CLI id, empty = the session's), `model`, `focus` (textarea appended to the review brief; the governing rules in the security / standards modes), `block` bool (true: findings fail the stop) | a worker spawned by `spawn_worker` with a brief: review `git diff <base>...HEAD` for correctness, report findings as a numbered list in its report (`report_result`, as children do); done when the worker has finished (the join machinery: `detail.children = [title]`); `detail.findings` (text), `block` + non-empty → fail with them (`on_fail: agent` hands them to the agent). The worker is killed (mode delete) when the stop is done unless `keep` bool (false) | "no findings" / "3 findings" | `review-pilot` |
| (agent) | | `agent` gains `program` + `model` (used only with `fresh`: the new session's CLI and model — "switch pilot"); `_run_agent`'s fresh path passes them | | | `review-pilot` |
| `call` | outside · remote · robot | `method` GET\|POST\|PUT (POST), `url`, `headers` (textarea, `K: V` lines), `body` (textarea; placeholders incl. `{receipt}` = the previous stop's receipt), `expect` (status prefix, default "2"), `timeout_minutes` 5 | the response status matches `expect`; `detail` {status, body tail ≤ 2000}; secrets: headers are stored as given (the plan is local) but never logged | "200" | `hooks` |
| `listen` | outside · — · robot · `needs_session` false | `match` (substring the body must contain, optional), `timeout_minutes` (0 = forever) | an inbound `POST /api/hooks/flight/{token}` arrives (token = 32 hex minted at run, `detail.token`; the box shows the URL); `detail.payload` ≤ 8 KB; timeout → fail. The route is public (auth.py: `_PUBLIC_FLIGHT_HOOK = re.compile(r"/api/hooks/flight/[a-f0-9]{32}(/done)?")`, POST only) and answers 404 for an unknown or spent token | "heard" | `hooks` |
| `someone` | people · — · **you** · `needs_session` false | `who` (text: a peer link's name, or empty = the webhook), `text`, `items` (checklist), `webhook` (URL; a Slack incoming webhook or any JSON POST) | the same perch state machine as `you` (waiting_you → Done/Skip), but the ask goes OUT: a peer by that name gets a MindFlock message (the mailbox) with the text and a one-tap link; the webhook gets `{text, items, done_url}`; the link is `GET /api/hooks/flight/{token}/done` (a tiny page with one Done button posting to itself) and `POST …/done` marks the perch done with `by: "someone"`. You can still press Done yourself | "done by Mauricio" | `hooks` |
| `preview` | outside · remote · robot | `command` (prints the URL as its last line, or `MINDFLOCK_PREVIEW_URL=…`), `timeout_minutes` 20, `stop_command` (optional; run when the flight finishes or is cancelled) | the command exits 0 and a URL was found → `detail.url`; `{preview}` fills later prompts; the verify checklist's deep link carries it when present | the URL's host | `preview-shots` |
| `screenshot` | outside · — · robot | `urls` (textarea, `{preview}` ok), `widths` ("1280, 390"), `timeout_minutes` 10, `comment_pr` bool (false: post the list of shots as a PR comment — text only) | every URL × width rendered with playwright-core + a Chrome/Chromium the server finds (`google-chrome`, `chromium`, `chromium-browser`; none → fail "no browser"); PNGs under `<flights dir>/shots/<fid>/<sid>/<n>.png`, served by the kind's router `GET /api/flights/{id}/shots/{sid}/{name}`; `detail.shots` [{name, url, width}] | "4 shots" | `preview-shots` |

Every one: `fields` rows from `flights._f`, `summary` a sentence, a
`default_title`; `StopIcon` glyphs for all of them are the `looks` task's.
What a stop stores must survive a server restart (the judge re-reads the
store, a lost thread → `run` again: see `_judge_command`).

### Triggers (task `triggers`, full-stack)

`TRIGGER_KINDS` grows `main_push` (the default branch moved), `main_red` (a
check on the default branch's head is failing) and `hook` (an inbound
`POST /api/hooks/flyway/{token}` — Grafana, Slack, CI; the plan's
`trigger.token` is minted on save and shown in the inspector). `trigger.repo`
(an absolute repo path) is required for `main_*`; `trigger.quiet_minutes`
(default 60) stops a re-fire. `backend/web/core/flight_triggers.py` ticks
every 60 s inside `template_schedule.run_loop` (next to `tick_flights`):
`git fetch` + compare `origin/<default>` with the last seen sha
(`<flights dir>/trigger_state.json`), the checks of that sha through the
existing GitHub REST helpers; a fire = the schedule path's own
`fly_plan`-style start (a session from the plan's `session` config, subject
`{kind: "manual", title: "main moved to a1b2c3d"}`). Frontend: `TriggerKind`
+ `TRIGGER_LABEL`, the TakeoffDialog / inspector's "Takes off" section gains
the three with their fields (repo via `PathField`, the hook URL with a copy
button); the sky-side lives in `TakeoffDialog.tsx` only.

### Built-in flyways and presets (task `builtins`, after `when-judge` merges)

New built-ins (`_builtin_defaults`): **Bot PR** (trigger pr: Agent reads it →
Test → Sync → Checks → Merge; no perch), **Red main** (trigger main_red:
Agent investigates → Test → Commit → Push → Open PR → Perch), **Nightly
chores** (trigger schedule 03:00: Agent updates deps → Test → Commit → Push →
Open PR), **Release** (manual: Agent writes the notes → Commit → Push → Open PR
→ Checks → Perch: approve → Merge → Agent tags and publishes the release →
Land), **Bug report** (trigger ticket: Test `expect fail` "write the failing
test first" → Agent → Test → Commit → Push → Verify → Open PR → Reviews →
Checks → Perch → Merge → Ticket). Presets = saved stops of one agent step,
trigger manual, ids `builtin:preset-docs` "Update the docs", `-tests` "Write
tests", `-security` "Security pass", `-selfreview` "Self-review",
`-explain` "Explain the diff", each with a real prompt. Every built-in stays
resettable. Also this task: the **receipts** of the built-in kinds (above).

### The frontend

**`when-ui`** (`StopParts.tsx`, `StopDialog.tsx`, `lib/flights.ts` types,
`lib/stopEditing.ts`): `FlightStep.when?: string`; the stop form gets a
"Only when" field under the kind's fields with a ⓘ popover listing the
grammar and live validation through `GET /api/flights/when` (debounced; the
error under the field, the canonical form replacing the text on blur); a
skipped stop reads "skipped: …" in its dialog; `kindName` says **Someone**
for `someone`; the stop form shows the kind's `default_on_fail` as the
unselected default. Tests in `stopWhen.test.ts`.

**`looks`** (`FlywayCanvas.tsx/.css`, `SkyLine.tsx/.css`, `Bird.tsx`,
`StopMenu.tsx`, `StopDialog.tsx` "What it did" only, `lib/flightDiagram.ts`,
new `lib/stopRisk.ts`, `lib/lineTime.ts`): (1) the **risk stripe**: a 3px
left edge on a box per tag (`riskOf(step, kinds)`: the kind's `risk`; ship
`lane: merge` adds `irreversible`, `lane: commit` has none), tooltip names
the tags, colours by token (remote = the ship tone, irreversible = the
failed tone, spends = the perch gold); (2) **receipts**: a flown box shows
`detail.receipt` under its title in the muted tone (mid and near bands), the
dialog leads "What it did" with it and shows `detail.shots` thumbnails
(`<img>` from their urls, click = open) when present; (3) a **`when`
chip** ("if") on a box with one, tooltip the expression; (4) **time under
the line**: a 4px ruler under the row of stops, one segment per flown stop
proportional to its duration (from `history`), hover names "Checks · 12
min"; the total at the right end; hidden far; (5) **ageing**: a session
line whose row's `activity_since` is > 24 h → `data-age="old"` (the title
bar yellows like paper), > 72 h → `"stale"` (and a "3 days still" word in
the bar); (6) **formation**: a parent bird with worker branches draws the
workers' birds in a V behind it (`birdPerches` gives the offsets; purely
visual); (7) **icons** for test, sync, checkpoint, land, budget, review,
judge, call, listen, someone, preview, screenshot in `StopIcon`; (8) the
flown **checkpoint**'s menu gets "Rewind to here" (`POST …/op/rewind`,
confirm inline since it resets the worktree; refused with the server's
sentence when the session works). Tests `stopRisk.test.ts`,
`lineTime.test.ts`, `skyLooks.test.ts`.

**`objects`** (`SkyView.tsx`, new `LandingStrip.tsx/.css`, `PeopleBlocks.tsx`,
`DeviceBlocks.tsx`, `StickyNote.tsx/.css`, `lib/skyObjects.ts`; backend
`backend/web/core/landing.py` + `backend/web/routes/sky.py`, included from
`server.py` next to the flights router): (1) the **landing strip**: a sky
object (layout key `landing`, placed right of every line, drags by its head,
folds) showing the default branch of each repo in the sky: its name, the
checks of its head (green / red / none, from the GitHub helpers), the last
merge's age, and **Landed today**: the flights finished `done` whose last
ship was `merge` in the last 24 h (title, PR link) — `GET /api/sky/landing`
`{repos: [{root, branch, head, checks, last_merge_at, landed: [{flight,
title, pr_url, at}]}]}`; (2) **people blocks**: one per peer link (`GET
/api/peer/links`), key `person:<id>`, dropping a session line on it shares
the session (the existing share op, with its SAS gate — the block says
"Shared" after); (3) **device blocks**: one per device of yours (`GET
/api/devices`, not this one), key `device:<name>`; dropping a line on it
moves the session (the existing `POST /api/instances/{title}/move`, after
the inline confirm since it pauses the session); (4) **sticky notes**: ⋯ →
"Add a note" / the palette's Note chip places one at `spotNear`; a note is a
sky object (key `note:<id>`, resizable, inline-edited, Delete removes it with
Undo) stored server-side in `<flights dir>/sky_notes.json` (`GET/PUT
/api/sky/notes`, synced like layout is not — per server); a note dropped on
a line attaches to it (`line` field) and moves with it. Registry
`lib/skyObjects.ts`: `{key, frame(stored), place: "left"|"right"|"free",
render(host)}` so SkyView iterates one list for intake, palette, landing,
people, devices and notes (migrate intake + palette onto it). Tests
`skyObjects.test.ts`, `landing.test.ts`, `tests/unit/test_sky_routes.py`.

**`weather`** (`SkyFlock.tsx/.css`, new `useWeather.ts`): the flock canvas
reads `/api/usage` (the top bar's window estimate): clouds drift in as the
plan window passes 60 %, thicken past 85 % (soft radial blobs behind the
birds, the `prefers-reduced-motion` rule respected); the sky dims (a
night tint on the plane's background) during the notification quiet hours
when they are set; a ☁ word in the Sky's title bar names the reason
("usage 87 %"). Nothing moves a box. Test `weather.test.ts` (pure
`weatherFor(usage, hour, quiet)`).

**`palette-groups`** (after round 14's palette lands: `StopShelf.tsx`,
`StopCatalog.tsx`, `SkyPalette.tsx`, `lib/stopCatalog.ts`): the chips group
by `family` with a thin label (Work · Ship · Outside · People) in the
palette and the catalog; the shelf stays the flat ★ pick.

### Workers and fences

Backend tasks: `test`, `sync-checkpoint`, `land-budget`, `review-pilot`,
`hooks`, `preview-shots`, `when-judge`, `triggers`, `builtins` (last).
Frontend: `looks`, `when-ui`, `objects`, `weather`, `palette-groups` (last).
Each owns the files its row names plus its tests and an "As built: round 15
<task>" section here; nothing else. Core edits outside the seam belong to
`when-judge` (normalise/plan/next_action/driver facts), `land-budget`
(`Kind.guard` + the guard pass), `review-pilot` (agent program/model),
`preview-shots` (`fill_prompt` `{preview}`), `triggers`
(`TRIGGER_KINDS`/normalise trigger/template_schedule tick), `builtins`
(`_builtin_defaults` + the built-in judges' receipts). `tsc` and `vitest`
run from `frontend/` (node_modules is linked); backend tests with
`UV_PYTHON=3.12 uv run pytest tests/unit/test_flight*.py`.

### As built: round 15 test

`backend/web/core/flight_kinds/test.py` registers `test` (work · no risk ·
robot, needs the session). Config `{command, expect, rerun_failures,
timeout_minutes}`; a strict plan refuses an `expect` other than pass/fail
(`…config.expect`), a lenient one reads it as pass. Default title "Run the
tests" ("Run the tests (expect a failure)" with `expect: fail`).

- **Command.** Empty = `detect_command(worktree)`: pytest when `pytest.ini`,
  pyproject `[tool.pytest…]`, setup.cfg `[tool:pytest]` or tox.ini `[pytest]`
  (`uv run pytest -q -rfE` with a uv.lock, else `.venv/bin/python -m pytest`,
  else `python3 -m pytest`), `npm test` when package.json has a test script
  (npm init's "no test specified" placeholder does not count), `go test
  ./...` with go.mod, `cargo test` with Cargo.toml; none → fail "no test
  command: …". An auto-detected command runs with `CI=1` (vitest/jest run
  once instead of watching); every run gets `NO_COLOR=1 FORCE_COLOR=0`, and
  the parsers strip ANSI anyway. The job is `run_shell(…, keep=20000)` in
  `worktree_of(flight)` (no worktree → fail).
- **Parsers** (pure, `parse_pytest` / `parse_js` / `parse_go` /
  `parse_cargo`; `parse_output` sums every runner that recognises the
  output, so a `make test` running pytest + vitest reads as one result):
  pytest's last "N passed, M failed, K errors … in Xs" line (errors count as
  failures; FAILED/ERROR short-summary names, else `-v`'s "x::y FAILED");
  vitest "Tests  1 failed | 4 passed" / jest "Tests: 1 failed, 4 passed"
  (names: vitest "FAIL file > suite > test", else jest "● suite › test",
  else ×/✕ lines, else FAIL files); go `--- FAIL:` / `--- PASS:` (top-level
  counted, subtests named) and, without `-v`, the `ok`/`FAIL<TAB>pkg` lines
  counted as packages (receipt "N packages ok"); cargo every "test result:"
  line summed, names from "test x ... FAILED". No summary → the exit code
  alone decides.
- **Judge.** `obs["test"]` is `job_of` (via `observe`); None (no job this
  server run) → `run` "running the tests again (interrupted)". `expect:
  pass`: exit 0 and no failures → done; 1..3 failures with `rerun_failures`
  → a `run` with `phase: "rerun"` (the stop keeps its `since` and history;
  the runner starts the SAME command again, `detail.runs = 2`,
  `detail.first` = the first run's counts) and the second run is the
  verdict; otherwise fail. `expect: fail`: ≥ 1 failure or a non-zero exit →
  done "failed as expected"; exit 0 with none → fail "passed unexpectedly";
  exit -1 (timed out / could not start) is not the asked-for failure and
  fails the stop.
- **Stores** `detail {passed, failed (None when unparsed), failures (≤ 20
  names), runs, exit, output (tail ≤ 2000), command, receipt}`; receipts
  "312 passed", "312 passed after a rerun", "2 packages ok", "passed"
  (exit 0, no summary), "failed as expected".
- **Fail reason** (what `on_fail: agent` hands over): "2 failed: a, b" /
  "2 failed (again on the rerun): …" / "exited 2" / "the tests did not
  finish", then the output's tail, ≤ 2000 chars in all.
- **Limits.** The rerun runs the whole command again (not only the failing
  tests); a restart mid-rerun starts over at run 1; go without `-v` cannot
  count passing tests in a package that also failed (passed reads 0).
### As built: round 15 review-pilot

**`review`** (`flight_kinds/review.py`, tests `test_flight_kind_review.py`):
config `program` (≤ 100), `model` (≤ 200), `focus`, `block` (true), `keep`
(false); title "Second opinion" ("Second opinion (codex)" with a program).
`run` computes the base (the session's `BaseBranch`, else `origin/HEAD`) and
calls `spawn_worker(…, report_back=True, title_prefix="review")` with a
brief: review `git diff <base>...HEAD` for correctness bugs only, change no
files, end with a report whose summary is `No findings` or a numbered list
(≤ 10) `1. file:line — finding`; `detail` `{children: [title], base}`. The
worker forks from the session's HEAD, so uncommitted work in the session is
not in what it reviews.

`observe` reads the worker through `_children_obs` (filtered to its title)
plus its **full** report from the session's mailbox (`mailbox.last_result`,
since the stop began): the row's `last_report.summary` is one line capped at
140 chars, too short for a list. Each line is `mailbox.sanitize`d (no forged
`[MindFlock …]` frames reach the agent) and the text capped at 4000 chars. A
worker that is gone is judged by that mailbox report. Once finished
(`child_state` done/failed) and not `keep`, observe closes it — the core of
`kill_session`: `session_end.end_session(wipe=True)` when its worktree is
clean and holds no commits the session's HEAD lacks, else `wipe=False`
(Recently closed keeps it) — and remembers the outcome per (flight, step,
since, title) for this server run, so a pass whose verdict was lost judges
from it. `detail.closed` says `deleted` / `closed`.

`judge`: no children → `run` (a restart between spawn and record spawns
again); no observation → wait "second opinion in progress"; working → wait
"second opinion in progress (<title>)" (`detail.reviewer` is the join
entry), asking → wait; failed, or gone without a report → fail "the
reviewer did not finish"; finished without any report (a CLI without the
MCP) → done, receipt "no report"; "No findings" / empty → done, receipt "no
findings"; otherwise `detail.findings` + receipt "N findings" (a report that
is not a numbered list counts as 1) and, with `block`, fail "the second
opinion has N findings:\n<list>" — `on_fail: agent` types that list to the
agent, and the retry after its fix spawns a fresh reviewer (a new
`since`, a new title). Open: after a restart a deleted reviewer that had
not reported reads as "did not finish" (the memo is per process).

**A reviewer that never started** (`flight_driver._launch_obs` /
`_launch_verdict`, tests `test_flight_review_reviewer_start.py`) is never
"did not finish" handed to the agent — it has nothing to fix. Known as: the
launch gate's failed queued launch, the server's background-create failure
record (`GET /api/create_failures`, newer than the stop's start), or — no
record — a reviewer gone without a report whose join entry never left
`loading` (`started` sticks on the entry once it has). The first time the
stop spawns ONE fresh reviewer under a new title (`run`, `detail.respawned`
= "couldn't start the reviewer: <why>", in the note as "asking review-2 for
a second opinion (<why>)"); if that one cannot start either, the stop fails
with "couldn't start the reviewer: <why>" under its `on_fail`, `agent`
turned into `halt` — no fix round spent, nothing typed to the agent.

**(agent) switch pilot**: `agent` gains `program` (≤ 100, "CLI for a fresh
session (empty = the same)") and `model` (≤ 200); `_run_agent`'s `fresh`
path puts them over the plan's session (`program`, `profile_model`). They
apply **only with `fresh`**: on a stop without it they are stored and
ignored (the stop form should say so). Tests `test_flights_pilot.py`.

`tests/unit/test_flights.py::test_kinds_registry_drives_the_editor` pins
the exact kinds list, so every plug-in kind (this one included) fails it:
it wants `kinds[:12] == [...]` — outside this task's fence.
### As built: round 15 sync-checkpoint

Two plug-in modules, `flight_kinds/sync.py` and `flight_kinds/checkpoint.py`
(family `ship`, no risk tags, robot, `needs_session`), git through
`git_ops._run_git` / `_is_dirty` / `_git_head_sha` / `_git_count` /
`_has_origin`.

**`sync`.** Config `{with, how, fetch}`; strict mode refuses a `how` other
than rebase/merge and a `with` that is not a plain branch name (leading `-`,
spaces, `..`); lenient mode falls back to `""` / `rebase`. Default title
"Sync with <with|main>" (rebase) / "Merge <with|main>". `run` (blocking half
on a thread): no worktree → `StepFailed`; the target is `with` or
`default_branch(folder)` (`origin/HEAD`'s short name, else `main` / `master`
on origin or locally); the ref is `origin/<target>` when the repo has an
origin, else the local branch. A rebase / merge **already in progress**
(`rebase-merge` / `rebase-apply` / `MERGE_HEAD` under `git rev-parse
--git-dir`, so a linked worktree's own dir) starts nothing and records
`detail.resumed` — that is the retry after a conflict, and the judge then
ignores the earlier attempt's job. Otherwise `git status --porcelain`
(untracked included) non-empty → `StepFailed("the worktree has uncommitted
changes: commit them first, then sync")`; else `run_shell` a `set -e` script
(`git fetch origin <target>` when `fetch` and an origin, then `git rebase
<ref>` / `git merge --no-edit <ref>`, `GIT_EDITOR=true`, 5 min timeout).
`detail` {folder, target, ref, how, before (HEAD), started}. `observe`
→ `obs["sync"]`: the job; once it ended (or there is none) `busy`,
`conflicts` (`git diff --name-only --diff-filter=U`), `contains` (`git
merge-base --is-ancestor <tip> HEAD`), `head`, `ahead` (`<ref>..HEAD`). The
judge: job running → wait; busy → **fail** "conflicts with main in a.py, b.py
— resolve them, `git add` the files and run `git rebase --continue`" (merge:
"`git commit` to finish the merge"), `detail.conflicts`, nothing aborted;
contains → done; no job this server run (interrupted) and not there yet →
`run` again; the job failed otherwise → fail with its exit and output tail.
Receipts: "up to date with main" (HEAD unchanged, not resumed), "rebased N
commits" (N = the branch's own commits on top of the target afterwards),
"fast-forwarded to main" (a rebase with no commits of its own), "merged main".

**`checkpoint`.** Config `{note}` (≤ 500), title "Checkpoint" /
"Checkpoint: <note's first line>". `run` → `done` straight away: `detail`
{sha, stash, ref, repo, receipt "at a1b2c3d"}; `stash` is `git stash create`
when the worktree is dirty (never on the shared stash stack, the worktree is
untouched); `git update-ref refs/mindflock/flights/<fid>/<sid> <stash or sha>`
keeps it alive (a stash commit's first parent is the sha, so both survive
gc; ids outside `[A-Za-z0-9_-]` become `_`). **Where the ref lives**: refs
outside `refs/worktree/` are shared by every worktree of a repo, so it is in
the session's repo's common git dir (`git rev-parse --git-common-dir`,
recorded as `detail.repo`), not in the worktree. Judge (after a restart):
`detail.sha` → done, else `run` again. Untracked files are not in a `stash
create` object and `reset --hard` leaves them alone: a rewind neither
restores nor removes them.

`ops["rewind"]` (`POST /api/flights/{id}/steps/{sid}/op/rewind`): no
`detail.sha` → 409 `no_checkpoint`; a finished / grounded flight → the jump's
own refusal (`finished` / `grounded`) **before** anything is reset; the
session's `_session_obs` activity `working` → 409 `working`; no worktree →
409 `no_worktree`; then `git reset --hard <sha>`, `git stash apply --index
<stash>` (plain `apply` as a fallback; either failing → 409
`rewind_failed` naming what happened) and `flights.mutate(… op_jump(next
step))` — not `flight_driver.operate`, so `step_op` announces the change
once. The last stop leaves the cursor. Returns `{flight, sha}`.

**For the integrator** (outside this task's fence):
- `flight_driver.remove` should call `checkpoint.forget(f)` (with the flight
  read before `_flights.delete`): it deletes every
  `refs/mindflock/flights/<fid>/*` in each repo a checkpoint of the flight
  recorded (works with the worktree already removed), returns the count and
  never raises.
- `tests/unit/test_flights.py::test_kinds_registry_drives_the_editor` pins
  the whole kinds list to the 12 built-ins; with any round-15 kind loaded it
  fails. It should check the built-ins as a prefix (or list the new kinds).
### As built: round 15 land-budget

- **The guard seam.** `Kind.guard(flight, step, obs, now) -> action|None`.
  `flights._guard_trip` runs in `next_action` right before a pending step
  would start (`run`) and right before a running step is judged (also before
  the fix phase). It looks at the done steps before the cursor that have a
  guard, and the first non-None action wins. A fail is resolved by
  `_resolve_fail` with the GUARD step's `on_fail` (and the current step's
  `rounds`), and the action carries `guard: <sid>`. If it also carries
  `guard_detail`, `apply` merges that into the guard step's own detail.
  `flight_driver._guard_obs` fills `obs["guard"][sid]` from each such step's
  `observe` while the current step is pending or running (not while it waits
  on you). It handles errors the same way the plug-in observe does.
- **`budget`** (`flight_kinds/budget.py`) reads the session's figures from
  `session_stats._session_tokens`, the same cost estimate as the session
  row. `usd` = `cost`. `tokens` = in + out + cache_write: cache reads are
  left out because they would trip any limit within minutes. `elapsed_min`
  = now − `flight.started`. A budget with no limit passes with "no limit
  set" (a newly placed block is not refused). When the guard trips, it
  records `detail.tripped_at = {measure: figure}` on the budget step. It
  only fires again once that figure is ≥ 1.1× the recorded value, so Done
  or Retry on the perched step lets the flight carry on.
- **`land`** (`flight_kinds/land.py`) counts the branch as merged when
  `git merge-base --is-ancestor refs/heads/<branch> origin/<default>` passes
  after a fetch, or when `github_pr.find_pr_sync` reports a MERGED PR into
  that default branch (this covers a squash merge). Otherwise the stop fails
  with "not merged into <default>". `run` also refuses when the land step is
  not the flight's last step. Strict `normalize_steps` raises "a Land stop is
  the last stop"; lenient mode keeps the steps as stored. A `land` step found
  running after a restart runs again; the check is idempotent.
- **The tidy-up thread** (`_close_later`, a daemon thread) sleeps 2 s and
  then waits up to 30 s for the flight to read `done`. If the flight is
  cancelled, halted or gone, it leaves everything alone. It then runs `git
  push origin --delete <branch>` when origin has the branch, and ends the
  session through `session_end.end_session`, the path the routes behind
  `kill_session` use. `close: delete` falls back to close, and the note says
  why, when the MCP tool would refuse the delete: the session runs in place,
  it has uncommitted or untracked files, or another session shares its
  folder (also on `FolderInUse` at the time of the delete). The "spawned by
  an agent" rule is not mirrored, because the owner's plan asked for the
  delete. After a delete, `delete_branch` removes a branch that existed
  before the session (Kill keeps those). `delete_branch: false` re-creates
  the branch at its old sha. Close mode keeps the worktree and therefore the
  branch. The result goes into the step's `detail.closed` ("deleted" /
  "closed" / "failed: …"). **Failure mode:** if the server restarts between
  the `done` and the thread, the session stays open (the stop is done; close
  it by hand).
- **Open:** `tests/unit/test_flights.py::test_kinds_registry_drives_the_editor`
  pins the exact list of kinds, so it fails for every plug-in kind
  (budget, land, …). The file is outside this task's fence.
### As built: round 15 hooks

- **`flight_kinds/_hooks.py`** (not a kind) has the token store: `mint(fid, sid, kind)` gives back the stop's unspent token if it has one (after a restart, or a retry after a timeout), otherwise it mints `token_hex(16)`. Tokens persist in `<flights dir>/hook_tokens.json` (mode 600) and are spent on use; spent ones are pruned after 30 days. The module builds ONE router, registered through `listen`'s `Kind.router`. It is built by `_build_router()`, which returns None without the web extra, so `flights` still imports there. The module drops `from __future__ import annotations` so that FastAPI sees real `Request` types on the nested routes. A token is accepted only while its stop is the flight's current step in the expected state (listen `running`, someone `waiting_you`) and `detail.token` matches. Anything else answers 404.
- **auth.py**: `_PUBLIC_FLIGHT_HOOK` + `_public_flight_hook_route(scope)` (HTTP only; POST to either shape, GET only to `/done`), consulted beside `_public_voice_act_route`.
- **`listen`**: run → `detail {token, url, full_url?}` (`full_url` when `mobile_announce.act_base()` knows this device's address), note "waiting at <url>". A POST body is read up to 256 KB (413 above that). It is stored as JSON when it parses within 8 KB; otherwise it is stored as text cut to 8 KB, with `detail.truncated`. The **route** does the `match` check: a body without the match is kept in `detail.ignored` (last 2000 chars) and gets a 202, and the token stays armed. A match → `detail.payload`, token spent, loop woken, 200. Judge: payload → done "heard". If `match` was edited after the payload arrived → `run` (re-arm with a fresh token; the old payload is dropped). Timeout → fail "nothing arrived in N min"; a lost token → `run`.
- **`call`**: urllib on a thread into the module's `_JOBS`, read by `observe`. Content-Type is guessed (JSON for a `{`/`[` body) unless a header sets it. Placeholders fill `url` and `body` (`{receipt}` = the nearest earlier stop in state `done`). It reads at most 1 MB and keeps a 2000-char tail. A network error → fail "no answer from <host>: …". Headers are never logged or stored in `detail`.
- **`someone`**: `apply` does park a `you` action returned from `_perform` (verified: `_record_outcome` → `apply` → `waiting_you`), so run sends and returns `{"action": "you", note: text, detail: {items, token, done_url, sent_to, receipt: "done by you"}}`. The link's Done writes `receipt`/`note` "done by <who>" (or "done by someone" for a webhook ask) and `detail.by = "someone"`, then calls `flight_driver.operate(…, "done")`. Your own Done keeps the preset "done by you". The peer path: the link whose `peer_name` matches `who` (case-blind) → `PeerService.human_message`. The webhook gets `{text, items, done_url}`, and `text` carries the items and link too (Slack shows only `text`).
- **Open**: (1) `flights.you_text` has no `someone` branch, so the perch's push/event text is the stop's title, not its `text` (`ss.note` holds the text). That fix is one line in `flights.py`, which is outside this fence. (2) With no tailnet phone URL, the done link is relative (`/api/hooks/flight/<t>/done`) and useless to a peer. (3) Spent `listen`/`someone` tokens of a deleted flight still answer 404 through the step check; nothing calls `_hooks.forget_flight` yet.
### As built: round 15 weather

- `lib/weather.ts` (pure): `weatherFor({usagePct, hour, quiet, reducedMotion})
  → {clouds: 0|1|2, night, word, drift}`. Clouds 1 past 60 %, 2 past 85 %;
  night when the local hour is in `[from, to)`, wrapping midnight (an empty
  window is never night); `word` = "usage 87 %" (usage wins), else "quiet
  hours", else "". `drift` = clouds and not reduced motion. Readers:
  `windowUsagePct(usage)` (the FULLEST live `windowed` provider window in
  `/api/usage`: the Sky has no focused pane), `quietHours(settings)`
  (`notifications.quiet_hours: {from, to}`, hours or "HH:MM"). Clouds:
  `cloudCount` (3 / 6), `seedClouds`, `fitClouds` (new ones wait off the left
  edge and drift in; surplus ones are marked `leaving` and drift off the
  right; without drift they simply appear / go), `stepClouds`.
- `components/flights/useWeather.ts`: `useUsage()` (the top bar's query, no new
  endpoint) + `useSettingsDoc(true)` + `useNow(60s)` + `useReducedMotion()`
  (moved out of SkyFlock, exported).
- `SkyFlock.tsx`: clouds are painted on the flock's canvas before the birds
  (soft radial puffs, greyer and denser past 85 %); under reduced motion the
  canvas still shows static clouds (no birds, no loop) and renders nothing at
  all in clear weather; `paused` freezes the clouds too. Night = a
  `.sky-night` tint div before the canvas (`pointer-events: none`, a 4s dusk
  fade, none under reduced motion). Without a QueryClient (static renders,
  tests) the sky flies in `CLEAR` weather. Exports `WeatherWord` (live) and
  `WeatherLabel({weather})` (pure): "☁ usage 87 %" (`is-cloudy`; `is-storm` =
  attention colour past 85 %) or "☾ quiet hours".
- **Mount `<WeatherWord />` in the Sky's title bar in `SkyView.tsx`** (not done:
  SkyView belongs to another worker).
- **Open: quiet hours do not exist in settings yet.** `NotificationSettings`
  (`backend/config/settings.py`) has no `quiet_hours` and its `from_dict` drops
  unknown keys, so night never shows until that field (+ settings_sync and a
  Settings control) is added. Test: `__tests__/weather.test.ts`.
### As built: round 15 when-ui

- `FlightStep.when?: string` (lib/flights.ts); `planFromSteps` carries it
  (trimmed, left out when empty); `duplicateStep` / `toggleHalt` keep it by
  spread. `newStep` takes the kind's `default_on_fail` through
  `defaultOnFail(def)` ("halt" for an older server or an unknown word).
- `lib/whenHelp.ts`: `WHEN_GRAMMAR` (the contract table's rows),
  `looksLikeWhen` (non-empty, one line, ≤ 300), `checkWhen(expr)` → `GET
  /api/flights/when?expr=…` (null on a 404 or a server/network error: no live
  validation; a refusal carrying the contract's shape is the answer),
  `whenChecker` (300 ms debounce, latest text only, canonical handed back on
  blur, never asked again once the server has no validator), `whenError`,
  `skippedNote`. `checkWhen` lives here, not in `lib/flightsApi.ts` (outside
  this task's fence).
- `StopForm` draws `WhenField` ("Only when", mono, ⓘ → an inline grammar
  popover, Esc closes it) under the kind's fields; the error under the field;
  "If it fails" presses `step.on_fail || defaultOnFail(def)`, marks the
  kind's default with a title and a "Default for <kind>: …" hint.
  `StopInspector` takes `run` and reads a skipped stop's note in the muted
  tone. `kindName` says Someone for `someone`.
- Tests: `stopWhen.test.ts` (27).
- **Open (outside the fence):** `StopDialog.tsx`'s Save builds its
  `StopPatch` from title/config/on_fail/halt only, and `lib/flightsApi.ts`'s
  `wireStep` / `patchStop` drop `when` — so on a session's line the field
  edits but `when` is not sent until those carry it (`StopPatch` and
  `patchStop`'s `Pick` gain `"when"`, Save sends `when: draft.when ?? ""`,
  `wireStep` copies a set `when`).

### As built: round 15 triggers

`TRIGGER_KINDS` is `manual, ticket, pr, issue, schedule, main_push, main_red,
hook` (`MAIN_TRIGGERS` = the two `main_*`). The trigger block of every plan
now carries `repo` (text, ≤ 1000), `quiet_minutes` (int, default
`TRIGGER_QUIET_MINUTES` = 60, 0..10080) and `token` (32 lower-case hex or
""). Strict mode refuses a `main_*` trigger whose `repo` is empty ("a
repository to watch is needed"), relative ("the repository is an absolute
path") or not a git work tree (a `.git` dir or worktree file:
`flights._is_repo`, no subprocess) — all `PlanError(…, "trigger.repo")`;
lenient (synced) plans keep the text. **The token** is minted by
`flights.mint_hook_token` inside `save_plan` and `put_plan`: a `hook` plan
without one gets `token_hex(16)`; a save that leaves the token out keeps the
stored one; a plan switched off `hook` keeps its token (switching back keeps
the URL) but no longer answers. A flight's `by` accepts the trigger names
(`BY_KINDS` = user, intake, schedule, main_push, main_red, hook).

**The watcher** (`backend/web/core/flight_triggers.py`). `template_schedule.
run_loop` has a third step, `_tick_triggers` → `flight_triggers.maybe_tick()`,
which paces the loop's 30 s beat down to `TICK_SECONDS` = 60 and calls
`tick(now, start, plans, checks_reader)` (the tests' seams). Per watching
plan (`main_*`, not paused, a repo set) on a thread: `default_branch(repo)`
(`origin/HEAD`'s short name, else main / master on origin or locally) then
`fetch_tip` (`git fetch --quiet origin <branch>` with `GIT_TERMINAL_PROMPT=0`,
60 s, then `rev-parse origin/<branch>`; a repo with no origin reads the local
branch). State lives in `<flights dir>/trigger_state.json`, `{plan_id:
{branch, sha, fired_at, pending?, held?, error?, last_status?, checks?}}`,
rewritten only when a row changed; rows of plans that stopped watching are
dropped (a hook plan's `fired_at` row stays). Rules:

- **first sight arms**: the tip is recorded, nothing fires (a schedule counts
  from when it was set).
- **`main_push`** fires when the tip differs from `sha`. Inside
  `quiet_minutes` of `fired_at` the move is kept as `pending` (the seen sha
  stays) and fires, once, on the latest tip when the quiet is over.
- **`main_red`**: `checks` is `{sha, verdict, names, fired, read_at}` for
  the tip; `read_checks(repo, sha)` (GitHub Checks + combined Status API
  through `github_pr.repo_ref` / `api_token` / `_request`; failing names
  come from `check_runs[].name` and `statuses[].context`) runs each tick
  while the verdict is `pending` / `none` / `unknown` and stops once it is
  `ok` or `failed` (final per sha). `failed` + not yet `fired` + outside the
  quiet → fire, `fired = true`: once per sha. A tip already red at first
  sight fires too.
- **busy = held, not skipped**: before firing, `flight_busy_reason` of the
  plan's run record; the previous flight still in the air (or its session
  working) puts `held: <why>` on the row and the move waits — no skipped run
  and no `flight.changed` every minute — then fires when it lands. A refusal
  (400: no folder, no agent…) is the schedule's own failed run and **spends
  the move** (`sha` advances / `fired = true`, `last_status`, no `fired_at`):
  never a retry a minute.
- a fire is `template_schedule.fire_or_skip(plan, kind, subject=…)` →
  `fire_flight`, which gained `subject` (default: the schedule's `{kind:
  schedule, title: <plan name>}`) and records `by` = the trigger's name
  (`_BY_OF_TRIGGER`; anything else is `user`). Subjects: `{kind: "manual",
  title: "main moved to a1b2c3d"}` / `"main is red at a1b2c3d (ci, lint)"`
  (≤ 6 names, then "+N more"). The run lands in `schedule_runs.json` under
  the plan id with `trigger: main_push` etc.
- one plan's exception becomes its row's `error` (`"no default branch"`,
  `"cannot fetch main"`, the exception text); the rest of the tick goes on.

**The hook.** `POST /api/hooks/flyway/{token}` (routes/flights.py, before
`/api/flights/{flight_id}`; public in auth.py: `_PUBLIC_FLYWAY_HOOK =
re.compile(r"/api/hooks/flyway/[a-f0-9]{32}")`, `_public_flyway_hook_route`
= HTTP + POST + fullmatch, added to the gate's `public` tuple beside the voice
act — GET on the path, 31 / 33 chars, upper-case hex and every neighbour stay
401). The body (first `HOOK_BODY_MAX` = 64 KB) names the subject via
`hook_title`: a JSON object's `title` (else `text`), else the text itself,
whitespace squashed, the first 80 chars → `"hook: <text>"` (`"hook"` for
nothing). `hook_fire` answers 404 `unknown_token` (no `hook` plan holds it),
409 `paused`, 429 `quiet` with `retry_after` seconds and a sentence, 409
`busy` while the previous flight is in the air (via `fire_or_skip`, which
records the skip — a POST cannot wait), else 202 `{flight}` and `fired_at` in
the state row. `fire_or_skip` is the busy-checking wrapper the hook uses;
`tick_flights` keeps its own inline copy of the same rule.

**Frontend.** `TriggerKind` + `TRIGGER_LABEL` ("When main moves", "When main
is red", "When a hook fires"); `FlightTrigger` gains optional `repo`,
`quiet_minutes`, `token`; `MAIN_TRIGGERS`, `triggerMakesSession(kind)` (the
three make their own session like manual / schedule, so the Repository /
Agent / Model fields follow) and `hookUrl(trigger, origin?)` (`<origin>/api/
hooks/flyway/<token>`, "" until saved; `window.location.origin` when there
is a window). `TakeoffFields` (TakeoffDialog.tsx) shows, for `main_*`, a
**Watches** row — `PathField` (`id="fl-trigger-repo"`, placeholder "An
absolute path to a git repository") with a hint saying what is watched —
and for `hook` a **Hook** row (`HookUrlRow`: before the plan is saved the
hint "Save to get the address outside systems POST to."; after, a read-only
`fl-mono` input that selects itself on focus and a **Copy** button →
`navigator.clipboard.writeText` + toast "Copied the hook address", "Copied"
on the button for 1.5 s, a toast when the clipboard is not there — never
`prompt`); both get **Quiet** (`NumberField`, min) and **Paused**.
`TakeoffDialog.css`: `.fl-hook-url` flex row, the input `flex: 1 1 auto`,
`.fl-hook-copy` nowrap. The plans list carries `runs` for schedule plans
only, as before: a trigger plan's last fire is in `schedule_runs.json` but
not yet shown — a follow-up for whoever extends `flight_plans_status`.

Tests: `tests/unit/test_flight_triggers.py` (28 tests, 49 cases: the kinds and fields, the
token, a real tmp repo + clone for the fire rules, quiet minutes, held /
spent moves, `read_checks` over a faked `_request`, `hook_title`, the hook
route on a bare `FastAPI()` + the flights router, the gate regex and the
gated app), `frontend/src/__tests__/flightTriggers.test.ts` (10).
`test_flights_schedule.py` needed no change (`fire_flight`'s default subject
and `by` are the schedule's).
### As built: round 15 preview-shots

**`preview`** (`flight_kinds/preview.py`, tests `test_flight_kind_preview.py`;
outside · `remote` · robot, `needs_session`). Config `{command,
timeout_minutes (20), stop_command}`; a strict plan refuses an empty
`command` (`…config.command`), a lenient one keeps "" and `run` fails "the
preview stop has no command". Title "Deploy a preview". `run`: the session's
folder from `_session_obs` (none → fail "no worktree"), then `run_shell(…,
keep=8000)` there with `MINDFLOCK_BRANCH` = the session's branch;
`detail.command`. `observe` = `job_of`. The judge: no job this server run →
`run` "starting the preview again (interrupted)"; running → wait "deploying
the preview"; exit ≠ 0 → fail "the preview command exited N" ("did not
finish" for -1) + the output's tail (≤ 2000 in all); exit 0 → `find_url`:
the **last** `MINDFLOCK_PREVIEW_URL=<url>` line anywhere, else the last
http(s) URL on the last non-empty line (so "Website draft URL: https://…"
works; trailing `.,;:)]}>"'` dropped, ANSI stripped), validated with
`urllib.parse` (http/https, a host, a sane port); none → fail "printed no
URL" + tail. Done: `detail {url, exit, output (tail), receipt}`, receipt the
host (`localhost:5173` keeps its port).

`preview.latest_url(flight)` is the last done preview stop's `detail.url` in
plan order ("" when none) — the rule `fill_prompt`'s `{preview}` should use.

`preview.stop_all(flight) -> [{step, exit, output}]`: blocking, never
raises; for every **done** preview stop with a `stop_command`, runs it with
`/bin/sh -c` in the session's worktree (the cwd is left alone when it is
gone), env `MINDFLOCK_PREVIEW_URL` / `MINDFLOCK_BRANCH` / `MINDFLOCK_FLIGHT`,
120 s each; a non-zero exit goes to the error log. **For the integrator**
(the driver is outside this fence): call `await
asyncio.to_thread(preview.stop_all, flight)` once when a flight finishes
(`done` / `failed`) and when it is cancelled or removed — nothing calls it
yet, so `stop_command` is stored but never run until then.

**`screenshot`** (`flight_kinds/screenshot.py`, tests
`test_flight_kind_screenshot.py`; outside · no risk · robot, `needs_session`
false). Config `{urls ("{preview}" when empty), widths ("1280, 390"),
timeout_minutes (10), comment_pr (false)}`; title "Screenshots". **No
playwright**: the browser's own headless mode. `find_browser()`:
`$MINDFLOCK_CHROME` (an executable path, or a name on PATH; a bad one falls
through), else `google-chrome`, `google-chrome-stable`, `chromium`,
`chromium-browser` on PATH; none → fail "no browser: install google-chrome
or chromium, or set MINDFLOCK_CHROME". `run`: `fill_urls` (`{preview}` =
`latest_url`, then `fill_prompt` for the rest; one per line, lines that are
not http(s) URLs once filled are dropped; none → fail "no URL to shoot"),
`parse_widths` (every number, clamped 200..4000, deduped; [1280] when
none), `plan_shots` (URL × width numbered from 1, ≤ 40), the store folder
`<flights_dir()>/shots/<fid>/<sid>/` made and an earlier run's `N.png`
cleared, then `run_shell` of `build_script`: a `mktemp -d` throwaway
profile (removed on exit; an open Chrome of the owner's is never touched)
and per shot `<chrome> --headless=new --disable-gpu --hide-scrollbars
--no-first-run --no-default-browser-check --user-data-dir=… --window-size=W,900
--screenshot=<dir>/N.png <url> || echo 'shot N failed'` (everything
`shlex.quote`d; one failed URL does not stop the rest). `detail {pairs,
browser}`. Verified once by hand with `/usr/bin/google-chrome` here: 1280×900
and 390×900 PNGs.

`observe` → `obs["screenshot"] = {job, shots, comment}`: once the job ended,
`list_shots` (the non-empty `N.png` files in number order → `[{name, url:
"/api/flights/<fid>/shots/<sid>/<name>", width, page}]`, width/page from
`detail.pairs`). The judge: no job → `run` "taking the screenshots again
(interrupted)"; running → wait; no shots → fail "no screenshots were taken"
+ the output's tail; else done, `detail {shots, output, comment?}`, receipt
"N shots" ("1 shot"). Chrome's stderr noise (dbus, GPU) lands in the tail.

The router (`Kind.router`, built only when FastAPI imports, so the CLI
without the web extra still loads the kind): `GET
/api/flights/{flight_id}/shots/{step_id}/{name}` → `FileResponse`
`image/png`; ids must match `[A-Za-z0-9_-]{1,64}`, the name `^[0-9]+\.png$`,
anything else (or a missing file) → 404 `not_found`. It sits behind the
normal auth gate like every `/api` route.

`comment_pr`: `github_pr` has no comment function, so `post_comment` uses
its own `comment_pr(wt, number, body)` when one appears and otherwise the
module's REST seam (`repo_ref` + `api_token` + `_request("POST",
"/repos/<slug>/issues/<n>/comments")`). Text only: "Screenshots from
MindFlock (N):" + "- N.png — <url> at <w>px" per shot (the PNGs are local,
not uploaded). The outcome is `detail.comment`: "posted", "skipped: the
session has no PR" / "no GitHub repo behind the session's origin" / "no
GitHub token", or "failed: <GitHub's words>"; it never fails the stop. It is
posted once per (flight, step, `since`) per server run (a memo, like
review's), so a restart after posting but before the verdict posts again.

Open: the verify checklist's deep link does not carry the preview URL yet
(outside this fence); nothing in the frontend shows `detail.shots` yet.
### As built: round 15 when-judge

**`when` (`backend/web/core/flight_when.py`, pure).** `parse(text) -> Cond`
(a nested tuple: `("touches", [globs])`, `("lines", ">", n)`, `("files", "<",
n)`, `("weekday",)`, `("weekend",)`, `("hour", h1, h2)`, `("branch", glob)`,
`("label", name)`, `("judge", sid_or_"", value)`, `("always",)`, `("never",)`,
`("not", c)`, `("and", a, b)`, `("or", a, b)`), `canonical(cond)`,
`evaluate(cond, facts) -> (bool, why)`, `needs(cond) -> {"diff", "labels",
"clock", "branch", "judge"}`, `matches(path, glob)`. Case-insensitive words,
`and` tighter than `or`, `not` a prefix, parentheses allowed; `WhenError(message,
hint)` names the problem ("files needs a whole number, not 'x'", "expected
'and' here, found the end") and the hint lists the words. Canonical: lower-case
words, one space, globs as typed, `and` chains flat, parentheses only where an
`or` sits under `and` or `not`. A judge value is lower-cased; a label is
compared case-insensitively. Globs: `frontend/**` and `frontend/` and a bare
`frontend` mean under that folder (anywhere in the tree for the trailing-slash
form), `*.sql` matches the file name anywhere, a pattern with a `/` matches the
whole path (`*` never crosses `/`, `**` does, `a/**/b` also matches `a/b`),
a bare file name matches at any depth. `hour between H1 and H2` is `[H1, H2)`
and wraps past midnight when `H2 < H1`; `H1 == H2` is always true. A missing
fact reads as empty: no diff → "the diff touches no file", no labels → "no
labels", no judge → "the judge has not answered". `why` for `and` is the first
false side (or the trues joined with `; ` when it holds), for `or` the first
true side (or all the falses joined).

**Core (`flights.py`).** `normalize_step` keeps `when` canonicalised (≤ 300
chars; strict → `PlanError(message, "steps[i].when")`; lenient → dropped);
`_normalize_when(raw, strict, where)` is what the route calls too. A pending
step's `when` is judged right after the halt point (a halt still pauses before
a skipped step) and before the `you` / ticket shortcuts and the session hold:
`obs["facts"]` present and false → `{"action": "skip_when", "note": "skipped:
<canonical> — <why>"}`; `apply` records it as `skipped` (state, note, history
`skipped`, cursor on) and the driver's chain carries on into the next step in
the same pass; facts absent with a `when` → wait "checking <canonical>" (a
safety net: the driver always gathers them); a stored `when` nobody can parse
never blocks a flight (it runs). `fill_prompt` gains `{preview}` =
`preview_url(flight)`, the latest done `preview` stop's `detail.url` or "".

**Driver facts (`flight_driver.py`).** `observe` gathers `obs["facts"]` only for
a pending step with a `when`, and only the groups `needs` names (`when_facts`):
`clock` = the server's local hour + weekday (Monday 0), `branch` = the row's
branch (else the flight's), `judge` = `when_judges(flight, step)` → `{sid:
value}` of every done judge stop + `judge_last` = the nearest earlier one,
`labels` = `_when_labels` (the subject's number when it is a PR / issue, else
the PR found from the branch, then `GET /repos/{slug}/issues/{n}/labels` with
the existing token; cached 60 s per flight; `[]` without a PR, token or answer),
`diff` = `when_diff(flight, folder)` → `{files, added, removed, base, head}`.
**The base** is `diff_base`: the server's `_session_base_branch(inst)` (the
recorded base, else `origin/HEAD` → main/master → the configured base), else
`origin/HEAD`'s target, then main / master; each candidate is tried as the local
branch first, its `origin/` twin second, verified with `rev-parse --verify`.
The diff is the **working tree** against the merge-base (`git diff --numstat
<merge-base>` + `ls-files --others --exclude-standard`, untracked files counted
by their lines, ≤ 200 of them), so a condition before the first commit still
sees the agent's edits. **The cache** `_WHEN_DIFF` is keyed `(flight id, HEAD
sha + a sha1 of git status --porcelain)`: a warm pass costs `rev-parse` +
`status`, an edit or a commit misses; ≤ 64 entries, the flight's own evicted
first. A group that cannot be gathered reads as empty (logged); the condition
decides, the flight never wedges. `diff_stat_text(diff)` renders the facts for
a model prompt.

**Route.** `GET /api/flights/when?expr=…` → `{ok, canonical, error, field,
hint}` (`field` is `when` on an error; empty `expr` is ok with `""`). Before
the `{flight_id}` routes.

**Judge (`flight_kinds/judge.py`, kind `judge`, family work, robot, risk none,
`needs_session` + `types_to_agent`).** Config `question` (textarea, required
strict → `steps[i].config.question`), `options` (text; comma- or line-separated,
lower-cased, deduplicated, ≤ 12, default "yes, no", stored as "a, b"), `by`
model | agent (default model; strict → `steps[i].config.by`). Default title
"Judge: <first line of the question, 50 chars>". `by: model`: `run` starts a
thread (`_ANSWERS[(fid, sid)] = {running, answer, error}`, like the command
kind's job) that asks `_ask_model(prompt)` — the describe machinery
(`flight_describe._program` + `commit_message.pick_argv/_run/neutral_cwd`,
60 s) — with `diff_stat_text(when_diff(...))` + the question + "Answer with
exactly one of: <options>"; `observe` returns the record, `judge` waits
"asking the model: <question>", fails with the one-shot's reason, else `pick`
reads the answer (the last `ANSWER:` line when there is one, else the text;
exact option first, else the one option appearing as a whole word on the last
line) → done `detail {value, receipt: value, answer (≤ 300)}`, note "the model
answered yes"; outside the options → fail "the model answered '<last line>' —
not one of yes, no". A lost thread (restart) → run again. `by: agent`: `run`
delivers `agent_prompt` (the question + "Reply with exactly one line of the
form ANSWER: <one of: …>") through `_deliver`; `judge` runs `flights._settle`
(worked + idle for the settle window), then reads `obs["judge"]["answer"]`,
which `observe` fills — only while the agent is idle — from
`session_stats._agent_transcript_text(folder, tmux name)` via `answer_after`:
the first `ANSWER:` line AFTER the last occurrence of the question (never an
earlier judge's). No line → fail "the agent gave no ANSWER: line"; no
transcript (a non-Claude session) → fail "… (by: agent needs a Claude session)
— use by: model". So `by: agent` is built, Claude sessions only.

**Limits.** `label` needs a token and a PR / issue number; the model judge
costs one one-shot per run (no cache: the diff may have moved); the diff
counts untracked files up to 200 and reads ≤ 1 MB of each; `judge is` with no
earlier judge stop reads "the judge has not answered" (false). `{preview}`
fills from a `preview` stop that the `preview-shots` task builds. Nothing in
the frontend yet (`when-ui`). Tests: `tests/unit/test_flight_when.py`,
`tests/unit/test_flight_kind_judge.py`.
### As built: round 15 looks

The `looks` task of round 15 (2026-10-30): the Sky's boxes and lines carry
information. Files: `FlywayCanvas.tsx/.css`, `SkyLine.tsx/.css`, `Bird.tsx`,
`StopMenu.tsx`, `useSessionLine.tsx` ("What it did"), `StopDialog.css`,
`lib/flightDiagram.ts`, new `lib/stopRisk.ts` and `lib/lineTime.ts`. Tests
`stopRisk.test.ts`, `lineTime.test.ts`, `skyLooks.test.ts`. The geometry of
a box never changes with any of it (the semantic-zoom rule holds).

**The data on a node.** `flightStops` and `flywayDiagram` put three optional
fields on a stop's `DiagramNode`: `risk` (`RiskTag[]`, only when non-empty),
`receipt` (a flown stop's `detail.receipt`, one line, ≤ 80 chars via
`receiptOf`) and `when` (the step's `when`, read as `(s as {when?})` since
the type lands in `when-ui`). `groupNode` wears the union of its sub-steps'
`risk`. The canvas reads the node; it needs no `kinds` prop.

| # | What | Hook |
|---|---|---|
| 1 | **Risk stripe** — `riskOf(step, kinds)`: the kind's `risk` tags; ship `lane: merge` adds `irreversible`, `lane: commit` has none; an older server without `risk` still marks ship and ticket `remote`. Canonical order remote → irreversible → spends. | `<span class="fl-risk" data-risk="remote irreversible" title="Touches the shared remote · Can't be undone">` with one `<i class="fl-risk-<tag>">` per tag, a 3px column inside the box's left edge, bands stacked top to bottom. Tokens on `.fl-canvas`: `--fl-risk-remote: var(--tone-waiting)` (the ship chips' blue), `--fl-risk-irreversible: var(--red)`, `--fl-risk-spends: var(--gold)`. Flown boxes dim it to 55 %. `riskTitle(tags)` is the tooltip. |
| 2 | **Receipts** — a flown box shows `detail.receipt` under its title; "What it did" (`stopLog`) leads with it and shows `detail.shots` as thumbnails. | `.fl-box-receipt` right after `.fl-box-head`, muted, one line (two at wide / near); not rendered at far and `.fl-lod-far .fl-box-receipt {display:none}` holds the band either way. Dialog: `<p class="stop-dialog-receipt">`, then `.stop-dialog-shots` of `<a class="stop-dialog-shot" href=url target=_blank>` each with `<img loading=lazy>` and the name · width (`shotsOf(detail)` keeps the well-formed entries). A receipt or shots alone are enough to show the section. |
| 3 | **"if" chip** — a stop with a `when`. | `<span class="fl-box-if" title="Only when <expr>">if</span>` in the head after the title; hidden far. |
| 4 | **Time under the line** — `segmentsFor(flight)`: one segment per flown stop (done / skipped / failed) in line order, its seconds from `history` (`started` → `done`/`failed`/`skipped`, every round added; never started = 0); `fmtDuration`, `segmentTitle` ("Checks · 12 min", "Push · 1 min · failed"), `totalSeconds`. | `LineRuler` (SkyLine): `.skl-ruler` > `.skl-ruler-bar` (4px) with `.skl-ruler-seg.skl-ruler-<state>` sized by `flex-grow: seconds` (floor 0.5 % so a quick stop still shows), `data-step`, `title`; `.skl-ruler-total` at the right; `aria-label="Time flown: 12 min over 2 stops"`. Under the canvas inside `.skl-scale`, rendered only when `lod !== "far"` and hidden by CSS at far too; nothing drawn when no stop has flown or none took time. |
| 5 | **Ageing** — `ageOf(activity_since)`: `"old"` past 24 h, `"stale"` past 72 h, null for 0 / unknown; `ageWord` → "3 days still". | `.skl[data-age="old"|"stale"]`: the bar yellows like paper (a gold gradient, stronger when stale, the name tinted); `<span class="skl-age">3 days still</span>` in the bar when stale (hidden far). `activity_since` is the row's, epoch seconds. |
| 6 | **Formation** — `birdFormation(birdPerches(d))`: with a main bird on the line, every worker bird (`child`) gets `formation: formationSlot(k)` = `{dx: -row, dy: ±row}` in bird-lengths (k 0,1 → row 1; 2,3 → row 2…); with no leader, no slots. `birdPerches` itself is unchanged (round 12's `skyBranches.test` pins its output). | The canvas draws a slotted worker bird at `formationAt(leader, slot, 18, vertical)` — 0.9 bird-lengths back along the line (left, or up when vertical) and out to the side — instead of on its branch box; `BirdMarker` gets `formation` → `data-formation="v"`; `.fl-bird-marker.fl-bird-branch[data-formation]` is lighter and hops into place. Class and `aria-label` stay round 12's; the click still opens the child. While the leader is being dragged the workers stay on their boxes. |
| 7 | **Icons** — `StopIcon` glyphs for test (flask), sync (two arrows merging), checkpoint (flag), land (plane onto the strip), budget (coin), review (eye), judge (scales), call (plug, arrow out), listen (antenna), someone (person), preview (monitor with a globe), screenshot (camera). | Same 24×24 stroke style; all twelve render distinct markup, none the fallback circle. |
| 8 | **Rewind to here** — a flown (`done`) `checkpoint` stop of a live flight: `canRewind(n, flightId)`; `withRewind(items, n, flightId)` slots `REWIND_ITEM` (`key: "rewind"`, danger) before the menu's notes. `StopMenu` now takes `SkyMenuItem` / `SkyMenuKey` = lib/stopEditing's keys plus `"rewind"`. | The press opens `.fl-rewind` at the menu's spot on the stage (counter-scaled by `--sky-z` like the menu): the app's `InlineConfirm` ("Rewind to “Checkpoint”?", body names the receipt, confirm "Rewind", Cancel focused; Esc closes and refocuses the box) — never `window.confirm`. Confirm → `POST /api/flights/{id}/steps/{sid}/op/rewind` through `api()`; success toasts and refetches flights + rows; a refusal (409 while the session works) shows the server's sentence in `<p class="fl-rewind-error" role="alert">` under the question and the row stays for a second try. |

Bands, in one place: **far** = icon + title (+ the risk stripe, which is
geometry-free paint); no receipt, no "if", no ruler, no age word. **Mid** =
all of the above plus receipt (one line), "if", ruler, age word. **Near** =
the receipt may wrap to two lines; the ruler's type counter-scales.

Open: the receipts themselves come from the backend `receipts` task — until
it lands, only kinds that already set `detail.receipt` show one. The
`check_rewind` 409 sentence is the server's; the UI shows whatever it says.

### As built: round 15 palette-groups

Source: `lib/stopCatalog.ts` (`familyOrder`, `familyLabel`, `hasFamilies`,
`familyGroups`, `catalogCards` → `groups` / `grouped`),
`components/flights/StopCatalog.tsx` (+ `.css`), `SkyPalette.tsx` (its
docstring only); tests `stopFamilies.test.ts` (new) and the source pins in
`stopCatalog.test.ts`. `StopShelf` is untouched.

**The families.** `familyOrder = ["work", "ship", "outside", "people"]`;
`familyLabel(f)` → Work · Ship · Outside · People, a family the frontend
doesn't know reads as itself capitalised, none reads **Other**.
`familyGroups(kinds, grouped = hasFamilies(kinds))` groups in that order, then
any unknown family (as first seen), then the kinds with no family (Other,
last); each group keeps the order it was given, and only non-empty groups come
back. `hasFamilies` is "some kind names a family": an older server (no
`family` on any row) gives ONE flat group (`family` and `label` ""), or none
when there are no kinds.

**The cards.** `catalogCards(kinds, plans, query)` now also returns
`grouped` (decided over EVERY kind, so a filter never flips the layout) and
`groups` (the filtered kinds' cards by family; a filter hides the groups it
empties). `blocks` (and so `all`, the arrows' order) is the groups flattened:
`catalogKinds`' defaults-first order holds inside each family (Work: agent,
split, join, command…; People: Perch, then Verify).

**The body.** Grouped, the Blocks section (`data-section="blocks"`, still
`role="group"` "Blocks") holds one `<div class="sky-catalog-family"
data-family="…">` per group: a thin label row (`<h3
class="sky-catalog-label">Work</h3>`) over its own `.sky-catalog-grid`; the
section's "Blocks" heading is gone (the family labels replace it). The list
gets `grouped`, which turns every label — Multi-step's too — into a row: the
label then a 1px hairline to the edge (`::after`, `var(--border)`), so the
saved stops' region (still its own section, after the families) lines up with
them. Compact (the palette) the labels are 10px and the family gaps 8px.
Flat (an older server) the markup is round 14's exactly: one "Blocks" grid,
no `grouped` class, no family labels at all.

**The lead.** The palette's Session card sits first in **Work** (flat: first
in the one grid). A filter that matches "Session" but no work kind keeps an
otherwise empty Work group holding just the Session card; Work leads the
order, so the lead is always the first card and the arrows' `- leads` offset
holds. The arrows walk the grids as laid out: `gridMove`'s sizes are one per
family (the lead counted in Work) then Multi-step, so ↓ from Work's last row
lands in Ship's first, keeping the column; `gridCols` reads the first grid
(they all share one template).

**Perch** keeps its gold (`.sky-catalog-card.perch`, from `actor === "you"`)
inside People. **The bottom shelf** stays the flat ★ pick: its markup is the
same byte for byte with or without families (pinned in `stopFamilies.test.ts`).

Not seen in a browser: no `npm run build` in this task (fenced, and the
contract says not to), so the label row's look is checked by its CSS pins
only.
### As built: round 15 objects

> Code: `frontend/src/lib/skyObjects.ts` (the registry), `SkyView.tsx` (one
> loop: `objects`, `hostFor`, `objectFor`, the drag onto a block, the notes),
> `lib/skyLayout.ts` (`LANDING_*`, `NOTE_*`, `BLOCK_*`, `rightColumn`),
> `LandingStrip.tsx/.css`, `PeopleBlocks.tsx`, `DeviceBlocks.tsx`,
> `StickyNote.tsx/.css`, `SkyObjects.css`, `api/skyApi.ts`; backend
> `backend/web/core/landing.py`, `core/sky_notes.py`, `routes/sky.py`
> (included from `server.py` next to the flights router). Tests:
> `skyObjects.test.ts`, `landing.test.ts`, `tests/unit/test_landing.py`,
> `tests/unit/test_sky_routes.py`; the round 12/13 pins in `skyIntake`,
> `skyPalette`, `skyView`, `skyResize`, `skyStable`, `sky`, `skyMerge` still
> hold.

**The registry.** `SKY_OBJECTS: SkyObjectDef[]` has one entry per KIND —
`{key, kind, place, min, max, className, accepts?, frame(stored),
size(stored, measured), on(data), render(host)}` — in placement order:
`intake` and `palette` (`place: "left"`: first in `assignSpots`, as before),
`landing`, `device:` and `person:` (`place: "right"`: a column right of every
line), `note:` (`place: "free"`: wherever it was put). `skyObjects(data)`
expands the kinds into the objects on the board now (`on(data)` reads the
view's mode, phone-ness, `intakeOn` / `paletteOn`, the links, the devices,
the notes — a phone and This session have none but the lane). SkyView
iterates that ONE list: the placement memo (`rightItems`, `rightColumn`,
`notePos`), `lineAlive` (`objectById.has(id)` first), `rectOf`, `fitAll`,
`spotRef`'s `taken`, and the plane (`{objects.map(objectFor)}`). Every object
gets a `SkyObjectHost` — `{key, zoom, lod, layoutEntry, pos, draggable,
selected, dragging, target, onHeadDown, onResize, onMeasure, onLayout, view,
data}` — where `view` is the view's own data and handlers (`SkyObjectView`:
rows, plans, the landing data, the links, the devices, the notes, `dropped`
/ `answerDrop`, the note handlers, and `renderIntake` / `renderPalette`).
The intake lane and the palette keep their JSX in SkyView (their props are
the round 12/13 closures, pinned by those tests); the registry's `render`
for them calls back into the view. The others render through the registry
(`createElement`) inside a `.sky-object[data-object=<key>]` wrapper placed
like a slot (`SkyObjects.css`: absolute, glides only under `data-tidying`,
`is-dragging` / `is-selected` / `is-merge-target` from `objectClass`).
`skyObjectOf` reads `data-object`; `skyTargetOf` takes any `[data-sky-head]`
as a "line" press, so an object's head drags it on the line drag's own path
(`onDragStart`, pinned where dropped; a click on it focuses no session —
`isObjectKey(id)` returns before the focus).

**The right column** (`rightColumn(placed, items)`): a newcomer with no spot
goes just right of everything placed (`max(x + w) + SKY_GAP`), stacked from
`SKY_MARGIN` with half a gap between — the strip, then the device blocks,
then the people blocks. Its spot is kept like a line's (`fresh` → `autoPos`
once measured), so a line that grows or arrives later never shoves it; Tidy
forgets it and it is placed again.

**The landing strip** (`LandingStrip`, key `landing`): the intake lane's
chrome (`.sky-lane`, its head with `data-sky-head`, the ‹ fold to a rail
remembered in `mf_sky_landing_folded`, the ▸/▾ expand on `open`, the corner
`onResize` with the delta over the zoom, a ResizeObserver to `onMeasure`;
its frame 220–1200 × 120–1600, `landingFrame`). One block per repo: the
checks dot (`is-green` / `is-red` / `is-pending` / `is-none`, tooltip the
head's short sha), the folder's name, the default branch, "merged 2h ago"
(`mergeWords`), and **Landed today**: newest first, the session's NAME
(`windowName`), "PR ↗" when there is a URL, three rows folded (`+N more —
expand ▸`), every row expanded. Data from `useLandingData(enabled)`
(`GET /api/sky/landing`, polled every minute, off in This session and on a
phone). Backend `core/landing.py`: `repo_roots(instances)` (this machine's
sessions' repo roots — never a `dev::` row or a `PeerShare` — distinct, in
order, existing), `repo_facts(root)` (git only: `default_branch` from
`origin/HEAD` else `main` / `master` else the checked-out branch;
`branch_ref` prefers `origin/<branch>`; `head_of`; `last_merge_at` = the
last merge commit's time, else the last commit's), `landed(flights, root,
now)` (state `done`, `finished` within 24 h, `repo_root` = this root, the
LAST DONE ship step's lane `merge`; `pr_url` from that step's detail
`pr_url` / `url`), `checks_tone` (`pr_checks`'s ok / failed / pending →
green / red / pending, none and unknown → none; a raised check reads none),
and `landing(instances, flights, now)` cached one answer per set of repos
for `CACHE_TTL_S` = 60 s — the only network call is the checks.

**People blocks** (`PersonBlock`, key `person:<link id>`, from
`usePeerLinks()` → `links`): the peer's name and a state dot (`verified` /
`unverified` / `offline`), what the link already shares ("Shared · name" or
"Shared · N sessions", from `fetchPeerSessions`, asked on mount and after a
share). A session's line dragged ALONE (not grounded, not a selection)
hit-tests the blocks that `accepts: "session"` on every move (`hitAt`); the
block under the pointer wears `.is-merge-target`, the dragged slot
`.is-merging`; a release over one does not pin the line — the block asks
inline (`dropped[key] = title`): "Share “name” with Bob?", the mode radios
(`MODE_CHOICES`), the "I understand" tick for a full share (`canShare`),
Share / Cancel. The gates stay: `sas_verified` false → "Press It matches in
People first" and no Share button; a title not in `shareableSessions` →
refused with the reason. Share → `sharePeerSession(link_id, title, mode)`,
the People screen's toast, `answerDrop(key)`. Esc cancels every open ask.

**Device blocks** (`DeviceBlock`, key `device:<name>`, from `useDevices()`
minus `self`): the host name, the connected dot, "os · N sessions". A line
dropped asks inline: "Move “name” to mac-mini? Its agent pauses while it
moves.", the "Even if it is mid-turn" tick (`force`), Move / Cancel; a
device not connected refuses with the reason. Move →
`moveSessionToDevice(title, device, force)` = the existing `POST
/api/instances/{title}/move {device, force}`; a refusal shows the route's
sentence (`moveErrorText`), and `bad_dest` — what a server that only moves
folders on this machine answers — reads "This server can't move a session
to another device yet" (`NO_DEVICE_MOVE`). **Open:** `core/move_session.py`
moves folders on one machine and refuses other devices' rows; a `device`
target (copy the worktree over, start it there, close it here) is backend
work outside this task's fence — until it lands the block is honest about
it.

**Sticky notes** (`StickyNote`, key `note:<id>`): ⋯ (the toolbar's new
`OverflowMenu`) → **Add a note** in the middle of the view, or a
right-click on empty sky (`onContextMenu` → `.sky-ground-menu` at the
pointer, closed by any press elsewhere or Esc) → one at the pointer; it
opens in edit at once. A note is paper-yellow with a thin head
(`data-sky-head`: drags it; a press focuses the note root, `tabIndex -1`), its
text (a click edits: a textarea, blur saves through `changeNote`, Esc puts
the text back, 2000 characters at most), a corner (`onResize` → the note's
own `w` / `h`), and × / Delete / Backspace (SkyView's capture-phase Delete
sees `.sky-note[data-note]` first and calls `removeNote`: gone, with a
"Note removed" toast whose Undo puts it back). Notes are stored
SERVER-SIDE: `useSkyNotesQuery()` reads `GET /api/sky/notes` once, the view
keeps them (`notes`, `notesRef`) and `putNotes` replaces them with `PUT
/api/sky/notes` 400 ms after the last change; `core/sky_notes.py` keeps
`<flights dir>/sky_notes.json` (`normalize_notes`: junk dropped, ids made
unique, `w`/`h` clamped 120–800, text capped, at most 500 notes; a non-list
is a 400 `bad_notes`). A note's spot is its own `x` / `y` — never the
layout store — so `place[note:<id>] = notePos(note, lineAt)`: free at its
numbers, attached (`line`) relative to that line's spot, so it moves with
the line (a line that is gone leaves it at its numbers). A note dragged
alone hit-tests every line; released over one it attaches (`noteDropped`:
the offset from the line's spot), elsewhere it sits free where it fell.
Notes are not selectable by the marquee and are never on a phone or in
This session. The palette's **Note chip** named by the contract belongs to
the `palette-groups` task (SkyPalette is not this task's); the ⋯ and the
right-click are the two ways in for now.

**Not changed:** the marquee still takes only the lines and the lane
(`selectables`); `SkySelection`'s Remove and Tidy these never see an object;
the shelf chip drops (`lineAt`) ignore objects, so a chip dropped on the
strip lands on empty sky under it.

### As built: round 15 builtins

**The built-ins.** `_builtin_defaults` keeps the original four first and in
their order (`BUILTIN_IDS[:4]`; `_BUILTIN_FOR_KIND` / `plan_for_depth` still
cut only those), then the five flyways, then the five presets. All are seeded,
editable and resettable (`reset_plan` / `delete_plan` → "reset"), like the
four. Every agent step keeps `join` on; every `test` and `sync` step and every
`checks` step has `on_fail: agent` (the failing names / the conflicting files
go to the agent, the stop retries).

- **Bot PR** (`builtin:botpr`, trigger pr): Agent reads it → Test → Sync with
  main → Checks → Merge. No perch. The agent's prompt (appended to the PR's
  own intake prompt) reads the bumped packages' changelogs for breaking
  changes and commits the smallest adaptation, or changes nothing.
- **Red main** (`builtin:redmain`, trigger `main_red`, `repo: ""`): Agent
  investigates → Test → Commit → Push → Open PR → Perch "Look at the fix". An
  empty `repo` loads leniently and `flight_triggers.watching_plans` skips a
  plan with no repo, so the seed is inert: **the owner picks the repo in the
  inspector** (a save is strict, so it must name one: `trigger.repo` "a
  repository to watch is needed").
- **Nightly chores** (`builtin:nightly`, trigger schedule `{at: "03:00",
  days: "daily"}`, **`paused: true`**): Agent updates deps → Test → Commit →
  Push → Open PR. Seeded paused (a deviation from the contract's plain
  "schedule 03:00"): an armed seed would try to take off on every install,
  every night, before anyone set its folder. It shows under Scheduled flyways
  as paused; switching the trigger on arms it.
- **Release** (`builtin:release`, manual): Agent writes the notes → Commit →
  Push → Open PR → Checks → Perch "Approve the release" → Merge → Agent tags
  and publishes → Land (last, as `land` requires).
- **Bug report** (`builtin:bugreport`, trigger ticket): Agent "Write a failing
  test that reproduces the report" → Test `expect: fail` titled "Write the
  failing test first" → Agent "Make it pass" → Test → Commit → Push → Verify
  (this branch · agent only · right away) → Open PR → Reviews settle → Checks → Perch "Approve the merge" →
  Merge → Ticket: comment ("Fixed by MindFlock"; set `state` to move it). The
  contract listed the expect-fail Test first; an agent step to write the test
  comes before it, since a Test stop runs tests, it does not write them.
- **Presets** (`builtin:preset-docs` "Update the docs", `-tests` "Write
  tests", `-security` "Security pass", `-selfreview` "Self-review",
  `-explain` "Explain the diff"): trigger manual, one agent step titled like
  the preset, a 3–5 sentence prompt naming `{branch}` (the diff of the branch
  against the default branch). Dropped onto a line they are `op_add_plan`
  groups of one stop. The prompts live in module constants (`_BOT_PR_PROMPT`
  … `_PRESETS`) above `_builtin_defaults`.

**Receipts** (`detail.receipt`, ≤ `MAX_RECEIPT` = 80 chars, cut by
`flights._receipt`) on the `done` of every built-in kind:

| kind | receipt | where |
|---|---|---|
| ship | `PR #291` (lane pr: the number from the record's url, `/pull/N` or `/merge_requests/N`; "PR opened" with none) / `merged` / `pushed` / `committed` | `_judge_ship` via `ship_receipt(lane, url)` |
| checks | `all green`; `no checks` when none reported after the grace | `_judge_checks` |
| reviews | `2 rounds, quiet` / `1 round, quiet` / `no rounds, quiet` (`detail.review_rounds`) | `_judge_reviews` |
| command | `exit 0` | `_judge_command` |
| agent | `settled after 12 min` / `settled after 2 h 5 min` (since the step started, ≥ 1 min) | `_judge_agent` (both done paths) |
| join | `2 of 3 done` (workers whose entry is `done`, of all); `no workers` | `_judge_join` |
| wait | `10 min` / `until 18:00` (from the config) | `_judge_wait` |
| verify | `3 of 4`: answered items of all; the driver's `_verify_obs` now also returns `answered` (steps with a result in the latest run); an observation without it counts what is not open | `_judge_verify` |
| split | `3 workers` | `flight_driver._run_split` |
| ticket | `→ Done` (the step's `state` as typed) / `commented` / `nothing to do` | `flight_driver._perform` via `ticket_receipt(config)` |
| notify | `sent` | `flight_driver._perform` |

`plural(n, word)`, `ship_receipt` and `ticket_receipt` are module-level
helpers in `flights.py`. The step-state shape is unchanged otherwise (the
receipt is one more key in `detail`). `you` stops get none (a person pressed
Done; the `someone` plug-in sets its own).

**Outside this task.** `tests/unit/test_flights_schedule.py::
test_rows_are_the_scheduled_plans_in_template_shape` pins the scheduled rows
to `{builtin:schedule, <its plan>}`; with Nightly chores seeded it also lists
`builtin:nightly` (paused). That file was outside this task's fence: its
expected set needs `"builtin:nightly"` added. The frontend shows the new
built-ins as it shows any plan; no frontend change was made here.

Tests: `tests/unit/test_flights_builtins.py` (order, shapes, the depth cut,
strict validity, reset, the red-main repo gate, nightly paused, presets),
`tests/unit/test_flights_receipts.py` (every receipt above, judges and
executors); `test_flights.py`'s two count pins now read `len(BUILTIN_IDS)`.

## Round 16: the Loop block (contract)

The owner: "In sky mode I want the ability to loop an action or prompt or
cycle. I should be able to pick the number of times it loops, how often it
loops, if it waits for limits, if it clears context on each run. I am trying
to do an iterative sky UI improvement and I need this feature before that."

A **Loop** is a stop like any other (plug-in kind `loop`, family `work`,
robot, `default_on_fail: halt`), placed AFTER the stops it repeats. When the
flight reaches it, it sends the flight back to an earlier stop — N times, at
most every so often, waiting out usage limits, clearing the agent's context
first when asked — and lets the flight carry on past it when the count (or a
condition) is met. A Loop at the end of a line with "Repeat from: the first
stop" is a cycle of the whole flight; a Loop right after one Agent stop
repeats that prompt.

### Backend (`backend/web/core/flight_kinds/loop.py` + core)

Config (`spec.fields` drive the dialog):

| field | type | meaning |
|---|---|---|
| `back_to` | select, `dynamic: "stops_before"` (the UI fills the options with the line's stops before the loop; `""` = the first stop of the flight, or of the loop's own group when it has one) | the stop the flight returns to |
| `times` | int, default 3, 0 = until stopped, ≤ 1000 | how many runs in all (the first pass counts as run 1; a Loop with `times: 3` sends the flight back twice) |
| `every_minutes` | int, default 0, ≤ 7 days | the least time between the starts of two runs (0 = straight away) |
| `wait_limits` | bool, default true | while the session is usage-limited (`obs["session"]["limited"]`), wait instead of starting the next run — the wait is not a failure and does not count |
| `fresh` | bool, default false | clear the agent's context before each run: the provider's `clear_context_command()` (new on `providers/base.py`, default `None` = nothing to send; Claude `"/clear"`, Codex `"/new"`) delivered into the session's queue before the jump back |
| `until` | text, optional, a `when` expression (round 15's grammar; strict mode validates it) | stop looping when it is true, whatever `times` says |

Behaviour (`judge` is pure; `run` does the side effects):

- `detail.runs` = runs completed so far (0 on the first arrival), `detail.last_start` = when the current run began (set when the flight jumps back; the first run's start is the flight's `started` or the `back_to` step's first `since`).
- On arrival: if `until` is true, or `times > 0 and runs + 1 >= times` → `done`, receipt `"looped 3 times"` (or `"stopped by: <until>"`), note the same. Else if `wait_limits` and the session is limited → `wait`, note `"waiting on the usage limit"`. Else if `every_minutes` and `now - last_start < every_minutes*60` → `wait`, note `"next run in 12 min"`. Else → `run`.
- `run`: when `fresh`, deliver the provider's clear command (skip quietly when none). Then the core's **`loop_back(flight, loop_sid, to_index, now)`** (new in flights.py): `runs += 1`, `last_start = now` on the loop step's detail, every step from `to_index` to the loop step (exclusive) reset to pending (`_blank_step_state`; their configs untouched; a halt point among them is armed again), the loop step itself stays `running` with its detail, the cursor moves to `to_index`, history `looped` ("run 2 of 3 — back to <title>"; add `looped` to HISTORY_EVENTS), `_let_through` as a jump does. `times: 0` loops until the owner removes the Loop, skips it, or halts.
- A Loop whose `back_to` is not before it (or names a missing stop) fails with `"nothing to repeat"` (on_fail halt).
- Removing a Loop mid-flight (the current step) is the usual "stop removed" pause; Skip on it = carry on past it.
- Tests: `tests/unit/test_flights_loop.py` (judge decisions, loop_back resets, fresh delivery, limits, until, the receipt, the route round-trip through `POST /start {steps}` + the driver pass); the kinds-list pins in the other tests are updated for the new kind.

### Frontend

- The Loop box: icon ↻ (Bird.tsx's `StopIcon`), title from `default_title`: `"Loop ×3"`, `"Loop ×3 every 30 min"`, `"Loop until <until>"`, `"Loop forever"`. While it flies the box's second line is `"run 2 of 3 · next in 12 min"` / `"waiting on the limit"`; a flown Loop shows its receipt.
- **The return arc**: `flightDiagram` adds an edge `{kind: "loop", from: <loop node>, to: <back_to node>}` and FlywayCanvas draws it as an arc ABOVE the line from the loop box back to the target (an SVG path with an arrowhead, dashed while pending, solid while running, the run counter as a small pill on the arc), scaled with the boxes. Collapsed groups: the arc lands on the group box.
- The dialog: the kinds dto drives the fields; a field with `dynamic: "stops_before"` (new on `StepKindField`) gets its options from the line: every stop before the loop (in a group: its sub-steps first), labelled by title, `""` labelled "the first stop". `until` gets the `when` field's editor if round 15 made one, else a text field with the grammar hint.
- The shelf's default blocks gain **Loop** after Wait (`DEFAULT_KINDS`); the palette/catalog show it under Work.
- Tests: flightDiagram (the edge), flightsUi/skyBranches (the arc markup), stopDialog (dynamic options), stopCatalog (defaults).

### As built: round 16 loop backend

`backend/web/core/flight_kinds/loop.py` (kind `loop`) + the core in
`flights.py` (`loop_target`, `loop_back`, an `apply` branch for a `loop`
action, `"looped"` in `HISTORY_EVENTS`, `_f(..., dynamic=)`) + the provider
hook `clear_context_command()`. Tests: `tests/unit/test_flights_loop.py`
(38); the select-options pin in `test_flights.py` accepts a dynamic select.

**The dto row** (`GET /api/flights/kinds`): `{kind: "loop", label: "Loop",
actor: "robot", family: "work", risk: [], default_on_fail: "halt",
default_title: "Loop ×3", summary: "Send the flight back to an earlier stop,
N times, every so often"}`, fields in this order:

| name | type | default | notes |
|---|---|---|---|
| `back_to` | select | `""` | `options: []`, `dynamic: "stops_before"` (a new optional key on any field row, passed through `kinds_dto` unchanged); a stop id, `""` = the first stop (of the loop's group when it has one) |
| `times` | int | 3 | 0 = until stopped; strict: 0–1000 |
| `every_minutes` | int | 0 | strict: 0–10080 (7 days) |
| `wait_limits` | bool | true | |
| `fresh` | bool | false | |
| `until` | text | `""` | a `when` expression, stored canonical; placeholder `e.g. judge is yes (empty = by count)` |

Strict config errors name `steps[i].config.until` / `.back_to` (not an id
shape) / `.times` / `.every_minutes`. Lenient (a block on the ground) keeps a
half-typed `until` as typed; the takeoff check refuses it, and a stored one
the judge cannot parse fails the stop `"until: <why>"`. Titles: `Loop ×3`,
`Loop ×3 every 30 min`, `Loop ×5 every 2 h`, `Loop until <until>`, `Loop
forever`, `Loop every 15 min` (times 0 + an interval).

**Detail keys** on the loop step: `runs` (runs completed; 0 on arrival, +1
per jump back), `last_start` (epoch s, when the current run began; run 1's
is the target stop's `since`, else the flight's `started`), `target` (the
stop id the last/next jump goes to), `next_at` (epoch s of the next run while
waiting on the interval; dropped while waiting on the limit and on a jump),
`receipt` on done (`"looped 3 times"` / `"looped 1 time"` / `"stopped by:
<until>"`). The step's `note` while it flies: `"run 2 of 3 — back to
<title>"` (`"run 502 — back to …"` for times 0), `"next run in 12 min"`,
`"waiting on the usage limit"`, `"checking <until>"`.

**History**: one `looped` event per jump back, on the loop step, note `"run 2
of 3 — back to <title>"`; the steps it resets get no events of their own
(their `started` / `done` follow as they fly again).

**How it flows** (why a new action): a plug-in's `run` gets a COPY of the
flight (the driver records outcomes through `_record_outcome` → `apply`), so
the jump cannot happen inside `run`. First arrival: the core's generic `run`
starts the step and `loop.run` records `runs: 0` + `last_start`; later passes
ask the judge, whose repeat is `{"action": "run", "phase": "loop"}` (a phase,
so `_mark_intent` does not restart the step and wipe its detail); `loop.run`
then delivers the clear command when `fresh` and returns `{"action": "loop",
"to": <stop id>}`, which `apply` hands to `loop_back` (re-resolving the
target; a missing one halts with "nothing to repeat"). The loop step stays
`running`, so the judge is asked again when `_advance` reaches it.

**Edge cases**: a Loop first in line (or first in its group with `""`), a
`back_to` after it or missing → fails `"nothing to repeat"` (halt). `times:
1` is done on arrival (never jumps). `until` wins over `times` and is checked
first; its facts come from the loop's own `observe` (`flight_driver.
when_facts` with the loop as the step, so `judge is …` reads judges before
the loop). `wait_limits` reads `obs["session"]["limited"]` or, since the
driver reads a non-agent row for a loop, `obs["loop"]["limited"]` (observe
reads the agent row only then). A limit wait does not count a run. A halt
point inside the repeated stops is armed again; the target itself is let
through, as a jump onto it is. Skip on the current Loop carries on past it,
Remove is the usual `stop removed` pause, a backward jump onto an earlier
stop resets the loop (`runs` from 0) — no special casing. Nested loops: an
inner Loop inside the outer's run is reset with it, so it counts afresh each
outer run. `fresh` with a provider whose `clear_context_command()` is None
sends nothing. Providers: `BaseProvider` None, Claude `"/clear"`, Codex
`"/new"` (checked against the Codex 0.164 TUI's slash list: "start a new chat
during a conversation"; its `/clear` also wipes the terminal). The command
goes through `_deliver` (the session's queue) before the jump, so it lands
ahead of the repeated Agent stop's prompt.

An attached `POST /api/flights/start {title, steps: [agent, loop]}` puts the
cursor on the Loop (the session is already doing the Agent stop), so a
`times: 2, fresh: true` flight types `/clear` then the prompt once.
### As built: round 16 loop UI

The `loop-ui` task of round 16 (2026-10-31), built against the contract's
dto (mocked in tests; the backend landed in parallel). Files:
`lib/flightDiagram.ts`, `lib/flights.ts`, `lib/stopCatalog.ts`,
`FlywayCanvas.tsx/.css`, `Bird.tsx`, `StopParts.tsx`, `StopDialog.tsx/.css`,
`useSessionLine.tsx`, `useGroundedLine.tsx`. Tests: new
`flightLoop.test.ts` (23), plus cases in `flightDiagram`, `flightsUi`,
`skyBranches`, `stopDialog`, `stopCatalog`, `stopShelf`, `skyBirds`.

**The box.** `StopIcon` `loop` is ↻ (an open circle with its arrowhead, the
stroke style). `stopSummary` of a loop: "3 runs" / "until stopped" / "until
<until>", then "every 1 h 30 min" (`minutesWord`), "fresh context". A
`DiagramNode` of a loop carries `loop: LoopMark` = `{runs, times, state}`
(`loopMark(step, rec)`: `runs` = `detail.runs`, `times` = config (default 3,
0 = forever), state `pending` → `running` (record running / waiting, or any
run done) → `done` (done / skipped)). While the loop is at the cursor, or has
sent the flight back (`runs > 0`), its second line is `loopLine`: the
judge's note first — "waiting on the usage limit" → "waiting on the limit",
"next run in 12 min" → "run 2 of 3 · next in 12 min" — else the counter "run
2 of 3" / "run 2 — until <until>" / "run 5". **A Loop that sent the flight
back keeps its record `running`**, so `flightStops` draws it `pending` while
the cursor is before it (one bird, one current box: the stop the flight is
back at); it stays editable then (the server decides what a PATCH may change).

**The edge.** `loopEdges(line)` → `{kind: "loop", from: <loop node>, to:
<target>, loop: LoopMark}` for every main-row loop stop: `back_to` names a
stop BEFORE it, `""` is the line's first stop, or the first sub-step of the
loop's own group; missing / not before → no edge. `flywayDiagram`,
`flightDiagram` and `sessionDiagram` (only with the full flight — the
row-only fallback has no steps) add them after the line. `foldRun` now
spreads the edge (`{...e, from, to}`), so folding re-aims an arc at the box
that holds its target: a collapsed group → the group box, the flown pill →
the pill; a loop folded into the same box as its target draws nothing.
`reorderDiagram` keeps them (a drag's preview). `DiagramEdgeKind` gains
`"loop"`; `DiagramEdge.loop` / `Wire.loop` + `Wire.apex`.

**The arc.** `layoutDiagram` draws a loop edge with `loopArc`: horizontal, a
cubic from the loop box's top centre (`x + w/2, y`) up and back down onto the
target's top centre (`y - 2`, the arrowhead), its control points 4/3 ×
`loopRise(span)` above the higher box top so the apex sits the rise above
the line — `loopRise` = 18 + 0.16 × span, clamped 30–84 px; vertical (a
phone), out of the loop box's right edge, round the right of the column,
into the target's right edge (the layout's width grows to hold it).
`Layout.rise` = how far the arcs reach above y = 0 plus `LOOP_PILL_ROOM`
(14); the canvas's `top` = max(its old 26 / pad, `ceil(rise) + pad`).
Markup in the existing `.fl-wires` SVG: `<path class="fl-wire fl-wire-loop
fl-edge-loop fl-loop-<pending|running|done>" d="M … C …"
marker-end="url(#…-arrow)">` (key `from>to>loop`). CSS: pending dashed
(`5 4`, accent 50 %), running solid `--tone-working` 2.2px, done muted.
**The pill**: `<span class="fl-loop-pill fl-loop-<state>" style="transform:
translate(apex) translate(-50%,-50%) scale(calc(1 / var(--sky-z, 1)))"
title="Run 2 of 3" aria-label="Run 2 of 3">2 / 3</span>` on the stage —
`loopPill`: `runs+1 / times`, "2 / ∞" for forever, nothing while the loop is
still ahead and has never run; `loopPillTitle` ("Run 2 of 3", "Run 2 — it
loops until stopped", "Looped: run 3 of 3").

**The dialog.** `StepKindField.dynamic?: "stops_before"`. `stopsBefore(steps,
step)` (lib/flights): `{value: "", label: "the first stop"}` ("the first
stop of <group>" when the step is a sub-step), then every step before it in
flight order `{value: id, label: title, group?: name}`; a step not on the
line yet sees the whole line. `StopForm` takes `lineSteps` and hands a
dynamic field `choices`; `StopField` draws it as `StopsBeforeField`: chip
rows (`ChipRow`, `.fl-pick`; Flights never uses a native `<select>`) in
`.fl-stops-before[data-dynamic="stops_before"]`, a group's sub-steps in a
`.fl-stops-before-group` row under `.fl-stops-before-name`; a saved value no
longer before the loop shows as its own chip ("a stop no longer before this
one") until it is changed. A loop's `until` is `WhenField` (now taking
`label` / `placeholder` / `hint`; aria-label "Until", the server's
placeholder, the ⓘ grammar and the live check). `StopDialog.lineSteps` ←
`useSessionLine` (`flight?.steps`; also the pane's Flight tab) and
`useGroundedLine` (`flight.steps`); `StopInspector` passes it through too.

**The shelf.** `DEFAULT_KINDS` = agent, you, ship, verify, wait, **loop** —
shown once the server lists the kind; the palette groups it by the server's
`family`.

**Seen in the browser** (a scratch build + the playwright harness, a 6-stop
line back at stop 2 of run 2): the arc rises over Judge it / Perch from the
Loop box onto "Improve the sky" with its arrowhead, solid green, "2 / 3" on
the apex; the box reads "Loop ×3 every 30 min · run 2 of 3"; the bird sits
on Screenshot; the shelf shows Loop after Wait; the dialog lists the first
stop + the four stops before the loop as chips and Until as the mono when
field.
### As built: round 16 MCP flights

The owner: "I want the ability to use the MindFlock MCP to set up flights, so
if I describe a flight it should be able to set it up and run it." The
MindFlock MCP gains six tools in `backend/mcp/flights.py`, a `Toolbox` mixin
like `runs.py`. That takes it from 28 to 34 tools; peer mode keeps its own 10
and never sees these six. The full reference, with a worked example, is in
[mcp.md](mcp.md#routes-from-a-sentence-to-a-running-route).

| Tool | Route(s) |
|---|---|
| `describe_flight(text, want="plan"\|"step", kinds=true)` | `POST /api/flights/describe`, plus `GET /kinds` for the compact kinds list (`kind · label · summary · fields: …`, a "waits on a person" mark, `[risk]` tags) |
| `start_flight(steps \| plan_id, title=you, grounded, name, at, confirm_merge)` | `POST /api/flights/start` (`at` is sent as `start_at`; `by: "agent:<you>"`), then `PATCH /{id} {plan_name}` for `name` on a non-grounded flight |
| `flight_status(id \| title)` | `GET /api/flights/{id}`, or `GET /api/flights` filtered by `session` (the live flight first, else the latest) |
| `control_flight(id \| title, op, step, note)` | `/pause`, `/resume`, `/cancel`, `/steps/{sid}/skip`, `/retry`, `/done {note}`, `/jump {step}` |
| `edit_flight(id, add, at, remove, rename)` | `DELETE /steps/{sid}` per id, then `POST /steps {steps, at}`, then `PATCH /{id} {plan_name}` |
| `save_flight_plan(name, steps, description, trigger)` | `POST /api/flights/plans` |

Choices the contract left open:

- **No kind list in the MCP.** A step's `kind` is an open string in every
  schema. `describe_flight` returns whatever `GET /kinds` serves (the Loop
  included), and the server validates the steps (400 `bad_step` with
  `field`, passed through).
- **Scope = `ship_session`'s rule** (`Policy.require_ship`). The tools act on
  the caller or a session it manages, never on a team-run member. A stop that
  can't be undone needs `confirm_merge`: a `ship` stop at lane `merge`, or a
  kind whose `risk` includes `irreversible` (read from the kinds list, so a
  new irreversible kind is covered with no change here). Under `children`
  scope a session may not fly such a stop on itself, and the refusal suggests
  `grounded: true`, so the user attaches the session. A grounded flight needs
  only write scope. For `start_flight` with `plan_id`, the plan's steps are
  checked too.
- **An existing session starts at its first non-`agent` stop** (the server's
  rule for `title`). `at: "s1"` flies a leading agent stop, whose prompt is
  queued for when the session's turn ends. The MCP passes no
  `absorb_fasttrack`, so the server default applies: a running fast-track
  becomes the leading ship stops, and the tool reports them as `absorbed`.
- **describe never fails on a missing model.** On 409 `no_model` and 422
  `could_not_describe`, the tool returns `{steps: [], error, hint, kinds}`
  with the kinds list included, so the agent writes the stops itself.
- **flight_status stays compact** (`compact_flight`, about 2000 JSON
  characters at most). Each stop is one line: id, state, kind, title, when,
  halt point, group, note, and `✓ receipt`. History keeps the last 10 lines.
  Over budget, the output shrinks in this order: flown stops behind the
  cursor fold into a count, titles, notes and history shrink, then the stops
  far ahead fold.
- **The step ops default to the current stop** (`skip`, `retry`, `done`).
  `jump` needs `step`.
- **PATCH.** `backend.client` has no `patch()`, so `Api.request` sends PATCH
  through `client._request(..., method="PATCH")`, and `Api.patch` is new.
- **INSTRUCTIONS** gains `Flights: describe_flight → start_flight;
  flight_status; control_flight; edit_flight, save_flight_plan.`, still under
  2000 characters (1989) after small trims elsewhere.

Outside this change (files outside the worker's fence):

- `providers/mcp_attach.py` `AUTO_APPROVED_TOOLS` could pre-approve the two
  reads, `describe_flight` and `flight_status`. Until it does, both ask first
  in a permission-gated session.
- AGENTS.md and docs/architecture.md still say "28 tools".
- A public `client.patch()` would replace the private call.

Tests: `tests/unit/test_mcp_flights.py` covers each tool's request shape
against the in-memory API, the scope and confirm refusals, the kinds list in
`describe_flight`, `flight_status` compactness on a 40-stop flight, and
`Api.patch`. The 34-tool and INSTRUCTIONS pins are in `test_mcp_tools.py`.


### Redrawn: a Loop is its return wire, not a box (2026-11-02)

The owner: "why are loops their own block that doesn't actually loop around
to the bottom — I want to see the loop in the block diagram, not a block in
the chain". The data model and the backend are unchanged (a Loop is still a
`loop` step in `steps`, and its mechanics are round 16's); only the drawing
moved:

- **No Loop box.** `liftLoops(line)` (lib/flightDiagram) takes every Loop
  that has something to repeat off the line — the line runs straight past it
  — and gives back its `loop` edge from the LAST stop it repeats (the last
  non-Loop stop before it) to its `back_to` box, carrying the Loop node as
  `edge.via` (in the flight's indexes). A Loop with nothing before it, or whose
  target is gone, stays a box. flywayDiagram, flightDiagram and sessionDiagram
  all lift; `groundedDiagram` maps `via` the way it maps the boxes.
- **Under the line.** `loopArc` draws a flowchart loop-back: down out of the
  bottom of the last repeated box (`LOOP_FROM` 0.72 of its width), back along
  under every box in its span (branches and children too) `LOOP_DROP` px below
  the lowest, and up into the bottom of the first (`LOOP_TO` 0.28), arrowhead
  on it, rounded corners. A one-stop loop is a U under that stop. `Layout.height`
  grows for it (`rise` stays 0); the phone's vertical wire still goes round
  the right of the column. The cycle band is gone (the wire says it).
- **The pill is the Loop.** Always shown on the middle of the run under the
  line: "↻ ×3" / "↻ until <until>" / "↻ until stopped" before it runs
  (`loopIdlePill`), "2 / 3" while it goes round. It is a button: a click or
  Enter opens the Loop's dialog (`edit`), right-click its menu, Delete removes it.
- **Folding.** `foldRun` keeps a loop edge whose ends fold into one box (a
  Loop inside a collapsed group, a finished loop inside the flown pill): it
  goes round that box.
- **The bird** at a Loop (deciding, waiting out its pace or a limit, halted
  on it — `loopAtWork`) stands on the stop its wire leaves from; `lineWhere`
  names the Loop ("at Loop ×3 · run 2 of 3 · next in 12 min");
  `currentNodeId` reveals that stop.
- Drops and moves count drawn places (`drawnCanvas`): a stop dropped in the gap
  after the wire's last stop lands AFTER the Loop (outside it).

**Drawing loops by hand (same day).**
- Every stop that can take a Loop after it wears a small ↻ on its bottom-right
  corner (hover / selected / focused). Drag it back onto a stop at or before
  it (itself = loop just that stop): `onLoopFrom(from, to)` tacks a Loop right
  after `from` (`loopSlotAfter`) with `back_to` = `to`'s first step
  (`loopStepTo`, `firstStepOf`). The wire follows the pointer and snaps onto
  the stop under it (`.fl-loop-target`, `loopWirePath`); Esc or a drop on
  nothing cancels.
- A Loop's pill dragged onto another stop at or before the wire's start
  re-aims it: `onLoopTo(loop, to)` PATCHes `config.back_to`. A press that
  never moves still opens the dialog.
- In the Sky, a session line's trailing + dragged onto a stops-only line does
  what dragging that line onto the session does (`mergeGrounded`): a wire
  follows the pointer (`.sky-connect`), the line under it lights.
- Moved stops keep their Loop's target: `POST /steps {steps}` now carries the
  steps' old ids and `_follow_loop_targets` re-aims a Loop whose `back_to`
  names one of the batch at its new id (also for a saved stop tacked on,
  `op_add_plan`). Before this, a moved Loop pointed at whatever had the old id.

## Round 17: the Sky as a canvas tool (contract)

The owner (2026-10-31, a standing ask, one round per cycle): "iterate on the
sky design and user experience to make it as seamless and intuitive as
possible; look to other apps that exist for inspiration; take the best parts
of all of them; make sure you have a judge agent." This round takes the
manners every good infinite canvas has settled on and a fresh-eyes judge
measures the Sky against them before and after.

### The inspiration digest (what the best canvases agree on)

| From | The manner | Sky today | This round |
|---|---|---|---|
| Figma / FigJam | Space+drag pans; wheel zooms about the cursor; `Shift+1` fit all, `Shift+2` fit selection, `Shift+0` 100 %; the zoom readout is a MENU of presets; smart guides snap edges and centres while dragging; a selected thing wears one thin accent outline, hover a fainter one | pan/zoom yes; 0 / Fit / 100 % buttons; no selection fit; no guides | zoom menu, fit selection, snapping guides, hover/selected rings |
| tldraw | the select tool is the default and a drag on empty space marquees; `H` hand; the context toolbar floats ABOVE the selection; smooth eased camera moves; a `?` sheet of shortcuts | click/drag tools (V/H), selection bar at the foot | eased fit/focus, `?` sheet |
| Miro | a minimap in the corner with the viewport rectangle; frames list to jump; zoom presets | none | minimap + off-screen hints |
| n8n | `Tab` opens the node panel with its search focused; double-click on empty canvas opens it at the pointer; "Tidy up"; sticky notes; a node shows its run status inline | palette float/dock; Tidy; notes; status on boxes | Tab / double-click to the palette |
| Zapier | each step's status chip reads at a glance; the path's label on the edge | receipts, states | (judge checks legibility) |
| Excalidraw | Delete removes, Ctrl+Z / Ctrl+Shift+Z undo / redo; zen mode hides chrome; cursors say what a drag will do (grab, grabbing, crosshair, move) | undo only; cursors partial | redo, cursor contract, focus mode |
| Linear | keyboard first; one accent colour; calm density; the `?` overlay | — | `?` sheet, density pass |
| Apple Freeform | touch: pinch, two-finger pan, long-press menu; big targets | pinch/pan | ≥ 40 px targets on phone (judge checks) |

Principles kept from rounds 13–15: nothing lands by itself, direct
manipulation, every object has a name and can go, selection may be empty,
palettes dock or float, geometry never depends on zoom.

### The judge (task `judge`, read-only, runs first and again after the builders)

A fresh-eyes judge session SEES the Sky, never reads its source. It builds a
screenshot harness in its scratch dir (the recipe in the brief at
`~/MindFlock/ux-lab/JUDGE_BRIEF_R17.md`: the built bundle served statically +
the demo mocks + its own `/api/flights/*` and `/api/instances` rows), shoots
six states (first visit with nothing; three lines; twelve lines; a flight
mid-way with a perch waiting on you; zoomed far; a 390 px phone), tries the
gestures (wheel, Space+drag, marquee, Tab, `?`, double-click, Delete,
Ctrl+Z), and scores the Sky 1–5 on: first-five-minutes clarity,
discoverability, feedback (does every action show its effect?), consistency,
speed/smoothness, forgiveness (undo, confirm only when it must), delight.
Its report (`JUDGE-R17-<n>.md` in `~/MindFlock/ux-lab/round17/`, with the
PNGs beside it) lists at most twelve findings, each: what I saw / what the
best apps do / the fix in one sentence / severity. The second pass re-shoots
the same six states on the rebuilt bundle and says what changed and what is
still wrong; its leftovers seed the next cycle.

### The builders (disjoint files; the seams are the three placeholder components and `lib/skySnap.ts`, committed with this contract)

**`sky-core`** (`SkyView.tsx`, `Sky.css`, `lib/skyLayout.ts`, `lib/skySnap.ts`,
`lib/skyUndo.ts`, `lib/skySelect.ts`, `lib/viewMode.ts`, tests
`skySnap.test.ts`, `skyCanvas.test.ts`, existing sky*.test.ts updates):
(1) **snapping**: `snapBox(moving, others, threshold)` snaps a dragged line or
object's left/centre/right and top/middle/bottom to the others' within 8
screen px (threshold = 8 / zoom), returns the guides; the sky draws them as
1 px accent lines (`.sky-guide`) while the drag lasts; `Alt` held = no snap;
(2) **fit selection**: `Shift+2` / the zoom menu's "Fit selection" fits the
selected boxes (lines and objects), `Shift+1` fits all (0 stays), `Shift+0` =
100 %; (3) **eased camera**: fit / focus / jump animate the camera over
~220 ms (ease-out; none under `prefers-reduced-motion`), no FLIP ghosts
involved; (4) **redo**: `lib/skyUndo` gains a redo stack (`popRedo`,
`redoCount`; an undo pushes its inverse when the entry provides `redo()`,
else nothing — the kinds that can: stop/group removal, grounded line delete,
note delete), `Ctrl+Shift+Z` / `⌘⇧Z` redoes, toast "Redid: …"; (5) **Tab**
opens the palette floating with its filter focused (`onFocusFilter`), a
second Tab docks it; **double-click on empty sky** opens it at the pointer
(placing lands there) — a double-click on a line keeps its meaning; (6) the
**cursor contract** (`Sky.css`): `grab` on a bar, `grabbing` while dragging,
`crosshair` for the marquee, `move` for a snapping box, `zoom-in`/`zoom-out`
never (the wheel does it), `copy` while a chip drags over a line; (7)
**focus mode**: `F` with a selection dims everything else to 35 % (a `.sky-
focus` class on the plane, `data-dim` on the rest), any click or Esc lifts
it; (8) mounts `<Minimap>`, `<OffscreenHints>`, `<ZoomMenu>` (replacing the
`sky-zoom-read` span) and `<SkyShortcuts>` (`?` opens, Esc closes) with the
seam props, feeding the rects from `rectOf` + the objects' frames and the
lines' state tones; passes `groups` = the sky's real shortcut list.

**`minimap`** (`Minimap.tsx` + `Minimap.css`, `lib/minimap.ts`, tests
`minimap.test.ts`): the map in the sky's bottom-right (above the shelf,
right of the selection bar), 180×120 px, folded to a ◰ glyph by a click on
its head and remembered (`mf_sky_minimap`); draws every rect as a rounded
block in its state tone (lines), a muted block (objects), the intake lane
and landing strip as thin bars; the viewport as an accent rectangle that
DRAGS (plane = map × scale) and a click outside it centres the camera there;
a click on a block → `onJump(id)`; hover names the line; a plane bigger than
the map is scaled to fit with 8 px padding; hidden under 520 px wide.
**Off-screen hints**: for each line outside the viewport, a 22 px chip at
the nearest edge (clamped to the edge, 8 px in), the bird-coloured dot of its
state + the title (ellipsis at 14 chars), click = `onJump`; at most 8, the
nearest first; none while the minimap is open AND wide enough — no, keep
both: the hints are for "where did my line go" at a glance, the map for the
whole picture; hidden at lod far (everything is on screen then anyway).

**`zoom-shortcuts`** (`ZoomMenu.tsx` + `ZoomMenu.css`, `SkyShortcuts.tsx` +
`SkyShortcuts.css`, `lib/skyShortcuts.ts`, tests `zoomMenu.test.ts`,
`skyShortcuts.test.ts`): the readout becomes a button opening a small menu
(inline DOM, Esc/outside closes, arrow keys walk it): Fit all (Shift+1 / 0),
Fit selection (Shift+2, disabled without one), Fit the focused line (1),
then 50 % · 100 % (Shift+0) · 150 % · 200 %, each with its key; the current
zoom ticked. The shortcuts sheet: a centred card (not a native dialog)
listing the groups in two columns with key caps (`<kbd>`), a filter field at
the top, `?` or Esc closes, the sky's real list in `lib/skyShortcuts.ts`
(`SKY_SHORTCUT_GROUPS`: Move around / Select / Edit stops / Lines and
objects / The palette / Help) — `sky-core` imports it for `groups`.

**`polish-css`** (`SkyLine.css`, `FlywayCanvas.css`, `GroundedLine.css`,
`IntakeLane.css`, `LandingStrip.css`, `SkyObjects.css`, `StickyNote.css`,
`SkySelection.css`, `SkyPalette.css`, `StopShelf.css`; tests: a
`skyPolish.test.ts` that pins the tokens): one accent ring for selection
(2 px, `--accent`, 2 px offset), a fainter hover ring (1 px, 50 %) on every
box, line bar and object head; `:focus-visible` matches hover + the ring;
transitions 120 ms on transform/box-shadow/outline only (never on layout);
one shadow scale (resting / raised while dragging); radii unified (lines 10,
boxes 8, chips 999); touch targets ≥ 40 px for every control on `@media
(hover: none)`; the drop gap's highlight reads at far too; no colour that
is not a token. Nothing moves; nothing changes size at rest.

### After the builders

The integrator merges, rebuilds the bundle in the round's worktree, the
judge re-shoots, the integrator fixes what is quick, merges into
`emandel2630/flights`, deploys. The judge's leftovers are the next cycle's
seed, written at the top of the next round's contract.

### As built: round 17 minimap

`components/flights/Minimap.tsx` fills both seams; the maths is
`lib/minimap.ts`, the look `Minimap.css`, the tests `minimap.test.ts`. The
props are the seam's, unchanged.

**The map** (`.sky-minimap`, absolute; the sky places it, the CSS defaults to
`right: 12px; bottom: 12px` until it does) is a 22 px head row ("Map" + the
◰ glyph) over a 180 × 120 body. Folded, only the glyph shows (`.is-folded`,
30 px; 40 px on `hover: none`). `open` / `onToggle` belong to the sky, and so
does storing the fold: `lib/minimap` exports `MINIMAP_KEY = "mf_sky_minimap"`
and `minimapOpen(pref)` (open unless `"0"`) for it. `mapScale(rects, map,
pad)` fits the content's bounds into the body with 8 px of padding, centred,
one scale for both axes, never over 1:1. The map is drawn from the content
alone, not content ∪ viewport, so the scale holds still while the viewport
rectangle is dragged; a viewport off the content is clipped at the map's
edge. `mapBlocks` draws:

- lines (`line`, `grounded`) as rounded blocks in their state tone;
- objects (palette, note, person, device) as muted blocks;
- `intake` and `landing` as a 3 px bar along their long side.

Every block is at least 3 px. Objects are drawn first, then lines, so a line
wins a shared pixel and a click. Hovering a block shows its `title`: the
label, or the kind. `selected` and `focused` add an accent or a text-coloured
outline.

**Tones** (`toneVar`) follow the birds: `working` is `--tone-working`;
`asking`, `waiting_you` and `you` are `--tone-attention`; `waiting` is
`--tone-waiting`; `halted`, `broken` and `failed` are `--tone-broken`; `idle`
and `done` are `--tone-neutral`; anything else is `--tone-muted`. The colour
reaches the CSS as an inline `--mm-tone`.

**Pointer**: one handler set on the map body, with pointer capture.

- A press that moves more than 3 px is a drag. It pans by `dragCamera`
  (Δmap ÷ scale × z, same zoom), grabbing wherever you pressed: the viewport
  rectangle or empty map.
- A press that doesn't move is a click. On a block it calls `onJump(id)`;
  anywhere else it calls `onCamera(centreAt(point))`, same zoom.

Both components are **hookless**: a press's start lives in a module
`WeakMap` keyed by the map element. The suite has no jsdom, so the tests call
`Minimap(props)` and drive `onPointerDown/Move/Up` with plain event objects.
The root carries `data-no-drag`, so SkyView's `SKY_CONTROLS` reads a press on
it as a control, not a marquee or a pan. A double-click on the map stops
there. Below 520 px wide, `@media` hides the map.

**Off-screen hints** (`.sky-offscreen`, inset 0, `pointer-events: none`
except on the chips) cover lines only. `edgeHints(rects, cam, viewport, max)`
takes each rect wholly off screen and pins its chip 8 px in from the edge it
lies beyond (past a corner, the edge on the axis where it lies further
out). The chip sits at the rect's middle along that edge, clamped so it stays
whole (a top or bottom chip assumes the 140 px max width). It keeps the
nearest 8, sorted by distance from the viewport, and pushes chips on one edge
apart so none covers another. A chip is a 22 px pill:

- an arrow for the side the line lies on, the state dot, and the title cut at
  14 characters (`shortLabel`);
- `title` / `aria-label` "Go to <name>";
- a click calls `onJump`.

There are no hints at lod far.

**Tiers**: `flightsButtons.test.ts` requires every flights button to carry a
tier class. The glyph wears `fl-icon-btn` (quiet) and the chip `fl-pick`
(chip). The minimap's own rules are scoped (`.sky-minimap .sky-minimap-glyph`,
`.sky-offscreen .sky-offscreen-chip`), so they win over the tier's geometry
whatever order the stylesheets load in. The chip resets `.fl-pick`'s 28 px
`min-height` to 0. Under `prefers-reduced-motion` there are no transitions.

For `sky-core`, when mounting: feed `rects` with `tone` = the line's state
tone and `label` = its title; persist the fold under `MINIMAP_KEY`. The wheel
over the map still reaches the sky's native wheel listener, which zooms about
the pointer; `.sky-own-scroll` on the map would silence that if the judge
finds it odd.
### As built: round 17 zoom-shortcuts

The two seams filled in, their props unchanged; `sky-core` mounts them.

**`ZoomMenu`** (`ZoomMenu.tsx` + `ZoomMenu.css`). The readout is now a
button (`sky-zoom-btn sky-zoom-read sky-zoom-menu-btn`, so it wears the zoom
group's secondary tier and keeps the readout's muted tabular look) reading
`87 %` with a small ▾, `aria-haspopup="menu"` / `aria-expanded`. A click, or
↓ / ↑ on it, opens an inline `role="menu"` (never a native select), portalled
to `<body>` at fixed coordinates under the button with right edges aligned
(above it when the window is short): the zoom group clips (`overflow:
hidden`) and the toolbar must not grow. Rows, each a `menuitem` with its caps
on the right (`aria-keyshortcuts` too): **Fit all** (Shift 1 / 0), **Fit
selection** (Shift 2, disabled when `!canFitSelection`), **Fit the focused
line** (1, disabled when `!canFitFocused`), a separator, then **50 % · 100 %
(Shift 0) · 150 % · 200 %**: a preset outside `min..max` is disabled, the one
the zoom sits on is ticked ✓ with `aria-current` (`presetAt`: the nearest
preset within 2 percentage points, none in between). Picking a row calls its
prop and closes, the focus back on the button; ↑ / ↓ wrap over the enabled
rows, Home / End jump, Esc or Tab close (Esc listened in the capture phase
and stopped, so the sky's own Esc doesn't also clear a selection), a press
outside, a resize or the window losing focus close. Opening focuses the
ticked row, else the first. The rows are the menu-row tier (`fl-stopmenu-
item`, StopMenu.css): `flightsButtons.test.ts` pins every flights button to a
tier, so ZoomMenu.css only lays a row out (tick · label · caps) and draws the
caps. Exported for the tests (no DOM in the node environment): `ZOOM_PRESETS`,
`zoomLabel`, `presetAt`, `zoomMenuItems(props)`, `zoomMenuKey(key, at,
live)` and `ZoomMenuList` (the open rows without positioning).

**`SkyShortcuts`** (`SkyShortcuts.tsx` + `SkyShortcuts.css`). A centred card
(`role="dialog"`, `aria-modal`, `aria-label="Keyboard shortcuts"`, not a
native `<dialog>`) over a fixed dimmed backdrop, portalled to `<body>`; a
press on the backdrop closes. The head says "? or Esc to close" beside a ×
(the quiet tier, `fl-icon-btn`); under it the filter, then the groups in two
balanced CSS columns (one under 640 px) inside a scrolling body, each row its
caps as `<kbd>` (a pointer word — drag, click, double-click, right-click,
Wheel — drawn as a dashed, muted "gesture" cap) and the words. While open a
window keydown listener in the capture phase owns the keys: Esc closes, `?`
closes while the filter is empty (with text in it, `?` is a character to
search for), Tab cycles inside the card, a character typed with the focus on
the × goes to the filter, and nothing reaches the sky's keys underneath (T,
V, 0… typed in the filter never tidy or fit). On open the filter is cleared
and focused; on close the focus returns to where it was. The sheet applies
`withPlatform(groups, isMacLike())` itself, so the sky may pass
`SKY_SHORTCUT_GROUPS` as is. Exported for the tests: `sheetKey(key,
filter)` and `SkyShortcutsCard` (the card alone).

**`lib/skyShortcuts.ts`**: `SKY_SHORTCUT_GROUPS`, read off SkyView's key
handlers and the round 12–17 sections, in six groups — Move around (Space
drag, Wheel, Ctrl Wheel, Arrows, + / −, Shift 1 and 0, Shift 2, 1, Shift 0,
H), Select (V, drag, Shift drag, Shift click, Ctrl A, F, Esc), Edit stops
(double-click, Enter, right-click, Delete, Backspace, Ctrl Z, Ctrl Shift Z,
Esc), Lines and objects (drag a bar, Alt drag = no snap, T, Ctrl N, Ctrl Y),
The palette (Tab, double-click on empty sky, click, drag onto a line, Esc),
Help (?, Esc). Each key is one cap; a key's row says what it does, not how
it is wired. `withPlatform(groups, isMac)` maps Ctrl → ⌘ and Alt → ⌥ (the
keys the sky reads as `metaKey` / `altKey` there), never mutates, and is
idempotent. `filterShortcuts(groups, query)` keeps the rows whose group,
caps or words hold every word of the query, case-folded, "+" splitting
words ("ctrl+z" = "ctrl z"); a one-character word is a KEY and matches a cap
only ("ctrl z" finds Undo and Redo, not Ctrl Wheel's "zoom"); caps answer to
aliases (⌘ to ctrl / cmd, ⌥ to alt / option, − to minus, ? to help, Esc to
escape); emptied groups drop out, and the sheet says "No shortcut matches
“…”." when nothing is left.

Open for `sky-core`: the rows name the keys the contract adds (Shift 1 / 2 /
0, Ctrl Shift Z, Tab, double-click on empty sky to the palette, F, ?, Alt to
skip snapping); a key that lands differently should change its row here.
Note that Shift 1 arrives as `e.key === "!"` (and Shift 2 as "@", Shift 0 as
")") on a US layout: match `e.code` (`Digit1`) with `shiftKey`.

Tests: `zoomMenu.test.ts` (the closed button, the rows and their order, the
caps, greying, the tick, each row's callback, the menu keys, the stylesheet)
and `skyShortcuts.test.ts` (the six groups and the keys SkyView answers to,
`withPlatform`, the filter by words and by keys, the card's markup, closed =
nothing, `sheetKey`, the capture listener and focus wiring, the two columns).
### As built: round 17 polish-css

CSS only, no component changed. Tests: `frontend/src/__tests__/skyPolish.test.ts`
(13 tests, the sheets read as text).

**One ring, one outline.** Every ringed thing wears it at rest, transparent:
`outline: calc(1px / var(--sky-z, 1)) solid transparent; outline-offset:
calc(2px / var(--sky-z, 1))`. That covers `.fl-box`, `.skl` (a session's line
and a grounded one), `.sky-object` (people/device blocks, notes, the landing
strip; radius 8px so it hugs the card) and the intake lane
(`.sky .sky-intake > .sky-lane`). So hover, focus and selection change only
the colour and width. The ring eases in, and nothing moves or grows. Every
width is divided by `--sky-z`, so the ring is 2 (or 1) **screen** px at any
zoom. At 40 % a plane-px ring would be under one pixel on screen.

| State | The ring | Where |
|---|---|---|
| rest | 1px, transparent | each element's own rule |
| hover | 1px, `color-mix(accent 50%)` | `.fl-box:where(:not(.fl-box-open):hover)`, the line's bar (`.skl:where(:has(> .skl-bar:hover))`), an object's head (`.sky-block-head` / `.sky-note-head` / `.sky-lane-head`), the lane's head. `:where` keeps it at zero weight, so the selection always wins. An open box is never restyled. |
| `:focus-visible` | 1px, the accent (the hover ring in the selection's colour) | `.fl-box`, `.skl`, `.skl.skl-grounded`, `.sky-object:where(:has(:focus-visible))`, a focused note (`.sky-object:has(> .sky-note:focus)`) |
| selected | **2px, the accent, 2px out, solid** | ONE rule in `SkySelection.css`: `.skl.is-selected`, `.skl.is-focused`, `.fl-box.selected`, `.fl-box.fl-box-selected`, `.sky-object.is-selected`, `.sky .sky-intake.is-selected > .sky-lane` (this beats Sky.css's dashed lane rule by weight). A lifted box (`.fl-box.fl-lifted`) wears it too. |

These replaced: the dashed selection outlines (lines 5px out, boxes 3px out),
the 3px `box-shadow` focus/selected rings on `.fl-box`, `.skl.is-focused` and
the grounded line, and the note's 2px box-shadow ring. The border-colour
cues stay (a selected line or box keeps its accent edge). The state rings
(`.fl-st-current` / `-perched` / `-halted` / `-paused`, `.fl-described`) are
still box-shadows, so a running box that is also selected shows both.

The `.skl:focus:not(:focus-visible) { outline: none }` rule became
`.skl:where(:focus:not(:focus-visible)) { outline-color: transparent }`. At
weight (0,3,0) it had hidden a selected line's ring as soon as a press on its
bar focused it.

**One shadow scale.** `--sky-shadow-rest` and `--sky-shadow-raised` are
defined once, in `FlywayCanvas.css` on `.sky, .fl-canvas`, with a softer pair
under `.light`. FlywayCanvas.css rather than SkyLine.css, because the pane's
Flight tab and the flyway editor draw boxes outside `.sky`. Rest is used on
`.skl`, `.fl-box`, `.sky-block` (it had none), `.sky-note` and `.sky-palette`,
and under the merge-target halos. Raised is used on
`.sky-line-slot.is-dragging > .skl`,
`.sky-object.is-dragging > :is(.sky-block, .sky-note, .sky-lane)`,
`.sky .sky-intake.is-dragging > .sky-lane`,
`.sky-palette-slot.is-dragging .sky-palette`, `.fl-lifted` (it lost its 45 %
accent halo for the ring) and the selection bar. Popovers and menus
(`.fl-tools`, `.sky-ground-menu`, StopParts' menus, `.fl-rewind`) and an
open box's cards are chrome, so they keep their own shadows.

**Radii.** Lines are 10px: `.skl` (was 12) and its corner handle; the floating
`.sky-palette` (was 12). Boxes are 8px: `.fl-box` (was 10), the sky's roomy
box (was 12) and branch (was 10), the grounded line's head box (was 10),
`.sky-block` (was 10), `.sky-note` and its head (was 6), and `.sky-lane-block`
(was 9). Chips are 999px: the shelf's `.sky-stop-chip` (was the button
tier's 6px). Kept on purpose: the perch's 18px (its shape says "waits on
you", and `flightsUi.test.ts` pins it) and the flown pill's 999px.

**Motion.** The new transitions are 120ms ease on transform, box-shadow,
outline-color and opacity only. `.fl-box` dropped its border-color leg and
its 0.15–0.18s timings, and the line's corner handle went from 0.15s to
120ms. No sheet in the round transitions a size. Under
`prefers-reduced-motion: reduce` they are off: `.fl-box` (as before), `.skl`,
`.skl-box-resize`, `.sky-object`, `.sky-block`, `.sky-note`, `.sky-palette`
and the lane.

**Touch.** In one `@media (hover: none)` block at the end of
FlywayCanvas.css, every Sky control gets a centred `::after` at least 40px
square (`min-width` / `min-height: 40px`, `translate(-50%, -50%)`). That
covers the ◧ size control, the card fold, the ⋯ summaries, `.fl-tool`,
`.fl-toggle`, `.fl-pick`, `.fl-icon-btn`, `.fl-palette-item`, `.sky .fl-btn`,
the lane's toggles and rail icon, the shelf's fold and chips, the selection
bar's buttons, and the absolutely placed handles (`.skl-box-resize`,
`.skl-resize`, `.sky-palette-resize`, `.fl-gap-add`, `.fl-bird-marker`).
Controls in flow get `position: relative`, while the absolutely placed ones
already are a containing block. Nothing grows: a bigger `min-height` would
have pushed the 42px line bar and the shelf.

The hit area is in plane px. The phone's list view has no camera, so it is
40 screen px there. A tablet's zoomed-out sky gets proportionally less, and
the counter-scaled controls (the gaps, the handles) keep their full 40.

**The drop.** The lit gap (`.fl-gap-add.fl-gap-lit`, "Tack on here") is now
a 2px accent ring over a 2px panel gap (`0 0 0 2px var(--panel), 0 0 0 4px
var(--accent)`); it was a 25 % halo. The pill is screen-sized, so this reads
at every band. At far, the end + that takes a flyway
(`.skl[data-lod="far"] .fl-box-add.fl-drop`) keeps a 2 screen-px ring
(`calc(2px / var(--sky-z, 1))`) and a 30 % wash (was 16 %).

**Open.** Two pins outside the fence still describe the old look and fail
until they are updated. `skySelect.test.ts:308-309` wants
`outline: 2px dashed var(--accent)` on `.skl.is-selected` and
`.fl-box.fl-box-selected`; it should pin the one rule's
`outline: calc(2px / var(--sky-z, 1)) solid var(--accent)` (skyPolish pins it
already). `skyVisibility.test.ts:428` wants `0 0 0 3px` in `.fl-box.selected`;
it should want `outline-color: var(--accent)`. Sky.css (sky-core's) still has
the dashed `.sky-intake.is-selected > .sky-lane` rule. It is dead weight now
that the heavier rule overrides it.

### As built: round 17 sky-core

The `sky-core` task of round 17 (2026-10-31). Files: `SkyView.tsx`,
`Sky.css`, `lib/skyLayout.ts`, `lib/skySnap.ts`, `lib/skyUndo.ts`. Tests:
new `skySnap.test.ts` (9: the pure `snapBox` and the sky's wiring) and
`skyCanvas.test.ts` (15: fit selection, the eased camera, the redo stack,
Tab / double-click, the cursor contract, focus mode, the seams mounted);
`sky*.test.ts` all pass (21 files, 444 tests). `lib/skySelect.ts` and
`lib/viewMode.ts` needed no change (the selection and reduced-motion
readers already said what the round needs).

**What each key does now** (all with the sky's keys owning the keyboard —
nothing that takes keys itself has the focus, no dialog over the sky):

| Key | Does |
|---|---|
| `0` | fit all, legibly (`firstFit`) — as before, now eased |
| `Shift+1` | fit all (the same as `0`; Figma's key) |
| `1` | fit the focused line — as before, eased |
| `Shift+2` | fit the selection: the selected lines' and objects' bounds, or the selected stops' boxes (`fitSelection`, 1.25 cap; 1.5 for stops); nothing selected — nothing happens |
| `Shift+0` | 100 % about the window's middle (eased); the toolbar's 100 % button says it |
| `+` `−` arrows, the wheel, a pan, a pinch | as before, direct — and any of them stops a glide where it is |
| `Tab` | the palette floats with its filter focused (where it was last pinned, else the view's left third under the toolbar); floating already — docks it. Inside the floating palette's filter (where the first Tab put the keyboard) a second Tab docks it too (Shift+Tab stays the browser's). Not on a phone |
| double-click on empty sky | the palette floats AT THE POINTER with its filter focused (n8n); placing from it lands beside it (`placeGrounded(…, "palette")`). A double-click on a line keeps its meaning (it never reaches the sky). A phone (narrow) keeps the new-session block |
| `F` | with a selection: focus mode — `.sky-focus` on the plane, `data-dim` on every slot and object not selected (a stop selection keeps its line bright); everything dimmed fades to 35 % (60 % under the pointer), opacity only, 120 ms. Any press on the sky, Esc, or clearing the selection lifts it |
| `?` | the shortcuts sheet (`SkyShortcuts`, `open`); Esc closes it before anything else Esc does; the ⋯ menu has "Keyboard shortcuts…" for the mouse |
| `Ctrl+Z` / `⌘Z` | undo, as round 14 |
| `Ctrl+Shift+Z` / `⌘⇧Z` | redo: `popRedo` → "Redid: <label>", or "Nothing to redo". Same gates as undo (not in a field, a pane, a menu, a dialog, nor mid-drag). `Ctrl+Y` stays the view toggle |
| `Alt` held while dragging | no snapping (the raw delta) |

**Snapping** (`lib/skySnap`). `snapBox(moving, others, threshold)`: the
moving box's left / centre / right and top / middle / bottom against every
other box's, each axis on its own, the nearest alignment within `threshold`
winning (ties: the first found, the others in drawing order); returns the
snapped `x`, `y` and the guides — a vertical one at the aligned x spanning
from the moving box to the box it aligned with (both heights), a horizontal
one likewise. `snapThreshold(z) = 8 / z` (8 screen px). The sky
(`onDragStart`'s `delta`) snaps the PRESSED box against everything that
stays put (every line, object, the lane, the palette — not the selection
being dragged) and moves the rest of the selection by the same snapped
delta; the last move's delta is what the drop pins (so a drop lands where
the guides said). The guides are drawn in the plane (`.sky-guide`,
`data-axis="x|y"`, `left/top/height` or `top/left/width` in plane px) at 1
SCREEN px of `--accent` whatever the zoom (`calc(1px / var(--sky-z))`),
`pointer-events: none`, gone on release (`setGuides([])`). The plane
carries `data-snapping` while any guide shows; the dragged slot wears
`is-snapping`.

**The eased camera.** `goTo(target, instant?)` in SkyView: ~220 ms
(`CAM_EASE_MS`) on `requestAnimationFrame`, ease-out cubic (`easeOut`), the
zoom interpolated in log space (`lerpCamera`), the LAST frame
`setCamState(target)` exactly — then nothing moves. Instant when: `instant`
(the first picture after the lines settle — there is nothing to glide
from), reduced motion (the setting or the media query, `motionOff`), the
camera is already there (`sameCamera`), or no frame scheduler. `setCam` is
now a wrapper that cancels a glide first: the wheel, a pan, a pinch, `+`/`−`,
the arrows, the minimap's drag all cut a glide short where it is. Goes
through `goTo`: `0` / `Shift+1` / Fit, `1`, `Shift+2`, `Shift+0` and the
zoom menu's presets (`zoomTo`), the focus effect's centring when a session
picked elsewhere is off screen (`centerOn`), the minimap's and the hints'
jump (`jumpTo`). No FLIP ghosts: the plane's transform is the only thing
that moves.

**Redo** (`lib/skyUndo`). `UndoInput.redo?()`; `popUndo` / `takeUndo` move
an entry that has `redo()` onto the redo stack (its clock restarted), else
nothing; `popRedo` takes the newest back onto the undo stack and returns it
(the caller runs `redo()`); `redoCount`; any `pushUndo` empties the redo
stack (the branch is gone); the redo stack expires and caps like the undo
stack; `clearUndo` empties both. Entries that carry `redo()` now:
- the grounded line's delete (`removeGrounded` in SkyView): undo remakes
  the line (a NEW flight id, kept in a closure), redo deletes that one;
- the merge onto a session (`mergeGrounded`): undo remakes the grounded
  line, redo removes the remade line again (the stops stay on the session
  either way, as round 14 said);
- the note's delete (`removeNote`): it is one `pushUndo` entry now (so
  Ctrl+Z reaches it too), the toast's Undo is `undoAction(entry, …)`, redo
  removes it again.
- NOT yet: a stop's or a group's removal (`removeStop` / `removeGroup` in
  `useSessionLine.tsx`, the selection bar's `removeStops` / `removeLines`
  in `SkySelection.tsx`, a grounded line's own Remove in
  `GroundedLine.tsx`, a close in `SkyLine.tsx`'s `pushCloseUndo`). Those
  entries are pushed OUTSIDE this task's fence; each needs a `redo()`
  that drops the re-tacked step (`dropStop` / `dropGroup` by the id the
  undo's `tackOn` returned) — the integrator or the next cycle wires them.
  Ctrl+Shift+Z after undoing one of these says "Nothing to redo".

**The cursor contract** (`Sky.css`, under `.sky-viewport` so it wins over
the bars' own sheets): `grab` on `.skl-bar`, `.sky-line-bar`,
`.sky-lane-head`, `[data-sky-head]`, `.sky-object-head`,
`.sky-palette-head`; `grabbing` on the same inside `.is-dragging`;
`crosshair` on the viewport in drag mode (`.sky[data-tool="drag"]` — a drag
on empty sky is the marquee; click mode's `grab` stays); `move` on the bar
of the slot that is snapping (`.is-snapping`) and on a dragging object's
head while the plane says `data-snapping`; `copy` on and inside
`.is-drop-target` / `.is-merge-target`; no `zoom-in` / `zoom-out` anywhere
in the sheet.

**The seams mounted** (inside the viewport, after the selection bar):
- `<OffscreenHints rects cam viewport onJump>` and, in
  `.sky-minimap-host` (absolute, right 12 / bottom 12, z 9, hidden under
  520 px), `<Minimap rects cam viewport onCamera onJump open onToggle>`.
  `rects` (`mapRects`): every line from `rectOf` — `kind` "line" or
  "grounded", `tone` = the session's state word (`deriveSessionState(row)
  .state`: starting / working / needs_you / idle / limit / offline) or the
  grounded flight's `state`, `label` = its name, `selected`, `focused` (the
  focus line, or the grounded focus) — and every object (`kind` = the
  registry kind, label "Intake" for the lane, else its key). `onCamera` sets
  the camera directly (a drag); `onJump` = `jumpTo`: eased `centerOn` at the
  current zoom (≥ 0.85) and the line focused as a press on it would (no box
  opens). `open` is remembered in `localStorage` `mf_sky_minimap` ("0" =
  folded; default open).
- `<ZoomMenu zoom min max onZoomTo onFitAll onFitSelection onFitFocused
  canFitSelection canFitFocused>` in the toolbar's zoom group where the
  `sky-zoom-read` span was (the seam still renders that span until the
  `zoom-shortcuts` task fills it, so the readout never goes missing).
- `<SkyShortcuts open onClose groups={[]}>` with a `TODO(zoom-shortcuts)`
  comment: `lib/skyShortcuts.ts` (`SKY_SHORTCUT_GROUPS`) is that task's file
  and did not exist in this worktree; the integrator imports it and passes
  it as `groups`.

**Copy that moved.** The empty sky's line reads "double-click anywhere for
the blocks, or press the Session chip in the shelf below, to start one";
the viewport's aria-label ends "double-click for the blocks, ? for every
key". The ⋯ menu gained "Keyboard shortcuts…".

**Test pins changed deliberately** (round 14/15 literals that round 17
replaces): `sky.test.ts` (the plane's className now carries `sky-focus`);
`skyZoom.test.ts` (`setCam((whole ? …` → `goTo(…, instant)`; the 100 %
button's title says Shift+0); `skyView.test.ts` (`setCam(centerOn` →
`goTo(centerOn`; the first fit is `fitFocused(true)`; the 1 button's
`onClick={() => fitFocused()}`; the "no redo" test is now the redo test;
the grounded delete's entry has `redo`; `focusFilter={!!catalogOpen ||
filterFocus}`; `setPaletteMode("shelf")` is a block); `skyMerge.test.ts`
(the merge's entry has `redo`); `skyObjects.test.ts` (the ⋯ menu has two
items; the note's toast uses `undoAction`). Every other pin holds.

**Open.** The judge's second pass decides whether Tab stealing the keyboard
from a focused line is right (n8n's manner, but it is also the browser's
focus key); the stop / group redo entries above; `SkyGuide`'s and the
tour's copy still say a double-click starts a session where they mention
it (outside this fence).

## Round 18: free-floating chains (the n8n look) — contract

The owner (2026-11-02): boxes should float free like n8n — icon tiles, ports,
wires you drag — not rows that look like they wait for a session; show which
session a chain runs in; let one session's chain trigger a chain in another
session: "I want really max modularity".

**Words.** A **chain** is what the Sky used to call a line: one flight's stops
(a session's live flight, or a stops-only/grounded flight) plus its **head**.
A **node** is one stop (or a collapsed group) drawn as a tile. A **wire**
joins an **out port** (right side of a tile) to an **in port** (left side).

### What it looks like
- **Node** = a 64×64 rounded tile (radius 12) holding the kind's icon (28 px,
  the kind's family colour), the title under it (12.5 px semibold, ≤2 lines,
  centred) and the summary under that (11 px muted, 1 line). No card around
  a chain, no row background. State on the tile: running = green border +
  soft glow, waiting on you = gold, halted = red dashed, failed = red, done =
  a small ✓ badge on the tile's top-right and a dimmer tile, pending = plain,
  a halt point = the ⏸ mark, the risk stripe = small coloured dots under the
  tile. The bird perches on the tile's top edge. A collapsed group = a tile
  with a stacked (2 offset borders) look and "k of n" in its summary.
- **Ports**: in = a small square on the tile's left middle, out = a small
  circle on the right middle; both grow on hover. The chain's last node
  carries n8n's trailing "+" stub on its out port (a press opens the palette
  there; a drag starts a wire).
- **Wires**: cubic béziers out→in (horizontal tangents). Line wires plain;
  loop wires (`via` = the Loop) leave the out port, run under both tiles and
  come up into the target's in port, pill in the middle (round "Redrawn"
  rules: the Loop has no tile); fan/join for Split/Join and children as now.
  Handoff wires (cross-chain, below) dashed in the source session's colour.
- **Head** (`ChainHead`): a session chain starts with the **session tile** —
  an n8n trigger-style node (rounded left side) with the session's name,
  state chip and bird; a click opens its card as the session box does today;
  its out port feeds the first stop. A stops-only chain that another chain
  hands off to starts with a **trigger tile** "When <chain> reaches <stop>"
  naming the session it will run in. A stops-only chain with neither has
  **no head**: its first node's in port shows an empty socket ("Wire a
  session's out port here to run it") — the "No session yet" box is gone.
- **Session colour**: `chainColor(sessionTitle)` — one of 8 hues, stable per
  title; the head wears it as a dot and its chain's wires at low opacity, so
  "this chain runs in that session" reads at a glance. Stops-only: neutral.

### Free positions
- Every step may carry `pos: {x, y}` — integers, chain-local plane px,
  relative to the chain's origin (the head's top-left; with no head, the
  slot's origin). The sky layout store keeps placing chain origins as it
  places lines today (`mf_sky_layout`, Tidy, pins, marquee).
- `autoPlace` gives a node without `pos` a spot: left→right after the
  previous node (tile pitch 150 px), branches 150 px below their split.
- Dragging a tile moves only that node (8 px grid snap, Alt = free); it never
  reorders. Order is the wires. The chain's bbox follows its nodes.
- Backend: `pos` is kept by the normaliser, dropped when not two finite
  numbers (clamped ±100000, rounded). `PATCH /api/flights/{id}/steps/{sid}
  {pos}` is allowed on ANY step (flown and current too — layout only, never
  `_editable`). Bulk: `POST /api/flights/{id}/layout {positions: {<sid>:
  {x, y} | null}}` (null clears; unknown ids ignored) → `{flight}`. Saved
  plans keep `pos` too (a saved stop dropped back keeps its shape).

### Wiring (drag from a port)
The host (SkyView) runs every wire drag, so a wire can reach another chain:
a rubber band from the port follows the pointer; the node or head under it
lights when the drop means something; `wireIntent` (pure, lib/skyGraph) says
what a drop does:
| From | Dropped on | Intent | API |
|---|---|---|---|
| node X out (chain A) | a node Y of A after X | `move`: Y right after X | `moveStop` |
| node X out | a node Y of A at or before X (X itself too) | `loop`: a Loop after X back to Y | `tackOn` Loop (`loopStepTo`) |
| node X out | the first node / socket of a stops-only chain B | `merge`: B's stops spliced in right after X | `tackOnSteps(A, B.steps, at)` + `deleteFlight(B)` (as `mergeGrounded`) |
| node X out (A has a session) | the head of a stops-only chain B whose drop asks for another session (Shift held, or B already has a trigger) | `handoff`: a Handoff step after X targeting B | `tackOn` Handoff `{target: "flight:<B>", session: "new"}` |
| a session tile's out | the first node / socket of a stops-only chain B | `attach`: B flies on that session | `attachGrounded` / `tackOnSteps` (as `mergeGrounded`) |
| node X out / session out / "+" | empty sky | `palette`: the palette opens there; the picked stop goes right after X with `pos` = the drop | `tackOn` + layout |
| an in port's wire end (grab the wire near its in port) | empty sky | `split`: the node and every stop after it leave into a new stops-only chain placed there | `POST …/steps/{sid}/split_off` |
| anything | nothing that fits | `none` (the band snaps back) | — |
Rules: only steps that can still change move (the server refuses others; the
UI greys the target). Esc cancels. The ↻ handle and pill drags from the
"Redrawn" rounds keep working on the classic FlywayCanvas; in the graph the
out-port drag IS the loop gesture, and a loop pill still re-aims (`onLoopTo`).

### One chain triggers another (the Handoff kind)
New plug-in kind `handoff` (label "Start a chain", family "flow", actor
robot, icon ↗): config
- `target`: `"flight:<id>"` — a stops-only chain on the board (consumed: it
  takes off on the session), or `"plan:<id>"` — a saved stop/flyway (a fresh
  copy each time: the reusable module). Required to take off.
- `session`: an existing session title, or `"new"` (a new session in this
  flight's repo, the program/model of this flight's session — the Split's
  `spawn_worker` path). Default `"new"`.
- `wait`: bool, default false — false: done once the target has taken off;
  true: running until the target flight lands (landed → done, failed /
  cancelled → this step fails with the target's reason).
Run: start the target (flight → `attach` on that session; plan → `fly_plan`
onto it), record `detail.child` = the started flight id, `detail.session` =
where it runs, receipt "started <name> on <session>". The started flight
carries `triggered_by: {flight, step, session}` in its dto. A target already
flying / gone → the step fails with a sentence. Dialog fields: target = select
filled by `dynamic: "chains"` (the board's stops-only chains + saved stops),
session = select `dynamic: "sessions"` + "a new session".
Drawn: a handoff wire from the Handoff node's out port to the target chain's
head (its trigger tile); once started, to the started flight's head on its
session.

### Split off
`op_split_off(flight, sid, pos=None)` → a new grounded flight holding
steps [sid…] (each still pending — else 409 `not_pending`), named "<plan
name> (from <step title>)", loops re-aimed (`_follow_loop_targets`), the
first step's `pos` = the drop's; the steps leave `flight`. Route `POST
/api/flights/{id}/steps/{sid}/split_off {pos?}` → `{flight, chain}`.

### What stays
FlywayCanvas (the classic row) stays for the pane's Flight tab, the strip,
the plan preview and the Session flight panel; `liftLoops` & co. feed both.
Only the Sky draws chains.

### Seams (pre-committed; workers fill them)
- `frontend/src/lib/skyGraph.ts` — types `ChainHead`, `GraphNode`,
  `GraphWire`, `ChainGraph`, `WireIntent`, `WireEnd`; functions `chainGraph`,
  `autoPlace`, `wirePath`, `loopWirePathG`, `chainColor`, `portAt`,
  `nodeAt`, `wireIntent` (stubs).
- `frontend/src/components/flights/SkyChain.tsx` — `SkyChainProps` and a
  placeholder render.
- `lib/flights.ts`: `FlightStep.pos?`, `Flight.triggered_by?`.
- `lib/flightsApi.ts`: `saveStopPositions`, `splitOffChain`.
- Each worker wrote its notes to `docs/flights-r18/<task>.md`, folded in
  below.

### Round 18 — as built (2026-11-02)

The workers' integrator notes, folded in (backend, graph, sky), then the integrator's own.

#### Backend (r18-backend)

#### Round 18 backend: notes for the integrator

Everything here is live on branch `emandel2630/r18-backend`. Tests:
`tests/unit/test_flights_r18_backend.py` (48 tests).

##### 1. Step `pos`

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

##### 2. Split off

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

##### 3. The `handoff` kind ("Start a chain")

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

###### `triggered_by` on the flight dto

A flight started by a Handoff carries
`triggered_by: {flight: <source flight id>, step: <handoff step id>, session: <the SOURCE flight's session>}`.
The session the child runs in is the child's own `session`. The key is
**omitted** on every other flight, so don't expect `null`. It persists in
flights.json.

##### 4. MCP

`backend/mcp/flights.py` is generic: it reads kinds from
`GET /api/flights/kinds` and passes step dicts through untouched, so
`handoff` shows up in `describe_flight`'s kinds and `pos` survives
`start_flight` / `edit_flight` / `save_flight_plan`. No MCP change was made,
and split_off is **not** in `edit_flight`.

##### 5. For the frontend

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

##### Open issue (outside my fence)

`tests/unit/test_flights.py::test_kinds_registry_drives_the_editor`
(line ~85) asserts that every select with empty `options` has
`dynamic == "stops_before"`. The handoff's `target` (`dynamic: "chains"`)
fails it. One-line fix: `assert fld["options"] or fld.get("dynamic") in
("stops_before", "chains", "sessions")`.

#### Graph model and tiles (r18-graph)

#### Round 18 — graph worker notes (skyGraph + SkyChain)

What the integrator (SkyView) needs to wire the free-floating chains in. Spec:
`docs/flights.md` "Round 18: free-floating chains (the n8n look) — contract".
Every seam name and signature is unchanged; everything below marked *added*
is optional and new.

##### `frontend/src/lib/skyGraph.ts`

Coordinates are **chain-local plane px**: (0, 0) is the head's top-left (no
head: the slot's origin). A node's box is `NODE_W × NODE_H` (132 × 118); its
tile (`TILE` = 64) sits centred at the box's top, the label area under it.

| Export | What |
|---|---|
| `chainGraph(d, {head, key, flightId})` | The chain of a `sessionDiagram` / `groundedDiagram` (collapse groups first, as the Sky does today). The diagram's session node becomes the head (`id === HEAD_ID`, `"head"`); parent rows are dropped; main-row stops / groups / the flown pill / rungs become nodes; branch / below rows (workers, hung children) are nodes too, placed under their split. |
| `autoPlace(nodes)` | Line order in; pinned nodes keep their spot; the rest go `PITCH_X` right of the node before (so an un-positioned tail **follows** a moved node); branches `PITCH_Y` below their split, half a pitch right, stacking. |
| `moveNode(g, id, pos)` *added* | The graph with `id` pinned at `pos` (tail follows, wires re-route, bbox follows). SkyChain uses it for the drag preview; the host can use it for an optimistic update. The head never moves (returns `g`). |
| `snapPos(p, free)` *added* | 8 px grid (`GRID_SNAP`), or rounded when Alt is held. |
| `chainBBox(g)` *added* | What `g.bbox` is: every box, the `+`, each loop's run + pill + hook bulge. `bbox.x/y` go **negative** when a node is dragged left of / above the origin. |
| `outPortOf`, `inPortOf`, `portPos(n, side)` *added* | Port spots (the rubber band starts at `portPos`). |
| `wirePath(a, b)` | Cubic out→in, horizontal tangents. |
| `loopWirePathG(a, b, others, lane = 0)` | Out port → hooks down → runs `LOOP_CLEAR` under the lowest of a, b, `others` (whole boxes, labels included) → hooks up into b's in port. `lane` (*added*, optional) drops nested loops `LOOP_LANE` lower; `chainGraph` assigns lanes (wider span = lower). |
| `chainColor(title)` | One of `CHAIN_HUES` (8), stable per title; `var(--muted)` for `""`, null and `"new"`. |
| `nodeAt(g, p)`, `portAt(g, p, slop)` | Chain-local hit tests (topmost box; nearest *live* port). |
| `wireIntent(src, dst, {shift})` | The Wiring table (below). |
| `wireTargets(src, chains, ctx)` *added* | `Map<chainKey, Set<nodeId>>` of every node a drop means something on — pass each chain's set as its `targets`. |
| `canFollow(n, insertFrom)`, `loopable(n)` *added* | The rules `wireIntent` uses: a stop may go right after `n` (its next slot ≥ `insertFrom`); a node a Loop can leave from / return to. |

`ChainGraph` *added fields*: `insertFrom` (the diagram's) and `plus: {after, at}
| null` (the node the trailing `+` hangs off — `"head"` when there are no
stops — and the button's centre). `GraphWire.lane` *added* (loops).

Ports: head = out only. A stop's `inPort` = it is `editable` (can still
change), or it is the first node of a stops-only chain; `outPort` =
`canFollow` (a live flight's current stop has one; flown stops don't).

###### `wireIntent` — chains are named by **key**

All intents name chains by `ChainGraph.key` (a session title, or
`"flight:<id>"` for a grounded chain — the same form as `groundedKey`); a
handoff's `target` is `"flight:<B.flightId>"`. Steps are diagram nodes (flight
indexes in `node.index`).

| src | dst | intent |
|---|---|---|
| X out (X can be followed) | Y of the same chain, `Y.index > X.index`, Y an editable **stop** (groups don't move), not already right after X | `move {chain, step: Y, after: X}` |
| X out | Y of the same chain at or before X (X itself too), both `loopable` | `loop {chain, from: X, to: Y}` → `tackOn` Loop via `loopStepTo` at `loopSlotAfter(X)` |
| X out | first node of a stops-only chain B (no trigger), no Shift | `merge {chain: A, after: X, from: B.key}` |
| X out, A has a session | B's first node **or trigger tile**, with Shift or B already triggered | `handoff {chain: A, from: X, target: "flight:<id>"}` |
| session head out | B's first node / socket (or its trigger tile) | `attach {session, chain: B.key}` |
| X out / session head out / `+` | `{sky}` | `palette {chain, after: X \| null, at}` (`null` = after the head: insert at `insertFrom`) |
| X **in** (editable stop/group; not the first of a stops-only chain) | `{sky}` | `split {chain, step: X, at}` |
| anything else | | `none` |

`dst` for the socket = `{chain: B, node: B's first node, side: "in"}`. The
side of `dst` is ignored (a drop on the node counts).

##### `frontend/src/components/flights/SkyChain.tsx`

Props as pre-committed, plus *added* optional:
- `kinds?: StepKindDef[]` — family colours follow the server's `family`
  (else StopIcons' built-in map).
- `onPillDown?(via, e)` — a loop pill pressed (before its click): start a
  re-aim drag here if you want `onLoopTo` in the graph. `onLoopPill` still
  fires on click.

Exported helpers: `tileSub(n)` (group "k of n · current", receipt, else sub),
`tileLabel(n, head?)`.

Behaviour the host relies on:
- **Root** (`.sky-chain`, `role=list`, `data-chain`, `data-flight`,
  `data-head`) is sized `bbox.x + bbox.w` × `bbox.y + bbox.h`, overflow
  visible; position it at the chain's origin. Nodes at negative spots overflow
  left/up — use `graph.bbox` for the real extent (Tidy, marquee, fit).
- **Node drag** is internal: tile pointerdown is `stopPropagation`ed only for
  draggable nodes (stops / groups, when `onNodeMove` is set). Head, flown
  pill, branch tiles let the pointerdown bubble (the host can drag the whole
  chain by its head). Preview = `moveNode`; `onNodeMove(node, pos)` fires on
  release only if the spot changed; Esc cancels.
- **Ports**: `<span class="sky-port sky-port-in|out" data-port="in|out"
  data-node="<id>">` call `onPortDown(node, side, e)` on pointerdown (button
  0, propagation stopped). Passive wire ends on fixed tiles get
  `sky-port-off`, no data attributes.
- **`+`**: `<button class="sky-plus" data-port="out" data-node="<after>"
  data-plus>` — a click → `onPlus(afterNode)`; once the pointer moves 4 px it
  calls `onPortDown(afterNode, "out", <the pointermove event>)` (the host's
  drag starts from the move, so a plain click never starts one).
- **Socket** (headless chain): `<span class="sky-socket" data-port="in"
  data-node="<first>" data-socket>` — a drop target only; lit by
  `targets`/`over` on the first node's id.
- **Clicks**: a stop/group → `onSelect`; head, flown pill, branch/child →
  `onOpen`. Double-click / Enter → `onEdit` (stops); Space = click;
  Delete/Backspace → `onRemove` (stops/groups and a focused loop pill);
  right-click → `onMenu(node, {clientX, clientY})`.
- `selected` may be a node id or the head's diagram id (`session:<title>`);
  `bird.nodeId` likewise maps the session node onto the head. The bird is a
  picture here (no hand); add one in a later round if the graph needs it.
- `--sky-z` must be set on an ancestor (the Sky already does) for the loop
  pill to stay screen-sized.
- Imports `FlywayCanvas.css` (bird marker) and `SkyChain.css`; the built
  bundle needs a rebuild (`npm run build`) by whoever owns `backend/web/static`.

##### CSS classes (SkyChain.css, prefix `sky-`)

`.sky-chain` · `.sky-wires` (the one SVG) · `.sky-wire` + `-line|-head|-fan|
-join|-loop|-add|-socket`, `.is-done`, `.is-live` (flowing dash into the
current stop), `.sky-loop-pending|running|done` · `.sky-node` +
`.sky-node-<kind>` / `.sky-head .sky-head-session|trigger`, `.st-<state>`,
`.sky-fam-work|ship|outside|people|flow`, `.tone-<chip tone>` (head),
`.is-group`, `.is-perch`, `.is-selected`, `.is-highlighted`, `.is-target`,
`.is-over`, `.is-dragging`, `.is-draggable`, `.is-fixed` · `.sky-tile`,
`.sky-tile-icon`, `.sky-node-title`, `.sky-node-sub`, `.sky-node-extra`,
`.sky-head-extra`, `.sky-head-dot`, `.sky-head-chip` · `.sky-badge` +
`-done|-failed|-halt|-if` · `.sky-risk` + `.sky-risk-<tag>` · `.sky-port` +
`-in|-out|-off` · `.sky-plus` · `.sky-socket` · `.sky-loop-pill` ·
`.sky-chain-bird`. Tokens are local custom properties on `.sky-chain`
(`--sky-tile-bg`, `--sky-wire`, `--sky-fam-*`, …) over the app's theme
variables; `.light` softens the mixes. The head's colour arrives as
`--sky-head-color` (inline); head/line wires get `stroke: head.color` inline at
0.6 opacity.

##### `frontend/src/components/flights/StopIcons.tsx` (new)

`TileIcon({kind, size})` — Bird.tsx's `StopIcon` plus `handoff` (↗ out of a
box) and `group` (stacked layers). `kindFamily(kind, kinds?)`,
`nodeFamily(node, kinds?)`, type `StopFamily` (`work | ship | outside | people
| flow`; Loop and Handoff are `flow`). When the backend adds the `handoff`
kind, the palette's `StopIcon` won't know its glyph — use `TileIcon` there
too, or move the glyph into `StopIcon`.

##### Not done here (outside the fence)

- Cross-chain handoff wires (dashed, source session's colour) — the host
  draws them between chains; SkyChain only draws a chain's own wires.
- The rubber band during a wire drag — host.
- Rebuilding `backend/web/static`.

#### Sky integration (r18-sky)

#### Round 18 — Sky integration notes (r18-sky)

Spec: `docs/flights.md` "Round 18: free-floating chains (the n8n look) —
contract". Built against the pre-committed `skyGraph.ts` / `SkyChain.tsx`
seams. Neither file is edited here. Checked against r18-graph's real versions
(merged on `emandel2630/flights`, f0e0f420) in a scratch copy: tsc is clean
and vitest is 5661/5662 (the one failure is the skySnap "snaps the pressed
box" timeout, which also fails on the base). Your commit 0ecc6d65 (the card's
double-click fold, the `mf:tour-reveal` camera glide) merges into SkyLine.tsx
and SkyView.tsx with **no conflicts** (`git merge-file` checked).

##### What changed

###### Lines are chains
- **SkyLine** draws a small `.skl-label` (Bird, name, where, age, pin, ⋯) and
  then `<SkyChain>` from `chainGraph(line.graphDiagram, {head, key: title,
  flightId})`. The head is `{kind: "session", title: windowName, sub: where ||
  task, session: title, color: chainColor(title), node: <the session node>}`.
  `renderHead` = the state chip. The head tile is the chain's drag handle (its
  press bubbles up to the label's `onDragStart`; a press that moved is not a
  click). A click on the head opens the session card. Cards (the session's,
  and each opened worker's) sit in a `.skl-cards` row under the chain.
  **PaneCard is untouched**, so your double-click fold is kept as-is. The ◧,
  the box-size ⋯ items, the corner resize and the scaled sizer are gone (a
  chain's tiles have one size). `dragBox`, `presetPatch`, `nextSize` and
  `flownCanvas` are still exported for the classic row.
- **GroundedLine** has no "No session yet" head box and no card frame. It
  draws a `.skl-label` (name / rename, chip, ⋯; still the drag handle) and
  then a chain. The new `head` prop takes a trigger head (from SkyView's
  `triggerHeads`), else `NO_HEAD` (headless, with SkyChain's socket).
- `.skl.skl-chainline` (SkyLine.css) drops the card look: no border,
  background, shadow or padding. Focus rings the label only. A chain the
  marquee took whole keeps the sky's one solid selection ring.
- Slot size = what the line root measures (label + `SkyChain` root sized by
  `bbox` + cards). Before measuring, SkyView estimates with `chainSize(diagram,
  head)`.

###### Hooks (useSessionLine / useGroundedLine)
- New on both: `graphDiagram` (the line in the **flight's** indexes, which
  chainGraph / wireIntent need; `diagram` / `canvas` stay drawn-place-counted
  for FlywayCanvas), `graphCanvas` (the raw handlers), `paletteAfter(node |
  null, pos?)`, `anchor` / `anchorPos` (where the palette or dialog floats).
- A stop picked from a palette opened at a drop carries `pos`. `wireStep` now
  sends `pos` (the backend keeps it on `POST /steps`).

###### Wiring (useSkyWiring.ts, SkyWires.tsx)
- `useChainRegistry`: each line calls `registerChain(entry)` (graph, the
  chain's offset in its slot, its `paletteAfter`) from a layout effect
  (`useChainEntry`). The registry always stores the latest entry. It bumps
  its `version` (which redraws the cross-chain wires) only when `entrySig`
  changes (ids and spots). **A render loop was found and fixed:** SkyView's
  `kinds || []` / `plans || []` made new arrays while loading, which remade
  every graph, which re-registered, which re-rendered. The fallbacks are
  stable consts now, and the signature check guards against it either way.
- `useSkyWiring`: `startWire(key, node, side, e)` from each chain's
  `onPortDown`. It uses window pointer listeners and draws the band in plane
  px. Hits are found in the DOM (`elementFromPoint` → `[data-node]` inside
  `.sky-chain[data-chain]`, never inside `.skl-card`). Targets come from
  `wireIntent` node by node (`wireTargetsOf`; this is equivalent to r18-graph's
  optional `wireTargets`, used here so the branch compiles against the stubs).
  Shift re-lights the targets, Esc cancels, and the click after a drag is
  eaten. SkyView's `onDownCapture` leaves `.sky-chain [data-port]` alone in
  both tools. The old `startConnect` / `.fl-box-add` gesture is removed: the
  `+` stub and the session head's out port now do it as `attach` / `merge`.
- `runWireIntent(intent, ctx, deps)` (pure, tested):
  - move → `moveStop(f, sid, loopSlotAfter(after))`
  - loop → `tackOn(loopStepTo(...), loopSlotAfter(from))`
  - merge / attach → SkyView's `mergeGrounded(from, into, was, at?)`, which
    now takes a flight index and a stops-only target, and strips the merged
    stops' `pos`
  - handoff → `tackOn(handoffStepFor(def, ids, "flight:<id>"),
    loopSlotAfter(from))` (`tackOnNew` with no flight; a toast if the server
    lacks the kind)
  - palette → the chain's `paletteAfter(after, drop - tile centre, snapped to
    8 px)`
  - split → `splitOffChain(f, sid, {x: 0, y: 0})`. The new chain's slot is
    pinned at the drop. Its other stops' saved spots are re-based onto the
    new origin (`saveStopPositions`).
- Node moves: `useNodeMoves` keeps an optimistic spot map, cleared once the
  server pos matches (or on failure), and `queueStopPos` (flightsApi) batches
  them into **one `POST …/layout` per flight** 250 ms after the last drop.
- Cross-chain wires: `handoffLinks(flights)` (groundedLines) lists every
  Handoff stop with a `flight:` target or a started `detail.child`.
  `handoffWires` draws each one from the Handoff tile's out port to the target
  chain's head (or first tile) in port: dashed, in the source session's
  colour, dimmer once started. These are drawn in `<SkyWires>` (`.sky-xwires`,
  plane coordinates, `pointer-events: none`), with the band.
- `triggerHeadFor(flight, flights, windowName)`: "When <session> reaches
  <stop>", "runs on <session | a new session>", `chainColor(source session)`.

###### Panels
- `ChainPanels`: the palette or the stop dialog is absolutely positioned
  inside `.skl-chain-wrap`, right of its tile (or 24 px right of a wire's drop
  spot). It never changes the slot's measured size.

###### StopDialog / StopParts
- `dynamic: "chains"` → `BoardChoiceField`: chips grouped "On the board" (the
  stops-only chains, `flight:<id>`) and "Saved stops — a fresh copy each time"
  (`plan:<id>`). `dynamic: "sessions"` → "a new session" (`new`) and then
  every session title (`windowName` labels). The options are read from the
  query cache (`queryClient.getQueryData`), so StopForm needs no provider. A
  value no longer on the board stays visible as its own chip.
  `lib/flights.ts`'s `StepKindField.dynamic` type is still `"stops_before"`
  (outside my fence), so StopParts reads it as a string (`boardDynamic`).
  **Integrator:** widen it to `"stops_before" | "chains" | "sessions"`.

###### Tour hooks (kept on the element itself)
- `sky-line`: the session line's slot (SkyView, unchanged).
- `sky-line-bar`: the chain's label.
- `sky-session-box`: the head tile, and `sky-stop`: the first stop tile.
  Both are set on SkyChain's own elements by `useTourHooks` (a DOM effect),
  since the seam has no prop for them. If r18-graph adds a `tourHooks` /
  `headProps` prop, switch over.
- `sky-grounded`: the stops-only chain's label, and its `.sky-socket`.
- `flights-bird`, `sky-selbar`, `sky-shelf` are untouched.

###### SkyGuide
- "Chains, tiles and wires" (ports, dragging a tile, out-port drops, `+`,
  pulling an in port off to split) and "Stops on their own, and handoffs"
  (the socket, Shift-drop = Start a chain, the trigger tile, the dashed wire)
  replace the old "No session yet" copy.

##### Still to do (for the integrator)

- **Props r18-graph added that are not passed yet** (the stubs don't have
  them): `kinds` on `<SkyChain>` (family colours from the server) and
  `onPillDown` (re-aiming a loop pill in the graph). `onLoopPill` is wired:
  it opens the Loop's dialog.
- **Right-click menu:** `onMenu` is not wired (the classic StopMenu was
  FlywayCanvas's).
- **Worker birds in formation:** FlywayCanvas flew these; SkyChain draws one
  bird.
- **Negative bbox:** a tile dragged left of or above the chain's origin
  overflows its slot (SkyChain sizes its root `bbox.x + bbox.w`). Tidy,
  marquee and snapping use the slot rect.
- **Existing layout behaviour, not new:** lines are placed under the intake
  lane at its first measured height. At a nearer zoom the lane draws its
  cards and grows over them. The base does the same.
- Rebuild `backend/web/static` after the merge (I never build into the
  worktree).
- **Outside my fence:** `frontend/src/__tests__/flightsUi.test.ts` ("the sky
  view: … every session a line with the bird on its active box") still asserts
  the row's `>Tack on a stop<` + and `.fl-bird-marker`. It fails on this
  branch. The fix, a 5-line change asserting `data-chain` on each line and
  the `bird={birdPerch(moves.diagram)}` source, is in the scratch patch
  `/tmp/claude-1000/-home-emandel2630--mindflock-worktrees-emandel2630-r18-sky-18e43fd578908858/7d51475c-55e3-42ff-874f-de5068097847/scratchpad/flightsUi-test.patch`.
  It passes against both the stubs and the real SkyChain.
- **Outside my fence:** `lib/flights.ts` `StepKindField.dynamic` should
  widen to `"stops_before" | "chains" | "sessions"`.

##### Tests

- New: `r18Sky.test.ts` (29 tests):
  - heads and handoff links, the trigger head, `handoffStepFor`
  - handoff wire geometry, `nodeOfStep`, `panelSpot`
  - `wireTargetsOf` / `nodeUnder` / `entrySig` (loop-proof) / `withLocalPos`
  - every `runWireIntent` op against mocked flight routes
  - `queueStopPos` batching against the mocked client, `wireStep` pos
  - the chains / sessions choices and their chips
  - the host wiring and tour hooks pinned in the source
- Rewritten to the chain behaviour: skyLine, skyResize, skyVisibility,
  skyZoom, skyLooks, skySelect, skyMerge, skyView, grounded, flightsUi,
  skyGuide. They assert only markup that the stub SkyChain and the real one
  both draw (`data-chain`, the label, cards), plus the source.
- `skyGraph.test.ts` is untouched (r18-graph's).

##### Screens (scratch, merged picture with mocks)

The scratch directory is
`/tmp/claude-1000/-home-emandel2630--mindflock-worktrees-emandel2630-r18-sky-18e43fd578908858/7d51475c-55e3-42ff-874f-de5068097847/scratchpad/`:

- `sky.png`: the board after Fit (four session chains, the trigger head, the
  dashed handoff wire)
- `sky-zoom.png`: close-up of a session chain
- `sky-palette.png`: a wire dropped on empty sky; the palette opens at the
  drop
- `sky-attach.png`: a session tile's out port dragged to a socket, targets
  lit (on release: `POST /api/flights/f_live/steps` with the group, then
  `DELETE` of the stops-only flight)

#### Integrator (same day)
- Tiles' right-click menu (`useChainMenu`): the classic canvas's items via
  `menuItemsFor` (no Earlier / Later — order is the wires), in a portal at
  the pointer (a fixed box inside the zoomed plane is not fixed).
- A Loop's pill dragged onto a stop of its chain re-aims it (`pillAimer` →
  the line's `onLoopTo`); the stop under the pointer wears `.sky-aim-over`.
- `kinds` passed to SkyChain (family colours). `StepKindField.dynamic`
  widened to `"stops_before" | "chains" | "sessions"`.
- Tours: `findTarget` takes the FIRST selector part that matches anything
  drawn (a fallback never beats the step's own target, even off screen), and
  among that part's matches prefers one on screen; the Sky pans to it
  (`mf:tour-reveal`). The Sky tour's copy speaks chains, tiles, ports and
  wires (15 steps, a new "Wires set the order").
- Known gaps: a tile dragged left of / above its chain's origin overflows the
  slot (marquee / minimap / Tidy measure the slot); the worker-bird formation
  and the bird's click/drag are not on the graph's bird yet.

## Round 19: a chain starts when you say so — contract

The owner (2026-11-02): "why does it start right away when I add something?
Once I run a chain or start a new prompt I should start the chain — this
logic needs expanding out." Building a chain on a session is not running it.

### The `ready` state
- New flight state **`ready`**: attached to a session, not flying. The driver
  never moves it (not in `ACTIVE`); it is `UNFINISHED` and `HOLDING` (one
  unfinished chain per session, as today — a second start on that session is
  409 `in_flight`). Every stop of a ready flight can change (`_editable`
  accepts it: tack on, edit, move, drop, group, layout, split_off), its
  cursor is 0, nothing is marked flown.
- `flight.start_on`: **`"prompt"`** — it takes off on ▶ Run OR the session's
  next prompt; **`"run"`** — only on ▶ Run; **`"now"`** — takes off at once
  (today's behaviour; never stored as ready). Stored on the flight; shown in
  the dto. A ready flight's `start_on` can change: `PATCH /api/flights/{id}
  {start_on}` (ready only, else 409 `not_ready`); `"now"` on a ready flight
  = run it.
- Who passes what: `POST /api/flights/start` and `POST /api/flights/{id}/
  attach` take an optional `start_on`; **absent = `"now"`** (back-compat: the
  Intake Fly button, schedules, triggers, the handoff kind, MCP `start_flight`
  keep starting at once). The Sky passes `"prompt"` when you build a chain by
  hand (a stop added to a session with no flight, a stops-only chain wired
  onto a session).

### Taking off
- **▶ Run**: `POST /api/flights/{id}/run` (ready → `running` at cursor 0, its
  first stop starts on the next driver pass; 409 `not_ready` otherwise).
  `control_flight` (MCP) gains verb `"run"`; `controlFlight(id, "run")` in
  lib/flightsApi.
- **The session's next prompt** (`start_on: "prompt"`): a ready flight takes
  off when its session starts a NEW turn after it became ready — the
  server's own send paths (`/api/instances/{title}/send`, voice, MCP
  `send_message` to that session, the prompt queue) call
  `flights.on_prompt(title)`, and the driver's tick also catches a turn the
  user typed straight into the terminal (activity → working with
  `activity_since` > `ready_since`). It then flies **after that turn**: state
  `running` with `detail.after_turn = true` on the first stop — the first stop
  waits until the session's turn ends (idle / waiting on you), then runs.
  When the first stop is an Agent stop with no prompt of its own, that turn IS
  it: it is marked done with receipt "your prompt" once the turn ends, and the
  flight goes on. History event `took_off` with `{by: "run" | "prompt"}`.
- A turn already under way when the chain became ready does not count; the
  next one does.

### UI (integrator, after the backend lands)
The head tile of a ready chain wears a "Ready" chip and a ▶ Run button; its
⋯ (and the chain's) says how it starts: "On your next prompt" / "Only when I
press Run" / "Run it now". The ready wording lives only in `ReadyHead`; the
tile's subtitle stays the session's own line (`lineWhere` / task headline).
Building a chain in the Sky no longer starts anything.

### Round 19 — as built (2026-11-02)

#### Backend (r19-start)

#### Flights round 19 backend — notes for the integrator

The backend half of docs/flights.md "Round 19: a chain starts when you say
so". Everything below is live on the server. The frontend types are in place
(`lib/flights.ts`, `lib/flightsApi.ts`); no UI was touched.

##### State `ready`

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
  or `"<session>: ready · runs when you press Run"`. This deliberately
  differs from the tile's start-on line ("starts on next prompt" /
  "starts on ▶ Run", 2026-11-03): a notification names the session and must
  read alone, while the tile already shows the Ready chip. Change both
  together if either moves.

##### New flight fields (stored, and in every flight dto)

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

##### History

- New event **`took_off`**. Its entry carries an extra key, `by`:
  `{"at", "step", "event": "took_off", "note", "by": "run" | "prompt"}`.
  `note` is `"Run pressed"` or `"on your next prompt"`. `step` is the first
  stop's id.
- Changing `start_on` on a ready flight writes an `edited` event:
  `"starts on your next prompt"` or `"starts when you press Run"`.
- Attaching a grounded chain as ready writes `attached`, as before.

##### Routes

| route | body | result |
|---|---|---|
| `POST /api/flights/start` | as before, plus optional `start_on` | absent / `null` / `""` = `"now"`, today's behaviour unchanged. `"prompt"` or `"run"`: 202 `{flight, absorbed}` with the flight `ready` at cursor 0. Leading agent stops are **not** skipped; nothing is started or seeded. A fast-track absorb still happens: its ship rungs become the leading stops at index 0, and the fast-track run is disarmed as today. With no `title`, the session is created with **no prompt** (`body.prompt` is ignored). A bad value is 400 `bad_start_on`. |
| `POST /api/flights/{id}/attach` | as before, plus optional `start_on` | same rule: ready at cursor 0, `seeded` ignored, and a session created here gets no prompt. |
| `POST /api/flights/{id}/run` | — | ready → `running` at cursor 0, with history `took_off {by: "run"}`; the first stop starts on the next driver pass. A halt point on the first stop pauses it there. Errors: 409 `not_ready` (any other state), 404 `unknown_flight`, 400 `bad_step` with `field` (a stop not ready to fly). |
| `PATCH /api/flights/{id}` | `{start_on}` (optionally with `plan_name`) | ready only, else 409 `not_ready`. `"prompt"` / `"run"` change it. `"now"` runs the flight, the same as `/run`. A bad value, `null` or `""` is 400 `bad_start_on`. `{plan_name}` alone is the rename, unchanged. With both keys, the rename goes first. → `{"flight"}` |

##### Taking off on the next prompt (`start_on: "prompt"`)

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

###### After that turn (`detail.after_turn`)

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

##### MCP

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

##### Defaults unchanged

None of these pass `start_on`, so they fly at once as before: the Intake Fly
button, schedules (`template_schedule`), triggers (`flight_triggers`), the
handoff kind (`attach` with no `start_on`), intake `arm_pending`, and MCP
`start_flight`.

##### Frontend (types only)

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

#### Frontend (integrator)
- `isShown(f)` (lib/flights) = `isLive` or `ready`: the session line, the
  child summaries and `liveFlightOf` draw / use a ready flight; a ready
  flight's stops are all editable (like grounded) and `insertFrom` is 0.
- Built by hand in the Sky → `start_on: "prompt"`: a stop tacked onto a
  session with no flight (`tackOnNew(…, "prompt")`), a saved stop dropped on
  one (`takeOff` / `startFlightOn(…, "prompt")`), a stops-only chain dragged,
  wired or +-connected onto a session (`mergeGrounded` → `attachGrounded(…,
  {start_on: "prompt"})`). The Intake Fly, ⋯ Fly on a session…, the new
  session block, schedules and MCP still take off at once.
- The session tile of a ready chain (Sky and Flight tab): `ReadyHead` — a
  "Ready" chip, ▶ Run (`runFlight`), and how it starts ("starts on next
  prompt" / "starts on ▶ Run"; a press flips it via `setStartOn`); its
  subtitle is the session's own line. ~~"runs on your next prompt" / "runs
  when you press ▶ Run"; subtitle "ready · …"~~ (round-19 first cut, see
  below).
  - **Fixed 2026-11-03 (ready head layout)**: the head said "ready" three
    times, the text collided, and ▶ Run was invisible. The button wore
    `.sky-run`, and `.sky-chain .sky-run` (the free chain's hover-only ▶
    circle, `opacity: 0`) caught it. Now: the subtitle is the session's own
    line (`where` / task headline), never "ready · …", in SkyLine and
    FlightChain alike. Under the name sits ONE row "[Ready] [▶ Run]", the
    button `.sky-ready-run` (a pill; never `.sky-run`), and below it one
    small, muted, un-underlined line, `.sky-start-on` ("starts on ▶ Run" /
    "starts on next prompt"; it still flips `start_on`, and its title says
    what a click does). The head's subtitle is one line with an ellipsis
    (`.sky-head > .sky-node-sub`), its name is one line while ready, and its
    rows never shrink (`flex-shrink: 0`). `.sky-node-sub`'s `overflow:
    hidden` used to let it shrink in the 132×140 box, so the next row
    drew over it. Budget at 100%: tile 64 + name 24 + sub 15 + head 4 + 18 +
    1 + 13 = 139 of 140, so any extra row or bigger font overflows.
  - The start-on toggle's tooltip comes from the exported `startTitle()`
    (ReadyHead.tsx): "Starts only when you press ▶ Run. Click to start it on
    this session's next prompt too." / "Starts on this session's next prompt
    (or ▶ Run). Click to start it only on ▶ Run." It is a plain button, not
    `.linklike`: `.sky button.linklike` is accent-coloured and underlined.
  - **Rule for CSS work:** a control rendered inside `.sky-chain` gets its
    own class and never reuses `.sky-run` or another free-chain class. The
    chain's descendant rules (SkyChain.css: `.sky-chain .sky-plus`, the
    hover-only `.sky-socket` at `opacity: 0`) style whatever wears those
    names, wherever it sits in the chain.

## Round 20: a session is a frame, the agent is a tile; parallel stops — contract

The owner (2026-11-02): "we are weirdly conflating agent with session — I
often click on session to edit what the agent is doing; a flight chain has to
be connected to a session but I'm not sure a session block makes sense"; and
"I placed two notifications after the PR and it forced them into a line —
why couldn't they run in parallel?" Both answers chosen: **frame + agent
tile**, **parallel side by side**.

### A. Frames (task `frames`, frontend)
- A **session** is no longer a block: it is a **frame** — a soft rounded
  container (1px border in the session's colour at low alpha, faint fill)
  around its chain, its title bar the session's name · branch · state chip ·
  ⋯ (today's chain label, now the frame's top edge; still the drag handle;
  double-click the bar = rename). The frame's size = its chain's bbox +
  padding.
- The **agent** is the chain's first tile, inside the frame: icon = the
  agent kind's (sparkle), title "Agent" (or the provider's name: "Claude",
  "Codex"), summary = what it is doing now (the task headline / last prompt,
  2 lines), its state chip under it, the bird on it. A click opens the
  session's terminal in that tile's place (today's in-place card); its
  right-click menu: Open terminal, Send a prompt…, Stop, plus the session's
  menu. Its out port starts the chain (today's head port).
- **Sub-sessions** are small frames hung under their parent's frame (the
  dashed bus from the parent's agent tile to each child frame's top), each
  with its own agent tile (state, bird) and its own chain if it flies one;
  opening one opens its card in its agent tile's place (today's `big`).
- A **stops-only chain** has no frame: free tiles. Dragging it into a frame
  (or wiring into the agent tile's out side) attaches it (today's merge
  target is the frame rect).
- The pane's Flight tab (FlightChain) draws the same frame + agent tile.
- Tour (lib/tours/sky.ts) and SkyGuide wording follow ("frame", "agent").

### B. Parallel stops (task `parallel`, backend + graph model)
- A step may carry `parallel: {id}` (`p_<6hex>`): consecutive steps sharing
  it form ONE parallel group — they run side by side; the flight goes on past
  the group when every member is done (one failing: the group fails as its
  `on_fail` says — the first failure's).
- Only steps that don't use the session's agent or worktree lanes may be
  parallel: a kind is **parallel-safe** unless it types to the agent
  (`types_to_agent`), is `agent`, `ship`, `reviews`, `split`, `join`, `land`,
  `sync`, `loop`, or a `you` perch… (perch IS allowed: it waits on a person).
  The kinds dto gains `parallel_safe: bool`. A refused grouping is 400
  `not_parallel_safe` naming the kind, with "use a Split for agents".
- Driver: at a parallel group the cursor stands on its first member; every
  pass judges/runs every unfinished member (each keeps its own step state);
  `apply` advances past the group only when all are done/skipped.
- Routes: `POST /api/flights/{id}/steps {step, at?, parallel_with?: sid}`
  inserts a step into `sid`'s group (making one when `sid` has none); `PATCH
  …/steps/{sid} {parallel: null}` takes a step out; ops are `_editable`
  (pending only). Saved plans keep `parallel`.
- Graph (lib/skyGraph): members of a group are placed in a column (the first
  at the group's spot, the rest PITCH_Y below — unless pinned); wires fork
  from the stop before to each member (`fan`) and join from each member into
  the stop after (`join`); the + of a group trails its last member's row.
  wireIntent gains `parallel`: a wire dropped from X's out port onto empty sky
  while X already has a next → "add a stop in parallel with X's next"
  (palette, then `parallel_with`); dropped on a stop Y of the same chain with
  Shift → Y joins X's next's group.

### Round 20 A — as built (frames, r20-frames)

#### Round 20 A — a session is a frame, the agent is a tile (as built)

Worker `r20-frames`. Spec: docs/flights.md "Round 20" part A. The owner:
"we are weirdly conflating agent with session — I often click on session to
edit what the agent is doing". This round draws the two as two things.

##### What changed, by file

All rendering-layer: no graph model change. `lib/skyGraph.ts`,
`lib/flightDiagram.ts`, `lib/flights*.ts` and `useSkyWiring.ts` are untouched
(r20-parallel owns them).

- **SkyLine.tsx / SkyLine.css**: the line root (`.skl.skl-chainline.skl-frame`)
  is the session's frame.
  - `.skl-frame-box` is the frame itself: an absolutely positioned box behind
    the chain, 1px session-colour border at ~38% alpha, ~5% fill, radius 14,
    `pointer-events: none` (empty frame area still pans / marquees as empty sky
    did). Its left/top go negative when a tile is dragged left of / above the
    chain's origin (`graph.bbox.x/y < 0`), so the frame always holds every
    tile. The bar follows with `margin-left` (widens it) and `translateY` (no
    reflow, so `useChainEntry`'s dx/dy stay right).
  - The bar (`.skl-bar.skl-label.skl-frame-bar`, still `data-tour="sky-line-bar"`,
    still the drag handle) shows name · branch · state chip · where · age ·
    📌 · ⋯. Double-click renames: an alias via `setAlias`, a name another
    session has is refused (`nameTakenBy`), as the rail's row does. The Bird
    glyph left the bar (the bird is on the agent tile).
  - The head is the agent: `title` = `agentTitle(provider)` ("Claude",
    "Codex", "Agent" when unknown); `sub` = the task headline, else the create
    task, else the last prompt (or round 19's "ready · …"). "Where the flight
    is" moved to the bar.
  - Right-click on the agent tile (or a sub-session's agent) opens the agent
    menu (`useAgentMenu`): Open terminal / Fold the terminal, Send a prompt…,
    Stop (Resume while paused), then a "Session “name”" label line and the ⋯'s
    items (minus Open, which Open terminal already is). Stops keep their menu.
  - The ⋯ keeps "Open session" / "Fold the session back" (skyVisibility.test
    pins those labels — outside this worker's fence).
  - Sub-sessions: `subFramesOf(rows, title, flights, kinds)` → each child's
    agent name and its own chain's stops; `subFrameBoxes` feeds every child
    node's box to `chainGraph`'s `big` (an open card's box when open).
    `hangFromAgent(diagram)` re-hangs every `below` child from the session
    node (so the dashed bus leaves the agent tile) and restores the straight
    wire across the stop they were hung at. `branch` workers (spawned at one
    of the flight's stops) keep their stop: that is where they came from.
  - CSS fix: the "card open → hide the head tile" rule was a descendant
    selector, so the open card's own Flight tab lost its head tile. Now
    `.skl-chain-wrap.has-card > .sky-chain > .sky-head …`.
- **SkyChain.tsx / SkyChain.css**: the head of a session chain gets
  `sky-agent` + `data-agent`, the sparkle icon (`TileIcon kind="agent"`), a
  plain rounded tile (no session "pill" shape), aria "Agent: Claude — …".
  A `child` / `branch` node with a session draws as `.sky-subframe`: a bar
  with its name, its agent tile (sparkle in its own `chainColor`), agent
  name, summary, chip, its bird on the tile, and a `MiniChain` strip (one
  18px square per stop, state-coloured, capped at 8 + "+n") when it flies
  one. Sub-frame tiles draw no ports. The bus (`fan` into a sub-session) gets
  `sky-wire-bus` (dashed). New exports: `agentTitle`, `agentDoing`,
  `subFramesOf`, `subFrameSize`, `subFrameBoxes`, `hangFromAgent`,
  `isSubSession`, `SubFrame`, `MiniStop`, size constants.
- **useChainMenu.tsx**: `agentMenuItems`, `useAgentMenu` (StopMenu in a
  portal + a `PromptComposer`: textarea, Enter sends, Shift+Enter new line,
  Esc/press outside closes, clamped to the viewport), `sendPrompt` →
  `POST /api/instances/{title}/send {text, dialog_safe: true}`. Stop is
  `pauseSession` (the app's "stops the agent, the session stays"); there is
  no interrupt-only route on the server.
- **FlightChain.tsx / Flights.css**: the Flight tab draws the same — a
  `.fl-chain-frame` with its bar (name · branch · state · where) round the
  chain, the agent head, sub-frames (`useInstances` + `useFlights` for
  `subFramesOf`), `hangFromAgent`. A click on the agent (or Open terminal)
  switches the pane to its Agent tab (`setLastTab(title, "agent")`); a
  sub-session's selects it. Tiles dragged to negative spots shift the chain
  in by margin (no host bookkeeping here).
- **lib/tours/sky.ts, SkyGuide.tsx**: "A session is a frame" / "The agent
  starts the chain" / stops-only chains "with no frame"; the guide's Lines
  section and sketch describe frame, bar, agent tile, sub-session frames.

##### Kept working

ReadyHead on the agent tile; the Loop self-wire on the head (head id stays
`"head"`); wire drags from the agent's out port; merge targets (the drop
target is still the line's slot rect = the frame; the frame box lights);
node drags (sub-frames drag as before: `isBranchRow`); the in-place own card
and sub-session cards (`renderOpen`); double-click fold of an open card; tour
hooks (`sky-session-box` = the agent tile, `sky-line-bar` = the frame bar,
`sky-stop` now skips sub-frames, `sky-grounded`).

##### Tests

- New `src/__tests__/r20Frames.test.ts` (21 tests).
- Rewritten to the new behaviour: skyLine.test (frame classes, agent head,
  `hangFromAgent`, headSub, focus ring), skyChain.test (head classes),
  r18Sky.test (tour hook selector), tours.test (word list: "frame", "agent").
- Full run from a scratch copy: 311 files, 5845 tests passed; `tsc --noEmit`
  clean.

##### For the integrator

- **Rebuild the committed bundle** (`npm run build` in `frontend/`) — this
  worker was fenced out of `backend/web/static`.
- Source-text tests outside the fence pinned some strings, so they were kept
  rather than reworded: SkyLine's `aria-label={name + "'s chain"}`, the ⋯
  labels "Open session" / "Fold the session back"
  (skyVisibility/skyResize), and the literal
  `firstLine(row.task_headline || row.task || "")` (skyZoom).
- `endSession.test` forbids any `prompt(` call under src/, hence
  `compose` (not `prompt`) for the composer opener.
- Merging with r20-parallel: `hangFromAgent` reads `branch.from/to` and the
  `branch` edges only; parallel groups don't touch those. If skyGraph's
  `autoPlace` changes how branches lay out under the head, sub-frame sizes
  come in through `big` as before.
- Screenshots (scratch): sky.png (whole sky), c-close.png (one frame up
  close), sky-menu.png, sky-prompt.png, sky-open.png (terminal in the agent
  tile's place), c-flighttab.png (the Flight tab), m-merge.png (a stops-only
  chain dragged onto a frame).

### Round 20 B — as built (parallel stops, r20-parallel)

#### Flights round 20 B — parallel stops (integrator notes)

Spec: `docs/flights.md` → "Round 20: a session is a frame, the agent is a
tile; parallel stops — contract", part **B**. The owner put two Notify stops
after Open PR and they were forced into a line; now stops can run **side by
side**. This branch (`emandel2630/r20-parallel`) ships the backend and the
graph model. It changes **no UI component**: SkyChain / SkyLine / SkyView /
FlightChain / the palette belong to the frames worker. The last section lists
what they need to call.

##### The field

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

##### Parallel-safe kinds (`parallel_safe` in `GET /api/flights/kinds`)

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

##### Driver semantics

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

##### Routes

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

##### Frontend model

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

##### What the UI must do (frames worker / integrator)

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

##### Tests

- `tests/unit/test_flights_r20_backend.py` — 37 tests: the kind table and the
  dto, the normaliser, take-off / ground / attach / split-off, the pure
  planner at a group, the driver passes (`step_flight` with a fake server),
  and the routes.
- `frontend/src/__tests__/r20Parallel.test.ts` — 24 tests: lib/flights,
  flightsApi bodies, the diagram's fork / join / fold / layout, skyGraph's
  column / wires / + / wireIntent, and runWireIntent.
- No existing skyGraph or flightDiagram test had to change.

#### Integrator (same day)
- Both line hooks offer `paletteParallel(withNode, pos)` (its pick goes through
  `tackOnParallel`), handed to the host by `useChainEntry`; SkyView passes
  `kinds` to `useSkyWiring`; the Flight tab's own wire drop offers it too.
- A tile's right-click menu: "Run in parallel with the next stop"
  (`joinParallel`) / "Run on its own" (`leaveParallel`), on a stop still
  ahead whose kind (and the next one's) is parallel-safe.
- Parallel fork / join wires are solid (only the sub-session bus is dashed).
- Also: only a DONE flight lingers on its session's line (cancelled ones —
  old Loop tests — had reappeared); sub-session frames are left-aligned.

## Free chains: a default FOLDER, not a default session (2026-11-02)

The owner, after the default session shipped: "you still spawned a shell …
I really don't like that some sort of agent thing is required … I just meant
there should be a default dir we work in", plus the floating "▶ Run" label
over stops-only chains looked wrong. Taken from n8n:

| n8n | Sky before | Sky now |
|---|---|---|
| A workflow runs in the n8n process; nothing is started per workflow (Execute Command runs on the host) | ▶ Run made a `default` session (a zsh in tmux, or an agent) and moved the chain onto it | A chain that needs no session runs FREE: `session ""`, `free: true`, `repo_root` = the folder. Nothing is started |
| The workflow stays; a run is an execution of it | The chain became the session's flight and left the board | The chain stays where it is while it runs (bird, ✓s, countdown); when the run ends it goes back on the ground with `last_run {state, reason, at}` and runs again from the top |
| Run from the trigger: ▶ beside the trigger node / Execute workflow | "▶ Run" pill on the chain's label bar | ▶ on the LEFT of the first stop (SkyChain `run`, `.sky-run`): on hover at rest, ■ (stop the run) while it runs; the bar shows Running / Waiting on you / Last run done |
| A node not wired to a trigger just does not run | — | (unchanged: a split-off stop is its own chain with its own ▶) |

Backend: `attach {title: "@default"}` = the default folder
(`general.default_folder`, else `~/.mindflock/default`); `attach {folder}` =
that folder. `needs_session(flight)` = `needs_agent` or any stop in
`SESSION_KINDS − FOLDER_KINDS` (`FOLDER_KINDS` = command, loop): a chain that
needs one gets a NEW session in the folder (the old `title: "" + repo_path`
path) — never a reused "default". A free chain's command / loop stops skip the
session hold; `_session_obs` gives a sessionless flight its `repo_root` as
`folder` (commands run there). `_finish` re-grounds a free chain unless it is
really over (`_FREE_GONE`: replaced, last stop removed); `op_attach` on a
chain with `last_run` resets every stop to pending, cursor 0. `prune` never
touches a sessionless flight. Removed: `ensure_default_session`,
`general.default_session`; `GET /api/flights/default-place` → `{folder}`.
The picker says "Default folder" / "Run in <folder>".

## A source's default stops, edited in the Sky (2026-11-02)

**The rule.** The defaults are ours: every intake source (a ticketing source,
a repo's PRs, a repo's issues) flies our built-in stops, cut at its Fast-track
depth (`builtin:ticket@pr`…), until it names its own saved stop in its `plan`
field (`TicketingSource.plan`; `plan` in `github.repo_settings[repo]` /
`github.issue_repo_settings[repo]`). A ticket's flight is a COPY of that plan,
so editing one ticket's line stays local to that ticket. The calls below are
the ONLY way the Sky changes a source's default. Logic:
`backend/web/core/flight_intake_default.py`; routes in `routes/flights.py`.

A source is `{kind, target}`: `tickets` + the source's id (its provider as a
fallback, like every intake lookup), or `prs` / `issues` + `owner/repo`
(matched case-insensitively against `github.repos` / `issue_repos`, or the
settings table).

| Call | Does |
|---|---|
| `GET /api/flights/intake-default?kind=&target=` | `{kind, target, label, plan_id, name, own, steps}` — what a NEW item from the source flies now, resolved like an intake start (`server._intake_plan` with no per-start pick): its own plan when it exists (`own: true`), else the built-in cut at its depth. All empty (`plan_id ""`, `steps []`) when nothing flies (no depth, no plan). `label` is the lane's: "Tickets · Shortcut", "Tickets · Jira · Platform", "PRs · o/r", "Issues · o/r". 400 `bad_kind`, 404 `unknown_source` |
| `PUT /api/flights/intake-default {kind, target, steps, name?}` | Steps (validated strictly: 400 `bad_step` + `field`) become the source's own saved stop. Its existing own plan is rewritten IN PLACE (name kept unless `name`) when it is not a built-in / depth cut, runs on no trigger of its own (schedule, main push/red, hook) and no other source or repo names it; otherwise a new plan "<label> stops" (or `name`), trigger = the intake kind, is made and the source pointed at it |
| `PUT … {kind, target, plan_id}` | The source flies that existing plan (404 `unknown_plan`); `""` clears the field — back to our built-in |
| `PUT … {from_flight}` | That flight's steps (reduced to plan shape) as `{kind, target, steps}`. Its source: an editor chain's `edits_default`, else its `subject` (ticket → tickets + `subject.source`; pr → prs; issue → issues). 404 `unknown_flight` / `no_source` |
| `GET /api/flights/intake-default/for-flight/{id}` | The GET shape for that flight's source (mapped as above); 404 `no_source` for a hand-made flight or a source since removed |

Every PUT answers the GET shape plus `previous_plan_id` (the field before the
write, `""` when unset) so the UI can Undo with `{kind, target, plan_id:
previous_plan_id}`. PUT is owner only (`auth.may_configure`, like the Intake
dialog). The field is written the way the Intake dialog saves it —
`settings.set_ticketing_sources` (the whole list, every other field kept) or
`update_settings(github={repo_settings|issue_repo_settings: …})` (a block
whose only key was `plan` is dropped) — then stamped for settings sync
(`settings_sync.local_change`) and passed to `settings_hooks.
after_settings_change`, so other devices get it and the pipeline's fresh read
(`session_runner._fresh_settings`) sees it. Reads drop the per-process
settings cache first. Saving steps drops the group a grounded editor puts round
the stop it was seeded from (one wrapping the whole chain, or one labelling
the source's current default); other groups are kept.

**The editor chain.** `POST /api/flights/start {grounded: true, plan_id? |
steps?, name?, edits_default: {kind, target}}` stores a grounded chain with
`edits_default` (400 `bad_edits_default` when malformed; the flight
normalizer drops a malformed one from the store). It is never flown:
`validate_for_takeoff` and `op_attach` refuse it with 409 `edits_default`
("this chain edits a source's default stops; save it as the default
instead"), which covers `POST /attach` with a session, `"@default"` / a folder
(refused before anything is started), and a Handoff aimed at it. Adding,
moving and removing its stops work as on any grounded chain; Save as default
is `PUT {from_flight: <its id>}`. A split-off piece is a plain chain.

## Calm Sky objects; devices on drag; Finished on request (2026-11-03)

- **One look** for every Sky object (the intake lane, the Finished strip, the
  device targets): the session frame's — a hairline border, a 30px tinted
  title bar in normal case, quiet rows instead of filled cards, small muted
  kind tags, and text that scales like the frames' (no ballooning at far zoom).
- **Devices are drop targets only while you drag.** No standing device block:
  dragging a session's chain by its frame bar shows a dock along the top of
  the view — "Move to <device> · N sessions there" — and dropping asks inline,
  then calls `POST /api/instances/{title}/move` as before.
- **The Finished strip is off by default.** The Sky's ⋯ → "Show the Finished
  strip" (per device, `mf_sky_finished`); × on the strip hides it. Shown, it
  says what it is: each repo's main branch (its CI and last merge) and the
  chains merged today. Hidden, it is never placed, keeps no room, and
  `/api/sky/landing` is not polled.
- The board's off-screen pointers hide while any dialog or menu is open.

## As built: Tidy fills the screen (2026-11-03)

- **The rows are as wide as fills the window.** Every line with no spot yet
  (all of them, after a Tidy, but also any session arriving later) is placed
  by `assignSpots` at the wrap width `fillWidth` (lib/skyLayout) picks: it
  tries about two dozen widths, from the old minimum (the window's width, at
  least `LINES_LEFT + 900` plane px) up to every newcomer on one row, plus
  each width at which one more fits on the
  first row. It scores each layout by the zoom the toolbar's Fit would show it
  at (the lines at full size; the intake lane, the palette and the right
  column count for their width and top only, the column added right of
  everything) and keeps
  the largest. A wider width must show things at least 10% larger than the
  minimum does (`FILL_GAIN`), and a tie goes to the narrowest: a few lines,
  or a portrait monitor (which would gain a few percent from a second column
  under the lane), are placed exactly as before; a landscape monitor gets
  rows that fill it instead of a tall column with empty sides. The widest
  candidate is also at least the placed lines' right edge, so a session
  arriving on an already-wide sky can land in a row that wide. The placement
  memo re-runs on window height too: a newcomer whose spot is not saved yet
  can move when the window is resized vertically. Each recompute runs
  `assignSpots` about 24 + N times (N newcomers).
- **The score is close to Fit, not identical.** It counts the floating
  palette for its width and top (Fit leaves it out), so with the palette open
  the chosen width can differ slightly from what Fit would pick. (This
  session's one line never reaches `fillWidth`: it sits at the corner.)
- **Tidy ends on the whole fit.** The toolbar's Tidy and `T` arm a fit; once
  every shown line (and the lane) has its new spot, the camera eases to the
  whole fit (`fitAll(true)`, as Fit). Disarmed after 2 s regardless, so a line
  never measured can't jump the camera later. Tidy these (the selection bar)
  and the lane making way for lines it grew over leave the camera where it is.
- **Tuning knobs:** `FILL_GAIN` (1.1) and the 24-step count live in
  `fillWidth` in `frontend/src/lib/skyLayout.ts`; the 2 s Tidy-fit disarm is
  `tidyFitTimer` in `frontend/src/components/flights/SkyView.tsx`.

## As built: return wires explain themselves

Every stop that can send work back to the agent draws a return wire on the
Sky and in the pane's Route tab (docs/vision/fixwire.md): an arrowhead into
the Agent stop before it (else the session's agent tile) and the case on the
wire — "if pre-commit fails" (a Commit stop: what its lane really hands back
on the rungs it climbs), "if the checks fail", "if CI checks fail" (any stop
set to **If it fails: Let the agent fix it**), "if reviewers ask for changes".
Quiet until it happens; then the wire turns the warning tone with the real
reason and round ("pre-commit failed at ruff · fixing 1/2"), the stop says
"Agent fixing · round 1" and the bird sits on the agent; after, "fixed after 1
round" until the route passes the next stop. Loops say their case in words
too ("↻ repeat ×3 · every 30 min"), and every return wire has an arrowhead.

- Server: `flights.returns_of` (pure; the API adds it as `returns` — the store
  never does), `detail.fix = {active, round, rounds, of, reason, since, to,
  outcome}` on the step (`lane_fix` from a Ship stop's lane record, the
  `on_fail: agent` hand-off, a Reviews stop's rounds), one `fixing` / `fixed`
  history event each (`_track_fix`).
- Client: edge kind `fix` (`returnEdges`), routed and stacked with the Loop
  wires (`loopWirePathG`, shared lanes), `arrowAt`, `wireLabel` (inside its
  own run, cut or dropped, never overlapping).

### Done by the agent

A Ship stop whose rung git already proves is done (the agent committed,
pushed or opened the PR itself) is **done by the agent**: `flights.agent_did`
reads the session's git stage (`flight_driver._ship_git_obs`; a dirty tree
proves nothing) for a pending Ship stop before it arms its lane, and for a
running one whose lane has not acted, never while the agent is mid-turn. The
pass rolls on through every following Ship stop that is done too (Commit,
Push, Open PR in one pass), each with `detail.by = "agent"`, a receipt
("already committed · 3 commits", "already pushed", "PR #123") and one
history event "done by the agent — …"; the face shows the agent glyph and
"by the agent" under the receipt.

## Flow polish (2026-11-10): as built

A round to make routes and Verify easy to steer and follow. The current
model is in [Using Flights](#using-flights), [Verify stops](#verify-stops),
[Halt points](#halt-points) and the HTTP API table; this lists what changed.

**Waits and reasons.**

- A stop waiting on you keeps its own reason (`ss.note`, e.g. "2 steps for you
  to check, then confirm") in the bell and on the Sky, instead of a bare
  "needs you". The session row's `flight.step` carries it as `note`, plus
  `failed_reason` when the stop's failure was handed to you.
- A stop that hits an unexpected error fails at once — "<stop> hit an error:
  <one line>" (the traceback goes to the log) — instead of reading "carrying
  out …" for 10 minutes and running again. Its *If it fails: Let the agent fix
  it* becomes a halt: a server error is not the agent's to fix.
- A stop that waits on memory says why and for how long ("· waited 3 min")
  and parks on you after 5 minutes with Retry / Skip; a Verify check and a
  reviewer stop wait the same way. A reviewer that never started reads
  "couldn't start the reviewer: <why>"; Retry, Skip or Jump takes a queued
  one off the launch queue.
- A CI checks stop records `detail.unknown_since` while it can't read the
  checks, waits saying why ("can't read the CI checks yet: <why>"), and fails
  after 5 minutes of it: "can't read the CI checks: <why> — fix that and
  Retry, or Skip". The clock resets when the checks read again.
- A stored "Only when" that can't be read is kept and halts the stop: "this
  stop's 'Only when' can't be read — edit or remove it".
- A needs-you ping is keyed by the stop and the time it parked, so a stop that
  needs you again after Retry, Jump or a Loop rings again.
- Waits and reasons use one duration style ("1 h 30 min", "3 min"), "running
  <stop>" replaces "carrying out <stop>", and every failure reason names an
  action that works.
- A Notify stop's receipt says where it went: "sent to your phone", "shown in
  the bell — phone notifications are off", or "not sent: <why>"; the driver
  emits `flight.notified` and the bell lists it as "<title>: <text>".

**Verify stops.** The three Who words, the evidence in a failure ("— saw:
…"), blocked / memory parks that take Retry (and Done = check again), a
check that can't be read (parks blocked) or whose record is gone (fails,
"Retry starts a new one"), and a released run that parks on you instead of
"the check never started" are in [Verify stops](#verify-stops). A step the
agent left for a person lists "— the agent couldn't: <why>" in the confirm
items.

**Strict options.** A strict save or start (`POST /api/flights/plans`,
`/start`, `/check`, a step PATCH or add on a route in the air) refuses what
lenient loading would have changed silently, as 400 `bad_step` with `field`
and the sentence in `detail`:

- an option the kind doesn't have — "unknown option 'timeout' on a CI checks
  stop — it has: timeout_minutes" (the older names `minutes`, `wait_live` and
  `every_minutes` still read);
- a whole number that isn't one or is out of range — "count on a Split stop
  must be at most 20 (got 50)";
- a true/false outside true/false/1/0/yes/no/on/off; a choice not in its list
  (lists the server fills itself are not checked); a Wait over 14 days;
- a Call's `expect` other than a status, a class like `2xx` or a range like
  `200-299` (`2xx` now matches correctly); a Budget limit it can't read
  (`$2.50` reads); an Ask someone with neither a peer nor a webhook.

Lenient loading (a stored or synced plan) is unchanged. On a route with no
session yet, a stop's PATCH refuses a bad value too; only an option still to
be filled in (an empty prompt or command) may wait for take-off.

**Editing the stop the route is on.** `title`, `config`, `on_fail` and `when`
may change on the current stop when it failed (the route halted on it, or its
failure was handed to you) or when the route is paused before it with nothing
of it run yet. The stop is reset (its halt point counts as passed) and a
failure that was waiting on you becomes a halt, so the next Retry or Carry on
runs it as edited; history `edited` says so. The stop dialog offers it with
"Save, then Retry/Carry on runs it with these settings" and stays open after
Save.

**Starting.**

- `POST /api/flights/check` answers a start's questions without starting it,
  and `/start` runs the driver's pre-check (`flight_driver.preflight`) before
  creating anything — both in the HTTP API table above.
- Attaching to an existing session passes only its **leading Agent stops with
  no prompt** (one `skipped` history line each: "no prompt to send — the
  session's own work counts as this stop"); an Agent stop with a prompt is
  the author's instruction and is sent. A busy session's first Agent stop
  still runs on the work in progress.
- An ad-hoc route renumbers its stops `s1..sN`; a Loop's `back_to` and the
  start's `start_at` that name the author's own ids now follow them to the
  new ids (`adhoc_plan(id_map=)`).

**The Sky.**

- A route running with no session (a free run) can be followed and un-stuck:
  its chip reads Halted (with the reason), At your halt point, Paused, Needs
  you or Running; its stops carry Retry / Skip / Done / Carry on; its stop
  dialog opens read-only with those remedies; the Needs you view draws it,
  and pressing it in the drawer flies the camera to it.
- The Needs you drawer says why each route waits (the stop's note, its
  failure, or "Paused at your halt point"), shows "Loading…" on its first
  read and "Can't reach the server — this list may be out of date." on an
  error; "Nothing needs you." only after a read that worked.
- A running stop's tile shows what it is doing now (the server's note, up to
  60 characters); its settings move to the tooltip. A paused route's frame is
  a calm dashed outline. The tools are **Select (V)** and **Pan (H)**.
- Saving: a session's route menu has **Save as a route…**; a route with no
  session asks for the name first (an inline field, checked like a rename);
  a line that came from a saved route offers **Save to “<name>”** and **Save
  as a new route…**; a saved route's menu has **Edit its stops**, which puts an
  editable copy on the Sky (remembered per device, `mf_sky_route_edits`) with
  a bar reading "Editing “<name>”" and a Save that writes it back and takes
  the copy off. Every save's toast ends "— routes already running keep their
  own copy".
- With no saved routes the shelf says what a route is and offers **Start a
  route**.

**Stops and their dialog.** Every option has a hint with its default and range
(number fields are clamped to it); "If it fails" explains the pick; a stop's
form opens on one sentence of what the route will do there (its halt point,
"Only when" and "If it fails"); an Agent stop's CLI and Model show only with
"Start a new session for this stop"; a Wait stop greys How long once a time
is set. The dialog's layout, remedies and discard confirm are in
docs/web-ui.md.

## Team PR rules as a route: review modes and a required reviewer (2026-11-11)

Two generic knobs let a team encode its PR rules (its SDLC) as a saved route:
the rules themselves live in the route's step config, never in the repo.
Code: `flight_kinds/review.py`, `flights.py` (`STEP_KINDS["reviews"]`,
`_judge_reviews`, `_required_reviewer`), `flight_driver.py` (`_fix_note`,
`_reviews_obs`, `_reviewer_posted`, `_is_reviewer`); tests
`tests/unit/test_flight_review_modes.py`.

### `review` modes

`mode` (select, default `bugs`; anything else is a `PlanError` on
`steps[i].config.mode`, read as `bugs` leniently) picks the reviewer's brief
and the stop's default title:

| `mode` | title | brief |
|---|---|---|
| `bugs` | Second opinion | unchanged: correctness bugs only, "Ignore style, naming and refactoring ideas"; `focus` is an `Also: …` line at the end |
| `security` | Security review | a security reviewer with a checklist: authn/authz, secrets and credentials, injection (SQL, shell, template), SSRF, path traversal, crypto misuse, IAM / infrastructure-as-code / RBAC, unsafe deserialization, sensitive data in logs, dependency risk; only real, exploitable issues, each with file:line |
| `standards` | Standards review | reviews against the team's rules in `focus`, including code quality, observability (meaningful logging, metrics, dashboards / alerts), testing (unit tests plus evidence of local testing) and anything the rules demand; no "ignore style" |

A program still suffixes the title ("Security review (codex)"). In
`security` and `standards`, a non-empty `focus` is the governing rules: it
leads the brief, right after the opening, as "The team's rules for this
review (they govern it):" between `----- rules -----` markers — never as
"Also:". `standards` with no `focus` reviews against sound engineering
standards. `focus` keeps `MAX_TEXT` (8000) characters with its newlines, so
a whole rules document fits (a 5000-character one survives normalize, save
and the brief). Every mode ends with the same report contract (`No findings`
or a numbered list of ≤ 10 `file:line — …`), so `judge`, `block` and
`on_fail` work unchanged; the notes and the fail reason name the mode ("the
security review has 2 findings:\n…", "standards review in progress").

**Fix or rebut.** When findings go to the agent (`on_fail: agent`, and the
"send it to the agent" button), `_fix_note` replaces "Fix it" with
`review.fix_instructions`: each finding must be FIXED or answered with a
written rebuttal; each rebuttal goes in a commit message (an empty commit
will do), where the next reviewer reads it (`git log <base>..HEAD`); every
fix and rebuttal is recorded, numbered, for the PR's description; then the
route asks for a fresh review. The security / standards briefs say a
finding answered by a rebuttal in the commit messages is not reported
again (the `bugs` brief is unchanged). Other fails keep "Fix it".

### `reviews`: a required reviewer

`require_reviewer` (text ≤ 100, default empty) names a reviewer the PR must
hear from, matched as `_is_bot` matches: a case-insensitive substring of the
author's login (`coderabbit` matches `coderabbitai` and
`coderabbitai[bot]`). `reviewer_wait_minutes` (int 1..1440, default 10) is
how long it gets.

- **Has it posted?** `_reviews_obs` adds `reviewer` (True / False / None =
  GitHub could not be asked) while the stop has not yet seen it post: True
  when an actionable comment is its own, else `_reviewer_posted` reads
  `GET /repos/{repo}/pulls/{n}/reviews` (a review in any state — every
  inline comment belongs to one) and `GET /repos/{repo}/issues/{n}/comments`
  (where a bot posts its summary). The actionable-comment fetch alone cannot
  answer: it drops resolved / outdated threads, and a clean review has none.
  Once `detail.reviewer_seen`, it stops asking.
- **The clock** starts at `detail.pr_since`: the first watch tick with a PR
  (the later of "the PR opened" and "the stop started"), stored with the
  seeding.
- **Waiting**: until it has posted, the stop never settles as quiet (a short
  `quiet_minutes` such as 5 still waits for the first review), noting
  "waiting for <reviewer> to review (3m of 10 min)". New comments from anyone
  still go to the agent meanwhile.
- **It posted** → `reviewer_seen`, and `quiet_since` restarts there: the PR
  then settles after `quiet_minutes` more without new comments, so
  "`quiet_minutes: 5`" reads "wait 5 minutes after the bot's review".
- **It never came** within `reviewer_wait_minutes` → fail "<reviewer> never
  reviewed this PR — it needs a human review" ("could not tell whether
  <reviewer> reviewed this PR — …" when GitHub could not be asked); the
  stop's `on_fail` decides what then (`you` asks you).
- **Rounds exhausted**: new comments past `max_rounds` fail "comments still
  open after N rounds — it needs a human review" (with or without a
  required reviewer; it read "still getting review comments after N rounds").

`include_bots: false` still drops a bot's comments from the rounds; it does
not hide the bot from the required-reviewer check.

### Reviews: instructions for every round

Two optional fields on the `reviews` stop add to the prompt each round
sends the agent (`_send_reviews` passes them to
`pr_runner.build_consolidated_pr_prompt`). With both at their defaults the
prompt is the pipeline's, byte for byte.

- **`each_round`** — "Also, every round" (textarea, ≤ 8000 characters,
  trimmed, default empty): the route's own instructions, sent with every
  batch of comments under a `## Also, every round` heading, after the
  comments and before `## When done`. This is where a team's rules for each
  round live, not in MindFlock. For example:

  ```text
  Before the push, re-run your code review and security review on this
  round's changes and fix what they find.
  Reply on the PR to each comment you decline, giving the reason.
  If the branch conflicts with the base, merge the base in and resolve it.
  ```

- **`reply_declined`** — "Reply on the PR to comments it declines" (bool,
  default false): the "When done" list gains a step asking the agent to
  reply on the PR to each comment it decides not to act on (in the review
  thread for an inline comment), giving the reason, so the reviewer sees it
  on the PR and not only in the agent's summary. The pipeline's own PR
  runner never sets it.

The route editor's stop form shows both from `GET /api/flights/kinds` (the
text as a multi-line box); `FIELD_HINT` in `frontend/src/lib/stopEditing.ts`
explains each.

### The cookbook recipe

`flight_cookbook.RECIPES` gains `team_rules_review_bot_loop` ("self-review
against the team's rules, then a review-bot loop (CodeRabbit) on the PR"):
Work the ticket → Tests pass (agent fixes) → Standards review (`mode:
standards`, the rules in `focus`, agent fixes or rebuts) → Security review
(`mode: security`, agent fixes or rebuts) → Open PR → Review bot loop
(`reviews`: `require_reviewer: coderabbit`, `reviewer_wait_minutes: 10`,
`quiet_minutes: 5`, `max_rounds: 5`, `on_fail: you`) → CI checks (agent
fixes). The `review` / `reviews` cookbook lines name the new fields, and
`step_phrase` reads a review by its mode and a reviews stop with "(waits for
<reviewer>)".

## Run the PR review on an open PR (2026-11-11)

A team keeps its PR review — the rules-based reviews, a "Proof of Code
Review" section on the PR, the review-bot loop, a verdict, a perch to merge
or ask for review — as a **review-only saved route**. **Run the PR review**
flies it on an open PR, also on one a session is already working on. Code:
`core/pr_review.py` (`run_pr_review`, `find_pr_session`, `resolve_plan`,
`first_stop`), `core/flights.py` (`op_merge_plan`, `merge_point`),
`routes/flights.py` (`POST /api/pr-review`), `mcp/flights.py`
(`run_pr_review`); setting `flights.pr_review_plan`
(docs/configuration.md); tests `tests/unit/test_pr_review_action.py`.

**Which route.** The call's `plan` (a saved route's id, or its name, case
aside), else the `flights.pr_review_plan` setting; neither → 400 `no_plan`,
not on the shelf → 404 `unknown_plan`. Nothing in the code names a review
bot or a company: the rules live in the route's stops.

**Which session.** `session` names it. With only `pr_url`
(`https://github.com/<owner>/<repo>/pull/<n>`), `find_pr_session` looks, in
order, for a live session whose row shows that PR (`pr_url`, no GitHub call),
Intake's own session for it (`pr-<repo>-<n>`, same repo), then one whose
branch is the PR's head (`GET /repos/{repo}/pulls/{n}` with the app's token,
else the Intake monitor's lookup) in that repo (its origin). A session on the
same branch name in another repo is not it. The fixes are made by that
session's own agent in its own checkout: **no second session is ever put on
the PR's branch**. A paused session (409 `paused`) or one held offline after
a crash (409 `held`) is refused — start it first.

**One route per session**, so what happens depends on the session:

| The session | `mode` | What |
|---|---|---|
| has no route | `started` | `POST /api/flights/start`'s body (`flight_driver.start`) with `title`, `start_at` = `first_stop(plan)` and a `pr` subject (the PR it shows, when known). The work is already on the PR, so the route starts at its **first stop** — past a leading Agent stop with no prompt of its own (that stop stands for "the work"; flown, it would only wait for a prompt nobody types). An Agent stop with a prompt is where it starts: its prompt is sent. A route of nothing but empty Agent stops → 400 `bad_plan`. `stops` = the stops ahead. The session's own fast-track run, if any, is absorbed as with any start. |
| is mid-route | `inserted` | `flights.op_merge_plan` under the store lock (through `flight_driver._changed`: `flight.changed`, the driver woken). `stops` = the stops added; `[]` when the route already had them all. |
| none for the PR | `created` | `pr_review.launch(repo, n, plan=…)` — exactly Intake → Pull requests' **Begin review**: in the background, the PR head is fetched into the repo's base clone and a worktree is added on its head branch, as for a ticket (the PR's own clone only as a fallback); the route armed on the deterministic title first. `stops` = the route's. Its refusals pass through (404 no such open PR, 409 already starting, 502). |

### The merge rule (`op_merge_plan`)

1. **Where** (`merge_point`): right after the current stop — after its whole
   parallel group; after the first stop on a route not yet in the air
   (ready). When a `reviews` or `ship` stop, or a kind whose `risk` says
   `irreversible` (Land), is already ahead, in front of the **first** of
   them instead: the review runs and its fixes are made before the PR's
   comment loop, a ship rung or a merge, which then carry them on. Never
   strictly inside a parallel group.
2. **Dedupe**: a plan stop is left out when the route has a stop of the same
   `kind` and the same `config` that ran (done), is on now (running,
   waiting on you) or is still ahead (pending at or after the cursor) — no
   second CodeRabbit loop when one is already ahead. A skipped or failed one
   does not count: its copy is added. A Loop whose target was left out goes
   too. Titles are not compared.
3. **Ids**: every added stop takes the next free `sN` (never one the steps or
   the history name); a Loop aimed at another added stop follows it.
4. **Group**: the added stops are one new group named after the plan
   (`group.plan_id` = its id), as `POST /api/flights/{id}/steps {plan_id}`
   makes one; a parallel group that lost a member is tidied.

A route that is over → 409 `finished`; past 40 stops → 409
`too_many_steps`. Nothing new to add changes nothing (no history line).

### Where it is offered

`POST /api/pr-review` (docs/web-api.md, the frontend's action on a PR row and
on a session with a PR) and the MCP tool `run_pr_review` (docs/mcp.md), so an
orchestrator agent can run a review on a PR its worker opened.
