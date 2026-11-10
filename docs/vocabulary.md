# MindFlock vocabulary — the words a user sees

The one list of user-facing words, decided by the owner (2026-11-03; it
replaces `ux-lab/round-consolidate/VOCAB.md` of 2026-11-02). **Every** label,
tooltip, aria-label, toast, hint, empty state, tour/guide step, menu item,
error message the UI shows, notification, voice line, CLI help text, MCP tool
description and doc uses these words. Internal names — types, ids, CSS classes,
store keys, API paths (`/api/flights/*`, `plans`, `steps`, `/api/test-plans`),
the `#flyways` / `#flights/*` deep links, component and file names like
`SkyChain.tsx` — **stay**: rename copy, never code.

## The picture in one breath (what a first-time user must get)

> **MindFlock runs your coding agents side by side.** Each **session** is one
> agent working on its own branch. Its **route** is the stops the work goes
> through until it is merged — the agent, its checks, your OK, the PR. The
> **Sky** shows every route at once, and what **needs you**.

Sky in one line: **air traffic control for your agents — every piece of work in
flight, the route it's cleared to fly, and the ones that need you.**
Grid in one line: **inside the work — your sessions side by side, talk to the
agents.** Same sessions, two views (Ctrl+Y switches).

The test for anything new in the Sky: does it show where work is, set the route
a kind of work takes, or handle something that needs you? A stop must **move
the work, check the work, or ask a person**.

## The words

| Say (UI copy) | Means | Never say (in copy) |
|---|---|---|
| **session** | one agent working in one folder/branch (a worktree + an agent CLI) | instance, workspace (as a noun for a session), frame, line |
| **agent** | the coding CLI working in a session (Claude, Codex, …) | robot, bot, assistant (for the agent) |
| **route** | the row of stops a piece of work goes through — a session's route, or a route on the Sky with no session yet | **chain**, line, flight (as a thing), flyway, plan, pipeline, "stops-only line" |
| **stop** | one box on a route, every kind | block, box, tile, node, step (in copy), card (for a stop) |
| **group** | a stop with stops inside | Multi-step, sub-steps |
| **saved route** | a route kept on the shelf to reuse (ticket to merge, nightly chores, …) | **saved chain**, saved stop, flyway, plan, preset, template (for these) |
| **Route** (pane tab) | the session pane tab that shows its route | Chain, Flight (as the tab) |
| **Run** / **Run now** | start a route (or a template) now | Fly, Fly now, Fly these stops, Take off (as a button) |
| **Starts:** By hand / Ticket / Schedule / … | what starts a saved route | Takes off… (as a dialog/menu name; "takes off" may stay as a verb in a sentence) |
| **Stop the route** | cancel a running route | Stop the chain, Cancel flight |
| **Paused** (**Pause** / **Carry on**) | a person holds the route where it is; **Carry on** picks it up | Halt now, Halt (for a pause) |
| **Halted** | a stop failed and the route waits (red): **Retry**, **Skip** or **Let the agent fix it**, on the stop | Paused (for a failure), perch, recovery |
| **halt point** (**Add halt point** / **Remove halt point**) | a breakpoint a person sets on a stop: the route **pauses** before it until you press Carry on | Halt here, Carry on past here, Pause here |
| **Add a stop** · **Jump here** · **Retry** · **Skip** · **Let the agent fix it** | the stop actions | Tack on, Fly here |
| **If it fails:** Halt · Let the agent fix it · Skip · Ask me | a stop's failure option | on_fail, recovery ladder |
| **Wait for me** | the stop kind that waits for your OK | Perch, You (as the kind's name) |
| **Needs you** | everything waiting on a person (the bell's word) | Perched on you, Waiting on you, Needs attention |
| **Verify** | the session's own agent checking its own work — on demand from the session, or as a stop on its route — and the verb (2026-11-10: replaces "behaviors") | behaviors, checker, checklist, verify session, test plan; Verify dialog/tab (it is not a place) |
| **steps** | what Verify's agent checks, each with its result and evidence; a step only a person can do is **yours** | behaviors, check items, test steps |
| **Verifying** · **Needs you** · **Verified** · **Verify failed** · **Verify blocked** | Verify's states; Needs you's actions are **Confirm verified** / **Not verified…**; progress reads "3 of 5 steps done" | Checking…, Not checked, Writing…, partial |
| **Agent, then me** · **Agent only** · **Agent, then my sign-off** | a Verify stop's Who (`both` · `agent` · `you`): the agent does its steps then waits on you for yours · the agent attempts every step with evidence, waiting only if something ends blocked or failed · the agent verifies, then always waits for your confirm | Only me |
| **Goes as far as** | how far a session's work goes by itself: Agent only · Commit · Push · Open PR · Merge when green — it IS the last ship stop of the route | Fast-track (as a noun/control name), autopilot, depth |
| **Merge when green** | the top rung of Goes as far as | Merge (as the rung) |
| **CI checks** | the stop that waits for CI | Checks (as that stop's name) |
| **workers** | sessions a session starts to split its task | pieces, sub-sessions, children, Flock (as a button) |
| **Intake** | where work arrives by itself: tickets, PRs, issues — each **source** says which route its items take | inbox, ingestion |
| **Schedule** | sessions and saved routes that start on a timer | cron, scheduled stops |
| **template** | a saved way to start a session (folder, agent, task) | preset |
| **Finished** / **Merged today** | the strip of work that landed | Landing, Landed today |
| **Sky** / **Grid** | the two views of the main area | canvas, board (for the Sky itself) |
| **What's new** | the pop-up that explains a release's features | changelog, release notes (in the UI) |

Bird flavour stays where it explains itself — the bird that moves along a route
to show where the work is, "takes off" as a verb in a sentence, the Sky — but a
user is never asked to learn a bird word as a noun.

## Process for a copy change

Grep `frontend/src/__tests__` and `tests/unit` (bundle/source pins) for every
string you change and update the pins deliberately — many tests pin copy.
`backend/web/static/` is build output: workers never commit it; the integrator
rebuilds it once.
