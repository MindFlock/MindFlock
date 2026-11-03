# Behaviors in the Sky (the Verify redesign) — contract

2026-11-03. The owner: "Verify is a very confusing thing even for me, the
creator of the app. It may be better suited to being more integrated with the
Sky instead of being a tab on the top. A redesign with more of a focus on
behaviors in the Sky."

## What was wrong

- Verify was a **separate place** (top-bar button, Alt+V, a big dialog) for
  something that is a fact *about one session's work*. You left the work to
  look at its checks, then came back.
- **"Checklist" meant four things** (a Verify plan, the Setup checklist, a Wait-for-me
  stop's tick items, the peer joint check) and "test plan" survived in places.
- **Six groups, nine verdict keys, seven button actions**; two on/off switches
  (dialog + sidebar bar); the same verdict in five places; eight ways in.
- **The chain and the dialog fought over one checklist**: a Verify stop parked
  on you showed a bare "Done" that passed every item unseen ("Done on the
  chain"), and a chain parked on Verify was counted on Verify's badge, not the Sky's.

## The idea in one sentence

**A session's behaviors live on its frame in the Sky**: what this work should do
in real use, each one checked by the agent or by you, drawn as the last tile of
the session's chain, opened in place, answered in place.

## Words (copy only; internal names — test_plans, /api/test-plans, TestPlan,
verify_enabled, the `verify` stop kind — stay)

| Say | Never say (in Verify copy) |
|---|---|
| **behavior** — one thing the work should do, checked in real use | step, check item, test step |
| **behaviors** — a session's set of them | checklist, test plan, plan |
| **Verify** — the stop that checks them, and the verb | |
| **checked by the agent** / **checked by you** | actor, human |
| statuses: **Writing… · Not checked · Checking… · Needs you · Failed · Verified** (+ "Verified live") | Not written yet, In progress, Not verified, partial |

US spelling: **behavior**.

## 1. The Behaviors tile (on the session's chain)

- Every session frame whose branch has behaviors (a `TestPlan` matched by
  `planForSession`, `lib/behaviors.ts`) draws a **Behaviors tile** as the LAST
  tile of its chain — after the last stop, or right of the agent tile when the
  session has no chain. It is wired like any stop (a plain wire in), but it is
  not a flight step: it is derived from the plan, it never runs, it cannot be
  dragged off or deleted from the chain (its ⋯ has "Delete these behaviors").
- If the chain HAS a Verify stop, that stop IS the tile (no second one); the
  Verify stop's tile shows the same summary.
- Tile face: ✓-shield icon, title "Behaviors", sub line = `summary.short`
  ("3 of 5 verified", "Needs you · 2", "Failed · 1", "Writing…", "Not checked"),
  tone class `is-<tone>` (quiet | busy | you | bad | ok).
- The **frame** carries the tone too: a thin red top edge when failed; the
  session's Needs-you bell (the existing waiting mark) when a behavior waits on
  you.
- **Click the tile → the Behaviors card opens in the tile's place** (the same
  "open in place" rule as a sub-session card; Esc / × folds it back).

## 2. The Behaviors card (`components/behaviors/BehaviorsCard.tsx`)

The ONE editor/answerer — replaces VerifyDialog's per-plan row, VerifyPlan,
VerifyStep. Props are frozen in the seam (`BehaviorsCardProps`).

- Head: status sentence (`summary.sentence`), ONE primary action
  (`summary.action`: Check now · Rerun · See what failed · Write them), ⋯ menu:
  Rewrite behaviors · It's deployed — check now · Fix in a new session (on
  failure) · Open the checking run · Cancel the check · Open the work session ·
  View diff · Delete these behaviors.
- Body: one row per behavior — its text (+ expect), who checks it (🤖 agent /
  🙋 you, a toggle), its result chip. Rows checked by you show **Pass · Fail ·
  Can't check** + a note field; agent rows show the agent's note as evidence.
  Undo on an answer. Edit/Remove on a row (remove only person-added, as now).
  "+ Add a behavior" inline at the foot.
- Keys while a row is focused: 1/p pass, 2/f fail, 3/b can't check, u undo,
  n note (as before).
- When the card belongs to a Verify stop that is parked on you, answering the
  last open behavior wakes the chain (the server already does this via
  `session.test_plan_settled`). There is **no blanket Done** any more.
- Honest timing line where it applies: "Checked before merge · checks again
  once live" (pre-merge run vs live, from planVerdict's logic).

## 3. The Behaviors board (a Sky object, `behaviors` key)

Behaviors with **no session frame in the Sky** (closed session, "another
branch", a watched repo's push) live on a calm Sky object, the **Behaviors
board** (same look as the intake lane / Finished strip): rows grouped by tone
(Needs you, Failed, Checking, Not checked, Verified today), each row expands
into the same BehaviorsCard. Shown when it has a row that needs you or failed,
or when turned on from the Sky ⋯ ("Show the Behaviors board", per device,
`mf_sky_behaviors`); × hides it.

The board's ⋯ holds what the dialog's header held:
- **Write behaviors on every push** (the `verify_enabled` switch — the ONLY switch;
  the sidebar Verify bar goes),
- **Watched repositories** (per-repo live branch, prompt, deploy delay, target;
  "Watch a GitHub repository for every push"),
- **Check another branch…** (repo + branch → POST /api/test-plans/branch),
- **Start empty behaviors for a session…**.

## 4. Ways in (one destination: the Sky)

`openBehaviors(target)` (`lib/behaviorsFocus.ts`, seam) switches to the Sky,
flies the camera to the session's frame (or the board) and opens the card.
Every former Verify entry calls it:

| Was | Now |
|---|---|
| top-bar **Verify** button | **gone** (top bar: Grid·Sky / Intake / Schedule / Settings) |
| Alt+V | `openBehaviors({ next: true })`: the next behavior that needs you, else the next failed, else the board |
| palette "Verify…" | "Behaviors — next that needs you" (+ "Show the Behaviors board") |
| `#verify`, `#verify/<branch>`, `#flights/checklists[/<b>]` | redirect to `#behaviors[/<plan id>]` → openBehaviors |
| session menu "Verify checklist…" | "Behaviors" |
| verdict chip on a sidebar row / pane head | click → openBehaviors({ title }) |
| bell rows for verify events | openBehaviors({ planId }) |
| Sky drawer "Needs you" checklist rows | fly to the tile (openBehaviors) |
| sidebar Verify bar | gone |
| read-only "Verifying" watch pane | stays; its back button → openBehaviors |
| Verify tour (lib/tours/verify.ts) | rewritten as a 3-step Sky tour of the tile, the card, the board |

The grid view's Flight tab draws the same SkyChain, so the tile shows there
too; openBehaviors from the grid switches to the Sky.

## 5. The Verify stop

- Parked on you: its card is the BehaviorsCard (per-behavior answers). The
  blanket "Done" (`_verify_done` → "Done on the chain") is removed — the
  server refuses Done on it (409 `answer_behaviors`): the stop moves on when
  the behaviors are answered, or the person presses **Skip**.
- **Failed: the route HALTS** (`on_fail` halt is the default). The reason leads
  with the count ("Failed · 2: login works; logout works"); the card says
  "Failed · 2 — halted…" and offers **Retry · Skip · Let the agent fix it**
  (`POST /api/flights/{id}/steps/{sid}/op/fix` — the agent gets the failure,
  the stop runs again after its turn). A Retry or a fix checks again: a
  failure answered before the attempt began is re-run by the agent, and a
  person's old Fail is theirs to answer again. No recovery ladder.
- No behaviors yet: running the stop writes them (the face reads "Writing…",
  then the summary).
- `who=you`: only behaviors checked by you park the chain on you; agent
  behaviors under `who=you` are run by the agent first (or, if the person
  chose "you" for all, they are shown as yours AND recorded when answered).
- A chain parked on a Verify stop counts in the Sky's Needs-you badge.

## Seams (pre-committed; do not change their signatures without the integrator)

- `frontend/src/lib/behaviorsFocus.ts` — `openBehaviors`, `useBehaviorsFocus`,
  `findPlan`, `behaviorsHash`, `parseBehaviorsHash`.
- `frontend/src/lib/behaviors.ts` — `BehaviorTone`, `BehaviorSummary`,
  `behaviorSummary(plan)`, `planForSession(plans, row)`, `orphanPlans(plans,
  rows)`, `nextNeedingYou(plans)`. Owned by vs-card (fills the bodies).
- `frontend/src/components/behaviors/BehaviorsSettings.tsx` — `BehaviorsSettingsProps` + a placeholder (the board's ⋯ settings). Owned by vs-card.
- `frontend/src/components/behaviors/BehaviorsCard.tsx` — `BehaviorsCardProps`
  + a placeholder. Owned by vs-card.

## As built

Each worker writes its notes to `docs/behaviors/<task>.md` (card, tile,
board); the integrator folds them in here.

**Connected (behaviors-connect, 2026-11-03):** the tile, the card in the
tile's place, the board (with its rows' cards and ⋯ settings), the drawer's
rows (`openBehaviors({ planId })`), the old `#flights/checklists[/<b>]` links
(→ `openBehaviors`, address `#behaviors` / `#sky`) and a `#behaviors[/<plan
id>]` link (load + Back/Forward → `openBehaviors`). Seen rendered against the
demo mocks in light and dark.

**Connected (vz-verify, 2026-11-03 — docs/vision/verify.md):** every §4
way in now flies the Sky — the top-bar Verify button is gone, Alt+V is
`openBehaviors({ next: true })`, the palette has "Behaviors — next that needs
you" + "Show the Behaviors board", `#verify` / `#verify/<branch>` land on the
behaviors and read `#behaviors`, the session menu says "Behaviors", the verdict
chip and the bell's rows open the behaviors, the sidebar Verify bar is
deleted, the watch pane's back button returns to its behaviors, and the Verify
tour is a 3-step Sky tour (stop, card, board). The Verify dialog
(VerifyDialog/Plan/Step/Chrome) is deleted; any leftover
`openDialogFor("verify")` lands in the Sky (`installVerifyRedirect`). §5 is
built: a failed Verify stop halts the route ("Failed · 2: …"), its card offers
Retry · Skip · Let the agent fix it (`…/steps/<id>/op/fix`), a Retry or fix
checks the behaviors again, and the blanket Done is refused.

**Still to connect (files vz-verify did not own — see docs/vision/verify.md
"Wanted"):** the halted face suffix "— halted" (`verifyStopFace` in
`withBehaviors`), "Let the agent fix it" in FlightStrip's halted actions,
`ON_FAIL_LABEL` words, verifyActions' "in Verify" toasts, the `verify` bar in
barDefs / SidebarBarsPicker, replayTour's `verify` surface.
