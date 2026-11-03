# Words outside the Sky — worker notes (vz-words-ui)

The frontend copy outside the Sky, renamed to `docs/vocabulary.md`. Copy only:
no type, id, CSS class, store key, settings key or API path changed. The
`backend/web/static/` bundle is **not** rebuilt here (the integrator rebuilds it once).

## Old → new

| Old (copy) | New | Where (main ones) |
|---|---|---|
| Chain (pane tab, its tooltip) | **Route** | `grid/Pane.tsx`, `lib/paneHeader.ts` ("Flight" in Customize → **Route**) |
| The chain halted / needs you | The **route** halted / needs you | `grid/Pane.tsx` |
| Saved chain · N stops | **Saved route** · N stops | `dialogs/TemplatesDialog.tsx` |
| templates and saved chains that start by themselves | … saved **routes** … | `intake/ScheduledTemplates.tsx` |
| New chain / Start a chain / a chain (fallbacks) | New **route** / Start a **route** / a **route** | `lib/groundedLines.ts` |
| A saved chain needs a name · Another saved chain is already called … | A saved **route** … | `lib/groundedLines.ts` (`checkRename`) |
| chain “X” needs you | **route** “X” needs you | `lib/sessionState.ts` |
| Flight “X” is paused at … its current step | **Route** “X” is paused at … its current **stop** | `lib/sessionState.ts` |
| Paused flight (rail chip) | **Paused route** | `lib/sessionState.ts` (`PAUSED_FLIGHT_LABEL`) |
| the flight goes on (Done/Skip tooltips) | the **route** goes on | `outbox/WaitingRow.tsx` |
| Its flight or fast-track is running | Its **route** is running | `lib/sessionActions.ts` |
| the birds on your chains | … your **routes** | `settings/screens/Appearance.tsx` |
| Fast-track (label / control name) | **Goes as far as** | New form, Options fold, Templates editor, Settings → Workspace, Commit dialog ("Then goes as far as"), batch list ("Each goes as far as") |
| Fast-track… (menu item, Ctrl+K F, palette row) | **Goes as far as…** | `lib/sessionActionTable.ts`, `lib/sessionMenu.ts` |
| Fast-track ⏩ (Customize button) | **Goes as far as ⏩** | `lib/paneHeader.ts` |
| Off (the bottom rung, `LANE_LABEL.leave`) | **Agent only** | `lib/autopilot.ts` — every rung list (New, ⏩ picker, Templates, Intake's per-start pick) |
| Stop after the agent (a source's bottom rung, `SOURCE_OFF_LABEL`) | **Agent only** | `lib/autopilot.ts` — also shows in the Sky's intake lane / source cards |
| Off (default) · New sessions start Off | **Agent only** (default) · … start at **Agent only** | `settings/screens/Workspace.tsx` |
| Fast-track: X · fast-track → X · fast-track off | goes as far as X · goes as far as **Agent only** | toasts in `lib/laneActions.ts`, `lib/newSessionSubmit.ts`; card meta + history in `TemplatesDialog.tsx`; Options summary in `NewList.tsx` |
| Fast-track is off: nothing is committed … | Goes as far as **Agent only**: nothing is committed … | `NewSessionDialog.tsx`, `FastTrackChoice.tsx`, `lib/laneActions.ts` |
| (needs fast-track) beside "Ask me before it ships" | (needs Commit or further) | `NewSessionDialog.tsx`, `FastTrackChoice.tsx` |
| Turns fast-track off | Sets Goes as far as to **Agent only** | `newSession/WorkspaceFold.tsx` |
| Fast-tracking to X / Fast-track finished at X and switched itself off | **Going as far as** X / **Went as far as** X — done; it is back to Agent only | `lib/autopilot.ts` (⏩ chip tooltip) |
| fast-track stopped (rail line, toast, Settings switch) | **shipping halted** | `lib/agentMessages.ts`, `EventToasts.tsx`, `settings/screens/Workspace.tsx` ("Tell the agent when shipping halts") |
| its fast-track ends at X | it goes as far as X | `outbox/outbox.ts`, `lib/agentMessages.ts` |
| fast-track: → PR (team run note) · fast-tracked to Commit/Off | goes as far as: → PR · goes as far as Commit/Agent only | `lib/splitRun.ts` |
| Fast-track this one to (Intake per-start pick) | How far this one goes | `intake/kit.tsx` |
| Verify checklist… (session menu) | **Behaviors…** | `lib/sessionMenu.ts` |
| checklist (Verify toasts, errors, Intake hints, bar picker) | **behaviors** | `lib/verifyActions.ts`, `dialogs/verify/emptyChecklist.ts`, `intake/RepoSources.tsx`, `customize/SidebarBarsPicker.tsx` |
| Joint checklist · steps · Run my steps (People → joint check) | Joint **behaviors** · **behaviors** · **Check mine** | `settings/screens/peer/JointCheck.tsx`, `lib/peerCheck.ts` |
| Couldn't change that step | Couldn't change that **behavior** | `lib/verifyActions.ts` |
| sub-session(s) | **worker(s)** | `sidebar/SidebarRow.tsx` (fold toggle), `settings/screens/peer/GuestLinks.tsx` |
| proposing the pieces / up to N pieces | proposing its **workers** / up to N **workers** | `lib/laneActions.ts`, `lib/runStart.ts` |
| is waiting on you (voice caption) · waiting on you (Schedule note) | **needs you** | `lib/voiceDesktop.ts`, `dialogs/TemplateEditor.tsx` |

## Deliberate keeps

- **Setup checklist** — `sidebar/Sidebar.tsx` comments, `SetupChecklist` component (the allowed meaning).
- **`LANE_SHORT.leave` = "off"** (`lib/autopilot.ts`) — the ⏩ chip's text-free state ("⏩" alone, its aria-label now says "Goes as far as: Agent only"). The short word is still "off"; flip it with `lib/stage.ts`'s owner if wanted (tests pin it in `fastTrack.test.ts`, `fastTrackSurfaces.test.ts`, `autopilot.test.ts`).
- **`LANE_KEY.leave` = "O"** — the picker letter; it still fits ("agent **O**nly").
- **"Needs attention"** on Settings → Connections and Intake's source test (`settings/screens/Connections.tsx:55`, `intake/sourceForm.ts:234`) — a connection's health (re-auth), not work waiting on a person.
- **"it's / they'll appear in Verify"** in `lib/verifyActions.ts` — a branch with no session has no route to point at; reword once the Verify place is retired.
- **"Fly the flock over your grid"** (`settings/screens/Appearance.tsx`) — bird flavour as a verb in a sentence about the decorative flock.
- **"plan"** for the team-run lead's split plan and the Map's agent plan — a different thing from a route.
- **`lib/workersRequest.ts` "connecting pieces"** — text typed to the agent, not UI copy.
- Code comments mentioning chain / fast-track were left (copy only).

## Retired words in files I don't own (for the integrator)

- `components/grid/FastTrackMenu.tsx:240` — "Its Chain tab: add, remove or move its stops" / "Its chain on the Sky"
- `components/grid/FastTrackMenu.tsx:280` — aria-label `Fast-track — ${name}`; `:289` heading **⏩ Fast-track**
- `components/sidebar/VerifyBar.tsx:107,130,132,139,141,147,149,179,188` — checklist(s) throughout
- `components/palette/settingsSearch.ts:141` — search term "sub-sessions" (a search alias — may be worth keeping)
- `lib/flights.ts:580,601` — default saved-route name "New chain"
- `lib/stage.ts:557,560,565,568` — ⏩ tooltip "Fast-track → …", "Fast-track stopped:", "Fast-track is off. Click to choose…"
- `lib/stage.ts:682,688,720,731,734` — "Checklist ✓ …", "Checklist failed — open the PR anyway?", "Verify checklist…", "A step on this branch's checklist failed", "Show the checklist"
