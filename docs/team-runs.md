# Team runs and ship lanes

"Work on these tickets together, 3 at a time, PR each." A **team run** (a
*group* on screen) takes a few ticket IDs and task lines, starts one session
per item, keeps at most N running with the rest queued, carries each one as
far as its **ship lane** says, and surfaces only what needs you. The server
does the plumbing — spawning, queueing, retries, nudges, restart safety —
deterministically; an agent only does the work.

- [Ship lanes](#ship-lanes)
- [Starting a group](#starting-a-group)
- [What the server does on its own](#what-the-server-does-on-its-own)
- [What needs you](#what-needs-you)
- [Restarts](#restarts)
- [Limits, budgets, duplicate windows](#limits-budgets-duplicate-windows)
- [Notifications](#notifications)
- [One PR for all](#one-pr-for-all)
- [Splitting one task](#splitting-one-task)
- [Where it lives](#where-it-lives)

## Ship lanes

A lane is your answer to "when it's done, what then?":

| Lane | MindFlock, once the agent is done |
|---|---|
| `leave` | Nothing. The work stays in the tree for you. |
| `commit` | Commits it, with a message written from the diff. |
| `push` | Commits and pushes the branch. |
| `pr` | Commits, pushes and opens a pull request. |
| `merge` | …and merges it once CI is green. |

Plus **Ask me before it ships**: the session stops one rung short of its first
outward step (a commit lane before the commit, the others before the push)
and waits in the Outbox; approving it (`POST /ship-now`) ships it at once. A
commit lane that parks for approval has its message written from the diff
right then: the card shows the exact message, and approving it unedited
commits exactly that text. The card's size is what will be committed.

A lane is **not a second engine**. It is carried out by the fast-track
autopilot — the same record the ⏩ button arms, with its guards intact: the
30 s idle dwell, proof the agent worked, the usage-limit freeze, the
pre-commit retry allow-list, the push gate's check, CI before a merge. One
driver per session, whoever asked. `POST /api/instances/{t}/lane` sets one on
any session (`/fast-track` is its alias, and never drops an "ask first"
someone chose); every row carries `lane: {target, ask_first, owner, by}` —
`by` is who chose it (`user`, or `agent:<title>` for an agent's
`set_autopilot`).

**Nothing ships beyond what you chose for that session or group:**

- A session with a lane gets its own worktree (the New dialog never creates
  it in place), and a lane on an in-place session whose folder another
  session shares is refused (409 `shared_with`): it would commit every
  sharer's work as one.
- A copy window on the same branch shows the lane of the window that drives
  it (`owner`) and is never armed: `/lane`, `/fast-track` and `/ship-now`
  answer **409** on it (naming the driver; "Leave it" is always allowed).
- **Ship it now** ships the lane the row showed (the UI sends it) or the
  session's own record — never the Settings fast-track default: a session
  with no lane of its own is refused (409).
- A group's **lead** ships once, through the group's release, and a
  **one-for-all member**'s work is the group's one PR: neither takes a lane of
  its own (409; the Ship menu says so). A **paused** group's member cannot be
  shipped now; a lane set on it while paused is recorded and armed on resume.
- A lane (and "ask first") you set on a member of an each-their-own group is
  recorded on the task (`task.lane`, `task.ask_first`): every re-arm — a fix
  after a failed hook, a resume, a retry — keeps it.
- An agent (MCP) never raises a lane you set, never lifts your "ask first",
  never ships a group's member or lead, and never releases or resumes a
  group you started (see [mcp.md](mcp.md)).

## Starting a group

From your own Claude (the MindFlock MCP): `start_team_run(items=["PAY-412",
"PAY-415", "Per-user rate limit on /webhooks"], lane="pr", concurrency=3)`,
then `wait_for_run(until="needs_you")`. See [mcp.md](mcp.md#start_team_run).

Over HTTP: `POST /api/runs/preview {text}` parses (one thing per line; a line
of only ticket IDs is that many tickets), resolves each ticket against the
Intake ticket list and then each configured source, and says which items
already have a session. A ticket that resolves nowhere is an error on its row
— never silently turned into a task. `POST /api/runs` starts it. See
[web-api.md](web-api.md#team-runs-and-the-outbox).

- **Tickets** start exactly like Intake's Begin work (`ticket_start.launch`):
  a provisioned session on the ticket's own branch, its source's agent CLI.
  They are **reserved** in the ingestion ledger when they are *queued*
  (`in_flight`, tagged `reserved_by: "run:<id>"`), so the pipeline never also
  picks one up. A reservation is handed back — exactly that one entry, never
  the ticket's history — on every path that drops a queued ticket: cancel,
  skip, a create that kept failing, a group whose lead could not be created
  or was removed. A ticket the pipeline (or another group) is already
  starting is left out with a warning; one that is started elsewhere while
  queued is skipped, never armed with the group's lane. The pipeline's
  startup reaper keeps a live group's reservations and hands back a gone
  group's.
- **Task lines** start like the New dialog (`session_create.create`): a
  worktree of `repo_path`, titled from the line ("per-user-rate-limit-webhooks")
  — never a title whose branch a closed session left behind (it would start
  on, and ship, those old commits).
- A ticket or title **that already has a session is adopted**, not restarted.
- Every prompt ends with a short server-owned brief: do only this task, stop
  when it is done and tested, don't push or open PRs yourself — MindFlock does
  that per the lane.

The sessions are **MindFlock's**, not the calling agent's: no parent, not in
its managed set, so an orchestrator cannot steer or kill what the run owns.

## What the server does on its own

A loop (`core/team_run_driver.py`, every 5 s or right after a change) re-derives
every task from what is really there and moves it along:

```
queued ─start─▶ starting ─row seen─▶ working ─autopilot acting─▶ shipping ─lane reached─▶ shipped
starting ─create failed─▶ queued after 30 s, 60 s ─▶ failed
working ─dialog─▶ needs_you(prompt) ─answered─▶ working
working ─idle with proof of work, no progress 10 min─▶ nudge ×2 ─▶ needs_you(stuck)
shipping ─hook failed─▶ fix prompt + re-arm ×2 ─▶ needs_you(ship_halted)
any ─you deleted the session─▶ cancelled (never re-created)
one for all: committed ─▶ integrating ─merged (ancestry)─▶ integrated
             integrating ─conflict─▶ the lead resolves | needs_you(conflict)
```

The record keeps **facts and intent** — reserved titles, the incarnation
(the session's `created_at` when the run started it), attempt and nudge
counts, PR urls, terminal states — never a position in a chain. That is what
makes first start and resume-after-restart one code path (the autopilot's
rule).

- **Concurrency.** Active = starting, working, needs you, shipping. A task
  waiting on you holds its slot on purpose: it keeps parallel prompts down
  while you are away. *Start now* jumps the cap once.
- **Nudges** go through the prompt queue (which never types into a dialog):
  *"If you're done, make sure tests pass and stop; if you're stuck, say
  what's blocking you in one line."* Only after the agent's work was
  corroborated in this incarnation (`agent_state.worked_at` — a CPU blip
  never stamps it), 10 minutes idle with no new diff, commit or report, and
  each nudge actually delivered.
- **A failed commit** (a hook, a check) is handed back to the agent once per
  attempt, twice: *"MindFlock's commit failed at `mypy`: …. Fix it, keep the
  change scoped to this task, then stop."* — then the lane is re-armed. What
  only a person can fix (no origin, on the base branch, a red zone, no GitHub
  credentials, a rejected push) escalates at once. A commit that died on git's own
  `index.lock` (another git process held it for a moment) is not a hook
  failure: the autopilot retries it, and only a lock that never clears
  escalates. A message you edited on the approval card survives every re-arm
  (a fix, a resume, a retry); the task line is only a placeholder, replaced at
  commit time by a message written from the diff — and never previewed as the
  message (nor is a subject a model wrote for an earlier commit). A pause keeps
  an edited message on the task and the resume commits it as written. Red CI
  on a merge lane is *shipped, not merged*.
- **The agent never answers a permission prompt for you.** Neither does the
  run.

## What needs you

Everything that does lands in the Outbox (`GET /api/outbox`): the
sessions on a dialog (any session, grouped or not), ask-first lanes waiting
for your go (with the commit message and diff stat), and a group's
escalations — stuck after two nudges, hooks failed twice, a session gone
after a restart, a create that failed after retries, the budget spent — each
with its next action (Retry, Retry fresh, Skip, Open, Raise budget).

Controls (routes, or `control_run` from the MCP): **Pause** (nothing new starts,
merges or ships — not even a session whose create was already under way, nor
in the pass the budget is crossed; a releasing lead is disarmed and the group
goes back to "ready to release"; agents keep working; resume re-arms exactly
what it held, never a member already merging back), **Cancel** (stop starting
and shipping; sessions and branches stay), **Retry** (only the task's own
session — a title an unrelated session took is refused, use Retry fresh;
fresh: a new `-2` title on a new branch, the old one kept and its lane
disarmed so it ships nothing; a vanished one-for-all member's committed
branch is merged back rather than forked around), **Start now**, **Skip** (a
running task is detached: its session keeps running and keeps its lane),
**Add lines** (to a one-for-all group they fork off the lead; refused while
its plan is pending or its release running; a checking or ready group goes
back to running so the release never ships without them), **Adopt** a live
session.

## Restarts

On boot the driver reconciles every run once, **silently** — what was already
standing is not re-announced; what the reconcile itself finds (a member gone
after the restart) is said once, after the boot quiet window:

| Stored | Observed | Result |
|---|---|---|
| starting / working / shipping | its row, same `created_at` | carries on (the autopilot record carries on by itself) |
| any | the title, a different `created_at` | needs you — "the title is used by another session"; never adopted silently |
| starting | no row, a provisioning row | waits |
| starting / working | no row, the branch exists | needs you — Re-create it on the existing branch (no double spawn) |
| starting | no row, no branch | retries the create (with backoff) |

A lease file per run means a second server (the desktop app plus a CLI
server) only reads it. The lease records its process: a restarted server
takes over a dead process's lease at once (and the old one hands its leases
back on shutdown), so the boot reconcile always runs; the run store's lock is
a file lock too, so two servers never both claim a run or lose each other's
updates.

## Limits, budgets, duplicate windows

- **Usage limits.** While a member is limited, the group starts nothing new on
  that CLI (`waiting_for_usage`); the stuck clock stops. The existing resume
  watcher brings members back.
- **Budget.** When the members' summed cost reaches `budget_usd` the group
  pauses and asks once ("Raise to $N" = resume with a new budget).
  Per-session budgets still hold the autopilot as before.
- **Duplicate windows** (`foo` + `foo-copy`) share one branch: ownership is
  keyed on `(repo, branch)`. Adopting a branch another group owns is refused;
  the copy shows the owner's lane and is never armed; the Outbox lists the
  branch once.

## Notifications

One emitter (the driver) feeds every channel at once. `run.needs_you` fires
once per (run, task, reason, incarnation) — persisted, so a restart never
repeats one — and only for escalations the group originated: a dialog is
already announced by `needs_input`, an approval is something you asked for.
Each carries its announce `key` (a group's ask is keyed per round: a plan's
second round, a new release head, a later check failure, a second budget
pause), which the bell dedupes on.
`run.finished` fires once per group, after the boot quiet window even if it
finished while the server was down, and says what the group did — a
one-for-all group "one PR opened", "its branch was pushed; the PR was not
opened", or "N merged into one branch, nothing pushed", never "N PRs". Rules: `run_needs_you` and
`run_finished` (on), `run_task_shipped` (off).

## One PR for all

Pick **PRs: One for all** (`policy.grouping: "together"`) and the lines are
worked on in parallel but ship as **one** branch and one PR:

1. MindFlock first starts the group's **lead**: a session of its own
   (`<group>-lead`, a worktree of the repository) whose branch everything
   merges into and whose agent is there to resolve conflicts. Every line then
   starts as a **worker of the lead** — `parent` = lead, cut from the lead's
   current commit (`base_ref`), its diff measured against the lead's branch —
   so the rail draws the group as one family, and each worker's
   `report_result` lands in the lead's Thread.
2. A member's lane is always **commit**, whatever the group's: its brief asks
   it to commit on its branch and report "done" with a `Tests:` line — but a
   member that committed and then sat idle for a minute on a clean tree is
   done whether or not it reported (its armed record mutes the turn-end
   event, so the server checks its commits beyond its base itself). So a
   one-for-all group's lane is never `leave` (refused: it would commit
   unasked) and its "ask me first" is the group's release, which asks. Done is
   a done report (or a turn that ended) on a clean tree with commits beyond
   its base — or the autopilot finishing the commit for it.
3. Done members join the **merge queue**: one at a time, oldest first, and
   only while the lead is idle (or its CLI exited — a merge is plain git),
   its tracked tree clean, no merge or rebase half-done, and on the group's
   branch. That branch is **pinned** when the lead is created: a lead that
   checks out something else (or detaches) is not followed — merges,
   ancestry, new members and the release all stay on the group's branch.
   The server runs `git merge --no-ff` in the lead's worktree
   (`core/git_merge.py`) and marks the member **merged back** only once its
   branch head is in the group branch's history — ancestry, never anyone's
   word. A merge the server itself left half-done (it died mid-merge) is
   unwound on the next pass.
4. A **conflict** is aborted (the lead's tree is left exactly as it was) and
   handed to the lead as a message naming the files and the task id; the
   queue waits. The lead merges it itself, resolves, commits and calls
   `report_integrated` (accepted only from the lead, for a member waiting to
   merge that has commits of its own — or MindFlock simply notices the
   merge). Two hand-offs that end with the lead idle and nothing merged, a
   conflict in a red-zone file, or a lead blocked for 10 minutes (a dirty
   tree, a MERGE_HEAD it left and stopped on, a rebase, the wrong branch, its
   agent offline with a conflict to resolve) → **needs you**; Retry puts it
   back in the queue. A lead that is gone (lost, not removed — removing it
   cancels the group) for 10 minutes is a group-level ask with *Cancel the
   group*.
5. Everything merged → the **check** runs on the merged branch (the repo's
   `.mindflock.toml` `check_command`; with none the card says so). A pass is
   credited only to the commit the check started on: a commit landing during
   (or after) it runs the check again. A failure
   goes back to the lead twice ("make the suite pass, commit, stop"), then to
   you (Outbox → Run the check again).
6. **Release** — the ONE outward step, and yours: the lead's Thread shows the
   ship card (title, base ← branch with the diff, "one commit per piece, kept
   as written + N conflict fixes by the lead", the body). Its buttons follow
   the group's lane: **Open the PR** never merges (only the explicit **merge
   when checks pass** does — the primary on a merge group, next to *Open the
   PR only*), and a push group's button pushes the branch. The release ships
   exactly what the card and the check showed: it is refused while the lead
   is mid-turn, has uncommitted or half-merged work, sits on another branch,
   or the group is paused; a HEAD that moved since the check sends the group
   back to checking. It arms the lead's lane through the autopilot,
   with a server-built title (the group's name, then what each member did)
   and a body with a section per member — what changed (its report), its
   paths, its commits, the tests it ran — plus the conflict fixes and the
   check. No `gh` and no token: the branch is pushed and the card links
   GitHub's prefilled compare page (a non-GitHub origin has none: the card
   offers Copy title / Copy body); the lead's record is finished, not
   left as a red ✗. A lead whose `origin` is a **folder on this machine** (a
   provisioned workspace cloned from a checkout that has no forge remote of
   its own) can only push there: the card says so before the click, offers
   one button (*Push to the local folder*), the release arms `push` whatever
   the lane, and it ends in a hand-off naming the folder — "pushed
   `<branch>` to `<folder>`, a folder on this machine, not GitHub, so no PR
   was opened" — with Copy title / Copy body, never a PR it did not open
   (`release.local_origin`). The PR title is built from whole words — never a cut
   name or a literal "…". A group whose lane is `leave` or `commit`
   ends right after the check — the merged branch is the result, nothing is
   pushed. `policy.release: "auto"` (API only) releases without the click.

The lead's rail chip reads **`→ PR?`** while the release waits on you, and the
Outbox lists it under Waiting on you.

## Splitting one task

Tick **Split a big line into parallel pieces first** (or *Split into parallel
pieces…* on a session — that session becomes the lead) and the group is a
one-for-all group whose lines come from the lead's plan:

1. The lead gets the task plus a server-owned brief: read the code, commit
   shared groundwork, and **propose** 2–8 pieces with
   `propose_run_plan(run_id, pieces=[{title, prompt, paths}], why)` — never
   spawn sessions. The group waits in `planning`.
2. The server **validates** the plan against the lead worktree's
   `git ls-files` and red zones: no two pieces may share a file (nor a new
   literal path one names and the other's globs cover, nor two globs that
   can match one new path), a piece may not sit
   wholly in a red zone, at most `MINDFLOCK_MAX_CHILDREN` pieces. Problems go
   back to the lead to fix (`422`); a good plan is `plan_ready`.
3. **You approve** — one click: *Start N workers* on the plan card in the
   lead's Thread (each piece with its prompt and an `only here:` chip; Edit
   is inline), or *Approve* in the Outbox. *Ask for a different split* sends
   it back with your note. Approval refuses a lead with uncommitted changes:
   workers fork from its last commit.
4. The server starts every piece at once as a worker of the lead and
   **fences** each to its paths — worktree green zones ("edit only here"),
   the red/green-zone guard enforcing them; tests and lockfiles stay
   writable, red always wins. The fence lands just after the worker starts,
   so nothing it changed before is exempted (those are breaches); a piece
   none of whose paths could be fenced stops shipping and needs you.
   *Split…* on a session turns that session's own lane off: the group ships
   it once, at the release.
5. From there it is the one-for-all path above: committed, merged back one at
   a time, conflicts to the lead, the check, and your release.

**The lead's repository is its worktree's**, never its `Path`. A ticket
session (the ingestion pipeline, Intake *Begin work*) is created with
`path="."` — its `Path` is the server's cwd — and its worktree is a
provisioned one. So the pieces' repository is read from the lead's worktree
(`git rev-parse --git-common-dir`, `git_merge.repo_of`): MindFlock's
`_base_<repo>` clone for a worktree-strategy lead, the lead's own clone for a
clone-strategy one, the picked repo for a plain session. The pieces are plain
worktrees of that repository, cut at the lead's HEAD, and merge back into the
lead's own branch — a ticket lead stays on `feature/sc-<id>/…` and keeps its
ticket-ledger entry. Branch-taken probes, the group's `repo_root`, and a
one-for-all ticket line's same-repository check (which also accepts the base
clone's origin, clone source and `_base_`-less name) all use it; a piece's
rail row names the repository, not the `_base_` folder.

A lead whose launch lost its MindFlock tools (after a restart) cannot propose:
its Thread says "Restart the lead to give it the MindFlock tools".

## Where it lives

| What | Where |
|---|---|
| Lanes | `backend/web/core/lanes.py`; routes `/lane`, `/ship-now`, `/fast-track` |
| Run record + planner (pure) | `backend/web/core/team_runs.py` |
| Driver, reconcile, operations | `backend/web/core/team_run_driver.py` (loop registered in the lifespan, off under pytest) |
| Outbox | `backend/web/core/outbox.py` |
| Starting sessions | `backend/web/core/ticket_start.py` (`launch`), `backend/web/core/session_create.py` |
| Merge-back | `backend/web/core/git_merge.py` (merge, ancestry, subjects, diff stat) |
| MCP tools | `backend/mcp/runs.py` (incl. the lead's `propose_run_plan`, `report_integrated`) |
| Store | `~/.mindflock/runs/` (`MINDFLOCK_RUNS_DIR`) |
| Tests | `tests/unit/test_team_runs_*.py` (incl. `_split`), `test_team_run_driver.py`, `test_team_run_split_driver.py`, `test_team_run_ticket_lead.py`, `test_team_run_events.py`, `test_git_merge.py`, `test_outbox.py`, `test_lanes.py`, `test_mcp_team_runs.py` |
