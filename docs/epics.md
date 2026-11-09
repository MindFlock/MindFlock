# Epics: one lead agent per epic

A Shortcut epic can be ingested like a ticket. Each epic becomes ONE **lead
session** that works **your** stories in it — side by side where they don't
depend on each other, one after another where one blocks another — and opens
**one pull request per story**, so a reviewer gets small, separate PRs instead
of one epic-sized branch.

## Turning it on

On a Shortcut source's card (Intake → Tickets → Edit):

| Field | What it does |
|---|---|
| **Epics in these states** (`epic_states`) | The epic-workflow states whose epics this source ingests (e.g. *On Deck*). Blank = no epics. |
| **Start epics by themselves** (`epic_auto`) | On: an epic in those states gets its lead automatically (checked every 2 minutes). Off: it is listed under **Intake → Tickets → Epics** with a **Start** button. |
| **Move epic to on start** (`epic_start_state`, advanced) | Optional: the epic state an epic moves to when its lead starts (e.g. *in progress*). |

The source's **Auto-start labels** gate epics the same way they gate its
stories: with any set, an epic auto-starts only when it carries one of them
(case-insensitive). An epic in the states without one is still listed under
Epics — saying "no auto-start label (…)" — and **Start** still works on it,
like a ticket outside the filter.

Only **your** epics (you are an owner) are ingested, and inside an epic only
the stories you own that aren't done are worked. Everyone else's stories are
given to the lead as context and never started.

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

* `GET /api/epics[?refresh=1]` → `{"epics": [{source, source_label, id, title,
  name, url, state, stories_total, mine_open, has_session, auto, labels,
  eligible, startable, reasons}], "errors": [{source, error}]}` (cached 60 s).
  `eligible` = would start by itself; `startable` = Start works.
* `POST /api/epics/start {"source", "id"}` → 202 `{started, title, stories}`;
  404 unknown source/epic, 409 a lead already works it / no stories of yours
  left / another server is starting it, 400 no repo.
* `POST /api/settings/ticketing/epic-states` → `{ok, states: [{id, name, type}]}`
  (the epic workflow, for the source card's pickers).

Two servers can share one config dir; an automatic start is claimed with an
exclusive file (`<config dir>/epics/<title>.claim`) so an epic gets one lead,
and an epic the loop started once is never restarted by it.
