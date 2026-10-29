# The Sky: the intake lane and the new-session block

Two pieces of **the Sky**, the full-window canvas of Flights where every
session is a line and work starts from the white space. The owner's ask:

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
- **⋯** — opens the source on Flights → Sources
  (`openDialogFor("flights", "sources/<kind>:<id>")`).

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
