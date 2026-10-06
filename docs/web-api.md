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
  "parent": "",               // lineage: the live session that spawned/adopted it ("" = a root)
  "spawned": false,           // true when an agent (not a human) created it
  "playbook": "",             // the playbook it was created with ("split"), "" = none
  "lane": {"target": "pr", "ask_first": false, "owner": "sc-19815"},  // ship lane, or null
  "run": {"id": "r_8f3k2m", "name": "Q4 payments", "task": "t3", "role": "task", "grouping": "each"},  // or null
  "created_at": 1759600000.0, // epoch the session record was created, or null
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
  "last_turn": "…",            // L3: ≤120-char snippet of the latest agent turn, or null
  "mcp_attached": true,        // this launch got the MindFlock MCP tools (null = unknown)
  "last_report": {"id": "m…", "status": "done", "summary": "…", "ts": 1759600300.0}  // or null
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
`activity` is layered. **Screen evidence comes first**: for a CLI with a
dialog parser (Claude Code, Codex) every probe captures the visible pane once,
and a dialog the provider parses there reads `clarify` whatever the hooks last
said — Claude's background sub-agents rewrite the hook marker (their tool
events say `working`, the main turn's Stop says `idle`) while a sub-agent's
permission prompt is still up. A stale `clarify` marker whose dialog is still
at the bottom of the screen also stays `clarify` (the pane layer below needs a
stable pane, and Claude blinks the pending tool's bullet while it waits). Then
the provider's own authoritative signal —
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

`parent` is the title of the session this one works for: the orchestrator
that spawned it, or one that later adopted it through
`POST /api/instances/{title}/parent`. It is `""` for a root, and also when the
stored parent is not a live local session. A stored link is only a claim, and
a reused title must never inherit someone else's children. `spawned` is
`true` when an agent created the session (`POST /api/instances` with
`spawned: true`, which the MindFlock MCP's `spawn_session` sends). It is set
once and never changes, and it is what allows an agent to delete the session.
`created_at` is when this session record was created (epoch seconds, `null`
when unknown). A title can be reused once its session is gone, so anything
keyed by title (a stored report from it, say) is compared against it.
`playbook` is the playbook the session was created with — `"split"` for the
New dialog's **Split across workers** (an orchestrator from its first prompt,
before its first worker exists; the UI's answer strip keys on it), `""` for
none. Set once at create time; persisted in `state.json` only when set.
Pending (not-yet-created intake) rows carry `parent: ""`, `spawned: false`,
`playbook: ""`, `created_at: null`. The cached fast path of this route recomputes `parent`
on every call, so a re-parent shows at once rather than on the next tick.
See [mcp.md](mcp.md#lineage-parents-workers-limits).

`lane` is the session's **fast-track** target (the UI never says "lane";
the API does) — how far MindFlock carries it once
its agent is done (`target`: `leave` · `commit` · `push` · `pr` · `merge`),
whether it asks first (`ask_first`), and `owner`, the window that drives the
branch: a copy window on the same `(repo, branch)` shows its owner's lane and
is never armed itself. `null` = Off (nothing armed). The autopilot
carries the lane out; see [the guided workflow](#guided-workflow-commit--push--pr--merge).
`run` names the team run the session belongs to (`role` `task` today; `lead`
and `piece` come with splits), or `null` — see
[Team runs](#team-runs). Pending rows carry both.

`mcp_attached` says whether **this launch** of the session's agent got the
MindFlock MCP tools: `true` when the launch carried the attach flags, `false`
when the CLI started without them (attach was off, the CLI can't be
attached, the provider ran the bare program, or a resumed session relaunched
a command configured before this server started), `null` when this server
process never saw it launch (an agent still running from before a restart).
Every launch site records it — the engine's first start and resume, the web
relaunch, the Assistant — in memory only; it is never written to
`state.json`. The UI uses `false` to tell you to restart the agent before a
playbook can name tools it doesn't have.

`last_report` is on a **worker's** row: the newest `kind: "result"` message
(its `report_result`) it sent to its **current** live parent, consumed or not
— `{"id", "status", "summary", "ts"}`, with `summary` the report's summary
line (its `Details:` block dropped) sanitized to one line of at most 140
characters and `status` the reported `done`/`blocked`/`failed`. `null` for a
root, before the first report, or when the only reports predate this
session's `created_at` (a namesake's). Read from the mailbox, cached per
(parent box version), never marking anything read. Pending rows carry
`mcp_attached: null`, `last_report: null`.

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
  "launch_args": ["--dangerously-skip-permissions"], // optional per-session flags
  "extra_launch_args": ["--model", "opus"], // optional: flags ADDED to the defaults
  "parent": "orch",             // optional: live session this one works for
  "spawned": true,              // optional: an agent created it (strict boolean)
  "base_ref": "3f2c9e1",        // optional: cut the worktree from this commit-ish
  "base_branch": "you/orch",    // optional, with base_ref: the recorded diff base
  "playbook": "split"           // optional: "Split across workers" (see below)
}
```

A full slash-path in the title (e.g. `feature/sc-1/foo`) is used verbatim as the
branch. `provisioned` + `repo_path` provisions **that local repo** (setup
commands auto-detected, no shared cache seeds); `provisioned` without
`repo_path` requires the configured `[repository].url`. Errors: 400 (empty
title, bad strategy/config), 409 (title exists, a spawn limit, or a branch or
workspace a closed session still holds; see below).

`launch_args` (optional) are extra CLI flags appended on **every** (re)start of
this session's agent, after the provider's own saved flags. They are validated
with the same rules as provider `[launch] args` (a list of non-empty tokens; no
newlines/NULs; ≤512 chars each) → **400** on invalid input. Omitting the key
means "not specified", so the session inherits the global default for its
provider (`coding_cli.default_launch_args`, see
[configuration.md](configuration.md)); an explicit list — **even `[]`** — is used
verbatim, so a default the caller toggled off is honored, not re-applied.

`extra_launch_args` (optional, ignored when `launch_args` is present) are
flags **added to** that global default instead of replacing it, validated the
same way. This is what the MindFlock MCP's `spawn_session` sends, so an
orchestrator adding `--model opus` never strips a worker's skip-permissions.
The two lists are merged by flag group (a flag with the values that follow
it): an identical group appears once, and the same flag with a different
value is kept after the default, so the CLI's last-flag-wins rule applies.

`profile_id` (optional) pins the auth profile the session's CLI runs under
(see [accounts.md](accounts.md)), with the same tri-state: absent/`""` =
inherit the app-wide default profile, `"default"` = explicitly the CLI's own
login, anything else must name a configured profile → **400** on an unknown
id.

`profile_model` (optional) overrides that profile's own model pin for this
session only — an OpenRouter model id for a gateway profile, the CLI's model
flag elsewhere. Blank keeps the profile's pin (which blank in turn means the
CLI's own default). Rejected with **400** when longer than 200 chars or
carrying a newline/NUL, since the value ends up in an env var and a launch
flag.

`plan_first` (optional, boolean) appends the plan-first instruction to
`prompt`: the agent lists every file it intends to create, modify or delete in
a `mindflock-plan` block and waits for the go-ahead (the Map's **Go** button).
Independently, a prompt always names the repo's red zones when it has any —
see [Code map & red zones](#code-map--red-zones).

`playbook` (optional, **legacy**) was the New dialog's **Split across
workers**; the dialog now starts a split as a [team run](#team-runs)
instead, and this field is kept for older clients. The only value is
`"split"`. The prompt is decorated through the playbook registry
(`backend/mcp/playbooks.py`): its first line becomes `Split across workers
(MindFlock): <task>` (the line the pane pins), followed by the split
instructions naming the agent's own MindFlock tools — commit shared
groundwork, one `spawn_session` per independent piece, `wait_for_session`,
review and merge each report, ask before deleting. Decoration runs before
the red-zone / plan-first notes and is idempotent (an already-decorated
prompt is left alone). The session is forced into a worktree of its own
(`in_place` is ignored), since its workers fork from its commits, and records
`playbook: "split"` on the session (the row's `playbook`). **400** for
any other value, an empty `prompt` (`… needs a task`), a non-git folder
(`… needs a git repo`), or a program whose CLI doesn't get the MindFlock tools
(`Split across workers needs the MindFlock tools: This CLI doesn't get the
MindFlock tools` — attach turned off, or a provider with no auto-attach).

**Lineage** (`parent`, `spawned`). These are what the MindFlock MCP's
`spawn_session` sends; see [mcp.md](mcp.md#lineage-parents-workers-limits).

- `parent` must name a live local session, else **400** `unknown parent
  session: <x>`. A parent that is over its budget answers **409** with
  `budget_locked: true`.
- `spawned` must be a real JSON boolean (`null` means false), else **400**
  `spawned must be a boolean`. It can't be set any other way, ever, so a
  string `"false"` must not read as true.
- **Spawn limits** are checked under the registry lock at the moment the title
  is claimed, so two concurrent creates can't both take the last slot. Each
  violation answers **409** with a message naming its env knob. With a
  `parent`: the parent may have at most `MINDFLOCK_MAX_CHILDREN` (default 8)
  live children, and the new session's depth (root = 0) may be at most
  `MINDFLOCK_MAX_SPAWN_DEPTH` (default 3). With `spawned: true`, with or
  without a parent: at most `MINDFLOCK_MAX_SPAWNED` (default 24) agent-spawned
  sessions live in total. The knobs are read on every request.
- `session.created` carries `parent` / `spawned: true` in its `data` when set.
- `/copy` never inherits either field.

**Fork point** (`base_ref`, `base_branch`). For plain worktree sessions only.
`base_ref` is a commit-ish resolved in `repo_path`; the new branch is cut from
it instead of the repo's HEAD. `repo_path` stays the canonical repo, so the
session's cleanup never depends on where the ref came from. `base_branch` is
recorded as the session's diff/stage base. Without it, the base is `base_ref`
itself when that is a local branch, else the repo's current branch.

| Request | Response |
|---|---|
| `base_ref` or `base_branch` with `provisioned` | **400** `base_ref is only supported for plain worktree sessions (not provisioned)` |
| `base_ref` or `base_branch` on an in-place session (including a non-git folder forced in-place) | **400** `… (not in-place)` |
| `base_branch` without `base_ref` | **400** `base_branch requires base_ref` |
| a ref that names no commit | **400** `unknown base_ref: <x> (no such commit in <repo>)` |
| an option-shaped or control-character ref | **400** `invalid base_ref: …` |

If the new branch name already exists in the repo (say, left over by a closed
or paused session with the same title), the create answers **409** `a branch
named X already exists (a closed or paused session may still hold it) — pick
another session title` before anything starts. Should the background start
refuse it anyway (a race), the existing branch is marked pre-existing and is
never deleted by the failed start's cleanup.

**Provisioned branch taken.** A provisioned create answers **409** when its
branch is still checked out in the base clone by a kept worktree (`a worktree
for branch X already exists at …`), or, with the `clone` strategy, when the
clone directory already exists (`a workspace for branch X already exists at
…`). Without this the start would fail later, or silently adopt the old
clone with its commits. Both messages say "already exists", the wording the
MCP's default-title retry keys on.

**How the prompt is delivered.** A plain session whose CLI takes no prompt
argument (aider, goose, opencode, cline, or a custom script resolving to the
generic provider) has no way to receive `prompt` at launch, so the prompt is
held in the session's prompt queue and typed in once the agent is idle, as for
a worktree with a setup pass. A bare shell as the program never gets a queued
prompt. A multi-line prompt is pasted as one bracketed paste, so it arrives
as one turn.

The 202 body is the usual session object, plus `prompt_delivery` (`seeded`:
the CLI gets it at launch; `queued`: held in the prompt queue; `none`: no
prompt), plus a `note` when the chosen account has no verified route for the
chosen agent — the session will run on the CLI's
own login, and the web UI warns about that at selection time while API and CLI
callers would otherwise never hear it.

### `GET /api/create_failures`

A create answers 202 before its background start runs, so a caller polling the
listing only sees a failed session's row vanish. This route says why:
`{"failures": {title: {"error", "ts"}}}` for every background start that
failed in the last 10 minutes (at most 64 kept), or only `?title=X`'s entry
(`{"failures": {}}` when there is none). `error` is the same text the
`session.create_failed` event carries. The MCP's `spawn_session` reads it to
explain a worker that vanished while loading.

### `POST /api/session-plan` → **200**

Fill in the New Session form from one sentence. **Creates nothing** — no
directory, no repo, no branch, no worktree, no session. It reads the filesystem,
asks one headless model turn, and answers with the fields the dialog already
owns; everything is still made by the user pressing **Create**, through the
unchanged `POST /api/instances` above.

```jsonc
{ "text": "work on the auth bug in acme-api, in a worktree" }
```

```jsonc
{
  "title": "auth-bug",                                          // string
  "repo_path": "/home/me/code/acme-api",       // string, always absolute
  "prompt": "Look at why token refresh fails after an idle hour and fix it.", // string
  "in_place": false,                                            // bool
  "init_repo": false,                                           // bool
  "folder_exists": true,                                        // bool
  "folder_display": "~/code/acme-api",         // string, ~-relative under $HOME
  "note": "Filled in from what you typed — check it and press Create. Using ~/code/acme-api. Work happens in a new worktree, not in the folder itself." // string
}
```

All eight keys are **always present**, and `repo_path` is always a non-empty
absolute path.

`folder_exists` is `false` only for a `new:<name>` answer that landed somewhere
nothing is yet — every numbered candidate came out of a walk of the real
filesystem. **A client must not create a session in a folder that does not exist
until the user has confirmed that folder in as many words.** Creating a directory
is the one thing a plan proposes that outlives the session and that closing it
never takes back: a worktree goes when the session does, but nobody comes back
for the folder. `folder_display` is the spelling to put in that question — `~`-relative under
`$HOME`, the plain path otherwise — identical to the one the `note` uses, so the
two never name the folder differently. It is never what a session is created
with (`repo_path` is), so no shortening here can change which directory is
opened. It deliberately differs from the spelling the *model* sees: the folder
menu in the prompt collapses anything outside `$HOME` to `…/<name>` so no
absolute path is ever in the model's context to copy, but a confirmation that
named no parent would be a question nobody can answer — and `resolve` reports
the realpath, so a symlinked `~/code` puts a brand-new project outside `$HOME`
routinely. **The model never names a folder.** The server walks the
filesystem first (the same recency ladder `GET /api/repos/suggest` uses, plus up
to three `GET /api/repos/search`-style name lookups drawn from the sentence),
renders that menu into the prompt as *home-relative* spellings only, and the
model answers with the **number** of a row — or the literal `new:<name>`, which
is sanitized to one path segment under a parent the server picks (first existing
of `~/code`, `~/projects`, `~/src`, `~/dev`, `~/work`, `~/Development`, else
`$HOME`). There is no branch of the resolver that turns model text into a
filesystem location, so the bare-name-becomes-`makedirs` hazard behind
`in_place` creation cannot be reached from here. An out-of-range number is an
**error, not a clamp** — clamping would hand back a folder the model never chose
under a note claiming it did.

`in_place` and `init_repo` mirror `POST /api/instances`' own clamps, so the form
can never show a mode the 202 would silently change: a non-git folder is always
`in_place: true`, and `init_repo` is set only for a project the sentence said was
new. `provisioned` is never emitted and never accepted. `note` is composed
server-side from resolved facts (which folder, which mode, why it was clamped,
and an "I wasn't certain" clause when the folder matched by substring rather than
by name) — the model never writes it, because a model-written note is a sentence
that can disagree with the form under it.

Errors carry one human sentence and leave the form untouched:

- **400** `{"error": "say what you want to work on"}` — blank body; and
  `{"error": "that reads like an answer format rather than a request — say what
  you want to work on"}` when the sentence is nothing but output-contract markup
  (those lines are dropped whole, never substring-stripped, so ordinary prose
  mentioning `<commit>` survives). The stripper is shared with
  `POST /api/tickets/compose` and knows **four** contract names, not three:
  `newsession`, `commit`, `testplan` and — since New → Ticket — `newticket`, all
  derived from one list (`session_plan._CONTRACT_NAMES`), so a forged
  `<newticket>` block is dropped from both boxes by the same code.
- **502** `{"error": "…"}` — no model to ask or an unreadable answer, e.g.
  `claude is not installed`, `codex did not answer within 75s`, `no installed CLI
  (aider) has a headless mode MindFlock can ask for a session plan`, `the CLI
  answered without a <newsession> block — nothing to read`, `the CLI didn't pick
  one of the folders — name the project you mean`, `the CLI picked folder 7, and
  there is no such folder in the list`, `the CLI echoed the example instead of
  reading what you typed`.

### Lifecycle

| Method | Path | Effect |
|---|---|---|
| DELETE | `/api/instances/{title}` | Kill session, remove worktree + branch |
| POST | `/api/instances/{title}/close` | End tmux, **keep** worktree; recorded in recently-closed |
| POST | `/api/instances/{title}/cleanup` | Kill + permanently delete the workspace dir (+ close its Cursor window) |
| POST | `/api/instances/{title}/copy` → 202 | New in-place session `<title>-copy` sharing the same worktree (inherits the source's agent **and** auth profile) |
| POST | `/api/instances/{title}/profile` | Hot-swap the session's auth profile. Body `{profile_id}` (`""` = inherit the global default, `"default"` = the CLI's own login) plus optional `profile_model` — **sending the key at all is what matters**: present sets this session's model override, absent keeps the current pin on a model-only no-op and *clears* it when the identity changes (a pin belongs to the catalog of the account it was picked from). Persists the pin and restarts the agent under the new identity; the worktree, shell pane and diff survive, and so does *that account's* conversation in this window — a thread belongs to the account that created it, so the marker is re-pointed at the incoming identity's own thread before the relaunch. → `{ok, profile_id, note, resumed}`, where `note` warns when the session's CLI has no route for the profile and `resumed` says whether the new identity had a conversation here to go back to (false = it starts fresh). Re-picking the identity and model already in force is a no-op that answers `{ok, unchanged: true}` **without** restarting the agent. 400 on an unknown id or a malformed model (nothing is mutated); 500 if the agent was killed and did not come back |
| POST | `/api/instances/{title}/parent` | Re-parent a session. Body `{"parent": "<title>"}` puts it under that live session; `{"parent": ""}` (or `null`) detaches it to a root. → **200** with the updated row. **404** for an unknown session. **400** for an unknown parent (`unknown parent session: X`), the session itself (`a session cannot be its own parent`), a parent that is the session's own descendant (`… that would make a cycle`), or a non-string `parent`. **409** when adopting would break a spawn limit: the new parent's live children (this one included) over `MINDFLOCK_MAX_CHILDREN`, or any session of the adopted subtree deeper than `MINDFLOCK_MAX_SPAWN_DEPTH` (the error names the knob). Detaching, and re-sending the current parent, are never limited. `spawned` is never touched. Persists |
| POST | `/api/instances/{title}/pause` | Pause (commit, detach, remove worktree, keep branch) |
| POST | `/api/instances/{title}/resume` | Resume a paused session |

Every way a session leaves the registry runs one teardown,
`_on_session_removed`. That covers `DELETE`, `/close`, `/cleanup`,
`/api/workspaces/delete`, a failed background start, and another MindFlock
process deleting it. The teardown does three things:

- it clears `parent` on the session's children, so they become roots;
- it drops the session's mailbox, so a reused title starts with an empty
  inbox;
- it deletes its MindFlock-MCP run file (`~/.mindflock/run/mcp-<tmux
  name>.json`).

Reopening a closed session restores its `parent` only when that parent is
still live.

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
| GET | `/api/instances/{title}/ship-status` | The commit/push facts a caller of the fire-and-forget `/commit` and `/push-branch` polls to learn whether its step finished (the MindFlock MCP's `ship_session` does) → `{title, now, branch, base, head_sha, upstream_sha, pushed, dirty, beyond_base, committing, commit_rc, commit_at, has_origin, failed_step, failed_hook, shell_tail?}`. `commit_rc` / `commit_at` are the exit status in `.mindflock_commit_status` and its mtime: the commit one-liner deletes the file first and writes it last, so a `commit_at` older than the moment you posted the commit is a previous attempt's. `committing` is the commit lock as the stage pill reads it. `upstream_sha` is the LOCAL `refs/remotes/origin/<branch>` (which a successful `git push -u` updates), so `pushed` (`head_sha == upstream_sha`) costs no network. `failed_step` / `failed_hook` only for a non-zero `commit_rc` with no commit running. `?tail=N` (≤ 200) adds the last `N` lines of the session's shell pane (≤ 6000 chars), where a failed hook or a refused push says why. 404 unknown title, 409 workspace not ready |
| POST | `/api/instances/{title}/push-branch` | `git push --no-verify -u origin HEAD` in the shell (hooks already ran on commit). **O3 soft gate:** when the repo's `.mindflock.toml` declares `check_command` and no check run passed against the current HEAD, returns `409 {error, check_required: true, check}`; re-POST with body `{"force": true}` to push anyway. **Red-zone gate** (checked first): a file inside an enforced red zone — or, while a green scope exists, outside it (not a companion, not an exemption still at its recorded blob) — committed between the fork point and `HEAD` returns `409 {error: "red zone breached: <path> (<pattern>)" | "outside green zone: <path>", red_zone_breaches: [{path, pattern, zone_id, kind}]}`; re-POST with `{"override_red_zones": true}` to push anyway (carry `force` too if the check gate also applies). See [Code map & red zones](#code-map--red-zones). |
| GET | `/api/instances/{title}/branches` | `{branches, current, default}` — the branch list backing the **Make PR** dialog's base picker. `branches` are `origin`'s remote heads (falling back to local heads when origin is unreachable, so it's never blank); `current` is the session's own branch (never a valid PR target); `default` is the pre-selected base (`repository.pr_base_branch` → the session's fork base). 404 unknown title, 409 workspace not ready |
| POST | `/api/instances/{title}/make-pr` | Opens a PR → `{ok: true, url}` (or `note: "PR already open"`). Three tiers, in order: `gh pr create --base <base> --fill` when `gh` is installed **and** authenticated; else the GitHub REST API with a token from the usual resolution chain; else **`200 {ok: false, compare_url}`** — a prefilled compare URL the UI opens in the browser, plus the remedy sentence "add a GitHub token in Intake → Pull requests, or install the GitHub CLI". A missing `gh` is never an error status. The UI's Make-PR dialog collects `<base>` from the branch picker above (and the frontend remembers the last base per repo — `prBaseByRepo` in `localStorage`); an omitted base falls back to the session's base branch. Same **red-zone gate** as push-branch, over `<base>...origin/<branch>` when that ref exists (the pushed branch is what the PR carries, diffed the way the forge diffs it — so upstream merges never read as breaches; `fork..origin/<branch>` when no base ref resolves), else `fork..HEAD`; `override_red_zones: true` in the body overrides. Optional `title` / `body` (strings, capped at 256 / 60000 chars) replace the commit-derived title / body on both the gh rung (then `--title … --body …`, the other half filled from the commits as `--fill` would) and the REST rung; 400 when either is not a string. The MindFlock MCP's `ship_session` sends a worker's `report_result` summary as the body |
| POST | `/api/instances/{title}/merge-pr` | Merges the branch's PR, same three tiers: `gh pr merge <branch> --merge`; else the REST API with a token; else **`200 {ok: false, pr_url}`** so the UI can send you to the PR page to merge it yourself. Optional body `{override_red_zones}` — the same red-zone gate as make-pr (409 + `red_zone_breaches`). The fast-track autopilot never sends the override, so a chain halts on a breach with the error as its reason |
| POST · DELETE | `/api/instances/{title}/fast-track` | ⏩ **arm / disarm the autopilot** for this session: carry it to `depth` (`commit`, `push`, `pr`, `merge`; default from Settings → Workspace) and stop. Arm-and-*wait* — pressing it while the agent is still working records the target and lets the driver act once the agent is verifiably done, rather than committing a half-written tree. Body `{depth?, message?, base?}`. **An omitted `message` inherits the previously armed one along with its `message_auto` placeholder flag** — an intake-armed run carries the ticket / PR / issue name, and re-arming used to overwrite that with a generated *"Work on `<slug>`"*; carrying the flag across is what stops a re-arm from freezing a placeholder into the commit. With no message anywhere, a *pending* on-disk one from a blocked commit is adopted (the rule `GET /commit-message` applies), else a default subject is generated from the tree and marked a placeholder. Every placeholder is replaced at commit time by a message written from the final diff (see [web-ui.md](web-ui.md#intake)); a message a human typed never is. → `{ok: true, autopilot}`, the authoritative record, so the client settles its toggle without a follow-up read (arming touches a JSON file and does no PR lookup — the press must not wait on the network). 400 unknown depth or the `agent` rung (that one is for intake, where the session does not exist yet), 404 unknown title, 409 workspace not ready. `by` (`"agent:<title>"`, sent by the MCP's `set_autopilot`) records who chose the lane; anything else is the user. **The alias never lowers a guard**: an "ask first" someone chose stays on (unticking it is a `/lane` call). The same refusals as `/lane` (copy window, a group's lead or one-for-all member — 409; a paused group's member — 409 `held`). DELETE stops the driver taking the *next* step — anything already typed into the shell keeps running → `{ok, stopped}` |
| POST | `/api/instances/{title}/lane` | **Set the ship lane**: body `{lane: "leave"\|"commit"\|"push"\|"pr"\|"merge", ask_first?: bool, message?, base?}` → `{ok: true, lane, autopilot}`. The lane is carried out by the autopilot — this is `/fast-track` with the lane vocabulary (`/fast-track` stays as its compatible alias; both go through `core/lanes.py`). `leave` disarms and answers `lane: null`. `ask_first` holds the run one rung short of its first outward step — a commit lane stops when the agent is done, a push / PR / merge lane once it has committed — and the session then waits in the bell for your go (`ship-now`). 400 unknown lane or a non-boolean `ask_first`, 404 unknown title, **409 workspace not ready** while the session is still loading (retry). **409** `{error, shared_with}` on an in-place session whose folder another live session shares; **409** `{error, driver}` on a **copy window** whose branch another window drives (a record or a group; `leave` is always allowed — it only disarms), and on a group's **lead** (it ships once, through the release) or a **one-for-all member** (its work is the group's one PR). A member of an each-their-own group: the lane and `ask_first` are recorded on its task (every re-arm keeps them); while the group is **paused** nothing is armed — `{ok: true, held: true, lane, autopilot}`, armed on resume. Rows carry `lane: {target, ask_first, owner, by}` (`by`: `user` or `agent:<title>`) |
| POST | `/api/instances/{title}/ship-now` | **Ship what is there now** — the bell's Commit / Open the PR on an ask-first lane, and "take what's there now": arms the lane (`{lane}` — the UI sends the lane the row showed — else the session's own record) without "ask first" and **without the 30 s idle dwell**, under this server's lease so the driver acts on its next pass. **Never the Settings fast-track rung**: a session with no lane of its own is refused (409 `this session has no lane of its own`). `{commit_message}` (the approval card's edited message) is committed as written. → `{ok: true, lane, autopilot}`. **409** `{error, activity}` while the agent is working (`the agent is mid-turn — stop it first`), on a dialog, or at its usage limit; **409** on a copy window (`{driver}`), a group's lead, a one-for-all member, or a paused group's member; 400 when the lane ships nothing (`leave`); 404 unknown title |
| POST | `/api/instances/{title}/reset-stage` | ↺ **back to idle** — pins this window's guided ladder back to its start on a clean branch, so the header stops insisting on Push / Make PR / Merge while you keep working on the same branch. **Nothing git-facing happens**: no reset, no revert, no PR close, and the published `stage` is untouched — the pin (`backend/web/core/stage_reset.py`) rides the row as `stage_reset` and only the UI ladder reads it. It releases itself when the worktree moves (dirty tree or new commit), never on the stage label. Also takes down the finished cycle's leftovers: a **halted** fast-track record (a live chain is left strictly alone) and a **stale** verification result (a current failure is never touched — the push gate reads it). → `{ok: true, pinned, dirty, cleared[], row}`, where `row` is the recomputed instance row so the presser's window flips now rather than on the next tick, and `pinned` is `false` on an already-dirty tree (that ladder is at its start already). 404 unknown title, 409 workspace not ready |

### Assigned tickets, PR auto-review + issue handling

| Method | Path | Behavior |
|---|---|---|
| GET | `/api/tickets` | Assigned tickets on the configured ticketing sources, each annotated with auto-ingest eligibility → `{tickets, sources, source_labels, buckets, done_buckets, ingest_states, errors[], stale}` (per ticket: `source`, `source_label`, `bucket`, `eligible`, `reasons`, `has_session`). The list is grouped by **source** and then by workflow-state bucket, so `source_labels` maps the key of EVERY configured source → its display label — including sources that returned nothing and ones that landed in `errors[]`, which deriving labels from the ticket rows alone would make vanish (`sources` is the subset that answered). The slowest of the three panel fan-outs (~3 s: one provider search per source + a `git ls-remote` per repo); per-source failures come back in `errors[]` rather than failing the call. Powers Intake → **Tickets** → **Assigned tickets** |
| POST | `/api/tickets/start` | Body `{source, id, agent?}` — force-start a coding session for one ticket, bypassing the auto-ingest filters. `agent` is the coding CLI for **this one launch** (the picker beside **Begin work**) and outranks the source's own; omit it — or send `""` — to use the configured chain (the source's Agent CLI, then `[mindflock].agent`, then the app default). 400 missing `source`/`id` or an `agent` no provider answers to, 404 ticket gone, 409 a session for it already exists. **Agent-spawned starts** (the MindFlock MCP's `spawn_ticket_session`) add `{parent?, spawned?, report_back?, note?, depth?}`: `parent` must be a live session (400) that is not over budget (409 `budget_locked`); `spawned` and `report_back` are strict booleans (400); `note` (≤ 4000 chars) is appended to the ticket prompt under *Note from session `<parent>`*; `report_back` appends the MCP's report-back footer when there is a parent and the ticket's CLI gets the MindFlock tools. The spawn limits (`MINDFLOCK_MAX_CHILDREN`, `MINDFLOCK_MAX_SPAWN_DEPTH`, `MINDFLOCK_MAX_SPAWNED`) are checked before the 202 (409 naming the knob) and again under the registry lock when the launch claims the title; a refusal there (or any other launch failure) is readable from `GET /api/create_failures`. The pending row and the launched instance carry `parent` / `spawned`. Such a start answers `202 {started, title, branch, program, parent, spawned, report_back, reason?}` (`reason` says why a requested footer was not added); a start without these fields answers the plain `{started, title}` |
| POST | `/api/tickets/merge` | Body `{source, from, into}` — fold the duplicate ticket `from` into `into` on the same source and **delete `from` in the tracker** (Intake → Tickets → **Merge into…**). Everything `from` has — description, acceptance criteria, comments and attached files — is appended to `into`'s description, an audit comment on `into` records where it came from, and only then is `from` deleted. The order is the contract, not an implementation detail: a failure at the append leaves BOTH tickets untouched, and a failure at the delete leaves a duplicated ticket rather than an erased one — so `deleted` and `delete_error` come back in a **200** body instead of as a 5xx, because by then almost everything did happen and a 5xx would say nothing had. → `{from, into, comments_copied, attachments_moved[], attachments_failed[], attachments_linked[], comment_error, deleted, delete_error}`; `attachments_linked` is the honest answer for a provider whose uploads outlive the ticket (GitHub, Linear) — nothing moved because nothing had to. Same source only: files cannot follow a ticket into another tracker and the survivor keeps its own queue's repo and agent. Needs an adapter that implements the provider writes (`TicketProvider.can_merge` — Shortcut, Jira, Linear, GitHub Issues; Asana implements no *merge* writes, so its rows never offer the control — it can still file a new ticket via `/api/tickets/compose` — which `GET /api/tickets` reports per row as `merge_ready`). Deleting a GitHub issue is the one that can refuse on permissions: it needs the GraphQL `deleteIssue` mutation and repo admin rights. 400 missing ids, the same ticket twice, or a provider that cannot merge; 404 no such source; 502 the tracker refused the FIRST write, with nothing changed |
| GET | `/api/tickets/sources` | `{sources[], ingest_on}` — every configured ticketing source with whether a ticket can be **filed into** it (`{key, label, provider, can_create, blocker}`). Powers the source picker in **New → Ticket**. Sources that cannot accept one are listed too, with `blocker` naming the field to set (a Jira source with no project key, a Linear source that names no team in a multi-team workspace), because "Jira isn't in the list" and "this Jira source needs a project" are different problems and only one is the user's to fix. Contacts no provider — `TicketProvider.create_blocker` is contracted offline — so opening the dialog costs no API calls. `ingest_on` is whether ticket ingestion is running at all, i.e. whether filing will also eventually start a session |
| POST | `/api/tickets/compose` | Body `{source, text}` — draft a ticket from one sentence and **file it on the tracker**, returning the link (**New → Ticket**). → `{source, source_label, provider, id, slug, name, url, description, criteria[]}`. The model writes a title, a description and acceptance criteria; the description is normalized to paragraphs + one `## Acceptance Criteria` heading + `-` bullets, which is the grammar `parse_acceptance_criteria` mines back out on ingestion and the only grammar Jira's ADF translator understands. The ticket is filed into the state the source already ingests from and assigned to its configured member, so it lands where the board (and the poller) is looking. **There is deliberately no route that takes ticket fields**: the tracker already has a form, and describing the work is the only thing MindFlock can do here that it cannot. Needs an adapter with `TicketProvider.can_create` (all five: GitHub Issues, Shortcut, Jira, Linear, Asana). 400 no source, an empty sentence, or one that is only an answer-format instruction; 404 no such source; 502 the source is blocked, the CLI could not draft, or the tracker refused — a 502 raised **after** the model turn carries `draft` (`{name, description, criteria}`) so a retry costs a button press rather than another ~25 s |
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

Two **POST** routes drop the assigned-tickets cache rather than refreshing it:
`/api/tickets/merge` and `/api/tickets/compose`. Both change what the Tickets
panel should show — one ticket gone, one ticket new — and re-sweeping inline
would put a provider search per source on a request that has already done its
work. Dropping the entry means the panel's next poll pays for one real sweep. So
a just-filed ticket appears in Intake because the cache was invalidated, not
because the panel went back to the tracker on its own.

### `POST /api/tickets/compose`

The ticket twin of `POST /api/session-plan`, and the opposite in one important
respect: this one **creates something**, on a server MindFlock does not own.
One sentence in, a filed ticket and a link out.

```jsonc
{ "source": "shortcut", "text": "the login page hangs for SSO users on slow connections" }
```

```jsonc
{
  "source": "shortcut",            // the pair every other ticket route is keyed on…
  "id": "4821",                    // …so the row can go straight to /api/tickets/start
  "source_label": "Shortcut",
  "provider": "shortcut",
  "slug": "sc-4821",
  "name": "SSO login hangs on slow connections",
  "url": "https://app.shortcut.com/acme/story/4821",   // the deliverable
  "description": "…\n\n## Acceptance Criteria\n- …",   // what was filed
  "criteria": ["…"]
}
```

**There is deliberately no route that takes ticket FIELDS.** The tracker already
has a form for filing a ticket by hand; a worse copy of it inside a session
dialog would earn nothing, and describing the work is the only thing MindFlock
can do here that the tracker cannot.

The shape of the call:

- **The draft is one headless model turn**, budgeted at **75 s**
  (`ticket_draft.TIMEOUT_DRAFT`) — the same budget the Describe box uses, for
  the same reason. It runs on `asyncio.to_thread`, off the loop serving the
  grid's websockets, and typically takes ~10–25 s.
- **`program` is not a parameter.** The server reads the flock's own default CLI
  (`ENGINE.default_program()`) per request and passes it in `pick_argv`'s FIRST
  slot — passing `""` there resolves to `claude` unconditionally, which is how a
  codex-only machine ends up being told a CLI it never chose is not installed.
  Same reasoning as `/api/session-plan`.
- **`text` is stripped of output-contract lines** before it is sent, by the same
  stripper `/api/session-plan` uses.
- **The blocker is checked before the model runs**, so a source that was never
  going to accept a ticket costs a round trip rather than a draft.
- It **invalidates the assigned-tickets cache**, so Intake → Tickets shows the
  new row on its next poll (see the caching contract above).

Errors carry one human sentence:

- **400** `{"error": "pick a source to file the ticket on"}` — no `source`;
  `{"error": "say what the ticket is for"}` — blank `text`; and `{"error": "that
  reads like an answer format rather than a request — say what the ticket is
  for"}` when the sentence is nothing but output-contract markup.
- **404** `{"error": "No ticketing source 'x' is configured — check Intake →
  Tickets"}`.
- **502** `{"error": "…"}` — the source is blocked (`create_blocker`), no CLI
  could be asked, the draft was unreadable, or the tracker refused. **A 502
  raised after the model turn carries the draft**, which is the one shape in
  this file that is not a bare `{error}`:

  ```jsonc
  { "error": "Jira could not create an issue in ENG (HTTP 401): …",
    "draft": { "name": "…", "description": "…", "criteria": ["…"] } }
  ```

  It is returned so a retry costs a button press rather than another ~25 s, and
  because at that moment the drafted text is the only copy of itself anywhere.

  One 502 is worth reading closely: *"The ticket was filed on X but the tracker
  did not return a link to it — check the board before filing it again."* That
  one means the create **succeeded** and only the link is missing, so **a retry
  will file a second ticket.** It is reported as a failure anyway, because a
  ticket the user cannot open is one they cannot act on — but the sentence says
  what happened rather than dressing it up.


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

Four of the five carry **notification rules** (`verify_plan_ready`,
`verify_plan_failed`, `verify_run_finished`, `verify_run_gave_up` — all
opt-in), reaching the browser and ntfy on exactly these meanings; `due` has
none, since it is the state the dialog already shows. A push body fills
`{session}` only, so it cannot carry `checked`'s `failed` / `needs_you` counts —
see [web-ui.md](web-ui.md#notifications-).

A plan's **id is its session title**, so it can contain slashes — every route
below uses a `{plan_id:path}` converter, and clients must percent-encode it.

**One checklist per (repo, branch).** The id is the title of the session that
first pushed, but the *dedupe* key is the normalized repository path plus the
branch: a second window on the same branch of the same repo (a duplicated
window, a repo adopted twice) writes nothing, and its later pushes refresh the
**first** window's plan — `tip_sha` and `refreshes` move on the owner, so the
shared checklist follows the branch rather than freezing at whichever commit the
owning window pushed last. The owner is the first-written plan; delete it and
the next push or button press from the other window writes its own. Two
exclusions: the same branch name in a *different* repo is a separate plan, and a
plan whose `repo_root` is blank covers nothing. Whether two windows dedupe rests
entirely on their `repo_root` normalizing to the same path (worktree vs main
checkout, trailing slash, symlinks) — a mismatch degrades to two plans, not to an
error. One asymmetry to know about: the Verify dialog decides what to *offer*
by the repo's **basename** (all a live session carries), while the server keys
on the full normalized path — so two different repos with the same directory
name on the same branch are hidden from the Write-a-checklist bar even though
the API would accept the second one.

| Method | Path | Behavior |
|---|---|---|
| GET | `/api/test-plans` | `{plans[], live_branch}` — every checklist, newest first. `live_branch` is the **flock-wide** default resolved fresh per request; each plan additionally carries `effective_live_branch`, the same question asked for *that plan's repo*. Compare a plan against its own, never against the top-level one, or every plan in a repo with an override reads as out of date. Per-plan: `state`, `steps[]` (`id`, `text`, `expect`, `actor`, `manual?`), `runs[]` (capped, newest kept), `run_session`, `branch`, `sha`, `tip_sha`, `refreshes`, `merged_at`, `live_at`, `merged_into`, `merged_into_at`, `merged_into_all`, `gen_started`, `gen_attempts`, `error`, `summary`, `intent`, `focus`, `notified_at`, `live_problem`. **`merged_into`** is the branch on `origin` the work has most recently reached (`""` while it is still only on the branch it was pushed to) — *not* `live_branch`, which is the branch the checklist is waiting for: in a repo that ships through a `staging` step the two disagree for most of a change's life, and the disagreement is the interesting part. `merged_into_at` is when it got there (`0` when the rung that answered could not say, i.e. the squash-merge case). `merged_into` keeps being refreshed — about every 5 minutes per plan — until a week after the work last landed anywhere, so it can climb *past* `effective_live_branch` (`main` when the live branch is `staging` and the work was promoted); `merged_into == effective_live_branch` is not a terminal state, and nothing in the server acts on it — it is display only. The week is measured from `merged_into_at`, falling back to `merged_at`, then `live_at`, then `generated_at` when it is `0`, so an undated squash-merge landing still stops being refreshed. `merged_into_all` is the trail, best first and one name per landing, so it is not "every branch that contains the commit" — every branch cut from `main` after a merge does. **`live_problem`** is why a checklist is not coming due when the answer is not "not yet" — origin has no such branch, or the branch's PR merged into a different one. Distinct from `error` (which means an operation you asked for failed); it clears itself. **`summary`** is the model's own one-sentence statement of what the change lets somebody do — what `title` (the session's name) could never be. **`intent`** is what the work was *asked* to do, snapshotted at push time from the ticket or the seed prompt; it is stored on the plan rather than read off the session, because plans outlive sessions and a rewrite that read it live ran with no ticket at all. **`focus`** is what you told the last rewrite it got wrong. The snapshotted session transcript is **not** on the wire: it is generation input, never UI. **`sha` vs `tip_sha`**: `sha` is the liveness anchor — written once and never moved, because ancestry is transitive and moving it forward could only make a checklist come due later or never. `tip_sha` is the newest commit seen pushed on the branch; it is what the diff is read at and what a pre-live run checks out, and it is recorded on every push whether or not a rewrite follows |
| POST | `/api/instances/{title}/test-plan` | Write a checklist for one session **by hand**, with no repo opted in → **202** `{plan, existing}`. Works for a **closed** session too, falling back to the recently-closed store for its branch and repo (409 when that branch is gone from the repo, 404 when neither store knows the name) — a checklist outlives its session everywhere else here, and creation demanding a live window made the button useless at the moment people ask for one. A closed session carries no seed prompt, so such a plan has no `intent` and is written from the diff alone. A headless one-shot reads the branch's diff and answers in up to three minutes; `existing: true` points at the plan that is already there rather than erroring (a **200**, not an error — the honest answer to "write one for this" when one exists is to point at it). `plan` then names the checklist that already **covers the branch**, which may be **another session's title** when the branch is open in more than one window (see "One checklist per (repo, branch)" above), and `state` is read from that owning plan — the same holds for the closed-session fallback. 404 unknown session, 409 no workspace yet / nothing committed on this branch / a detached HEAD |
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

### Code map & red zones

A **red zone** is a path glob the agent may read but must never create, modify,
delete or move — per repo (the default: every worktree and clone of the same
origin) or per worktree. The Map pane tab (see [web-ui.md](web-ui.md)) is the
**Code Tree**: the worktree as a tree (folders as branches, files as leaves,
tests as roots) laid out in the browser from `GET …/code-map` (files + import
graph), with the agents as birds from `GET …/code-map/live` (this session's
tool trail and plan, other sessions' edits on the repo), the blast radius of
what they edited, zones, breaches and off-plan edits; a leaf's card reads
`GET …/code-map/file`, the search box `GET …/code-map/search`. The `atlas` and
`entry-points` routes below are kept for API clients; the bundled UI no longer
calls them.

A **green zone** is the inverse ("only here"): while a worktree has one, the
agent may modify ONLY files inside its green zones — see [Green zones](#green-zones-only-here)
below. Routes and modules keep the `red-zones` / `red_zones` names; zones carry
`kind: "red"|"green"`.

**Storage.** Zones live in `~/.mindflock/red_zones.json`
(`MINDFLOCK_RED_ZONES_FILE`; `{version, repos: {repo_id: {label, zones,
plan_first, companions}}, worktrees: {realpath: {repo_id, zones, waive, green,
green_exempt}}}`, atomic writes). `zones` are RED everywhere; green zones live
under their own `green` key (worktree scope only) so an older server or an
older baked-in hook — which reads `zones`/`rules` as red — never sees one.
A repo id is the normalized `origin` (`github.com/owner/repo` — SSH and HTTPS
spellings agree; a local-path origin is followed up to 3 hops), else
`path:<repo root>`. Enforcement reads a **guard file** per worktree root
(`~/.mindflock-assistant/.red-zones/<sha1(realpath)[:20]>.json`,
`MINDFLOCK_RED_ZONE_DIR`) at hook fire time, so a zone added mid-flight applies
to the agent's very next tool call; the per-session **tool feed** the hook
appends to lives in `~/.mindflock-assistant/.tool-feed/<tmux>.jsonl`
(`MINDFLOCK_TOOL_FEED_DIR`). See [providers.md](providers.md) for the hook
itself and what "detect-only" means.

**Reconcile loop** (`backend/web/core/red_zone_monitor.py`, every 4s on a
worker thread): re-syncs each live worktree's guard file (v2: red `rules` /
`files` / `dirs` / `sym`, green `green_rules` + `companions`, `protect` whenever
either kind is present) (a rewritten or
deleted guard → `session.red_zone_tampered {what: "guard"}`), re-arms a hot-
reload provider's hook config when it lost the tool hook, gained
`disableAllHooks`, or still carries a tool hook baked by another build (the
hook tag ends in a hash of its embedded source — a stale one is reinstalled,
silently on first sight) (only a transition away from armed is announced:
`{what: "hooks"}`; a hooks file that exists but isn't valid JSON is never
rewritten — the guard reads `off` with the reason instead, and re-arms once it
parses), turns feed denies into `session.red_zone_blocked` (once per session
and zone per work cycle — the cycle resets when the session goes idle; a
refused push/PR carries `push: true` and is its own key) and computes
**breaches per worktree**: the session's change set ∩ enforced zones
(recomputed only when the worktree fingerprint moves, at most every 10s) and
zone-matched git-ignored files whose content left their baseline (taken **as
the file is when first listed** — a `__pycache__` that appears is not a
breach). The Bash stat-diff backstop's feed records only trigger that check:
a backstop path git and the content baseline don't confirm is dropped (the
one path it can add is a git-ignored zoned file an agent command created
since the last complete listing, and a `.claude/worktrees/<n>/…` sandbox
path its own checkout's git confirms) → `session.red_zone_breached` once per
(worktree, path), `detail` ending with what it blocks (only a committed
change blocks a push). Everything is **seeded silently** on first sight — a
restart announces nothing — and a zone-set change silently absorbs only the
paths no previously-enforced zone covers (a zone created over files already
changed is not news; a pending breach of an existing zone still is, and an
ignored file's baseline survives the change) — precisely: it compares the
breach set under the zone document before the change with the one after,
never "was an old rule matching it" (backwards for green, where an old green
rule ALLOWED the path). Paths are matched
case-insensitively where the worktree's filesystem is. A zone-store
change no route of ours made → `{what: "store"}` (reported, never reverted).
Feeds no registered session owns are size-trimmed and deleted after a day
quiet. Deleting a session (`DELETE /api/instances/{title}`) removes its guard
before Kill and tombstones the title so the loop can't re-adopt it mid-Kill;
its "This worktree" zones go only if Kill actually removed the folder (an
in-place session's checkout keeps them).
The row field `redzone` (`GET /api/instances`) is its summary: `{zones,
breaches, last_block_ts, guard, mode}` (`mode`: `"green"` while a green scope
is in force, `"red"` with red zones only, `null`) with `guard` one of `guarded` (armed; a feed
record seen since arming, or no tool call yet), `arming` (a hook fired since
arming but no feed record yet), `detect` (the provider has no hard guard),
`off` (not armed and re-arming failed, or the hooks file isn't valid JSON),
`none` (no zones) — or `null` when there is nothing to say. Committed
breaches also go into the guard's `breaches`, which makes the hook refuse
`git push` / `gh pr create|merge` / GitHub MCP writes; the push/make-pr/merge
routes compute their own at request time (see the guided-workflow table) —
a push over `fork..HEAD`, a PR/merge over `<base>...origin/<branch>` (what
the forge diffs, so upstream changes merged locally never read as this
branch's breach).

#### Green zones ("only here")

**One predicate.** `red_zones.classify(rel_real, rel_lex, zones_doc, ci)` →
`blocked | outside | companion | ok` decides every path for every consumer —
the hook (a mirror in `_tool_hook_src._mf_classify`), the monitor, the push/PR
gate, `/live`, the preview, plan flags and the Map (`classifyPath` in the
frontend). `tests/fixtures/zone_classify_cases.json` pins all three to the same
answers. Order: a red match on ANY representation of the path (and its
`.claude/worktrees/<n>/`-stripped form) → **blocked** (red always wins); no
green zone → **ok**; else EVERY in-root representation must be writable —
judged on the realpath too, so a symlink inside the scope pointing outside is
outside: a green match, a MindFlock workspace artifact (`.mindflock_*`) or the
nested sandbox dir itself → **ok**; a **companion** → writable, flagged amber,
never a breach; else **outside**. Paths outside the worktree root (`/tmp`,
another checkout such as the main clone of a worktree session) are not
governed — a green zone bounds the worktree, nothing else.

**Companions** — files an agent legitimately writes outside its scope as a
side effect of in-scope work: lockfiles (`uv.lock`, `poetry.lock`,
`package-lock.json`, `yarn.lock`, `pnpm-lock.yaml`, `Cargo.lock`, `go.sum`,
`Gemfile.lock`), `__snapshots__/`, `*.snap`, **test files that import a file
inside the scope** (from the Code Map import graph; "test" is the Atlas's own
predicate, `code_map.effective_tests` — a test-NAMED file that non-test code
imports, e.g. `test_plans.py` imported by `server.py`, is code and never a
companion), and the repo's configured
"derived outputs" (`GET/PUT /api/red-zones/companions`, e.g. a built bundle).

**Worktree scope only.** A green zone describes a task, not repo policy: a
repo-wide one would leak into Verify/intake sessions of the same repo and deny
their own bookkeeping. `scope: "repo"` → 400. It applies to every session on
that worktree (`sessions_here` in `GET …/red-zones`).

**Hook.** Denies `Edit`/`Write`/`NotebookEdit`/patches, MCP writes (for green
only the explicit verbs `write|create|update|edit|delete|move` — an MCP
"upload" reads a local file) and high-confidence Bash write targets (redirects,
`tee`, `rm`/`mv`/`touch`/`chmod`, `cp`/`ln`/`rsync` destinations, `sed`/`perl
-i`, `git rm|mv|checkout|restore`) that are blocked or outside; `mkdir`,
`git clean` (untracked files only; `-n` is a no-op), a word the shell would
still expand (`"$out"`, `${X:-y}`, a backtick) and anything outside the root
are not checked for green (a leading `~` is expanded), and an unparseable
command is never denied for green — the backstop covers all of them. An
`Edit`/`Write`/MCP write is judged by the guard of its REALPATH too, so a
symlink outside every worktree (`/tmp/x.py -> <root>/lib/a.py`) cannot write
past a red or green zone. The green reason: "MindFlock scope: {rel} is outside
the green zone(s) the user scoped this task to (≤5 names, then `(+N more)`).
Finish the in-scope work and list any out-of-scope files you need in your reply
instead of editing them." Each green deny lands in the feed as `deny: {path,
pattern: "outside green", kind: "green", request: true, reason}` — the Map
shows it as a scope request with **[Allow this file]**. **Reads are never
blocked** (a shell `cat` can't be, and blinding the agent to the callers of
what it edits breaks them); reads outside the scope come back in `/live` as
`peek` ("peeked outside scope", advisory). A read-restricted checkout (sparse
checkout of the scope + manifests) is future work.

**Revert allowance** (red and green). `git checkout [HEAD] [--] p`, `git
restore [--source=HEAD] p` and `rm p` are let through when `p` is in a backstop
`breach` of the session's own feed from the last 15 minutes that says the
revert is exact: `clean_at_pre: true` (and not `committed`) for
checkout/restore (the file had no uncommitted change before the flagged
command), `new: true` for `rm` (the command created it). A restore from any
OTHER tree-ish (`git checkout <ref> -- p`, `git restore --source=<ref> p`)
writes that ref's content and is never a revert. The v2
backstop suggested a `git checkout` its own pre-hook then denied.

**Backstop.** On every Bash call with zones in force the pre hook snapshots
`git --no-optional-locks status --porcelain=v1 -z -unormal` per base (the root
and the nested `.claude/worktrees/<n>` containing the cwd; 3 s timeout, 5000
entries; `--no-optional-locks` so it never takes `index.lock`; R/C entries'
origin consumed as a second field and counted as a write; submodules are one
path; `.claude/worktrees/` and embedded repos skipped). On a timeout the snap
stores "no baseline" and the post skips the green diff. Post: a tracked path
newly dirty — or dirty before with its stat changed — that is outside/blocked
is a breach (`{path, pattern: "outside green", kind: "green", clean_at_pre}`).
So is a path the command COMMITTED in the same call (`git commit`,
`cherry-pick`, a merge of a local branch): the pre snap records each base's
`HEAD`, and a forward move adds `git log --name-only <pre>..<post> --not
--remotes` (a pull/rebase/reset onto upstream is not this session's edit) —
those carry `committed: true` and are never offered a `git checkout --`.
A new untracked path outside is only a soft `artifact` flag on the record.
Feedback never tells the agent to revert work it may not own: "these files
outside your green zone(s) changed while your command ran … if you changed
them, stop and tell the user; don't revert files you didn't intend to change",
with a `git checkout --` offered only for paths clean at pre. Red breaches get
the same rule (a path that had WIP before is named "don't revert it").
Git-ignored files are out of scope for green.

**Exemptions.** When a green zone is added (or removed — the scope narrows),
every path already changed (working tree or committed vs the fork point) that
the change makes outside is recorded in `green_exempt: {rel: "sha [sha …]"}`
— every identity the path had then, space-separated: its working-tree blob
(`git hash-object`, or `"deleted"`) AND its blob at `HEAD` and at
`origin/<branch>` when pushed (a v3.0 single sha still reads). The monitor,
`/live` and the push/PR gate skip it while its content — the working tree's,
or the blob at the gated ref — equals any recorded one, so work committed
before the scope and then edited further no longer blocks the push; edited
again after the scope it is a breach. The add answer
carries `exempt` + `committed_outside`, and the UI offers "N files already
changed outside this scope — [Keep exempt] [Treat as breaches]" (default
exempt; `POST …/red-zones/exempt`).

**Messaging.** A green notice never says "revert" ("keep what you already
changed"); adding, widening (an allow) and removing a green zone each tell the
agent; a CLI without a hard guard is told edits are "flagged and block pushes",
not "blocked". A launch prompt names the scope ("Scope (MindFlock green zones):
only modify files under `a`, `b` — everything else is read-only (…)").
**Go — only the planned files** (`POST …/code-map/go {scope_to_plan: true}`)
turns the approved plan into anchored green zones (exact paths; the parent dir
of a new file) and names that scope in the go message; `/live` flags plan
items outside the scope (`outside: true`) on every poll.

Per-session routes return 404 for an unknown title, 409 `{"error": "workspace
not ready"}` without a worktree, and 409 "git is not installed" without git.

| Method | Path | Behavior |
|---|---|---|
| GET | `/api/instances/{title}/code-map?fp=` | `{root, repo: {id, label}\|null, fingerprint, files: [[rel, size, flags]], truncated, edges: [[src, dst]], graph_partial, langs}`. `flags`: 1 = zone-matched git-ignored file, 2 = test file; an edge means *src imports dst* (Python, JS/TS, CSS resolvers). `fingerprint` = worktree fingerprint + a digest of the zone set; when `fp` equals it → `{unchanged: true, fingerprint}` — except while the import graph is partial (`graph_partial: true`, the read budget ran out on a big/cold repo): then the route always rebuilds (resuming from the per-file memo) and the snapshot's `fingerprint` carries a `.partial` suffix, so it differs from `/live`'s and the client refetches until the graph is complete |
| GET | `/api/instances/{title}/code-map/live?since=` | `{now, fingerprint, repo, changed: [{path, status, added, removed}], feed: [records with ts > since, writes/reads worktree-relative, outside paths dropped, deny/breach/artifact passed through, subagent fields `agent`/`agent_type` (a call made inside a subagent) and `desc`/`atype` (the parent's Agent call) passed through, `peek: [rel]` = reads outside the green scope; ≤300], plan: {source: "declared"\|"exitplan"\|null, ts, items: [{path, intent, new, outside?, blocked?}], thread}, off_plan: [rel], zones: [effective zones incl. waived: {id, pattern, name, note, created, scope, re, waived, kind}], breaches: [{path, pattern, zone_id, committed, kind}], guard: {state, detail, hard, mode}, mode: "green"\|"red"\|null, exempt: {rel: "sha [sha …]"}, companions: [{pattern, re, source: "default"\|"repo"\|"tests"}], companion_files: [rel], activity, plan_supported, ci, others: [{session, path, ts}]}` — a green breach has `pattern: "outside green"`, `zone_id: null`, `kind: "green"` — `guard.detail` is a full sentence for the pill's tooltip (what the state prevents and what it doesn't, e.g. "Claude Code is blocked before it edits a red zone (hook verified 2 min ago)"), never the label; `ci` = the worktree's filesystem is case-insensitive, so zones match with IGNORECASE (the hook does); `others` are edits by other live sessions on the same repo in the last 10 minutes, on paths that exist here |
| GET | `/api/instances/{title}/code-map/atlas?path=` | One **Atlas** level: the children of directory `path` (`""` = repo root; single-child chains collapsed, JVM `src/main/<lang>/<package>` chains transparent — only for a real JVM source set, whose `main`/`test` holds a `java`/`kotlin`/`scala`/`groovy` root; its other children such as `src/integrationTest` are nodes of their own; a symlink resolving outside the worktree contributes no names) → `{path, crumbs: [{path, name}], nodes: [Node], tiers, partial, truncated, hidden, back_edges: [[from, to]], extras: {tests, files: [{path, name, files}]}, fingerprint}`. Node = `{path, name, kind: "dir"\|"file"\|"more", role: "code"\|"tests"\|"files", lang, langs: {ext: n}, files, loc, symbols, public, interface: [{name, kind, path, line, used_by, scope: "external"\|"internal"\|"declared"}] (≤ 8), interface_total, deps_out: [sibling path], deps_in: [sibling path], ext_out, ext_in, tier, entry, tested_by, tests}`. `tier` 0 = top row (callers) … increasing = more depended upon; −1 standalone code, −2 tests, −3 files / the "+N more" fold; `tiers: 0` = no relations (plain grid). `back_edges` = the lighter edge of each cycle (render "both ways"). Cached per worktree content fingerprint (`fp` is accepted and ignored). 400 for an absolute or `..` path |
| GET | `/api/instances/{title}/code-map/file?path=` | The file view → `{path, lang (family), loc, role, symbols: [{name, kind, line, end, public, sig, parent, children, changed?}], imports: {internal: [{path, name, folder, names}], external: [str]}, entry: [{kind: "http"\|"cli"\|"event"\|"main", method, route, line, handler, changed}], entry_groups: [{prefix, count, changed, items}] (only past 24 routes), used_by: [{path, name, folder, names}], tested_by: [path], changed_lines: [[a, b]] (new-side hunks vs the fork point), changed_symbols: ["Class.method"], zones: {red, green: bool\|null}, partial}`. `zones` goes through the shared classifier on the realpath AND the path as written (a symlink into a red zone is red): `red` = blocked, `green` = writable under the green scope (null = no green zone). An unreadable file (mode 000, EIO) is an empty view, never a 500. Paths are taken literally first (git tracks ` lead/` and `back\\slash/` on Linux), leniently (whitespace-trimmed, `\\` as `/`) only when the literal names nothing. 400 for an absolute / `..` / escaping / missing / directory path |
| GET | `/api/instances/{title}/code-map/search?q=&limit=40` | Files, folders, symbols and routes matching `q` (case-insensitive, camel/snake-aware, initials; tests rank lower) → `{items: [{path, name, kind, line, score}], partial}`; `kind` is a symbol kind, `"file"`, `"dir"` or `"route"` (a route matches by its route or by the `METHOD /route` label it shows); `limit` ≤ 200 |
| GET | `/api/instances/{title}/code-map/entry-points` | The repo-wide "swagger" lens → `{items: [{kind, method, route, line, handler, path, folder}] (≤ 500), total, dropped (entry points in test files, excluded), counts: {kind: n}, partial}` |
| POST | `/api/instances/{title}/code-map/ask-plan` | Body `{mode: "plan"\|"remaining"}` → sends the plan prompt (list every file you intend to touch in a `mindflock-plan` block, then wait) or its mid-flight variant → `{ok, told: "sent"\|"queued"\|false, reason?}` |
| POST | `/api/instances/{title}/code-map/go` | Body `{zone_ids: [...], scope_to_plan?: bool}` (zones staged during plan review) → one "go ahead, and don't modify these" message → `{ok, told, reason?}`. `scope_to_plan: true` = **Go — only the planned files**: the plan's items become anchored worktree green zones (an existing path exactly; a new file's parent dir; a new root-level file itself — every path glob-escaped, so `pages/[id].tsx` is literal), earlier work outside is exempted, the guard synced, and the message names the scope → also `{zones: [created], exempt: [rel]}`; 409 when the plan names no usable path |
| GET | `/api/instances/{title}/red-zones` | `{repo: {id, label}\|null, plan_first, zones: [effective, with kind], mode, exempt: {rel: "sha [sha …]"}, companions: [repo patterns], sessions_here}` (`sessions_here` = sessions sharing this worktree — a green zone applies to all of them) |
| POST | `/api/instances/{title}/red-zones` | Body `{pattern, name?, note?, kind: "red"\|"green" (default red), scope: "repo"\|"worktree" (default repo for red, worktree for green), tell_agent?, exempt?: bool}` → adds the zone, syncs the guard of every live worktree it reaches (repo scope: every live session on that repo id plus the repo's `git worktree list`) **before returning**, records files already changed inside it as pre-existing → `{ok, zone, zones, told, already_changed: [rel], reason?}`. 400 invalid pattern (empty, `..`, absolute, > 400 chars) or scope; 409 when repo scope is asked for and git can't identify the repo, or when the same pattern is already the other kind. `tell_agent` sends the zone notice. **Green**: `scope: "repo"` → 400; paths already changed outside the new scope are exempted (unless `exempt: false`) → also `{exempt: [rel], committed_outside: n}`; no `already_changed` seeding |
| POST | `/api/instances/{title}/red-zones/allow` | Body `{path, tell_agent?: bool (default true)}` — **[Allow this file]** on a scope request: an anchored worktree green zone for exactly `path` (glob-escaped by `red_zones.glob_escape`, so `app/[slug]/page.tsx` is literal, not a character class; synced now) and a "you may now edit it" notice → `{ok, zone, zones, told, reason?}`; 409 when the worktree has no green zone (a first one would scope the whole session to one file) or a red zone covers the path; 400 for an absolute / `..` path |
| POST | `/api/instances/{title}/red-zones/exempt` | Body `{exempt: bool, paths?: [rel]}` — `false` = "Treat as breaches" (drop those / all exemptions); `true` + `paths` = exempt them at their current content (working tree, `HEAD` and `origin/<branch>` blobs) → `{ok, exempt: {rel: "sha [sha …]"}}` |
| POST | `/api/instances/{title}/red-zones/preview` | Body `{pattern, kind?}` → dry run, nothing saved: `{re, count, sample: [≤20 rel], ignored_count, changed: [rel], truncated}`; 400 invalid pattern. `kind: "green"` adds `{writable_files, changed_outside: [rel], committed_outside: n, roots: [distinct matched top paths], unanchored: bool, anchored: "/<root>"\|null (offered when exactly one root), warnings: [str]}` — warns on an unanchored basename pattern (it matches at any depth) and on zero matches ("nothing exists here yet; the agent may only create new files under it") |
| DELETE | `/api/instances/{title}/red-zones/{zone_id}?tell_agent=` | Remove the zone (any scope) and re-sync the guards it reached → `{ok, zones, told}`; 404 unknown id. Removing a GREEN zone exempts the work already done inside it and tells the agent the scope narrowed (`?tell_agent=0` to skip) |
| POST | `/api/instances/{title}/red-zones/{zone_id}/waive` | Body `{waived: bool}` — "Allow here": a repo zone stays drawn but is not enforced in this worktree → `{ok, zones}`; 400 for a worktree zone, 404 unknown id |
| GET | `/api/red-zones` | `{repos: {repo_id: {label, zones, plan_first, companions}}}` — every repo, outside any session (`zones` are red; green is worktree-only) |
| POST | `/api/red-zones` | Body `{repo_id, pattern, name?, note?, label?}` → add a repo zone + re-sync every live worktree of that repo → `{ok, zone, repos}`; 400 missing repo_id / invalid pattern / `kind: "green"`; 409 when a worktree of the repo has the same pattern as a green zone |
| GET | `/api/red-zones/companions?repo_id=` | `{repo_id, patterns, defaults}` — the repo's companion patterns ("derived outputs" writable outside a green scope) and the built-in defaults; 400 without `repo_id` |
| PUT | `/api/red-zones/companions` | Body `{repo_id, patterns: [str], label?}` → replaces them (validated like zone patterns; nothing saved on a bad one), re-syncs the repo's live worktrees → `{ok, repo_id, patterns, defaults}` |
| DELETE | `/api/red-zones/{zone_id}` | → `{ok, repos}`; 404 unknown id |
| POST | `/api/red-zones/plan-first` | Body `{repo_id, on, label?}` → the repo's intake sessions (tickets, issues, PR reviews — started from Intake or by the ingestion pipeline) open with the plan-first instruction when their CLI supports plans → `{ok, repos}` |

**Delivering messages** (`ask-plan`, `go`, `tell_agent`): typed into the agent
when it is idle, **queued** on the session's prompt queue while it is
`working`, on a `clarify` prompt, or on the usage-limit screen — or whenever the
screen shows a dialog, whatever the activity says (typing there
would interleave with the turn or answer the prompt), and never used to reboot
an agent — a dead session reports `told: false` with a reason, as does one over
its cost budget. `POST /api/instances` accepts `plan_first: true` to append the
plan instruction to the initial prompt — for a CLI whose provider has
`plan_supported` (see `/api/providers/manage`) only: the instruction ends
"wait for my go-ahead", and the Go button is in the Map's Plan section, which
other CLIs don't have. Every launch prompt (manual, ticket, issue, PR review;
Intake or pipeline) names the repo's zones; a provisioned start keys off the
repo's local checkout, the configured default repo when local, or the
provisioning base clone.

### IDE

| Method | Path | Behavior |
|---|---|---|
| POST | `/api/instances/{title}/ide` | Open/focus the workspace in the configured IDE (Settings → Advanced; Cursor by default). GUI editors get new windows maximized + existing ones focused; terminal editors open in a new terminal window → `{ok, opened_new}`; 400 if the IDE isn't launchable |
| GET | `/api/ides` | The known-IDE registry: `{ides: [{command, name, kind, installed}], current, current_name}` — for the Settings detected-IDE picker |
| GET/POST | `/api/cursor/autoadopt` | Get/set `{enabled}` — auto-adopt Cursor-opened workspaces as sessions |

### Send a message + prompt queue

| Method | Path | Behavior |
|---|---|---|
| POST | `/api/instances/{title}/send` | Body `{text, submit?, dialog_safe?}`. Types `text` into the **agent** window and (default) presses Enter, booting/resuming the agent tmux first if it isn't running — so one call kicks a fresh session into motion (max token use). `submit:false` types without submitting. Enter is a separate keystroke a beat after the text so an agent TUI doesn't read the burst as a paste. `dialog_safe:true` (the web UI's Thread "Send now" and every playbook paste) re-probes the agent's live, uncached activity first — and checks the screen for a dialog, which outranks the reading (see the screen-evidence guard under [Inter-agent messages](#inter-agent-messages)) — and never types into a prompt or the usage-limit menu (a digit or an Enter there would answer it): on `clarify`/`limit`, a dialog on screen (or an activity that can't be read) a submitted message is **queued** instead → `{sent: false, queued: true, submitted: false}` (the drain delivers it once the agent is free), and an unsubmitted paste is **409** `{error, in_dialog: true}` with nothing typed (queueing it would submit it later). A working agent is still typed into. → `{sent, submitted}` (409 if the workspace is gone or `{budget_locked: true}` when the session is over budget, 502 if the send fails) |
| GET | `/api/instances/{title}/queue` | `{items: [{id, text, added}], pending, enabled, loop, loop_interval, wait_for_limit, limited_until, last_sent}` |
| POST | `/api/instances/{title}/queue` | Body `{text, index?}` — append a prompt, or insert it at a 0-based position (clamped) when `index` is given — or `{texts: [...]}` to bulk-append (one write; blank rows skipped, overflow past the queue cap dropped; response adds `added`/`skipped` counts). Enqueuing re-enables draining. → queue state |
| POST | `/api/instances/{title}/queue/flags` | Body `{enabled?, loop?, loop_interval?, wait_for_limit?}` — `enabled` gates auto-draining; `loop` re-queues each sent prompt so a self-improving prompt cycles forever; `wait_for_limit` holds draining until the usage window resets |
| POST | `/api/instances/{title}/queue/reorder` | Body `{id, index}` — move to an absolute 0-based position (clamped; the drag-and-drop path) — or `{id, direction}` (`up`/`down`) to nudge one slot |
| POST | `/api/instances/{title}/queue/edit` | Body `{id, text}` — rewrite a queued prompt in place |
| DELETE | `/api/instances/{title}/queue?item=<id>` | Remove one item; omit `item` to clear the whole queue |

A background drain loop feeds the queue into the agent whenever it is **idle**
(finished a turn / at its prompt) — idle that has *persisted*: for
`_QUEUE_IDLE_SETTLE` (12s) before the first send, dropped to
`_QUEUE_IDLE_SETTLE_MARKER` (4s) when the idle reading is authoritative
(`agent_state.reading_is_authoritative` — the CLI's own hook said so), so a
hook CLI's queue drains one pass sooner. It never interrupts
`working`/`clarify`, and
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

### Another agent's view: output + answer

What the MindFlock MCP's `read_output` and `answer_prompt` call, so one agent
can read another's result and unblock a dialog. See [mcp.md](mcp.md).

| Method | Path | Behavior |
|---|---|---|
| GET | `/api/instances/{title}/output?view=last_reply\|transcript\|screen&max_chars=N` | What the session's agent produced → `{view, text, truncated, activity}`, plus `fallback: true` when the view fell back. `last_reply` (the default) is the provider's newest assistant message. A provider with none (no readable transcript, or no reply yet) falls back to `screen`, answering `view: "screen", fallback: true`. `transcript` is the text `/history?pane=agent` serves (the provider transcript, else the pane's scrollback). `screen` is the visible pane only (`tmux capture-pane -p -J`, no scrollback), which is what a dialog looks like. `max_chars` defaults to 6000 and is clamped at 50000; truncation keeps the **tail**. `activity` is the memoized probe. **400** for a bad view or a non-integer/non-positive `max_chars`, **404** for an unknown session, **409** `no live session` when the view needs the tmux pane and it is gone |
| POST | `/api/instances/{title}/answer` | Answer a dialog the agent is blocked on. Body `{text?, keys?, dialog_id?, by?}`. `text` (≤ 2000 chars, all C0/C1 control characters stripped) is typed literally **without** Enter, even when it starts with `-`. Then each of `keys` (≤ 20, allow-list `Enter Escape Up Down Left Right Tab BTab Space 1`–`9 y n`) is pressed in order. Allowed while the agent's live, uncached activity is `clarify` or `limit`, or — whatever the reading (`offline` excepted) — while its provider **parses a dialog on the visible screen**: screen evidence beats the activity layer, so a click on a dialog that is plainly up is never refused because a background sub-agent's hooks made the session read `working` or `idle` (it then answers `activity_before: "clarify"`). Anything else answers **409** `session is not waiting on a prompt (activity: X)`, because typing into a working or idle agent is a prompt, not an answer (that is `/send`). `dialog_id` (optional, from `GET /dialog`) pins the answer to that dialog: when the dialog on screen now has a different id (it was answered meanwhile and the next one is up), or the screen can't be read, nothing is typed and the answer is **409** `{"error": "the prompt changed", "dialog_changed": true}`. One answer per dialog: the route holds a per-session lock from its screen read through its keys, and a settling answer (any of `1`–`9`, `Enter`, `Escape`, `y`, `n`) pinned to a `dialog_id` that got one less than 4 s ago (`agent_io.ANSWERED_HOLD_S`) is **409** `{"error": "that prompt was just answered", "dialog_answered": true}` — the CLI may not have redrawn yet. Seeing a different dialog in between (on `/dialog` or `/answer`) clears that memory, so an identical prompt asked again is answerable. `by` is `"agent"` (default) or `"user"`: an agent's answer doesn't count as human input; a person's click in the UI (`by: "user"`) is stamped as presence like `/send`. → `{ok: true, activity_before}`. **400** for a bad body (including a non-string or > 64-char `dialog_id`, or another `by`), **404** unknown, **409** over budget (`budget_locked: true`) or just answered (`dialog_answered: true`), **502** when tmux refuses the keys |
| GET | `/api/instances/{title}/dialog` | The dialog the agent is blocked on, as data, for the UI's answer buttons → `{id, parsed, question, command, options: [{key, label, kind}], source?}`. The session's provider parses the visible screen (`BaseProvider.parse_dialog`; Claude Code and Codex implement it, pinned against golden screens in `tests/unit/data/dialogs/`): `question` is one self-contained line (`"Bash command — Add redis as a dependency. Do you want to proceed?"`), `command` the command or MCP tool call it asks about (else `null`) — for Claude Code 2.x's Bash dialog the boxed command line itself (its description goes into `question`), for an MCP **Tool use** dialog the call led by the argument that names it, chosen by name — `title`, then `session` / `to` / `target`, else the first argument with a value (`hello-worker · mindflock — Spawn worker session`, also when Claude lists `prompt:` first; the arguments go into `question`) so a narrow strip still says WHICH call it is. A command the dialog's box hard-wrapped mid-token at the pane's edge (a long path) is joined back without a space; its own line breaks (a heredoc) are kept. A tab header on the heading (`· from the general-purpose agent 2 of 3`, a Claude background sub-agent's prompt) is cut off the question and reported as `source` (`"general-purpose agent"`, only when set). A side panel Claude draws to the right of the dialog (its diff view) is cut away before parsing. `options` the dialog's own numbered choices with `key` the digit to press and `kind` one of `yes` (approve once), `always` (a standing rule — "don't ask again", "allow all edits during this session"), `no` (refuse / exit) or `other`. A screen no parser recognizes answers `parsed: false`, the best-effort question line and no options — a numbered list the agent merely printed carries no selection cursor and never parses, and an option list counts only when nothing but blank lines, rules and a key-hint footer follows it (a numbered prompt in the transcript has more transcript or the input box under it). Claude Code's parser takes only its `❯` cursor (`>` marks the user's own prompts) and needs the rule Claude draws above every dialog — a rule with text drawn on it still counts. `id` is a 12-hex digest of the dialog built **per component** — each paragraph above the options (heading, command box, question), then each option as `key:label` — with the selection cursor and **all whitespace** removed; what the width can cut (every option label, Claude's collapsed "About the … Tool:" description, any paragraph with a line cut short with `…`) counts only by its first 24 characters before the cut, `ctrl+o to expand` hints and the tab header's `N of M` not at all. Nothing is left out whole, so it holds while the same dialog is up — arrowing through options, a resize that re-wraps or re-cuts its lines (47 to 200 columns), another tab being answered — and changes with the next prompt (another command, tool, argument or question); the 2-option variant Claude draws at ≤ 80 columns has other keys and so another id. Send it back as `/answer`'s `dialog_id`. Served while the live, uncached activity is `clarify`, or whatever the reading while the provider parses a dialog on screen; `limit`/`offline`, or another reading with no parsed dialog, is **409** `session is not waiting on a prompt (activity: X)` — or, with `?quiet=1` (what the UI's answer strips send), **204** with no body: "not waiting" is the routine answer for a strip whose row reading is a poll behind, and a 4xx would be logged by the browser every time. **404** unknown, **409** `no live session` (also with `quiet`), **500** when the capture fails |

### Inter-agent messages

A message one session's agent (through the MindFlock MCP), the CLI
(`mindflock msg`) or any API client leaves for another session. It is stored
in the recipient's mailbox (`~/.mindflock/mailbox.json`) and reaches it
**exactly once**, by whichever happens first:

- the server's delivery lane types it into the agent pane once the agent is
  stably idle, so it becomes `delivered`;
- the recipient fetches it with `mark_read`, so it becomes `read`, which also
  cancels the typing.

The full semantics are in [mcp.md](mcp.md#messages).

| Method | Path | Behavior |
|---|---|---|
| POST | `/api/instances/{title}/messages` → 201 | Leave a message for `title`. Body `{text, from?, reply_to?, delivery?, kind?, data?}`, described below the table. → **201** `{message, delivery: "delivered"\|"pending"\|"held", detail?}` and a `session.message` event on the recipient. **400**: empty or non-string `text`, `text` > 20000 chars, an unknown `from`, `from` == the recipient, a bad `delivery`/`kind`, `data` not an object or > 8192 bytes serialized, a non-string `reply_to`. **404**: unknown recipient |
| GET | `/api/instances/{title}/messages` | The inbox → `{messages: [...oldest→newest], unread, version}`. Query parameters are described below the table. **400** for a non-numeric `limit`/`wait`, a bad `kind` or an unparseable `after` id. **404** for an unknown session |
| POST | `/api/instances/{title}/messages/read` | Body `{ids: [...]}` or `{all: true}`. Marks those messages `read`; only `pending`/`held` ones change, and a pending message marked read is never typed. → `{marked, unread}` |

**POST body.**

- `text` (required): at most 20000 chars.
- `from`: a live session's title, or `""` (the default) for the CLI or an
  external client.
- `reply_to`: the id being answered.
- `delivery` (default `auto`):
  - `auto` types it in when the recipient is next stably idle;
  - `inbox` stores it only;
  - `now` types it immediately when the recipient is `idle` or `working`.
    `now` is allowed only from an ancestor of the recipient (or from `""`);
    from anyone else it is sent as `auto` with a `detail`. It is never typed
    into `clarify`, `limit` or an offline agent, never over an open
    long-poll, never while the recipient's screen shows a dialog (whatever
    its activity reads — see the screen-evidence guard under the delivery
    lane), never into a paused, over-budget or still-setting-up session,
    never within 20 s of the prompt queue relaunching the agent, never into a
    pane no agent CLI holds (a shell the agent quit to, or `vim`/`ssh` run
    there), and never while someone has typed in that window in the last
    45 s. In those cases the message stays `pending` for the lane, with a
    `detail` saying why.
- `kind`: `message` (the default) or `result`. `result` is what the MCP's
  `report_result` sends, with `data: {status, branch, head_sha, diff_stat}`.
  For a `result` from a session the server replaces `data.diff_stat` with one
  measured from the sender's worktree as the message is posted.
- `data`: any JSON object, at most 8192 bytes serialized.

**Safety rules.** A push (`auto`/`now`) that breaks one is stored `held`, with
`detail` naming the rule:

- `hop` is the `reply_to` message's hop + 1. A push deeper than
  `MINDFLOCK_MSG_MAX_HOPS` (default 6) is held as a "reply chain limit".
- More than 6 pushes from one sender to one recipient within 10 minutes, or
  more than 30 from one sender overall, are held as a "rate limit". Sender
  `""` is exempt, and so is a sender's **first** `kind: "result"` to a
  recipient in the window (the report the parent is waiting on). Later
  results count like any other push.

**Message object.**

```jsonc
{"id": "m1759600000123_42", "kind": "message", "from": "orch", "to": "orch-w1",
 "text": "…", "data": null, "ts": 1759600000.12, "reply_to": null, "hop": 0,
 "delivery": "auto",            // the mode actually applied (a downgrade reads "inbox")
 "state": "pending",            // pending | delivered | read | held
 "delivered_ts": null, "read_ts": null, "detail": ""}
```

Ids are `m<epoch_ms>_<seq>`, where `seq` is a file-wide counter, so ids sort
globally.

**GET query parameters.**

| Parameter | Default | Meaning |
|---|---|---|
| `unread` | 1 | only `pending` and `held` messages |
| `include_consumed` | 0 | `1` also returns `delivered` and `read` messages |
| `after` | — | a message id; only strictly newer messages |
| `from` | — | only messages from this sender; `""` selects the CLI and external clients |
| `kind` | — | `message` or `result` |
| `limit` | 50 | clamped to 1–200 |
| `mark_read` | 0 | `1` atomically marks the returned messages read |
| `wait` | 0 | seconds, clamped to 0–30: long-poll until a match arrives |

- **Which messages come back.** An unread-only query returns the **oldest**
  `limit` matches, a FIFO inbox. With `include_consumed=1` it returns the
  **newest** `limit`. Either way they are ordered oldest to newest.
- **`version`** comes from the same counter as the ids. It changes on every
  mutation of that box and only grows, including across a dropped box, so
  compare it with `!=`.
- **Long-poll.** `wait` polls the box's version on the event loop, never on a
  parked thread. It returns early when the session is removed or the client
  disconnects. While it runs, and for 5 s after it ends, the delivery lane
  doesn't type into that recipient, because the poll is about to hand the
  message over itself.

The **delivery lane** is a pass in the 5-second prompt-queue drain loop. It
types at most one message per recipient per pass, and only under all of these
conditions:

- the agent is started, not paused, not over budget, and its setup isn't
  running or failed;
- no long-poll is open on its inbox;
- the agent wasn't just rebooted by the queue (20 s grace);
- its live activity is `idle`, settled for 4 s with hook evidence or 12 s
  otherwise;
- the 8 s send cooldown has passed, counting the queue's sends too;
- the user's prompt queue doesn't want this turn, and no fast-track chain is
  mid-flight;
- its tmux session is alive; the lane **never** boots an agent;
- an agent CLI holds the pane: its executable (any provider's binary, or the
  session's own program, also as `node …/claude`) is in the pane's process
  tree, so a shell the agent quit to, or `vim`/`ssh`/`psql` started there,
  never receives the line;
- no usage-limit banner is showing;
- nobody has typed in that window in the last 45 s (the web terminal, or a
  raw `tmux attach` / SSH client);
- the **screen shows no dialog** — checked last, on a fresh capture.

**The screen-evidence guard.** Every automated typer — this lane, `delivery:
"now"`, the prompt-queue drain (and its usage-limit resume), the limit
watcher's `continue`, `/send` with `dialog_safe`, the Code Map's button
messages and the playbook render (its **409** "Answer its prompt first") — captures the pane right before it types
and **holds** when the session's provider sees a live dialog at the bottom of
the screen (`BaseProvider.dialog_on_screen`: a parse, or the provider's
waiting-prompt / trust phrases in the bottom 15 lines), whatever the activity
reads. Screen evidence beats the activity reading: in a live run the
orchestrator read `idle` (its main turn's Stop hook) with a background
sub-agent's `answer_prompt` permission on screen, this lane typed a worker's
result into it, and the line's Enter approved the dialog. A held message stays
`pending` (nothing is claimed), a held queue item stays queued; a capture that
fails is no evidence either way (the send itself then decides).

Typed messages don't count as human input and never touch the user's queue.
Every typer into an agent pane (the queue, the lane, `now`, `/answer`, `/send`,
the usage-limit resume) holds one lock per tmux session across its text, pause
and Enter, so two of them can never merge into one submitted turn.
A body over 1500 characters (after it is flattened to one line) is typed as a
one-line notice with a 300-character preview. The message stays `held` so the
full text can still be fetched.

### Worker order and fences

An orchestrator's say over its workers ([mcp.md](mcp.md#fences-and-order-workers-that-dont-collide)):
what each may change, and in what order they run. MindFlock enforces both.

| Method | Path | Behavior |
|---|---|---|
| POST | `/api/instances/{title}/fence` | `{only: [glob], keep_out: [glob], reason, by, clear}` → `{ok, fence, applied, held, told?, shared_folder?, problems?, note?}`. A **per-session** fence (the zone store's `worktrees[<folder>].sessions[<tmux name>]`, owner `orch:<by>`): `only` = the only paths this session may change, `keep_out` = paths it may not; enforced by the guard hook for this session alone. In its own worktree its companions stay writable; in a shared folder only its paths. Replaces its fence; `clear: true` removes it. A held worker gets it in front of its task; a running one is told (`told`). `applied: false` with a `note` = its folder isn't there yet (it lands before its task starts). **404** unknown session, **400** a bad glob, a path in both lists, or neither list, **409** a team run's member (its group fences it) |
| GET | `/api/instances/{title}/order` | → `{order}`: `null`, or `{mode: "parallel"\|"serial", max_parallel, cap, steps: [{n, workers: [{title, state: held\|running\|done\|stopped\|planned, word, detail, after, why: {title: "asked"\|"step"\|"one at a time"\|"overlap: <path>"}, fence, released_at, ended_at, planned}]}]}` — a worker's step is one more than its latest predecessor's |
| POST | `/api/instances/{title}/order` | `{mode, max_parallel (0–16, 0 = no limit), steps: [[title]], after: {worker: [title]}, start_now: [title]}`, any subset → `{ok, order}`. `steps` replaces the declared plan (titles may name workers not spawned yet); `after` re-orders a **held** worker; `start_now` releases held workers at once. **400** with `problems` (nothing applied) for a cycle, a title in two steps, an unknown or already-started worker, a bad mode or cap; **404** unknown session |

`POST /api/instances` takes `after: [title]` (hold the task until those are
done; needs `parent`), `fence: {only, keep_out, reason}` and `overlap:
"wait"|"parallel"`. An ordered create answers `prompt_delivery: "held"` and
`order: {held, after, why, fence}`: the session is created, its task waits in
its prompt queue (switched off and marked `held`, so a queued message never
switches it on) until its turn. `run_member: true` (set by team runs on their
own members) opts out. Each row carries `order: {state, word, detail, after,
fence}` for an ordered worker, else `null`; `code-map/live` adds the session's
own fence to `zones` (scope `session`, `locked`, `by`) and lists its workers'
fences as `fences: [{session, only, keep_out, reason, by}]`.

### Family thread

The mail an orchestrator and its workers exchanged, for a person to read — the
web UI's Thread tab. Strictly read-only: it never marks a message read and
never claims a pending delivery, so looking at it can't cancel a typing or eat
the report an orchestrator is waiting on.

| Method | Path | Behavior |
|---|---|---|
| GET | `/api/instances/{title}/thread?limit=50&before=<item id>` | → `{title, parent, members, finished, items, more, order}`. `order`: the order its workers run in (the shape of `GET …/order` below), or `null`. `finished`: its children that were closed or deleted, oldest first — `{title, branch, created_at, ended_at, how ("closed" = reopenable from Recently closed, or "deleted"), stage, pr_url, diff_stat, last_report, seed}`, recorded from each child's last row as it left and kept with this session (a reused title doesn't inherit them); their spawn records and messages are in `items` too. Messages count only when sent since both their sender and their recipient were created (a reused title doesn't inherit its deleted namesake's mail — the rule the row's `last_report` applies). A `before` id that is no longer stored (inboxes drop their oldest messages over their caps) pages back from the time its id carries (`m<ms>_<n>`, `spawn:<title>:<ms>`). **404** unknown session, **400** a non-integer/non-positive `limit` or a `before` id that is neither stored nor time-stamped |

- `parent`: the session's live parent, or `""`.
- `members`: the session itself (`role: "self"`), its live parent
  (`"parent"`) and its live children (`"child"`), each `{title, role, status,
  activity, activity_since, branch, diff_stat, created_at, last_report,
  base_sha}`. Status, activity, branch and diff stat come from the listing's
  tick snapshot (at most a tick old; a member it doesn't have yet gets a cheap
  row and the memoized activity probe). `last_report` is the row field above;
  `base_sha` is the commit the member's worktree was cut from (`null` when not
  recorded).
- `items`, oldest first (newest last): `{type, id, ts, from, to, text, status,
  state, base_sha}`.
  - `spawn`: one per parent→child edge in the family (the session's own
    spawn when it has a parent, and one per child). `ts` is the child's
    `created_at`, `text` the first 300 characters of its seed prompt when
    known (remembered at create time, else the instance's own prompt, else a
    provisioned workspace's prompt file; `""` otherwise), `base_sha` the
    child's fork commit. Its `id` is `spawn:<child>:<created_at ms>`.
  - `message` / `result`: every mailbox message whose sender **and**
    recipient are both members, in both directions, consumed or not (mail from
    the CLI or an unrelated session is left out). `text` is the stored body
    (capped at 4000 chars), `status` a result's `done`/`blocked`/`failed`,
    `state` its delivery state (`pending`/`delivered`/`read`/`held`).
- `limit` (default 50, max 200) keeps the newest items; `before` pages back
  from an item's `id`, and `more` says older items exist beyond the page.

### Playbooks

Named orchestration prompts the UI pastes into an agent's input box — **Split
across workers**, **Ask a session**, **Check on workers**, **Wrap up
workers**. The registry is `backend/mcp/playbooks.py`; every template is one
paragraph of at most 600 characters (Claude Code collapses a longer paste into
"[Pasted text]") that names only real MindFlock tools, spelled
`mcp__mindflock__<tool>` for Claude and by the bare name for other CLIs. The UI
pastes the rendered text with `POST /send {"text", "submit": false}`; nothing
runs until the user presses Enter, and every step the agent then takes goes
through its own MCP tools. See [mcp.md](mcp.md#from-the-ui).

| Method | Path | Behavior |
|---|---|---|
| GET | `/api/playbooks?title=<t>` | → `{"playbooks": [{id, label, desc, letter, args: [{name, label, kind: "text"\|"session", required}], when: "any"\|"has_children", available, disabled_reason}]}` in menu order. With `title`, the menu for that session: a `when: "has_children"` playbook is **omitted** while the session has no live children, and every item is `available: false` with a `disabled_reason` when the session's CLI doesn't get the MindFlock tools (`This CLI doesn't get the MindFlock tools` — attach off or a provider with no auto-attach), when this launch didn't (`mcp_attached: false` → `Restart this agent to give it the MindFlock tools`), or while it is in `clarify`/`limit` (`Answer its prompt first — pasting now would answer the dialog`; pasted text would land in the dialog). **404** unknown title. Without `title`: the whole registry, all available (the New dialog's list) |
| POST | `/api/playbooks/{id}/render` | Body `{title, args: {}}` → `{text}`: the prompt for `title`'s agent. A text argument left empty ends the text on its lead-in (`The task: `, `The question: `) so the user types straight on. `ask`'s `session` must name another live session; `wrapup`'s optional `only` must name one of `title`'s live children; `wrapup` lists the children that have reported and merges into `title`'s branch. **400** unknown id, a missing `title`, or bad args (an undeclared or non-string argument, a missing required one, one over its length cap, a session argument naming no such session, a filled-in text argument that would push the paste past 600 characters — the error names the room left); **404** unknown title; **409** `{error, disabled_reason}` when the paste can't go in now — the menu's reasons, with the activity probed live: the agent is on a prompt or the usage-limit menu, this launch has no MindFlock tools (`mcp_attached: false`), or its CLI gets none. Session arguments are matched exactly (no length cap of their own beyond 256, runs of spaces kept); the text quotes at most 120 characters of a title |

v1 registry:

| id | Label | Letter | Args | Shown | Tells the agent |
|---|---|---|---|---|---|
| `split` | Split across workers… | S | `task` (text) | any | `whoami`; commit shared groundwork; disjoint pieces; one `spawn_session` each; `wait_for_session`; answer only read-only prompts; per report `get_diff`, merge, run the tests; ask before `kill_session` with mode delete |
| `ask` | Ask a session… | A | `session` (required), `question` (text) | any | `send_message` to it, then `wait_for_message` from it, then use the answer (give up after 10 minutes and say so) |
| `workers` | Check on workers | C | — | has children | `list_sessions` filter children; one line per worker; answer only clearly safe read-only prompts, flag the rest; touch nothing |
| `wrapup` | Wrap up workers | W | `only` (session) | has children | per reported worker (or just `only`): `get_diff`, merge into the branch, full test suite; stop on a conflict or failure; ask before deleting |

### Team runs

A **team run** (a "group" on screen) is "work on these things together":
one session per ticket or task line, at most `concurrency` at a time with the
rest queued, each carried along its ship lane by the autopilot, with only what
needs you surfaced. The server keeps one record per run
(`~/.mindflock/runs/<id>.json`) and drives it with a 5 s loop. See
[team-runs.md](team-runs.md) for the model; the exact JSON is below.

`GET /api/outbox` still answers all four groups, though the web UI no longer
has an Outbox: the route feeds the bell's waiting list. The bell renders
`waiting` in its **Needs attention** list (merged with the per-session
attention rows, so there is one badge), and a finished group's header ⋯ menu
copies its entry from `summaries` (**Copy summary**; a group finished more
than a week ago, or a split's lead Thread, reads `summary.text_md` from
`GET /api/runs/{id}` instead). Nothing else in the UI reads it; `shipping`, `shipped` and `queued` are there for other clients (a
group's queued lines reach the UI through `GET /api/runs`).

| Method | Path | Behavior |
|---|---|---|
| POST | `/api/runs/preview` | Parse only, no side effects. `{text, repo_path?, program?}` → `{items, name_suggestion, lane_default, warnings}`. One thing per line; a line of only ticket-shaped tokens (`PAY-412`, `sc-9`, a URL, `org/repo#12`) is that many tickets. A ticket item: `{kind: "ticket", source, id, ref, title, repo, title_hint, has_session, error}` — an unresolvable one has `error: "not found in any source"` and is never turned into a task. A task item: `{kind: "task", text, repo, title_hint}`. A ticket that already has a session warns that it will be added, not restarted |
| POST | `/api/runs` | Create and start → **201** `{run: RunDTO, warnings}`. Body `{name?, items: [{kind: "ticket", source, id} \| {kind: "ticket", ref} \| {kind: "task", text}], policy: {lane?, ask_first?, grouping?, release?}, concurrency? (1–8, 3), program?, repo_path (required for task lines), budget_usd?, split?, split_optional?, max_pieces?}`. `split_optional` (with `split`; New's "Auto-split into up to N"): the lead may answer `POST /plan` with `pieces: []` to decline — the run is removed, the lead becomes an ordinary session armed with the group's lane, and `run.changed` fires with `state: "dissolved"` (`data: {run, lead, by, why}`); its lead is titled `<slug>`, not `<slug>-lead`. `max_pieces` (2+, with `split`) caps the plan, clamped to `MINDFLOCK_MAX_CHILDREN`. Both are ignored without `split: true`. `lane` defaults to the fast-track rung. A live session for an item is **adopted** (a warning), not a 409; an item another group owns is left out (a warning), and so is a ticket already in flight in the ingestion ledger for someone else (the pipeline is starting it). Queued tickets are **reserved** in the ledger (`in_flight`, `reserved_by: "run:<id>"`) so the pipeline never also spawns them; a reservation is handed back — only that entry — on every path that drops a queued ticket. Task-line titles never take a title whose branch already exists. **400**: `nothing to do`, `unknown lane`, `one PR for all commits each line …` (`grouping: "together"` with lane `leave`; `ask_first` there becomes `release: "ask"`), `repo_path is required for task lines`, `unknown agent …`; for `grouping: "together"` (one-for-all) `one-for-all needs a single repository` (no `repo_path`, or a ticket of another repository) and `one-for-all takes at most N lines` (`MINDFLOCK_MAX_CHILDREN`); for `split: true` `split needs exactly one line`, `this CLI doesn't get the MindFlock tools (…)`, `max_pieces must be a number`, `max_pieces must be at least 2`, and `lead is only for a split`. `lead: "<title>"` (split only) makes a live session the lead — any session in a git folder, also one that works directly in its folder or sits on its base/trunk branch (it plans; the approval decides where the pieces run, see `/plan/approve`; `lead.in_place` / `lead.trunk` say which) — 404 unknown, 409 workspace not ready / already in a group. A one-for-all group (and a split) creates its **lead** first (`<slug>-lead`, or `<slug>` for an optional split; a worktree of `repo_path`); when that create fails nothing is stored and the create's own status and sentence come back |
| GET | `/api/runs` | `{runs: [RunSummary]}`, newest first; `?active=1` unfinished only. RunSummary = `{id, name, state, paused, pause_reason, policy, counts: {queued, active, needs_you, shipped, failed, total}, cost_usd, created_at}` |
| GET | `/api/runs/{id}` | `{run: RunDTO}` — the run file minus bookkeeping, plus `counts`, `cost_usd`, `rev`, `waiting_for_usage` and per-task `row_present`. With `?wait=<s ≤ 60>&until=change\|needs_you\|done&rev=<n>` it long-polls and adds `reason` (that, or `timeout`). 404 unknown |
| POST | `/api/runs/{id}/pause` | `{reason?: "user"}` → `{run: RunSummary}`. Nothing new starts, merges or ships (every member's autopilot is held, a commit message you edited is kept for the resume; a releasing lead is disarmed and the group goes back to `release_ready`); a start or re-arm already under way lands held. Agents keep working. 409 when finished |
| POST | `/api/runs/{id}/resume` | `{budget_usd?}` → `{run: RunSummary}`: re-arms what the pause held (never a member already merging back), with each member's own lane / ask-first; `budget_usd` raises the budget in the same call (the bell's Raise) |
| POST | `/api/runs/{id}/cancel` | → `{run: RunSummary, kept_sessions: [title]}`. Stops starting and shipping: queued tasks are cancelled (their tickets go back to ingestion), running ones disarmed; every session and branch stays |
| POST | `/api/runs/{id}/tasks` | `{items: [...as at create]}` → `{run: RunDTO}` (adds lines; reopens a finished group). For a one-for-all group the lines fork off the lead (one repository, the child cap); **409** while its plan is pending or its release is running; a `checking` / `release_ready` group goes back to `running` (check and release card redone) |
| POST | `/api/runs/{id}/tasks/{task}/retry` | `{fresh?: false}` → `{task}`. A failed task, or one waiting on you: re-armed (or re-created); `fresh: true` starts `<title>-2` on a new branch and keeps the old one — disarmed, so it ships nothing more. **409** in any other state, and (non-fresh) when its title now belongs to an unrelated session (its incarnation differs — never armed). A vanished one-for-all member whose branch has commits goes back to the merge queue instead of being re-created |
| POST | `/api/runs/{id}/tasks/{task}/start-now` | → `{task}`: a queued task starts on the next pass, past the concurrency cap once. 409 unless queued, or while paused |
| POST | `/api/runs/{id}/tasks/{task}/skip` | → `{task}`: a queued task is removed; a running one is **detached** (its session keeps running, its lane unchanged, it leaves the group) |
| POST | `/api/runs/{id}/adopt` | `{title}` → `{task}`: add a live session (not restarted; the group's lane is armed on it). 404 unknown session; **409** `branch already in group <name>` when a group owns its `(repo, branch)` — a copy window is the same work |
| POST | `/api/runs/{id}/plan` | A split's plan: `{pieces: [{title, prompt, paths: [glob]}], why?, from?}` → `{plan, problems: []}`. Validated against the lead worktree's `git ls-files` and red zones: **422** `{error, plan: null, problems: [{piece, error}]}` for fewer than 2 / more than the run's `max_pieces` (else `MINDFLOCK_MAX_CHILDREN`) pieces, a piece without title/prompt/paths, a bad glob, a repeated title, two pieces sharing a file (`overlaps <other> on <file>` — over existing files, and over a new literal path one piece names and the other's globs cover), or a piece whose every file sits in a red zone. **409** when not a split, past `plan_ready`, or `from` names a session other than the lead (the lead's MCP sends `from`; the UI does not). Stores `plan.state: "proposed"`, run state `plan_ready`. On an optional split (`split_optional`), `pieces: []` declines instead → `{plan: null, problems: [], dissolved: true, lane}` (`lane` armed on the lead, `""` when none) and the run is gone. A decline is taken in `planning` and in `plan_ready` (a lead may withdraw a plan it proposed) and is checked against `from` like any plan (409 for a non-lead); on any other split it is a 422 like any plan under 2 pieces |
| POST | `/api/runs/{id}/plan/approve` | `{mode?: "worktrees" \| "same_folder"}` (default `worktrees`, recorded as `run.mode`) → `{run: RunDTO}`: every piece becomes a task (`kind: "piece"`, title `<lead base>-<piece>`) and starts at once as a worker of the lead (`parent` = lead, `spawned`). **`worktrees`**: `base_ref` = the lead's HEAD, `base_branch` = its branch, fenced to its `paths` with worktree green zones, armed at `commit`, merged back. A lead that works directly in its folder or sits on its base/trunk branch first gets a **new lead**: `<lead>-split` (numbered), a plain worktree of the same repository cut from the original's HEAD (`base_ref`), `base_branch` = the original's base, the integration brief; `run.lead` becomes it and `run.origin` = `{title, branch, head, in_place, trunk}` records the original, which is never merged into, switched or pushed. **`same_folder`**: each piece an in-place session in the lead's folder (`in_place: true`, no `base_ref`), fenced per session (`worktrees[<folder>].sessions` in the zone store, guard `sessions`), never armed; MindFlock commits each done piece's changed paths itself (plumbing, no hooks, trailer `MindFlock-Piece: <run>/<task>`), one commit per piece. **400** another mode. **409** without a proposed plan, the lead gone, uncommitted tracked changes in the lead (`code: "lead_dirty"`) or — for a new lead — in the original (`code: "origin_dirty"`, "they would not be in the split"), a `same_folder` lead on its trunk (`code: "trunk"`, `branch`: start a branch there first), or (`approve_plan` called directly) an in-place / trunk lead in `worktrees` (`code: "needs_lead"`) |
| POST | `/api/runs/{id}/lead/branch` | "Start a branch here first": `{}` → `{run: RunDTO}`. A split whose lead's folder is on its trunk (or detached): `git switch -c <branch prefix><lead>-split` (numbered when taken) in that folder — only on this call; uncommitted changes come along — and `lead.branch` is the new branch, `lead.trunk` false. **409** past `plan_ready`, the lead gone or already on its own branch, or a git operation in progress |
| POST | `/api/runs/{id}/plan/reject` | `{note?}` → `{run: RunDTO}` — "Ask for a different split": back to `planning`; the lead is messaged (with the note) to propose again |
| POST | `/api/runs/{id}/integrated` | The lead's `report_integrated`: `{task_id, head_sha, from}` → `{ok, verified}`. `from` is required and must be the lead (a person uses Retry); only a member waiting to merge (`integrating`, or needs-you on a conflict) with commits beyond its base is accepted (409 otherwise). Verified only when the member's branch head is in `head_sha` and `head_sha` is in the lead's HEAD; then it is `integrated` (`conflict_fixed`). `verified: false` leaves it in the merge queue |
| POST | `/api/runs/{id}/check` | `{}` → `{run: RunDTO}`: run the check on the merged branch again (409 unless `checking`) |
| POST | `/api/runs/{id}/release` | `{merge_when_green?: false}` → `{run: RunSummary}`. Only in `release_ready` (409 otherwise): arms the lead's lane — `pr` (`push` for a push group; `merge` **only** with `merge_when_green`: "Open the PR" never merges) — through `ship_now`. It ships exactly what the card and the check showed: **409** while the group is paused, the lead is mid-turn / on a dialog / at its limit, has uncommitted tracked changes (a same-folder split: **any** change in the lead's folder, untracked included — the lane would commit it into the PR), a merge or rebase in progress, or is off the group's pinned branch; when its HEAD moved since the check the group goes back to `checking` (409 says so) and the card is rebuilt. Armed with the server-built PR title and body on the autopilot record (`pr_title`/`pr_body`, passed to make-PR). The group then follows the lead's autopilot to `done` (`release.pr_url`), or to a handoff (`release.state: "handoff"`, `compare_url`) when no gh/token can file the PR |
| GET | `/api/outbox?group=<run id\|own\|all>` | What is waiting on you, shipping, shipped today and queued — for **every** session, de-duplicated on `(repo, branch)` → `{counts: {waiting, shipping, shipped, queued}, groups: {waiting, shipping, shipped, queued}, summaries}`. `waiting[]`: `{key, title, run, text, kind, reason, since, preview, actions}` with `kind` `prompt` (a dialog: `["answer","open"]`), `approve` (an ask-first lane parked for your go, with `step`, `lane` and `preview: {commit_message, pr_title, files, add, del}`: `["ship","diff","edit_message"]`), `stuck` / `blocked` (`["open","retry","skip"]`), `ship_halted` (`["retry","open","skip"]`), `restart` / `failed` (`["retry","retry_fresh","skip"]`), `budget` (`["raise_budget","stop"]`), `conflict` (`["retry","open","skip"]`), and a one-for-all group's own asks, titled with its lead: `plan` (`["approve","open"]`, `preview: {pieces: [{title, paths}]}`), `release` (`["release","open"]`, `preview: {pr_title, base, branch, files, add, del, check}`), `check_failed` (`["open","retry_check"]`), `lead_gone` (the lead lost for 10 minutes: `["cancel_group"]`), `stray` (a same-folder split: changes in the lead's folder no piece owns, or a commit no piece made: `["open"]`, `preview: {paths, commits}`). An `approve` item also carries `armed_at` (its identity: an edited message belongs to that card); its `preview.commit_message` is the FULL message — a person's, or the one drafted from the diff when a commit lane parked for approval (exactly what approving it unedited commits) — never a placeholder or a subject a model wrote for an earlier commit; for a commit approval `files/add/del` count the working tree's change (`diff_stat.uncommitted`) and `preview.pr_title` only a title someone set. A `release` item's preview carries the group's `lane` (the buttons follow it). `shipping[]`: `{key, title, run, text, step: commit\|check\|push\|make_pr\|merge\|integrate, note, lane}` (`integrate` = a one-for-all member waiting in the merge queue, or handed to the lead on a conflict). A one-for-all member never appears in `shipped[]` on its own — the group ships once, as its lead's PR. `shipped[]`: `{key, title, run, text, pr_url, pr_state, checks: pass\|fail\|pending\|none\|null, commit_subject, lane, files, verify: {id}\|null}`. `queued[]`: `{run: {id, name, task}, ref, text, title, retry_at}`. `summaries[]`: finished groups of the last 7 days `{run, name, state, finished_at, text_md}` |

**Events** (on `/api/events`, emitted only by the run driver): `run.changed`
`{run, state, counts}` (refetch; no notification) — or, when an auto-split's
lead declines, `{run, state: "dissolved", counts: {}, lead, by, why}` (the run
no longer exists; the web UI toasts it, skipping replayed events); `run.needs_you` `{run,
name, task, title, ref, reason, text, incarnation, key, detail}` once per (run,
task, reason, incarnation) — `key` is that announce key (per round for a
group's own asks), which the bell dedupes on — never for a dialog. The boot
reconcile seeds only what was already standing; what it finds (a member gone
after a restart) is announced once after the boot quiet window; `run.task_shipped` `{run,
name, task, title, ref, pr_url, incarnation, detail}`; `run.finished` `{run,
name, shipped, failed, cost_usd, duration_s, grouping, outcome, detail}` once,
across restarts (`outcome`: what the group did, e.g. "one PR opened").
Notify rules `run_needs_you` and `run_finished` are on by default,
`run_task_shipped` off. A one-for-all group also says `run.needs_you` once per
round for its own asks — `reason` `plan` (the lead proposed pieces),
`release` (one PR is ready to open), `check_failed` and `lead_gone` — with `session` and
`title` the lead and `task` `""`; its members are not announced as shipped.

RunDTO for a one-for-all group or a split adds `split`, `optional` (an
auto-split the lead may decline), `max_pieces` (the user's cap; `0` = the
server limit), `goal`, `mode`
(`""` until the plan is approved, then `worktrees` | `same_folder`),
`origin` (null, or the session a new lead was started from: `{title, branch,
head, in_place, trunk}`), `stray: {paths, commits}` (same folder), `lead:
{title, branch, base_branch, incarnation, adopted, in_place, trunk,
row_present}`, `plan:
{state, round, pieces, why, by, proposed_at, approved_at, base_sha, note}`,
`check: {state: pending|none|running|fixing|ok|failed|skipped, command, tests,
summary, sha, attempts, finished_at}` and `release: {state:
none|ready|releasing|done|handoff|failed, title, body, base, branch, lane,
pr_url, compare_url, head_sha, files, add, del, commits, conflict_fixes,
detail}`; its tasks add `head_sha`, `commits`, `conflict`, `conflict_fixed`,
`tests` and `merged_at`. Run states: `planning → plan_ready → running →
checking → release_ready → releasing → done`; lanes `leave`/`commit` end at
`done` right after the check.

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
| `GET /api/instances/{title}/find?pane=agent\|shell` | `{"mode": "tmux" \| "scroll" \| "overlay"}` — how Ctrl+F searches the pane, by what its mouse wheel scrolls: `tmux` when the app leaves the mouse to tmux (copy-mode search), `scroll` when the app grabbed the mouse and scrolls itself, `overlay` for an alternate screen without the mouse (or no live session) — the UI's history view then |
| `POST /api/instances/{title}/find` | One in-place find step, body `{pane, query, op, case?, word?, regex?, near?, within?}` (match case, whole word, regular expression, and a proximity term that must be within `within` lines — 0 = same line; an invalid pattern answers `{"status": "error"}`) with `op` = `prepare` (build the index now — the UI sends it as the bar opens; a no-op in tmux mode) / `search` (fresh query, lands on the newest hit at or above the reader's view) / `older` / `newer` / `close` (back to live) / `cancel` (stop a running step, never queued behind it). `tmux` mode returns `{"mode", "total", "index"}` (tmux scrolls and paints the hits). `scroll` mode indexes the app's own scrollback (a briefly tall window, swept by PageUp/PageDown or measured wheel bursts, only rows that move with the content) and jumps: `{"mode": "scroll", "status": "found"\|"none"\|"ready"\|"cancelled"\|"error", "total", "index", "row", "col", "region"}` — `row`/`col` the current hit on the screen, `region` the rows that scroll (the UI paints hits there); `prepare` on an unchanged pane answers `"cached": true`. Hits carry `len` and `spans` (the hit and its proximity partner) for painting |

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
| GET | `/api/recent?sizes=1&days=7` | The **Recently closed** page — closed sessions AND what is left on disk, in one list (this is what the UI reads; the two used to be separate dialogs). `{rows: [{id, source, title, branch, name, path, folder, kind, worktree, in_place, provisioned, exists, closed_at, mtime, last_used, size_bytes, active_session, stale}], stale_days, hidden: {protected, protected_names, protected_bytes, active, active_titles}, roots}`. `source` is `closed` (a reopen target from the undo store, id = its entry id) or `disk` (a directory no closed entry accounts for, id = `disk:<path>`). `worktree` is true only for a directory git generated as a *linked* worktree; `last_used` is epoch seconds, the max of the dir's mtime and the worktree's own git index/HEAD/reflog; `stale` marks the rows the sweep below would take, at `days` (default 7). NOT rows, but counted in `hidden`: protected base clones / cache refreshers, and anything a **live** session is working in (that one is in the sidebar). `size_bytes` and `protected_bytes` are computed only with `?sizes=1` (a `du` per row) |
| POST | `/api/workspaces/prune-worktrees` | **Remove unused worktrees — and nothing else.** Body `{days=7, dry_run=true, include_dirty=false}`. A candidate must pass every rule: strictly under MindFlock's own worktrees root; its `.git` is a gitdir **FILE** whose pointer runs through the repo's `worktrees/` dir and carries git's back-pointer to it — i.e. a linked worktree `git worktree add` generated, never a `--separate-git-dir` repository or a submodule checkout, which also have a `gitdir:` file (a repository, a clone, a `_base_*` mirror, a `pr-*` review clone and any plain folder are all excluded by that one test, re-checked immediately before each delete); not a mount point; no live session using or sharing it; and nothing has touched it for `days`. The candidate set is always recomputed **server-side** — no path a client sends is ever deleted. `dry_run` (the default) returns `{candidates: [{name, path, branch, size_bytes, last_used, dirty, titles}], candidate_count, total_bytes, dirty_count, kept: {active, recent, not_worktree, outside_root, protected}}` without touching anything. A real run deletes the **clean** candidates only, unless `include_dirty` — `dirty` means the directory holds something the base repo has never seen (uncommitted or untracked changes, or a detached HEAD whose commit no ref contains — deliberately NOT git-ignored content, which every worktree has, and which the confirmation says goes with the directory); a worktree's branch and commits live in the repo it came from and survive. It also forgets the closed entries whose worktree it removed, prunes each stale registration from the repo the worktree's own gitdir names, and collects the empty branch-slug dirs left behind → `+{removed, removed_count, failed, forgot, kept_dirty, freed_bytes, empty_dirs_removed}`. 400 on a negative or non-numeric `days` |
| GET | `/api/workspaces?sizes=1` | The raw disk listing (`/api/recent` is the filtered, annotated one the UI reads): `{workspaces: [{name, path, root, kind, worktree, gitdir, size_bytes, mtime, last_used, active_session}], roots}`; `kind` ∈ `base·refresher·pr·tmp·worktree·workspace` (`size_bytes` computed only with `?sizes=1`). **`last_used` is the max of the directory's mtime and the worktree's own git index / HEAD / reflog mtimes** — a directory whose root was merely touched is not thereby "used", and a worktree worked in through git shows it even when the root's mtime never moved. That definition is what `stale` and the sweep are computed from, so it is the number to reason about, not `mtime` |
| POST | `/api/workspaces/delete` | Body `{path}`. Must be under a managed root **and** a directory this server lists as a workspace — a flat child of a provisioning root, or a worktree leaf; a deeper path (e.g. inside a base clone) is refused, which is the "direct child" rule this row always claimed. Cache refreshers always protected. A base clone (`_base_*`) is deletable ONLY when nothing references it — no attached worktrees and no active session based on it; otherwise 400 naming the holders. Kills any active session on the dir first. Prunes the stale registration from the repo named by the worktree's own `gitdir:` pointer (which is what makes it work for a *nested* worktree path). |
| POST | `/api/workspaces/clear` | **Bulk reclaim, no UI surface** — retained for API compatibility only, and superseded by `prune-worktrees` above, which is the one the Recently-closed page offers and the only one of the two that obeys "never a repo". Prefer that route: the two rows are adjacent but not equivalent, because this one cannot tell a linked worktree from a clone. Sweep every managed root and delete each workspace in one pass → `{ok, removed_count, removed: [name…], kept_active: [title…]}`. Only **unprotected, idle** dirs go: protected shared infra (base clones + cache refreshers) and any dir a **live** session is using are skipped (the latter listed in `kept_active`). Note it does NOT distinguish a linked worktree from a clone, so a `pr-*` or clone-strategy workspace under a managed root goes too. Unlike per-row delete it never kills a running agent. Also GCs each removed worktree's `~/.claude.json` trust entry and prunes stale worktree registrations from base clones. |
| GET | `/api/recently-closed` | `[{id, title, branch, folder, in_place, provisioned, closed_at, exists}]` — the undo store itself (Ctrl+Z / Ctrl+Shift+T and the Verify dialog's closed-session targets read this; the page reads `/api/recent`) |
| POST | `/api/recently-closed/{id}/reopen` | Recreate a session on the preserved worktree (410 if the dir is gone) |
| POST | `/api/recently-closed/{id}/forget` | Body `{wipe}` — drop the entry, optionally delete the worktree. **409** when a still-running session shares that directory (a copy and its origin keep one worktree); an in-place session's folder is the user's own repo and is never deleted |

## Remote devices (tailnet)

Multi-device control (gated by the `general.remote_control` setting): other
MindFlock servers on your tailnet appear as device groups in the sidebar, their
sessions namespaced `<device>::<title>`, and every per-session route proxies to
the owning device. Sessions are equal wherever they run: New Session's **Runs
on** picker (shown once a second device is connected) starts one on any
connected device through the forward below.

Routing is **one hop**: a remote-flagged `GET /api/instances` (header
`X-MindFlock-Remote`) lists only that device's own sessions, never the ones it
mirrors, and a remote-flagged request is never proxied onward (400) — so two
devices paired both ways don't echo each other's sessions back as
`a::b::title`.

| Method | Path | Behavior |
|---|---|---|
| GET | `/api/remote/hello` | Identity/permission handshake target for other devices: `{app, version, device, host, remote_control, auth, shared_link}`. `shared_link` is the Tailscale Service name this device answers the shared phone link on (`""` for none). `/api/devices` echoes it per device, and `/m` reads `device` to resolve `<device>::<title>` deep links |
| GET | `/api/devices` | Tailnet devices running MindFlock + their connection state |
| POST | `/api/devices/{device}/connect` | Pair with a device (token exchange, persisted in `~/.mindflock/remote_devices.json`) |
| POST | `/api/devices/{device}/disconnect` | Drop the pairing |
| * | `/api/devices/{device}/fwd/<path>` | Forward to that device with its stored token (502 when not connected). Allow-listed to what New Session asks: `GET /api/config`, `/api/settings`, `/api/templates`, `/api/providers`, `/api/providers/manage`, `/api/repos/suggest`, `/api/repos/search`, `/api/repos/check`, `/api/browse`; `POST /api/mkdir`, `/api/session-plan`, `/api/instances` — anything else 404s. A forwarded create refreshes that device's session list before answering, so the next `GET /api/instances` already carries `<device>::<title>` |

## Config, providers, usage, settings

| Method | Path | Returns / accepts |
|---|---|---|
| GET | `/api/config` | `{default_program, provisioning_available, caps: {git, tailscale, ticketing, github, agent_mcp, team_runs}, home, repo_root, ide_name, onboarded, auth_mode, auth_enabled, fasttrack_depth, fasttrack_default}` — `fasttrack_default` is THE fast-track default batches start on — a list in New, Intake's Start together, and `POST /api/runs` with no `policy.lane` (Settings → Workspace "Fast-track goes as far as": a rung, or `off`, which is also what UNSET reads as; a single new session always starts Off and never reads it); `fasttrack_depth` is the rung an explicit depth-less `POST /fast-track` arms (never `off` — unset or Off reads as `pr` there, an explicit-arm convenience only). `caps` reports which optional integrations are usable right now; the UI hides absent features and shows "connect X" guidance wherever they are configured (a Settings screen, or an Intake tab via its `data-caps-need`). `caps.github` is true when **either** credential exists (`gh` authenticated **or** a token resolves) and is cached ~60 s (unlike its PATH-stat siblings it shells out to `gh auth status`, and this endpoint is hit on every page load); it gates one-click **Make PR** / **Merge**, and when false those buttons take the browser-URL path rather than disappearing — pushing is unaffected either way. `caps.agent_mcp` is the one non-boolean cap: `{"enabled": bool, "providers": ["claude", "codex"]}`. `enabled` says whether new launches attach the MindFlock MCP (`general.agent_mcp` and the `MINDFLOCK_AGENT_MCP` kill switch). `providers` lists the CLIs that get it automatically. On any error it reads `{enabled: false, providers: []}`. The MCP's `spawn_session` reads it to decide whether a worker can report back (see [mcp.md](mcp.md)). `caps.team_runs` is `{"split": bool, "together": bool, "max_pieces": int}` — `split` / `together` say which team-run shapes `POST /api/runs` accepts yet (both `false` until splits and one-for-all land; the create refuses them with a 400 off the same table); `max_pieces` (an int ≥ 2) is the most pieces a split may have (`MINDFLOCK_MAX_CHILDREN`, the cap on New's "Auto-split into up to N"). The UI shows the One-for-all PR choice, the split box and the Split into parallel pieces… action disabled with "coming next" while they are false |
| GET | `/api/providers` | `{providers: [{name, aliases, profiles: [{id, label}], default_selector}], default}` |
| GET | `/api/usage` | Rolling day/week/month/year token+cost totals per provider (Claude, Codex, …) |
| GET/POST | `/api/scroll-speed` | `{speed}` 1–20, applied live to tmux |
| GET/POST | `/api/window-refresh` | The scheduled coding-CLI keepalive: config + per-provider `last_fired` (Settings → Agent CLI) |
| GET | `/api/repos/suggest` | `{suggestions, home}` — folders to offer instead of a bare tree: the recency ladder (`general.last_repo_path` → live sessions newest-touched → the closed-session undo store), the folder the server was launched from, then a shallow sweep of the usual code directories. `POST /api/session-plan` builds its numbered folder menu from the **same** `server._recent_repo_paths` ladder (plus up to three name lookups drawn from the sentence), so the dialog's suggestion chips and a plan can never offer different folders |
| GET | `/api/browse?path=` | Directory listing `{path, parent, is_git, entries}` for the New-session dialog |
| POST | `/api/mkdir` | Body `{path, name}` (single path segment) → `{path}` |
| POST | `/api/paste-image?session=&name=` | Save a file pasted or dropped in the browser (raw bytes as the body) → `{path}`, the **absolute** path the UI then types into the PTY. `?session=<title>` stores it in that session's workspace under `.mindflock_pastes/` (git-excluded, so the agent needs no out-of-tree read); **omitting** `session` — the Assistant window, which has no worktree, and the phone UI — stores it under `~/.mindflock/pastes`. `?name=` keeps a sanitized copy of the original filename so the agent sees `report.pdf` rather than a blob; without it the extension comes from the content type (`image/png|jpeg|gif|webp|bmp`, else `.png` for any other `image/*`, else `.bin`). 400 on an empty body, 413 over 20 MB. Transient: each write prunes its own directory to the newest few pastes |
| GET | `/api/logs` | Tail of the server log (the UI's System-logs pane, 3 s poll) |
| GET | `/api/addons` | Addon manifests `{addons: [{id, label, managed, frontend}]}` |
| GET | `/api/doctor` | Dependency preflight: git/tmux/agent-CLI/uv checks with per-platform fixes, plus `gh` reported as **optional** (status `info`, detail "not found (optional — only PR create/merge and PR review need it; pushing uses plain git)" — never `fail`, so it can't trip the required-dependency exit); cached ~30 s, `?refresh=1` re-probes. Also carries `version` (the running engine's version) and `state_notice` |
| POST | `/api/doctor/ack-state-notice` | Dismiss the downgrade notice; clears it and the cached payload |
| GET | `/api/mobile` | Mobile URLs + QR payload (Settings → Mobile): `{urls, qr_target, qr_svg, token, local_only, serve_mode, note, shared}`. `shared` is the shared phone link (`general.shared_link`): `{enabled: false}`, or `{enabled, name, service, url, advertised, approved, tagged, error, devices}`. Once `advertised`, the shared URL leads `urls` (this device's own URLs follow, labelled **This device**) and is what `qr_target` encodes, with one `token=` per device: this one's plus every paired device's. Saving `general.shared_link` through `POST /api/settings` validates the name (one DNS label, `svc:` prefix optional; 400 otherwise), applies it immediately and returns `shared_link` (the same object) beside `settings` |
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
usage_restored|turn_ended|message|pr_state_changed|pr_review_changed|test_plan_ready|
test_plan_failed|test_plan_due|test_plan_checked|test_plan_gave_up|
red_zone_blocked|red_zone_breached|red_zone_tampered`
(plus addon-originated `addon.*`). `session.pr_review_changed` reports a
*reviewer's* verdict on the branch's open PR (`"" → approved →
changes_requested`), read off the PR's review list rather than from
`mergeable_state`, which cannot express "approved"; like `pr_state_changed` it
is seeded silently on first sight and an empty verdict never counts as a
transition, so a rate limit or an expired token can never read as a withdrawn
approval. `session.budget_exceeded` (J5) fires when a
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
**finished** (`data: {"idle_for": <float>}`) — see below.
`session.message` fires on the **recipient** whenever a message is left for it
([Inter-agent messages](#inter-agent-messages)). Its `data` is `{id, from,
kind, text, delivery, status?}`. `from` is `""` for the CLI or an external client.
`text` is the first 200 characters, flattened to one line. `delivery` is
`delivered`, `pending` or `held`: typed in already, waiting for the agent to
be idle, or kept in the inbox. A `kind: "result"` event also carries `status`
when the report has one: its `data.status` (`done`, `blocked` or `failed` from
`report_result`), sanitized and cut to 24 characters, so the UI can say how a
worker finished without fetching the message. The three
`session.red_zone_*` events (see [Code map & red zones](#code-map--red-zones))
carry `data.detail`, a sentence for humans that notification templates fill as
`{detail}`: `red_zone_blocked` `{count, zone_ids, patterns, paths, tool,
push, kind, detail}` (`push: true` = a refused `git push` / `gh pr` /
GitHub-MCP write, "blocked a push (zone breaches committed on this branch)";
`kind: "green"` = edits outside the green scope, one per work cycle whatever
the file: "blocked 2 edits outside the green zone(s): a.py, b.py"),
`red_zone_breached` `{paths, patterns, total, blocks_push, kind, detail}`
(`patterns` holds `"outside green"` for a green breach; `detail` ends with
what the breach blocks — only a committed change blocks a push),
`red_zone_tampered` `{what: "guard"|"hooks"|"store", detail}`. They are seeded
rather than boot-gated. The notification-center
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
work by the agent was *corroborated* in its current tmux incarnation, it has been
idle continuously for the evidence-tiered dwell (next paragraph), and no queued
prompt is waiting to wake it. It is emitted once per cycle of observed work — the evidence is
spent on emit and re-earned by the next armed `working` reading — so a session
left idle overnight announces itself once.

The dwell is **tiered by the evidence's strength** — `agent_state.work_evidence`
— because it only has to buy the confidence the evidence lacks: 12s when the
CLI's own hook report both armed the work AND delivered the idle (the fast lane
requires the END to be authoritative too — marker-armed work whose idle came
from a pane misread takes the slow lane outright), 25s for status-line-armed
work (pane CLIs), 45s for the CPU backstop. The hazards the old flat 45s
blanketed are now **exact gates**: recent human input (both terminal
websockets, `/send`, send-now — plus `tmux list-clients` client activity at
announce time, for people attached to tmux directly), a queue send-grace
(`peek_next` reads None the instant the last prompt is *popped*, which is at
tmux-typing time — the drain's own `armed`/`sent_at` record covers the
in-flight window), and fast-track (held exactly while the autopilot record
reads `running` *and its driver lease is live* — a wedged chain the driver
cannot step goes lease-stale and announcements resume). The working→idle
marker transition also forces a **fresh limit-screen probe** (a forced miss
never refreshes the probe throttle, so a banner that paints late is caught by
the next tick's re-probe), closing the old 15s probe-cache blind window.

An **authoritative idle/clarify skips the 3s activity settle** entirely — a
hook marker cannot misread a frame, so the chip and the default-on
"needs your input" push react within one tick. `limit` never skips: both
marker-branch limit reclassifications are pane captures, and one frame can
misread scrollback or quoted output — so the default-on "ran out of usage"
push keeps its two-sighting guard whatever triggered the reading.

*Corroborated*, because a `working` reading is not by itself evidence of a turn.
A reading paints the chip; only some readings may interrupt a human. The ladder
(`agent_state._verdict`'s `arms` argument):

- the **CLI's own report** — a fresh hook marker or live agent query — arms at
  any duration. A hook fires because a prompt was submitted or a tool ran;
- the provider's **live-turn status line** (Claude's `esc to interrupt`, a
  climbing token counter) arms too: it is on screen *because* a turn is running,
  which covers a marker that went stale mid-turn;
- a **busy process tree alone does not**. A parked Claude session's own
  auto-updater crossed `_CPU_ACTIVE_JIFFIES_PER_S` for one 4s poll, read as
  `working` for ~12s — past the settle above — and 45s later announced a turn
  its transcript shows never happened. `/clear`, a GC pause and a compile all
  cross that line as well;
- a CLI that does **not** report for itself keeps a CPU backstop: an unbroken
  busy run of `_CPU_ONLY_ARMS_AFTER_S` (20s) arms, which no single spike
  survives (`hard_since`/`proof` are cleared by EVERY non-working reading,
  whatever layer delivered it, so a run can never straddle an agent death, a
  Stop hook, or a relaunch). Such a provider's only other signal is a
  status-line regex, and a regex can be wrong — losing the announcement to a
  reworded hint would be worse than the spike the strict rule guards against.
  No backstop where the CLI's hooks are actually *speaking* (a marker was read
  this poll): those have two independent signals already, and the phantom this
  ladder exists to kill was one of theirs. A CLI that merely *declares* hooks
  while its marker never appears (an older codex build, a failed install)
  keeps the backstop — declared capability is not observed capability.

Of the bundled providers, Claude and Codex report through hooks; aider,
antigravity, cline, goose and opencode are covered by their status line, with
the CPU backstop behind it. `test_provider_activity_patterns.py` pins that
roster, so a provider added with no signal at all shows up as a failing test
rather than a silent notification. The backstop's 45s is chosen to sit above
every other idle dwell in the app (`_QUEUE_IDLE_SETTLE` 12s,
`autopilot.IDLE_SETTLE_S` 30s), so the queue drains and a fast-track chain
decides the agent is done *before* anyone is told the work finished; the
faster tiers don't need that ordering, because the send-grace and fast-track
gates above check those hazards exactly instead of outwaiting them.

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
| GET | `/api/assistant/state` | `{"activity": "working\|clarify\|limit\|idle\|offline"}` — what the Assistant's agent is doing, for the window's sidebar pill. **Polled, not pushed**: `WS /api/events` speaks for sessions the *engine* owns, and the Assistant is a window, not one of them. `offline` covers both "never started" and "it died", which is what the row should say before its first chat. The read goes through the same `_agent_activity` ladder and the same ~2.5 s probe memo a session row uses (the addon hands it a singleton stand-in shaped like an instance), so the two can never disagree about what "running" means |
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
| GET/PUT | `/api/settings/auth-profiles` | The auth-profiles list (multiple Claude accounts / OpenRouter keys — see [accounts.md](accounts.md)) plus `default_profile` and the `kinds` catalog. Same masked round-trip as the ticketing sources: `api_key` reads back as `•••set`, and a PUT that sends `""`/the mask keeps the stored key (matched by `id` — so **renaming** an id counts as a new profile and must re-send the real key; a key-kind profile that would land keyless is a 400, not a silent no-auth store). PUT validates everything **before writing anything** (a 400 always means nothing changed): ids (slug, unique), kinds, and a body `default_profile` against the incoming list. `account`-kind profiles get their isolated config dir created (0700). GET reports an env-pinned default as `default_profile` with `default_profile_env` + `default_profile_locked: true` when `$MINDFLOCK_AUTH_PROFILE` is set in the server's environment (it wins over the stored value at launch, so reporting the stored one would have the screen name one identity while every session runs as another). GET also derives `resolved_config_dir`, `login_command` and `supported_agents` per profile for the Settings → Accounts cards and the New dialog's agent steering. Removing a profile that live sessions are **pinned** to is a **409** naming them (`{error, in_use: [title]}`) — they would fall back to the CLI's own login without being told; resend with `force: true` to proceed anyway. Sessions that merely *inherit* the app default never block a removal |
| POST | `/api/settings/test/openrouter` | Validate an OpenRouter key (body `{api_key}` or `{profile_id}` for the stored one, optional `base_url`) → `{ok, label, usage, limit, models, error}` — the key's real spend from OpenRouter's `/key` plus the model list that turns the profile's model field into a picker. Always 200; branch on `ok` |
| GET | `/api/providers/manage` | Custom coding-CLI providers (user TOMLs) for the Settings CRUD screen. Each entry carries `plan_supported` — whether the Map can show the CLI's plan and send it Go (the New Session "Plan first" box is offered only then) |
| POST/PUT/DELETE | `/api/providers` · `/api/providers/{name}` | Create / update / delete a custom provider TOML. The body may carry `launch_args` (a list of saved flag tokens) alongside `resume_flag`/`skip_perms_flag`/`trust_patterns`/…; it is validated (400 on invalid) and all string values are TOML-escaped via `json.dumps`, so quotes in names/flags/patterns can't corrupt the file. |
| GET | `/api/providers/status` | Per-provider connection status → `{providers: [{name, aliases, binary, installed, path, authenticated, auth_detail, auth_known, login_command, install_hint, is_default}], default}`. The catch-all `generic` provider is omitted. This is the source for the Settings → **Agent CLI** default-provider picker — it reads `installed`/`path` to list only installed CLIs and self-correct a missing default. The `authenticated`/`auth_detail`/`auth_known`/`login_command` fields are still returned but **no longer read by the UI** (sign-in is delegated to each CLI; see [providers.md](providers.md)). |
| WS | `/api/providers/{name}/login-terminal` | **Unused by the UI.** PTY↔websocket bridge to a throwaway tmux session running the provider's login flow in `$HOME`. No frontend surfaces the one-click login any more (each CLI prompts for sign-in itself), but the bridge stays served and now takes `?profile=<id>` to run the login under an auth profile's isolation env (the credential lands in that account's config dir — see [accounts.md](accounts.md)). Closes 4500 with an `{type:"error"}` frame for an unknown provider/profile or a spawn failure. |
| POST | `/api/providers/{name}/login-close` | **Unused by the UI.** Best-effort teardown of a login session (`?profile=<id>` matches the terminal above). Always `200 {ok: true}`. |

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
- **Several devices, one origin.** The shared phone link is one hostname
  answered by any of your devices, each with its own token. So every sign-in
  sets `mf_auth_<12 hex of sha256(token)>` beside the plain `mf_auth`, and the
  gate accepts the request when any `mf_auth*` cookie (up to 16) is this
  server's token. `?token=` may repeat (the shared QR carries one per paired
  device). The request passes if one of them is this server's, and the
  redirect then stores all of them, each held to `[A-Za-z0-9_-]{8,256}`. A
  server never accepts a token that isn't its own.

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
  headers are answered — a public domain rebound to 127.0.0.1 gets 403. The
  one exception is the shared phone link's service hostname
  (`<name>.<tailnet>.ts.net`), once this server advertises it. `tailscale
  serve` keeps the original `Host` on the request it forwards to 127.0.0.1,
  and only the tailnet resolves that name.

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

### Engine updates

Three routes let the **server update itself** — `uv tool install --force` over
the very tool venv this process is running out of, then the same re-exec
`POST /api/server/restart` performs. The
desktop shell keeps its own updater (`electron/main.js`'s `engine:install`);
these are for every *other* client — a browser on the tailnet, `/m`, a second
machine — which could otherwise be told it was behind and do nothing about it.
Implementation and the reasoning live in `web/core/self_update.py`.

| Method | Path | Returns / accepts |
|---|---|---|
| GET | `/api/update/check` | `{current, latest, tag, release_url, notes, checked, available, kind, blocked, repo, state}`. `?refresh=1` bypasses the 15-minute `RELEASE_TTL_S` cache (the **Check again** button) — the releases endpoint is polled by every open settings screen and GitHub's unauthenticated limit is 60/hour. An unreachable GitHub answers an empty `latest` with `checked: false`, and is **never** reported as up-to-date: "couldn't tell" and "you're current" are different answers. `kind` is how this engine is installed (`uv-tool` \| `editable` \| `other`) and `blocked` is the human sentence for why it can't update here (empty = it can) |
| POST | `/api/update/start` | Body `{}` (or `{"ref": "v0.3.2"}`). The newest tag is resolved **server-side** by default — the button says "update to the newest version", and a stale settings screen doesn't get to decide what that is — then resolved to a commit and handed to `uv tool install --force`. → `{ok: true, ref, commit}`. **400** is a refusal with a reason: a dev/editable checkout, an engine not installed by `uv tool`, no `uv` on PATH, an install already running, a ref that resolves to nothing. **502** = GitHub unreachable, so there is no newest release to install |
| GET | `/api/update/state` | The progress file plus `restarting` (and a `log` tail for the UI's detail fold). **This route is also what re-execs the server** — exactly once, on the first poll that sees a finished install. The installer deliberately doesn't do it itself: calling back into the API would mean teaching a shell script the port and the auth token for a request the UI is already making |

The operational contract behind them:

- The installer runs **detached** (its own session via `setsid` where there is
  one), so it survives the restart that ends the update rather than being killed
  halfway through replacing its own venv.
- Progress lives in a **file**, `<config dir>/update.json`, not this process's
  memory — so a client polling *across* the restart still learns how the update
  ended. Full installer output goes to `<config dir>/update.log` (Settings →
  System logs).
- `INSTALL_TIMEOUT_S` is **30 minutes**, after which a `started` marker whose
  process is gone is aged out rather than disabling the button for ever.
- `MINDFLOCK_UPDATE_REPO` (`owner/repo`, releases) and `MINDFLOCK_INSTALL_REPO`
  (clone URL, the install source) override where both halves come from, for a
  fork or a staging repo — the same names `install.sh` and the desktop shell
  already honor.
- A **dev checkout is refused outright**, not attempted and failed:
  `uv tool install --force` would replace a contributor's editable install with
  a release build, and no button in a settings screen should be able to do that
  quietly.

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
