# Polish pass 3 — area (d): Intake sources

Walked as a new user in a fresh ux-lab sandbox (`~/polish-scratch/d/tools/lab.py`, port 9450)
driven by headless Chrome (playwright-core). Every outbound HTTP request from the sandbox server
(and its pipeline child) was rewritten by a `sitecustomize` to a local fake tracker on :9451
(Shortcut, Jira, Linear, Asana, GitHub): token containing "good" = OK, else 401. No real token,
no real network. Walked: Alt+I from empty → Add source → Shortcut → empty Test, bad token, good
token → What it does (states, labels, repo, agent) → Finish → panel → Edit → Anyone's (+ the
no-state guardrail) → Auto-start tab → Tickets on → sc-101 became a running session (stub
claude); GitHub Issues add next to a stored Shortcut source; Jira connect; bad-credential
messages per tracker via the test route; docs/ingestion-pipeline.md + docs/web-ui.md vs the UI.

| # | Sev | Finding | Repro / where | Status |
|---|---|---|---|---|
| 1 | high | A fresh install with NO ticket source searched api.app.shortcut.com with an empty token (HTTP 401) every time the Intake panel opened, and `GET /api/tickets` listed a phantom "Shortcut" source. `PipelineConfig.__post_init__` stood a blank `TicketProviderConfig()` (provider shortcut) in for "none". | Open Intake on an empty sandbox; fake API log shows `POST /stories/search owner_id ""` | FIXED c3dfac3c (list stays empty; `ticketing` keeps a blank stand-in for readers of the primary's defaults) |
| 2 | high | Adding **GitHub Issues** next to a saved Shortcut source (no GitHub connection yet) sent the **Shortcut token to api.github.com** as `Bearer`, and the answer ("GitHub rejected the token") named a token the user never typed. Root cause: `_source_cfg_from_body` fills a missing token from the PRIMARY source whatever its provider; github_issues then uses it via its deprecated per-source fallback. | Intake → Add source → GitHub Issues → Repository `acme/app` → Test & continue; fake log: `GET /api.github.com/user tok='Bearer <shortcut token>'` | FIXED c358f309 in-fence (the fallback takes only a GitHub-shaped token). ROOT CAUSE DEFERRED — outside fence: `backend/web/addons/settings.py:736-738` should fall back to the primary source only when its provider matches the body's (the same borrowing reaches Linear/Asana/Jira on a test or `/ticketing/states` call with no token — the UI's Required check blocks that path, the API does not) |
| 3 | med | Dead first click: press **Test & continue** with the token empty ("Required"), paste the token, press again → nothing happens. The "Required" note went on blur (the button's mousedown), everything below moved up a line under the pointer, the click landed on nothing. | Add Shortcut → Test & continue (empty) → paste token → Test & continue: no request; 2nd press works | FIXED 9deeb308 (`shownFieldError`: Required goes as soon as text is typed) — verified in the real UI with a scratch build |
| 4 | med | On an **Anyone's** source the state picker still offered "Any state — start every ticket assigned to me"; the server always refuses it (Anyone's needs a state/label), so it bounced back as "Not saved". | Edit → What it takes → Anyone's → remove the state → pick "Any state…" → 400 | FIXED c846d262 |
| 5 | med | The Connect step said nothing about which token to make or what it must be allowed to do — just a "Create a token ↗" link. | Add Shortcut/Jira/Linear/Asana → Connect | FIXED 0dcd0526 (catalog `hint` per secret field: where to click, and scopes/permissions) |
| 6 | low | GitHub Issues errors: "github_issues could not work out which repository … `origin` can be used.." (internal id, doubled full stop, points at Repo URL which is on step 3, not the Connect step); a 401 said "GitHub rejected the token" on a source with no token field. | Add GitHub Issues → Test & continue with no repo / no GitHub connection | FIXED c358f309 |
| 7 | low | docs/ingestion-pipeline.md had the GitHub Issues token chain backwards (per-source token is the LAST fallback) and said Add source seeds the first provider (it starts on "Pick your tracker"). | docs/ingestion-pipeline.md ~86-92 | FIXED f66ec392 (also web-ui.md: token hints, Required-as-you-type, no Any-state on Anyone's) |
| 8 | low | Step 3 "What it does": the Repo URL box is already red-bordered on arrival, before the user touched it. | Add Shortcut → good token → step 3 | DEFERRED (cosmetic; RepoListField) |
| 9 | low | Tickets tab shows two "auto-start is off" banners ("Auto-start is off everywhere. Turn on ›" and the "Auto-start: off" box); "Turn on ›" only switches tabs. | After Finish | DEFERRED (copy/layout call for the parent) |
| 10 | low | The intro "Connect a tracker. …" stays once a tracker is connected. | Tickets tab with a source | DEFERRED (nit) |
| 11 | low | Edit card: an Anyone's-without-state error shows the "Not saved" pill on the **Connect** step chip, while the problem is on **What it takes**. Same fact is also said three times (red error, field hint, empty-state box). | Edit → Anyone's → remove last state | DEFERRED |

Notes for later passes
- Fake-API harness: `~/polish-scratch/d/fakesite/sitecustomize.py` (rewrites aiohttp/urllib/httpx to
  `POLISH_FAKE_API`) + `~/polish-scratch/d/tools/fakeapi.py` + `lab.py` (LabServer subclass that
  prepends the fakesite to PYTHONPATH). `walk.js` with `BUILD=1` routes `/app.js`, `/style.css`
  and chunks to a scratch `vite build --outDir` so UI fixes can be seen without touching static/.
