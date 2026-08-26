# Web API reference

FastAPI app: `backend.web.server:app`. All routes are registered before the static
mount at `/`, so `/api/*` always wins. UI pages (`/`, `/m`, `*.html/.css/.js`) are
served with `Cache-Control: no-cache`.

Conventions: JSON bodies in, JSON out. Errors use standard status codes with
`{"detail": ...}` or `{"error": ...}`. `{title}` is the session title.

## Instances

### `GET /api/instances`

List all sessions. Each item:

```jsonc
{
  "title": "sc-19815", "branch": "feature/sc-19815/…", "repo": "…",
  "folder": "/abs/workspace", "folder_label": "…", "program": "claude",
  "path": "…", "status": "running|ready|loading|paused", "started": true,
  "tmux_name": "mindflock_sc-19815",
  "provisioned": true, "workspace_strategy": "worktree", "in_place": false,
  "stage": "provisioning|agent|precommit|interrupt|committed|pushed|pr|merged",
  "pr_url": "…",              // when a PR exists
  "failed_step": "…",         // when stage == "interrupt" (pre-commit ✗)
  "stage_reset": false,       // ↺ "back to idle" pin — show the ladder from the start
  "activity": "working|clarify|idle|offline",
  "activity_since": 1756200000.0,  // epoch the reported activity last changed; 0 = no live reading
  "tokens": 0, "tokens_in": 0, "tokens_cache_read": 0, "tokens_cache_write": 0,
  "tokens_ctx": 0, "tokens_ctx_window": 200000, "tokens_cost": 0.0,
  "tokens_model": "…",
  "diff_stat": {"files": 2, "additions": 42, "deletions": 7},  // or null
  "workspace_missing": false,  // L1(c): started but its directory vanished
  "has_origin": true,          // L2: workspace has an `origin` remote (cached ~30s)
  "last_turn": "…"             // L3: ≤120-char snippet of the latest agent turn, or null
}
```

Stage is inferred from git (upstream, origin SHA, commit lock files) plus a
GitHub lookup for the PR stages — `gh` when it is installed and authenticated,
otherwise the REST API with a resolved token. With neither credential the badge
still advances all the way to **pushed** (that part is pure git) but cannot see
an open PR, so it stops there. Because stage comes from git, work done outside
MindFlock (e.g. in Cursor) moves the badge too. The origin
branch SHA is a network `git ls-remote`, cached ~45 s — a push made outside
MindFlock can take up to ~45 s to advance the badge and enable **Make PR**
(MindFlock-initiated pushes bypass the cache via a pending window). The base
branch used for stage/diff/PR is **per session**: the branch the worktree was
cut from, recorded at creation (`base_branch` in `state.json`); sessions that
predate the field resolve `origin/HEAD` → `main`/`master` → the configured
provision base (only when the session's repo IS the configured repo). Existing-PR
detection (what advances the chip past `pushed` to `pr`) looks up the PR by
**head branch only** and is intentionally base-agnostic — Make PR can target any
base (a configured default like `staging`, or one chosen in the dialog the
server never sees), so a base-scoped lookup would miss the real PR and wedge the
chip on `pushed`. The base still keys the "is there work to push" (`beyond`)
test; only the PR lookup drops the base filter.
`stage_reset` is the **↺ "back to idle"** pin
(`POST /api/instances/{title}/reset-stage`, below), published *alongside*
`stage` and never instead of it. The row's `stage` stays exactly what git says,
so the autopilot driver, the verification-check kicker and every `*_changed`
event keep reading the same git-derived truth; only the UI's guided ladder
(chip, primary button, live step) honours the pin. Folding the pin into `stage`
would let an armed fast-track chain try to commit a clean tree. The pin is
process memory (never persisted, and pruned when its session goes) and releases
itself against the **worktree** — a dirty tree or a moved HEAD drops it on the
next stage read — never against the stage label, since filing a PR flips
`pushed` → `pr` a beat after it is set.
`activity` is layered: the provider's own authoritative signal is preferred —
per-session `{state, ts}` markers written by the CLI's lifecycle hooks, or
Claude's live `claude agents --json` report (see
[providers.md](providers.md)) — then CPU sampling of the pane's process tree
(a cached `/proc` scan, 2.0 s TTL kept deliberately below the server's 2.5 s
probe memo so two consecutive activity computations never share a snapshot and
read a zero CPU delta — phantom idle), then the pane-hash heuristic: changing
= `working`, provider "waiting" patterns = `clarify`, static ≥ 3 s = `idle`,
no tmux = `offline`.

`activity_since` is the epoch that **reported** activity last changed value
(`agent_state.state_since`, stamped by every classification layer, not just the
pane one), so the UI can rank how long a session has been in its current state
— attention ordering and the sidebar's wedged-session watchdog. It is `0` for a
session with no live reading at all (offline, never started), and consumers
should treat `0` as unknown rather than "changed at the epoch". **Re-check any
consumer against live values**: this field previously read a key nothing had
ever written, so it answered `0.0` for every session, and anything gated on
`activity_since > 0` — the wedged-session branch included — had never once run.

`diff_stat` (J3) is the total change the session has produced vs its
per-session base — committed-beyond-base **plus** uncommitted tracked changes,
one `git diff --shortstat <merge-base(base, HEAD)>` cached ~10 s per session.
Untracked files aren't counted (counting them would mutate the index on every
poll). `null` when unavailable (paused / loading / no worktree / git failure).

`workspace_missing` (L1c) is `true` for a started, non-paused session whose
workspace directory no longer exists (wiped outside MindFlock). The UI renders
these as a muted row + placeholder pane with a single **Clean up** action
(`DELETE /api/instances/{title}`, which tolerates the missing dir).
`has_origin` (L2) tells the UI to replace **Push** with setup guidance when the
workspace has no `origin` remote — pushing would only fail in the shell.
`last_turn` (L3) is a one-line, markdown-stripped snippet of the session's
latest conversational turn (provider-dependent; `null` when the provider
doesn't expose one) for at-a-glance triage across many sessions.

### `POST /api/instances` → **202**

Create + start a session. Provisioning runs in the background; the session
appears as `loading` until ready.

```jsonc
{
  "title": "sc-19815",          // optional with story_id (defaults to sc-<id>)
  "program": "claude",          // optional; default from ~/.mindflock/config.json
  "repo_path": "/path/to/repo", // base repo (plain sessions; optional for provisioned)
  "in_place": false,            // run directly in repo_path, no worktree
  "init_repo": false,           // git init + initial commit if needed
  "provisioned": true,          // provision a fully-loaded workspace
  "workspace_strategy": "worktree", // or "clone"
  "story_id": "19815",          // → branch feature/sc-19815/<slug>
  "prompt": "…ticket text…",    // seeds the agent on first launch
  "launch_args": ["--dangerously-skip-permissions"] // optional per-session flags
}
```

A full slash-path in the title (e.g. `feature/sc-1/foo`) is used verbatim as the
branch. `provisioned` + `repo_path` provisions **that local repo** (setup
commands auto-detected, no shared cache seeds); `provisioned` without
`repo_path` requires the configured `[repository].url`. Errors: 400 (empty
title, bad strategy/config), 409 (title exists).

`launch_args` (optional) are extra CLI flags appended on **every** (re)start of
this session's agent, after the provider's own saved flags. They are validated
with the same rules as provider `[launch] args` (a list of non-empty tokens; no
newlines/NULs; ≤512 chars each) → **400** on invalid input. Omitting the key
means "not specified", so the session inherits the global default for its
provider (`coding_cli.default_launch_args`, see
[configuration.md](configuration.md)); an explicit list — **even `[]`** — is used
verbatim, so a default the caller toggled off is honored, not re-applied.

### Lifecycle

| Method | Path | Effect |
|---|---|---|
| DELETE | `/api/instances/{title}` | Kill session, remove worktree + branch |
| POST | `/api/instances/{title}/close` | End tmux, **keep** worktree; recorded in recently-closed |
| POST | `/api/instances/{title}/cleanup` | Kill + permanently delete the workspace dir (+ close its Cursor window) |
| POST | `/api/instances/{title}/copy` → 202 | New in-place session `<title>-copy` sharing the same worktree |
| POST | `/api/instances/{title}/pause` | Pause (commit, detach, remove worktree, keep branch) |
| POST | `/api/instances/{title}/resume` | Resume a paused session |

### Diff

| Method | Path | Returns |
|---|---|---|
| GET | `/api/instances/{title}/diff?base=fork\|head` | `{added, removed, content, base, error}` — `base=fork` (default) diffs vs the session's fork point, `base=head` vs the current HEAD |
| GET | `/api/instances/{title}/file-diff?path=<rel>&base=fork\|head` | Whole-file unified diff `{content, error}` |

### Guided workflow (commit → push → PR → merge)

Commit and push are pure git: they run in the session's own shell against the
remote the repo already has, **SSH or HTTPS, used verbatim**, with the user's
own git credentials. The GitHub CLI is not involved and no remote URL is ever
rewritten. Only the two PR endpoints need to reach the GitHub API, and each
resolves a credential in the same order — `gh` (when installed *and*
authenticated) → the REST API with a token
(`backend.ticket_ingestion.github_auth.resolve_token`) → a browser URL. There is
no response whose only content is "gh is not installed".

| Method | Path | Behavior |
|---|---|---|
| POST | `/api/instances/{title}/commit` | Body `{message}`. Runs `git add -A` + `git commit` **in the session's shell tmux** (watch pre-commit hooks in the Terminal tab), retrying up to 5× when hooks auto-fix files. Works for every session type (plain, in-place, provisioned). Writes `.mindflock_commit_status` (exit code) and `.mindflock_commit_msg` (reused on empty re-commit). |
| GET | `/api/instances/{title}/commit-message` | `{message}` — the message of a commit the pre-commit hooks blocked, so the Commit dialog can offer it back instead of making you retype it. Reads `.mindflock_commit_msg` (the file `git commit -F` uses), but **only when `.mindflock_commit_status` records a non-zero exit** — the same condition that raises the `interrupt` stage. Absent or successful status → `{"message": ""}`, since a committed message pre-filled into the next commit is worse than an empty box. 404 unknown title, 409 workspace not ready |
| POST | `/api/instances/{title}/push-branch` | `git push --no-verify -u origin HEAD` in the shell (hooks already ran on commit). **O3 soft gate:** when the repo's `.mindflock.toml` declares `check_command` and no check run passed against the current HEAD, returns `409 {error, check_required: true, check}`; re-POST with body `{"force": true}` to push anyway. |
| GET | `/api/instances/{title}/branches` | `{branches, current, default}` — the branch list backing the **Make PR** dialog's base picker. `branches` are `origin`'s remote heads (falling back to local heads when origin is unreachable, so it's never blank); `current` is the session's own branch (never a valid PR target); `default` is the pre-selected base (`repository.pr_base_branch` → the session's fork base). 404 unknown title, 409 workspace not ready |
| POST | `/api/instances/{title}/make-pr` | Opens a PR → `{ok: true, url}` (or `note: "PR already open"`). Three tiers, in order: `gh pr create --base <base> --fill` when `gh` is installed **and** authenticated; else the GitHub REST API with a token from the usual resolution chain; else **`200 {ok: false, compare_url}`** — a prefilled compare URL the UI opens in the browser, plus the remedy sentence "add a GitHub token in Intake → Pull requests, or install the GitHub CLI". A missing `gh` is never an error status. The UI's Make-PR dialog collects `<base>` from the branch picker above (and the frontend remembers the last base per repo — `prBaseByRepo` in `localStorage`); an omitted base falls back to the session's base branch |
| POST | `/api/instances/{title}/merge-pr` | Merges the branch's PR, same three tiers: `gh pr merge <branch> --merge`; else the REST API with a token; else **`200 {ok: false, pr_url}`** so the UI can send you to the PR page to merge it yourself |
| POST | `/api/instances/{title}/reset-stage` | ↺ **back to idle** — pins this window's guided ladder back to its start on a clean branch, so the header stops insisting on Push / Make PR / Merge while you keep working on the same branch. **Nothing git-facing happens**: no reset, no revert, no PR close, and the published `stage` is untouched — the pin (`backend/web/core/stage_reset.py`) rides the row as `stage_reset` and only the UI ladder reads it. It releases itself when the worktree moves (dirty tree or new commit), never on the stage label. Also takes down the finished cycle's leftovers: a **halted** fast-track record (a live chain is left strictly alone) and a **stale** verification result (a current failure is never touched — the push gate reads it). → `{ok: true, pinned, dirty, cleared[], row}`, where `row` is the recomputed instance row so the presser's window flips now rather than on the next tick, and `pinned` is `false` on an already-dirty tree (that ladder is at its start already). 404 unknown title, 409 workspace not ready |

### Assigned tickets, PR auto-review + issue handling

| Method | Path | Behavior |
|---|---|---|
| GET | `/api/tickets` | Assigned tickets on the configured ticketing sources, each annotated with auto-ingest eligibility → `{tickets, sources, source_labels, buckets, done_buckets, ingest_states, errors[], stale}` (per ticket: `source`, `source_label`, `bucket`, `eligible`, `reasons`, `has_session`). The list is grouped by **source** and then by workflow-state bucket, so `source_labels` maps the key of EVERY configured source → its display label — including sources that returned nothing and ones that landed in `errors[]`, which deriving labels from the ticket rows alone would make vanish (`sources` is the subset that answered). The slowest of the three panel fan-outs (~3 s: one provider search per source + a `git ls-remote` per repo); per-source failures come back in `errors[]` rather than failing the call. Powers Intake → **Tickets** → **Assigned tickets** |
| POST | `/api/tickets/start` | Body `{source, id, agent?}` — force-start a coding session for one ticket, bypassing the auto-ingest filters. `agent` is the coding CLI for **this one launch** (the picker beside **Begin work**) and outranks the source's own; omit it — or send `""` — to use the configured chain (the source's Agent CLI, then `[mindflock].agent`, then the app default). 400 missing `source`/`id` or an `agent` no provider answers to, 404 ticket gone, 409 a session for it already exists |
| GET | `/api/github/prs` | Open PRs on the watched repos, each annotated with why auto-review did / didn't pick it up. **Every** open non-draft PR is listed, whatever it targets: a PR into a branch its repo isn't watching comes back with the skip reason `targets X, not the watched base (Y)` instead of being filtered out server-side, so the row is visible and still force-reviewable (the auto monitor, which asks GitHub only for the watched base, would never see it). The watched base is per repo — `github.repo_settings[repo].base_branch`, else the tab-wide `github.base_branch` |
| POST | `/api/github/prs/review` | Body `{repo, number, agent?}` — force-start a review session for one open PR (**Begin review** in Intake → Pull requests), bypassing the auto filters, a non-matching base included. `agent` is this launch's coding CLI and outranks the repo card's; blank falls through to the same chain the monitor uses (`github.repo_settings[repo].agent` → `github.agent` → `[mindflock].agent` → the app default). 400 bad `owner/name`/number or an unknown `agent`, 404 no such open PR, 409 a session for it already exists |
| GET | `/api/github/issues` | Open issues on the issue-handling repos (`github.issue_repos`), each annotated with auto-handling eligibility (`eligible`, `reasons`, `has_session`). PRs filtered out. Powers Intake → **Issues** |
| POST | `/api/github/issues/start` | Body `{repo, number, agent?}` — force-start a coding session for one open issue on a fresh branch, bypassing the age / already-handled filters. `agent` is this launch's coding CLI, outranking the repo card's (`github.issue_repo_settings[repo].agent` → `github.issue_agent` → `[mindflock].agent` → the app default). 400 bad `owner/name` or number or an unknown `agent`, 404 issue gone, 409 a session for it already exists |
| POST | `/api/intake/reopen` | Body `{kind}` — `tickets` \| `prs` \| `issues` — plus the same item identity that kind's start route takes (`{source, id}` for a ticket, `{repo, number}` for a PR or issue). Puts a session back on the workspace an earlier run of that item left on this machine instead of starting it over (**Reopen window**). The **server** re-resolves the workspace from the row in the panel's cached listing — never a path the client sends — so a tab left open for an hour cannot name a directory that has since been deleted: `backend/web/core/reopen.py` tries a recently-closed session for the item, then its provisioned clone directory, then a worktree still holding its branch, each gated on a real `.git`. A closed session is restored in full through the undo store (branch, program, prompt, provisioning flags); a workspace whose session was lost to a restart gets a fresh **in-place** session on the directory, in-place because the directory is not MindFlock's to delete. **200** carries the restored session's row, **202** the freshly opened one. 400 unknown `kind` or a payload that doesn't identify an item, 409 the session is already open (or the panel's list no longer holds the row — Refresh it), 410 no workspace for this item is left on this machine |

The three **POST** force-start routes above share one family of *per-launch*
overrides, each optional and each meaning "just this item, not the whole queue"
(they are the pickers beside **Begin work** / **Begin review** / **Start now**):

- `agent` — the coding CLI, outranking the source's / repo card's own.
- `depth` — how far the autopilot carries it (`agent`, `commit`, `push`, `pr`,
  `merge`). Unlike a per-source default an individual item **may** choose
  `merge`: the person picking it is looking at the one thing it will merge.
- `effort` — how hard the agent thinks about it, on one neutral ladder:
  `low`, `medium`, `high`, `xhigh`, `max`, `ultra`. The rung is translated into
  whichever CLI the start resolves to (`claude --effort xhigh`, `codex -c
  model_reasoning_effort=high`, `agy --effort high`) and **clamps** to that CLI's
  ceiling rather than being forwarded — a level a CLI doesn't know is either
  ignored with a warning or rejected by its API. `ultra` is that CLI's own
  beyond-the-ladder mode rather than a sixth rung: a level name its flag takes
  (Claude Code: `--effort ultracode`) or, failing that, a keyword appended to the
  seed prompt; a CLI with no effort setting at all ignores the field. The flags
  ride on the session's launch args, so a relaunch or a reboot-resume keeps the
  effort. Each CLI's rungs are published on `/api/providers` (`effort.levels`,
  plus `effort.ultra_level` / `effort.keyword`) so the picker can name both where
  that CLI tops out and what it calls the top.

Omitting a field — or sending `""` — keeps the configured behaviour; a value none
of them recognises is a **400** rather than a silent downgrade.

The three **GET** panel routes above share one caching contract
(`_cached_fanout` in `server.py`), because each is an upstream fan-out one of
the three Intake tabs polls while open:

- **≤20 s old** → the cached payload, `stale: false`.
- **20 s – 5 min old** → the cached payload is returned *immediately* with
  `stale: true`, and a single-flight background refresh sweeps upstream. Clients
  use `stale` to come back for the fresh copy in a moment (the UI re-polls every
  2 s while it is set) instead of sitting on data they know is being replaced.
  Those re-polls are cheap: a failed sweep backs off for 30 s, so they don't each
  turn into another request to an upstream that is already failing.
- **Older than 5 min, nothing cached, or `?fresh=1`** → the request awaits a real
  sweep. `fresh=1` is what each tab's **Refresh** button sends. (Note the
  spelling: these routes take `?fresh=1`, while `/api/doctor` and
  `/api/connections` take `?refresh=1`.)

**502** `{error}` is therefore returned only when there is no usable cached
payload — or when `fresh=1` asked for a real sweep. Once a panel has any payload
inside the 5-minute stale window, an upstream failure is logged and the last
known list keeps being served, so a GitHub/provider blip can't empty the panel;
the flip side is that a persistently failing upstream stays invisible to the
client for up to 5 minutes. `has_session` is annotated on a per-request copy, so
it stays live on cache hits. So is `workspace` — the reopenable directory an
earlier run of the row left behind (`{kind: closed|clone|worktree, path, branch,
entry_id?}`, absent when there is none), which is what puts **Reopen window** on
the row. That probe is read-only and best-effort — any failure annotates
nothing — and is per *pass*, not per row: the recently-closed store is read
once, each candidate directory is stat'd once, and each repo answers one
`git worktree list`, all indexed for the whole response. Rows that already have
a live session are skipped.

### Verify — checklists for what shipped

The back of the pipeline: work that reaches a repo's **live branch** gets a
model-written checklist, an agent settles the steps it can from a shell, and the
rest comes back to a person. See [web-ui.md](web-ui.md#verify) for the surface
and [configuration.md](configuration.md) for `.mindflock.toml`. Store:
`backend/web/core/test_plans.py` (`test_plans.json`); events:
`session.test_plan_ready`, `session.test_plan_failed`,
`session.test_plan_due`, `session.test_plan_checked`,
`session.test_plan_gave_up`. They are deliberately separate: *ready* means
"there are steps worth showing" (what the dialog refetches on), *failed* means a
generation or rewrite could not be written, *due* means the world changed
underneath the checklist — the sha reached the live branch and the deploy window
passed — *checked* means an agent finished working one, carrying `failed` and
`needs_you` so the client can say what is left without a second fetch, and
*gave_up* means a run was released after the two-hour deadline without ever
writing its answers (carrying `run_session` and `hours`). That last one used to
be silent: the plan simply went back to "not checked yet", so a session that had
run for two hours and reported nothing was indistinguishable from a button
nobody pressed.

A plan's **id is its session title**, so it can contain slashes — every route
below uses a `{plan_id:path}` converter, and clients must percent-encode it.

| Method | Path | Behavior |
|---|---|---|
| GET | `/api/test-plans` | `{plans[], live_branch}` — every checklist, newest first. `live_branch` is the **flock-wide** default resolved fresh per request; each plan additionally carries `effective_live_branch`, the same question asked for *that plan's repo*. Compare a plan against its own, never against the top-level one, or every plan in a repo with an override reads as out of date. Per-plan: `state`, `steps[]` (`id`, `text`, `expect`, `actor`, `manual?`), `runs[]` (capped, newest kept), `run_session`, `branch`, `sha`, `tip_sha`, `refreshes`, `merged_at`, `live_at`, `merged_into`, `merged_into_at`, `merged_into_all`, `gen_started`, `gen_attempts`, `error`, `summary`, `intent`, `focus`, `notified_at`, `live_problem`. **`merged_into`** is the branch on `origin` the work has most recently reached (`""` while it is still only on the branch it was pushed to) — *not* `live_branch`, which is the branch the checklist is waiting for: in a repo that ships through a `staging` step the two disagree for most of a change's life, and the disagreement is the interesting part. `merged_into_at` is when it got there (`0` when the rung that answered could not say, i.e. the squash-merge case); `merged_into_all` is the trail, best first and one name per landing, so it is not "every branch that contains the commit" — every branch cut from `main` after a merge does. **`live_problem`** is why a checklist is not coming due when the answer is not "not yet" — origin has no such branch, or the branch's PR merged into a different one. Distinct from `error` (which means an operation you asked for failed); it clears itself. **`summary`** is the model's own one-sentence statement of what the change lets somebody do — what `title` (the session's name) could never be. **`intent`** is what the work was *asked* to do, snapshotted at push time from the ticket or the seed prompt; it is stored on the plan rather than read off the session, because plans outlive sessions and a rewrite that read it live ran with no ticket at all. **`focus`** is what you told the last rewrite it got wrong. The snapshotted session transcript is **not** on the wire: it is generation input, never UI. **`sha` vs `tip_sha`**: `sha` is the liveness anchor — written once and never moved, because ancestry is transitive and moving it forward could only make a checklist come due later or never. `tip_sha` is the newest commit seen pushed on the branch; it is what the diff is read at and what a pre-live run checks out, and it is recorded on every push whether or not a rewrite follows |
| POST | `/api/instances/{title}/test-plan` | Write a checklist for one session **by hand**, with no repo opted in → **202** `{plan, existing}`. Works for a **closed** session too, falling back to the recently-closed store for its branch and repo (409 when that branch is gone from the repo, 404 when neither store knows the name) — a checklist outlives its session everywhere else here, and creation demanding a live window made the button useless at the moment people ask for one. A closed session carries no seed prompt, so such a plan has no `intent` and is written from the diff alone. A headless one-shot reads the branch's diff and answers in up to three minutes; `existing: true` points at the plan that is already there rather than erroring (a **200**, not an error — the honest answer to "write one for this" when one exists is to point at it). 404 unknown session, 409 no workspace yet / nothing committed on this branch / a detached HEAD |
| POST | `/api/test-plans/{plan_id:path}/run` | Start a real session that works the checklist's **agent** steps → `{session}`. Optional `{"steps": ["s3"]}` narrows it to those steps (the per-step re-check). 409 a run is already going / the plan has no steps / **every step is a person's** (an agent is forbidden from settling those, so a run would provision a workspace to hand the list straight back), 400 naming an unknown or `human` step |
| POST | `/api/test-plans/{plan_id:path}/result` | Body `{step_id, result, note}` — record one step's outcome as a **person** → `{plan}`. `result` is `pass` \| `fail` \| `blocked` \| `""` (empty un-answers it). Unlike the file a verify session writes, an unrecognised value is a **400** rather than being coerced. Answering the last outstanding step closes the plan — unless a run is still in flight, in which case the answer is recorded and `finish_run` performs the transition, so a mid-run answer cannot strand the agent working beside it. 404 unknown plan or step |
| POST | `/api/test-plans/{plan_id:path}/steps` | Body `{text, expect?, actor?}` — append a step a person wrote → `{plan}`. Marked `manual`, so it survives a regenerate. 400 empty text / bad `actor` / at the 25-step cap, 404 unknown plan |
| DELETE | `/api/test-plans/{plan_id:path}/steps/{step_id}` | Remove a step **a person added** → `{plan}`; recorded answers for it go too. 400 for a generated step (nothing would bring it back), 404 unknown plan or step |
| PATCH | `/api/test-plans/{plan_id:path}/steps/{step_id}` | Body `{text?, expect?, actor?}` — fix one step in place → `{plan}`. An absent key leaves that field alone. The step becomes `manual`, so the next rewrite keeps your wording. Changing `text` or `expect` changes the *question*, so any answer recorded against it is dropped; changing only `actor` keeps every answer, because who answers is not what is being asked. Also the only way out of a checklist an agent cannot run — an unrecognised `actor` is coerced to `human`, and the run route refuses a plan whose every step is a person's. 400 nothing to change / bad `actor` / a run is in flight, 404 unknown plan or step |
| POST | `/api/test-plans/{plan_id:path}/regenerate` | Re-ask the model for the steps → **202**. Optional body `{"focus": "…"}` — what the last draft got wrong, in your words; it is stored on the plan (so a later push keeps honouring it), placed above the ticket in the prompt, and cannot change the format the model must answer in. Steps marked `manual` (added *or* edited by you) are kept; answers recorded against steps that change are lost. A rewrite never un-ships a plan: one that has already gone live comes back `due`/`done`, not `generated`, and its "it shipped" push is never re-sent. **409** while a run is in flight — `generate` would set `generating` while the poller only looks at `running`, orphaning a real billed session. 404 unknown plan |
| POST | `/api/test-plans/{plan_id:path}/deployed` | Skip the rest of the repo's deploy window and make a merged checklist due now → `{plan}`. **`merged_at` vs `live_at`**: `merged_at` is when the work was first seen on the live branch, `live_at` is when it became yours to check — the gap is the deploy wait (`repository.deploy_delay_minutes`, or the repo's own `deploy_delay_minutes`, default 5). 409 unless the checklist is `generated` **and** merged; refusing rather than being idempotent, because on a `done` plan this would silently reopen a finished checklist |
| POST | `/api/test-plans/{plan_id:path}/cancel` | Stop a run without recording a verdict → `{session, plan}`. The verify session is **closed, not deleted**, so whatever it found is still readable in Recently closed. 404 unknown plan |
| DELETE | `/api/test-plans/{plan_id:path}` | Forget the checklist and its run history → `{ok, closed}`. Stops the run session first when one is going (`closed: true`) — it would otherwise be answering a checklist that no longer exists. 404 unknown plan |

**Who may settle what is enforced server-side, not requested.** The run prompt
tells the agent to leave `human` steps blocked, but a prompt is a request: any
answer an agent gives to a `human` step is stored as `blocked` (keeping its
note), and an agent's report never overwrites an answer a person already
recorded. Symmetrically, `blocked` means two different things depending on `by`
— an **agent's** blocked keeps the checklist open ("a person has to look at
this"), a **person's** blocked ("Can't check" in the UI) settles it. Neither is
ever a pass: the verdict is recomputed from the step results and stays `partial`.

**Opting a repo in** is `repository.verify_repos` (`owner/name`, matched
case-insensitively) OR'd with the repo's own committed `[workspace]
verify_on_push = true` — the only opt-in available to a checkout with no GitHub
origin. `repository.verify_enabled` is the master switch and pauses the automatic
half only (writing on push, and the liveness pass); the routes above keep
working. Per-repo overrides live in
`repository.verify_repo_settings[owner/name]` (`live_branch`,
`deploy_delay_minutes`, `target`, `prompt`) — see
[configuration.md](configuration.md) for what each decides.

**What a green checklist is evidence of.** MindFlock knows one hard fact — your
commit is an ancestor of the branch you ship from — and waits out a guess at
your pipeline on top of it (`deploy_delay_minutes`). What happens next depends
on one setting. With a repo's `target` set, a run exercises the steps against
**that deployment**, which is the system your users are on. With it blank, a run
checks out `origin/<live branch>` **in a linked worktree on this machine** and
exercises the steps there — so a green checklist means *the code users are
getting behaves*, not *the deployment is healthy*. Steps marked `human` are the
half that can always touch the real thing, which is why the generator hands
anything needing a real browser — or a service the agent has no tool for — to a
person rather than settling it from a shell. Log lines, dashboards and metrics
are the agent's when it carries observability tooling (e.g. Grafana MCP), and
the run prompt says so. A verify session writes its answers to
`.mindflock_verify.json` in its worktree root (git-excluded, and the only file
it is permitted to write).

**Cadence.** One background loop, two speeds: the full pass (housekeeping,
stalled-generation recovery, and a `git fetch` per waiting plan, within a
wall-clock budget) runs every 60 s, and while any plan is `running` the loop also
wakes every few seconds to do the purely local half — reading each run's result
file — so a finished run is reflected almost immediately rather than up to a
minute later.

### Worktree setup + verification gate (O2/O3)

Configured per repo by a committed `.mindflock.toml` (see
[configuration.md](configuration.md#mindflocktoml--per-repo-workspace-config)).
Status lives in marker files inside the worktree
(`.mindflock_setup.json` / `.mindflock_check.json`, git-excluded), so it
survives server restarts. Events: `session.setup_started/finished`,
`session.check_started/finished`.

| Method | Path | Behavior |
|---|---|---|
| GET | `/api/instances/{title}/setup?lines=200` | `{status, log}` — setup state (`running/ok/failed`, rc, copied files) + log tail |
| POST | `/api/instances/{title}/setup/rerun` | Re-run the setup pass (copy `copy_untracked` + `setup_commands`) → 202; 400 without config, 409 while running |
| GET | `/api/instances/{title}/check?lines=200` | `{status, log}` — check state incl. `sha` it ran against and `stale` vs current HEAD |
| POST | `/api/instances/{title}/check` | Run `check_command` in the worktree → 202; 400 without config, 409 while running. Also auto-runs when a session reaches the `committed`/`pushed` stage with no (or a stale) result. |

### IDE

| Method | Path | Behavior |
|---|---|---|
| POST | `/api/instances/{title}/ide` | Open/focus the workspace in the configured IDE (Settings → Advanced; Cursor by default). GUI editors get new windows maximized + existing ones focused; terminal editors open in a new terminal window → `{ok, opened_new}`; 400 if the IDE isn't launchable |
| GET | `/api/ides` | The known-IDE registry: `{ides: [{command, name, kind, installed}], current, current_name}` — for the Settings detected-IDE picker |
| GET/POST | `/api/cursor/autoadopt` | Get/set `{enabled}` — auto-adopt Cursor-opened workspaces as sessions |

### Send a message + prompt queue

| Method | Path | Behavior |
|---|---|---|
| POST | `/api/instances/{title}/send` | Body `{text, submit?}`. Types `text` into the **agent** window and (default) presses Enter, booting/resuming the agent tmux first if it isn't running — so one call kicks a fresh session into motion (max token use). `submit:false` types without submitting. Enter is a separate keystroke a beat after the text so an agent TUI doesn't read the burst as a paste. → `{sent, submitted}` (409 if the workspace is gone or `{budget_locked: true}` when the session is over budget, 502 if the send fails) |
| GET | `/api/instances/{title}/queue` | `{items: [{id, text, added}], pending, enabled, loop, loop_interval, wait_for_limit, limited_until, last_sent}` |
| POST | `/api/instances/{title}/queue` | Body `{text, index?}` — append a prompt, or insert it at a 0-based position (clamped) when `index` is given — or `{texts: [...]}` to bulk-append (one write; blank rows skipped, overflow past the queue cap dropped; response adds `added`/`skipped` counts). Enqueuing re-enables draining. → queue state |
| POST | `/api/instances/{title}/queue/flags` | Body `{enabled?, loop?, loop_interval?, wait_for_limit?}` — `enabled` gates auto-draining; `loop` re-queues each sent prompt so a self-improving prompt cycles forever; `wait_for_limit` holds draining until the usage window resets |
| POST | `/api/instances/{title}/queue/reorder` | Body `{id, index}` — move to an absolute 0-based position (clamped; the drag-and-drop path) — or `{id, direction}` (`up`/`down`) to nudge one slot |
| POST | `/api/instances/{title}/queue/edit` | Body `{id, text}` — rewrite a queued prompt in place |
| DELETE | `/api/instances/{title}/queue?item=<id>` | Remove one item; omit `item` to clear the whole queue |

A background drain loop feeds the queue into the agent whenever it is **idle**
(finished a turn / at its prompt): it never interrupts `working`/`clarify`, and
if a started session's agent tmux has died (e.g. the CLI exited when usage ran
out) it reboots it — rate-limited — so a queued run resumes on its own the
moment usage returns. Before each would-be send the drain re-checks the
usage-limit hold: a hold is armed from the provider's own usage meter **even
when no limit banner is on the pane** (covering a session that ran out mid-turn
and rebooted to a fresh idle prompt), and a window that reads spent but carries
no usable reset time holds on a bounded fallback rather than sending. A meter
that reads open — or is unavailable — leaves the queue free to send.

After MindFlock **itself** reboots a dead agent for a queued run, the drain
holds for `_QUEUE_BOOT_GRACE` (20 s) whatever the activity probe says. A CLI
relaunching with a large `--continue` transcript spends that time on a quiet,
I/O-bound start, and a quiet pane is now correctly read as `idle` — nothing on
that screen claims work is happening. Typing into a CLI that has not drawn its
input box loses the prompt, and since the send clears the queue's `armed` flag
the retry would only come after `_QUEUE_REARM_IDLE` (5 min). The old classifier
bought roughly this much grace by accident, by assuming a pane it had never seen
was working; the hold states it instead. `GET /api/instances` carries a
per-session
`queue: {pending, enabled, loop}` summary for the UI badge. Each auto-send emits
`session.prompt_sent`; queue edits emit `session.queue_changed`.

### Session budget (J5)

| Method | Path | Behavior |
|---|---|---|
| GET | `/api/instances/{title}/budget` | The session's effective budget + current estimated cost |
| POST | `/api/instances/{title}/budget/raise` | Body `{limit, hours}` — raise the budget for this session (unlocks a `budget_locked` send); emits `session.budget_raised` |

### Terminals (WebSocket)

| Path | What you get |
|---|---|
| `WS /api/instances/{title}/terminal` | The **agent** terminal — attaches (or restarts) the session's tmux |
| `WS /api/instances/{title}/shell` | An interactive **shell** in the workspace (separate tmux `<name>_sh`, created on demand, `history-limit 100000`) |
| `GET /api/instances/{title}/history` | The full tmux scrollback as text (the UI's "Copy all") |

Protocol (shared by all terminal sockets, implemented in
`static/core/ws-xterm.js` / `core/terminal.py::pump_pty`):

- binary frames server→client: raw PTY bytes (feed to xterm.js)
- text frames client→server: `{"type":"resize","cols":C,"rows":R}`
- text frames server→client: `{"type":"error","message":…}` on spawn failure
- close codes: **4404** instance gone, **4409** workspace not ready, **4500**
  spawn failure — clients stop reconnecting on 4404/4409, otherwise retry ~2.5 s
- auth close codes (any websocket, from the middleware): **4401**
  unauthenticated — the SPA/mobile head reload to the login page; **4403**
  cross-origin refusal — a hard refusal, not a login prompt (don't retry or
  re-prompt)

Detaching a socket never kills the tmux session.

## Workspaces, recently closed

| Method | Path | Behavior |
|---|---|---|
| GET | `/api/workspaces?sizes=1` | `{workspaces: [{name, path, root, kind, size_bytes, active_session}], roots}`; `kind` ∈ `base·refresher·pr·tmp·worktree·workspace` (`size_bytes` computed only with `?sizes=1`) |
| POST | `/api/workspaces/delete` | Body `{path}` (must be a direct child of a managed root; cache refreshers always protected). A base clone (`_base_*`) is deletable ONLY when nothing references it — no attached worktrees and no active session based on it; otherwise 400 naming the holders. Kills any active session on the dir first. |
| POST | `/api/workspaces/clear` | **Bulk reclaim**: sweep every managed root and delete each workspace in one pass → `{ok, removed_count, removed: [name…], kept_active: [title…]}`. Only **unprotected, idle** dirs go: protected shared infra (base clones + cache refreshers) and any dir a **live** session is using are skipped (the latter listed in `kept_active`). Unlike per-row delete it never kills a running agent. Also GCs each removed worktree's `~/.claude.json` trust entry and prunes stale worktree registrations from base clones. |
| GET | `/api/recently-closed` | `[{id, title, branch, folder, in_place, provisioned, closed_at, exists}]` |
| POST | `/api/recently-closed/{id}/reopen` | Recreate a session on the preserved worktree (410 if the dir is gone) |
| POST | `/api/recently-closed/{id}/forget` | Body `{wipe}` — drop the entry, optionally delete the worktree |

## Remote devices (tailnet)

Multi-device control (gated by the `general.remote_control` setting): other
MindFlock servers on your tailnet appear as device groups in the sidebar, their
sessions namespaced `<device>::<title>`, and every per-session route proxies to
the owning device.

| Method | Path | Behavior |
|---|---|---|
| GET | `/api/remote/hello` | Identity/permission handshake target for other devices |
| GET | `/api/devices` | Tailnet devices running MindFlock + their connection state |
| POST | `/api/devices/{device}/connect` | Pair with a device (token exchange, persisted in `~/.mindflock/remote_devices.json`) |
| POST | `/api/devices/{device}/disconnect` | Drop the pairing |

## Config, providers, usage, settings

| Method | Path | Returns / accepts |
|---|---|---|
| GET | `/api/config` | `{default_program, provisioning_available, caps: {git, tailscale, ticketing, github}, home, repo_root, ide_name, onboarded, auth_mode, auth_enabled}` — `caps` reports which optional integrations are usable right now; the UI hides absent features and shows "connect X" guidance wherever they are configured (a Settings screen, or an Intake tab via its `data-caps-need`). `caps.github` is true when **either** credential exists (`gh` authenticated **or** a token resolves) and is cached ~60 s (unlike its PATH-stat siblings it shells out to `gh auth status`, and this endpoint is hit on every page load); it gates one-click **Make PR** / **Merge**, and when false those buttons take the browser-URL path rather than disappearing — pushing is unaffected either way |
| GET | `/api/providers` | `{providers: [{name, aliases, profiles: [{id, label}], default_selector}], default}` |
| GET | `/api/usage` | Rolling day/week/month/year token+cost totals per provider (Claude, Codex, …) |
| GET/POST | `/api/scroll-speed` | `{speed}` 1–20, applied live to tmux |
| GET/POST | `/api/window-refresh` | The scheduled coding-CLI keepalive: config + per-provider `last_fired` (Settings → Agent CLI) |
| GET | `/api/browse?path=` | Directory listing `{path, parent, is_git, entries}` for the New-session dialog |
| POST | `/api/mkdir` | Body `{path, name}` (single path segment) → `{path}` |
| POST | `/api/paste-image?name=` | Save a pasted/dropped file for a session (transient retention) → its path |
| GET | `/api/logs` | Tail of the server log (the UI's System-logs pane, 3 s poll) |
| GET | `/api/addons` | Addon manifests `{addons: [{id, label, managed, frontend}]}` |
| GET | `/api/doctor` | Dependency preflight: git/tmux/agent-CLI/uv checks with per-platform fixes, plus `gh` reported as **optional** (status `info`, detail "not found (optional — only PR create/merge and PR review need it; pushing uses plain git)" — never `fail`, so it can't trip the required-dependency exit); cached ~30 s, `?refresh=1` re-probes. Also carries `version` (the running engine's version) and `state_notice` |
| POST | `/api/doctor/ack-state-notice` | Dismiss the downgrade notice; clears it and the cached payload |
| GET | `/api/mobile` | Mobile URLs + QR payload (Settings → Mobile) |
| GET | `/m` | The mobile UI page |

## Session events (WebSocket)

### `WS /api/events`

Live stream of the server-side session event bus (see
[docs/extensions.md](extensions.md)). Every frame is one JSON envelope:

```jsonc
{"seq": 42, "event": "session.status_changed", "session": "sc-19815",
 "old": "loading", "new": "running", "ts": 1719900000.0, "data": {}}
```

Events: `session.created|create_failed|deleted|paused|resumed|status_changed|
activity_changed|stage_changed|setup_started|setup_finished|check_started|
check_finished|budget_exceeded|budget_raised|prompt_sent|queue_changed|
usage_restored`
(plus addon-originated `addon.*`). `session.budget_exceeded` (J5) fires when a
session's estimated cost first crosses the configured
`general.session_budget_usd` — `data: {"cost": <float>, "budget": <float>}` —
once per session until the server restarts or the budget is changed.
`session.prompt_sent` fires when the drain loop auto-sends a queued prompt
(`data: {"text", "remaining", "loop"}`); `session.queue_changed` fires on any
queue edit (`data: {"pending", "enabled", "loop"}`). `session.usage_restored`
fires once per reopening of a provider's usage window — emitted by the same
drain-loop pass that nudges sessions parked on a limit screen to carry on
(`data: {"resumed": <bool>}`, false when `general.resume_on_usage_reset` is
off). It only ever fires for sessions that had actually run out, so it is the
"your usage is back" signal; running *out* is `session.activity_changed` with
`new == "limit"`. `session.turn_ended` is the one that says an agent has
**finished** (`data: {"idle_for": <float>}`) — see below. The notification-center
bell (frontend) curates these into a "what happened while I was away" feed.

`session.activity_changed` transitions **into** `idle`, `clarify`, or `limit`
are debounced server-side (~3s settle window, i.e. one extra ~4s tick): a
single poll can misread a busy pane as idle, and every consumer of this event —
ntfy pushes, desktop notifications, clarify toasts, shell hooks — would
otherwise fire on the flicker. A reading that reverts before it settles emits
nothing at all; transitions back to `working`/`offline` are instant.

That settle is a **flicker** filter and nothing more: it can only suppress a
reading that reverts. "The agent has finished" is a different question, and
`session.activity_changed` with `new == "idle"` is the wrong event to answer it
with — the CLI's Stop hook fires at the end of every assistant turn, so the flip
happens ten times in a ten-turn conversation, between two prompts of a draining
queue, and once more for a window that has merely been re-opened (attaching a
pane relaunches a dead agent, which then parks at an empty prompt). **`session.
turn_ended`** is the fact that answers it, and it asserts three things at once:
the agent was *observed working* in its current tmux incarnation, it has been
idle continuously for `_TURN_END_DWELL_S` (45s), and no queued prompt is waiting
to wake it. It is emitted once per cycle of observed work — the evidence is
spent on emit and re-earned by the next `working` reading — so a session left
idle overnight announces itself once. 45s is chosen to sit above every other
idle dwell in the app (`_QUEUE_IDLE_SETTLE` 12s, `autopilot.IDLE_SETTLE_S` 30s),
so the queue drains and a fast-track chain decides the agent is done *before*
anyone is told the work finished.

For ~30s after the server process starts (including a Settings-triggered
restart, which re-execs), the `status/activity/stage_changed` diff events are
swallowed entirely and `session.budget_exceeded` arms without emitting:
rediscovered sessions first register as loading/offline and then "transition"
to whatever they were parked in before the launch, which used to re-announce
the standing state of every session on every boot. The state snapshot still
updates during the window, so transitions after it diff against the truth.

On connect the server sends a **hello frame first** (L4): `seq: 0, event:
"hello"` with a `server_time` field (the server's clock), so clients can tell
replayed one-shot events (envelope `ts` < `server_time`) from live ones without
trusting their own clock — `mindflock.events.isReplay(env)` wraps this on the
frontend (L6). Then the ring-buffer backlog (~100 envelopes) is replayed;
`?since=<seq>` skips envelopes already seen, making reconnects lossless within
the buffer. Delivery is exactly-once per connection: an event emitted while the
backlog is being sent reaches the client from the backlog only, never twice
(seq-tracked). Clients only listen; a slow client loses events (bounded queue)
rather than blocking the server.

## Addon routes

**Ticket Ingestion** (addon id `mindflock`, prefix `/api/mindflock`) — controls the
pipeline as a managed subprocess (`python -m backend.ticket_ingestion` from the
repo root, own process group, singleton via `.mindflock-pipeline.lock`; also detects
and can stop an externally-started pipeline):

| Method | Path | Returns |
|---|---|---|
| GET | `/api/mindflock/status` | `{running, pid, since, log, available}` |
| POST | `/api/mindflock/start` | starts it (400 if no `config.toml`) |
| POST | `/api/mindflock/stop` | stops it (SIGTERM → SIGKILL of the process group) |
| WS | `/api/mindflock/logs` | read-only `tail -F` of `logs/ticket-ingestion.log` |

**Assistant** (addon id `assistant`, prefix `/api/assistant`) — one long-lived
`claude` tmux session in `~/.mindflock-assistant` (repo-independent), plus a todo
store:

| Method | Path | Returns |
|---|---|---|
| WS | `/api/assistant/terminal` | Interactive chat PTY |
| GET/PUT | `/api/assistant/instructions` | The assistant's standing instructions file |
| POST | `/api/assistant/restart` | Kill + relaunch the assistant tmux |
| GET | `/api/assistant/todos` | `{todos: [{id, text, done}]}` |
| PUT | `/api/assistant/todos` | Replace the full (reordered) todo array |

**Settings** (addon id `settings`, prefix `/api/settings`) — besides GET/POST
of the masked settings store (groups include `general`, e.g.
`general.session_budget_usd`, the J5 per-session cost guardrail; `0`/absent =
off), the account-attach "Test" validations (C5):

**The secret convention** (cross-cutting, not settings-only): every field the
server treats as a secret — API tokens, the ntfy access token — reads back as the
sentinel `•••set` when one is stored and `""` when none is, and a write of either
`""` **or** the sentinel *keeps* the saved value rather than clearing it. Only a
different non-empty string replaces a secret; clearing one is a deliberate
product decision per field (the ntfy token, for instance, is cleared by
retargeting the server, or explicitly with `{"clear_token": true}` — see Notify
below). The sentinel is one shared constant,
`SECRET_MASK` in `web/addons/base.py`, with a hand-mirrored counterpart in the
frontend's `settings/useSettings.tsx`; changing the string means changing both,
or the UI starts writing the literal mask into the store as a password.

| Method | Path | Returns |
|---|---|---|
| GET/POST | `/api/settings` | The masked settings store (secrets never echoed). POST **rejects** `coding_cli.default_provider` when that CLI is not installed (a `ValueError`-derived 400) — an absent CLI can never become the launch default. Two `github.*` keys are maps, not scalars: `repo_settings` and `issue_repo_settings`, keyed by `owner/name`, hold the PER-REPO overrides the Intake tab's repo cards write — `agent`, `base_branch` (PR review only; accepted but dropped for issues, whose work branches off the repo's own default), `min_age_minutes`, `skip_authors`. An absent repo key, or an absent field inside one, inherits the flat `github.*` value; a blank is dropped rather than stored, which is how a card field means "inherit the default" instead of "set it to empty" (`_repo_overrides` / `REPO_OVERRIDE_KEYS` in `backend/config/settings.py`) |
| GET | `/api/settings/auth-token` | The active web-auth token (for the QR / copy button) |
| POST | `/api/settings/test/shortcut` | Validate a Shortcut token (body `{api_token}` or the stored one) → `{ok, member_id, name, mention_name}` for auto-fill, or `{ok: false, error}` |
| POST | `/api/settings/test/github` | `{ok, token_source: "settings·env·gh-cli·none", gh_installed, gh_authenticated, detail}` |
| POST | `/api/settings/test/github-repo` | Body `{repo: "owner/name"}` — the per-repo twin of the row above: that one answers "is there a credential", this one answers "does it reach THIS repo", which is the failure people actually hit (a typo'd slug, a private repo the token has no scope for). One `GET /repos/{repo}` with the resolved token → `{ok: true, name, private, default_branch, can_push}` — `name` is GitHub's own `full_name`, `can_push` the token's push permission (reviewing pushes nothing, issue handling needs a branch, so read-only is worth saying out loud). Otherwise `{ok: false, error}`: a slug that isn't `owner/name`, no token available, an unreachable `api.github.com`, or GitHub's own `message` — a 404 reads as "no such repo, or this token cannot see it", because that is also what a private repo returns. **Always 200**, like the other probes, so branch on `ok`. Backs the **Test access** button on every repo card in Intake → Pull requests / Issues |
| POST | `/api/settings/test/agent` | Probe the configured agent CLI → `{ok, cli, auth}` (binary resolvable + login evidence) |
| POST | `/api/settings/test/local-model` | Probe a local model server (body `{runtime, base_url, model}`, each falling back to the stored value — so it can be tested *before* saving) → `{ok, runtime, base_url, models, error, supported_agents, default_base_urls}`. `models` turns the model field into a dropdown; `supported_agents` lists the installed CLIs that can actually be pointed at it (never `claude`) |
| GET | `/api/settings/providers/ticketing` | The ticketing-provider registry (fields per provider for the Intake → Tickets source cards) |
| POST | `/api/settings/test/ticketing` | Validate the active ticketing connection |
| POST | `/api/settings/ticketing/states` | Live workflow-state list for a ticketing source |
| GET/PUT | `/api/settings/ticketing/sources` | The multi-source ticketing config (per-source provider/repo/state) |
| GET | `/api/providers/manage` | Custom coding-CLI providers (user TOMLs) for the Settings CRUD screen |
| POST/PUT/DELETE | `/api/providers` · `/api/providers/{name}` | Create / update / delete a custom provider TOML. The body may carry `launch_args` (a list of saved flag tokens) alongside `resume_flag`/`skip_perms_flag`/`trust_patterns`/…; it is validated (400 on invalid) and all string values are TOML-escaped via `json.dumps`, so quotes in names/flags/patterns can't corrupt the file. |
| GET | `/api/providers/status` | Per-provider connection status → `{providers: [{name, aliases, binary, installed, path, authenticated, auth_detail, auth_known, login_command, install_hint, is_default}], default}`. The catch-all `generic` provider is omitted. This is the source for the Settings → **Agent CLI** default-provider picker — it reads `installed`/`path` to list only installed CLIs and self-correct a missing default. The `authenticated`/`auth_detail`/`auth_known`/`login_command` fields are still returned but **no longer read by the UI** (sign-in is delegated to each CLI; see [providers.md](providers.md)). |
| WS | `/api/providers/{name}/login-terminal` | **Deprecated / unused by the UI.** PTY↔websocket bridge to a throwaway tmux session running the provider's login flow in `$HOME`. Still served, but no frontend surfaces the one-click login any more (each CLI prompts for sign-in itself). Closes 4500 with an `{type:"error"}` frame for an unknown provider or a spawn failure. |
| POST | `/api/providers/{name}/login-close` | **Deprecated / unused by the UI.** Best-effort teardown of a login session. Always `200 {ok: true}`. |

**Doctor** (addon id `doctor`) — `GET /api/doctor` (listed above). Beyond
`checks` and `ok`, the payload carries two fields that ride along because this
is the endpoint every client already talks to:

```jsonc
{
  "checks": [...], "ok": true,
  "version": "0.1.0",            // the running ENGINE's version
  "state_notice": null           // or {file_version, supported_version, backup_path}
}
```

`version` lets the desktop shell detect app/engine drift — it pins the engine
to its own version at install time but only installs when the engine is
*absent*, so an app-only update would otherwise leave an old engine running
indefinitely. Serving it over HTTP keeps one code path across macOS, Linux and
Windows/WSL.

`state_notice` is non-null only after this build refused to read a `state.json`
written by a **newer** MindFlock: `LoadState` preserves that file as
`state.json.newer-<ts>` and starts with an empty session list, so the UI needs
to explain why every session disappeared and where the file went. `POST
/api/doctor/ack-state-notice` dismisses it (server-side, so it stays dismissed
across reloads).

**Connections** (addon id `connections`) — `GET /api/connections?refresh=1`:
one-call status of every external integration (GitHub, active ticketing
provider, agent CLI, tailscale) for the Settings → Connections screen.

**Traffic** (addon id `traffic`) — `GET /api/traffic?days=90&refresh=0`: the
product's *own* public reach, for the dev-only Settings → Site traffic screen
(see [web-ui.md](web-ui.md)). `days` is clamped to `1..90`; the whole payload is
cached **5 minutes** per `days` value (longer than Doctor's 30 s — this hits
GitHub's REST API and an external Worker, neither of which needs re-asking on
every panel open), and `refresh=1` bypasses that cache. Unlike every other
GitHub integration here it resolves no workspace remote: it always means
`MindFlock/MindFlock`.

```jsonc
{
  "generated": 1755100000.0,
  "repo": {"stars": 0, "forks": 0, "open_issues": 0, "url": "…"},  // or null
  "star_history": [{"day": "2026-08-10", "stars": 42}],            // cumulative
  "releases": [{"tag", "published_at", "prerelease", "assets": [{"name", "downloads"}], "total_downloads"}],
  "downloads_total": 0,
  "clicks": {
    "days": 90,
    "series": [{"day", "slug", "os", "clicks"}],
    "totals_by_slug": {"mac": 0},
    "visitors_by_day": [{"day", "visitors", "new_visitors", "returning_visitors", "unknown_visitors"}],
    "visitors_by_slug": [{"slug", "visitors", "new_visitors", "clicks"}],
    "totals": {"clicks", "visitors", "new_visitors"},              // or null
    "downloads": {"new_visitors", "new_visitors_clicked", "by_slug": [...]},  // or null
    "error": ""
  },
  "errors": {"github": null, "clicks": null}
}
```

Three contracts are worth stating outright, because a client that gets them
wrong produces plausible, wrong numbers rather than an obvious failure:

- **Per-upstream degradation.** Every call is best-effort and the endpoint
  always answers **200**. A GitHub rate limit sets `errors.github` and costs the
  `repo`/`releases`/`star_history` sections; an unreachable click Worker sets
  `errors.clicks` and costs only the `clicks` section. The two share nothing, so
  a click-tracking hiccup never hides stars the request already has.
- **Absent ≠ zero.** `clicks.totals` and `clicks.downloads` are `null` (never
  `0`) and `visitors_by_day`/`visitors_by_slug` are `[]` against a click Worker
  deployed before visitor attribution, or one answering with the wrong types.
  **`clicks.totals` is the capability marker** clients should branch on — the
  Worker emits the visitor sections together or not at all. The failure payload
  (`_empty_clicks`) carries every key the success payload does, so a client can
  index the sections unconditionally.
- **Unique counts are not additive.** Summing `visitors_by_day[].visitors` does
  **not** give `totals.visitors` — one person visiting on ten days is ten daily
  uniques and one window unique. Only the Worker holding the visitor ids can
  count a grain, so the server passes these sections through rather than
  deriving them, and so should any consumer. `new_visitors` is the one field
  that sums across days, since a first sighting happens on exactly one date.

**Cross-repo prerequisite.** The visitor sections come from the `webpage/worker`
Cloudflare Worker in the *marketing-site* repo (it derives a pseudonymous
per-click visitor id), and nothing in this repo can produce them. They stay
`null`/`[]` until that Worker is redeployed with a `VISITORS` KV namespace and a
`VISITOR_SALT` secret — see `worker/README.md` there. Counting starts at deploy
time, so visitors trail clicks until the window fills in. The Site traffic
screen says as much in place when `clicks.totals` is `null`.

**Templates** (addon id `templates`, prefix `/api/templates`) — saved
new-session templates (`~/.mindflock/session_templates.json`):

| Method | Path | Returns |
|---|---|---|
| GET | `/api/templates` | `{templates: [...]}` |
| POST | `/api/templates` | Save/overwrite a template |
| DELETE | `/api/templates/{name}` | Remove a template |

**Notify** (addon id `notify`, prefix `/api/notify`) — the reference addon for
the generic extension path (see [docs/extensions.md](extensions.md)):

| Method | Path | Returns |
|---|---|---|
| GET | `/api/notify/config` | `{rules: [{id, label, event, old, new, title, body, enabled}]}` — the event → notification rules, applied client-side by `static/addons/notify.js` and server-side by the ntfy channel |
| POST | `/api/notify/rules/{rule_id}` | Enable/disable one rule (for **both** channels) |
| GET | `/api/notify/ntfy` | The ntfy channel's state: `{enabled, server, server_default, topic, has_token, click_url, configured, active, public_server, subscribe_url, qr_svg, suggested_topic, last}`. Never the token — only `has_token`; `suggested_topic` is a fresh random name, `last` is `{ts, ok, error}` of the most recent push |
| POST | `/api/notify/ntfy` | Save `{enabled?, server?, topic?, token?, click_url?, clear_token?}` (only the keys present are touched); returns the `GET` view, plus a `note` when something was rewritten. `clear_token: true` removes the saved token — the escape hatch from "empty = keep", and it wins over a `token` in the same payload. `400 {error}` on an invalid topic/server URL |
| POST | `/api/notify/ntfy/test` | Send one test push — `{ok, error}`. Takes its config from the body when supplied, so a topic can be verified before it is saved; exempt from the rate cap |

The ntfy channel is a **server-side** delivery path for the same rules
(`web/core/ntfy.py`): the server publishes to the topic over ntfy's JSON publish
API (POST to the server root, topic in the body — session titles are arbitrary
UTF-8, which an `X-Title` header could not carry), so an alert arrives with no
browser tab open. It is off until configured, resolves through
env → `settings.json` → defaults (`MINDFLOCK_NTFY_ENABLED` / `_SERVER` /
`_TOPIC` / `_TOKEN` / `_CLICK_URL`, where an env-supplied topic is an implicit
opt-in for headless boxes), and is capped at 60 pushes/hour per process.

Both channels collapse repeats per **(session, rule id)** for 5 s
(`notify.py::_DEDUPE_SECONDS` / `notify.js::DEDUPE_MS`), not per (session,
event). Three rules ride `session.activity_changed`, and an event-keyed window
let whichever fired first swallow the rest — a default-on **ran out of usage**
push eating a default-on **needs your input** push, and, since the same key is
also the browser `Notification` tag, replacing its still-visible popup. The
window is a *flap* collapser and nothing more; it is meaningless at turn
cadence, which is why `session.turn_ended` is deduped by spending its work
evidence instead — once per observed work cycle — rather than by a timer.

Two write-path guards worth knowing: the token follows the store's secret
convention (empty or the `•••set` sentinel keeps the saved one) **and** is
dropped when the server URL is retargeted at a different host without a fresh
token, so server A's credential is never sent to server B; and a `token=` query
parameter in `click_url` is stripped, since that URL is stored on the ntfy
server.

Unlike the app's other secrets, this one can also be *removed*, with
`{"clear_token": true}`. The reason is specific to ntfy: the token is optional
(public topics need none), and a wrong token is strictly worse than no token —
ntfy answers a bad credential with `401 unauthorized` rather than ignoring it,
so a stray value breaks a publish that would have succeeded unauthenticated.
Without an explicit clear, "empty = keep" would make a mistyped token permanent
short of retargeting the server or hand-editing `settings.json`.

**Errors: branch on the body, not the status.** The two write endpoints report
failure differently on purpose. `POST /api/notify/ntfy` is a validating write, so
a bad topic or server URL is a `400 {error}` and nothing is saved.
`POST /api/notify/ntfy/test` is a *probe* — every outcome it produces itself is a
`200` with `{ok, error}`, including an invalid config and a send that failed
outright (DNS, TLS, timeout, a `403` from ntfy); only a malformed request body gets
a status error, from FastAPI's own validation. A client that branches on HTTP
status therefore reads every failed test as a success: branch on `ok` and display
`error`, which carries the ntfy server's own error sentence when it sent one.

**Rate cap** — pushes are capped at **60 per rolling hour, per server process**,
shared across every session and every rule (not per-session, not per-rule). It is
a runaway guard, not a tunable: no env var or setting changes it. `POST
/api/notify/ntfy/test` is **exempt**, so a test still reports a true verdict while
the event channel is being throttled.

A throttled push is **not** observable over the API. The cap is checked before the
HTTP attempt and returns `"Rate limit: too many ntfy pushes this hour"` to its
caller, but the event path is fire-and-forget and discards that return value, and
the drop is deliberately *not* recorded as a `last` result — so `last` keeps
showing the last real attempt rather than being buried under throttle noise. The
only trace is one server-log line per window (`ntfy: over 60 pushes/hour …`). If a
client needs to explain missing pushes, that log line is the evidence; `last: {ok:
true}` alongside silent phones is the symptom.

**Outbound reach** — `POST /api/notify/ntfy/test` publishes to the `server` in the
*request body*, so an authenticated caller can make the MindFlock process issue
one outbound `http(s)` POST (with a JSON body they largely control) to a host of
their choosing, and — when that host answers `4xx`/`5xx` — read back the first
~200 characters of its response body, surfaced as `error`. That is inherent to
"test before you save", and it is bounded (one call, 10 s total timeout, nothing
echoed back on a `2xx`), but a request-forgery probe is a fair way to describe it.
Hence two things worth not undoing: this endpoint takes the same auth middleware as
everything else, and the gate switches itself on for any non-local `CS_WEB_MODE`
(see [Authentication](#authentication)). An exposed server with `MINDFLOCK_AUTH=0`
would hand this probe to the network.

## Authentication

A single shared bearer token gates the whole server — HTTP routes and
websockets alike — via one ASGI middleware (`web/core/auth.py`).

- **On when exposed.** Enabled when `CS_WEB_MODE` is a non-local mode (e.g.
  tailscale — an explicit opt-in; `run.py` defaults to local), OR
  `MINDFLOCK_AUTH_TOKEN` is set, OR `MINDFLOCK_AUTH=1`. Off for a plain
  localhost run, a bare `uvicorn`, and the test suite (all leave `CS_WEB_MODE`
  local/unset). `MINDFLOCK_AUTH=0` forces off. A *persisted* token never flips
  the gate on by itself.
- **Token.** `MINDFLOCK_AUTH_TOKEN` env → `general.auth_token` setting →
  auto-generated + persisted on first exposed start. Printed in the startup
  banner and baked into the `/m?token=…` QR so a phone lands signed in.
- **Proving it.** An `mf_auth` cookie, `Authorization: Bearer <token>`, or
  `?token=` (which redirects to set the cookie and strip the token from the
  URL). A browser navigation without a token gets a tiny inline login page; an
  API call gets `401`; a websocket is closed with code **4401** (the SPA/mobile
  head reload to the login page on 401/4401).

Independent of the token gate — enforced even when it's off — the middleware
refuses browser cross-origin requests and DNS-rebinding hosts. These checks
run **before everything else**, public paths included: a cross-site
`POST /api/auth` is refused too.

- **Origin check (all modes).** A request carrying an `Origin` header whose
  host is neither loopback nor the request's own `Host` is refused (HTTP 403 /
  WS close **4403**). WebSocket handshakes ignore CORS, so this is what stops
  a malicious webpage from opening `ws://127.0.0.1:8765/...` and driving the
  agent terminals. Non-browser clients (curl, the CLI, other MindFlock
  servers) send no `Origin` and are unaffected.
- **Host check (local mode).** With `CS_WEB_MODE=local` only loopback `Host`
  headers are answered — a public domain rebound to 127.0.0.1 gets 403.

| Method | Path | Behavior |
|---|---|---|
| POST | `/api/auth` | Body `{token}` — validate + set the `mf_auth` cookie (login-page target; always allowed through the gate). `200 {ok}` or `401`; never echoes the token |
| GET | `/api/settings/auth-token` | This device's token in the clear (behind the gate) for Settings → Security |
| POST | `/api/settings/auth-token/rotate` | Mint + persist a NEW token (compromise recovery): every issued cookie/QR/paired device is invalidated; the response re-issues the caller's cookie. `409` when `MINDFLOCK_AUTH_TOKEN` pins the token; `500` when persisting the new token fails (the old token stays valid) |

## Server lifecycle

Startup (`lifespan`): a 4 s reload loop adopts sessions created by other
processes; the Cursor auto-adopt loop starts (disable initial state with
`CS_CURSOR_AUTOADOPT=0`); the prompt-queue drain loop starts (feeds queued
prompts to idle agents); the persisted scroll speed is applied; a banner with
the local + tailnet mobile URLs (the access token + a QR code if `segno` is
installed) is printed; each addon's `on_startup` runs. Shutdown reverses addon
hooks and cancels background tasks. tmux sessions are *not* touched — they
outlive the server.

## Launching the server

```bash
mindflock serve [local|tailscale] [--port N]   # default local (127.0.0.1)
./backend/web/run.sh [tailscale|local]   # same, from a source checkout; PORT=… to override
python -m backend.web.run [local|tailscale] [port]
```

The desktop app (see `electron/README.md`) auto-starts the server itself, so
manual launching is a headless/dev concern. `run.py` accepts mode/port CLI
tokens in either order and honors `CS_WEB_MODE`,
`PORT`/`UVICORN_PORT`. The default (local) mode binds `127.0.0.1` — nothing
off the machine can reach the server. Tailscale mode is an explicit opt-in
that binds all interfaces (`0.0.0.0`), so the port is reachable from your LAN
as well as your tailnet — every non-local bind is protected by the auth token
printed at startup (unauthenticated clients get 401). Nothing is exposed to
the public internet unless you forward the port yourself. For HTTPS run
`tailscale serve --bg 8765` once and use `./run.sh local`.
