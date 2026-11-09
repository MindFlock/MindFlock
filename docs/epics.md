# Epics: one lead agent per epic

A Shortcut epic can be started like a ticket. Each epic becomes ONE **lead
session** that works **your** stories in it — side by side where they don't
depend on each other, one after another where one blocks another — and opens
**one pull request per story**, so a reviewer gets small, separate PRs instead
of one epic-sized branch.

## The Epics tab

**Intake → Epics** lists every epic **you own** that isn't done, on every
Shortcut source — the way the Assigned-tickets panel lists every ticket
assigned to you, with no setup (`epic_states` doesn't have to be set). They
are grouped by source (each source gets a heading, even with no epics) and,
inside it, by epic state in the epic workflow's order. Each epic shows how
many of your stories in it are still open, and has a **Start** button: Start works on any epic of yours that no lead already works and that
still has stories of yours to do. Archived epics and epics in a *done*-type
state are not listed. An epic whose state the epic workflow doesn't name is
classified by its legacy state (*to do* / *in progress* / *done*). A source
whose epic workflow (`/epic-workflow`) can't be read shows an error line
instead of a guessed list.

## Starting epics by themselves

On a Shortcut source's card (Intake → Tickets → Edit):

| Field | What it does |
|---|---|
| **Epics in these states** (`epic_states`) | The epic-workflow states whose epics **auto-start** (e.g. *On Deck*), with `epic_auto` on. It doesn't limit the Epics tab: blank = nothing auto-starts, your epics are still listed. A *done*-type state here has no effect — done epics are never listed, so never auto-start. |
| **Start epics by themselves** (`epic_auto`) | On: an epic of yours in those states gets its lead automatically (checked every 2 minutes). Off: you start it from the Epics tab. |
| **Move epic to on start** (`epic_start_state`, advanced) | Optional: the epic state an epic moves to when its lead starts (e.g. *in progress*). |

The source's **Auto-start labels** gate epics the same way they gate its
stories: with any set, an epic auto-starts only when it carries one of them
(case-insensitive). An epic that won't auto-start — outside the states ("not
in an auto-start state") or without a label ("no auto-start label (…)") — is
still listed with the reason, and **Start** still works on it, like a ticket
outside the filter.

Only **your** epics (you are an owner) are listed, and inside an epic only the
stories you own that aren't done are worked. Everyone else's stories are given
to the lead as context and never started.

The 2-minute auto-start pass reads only the sources that can auto-start
(`epic_auto` on and `epic_states` set); each costs a `GET /epics`, `GET
/epic-workflow` and a story read for each of its first 12 epics.

## What the lead does

The lead session is titled `sc-epic-<id>` on branch `feature/sc-epic-<id>/…`.
Its first prompt (`backend/web/core/epic_start.py` `build_lead_prompt`) lists
your open stories with their Shortcut links (*blocks* / *blocked by* /
*relates to*), the rest of the epic as context, the epic's description, and
this playbook:

1. **Plan** the order: a story another one blocks waits for it, and so does a
   story that builds on another's code or touches the same files; the rest run
   in parallel. The plan is its first reply (a table).
2. **Start the independent stories at once**, one worker each:
   `spawn_ticket_session(ticket, source, autopilot="push", note=…)` — the
   ordinary ticket start (the story moves to its start state, gets
   `feature/sc-<id>/…`), going only as far as a pushed branch. The note
   carries the contract between the pieces and "message your lead before
   changing a shared interface".
3. **Start a dependent story** once what it waits for reported done, telling
   it to `git reset --hard origin/<predecessor's branch>` first.
4. **Open each PR itself**: `ship_session(depth="pr", base=…)` — the repo's
   PR base for an independent story, the predecessor's branch for a dependent
   one (a stacked PR shows only its own diff).
5. **Keep them working together**: relay contract changes, answer questions,
   keep a status table; wake on reports with `wait_for_session(mode="any")`.
6. **Finish** with story → PR → base, and the merge order of stacked PRs.

It never merges a PR, never pushes to the base branch, and never starts a story
someone else owns.

## API

* `GET /api/epics[?refresh=1]` → (cached 60 s)

  ```text
  {
    "epics": [{"source", "source_label", "id", "title", "name", "url",
               "state", "state_type", "state_rank", "in_states",
               "stories_total", "mine_open", "has_session", "auto", "labels",
               "eligible", "startable", "reasons"}],
    "sources": [{"source", "source_label", "epic_states", "auto"}],
    "errors": [{"source", "error"}]
  }
  ```

  Every not-done epic you own, per source. `state` is the epic state's name,
  `state_type` its type (`unstarted` / `started` / `done`, or `""`),
  `state_rank` its position in the epic workflow (to order the state groups).
  `in_states` = the state is one of the source's `epic_states`. `auto` = the
  source's `epic_auto`. `eligible` = would start by itself (in_states, an
  auto-start label when any are set, not taken, stories of yours left);
  `reasons` says why not, from exactly these strings: `"not in an
  auto-start state"`, `"a lead session is already working it"`, `"no stories
  of yours left to do in it"`, `"no auto-start label (<labels, comma-joined>)"`.
  `startable` = Start works (not taken, stories of yours left). `mine_open` =
  your open stories in it — read for the first 12 epics per source,
  auto-start-state ones first, then workflow order; `null` past that (a
  `null` doesn't block `eligible`, only `0` does). `sources` has one entry
  per epics-capable source, epics or none — also one that has an entry in
  `errors`. Its `epic_states` are state NAMES (the raw id where the workflow
  doesn't name it).
* `POST /api/epics/start {"source", "id"}` → 202 `{started, title, stories}`;
  any epic of yours on any epics-capable source (with or without
  `epic_states`); 404 unknown source/epic, 409 a lead already works it / no
  stories of yours left / another server is starting it, 400 no repo.
* `POST /api/settings/ticketing/epic-states` → `{ok, states: [{id, name, type}]}`
  (the epic workflow, for the source card's pickers).

**Provider contract.** A provider reads epics when it sets `can_epics`
(today only Shortcut) and implements `list_epic_states()` and
`list_epics(state_ids, mine)`. `state_ids=None` = every state that isn't a
*done*-type one (what the listing uses); a list = only those state ids; an
empty list = `[]` — so `None` and `[]` are not interchangeable. Rows carry
`state_type` (`unstarted` / `started` / `done`, `""` unknown). With
`state_ids=None`, a failed workflow read raises (the source lands in
`errors`) rather than guess which states are done; with named states it only
loses `state_type`.

Two servers can share one config dir; an automatic start is claimed with an
exclusive file (`<config dir>/epics/<title>.claim`) so an epic gets one lead,
and an epic the loop started once is never restarted by it.
