# Polish loop ledger

One area per pass. Areas: (a) install/doctor/onboarding, (b) Settings → Voice,
(c) agent CLI / provider setup, (d) Intake sources, (e) Devices (phone,
tailscale, guest links), (f) peer links, (g) flights/routes UI, (h) MCP worker
flows. Never redo a FIXED item.

## Areas walked

| Pass | Area | How |
|---|---|---|
| 1 | (h) MCP worker flows | ux-lab sandbox server (fresh HOME, private tmux, stub claude) + a stdio driver for `python -m backend.mcp` as orchestrator `orch` and worker `w-fence`: whoami, spawn_session (fence-only, `after=`), set_order (view, start_now), fence_session, report_result, get_session. Plus a read-only look at the live `~/.mindflock/worker_order.json` + `prompt_queues.json` for the seed bug. |

## Findings

| # | Pass | Sev | Finding | Repro / where | Status |
|---|---|---|---|---|---|
| 1 | 1 | high | Seed: fenced worker's task never arrives (`prompt_delivery: held`, queue `{pending:1, enabled:false}`, set_order start_now → "not one of your workers"). Root cause: **two servers share `~/.mindflock`** (:8765 service + :9000 dev launcher). The one that did NOT create the worker hasn't adopted it yet (state sync every ~4 s), and its order pass read "not in my engine" as deleted → `forget` the held worker (record gone, task parked behind held+off queue for ever). A running worker was marked `done/gone` (polish-loop itself: done 1.07 s after spawn), which would release its dependents early. | `worker_order_driver.observe` / `_pass_sync` write-back; live store showed `stt-moonshine` queue held with no record, `polish-loop` done/gone | FIXED 7cc13e06 |
| 2 | 1 | high | Same cluster: `_sweep_fences` drops an orchestrator fence whose session isn't in THIS engine → other server's fenced worker loses its fence ("ended"); record still says applied, so it runs unfenced. | `worker_order_driver._sweep_fences` | FIXED 4290ca12 |
| 3 | 1 | med | Already-stranded held tasks (no order record) never heal; `set_order(start_now)` can't release them. | stt-moonshine on the live server | FIXED 7cc13e06 (pass hands orphaned held tasks over after 30 s; start_now releases its own record-less worker) |
| 4 | 1 | med | Worker rows' task headline reads MindFlock's wrapper, not the task: fenced worker → "Call mcpmindflockreport_result", held worker → "Commit changes on current branch". | sandbox `w-fence` / `w-after` rows; `backend/task_headline.py` | FIXED 4d66c29e |
| 5 | 1 | low | A spawn queued only by start spacing (20 s) says "MindFlock starts it once memory allows" while capacity shows room for 12. | spawn two workers back to back; `mcp/tools.py` queued hint, `mcp/ship.py` | FIXED 6cd79d7b (hint points at `reason`; status literal kept — clients match on it) |
| 6 | 1 | low | Order row detail has a trailing space: "MindFlock held this task until w-fence finished. " (empty carry). | `worker_order_driver._release` | DEFERRED (cosmetic) |
| 7 | 1 | low | `wait_for_session` rejects `timeout` ("accepted: … timeout_s") — clear error, but other tools take `timeout`? | MCP | DEFERRED (error already names the fix) |
| 8 | 1 | ? | Other loops may judge "gone" from their own engine only: `flight_driver.py` ~2214/~2679, `team_run_driver.py` ~978, `red_zone_monitor.py` ~474. Not walked yet. | | TO CHECK (go deeper next (h) visit) |

## Notes for later passes
- Sandbox: `env -u CS_WEB_MODE -u MINDFLOCK_REPO_ROOT -u PORT -u TMUX … uv run python ~/MindFlock/ux-lab/sandbox.py --scratch <dir>`; create a session with `POST /api/instances {"title","repo_path","program":"claude"}`; drive MCP with `MINDFLOCK_SESSION_TITLE=<title> MINDFLOCK_PORT=<port> HOME=<sandbox home> python -P -m backend.mcp` over stdio.
- A single sandbox server can't reproduce the two-server race; the tests simulate it (`fake.disk` = titles live on disk elsewhere).
