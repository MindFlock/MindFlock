# vs-card — the Behaviors card, its summary words, the board's settings

Worker notes for docs/behaviors.md §2, §3 (settings) and "Words". The
integrator folds these into the contract.

## What was built

| File | What |
|---|---|
| `frontend/src/lib/behaviors.ts` | Seam bodies. `behaviorSummary(plan)` is one ordered decision (the old `planStatus` order) in the new words: tone · status · short · sentence · action. `planForSession` now refuses a verify run's own session. `nextNeedingYou` prefers a Needs-you plan that has not also failed (a failed one reads "Failed" everywhere, so it comes second). New exports: `timingLine(plan)`, `behaviorsCount(n)`. |
| `frontend/src/components/behaviors/BehaviorsCard.tsx` (+ `.css`) | The card. Also exports `ShieldIcon` (the ✓-shield — the tile can reuse it), `AddBehavior`, `addBehavior()`. |
| `frontend/src/components/behaviors/BehaviorRow.tsx` | One behavior row (answers, note, who toggle, chip, Undo, Edit, Remove, Fix, keys). |
| `frontend/src/components/behaviors/cardModel.ts` | The card's pure decisions (⋯ items, chips, who, keys, primary target) — pinned by tests, since vitest is node-only. |
| `frontend/src/components/behaviors/BehaviorsSettings.tsx` (+ `.css`) | The board's ⋯ settings: the switch, Watched repositories, Check another branch…, Start empty behaviors for a session…. Brings its own `SettingsCtx`, so it mounts anywhere. Exports `writesOnPush`, `emptyTargets`, `targetLabel`. |
| `frontend/src/lib/verifyActions.ts` | Unpinned toasts reworded (Rewriting the behaviors…, Check cancelled, Couldn't start the check…, Couldn't remove that behavior; the "all answered" toast now names the new status). New: `askBehaviorsForBranch(repo, branch, {empty})`, `startEmptyBehaviors(session)`. |

## The summary (statuses and the one action)

| State | tone | status | short | action |
|---|---|---|---|---|
| writing | busy | Writing… | Writing… | — |
| writing stalled / couldn't be written / came back empty | quiet | Not checked | None yet | Write them (asks the agent again) |
| branch has no commits | quiet | Not checked | None yet | — |
| empty on purpose (`origin: manual`) | you | Needs you | Needs you | Write them (opens "+ Add a behavior") |
| not merged, never checked | quiet | Not checked | Not checked | Check now (only if an agent behavior exists) |
| merged, waiting to deploy | quiet | Not checked | Not checked | — (⋯ "It's deployed — check now") |
| live, unchecked | you | Needs you | Needs you · n | Check now, or Answer when all are yours |
| checking | busy | Checking… | Checking… | — |
| run's agent gone | quiet | Not checked | n of m verified | Rerun / Check now |
| a behavior failed | bad | Failed | Failed · n | See what failed (focuses the first failed row) |
| yours left after a run | you | Needs you | Needs you · n | Answer (focuses the first of yours) |
| stopped early / you couldn't check some | quiet | Not checked | n of m verified | Rerun (if the agent has one) |
| every behavior passed | ok | Verified / Verified live | same | — |

The honest timing line goes after the sentence: "Checked before merge ·
checks again once live" when the current answer is a pre-merge run, or
"Checked before merge (every behavior passed) · checks again once live" when
the work has shipped and that check is the only one on record.

## The card

- Head: ✓-shield, "Behaviors", the status pill, a count, ⋯, × (Esc does the
  same, outside inputs). Then the sentence and the one primary button
  (`.btn-primary`). With `flightStep` set, a **Skip this stop** button
  (`stepSkip`) sits next to it. There is no Done.
- ⋯ goes in a portal on `document.body` using `StopMenu` (the same as
  `useChainMenu`/`useAgentMenu`, `--sky-z: 1`). It holds, when they apply:
  Rewrite behaviors (an inline box with an optional "check this instead"
  field; held mid-run), It's deployed — check now, Fix in a new session (on
  failure), Check now / Rerun (when that isn't the primary), Open the
  checking run, Cancel the check, Open the work session, View diff, What
  could break, Open PR ↗, and Delete these behaviors (confirmed inline with
  `InlineConfirm`).
- Rows: the chip (✓ Passed · ✗ Failed · ● Needs you · – Couldn't check · ○ Not
  checked), the text and "Expect: …", the who toggle (🤖 agent / 🙋 you →
  PATCH actor; disabled mid-run), and Pass · Fail · Can't check on yours and
  on any row a person already answered. The agent's note shows as "The
  agent: …". Also on a row: Undo, + note, Fix (on a failed row), Edit, and
  Remove (only on behaviors a person added, confirmed inline). Keys on a
  focused row: 1/p, 2/f, 3/b, u, n. Note drafts survive a remount.
- Foot: "+ Add a behavior" → text, expect, who (defaults to the agent, as
  the old dialog did). Enter adds and keeps the form open for the next one.
- Type scale: `--bh-s: clamp(1, 0.8 / --sky-z, 1.3)`, the frames' "mid"
  rule. It is 1 off the canvas, so `compact` and board rows work outside the
  Sky. Text is always `--text`/`--muted` on `--panel`; tone shows only on
  edges, borders and glyphs.

## For the integrator to connect

Checked by behaviors-connect (2026-11-03) — all already wired in the card:

- **Open the checking run** calls `openVerifyPane(run)` then
  `setView("grid")`; **View diff** / **What could break** switch to the grid
  too; **Open the work session** calls `showSky(title)`.
- The root carries `data-plan`, `data-no-drag`; the rows list has
  `sky-own-scroll`. The tile's box is sized by `behaviorsCardBox` (now 100 px
  a row), and a `compact` card (a board row, the grid's Flight strip) reads at
  `--bh-s: 1`.
- `BehaviorsSettings`'s "Check another branch…" / "Start empty…" call
  `onClose` then `openBehaviors({ planId })`.
- Not done (on purpose): rewording `writePlanFor`, `writePlanForBranch` and
  `editPlanStep`'s "checklist"/"step" errors — they move with
  `startFinish.test.ts`, `verifyAnotherBranch.test.ts` and
  `verifyDialogUi.test.ts` once the dialog retires.
- The "Verify stop parked on you" wake-up is the server's
  (`session.test_plan_settled`). The card adds nothing for it.

## Open gaps

- Seen rendered 2026-11-03 (static bundle + demo mocks, light and dark): in
  the tile's place on a session frame, and as a board row. Fixed then: the
  tile box hid rows 2–3, and a board row's head pushed ⋯/× past the edge.
- Per-behavior "Check this one again" (the old step menu's scoped rerun) was
  dropped. Rerun covers the whole set, and Fix and Edit are on the row.
  `startChecklistRun(plan, { only: [id] })` still exists if it's wanted back.
- There is no inline "watch the run here" terminal (the old row's
  `useWsTerm`). "Open the checking run" opens the grid pane instead.
