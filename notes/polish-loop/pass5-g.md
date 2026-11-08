# Polish pass 5 — (g) the Sky / routes UI

Walked as a new user in headless Chrome against a fresh ux-lab sandbox on :9530
(stub `claude`, nothing adopted, `/api/instances` empty at boot): the empty Sky,
All stops, the ⋯ menu, a route built from nothing by right-click, the saved
routes (All stops → SAVED ROUTES; Schedule's Nightly chores / Scheduled task),
a route on a stub session (Command ✓ → Wait for me → a Command that exits 1 →
Notify): Done, the halted stop's Retry / Skip / Let the agent fix it, Skip to
the end; docs/flights.md + docs/web-ui.md against the UI and vocabulary.md.
Fixes checked in the browser with a scratch build of the bundle routed over
`/app.js` (the committed bundle is untouched).

| # | Sev | Finding | Repro/where | Status |
|---|---|---|---|---|
| 1 | high | On a fresh install the "Nothing in the Sky yet" card drew ON TOP of All stops (the floating palette): half the catalog's tiles were hidden behind it. The card is a sibling drawn after the transformed plane, so the palette's z-index can't lift it. | Empty Sky → shelf **All stops…** · `SkyView.tsx` SkyEmpty mount | FIXED 07f002d7 (card not drawn while the palette floats over a blank sky) |
| 2 | high | A stop placed by right-click (**Add a stop…**) or a drop onto the sky while the default **This session** is on vanished without a word: the route was made (`POST /api/flights/start {grounded}`) but This session draws no route without a session, and the card still said "Nothing in the Sky yet". Only the shelf/palette click had a toast. | Fresh Sky → right-click → type "agent" → Enter · `SkyView.dropGrounded` | FIXED 07f002d7 (`groundedLanding`: no session at all → the view moves to All; else a toast with **Show all**; This session's empty line says "Your routes with no session yet show under All.") |
| 3 | low | A Wait for me stop under its ✓ still read "waits for your Done". | Route with a Wait for me → **Done** · `flightDiagram.flightStops` | FIXED 07f002d7 (`youFlownSub`: "done" / "skipped") |
| 4 | low | docs had no user-level "build a route / a failed stop" section; docs/flights.md + docs/web-ui.md named Schedule's buttons **Fly now** / **Edit in the Sky** (the UI says Run now / Schedule… / Edit; "Fly now" is a never-say word). | docs/flights.md "Scheduled saved routes", docs/web-ui.md ~3312, ~4286 | FIXED cafb6b77 |
| 5 | low | After **Done** on a Wait for me, the session's chip (rail + route head) kept the old stop's name ("Wait for me: Try it") for a poll or more while the route already waited at the next stop; the row's `flight` summary is briefly `null` right after the POST, so refetching at once wouldn't help. | Route with two Wait for me stops → Done on the first | DEFERRED (row summary lags the flight; the fix is in the server's row build, outside the fence) |
| 6 | nit | The route head says the same thing three times while waiting: chip "Wait for me: X", "needs you at X · …", and the same chip again under the agent box. | Any route at a Wait for me | DEFERRED (design call) |
| 7 | nit | Schedule shows **Run now** enabled on saved routes that say "Run now and schedules need a folder. Pick one". | Schedule → Nightly chores / Scheduled task | DEFERRED (Schedule dialog is outside the fence; not tried, to avoid a real run) |
| 8 | nit | Toolbar zoom group reads "− 100 % ▾ + 1 Fit 100%": two 100%s and a bare "1". | Sky toolbar | DEFERRED (no change without the owner) |

Tests: `npx vitest run` — 355 files / 6483 tests pass (new `skyPass5.test.ts`;
`firstRunVision.test.ts` `blank` pin updated deliberately); `npx tsc --noEmit` clean.

PASS RESULT: found 5 worth fixing, fixed 4, left 1 (+3 nits deferred)
