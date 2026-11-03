# Words: the backend and the docs (vz-words-backend, 2026-11-03)

The user-facing strings the **backend** produces and the **docs** prose,
renamed to [docs/vocabulary.md](../vocabulary.md). Copy only: no identifier,
kind id, JSON field, API path, settings key, CLI command name or MCP tool name
changed.

## Old → new, per area

### Stop kinds (`GET /api/flights/kinds`: `core/flights.py` `STEP_KINDS`, `core/flight_kinds/*.py`)

| Where | Old | New |
|---|---|---|
| `ship` summary | Commit, push, open the PR or merge it — Fast-track carries it out. | Commit, push, open the PR or merge it when green — as far as you pick. |
| `ship` field `lane` | How far | Goes as far as |
| `verify` summary | Write the branch's checklist and work through it; the chain waits for you on your items. | Write the branch's behaviors and check each one; the route waits for you on the ones that are yours. |
| `you` summary | The chain waits until you are done… | The route waits until you are done… |
| `you` / `someone` field `items` | Checklist (one per line) | Things to tick off (one per line) |
| `ticket` summary | Move the chain's ticket… | Move the route's ticket… |
| `handoff` label / default title | Start a chain | Start a route |
| `handoff` summary | Start another chain on a session …, and carry on or wait for it to land | Start another route on a session …, and carry on or wait for it to finish |
| `handoff` fields | Chain to start · Wait for it to land | Route to start · Wait for it to finish |
| `land` label / default title | Land | Finish |
| `land` summary | … finish the flight, delete the branch … | … finish the route, delete the branch … |
| `budget` summary | Check the flight is within a budget …; with scope flight it keeps watching every later stop. | Check the route is within a budget …; checking the whole route, it keeps watching every later stop. |
| `budget` field | Most minutes in the air | Most minutes running |
| `loop` summary | Send the flight back to an earlier stop … | Send the route back to an earlier stop … |
| `someone` summary | … waits like a perch until they press Done. | … waits like Wait for me until they press Done. |
| `preview` field | Take it down when the flight ends | Take it down when the route ends |
| `ship` default title, lane `merge` | Merge | Merge when green |

### Built-in saved routes (`_builtin_defaults`, `_ladder_plan`)

| Old | New |
|---|---|
| ship stop titled **Merge** (Ticket to merge, Bot PR, Release, Bug report) | **Merge when green** |
| Release's last stop **Land**; description "… tag and publish, land." | **Finish**; "… tag and publish, finish." |
| Verify on live: "… work through its checklist." | "… check its behaviors." |
| Depth cut name "Fast-track to Open PR"; "The plain fast-track ladder: …" | "Goes as far as Open PR"; "The plain ladder: …" |
| Untitled chain · Ad-hoc chain · Flight on X | Untitled route · Ad-hoc route · Route on X |

### Receipts, history, waits, refusals (flights, driver, kinds, routes, triggers, intake default)

- every *chain* / *saved chain* in a refusal → **route** / **saved route**
  ("a route needs at least one stop", "no such saved route: X", "X is already
  on a route (…)", "this route has no session yet — run it on one first",
  "that is the whole route — move the route instead", "a route on the Sky
  needs plan_id or steps", …).
- "no such flight" / "no such plan" → "no such route" / "no such saved route";
  "cancel the flight first" → "stop the route first".
- Verify stop waits: writing the checklist → **writing the behaviors**; the
  agent is working through the checklist → **the agent is checking the
  behaviors**; N checklist items for you → **N behaviors for you to check**;
  checklist item failed: X → **behavior failed: X**; the bell text "Check N
  items on the checklist" → **Check N behaviors**.
- Ship stop waits: "Fast-track is busy with your own run" → "the session is
  already shipping by itself (Goes as far as)"; "Fast-track stopped" →
  "shipping stopped"; "Fast-track was turned off before X finished" → "Goes as
  far as was turned off before X finished"; history "taken over from
  Fast-track: …" → "taken over from Goes as far as: …".
- Finish (Land) receipts: landed / landed on main → **finished** / **finished
  on main**; handoff: "X landed" / "waiting for X to land" → "X finished" /
  "waiting for X to finish"; "X is already flying" → "… running".
- "waiting on you" (someone stop wait) → **needs you**; "X is not waiting on
  you" → "X does not need you now".
- Typed into the agent's terminal: `[MindFlock flight] X failed: … the flight
  retries the step` → `[MindFlock route] X failed: … the route retries the
  stop` (also the review and judge stops' prompts' prefix).
- Cancel reason "replaced by a new flight" → "replaced by a new route" (the
  old wording stays in `_FREE_GONE` so old records still match).

### Goes as far as (was Fast-track), outside flights

autopilot notes typed to the agent ("Shipping paused: …" / "Shipping
stopped: …", "MindFlock will then push … by itself", "That commit is as far as
this session goes by itself."), lanes / move_session / sleep / team runs /
templates / server refusals ("could not arm Goes as far as", "this session has
no Goes as far as of its own — pick how far it goes first", the reset
response's cleared item, the same-folder refusal "shipping by itself here
would commit both sessions' work as one"), the optimizer tips, the ticket
pipeline's log line ("will not ship itself").

### Behaviors (was checklist / test plan), Verify's server side

- `server.py` errors: "test plan not found: X" → "behaviors not found: X";
  "That checklist no longer exists." → "Those behaviors no longer exist.";
  "nothing on this checklist failed" → "no behavior here failed"; "couldn't
  save the checklist — …" → "couldn't save the behaviors — …"; every "this
  checklist" → "these behaviors"; the peer refusal says "Verify is not
  available for a shared-folder (peer) session".
- `test_plans.py` / `test_plans_driver.py` user lines: could not write the
  behaviors · writing the behaviors failed · "N behaviors to check, M for you."
  (phone push) · "These behaviors are waiting for main …".
- The model-facing prompts in `test_plans.py` keep "checklist" (they are
  instructions to the writing model, never shown).

### Notifications (`addons/notify.py`)

| Old | New |
|---|---|
| A checklist is ready / {session}: checklist ready | Behaviors are written / {session}: behaviors written |
| A checklist couldn't be written | Behaviors couldn't be written |
| A checklist is ready to check / Its checklist is ready in Verify. | Behaviors are ready to check / Its behaviors are ready to check on its route. |
| A checklist fails / {session}: checklist failed | A behavior fails / {session}: a behavior failed |
| An agent finishes a checklist / checklist run finished | An agent finishes checking behaviors / behaviors checked |
| A checklist run stops without answers | A behaviors check stops without answers |
| "open Verify" | "the Verify stop on its route" |
| A flight waits on you / A flight needs you | A route needs you |
| A flight finishes / Flight finished / "A flight landed, stopped on a problem or was cancelled." | A route finishes / Route finished / "A route finished, halted on a problem or was stopped." |

### Voice

- `voice_router`: the session list says "(needs you)"; "X is waiting on you:
  open the screen." → "X needs you: open the screen."; help detection also
  hears *saved route(s), the Sky, goes as far as, behavior(s), wait for me*
  (the old words still match).
- Phone page (`static/mobile.js`, hand-written source): "Waiting on you" voice
  strip word → **Needs you**; every "X is waiting on you" voice line → "X
  needs you"; the route card ("Route · …", "done — the route carries on"),
  "Paused route", the Needs you hint "route “…” needs you; press Done".

### CLI (`backend/cli.py`, help + output only)

`mindflock flights` / `mindflock flight …` keep their names (and `plans` /
`flyways`). Help says route / saved route / Wait for me / Stop the route; the
`FLYWAY` / `FLIGHT` metavars are `ROUTE`; the table header `FLYWAY` →
`SAVED ROUTE`; "(perched on you)" → "(needs you)"; "took off on" → "started";
"cancelled X" → "stopped the route X"; "* built-in (editable; reset it from
the saved routes shelf)".

### MCP (`backend/mcp/`, descriptions + titles only)

Tool names (`describe_flight`, `start_flight`, `flight_status`,
`control_flight`, `edit_flight`, `save_flight_plan`, `set_autopilot`) stay.
Their descriptions and titles say route / saved route / Wait for me / the Sky
("Describe a route", "Start a route", "Route status", "Steer a route", "Edit a
route's stops", "Arm a session's Goes as far as"); `start_team_run`,
`ship_session`, `set_autopilot`, `spawn_ticket_session` say Goes as far as;
the server instructions' line reads "Routes: describe_flight → …".

### Docs

- `docs/flights.md`: the top blurb, the Vocabulary section (now a pointer to
  vocabulary.md plus a key for reading the round sections' old words), "The
  idea" and "Using Flights" rewritten; the round contracts below are history
  and keep their words. Anchor `#how-a-ticket-becomes-a-flight` →
  `#how-a-ticket-gets-its-route` (the one in-file link updated).
- `docs/cli.md` (flights commands), `docs/mcp.md` (the routes section, now
  `#routes-from-a-sentence-to-a-running-route`; links in mcp.md and
  flights.md updated; Goes as far as throughout), `docs/team-runs.md` (title
  and `## Goes as far as`, anchor `#goes-as-far-as`; the **Off** rung reads
  **Agent only**), `docs/configuration.md` (the `plan` field, Goes as far as,
  the Verify repo keys say behaviors), `docs/ingestion-pipeline.md` ("Which
  route a started item takes", anchor updated in configuration.md),
  `docs/extensions.md` (the flight events' descriptions), `docs/people.md`.

## Deliberate keeps

- Internal names everywhere: `/api/flights/*`, `/api/hooks/flyway/*`,
  `/fast-track` route alias, `fasttrack_*` settings keys, kind ids, the
  handoff field's `dynamic: "chains"`, budget's `scope` option ids
  `flight` / `here` (the frontend labels options), event names
  `flight.needs_you` / `flight.finished`, `test_plan_*` events.
- The CLI command words `flights`, `flight`, `plans`, `flyways` (code).
- Model-facing prompts (`flight_describe.py`, the judge's question prompt,
  the test-plan writing prompts): never shown to a person.
- Log lines (`flight %s: …`, `test plan %s generation failed`, …).
- **Peer joint checklist** (`backend/peer/*`, `mcp/peer_tools.py`,
  `docs/peer-link.md`): a different feature from Verify's behaviors.
- "reply chain" (mailbox hop limit), auth/token/parent "chain" — not routes.
- The **Setup checklist** (first run) — not behaviors; owned by the firstrun
  worker.
- Developer docs that describe the code (`architecture.md`,
  `session-engine.md`, `web-api.md`, `providers.md`) and the round-history
  docs (`flights-loop-agent*.md`, `flights-reopen.md`,
  `queue-waits-on-workers.md`) keep their words.

## Outside my fence (for the integrator)

- `backend/web/core/settings_sync.py` (fenced): the synced-field labels
  "Fast-track depth" and "Fast-track: tell the agent what failed" are still
  shown in the settings-sync UI.
- `docs/web-ui.md`, `docs/development.md`, `README.md` (fenced) still say
  flight / chain / Fast-track where they describe these features.
- Voice help reads the docs by heading: "what does autopilot do" now finds
  team-runs.md's **Goes as far as** section (its synonym table learned
  *goes far*); web-ui.md's "Fast-track (⏩)" section still answers until its
  owner renames it.

## Frontend pins / fixtures that mirror the old backend copy

None of these fail (the vitest suite uses fixtures, not the server), but they
now disagree with what the server serves — update them with the frontend copy:

- `frontend/src/__tests__/r18Sky.test.ts:56-63,98` — the handoff kind fixture:
  label / default_title "Start a chain", field "Wait for it to land" → "Start
  a route", "Wait for it to finish".
- `frontend/src/__tests__/skyOnramp.test.ts:52,75` — `plan_name: "Flight on
  me"` (the server now names an ad-hoc route "Route on X").
- `frontend/src/__tests__/skyLooks.test.ts:45`, `skyVisibility.test.ts:41`,
  `skyResize.test.ts:140`, `stopGroups.test.ts:42,150,153` — ship stops
  titled "Merge" (the server's built-ins and its default title for lane
  `merge` are now "Merge when green").
- `frontend/src/__tests__/shellVerify.test.ts:49,66` and
  `components/NotificationsBell.tsx:250` — the bell builds its own "checklist
  failed — N steps" text (frontend copy; the server's push now says "a
  behavior failed").
- `frontend/src/lib/agentMessages.ts:583`, `lib/stage.ts:560` — frontend copy
  "Fast-track stopped: …" (the server's agent note now starts "Shipping
  stopped: …").
- Anything matching the receipt `"landed"` of the Finish stop: none found in
  `frontend/src` (it only reads `landed` as the landing-strip field, which is
  unchanged).
