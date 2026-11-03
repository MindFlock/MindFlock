# Verify on the route — as built (vz-verify, 2026-11-03)

The owner: "do you have any thoughts on verify and how we can kind of get it
out of being a menu really and make it more of a thing in the chain [route]?"
and "I kind of like the halt thing, we don't really need recovery."

**Verify is not a place any more.** It is a stop on the route; a session's
behaviors live on its route in the Sky (docs/behaviors.md). Every former way
in now flies the Sky (`openBehaviors`), and the dialog is gone.

## What changed

### The ways in (docs/behaviors.md §4 — all connected)

| Was | Now |
|---|---|
| top-bar **Verify** button (+ its due/failing badge, its ⋯ fold row) | gone — the bar is New · Grid/Sky · Intake · Schedule · Settings (`TopBar.tsx`) |
| Alt+V → Verify dialog | `openBehaviors({ next: true })`, help "Behaviors — the next that needs you" (`lib/keymap.ts`, id `verify` kept so a rebind survives) |
| palette "Verify…" | "Behaviors — next that needs you" (Alt+V) and "Show the Behaviors board" (`CommandPalette.tsx`; the old words stay in the keywords) |
| `#verify`, `#verify/<branch>` (a surface) | not a surface (`verify` dropped from `BY_HASH`); `parseBehaviorsLink` takes them, `openBehaviors({ next: true })` / `({ branch })`, and the address is rewritten in place to `#behaviors` (`behaviorsAddress`) — on load and on Back/Forward (`lib/surfaceHash.ts`) |
| session menu "Verify checklist…" | "Behaviors" → `openBehaviors({ planId, title, branch })`; its Open-PR note is "Behaviors ✗ Failed", the question "A behavior failed — open the PR anyway?" / "Show the behaviors" (`lib/stage.ts`); `openChecklist(branch)` → `openBehaviors({ branch })` (`lib/checklistLink.ts`) |
| verdict chip → Verify on the branch | → `openBehaviors({ planId, title, branch })`, tooltip "Behaviors: … Show them on the route in the Sky." (`VerdictChip.tsx`; the unused `VerdictBadge` went with the dialog) |
| bell's verify rows → Verify | → `openBehaviors({ planId })`, named "Behaviors · <name>", texts "couldn't write the behaviors", "behaviors failed — 2 behaviors", "behavior failed — <text>" (`NotificationsBell.tsx`) |
| sidebar Verify bar | deleted (`VerifyBar.tsx`, its `SidebarBars` case) |
| watch pane's "Verify" back button | "Behaviors" → `openBehaviors({ planId })` of the plan whose `run_session` it is, else `{ next: true }` (`SpecialPane.tsx`) |
| Verify tour (5 dialog steps) | 3 Sky steps: the Behaviors stop on the route (`[data-tour="behaviors-stop"], .sky-node.sky-behaviors`), its card opened in place (`behaviors-card`, added on `BehaviorsCard`), the board (`behaviors-board`, added on `BehaviorsBoard`) (`lib/tours/verify.ts`) |

**A catch-all for callers outside this task's files:** `installVerifyRedirect()`
(`lib/behaviorsFocus.ts`, installed by `App.tsx`) turns any
`openDialogFor("verify", target)` into `closeDialog()` + `openBehaviors(
verifyDialogTarget(target))` — the target tried as plan id, title and branch;
none = `{ next: true }`. It is what keeps `replayTour("verify")`, Customize's
"Open Verify" bar link and any extension working.

### The dialog is retired

Deleted: `VerifyDialog.tsx`, `VerifyPlan.tsx`, `VerifyStep.tsx`,
`VerifyChrome.tsx`, `sidebar/VerifyBar.tsx`. The one piece still used —
the watched-repositories line — moved to
`components/behaviors/WatchedRepositories.tsx` (BehaviorsSettings mounts it).
From `components/dialogs/verify.ts` the helpers only the dialog/top bar/bar
used are gone: `dueCount`, `failingCount`, `verifyCountLine`,
`liveBranchOverridden`, `planTargets`, `closedTargets`, `verifyDeepLinkPlan`,
`noTargetsReason`, `GROUP_KEYS`/`groupLabel`/`GROUPS`, `planMatches`,
`canRunNow`, `groupPlans`, `tallyBits`/`tallySentence`, `stepKeyAllowed`,
`runEvidence`, `runTreeMismatch` (and the `matchesTokens` import). Everything
the Behaviors card/tile/board, the joint check and the session menu read stays.
**Kept:** `VerifyDialog.css` — `styles/index.css` imports it and its `.vchip`,
`.vf-menu*`, `.vf-sources` rules are shared (VerdictChip, OverflowMenu, the
peer joint check, StopMenu, WatchedRepositories). Backend routes
(`/api/test-plans`) untouched.

### Verify, deliberately, on the route (backend: `flights.py`, `flight_driver.py`)

- **The stop picker's blurb** comes from `STEP_KINDS["verify"]["summary"]`:
  "Checks this work's behaviors; the agent checks what it can, the rest waits
  for you." (`GET /api/flights/kinds`), so QuickAdd / SkyPalette / StopCatalog
  show it without an edit there.
- **Default `on_fail` = halt**, now explicit (`default_on_fail: "halt"`).
- **No behaviors yet:** the stop's run already asks for them
  (`_ensure_checklist` → `_ensure_test_plan(manual=True)`), which creates the
  record in `generating` at once — the stop's face is the summary, which reads
  "Writing…" until they are written, then "3 of 5 verified" etc. Its notes are
  "writing the behaviors", "the agent is checking the behaviors", "N behaviors
  for you", "every behavior has an answer".
- **Failed behaviors HALT the route.** The fail reason leads with the count —
  `"Failed · 2: login works; logout works"` — which is the route's halted
  reason. The Verify stop's card (BehaviorsCard with `flightStep`, opened in
  the stop's place) on a halted stop says "Failed · 2 — halted. The route waits
  here until you Retry, Skip or let the agent fix it." and offers exactly
  **Retry · Skip · Let the agent fix it** (`cardModel.verifyStopStand`,
  `haltedLine`). No recovery ladder.
- **"Let the agent fix it"** is a new built-in step op,
  `POST /api/flights/{id}/steps/{sid}/op/fix` (the existing generic op route;
  `flight_driver.step_op` falls back to the built-in `fix` when the kind has no
  plug-in op of that name): `flights.op_fix` puts the halted stop in the
  existing `fix` phase (one fix round), the driver hands the failure to the
  session's agent (`_fix_note`), and when its turn ends the stop runs again.
  If the agent can't be asked, `op_fix_failed` halts it again saying why.
- **Retry / a fix checks again.** A failure answered BEFORE the stop's current
  attempt began (the obs now carries `failed_at`) is stale: the agent re-runs
  its behaviors first (`recheck`), and a person's old Fail is theirs to answer
  again (the route goes to Needs you). A fresh failure halts again. Without
  this, Retry and on_fail-agent re-failed instantly on the old result.
- **No blanket Done.** `operate("done")` on a Verify stop waiting on you is
  refused (409 `answer_behaviors`: "answer its behaviors (or Skip it)…"); the
  old `_verify_done` that passed every open behavior "Done on the chain" is
  gone. (Done on a Verify FAILURE handed to you — `on_fail: you` — still works.)
- **Needs you:** a route parked on a Verify stop is `waiting_you`, which
  `needsYouFlights` / `perchBadge` already count on the Sky's badge (pinned in
  `verifyOnTheRoute.test.ts`).
- **One thing on a route:** `withBehaviors` (lib/flightDiagram) puts the
  behaviors mark on the route's Verify stop when it has one and adds no
  Behaviors tile — confirmed in the built code and pinned in
  `behaviorsTile.test.ts` ("the chain's Verify stop IS the tile").

## Wanted in files this task did not own (for the integrator)

- **The halted face line "Failed · 2 — halted"** — `lib/flightDiagram.ts`
  `withBehaviors` (or SkyChain's `sub`) should wear
  `verifyStopFace(mark.short, n.state)` (added to `lib/behaviors.ts`) on the
  Verify stop: `behaviors: { ...mark, short: verifyStopFace(mark.short, n.state) }`.
  Today the face says "Failed · 2" with the halt badge.
- **FlightStrip.tsx** `YouStepActions` (the Route pane tab's halted-stop
  actions) shows Retry · Skip only; for a halted Verify stop add "Let the agent
  fix it" (`flightAction(..., flightPath(id, "/steps/" + sid + "/op/fix"))`)
  — or render `BehaviorsCard` there as `VerifyStopActions` does when parked.
  Its tooltips still say "chain".
- **Other Done buttons on a parked stop** — `outbox/WaitingRow.tsx` (the
  bell's waiting row), `flights/useChainMenu.tsx` / `FlywayCanvas.tsx` (a
  stop's ⋯ "done"), `useSessionLine.tsx` (`onDone` on a perched stop) — call
  `stepDone` without looking at the kind. On a Verify stop parked on you the
  server now refuses it (409 `answer_behaviors`, shown in the error card); hide
  Done there for `kind === "verify"` and offer "Behaviors" (`openBehaviors({
  planId })`) instead.
- **lib/flights.ts `ON_FAIL_LABEL`** still reads "Stop and tell me / Hand it to
  the agent / Skip it / Ask me"; vocabulary: "Halt · Let the agent fix it ·
  Skip · Ask me".
- **lib/verifyActions.ts** toasts still say "…already has a checklist — it's in
  Verify" / "it'll appear in Verify" — wanted: "…already has behaviors — they're
  on its route in the Sky" / "they'll appear on the Behaviors board".
- **components/sidebar/barDefs.ts** still lists `{ key: "verify", label:
  "Verify" }` and **customize/SidebarBarsPicker.tsx** its "Open Verify" link:
  drop both (a saved order naming `verify` renders an empty slot; the link
  lands in the Sky via the redirect).
- **components/onboarding/replayTour.ts** `SURFACE.verify = "verify"` opens the
  retired dialog name (the redirect makes it the Sky): replace with
  `openBehaviors({ next: true })` like the `sky` branch.
- **lib/vocab.ts `VERIFY_BLURB`** ("check that pushed changes work") has no
  user left in this task's files.
- **components/dialogs/verify/emptyChecklist.ts** is dead (only VerifyChrome
  used it).
- **state/store.ts** still has `"verify"` in `DialogName`; it can go with the
  redirect once nothing outside calls `openDialogFor("verify")`.
- **lib/calm.ts `topBarQuiet`** still returns a `verify` flag nothing reads.
- **The Verify tour no longer opens by itself** (it was `useAutoTour("verify")`
  in the dialog); the first-run tours worker decides whether the Sky should
  auto-open it on the first Behaviors stop.
