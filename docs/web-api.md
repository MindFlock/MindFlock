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
  "git_repo": true,           // its folder is in a git repo (always for a worktree; an in-place folder or a parent has .git) — a filesystem look, no git call
  "parent": "",               // lineage: the live session that spawned/adopted it ("" = a root)
  "spawned": false,           // true when an agent (not a human) created it
  "playbook": "",             // the playbook it was created with ("split"), "" = none
  "hidden_by": "",            // feature that owns it and keeps it off the rail ("verify"), "" = the user's work
  "person": "<uuid>",         // the person it was created for (docs/people.md), "" = predates people / a peer-link session
  "shared_branch": false,     // works ON a pushed branch others push to too (created with continue_branch)
  "behind": 0,                // commits the remote branch has that HEAD lacks, from the last fetch (never fetched per poll)
  "owner": "<uuid>",          // who it belongs to: `person` unless POST /owner changed it ("" = nobody known)
  "driver": "<uuid>",         // who holds its keyboard, "" = nobody (docs/people.md#owner-driver-viewers)
  "driver_name": "Ada",       // the driver in a sentence (name, else their device, else "Someone"), "" = nobody
  "viewers": {"<uuid>": {"name": "Ada", "device": "laptop", "devices": ["laptop"], "since": 1759600000.0, "typing": false}},
  "lane": {"target": "pr", "ask_first": false, "owner": "sc-19815"},  // ship lane, or null
  "run": {"id": "r_8f3k2m", "name": "Q4 payments", "task": "t3", "role": "task", "grouping": "each"},  // or null
  "created_at": 1759600000.0, // epoch the session record was created, or null
  "stage": "provisioning|agent|precommit|interrupt|committed|pushed|pr|merged",
  "pr_url": "…",              // when a PR exists
  "failed_step": "…",         // when stage == "interrupt" (pre-commit ✗)
  "stage_reset": false,       // ↺ "back to idle" pin — show the ladder from the start
  "activity": "working|clarify|idle|offline",
  "activity_since": 1756200000.0,  // epoch the reported activity last changed; 0 = no live reading
  "background": [              // jobs the agent backgrounded and left running past its turn. Null on the
                               // cheap/placeholder snapshot, on non-Claude and non-Linux sessions, and
                               // whenever live and hook signals agree, so null does NOT mean "no jobs"
    {"id": "b8o2q11hd", "pid": 3290210, "command": "until ! pgrep -f …", "state": "running|waiting|stuck",
     "reason": "…", "started": 1791902665.0, "quiet_s": 2216.0,
     "expect_s": 21600.0,        // declared expected duration (s), or null
     "expected_by": "you|agent|command",  // who declared it (you > agent > command), or null
     "eta": 1791924265.0,        // started + expect_s, or null
     "longest_gap_s": 2460.0,    // longest quiet stretch it has RESUMED from (learned; 0 after a restart)
     "threshold_s": 32400.0,     // stuck after this much quiet: max(900, 2 × longest_gap_s, 1.5 × expect_s)
     "past_expected": false}     // more than expect_s since it started (only with an expectation)
  ],                             // reason: "2h 10m of ~6h expected" · "quiet 22m — it has resumed after 41m before"
                                 // · "expected ~6h, now 9h 10m in and quiet for 3h" · "no CPU or output for 15m"
  "tokens": 0, "tokens_in": 0, "tokens_cache_read": 0, "tokens_cache_write": 0,
  "tokens_ctx": 0, "tokens_ctx_window": 200000, "tokens_cost": 0.0,
  "tokens_model": "…",
  "diff_stat": {"files": 2, "additions": 42, "deletions": 7},  // or null
  "workspace_missing": false,  // L1(c): started but its directory vanished
  "sleeping": null,            // or {"since", "reason": "idle"|"memory"|"manual", "freed_mb"}: agent asleep
  "has_origin": true,          // L2: workspace has an `origin` remote (cached ~30s)
  "last_turn": "…",            // L3: ≤120-char snippet of the latest agent turn, or null
  "last_say": "…",             // the spoken line of the newest reply (its `Say:` line, else a digest), ≤ 320 chars, or null
  "voice": false,              // voice mode is on for this session (a phone is talking to it)
  "asking": null,              // or {"kind": "dialog"|"question", "text", "ts", "dialog_id", "parsed", "source"}: it is asking you something
  "task_headline": "Fix pin bar drawing below terminal",  // newest MEANINGFUL prompt as a verb phrase, or null
  "task_headline_prompt": "can you please fix the bug where…",  // the prompt task_headline was read from, or null
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
MindFlock can take up to ~45 s to advance the badge and enable **Open PR**
(MindFlock-initiated pushes bypass the cache via a pending window). The base
branch used for stage/diff/PR is **per session**: the branch the worktree was
cut from, recorded at creation (`base_branch` in `state.json`); sessions that
predate the field resolve `origin/HEAD` → `main`/`master` → the configured
provision base (only when the session's repo IS the configured repo). Existing-PR
detection (what advances the chip past `pushed` to `pr`) looks up the PR by
**head branch only** and is intentionally base-agnostic — Open PR can target any
base (a configured default like `staging`, or one chosen in the dialog the
server never sees), so a base-scoped lookup would miss the real PR and wedge the
chip on `pushed`. The base still keys the "is there work to push" (`beyond`)
test; only the PR lookup drops the base filter.
`stage_reset` is the **↺ "back to idle"** pin
(`POST /api/instances/{title}/reset-stage`, below), published *alongside*
`stage` and never instead of it. The row's `stage` stays exactly what git says,
so the Fast-track driver, the verification-check kicker and every `*_changed`
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
[providers.md](providers.md); Claude calls a session `busy` while any
background job is alive, so when the Stop hook says the turn ended the
provider reads the jobs instead — see [Background jobs](providers.md#background-jobs))
— then CPU sampling of the pane's process tree
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

`sleeping` is set while the session's agent is asleep
([session-engine.md](session-engine.md#sleeping-agents)): its CLI and MCP
servers were stopped on purpose, idle, to free memory (`freed_mb` ≈ what they
held); `activity` reads `offline` meanwhile, but it is not a crash — the UI
paints it **Sleeping**, outside Offline, the bell and "Restart all". Opening
its terminal, `/send`, the queue, Send now, a mailbox message or `/wake`
wakes it, resuming the conversation. A **paused** session is never woken or
relaunched by any of these: its terminal socket closes with 4423, and `/send`
and Send now answer 409 `{paused: true}` — only `/resume` starts its agent.

`workspace_missing` (L1c) is `true` for a started, non-paused session whose
workspace directory no longer exists (deleted outside MindFlock). The UI renders
these as a muted row + placeholder pane with a single **Delete** action
(`DELETE /api/instances/{title}`, which tolerates the missing dir).
`has_origin` (L2) tells the UI to replace **Push** with setup guidance when the
workspace has no `origin` remote — pushing would only fail in the shell.
`last_turn` (L3) is a one-line, markdown-stripped snippet of the session's
latest conversational turn (provider-dependent; `null` when the provider
doesn't expose one) for at-a-glance triage across many sessions.
`last_say` is the session's **spoken line** — what the phone's voice mode
says when the turn ends: the `Say:` line the session wrote at the end of its
newest reply, else a digest of that reply (≤ 320 chars; `null` when there is
no reply). `voice` is whether voice mode is on for the session (one stat on
its flag file). Both are served on the cheap snapshot path too (`last_say`
carried over, `voice` read fresh). See
[Voice](#voice-talking-to-a-session-from-the-phone) for where the line comes
from and the routes that set the flag.
`asking` is the first-class **"it is asking you something"** signal
(`core/asking.py`), `null` when it is not, else one shape for both ways an
agent waits on a person:

* `kind: "dialog"` — the activity reads `clarify` (a dialog on screen, the
  CLI's hook, a waiting-prompt pattern). `text` is the dialog's question line
  (≤ 300 chars), `dialog_id` / `parsed` / `source` what
  [`GET /dialog`](#another-agents-view-output--answer) would answer, all read
  off the screen the activity probe already captured (parsed once per
  distinct screen, never a second capture); with no fresh capture they are
  `null` / `false` and a client fetches `/dialog`. `ts` is when the reading
  became `clarify`.
* `kind: "question"` — the agent asked in **plain prose** and its turn ended:
  the activity reads `idle`, the newest reply ENDS with a question, and no
  input has reached the session since. `text` is the question sentence(s) of
  the reply's final paragraph (≤ 300 chars; a `Say:` line decides when the
  reply has one; a `?` in an earlier paragraph or inside code does not
  count), `ts` the reply's stamp (the Stop hook's; else when it was first
  seen), `dialog_id: null`, `parsed: false`, `source` where the reply came
  from: `"hook"` (Claude's reply file), `"transcript"` (the provider's newest
  assistant message — Claude's transcript, Codex's rollout) or `"screen"`
  (the last text paragraph above the input prompt, any other CLI). Any input
  clears it — a `/send`, an `/answer` by a user, a keystroke in a web
  terminal (scroll-wheel included), a queued prompt going in — and so does
  [`POST /asking/dismiss`](#another-agents-view-output--answer); a newer
  reply that asks shows again. It is answered with a prompt (`/send`).

`asking` is a probe field (memoized ~2.5 s, carried over on the cheap
snapshot path). `notifications.ask_popup` (default on) says whether the UIs
pop it up mid-screen ([configuration](configuration.md));
`notifications.prose_questions` (default **off**) says whether a reply that
ends with a question in plain words counts at all — off, `asking` carries
only dialogs, no `session.question` fires, the outbox lists no question rows
and `/dialog` serves none; a real prompt (a permission, a plan approval,
Claude's question menu) is unaffected.
`task_headline` is what the session is working on as a short verb phrase
(~2–6 words, sentence case) — the sidebar row's task line. It is read from
`last_prompt_full` by fixed rules (`backend/task_headline.py`: no model, no
network): politeness and preambles are dropped, MindFlock's message envelope
is stripped, questions become *Investigate/Explain/Check …* and complaints
*Fix/Polish/Speed up …*. A phrase is only built when a rule matches cleanly;
otherwise the headline is the person's own words — the first sentence of the
ask, cleaned the same way and cut to 8 words with "…" — never a guessed
phrase. A prompt that carries no task — "continue", "yes", a
status ping, a bare "commit and push", MindFlock's revive prompt or its own
notes — does not replace it: the row keeps the last meaningful headline of
the same conversation (process memory, keyed by title + the window's
conversation id), else the headline of `task`, else that prompt's own
("Continue"). `null` when there is nothing at all. `task_headline_prompt` is
the prompt the headline was read from (the remembered one, or `task`) — the
row's tooltip, so a later "continue" never stands in for it; `last_prompt` /
`last_prompt_full` stay the newest prompt. Rows from a paired device that
predates the fields get both computed by the gateway from their
`last_prompt`.

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
`hidden_by` names the feature that owns the session and keeps it off the
session rail, the `Ctrl/Alt+1…9` window numbers and the prompt-target
pickers: `"verify"` for a Verify checklist run (`POST /api/test-plans/{id}/run`
creates its session with the flag), `""` for the user's own work. It is web
metadata (`backend/web/core/hidden_sessions.py`, persisted as
`session_hidden.json` in the config dir), not engine state, and it is dropped
with the session so a reused title starts unhidden. **The title is not the
signal**: a session someone named `verify-foo` has `hidden_by: ""` and is
listed like any other. Runs created before the field existed are flagged at
server start when a checklist still names them as one of its runs; a client
that receives a row with no `hidden_by` at all (an older server) falls back
to the `verify-` prefix.
Pending (not-yet-created intake) rows carry `parent: ""`, `spawned: false`,
`playbook: ""`, `created_at: null`. The cached fast path of this route recomputes `parent`
on every call, so a re-parent shows at once rather than on the next tick.
See [mcp.md](mcp.md#lineage-parents-workers-limits).

`lane` is the session's **Fast-track** target (the UI never says "lane";
the API does) — how far MindFlock carries it once
its agent is done (`target`: `leave` · `commit` · `push` · `pr` · `merge`),
whether it asks first (`ask_first`), and `owner`, the session that drives the
branch: a copy session on the same `(repo, branch)` shows its owner's fast-track and
is never armed itself. `null` = Off (nothing armed). The Fast-track driver
carries it out; see [the guided workflow](#guided-workflow-commit--push--pr--merge).
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
  "title": "sc-19815",          // optional: with story_id defaults to sc-<id>; else
                                //   named from "prompt" (add-readme-section-about-setup,
                                //   -2 when taken), else "untitled" / "untitled-N"
  "program": "claude",          // optional; default from ~/.mindflock/config.json
  "repo_path": "/path/to/repo", // base repo (plain sessions; optional for provisioned)
  "in_place": false,            // run directly in repo_path, no worktree
  "init_repo": false,           // git init + initial commit if needed
  "create_folder": false,       // make repo_path when it doesn't exist (strict true)
  "provisioned": true,          // provision a fully-loaded workspace
  "workspace_strategy": "worktree", // or "clone"
  "story_id": "19815",          // → branch feature/sc-19815/<slug>
  "prompt": "…ticket text…",    // seeds the agent on first launch
  "launch_args": ["--dangerously-skip-permissions"], // optional per-session flags
  "extra_launch_args": ["--model", "opus"], // optional: flags ADDED to the defaults
  "parent": "orch",             // optional: live session this one works for
  "spawned": true,              // optional: an agent created it (strict boolean)
  "hidden_by": "verify",        // optional: a feature's helper session, kept off the rail
  "base_ref": "3f2c9e1",        // optional: cut the worktree from this commit-ish
  "base_branch": "you/orch",    // optional, with base_ref: the recorded diff base
  "continue_branch": "feat",    // optional, with base_ref "origin/feat": work ON feat itself (shared)
  "playbook": "split"           // optional: "Split across workers" (see below)
}
```

A full slash-path in the title (e.g. `feature/sc-1/foo`) is used verbatim as the
branch. `provisioned` + `repo_path` provisions **that local repo** (setup
commands auto-detected, no shared cache seeds); `provisioned` without
`repo_path` requires the configured `[repository].url`. Errors: 400 (empty
title, bad strategy/config), 409 (title exists, a spawn limit, or a branch or
workspace a closed session still holds; see below).

**A folder is never created implicitly.** A `repo_path` that does not exist is
**400** `{"error": "No folder at <path>", "detail": "No folder at <path>",
"reason": "no_folder", "path": "<resolved path>"}` — nothing is made, nothing
is started — unless the body carries `"create_folder": true` (strictly the
boolean), which makes the folder (and, with `init_repo`, `git init`s it plus an
initial commit). `path` is the resolved absolute path the check used. The
folder becomes `general.last_repo_path` (the New dialog's first Recent folder)
only once the session has actually **started** — a create that fails in the
background is not remembered.

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

`hidden_by` (optional) marks the session as a feature's helper rather than
the user's work, which keeps it off the session rail, the window numbers and
the prompt-target pickers (the row's `hidden_by`, above). The only value is
`"verify"` — what `POST /api/test-plans/{id}/run` passes for a checklist run;
absent, `null` or `""` is an ordinary session. Anything else answers **400**
`hidden_by must be one of verify (got …)`, so a typo cannot quietly hide a
session. The flag is recorded under the claimed title once the create
succeeds and is never read from the title: `verify-…` is accepted as a name
like any other, and without the flag such a session stays listed. Refused for
a shared-folder session.

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
- **Memory guard.** A `spawned: true` create (not a team run's own member) is
  refused **409** while the machine's memory reads low or critical
  (`GET /api/system/memory`): `{error: "memory is low: … — new agent-spawned
  sessions wait until it recovers; …", memory: {…the reading…}}`. A person's
  create is never refused. It is checked before the split-across-workers
  validation; the `memory` key is what tells it apart from the depth / budget
  409s. A caller should wait for it to clear, not retry in a loop.
- `session.created` carries `parent` / `spawned: true` in its `data` when set.
- `/copy` and `/branch` never inherit either field.

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

**Continue a pushed branch** (`continue_branch`). With `base_ref:
"<remote>/<x>"` and `continue_branch: "x"` the session's worktree is checked
out ON `x`, tracking `<remote>/x`, instead of on a new branch — two people (or
two of your machines) on the same pushed branch. The remote is fetched first
(bounded, 20 s; offline is tolerated when the remote-tracking ref is already
known). A missing local `x` is created tracking `<remote>/x`; an existing one
gets that upstream if it has none and is fast-forwarded when strictly behind
(a diverged local branch is left alone — the push check below catches it).
The engine records `x` as the session's `branch` and diff base exactly as for
any existing branch, and never deletes it on cleanup. The row carries
`shared_branch: true` (web metadata, `core/shared_branch.py`).

| Request | Response |
|---|---|
| `continue_branch` with `provisioned`, or on an in-place session | **400** `continue_branch is only supported for plain worktree sessions (…)` |
| `base_ref` is not `<remote>/<x>` for a remote of the repo | **400** `continue_branch needs base_ref to be a remote-tracking branch (<remote>/x), got …` |
| `base_ref` names a different branch | **400** `continue_branch … does not match base_ref …` |
| the remote has no such branch after the fetch | **400** `<remote>/x does not exist on <remote>` |
| `x` is checked out in a worktree of this repo | **409** `x is already checked out in <path> — work in that session, or close it first` |

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
  `claude is not installed`, `codex did not answer within 30s`, `no installed CLI
  (aider) has a headless mode MindFlock can ask for a session plan`, `the CLI
  answered without a <newsession> block — nothing to read`, `the CLI didn't pick
  one of the folders — name the project you mean`, `the CLI picked folder 7, and
  there is no such folder in the list`, `the CLI echoed the example instead of
  reading what you typed`.

### Lifecycle

| Method | Path | Effect |
|---|---|---|
| DELETE | `/api/instances/{title}` | Kill session, remove worktree + branch → `{ok, title, wiped: true, children}`. **409** `{error: "folder in use", sessions: [titles]}` — and nothing changes — while another live session (a copy from Duplicate session) works in the same folder (real paths compared). Recorded in Recently closed like `/close` (listed with its folder gone). `children`: its child sessions, which keep running under *its* parent (or as roots) |
| POST | `/api/instances/{title}/close` | End tmux, **keep** worktree; recorded in recently-closed → `{ok, title, wiped: false, children}`. Never refused for a shared folder (keeping it endangers nobody) |
| POST | `/api/instances/{title}/cleanup` | Kill + permanently delete the workspace dir (+ close its Cursor window) → `{ok, title, wiped: true, children}`; the same **409** `folder in use` as DELETE; recorded in Recently closed |
| POST | `/api/instances/{title}/copy` → 202 | New in-place session `<title>-copy` sharing the same worktree (inherits the source's agent **and** auth profile) |
| POST | `/api/instances/{title}/branch` → 202 | **Branch session**: a new session `<title>-branch` (numbered `-branch-2`… past taken titles *and* taken branches) in its own worktree on a new branch `<branch_prefix><new title>` cut from the source's current `HEAD`, with a copy of the source's uncommitted changes (tracked edits, deletions, untracked non-ignored files) laid over it before its agent starts — unstaged; the source is untouched. Same agent CLI, the source's own `launch_args`, `profile_id` and `profile_model`; no parent; a fresh conversation. Created through the same path as `POST /api/instances` (`base_ref` = the source's HEAD sha, `base_branch` = its branch, so diffs and PRs compare against it). → the new row plus `branched_from`, `base_branch` (`""` when the source is detached) and `carried` (whether there were uncommitted changes). **409** `{error}`: a remote `device::title` row or a request relayed from another device; a shared-folder session (`peer_share: true`); a session still starting; a folder outside git (`"Branch needs a git repository — <folder> isn't one"`) or with no commit; a clone-strategy workspace; changes git couldn't snapshot. **404** unknown session. `session.created` carries `branched_from` |
| POST | `/api/instances/{title}/profile` | Hot-swap the session's auth profile. Body `{profile_id}` (`""` = inherit the global default, `"default"` = the CLI's own login) plus optional `profile_model` — **sending the key at all is what matters**: present sets this session's model override, absent keeps the current pin on a model-only no-op and *clears* it when the identity changes (a pin belongs to the catalog of the account it was picked from). Persists the pin and restarts the agent under the new identity; the worktree, shell pane and diff survive, and so does *that account's* conversation in this window — a thread belongs to the account that created it, so the marker is re-pointed at the incoming identity's own thread before the relaunch. → `{ok, profile_id, note, resumed}`, where `note` warns when the session's CLI has no route for the profile and `resumed` says whether the new identity had a conversation here to go back to (false = it starts fresh). Re-picking the identity and model already in force is a no-op that answers `{ok, unchanged: true}` **without** restarting the agent. 400 on an unknown id or a malformed model (nothing is mutated); 500 if the agent was killed and did not come back |
| POST | `/api/instances/{title}/parent` | Re-parent a session. Body `{"parent": "<title>"}` puts it under that live session; `{"parent": ""}` (or `null`) detaches it to a root. → **200** with the updated row. **404** for an unknown session. **400** for an unknown parent (`unknown parent session: X`), the session itself (`a session cannot be its own parent`), a parent that is the session's own descendant (`… that would make a cycle`), or a non-string `parent`. **409** when adopting would break a spawn limit: the new parent's live children (this one included) over `MINDFLOCK_MAX_CHILDREN`, or any session of the adopted subtree deeper than `MINDFLOCK_MAX_SPAWN_DEPTH` (the error names the knob). Detaching, and re-sending the current parent, are never limited. `spawned` is never touched. Persists |
| POST | `/api/instances/{title}/pause` | Pause: stop the agent (its tmux session and PTY are killed) and keep the worktree and branch **exactly as they are** — no commit, no removal; `status` becomes `paused`. 400 when the engine refuses (not started, already paused) |
| POST | `/api/instances/{title}/resume` | Resume a paused session: relaunch the agent in the same worktree. A session paused under the old scheme (worktree removed, branch kept) is rebuilt first |
| POST | `/api/instances/{title}/background/{pid}/stop` | Stop one **background job** the agent left running past its turn — the rail's **job stuck ✕** chip. Only a pid that is a *current* job root of this session (see `background` on the row and [providers.md](providers.md#background-jobs)) is signalled: SIGTERM to the job's process group, SIGKILL after 2 s. Claude Code then reports the job as ended, which may start a short turn. → `{title, pid, stopped: true}`. **404** unknown title; **409** `no such background job in this session` |
| POST | `/api/instances/{title}/jobs/{job_id}/expect` | Say how long one **background job** should take, so it is not called stuck while inside that window — the chip menu's **Expected to take…**. Body `{duration, by?}`: `duration` is `"90s"`, `"20m"`, `"6h"`, `"2d"`, `"1h30m"` or a number of seconds, 1 minute to 14 days; `by` is `"you"` (default) or `"agent"` (the MCP tool `expect_background_job`). `job_id` is the row's `background[].id` (the id Claude Code printed when it backgrounded the command). Stored in `~/.mindflock/job_expectations.json` (under `$MINDFLOCK_HOME` when set), pruned a day after the job is gone. Precedence: a person's expectation, then the agent's, then a `MINDFLOCK_EXPECT=<duration>` prefix in the command itself; an agent's write over a person's changes nothing and answers `ok: false` with theirs. The job's stuck threshold becomes `max(15 min, 2 × longest_gap_s, 1.5 × expect_s)`, and when it first passes `expect_s` the server emits `session.job_past_expected` once. → `{ok, title, job_id, expect_s, expected_by, eta, state, reason}`. **400** bad duration or `by`; **404** unknown title, or a job id that is not a current job of this session |
| DELETE | `/api/instances/{title}/jobs/{job_id}/expect` | Clear that expectation (a person's or the agent's; a command's own `MINDFLOCK_EXPECT=` still applies) → the same shape, `ok: false` when none was set. **404** unknown title or job |
| POST | `/api/instances/{title}/sleep` | Put the agent to sleep now ([session-engine.md](session-engine.md#sleeping-agents)): its CLI and MCP servers stop to free memory; the session, worktree and conversation stay. Every safety rule of the sleep sweep holds — idle with its turn ended, no dialog, no background job, nothing queued or waiting, not in a team run / fast-track chain / busy family, a CLI that can resume, its own workspace, no terminal attached — but not the timing ones. → `{title, sleeping: {since, reason: "manual", freed_mb}}`. **404** unknown; **409** `{error, veto}` (`veto` names the rule, e.g. `not idle`, `open in a terminal`, `prompts queued`) |
| POST | `/api/instances/{title}/wake` | Wake a sleeping agent — the rail's **Wake**: relaunch it, resuming its conversation, and open the prompt queue's boot grace (nothing is typed before its prompt is drawn). Opening its terminal or sending it anything does the same. → `{title, woke}` (`false` = it wasn't asleep, nothing done). **404** unknown; **409** with the relaunch's reason (`workspace no longer exists`, …) |
| POST | `/api/instances/{title}/revive` | Restart an **offline** agent — the rail chip's **Restart** action (the web UI's one session state, [web-ui.md](web-ui.md#one-session-state)). Runs the same lazy revive a terminal attach or `/send` does: a live agent tmux is reused, a dead one is relaunched (resuming the conversation after a crash, fresh after a clean quit). An agent that died mid-turn is also queued a *continue where you left off* at the front of its prompt queue ([session-engine.md](session-engine.md#after-a-restart-background-revive-and-nothing-dropped-silently)). Never held for memory. → `{title, revived: true, continued: bool, memory_warning}` (`memory_warning`: the one-line reason when memory is low, else `""`). **404** unknown; **409** `session is paused — resume it instead`, or the revive's own reason (`workspace no longer exists`, a shared folder the agent may not launch in) |

`DELETE`, `/close`, `/cleanup` and `/api/workspaces/delete` end a session
through one function, `core/session_end.py`'s `end_session(engine, title,
wipe=…)`: it stops the agent, keeps (`wipe=False`) or removes (`wipe=True`,
after the folder-in-use check) the worktree and branch, **always** records
the session in Recently closed, hands its children to its own parent (or
makes them roots) and reports them as `children`, then runs the teardown
below.

Every way a session leaves the registry runs one teardown,
`_on_session_removed`. That covers the four routes above, a failed
background start, and another MindFlock process deleting it. The teardown
does three things:

- it clears `parent` on any child still pointing at the session, so they
  become roots (the routes have already moved theirs up one step);
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
| POST | `/api/instances/{title}/commit` | Body `{message}`. Runs `git add -A` + `git commit` **in the session's shell tmux** (watch pre-commit hooks in the Terminal tab), retrying up to 5× when hooks auto-fix files. Works for every session type (plain, in-place, provisioned). Writes `.mindflock_commit_status` (exit code) and `.mindflock_commit_msg` (reused on empty re-commit). | **Zones warn a person's own window:** for a session not spawned by an agent (no `spawned`, no `parent`), a commit whose uncommitted changes (untracked included) fall inside a red zone or outside a green one answers **409** `{error, red_zone_breaches: [{path, pattern, zone_id, kind}], zone_warning: true}` without committing; re-POST with `proceed_anyway: true` to commit anyway. Agents' sessions are never warned here (the guard stops their edits).
| GET | `/api/instances/{title}/commit-message` | `{message}` — the message of a commit the pre-commit hooks blocked, so the Commit dialog can offer it back instead of making you retype it. Reads `.mindflock_commit_msg` (the file `git commit -F` uses), but **only when `.mindflock_commit_status` records a non-zero exit** — the same condition that raises the `interrupt` stage. Absent or successful status → `{"message": ""}`, since a committed message pre-filled into the next commit is worse than an empty box. 404 unknown title, 409 workspace not ready |
| GET | `/api/instances/{title}/ship-status` | The commit/push facts a caller of the fire-and-forget `/commit` and `/push-branch` polls to learn whether its step finished (the MindFlock MCP's `ship_session` does) → `{title, now, branch, base, head_sha, upstream_sha, pushed, dirty, beyond_base, committing, commit_rc, commit_at, has_origin, failed_step, failed_hook, failed_output, shell_tail?}`. `commit_rc` / `commit_at` are the exit status in `.mindflock_commit_status` and its mtime: the commit one-liner deletes the file first and writes it last, so a `commit_at` older than the moment you posted the commit is a previous attempt's. `committing` is the commit lock as the stage pill reads it. `upstream_sha` is the LOCAL `refs/remotes/origin/<branch>` (which a successful `git push -u` updates), so `pushed` (`head_sha == upstream_sha`) costs no network. `failed_step` / `failed_hook` / `failed_output` only for a non-zero `commit_rc` with no commit running (else null); `failed_output` is the failed hook's own section of the shell pane — its `- hook id:` block to the next hook, ANSI and `` ` $ ; \ ' `` stripped, ≤ 25 lines (head 5 + tail 20 with a `… N lines …` marker) and ≤ 1500 chars, the text Fast-track's failure note quotes. `?tail=N` (≤ 200) adds the last `N` lines of the session's shell pane (≤ 6000 chars), where a failed hook or a refused push says why. 404 unknown title, 409 workspace not ready |
| POST | `/api/instances/{title}/push-branch` | `GIT_TERMINAL_PROMPT=0 git push --no-verify -u origin HEAD` in the shell (hooks already ran on commit; prompts off, so a missing HTTPS credential fails at once). Its output is tee'd to `mindflock_push.log` in the worktree's private git dir; when it shows a missing sign-in, SSH key or git identity the push watcher emits `session.push_failed` `{reason, message, fix, detail}` once. **O3 soft gate:** when the repo's `.mindflock.toml` declares `check_command` and no check run passed against the current HEAD, returns `409 {error, check_required: true, check}`; re-POST with body `{"force": true}` to push anyway. **Red-zone gate** (checked first): a file inside an enforced red zone — or, while a green scope exists, outside it (not a companion, not an exemption still at its recorded blob) — committed between the fork point and `HEAD` returns `409 {error: "red zone breached: <path> (<pattern>)" | "outside green zone: <path>", red_zone_breaches: [{path, pattern, zone_id, kind}]}`; re-POST with `{"override_red_zones": true}` to push anyway (carry `force` too if the check gate also applies). See [Code map & red zones](#code-map--red-zones). **Divergence check** (every session, after the gates): `git fetch origin <branch>` (bounded, 20 s; fails open when offline or the branch isn't on origin yet), and when `origin/<branch>` has commits `HEAD` lacks, `409 {error: "<branch> moved on origin: N new commit(s) by <authors>", behind: N, authors: [...], remote, branch, diverged: true}` instead of a push git would reject. Never a force-push: the way out is `/rebase-remote`. |
| POST | `/api/instances/{title}/rebase-remote` | Body `{remote?}` (default `origin`; must be a remote of the worktree). `git pull --rebase --no-autostash <remote> <branch>` in the worktree, on the checked-out branch → `200 {ok, head, behind, row}`. On a conflict the rebase is **aborted** — `HEAD` and the tree are exactly as before — and the answer is `409 {error, conflicts: [paths], remote, branch}` so the person or the agent resolves it by hand. `409` for uncommitted tracked changes (`dirty: true`), a rebase already in progress, or a detached HEAD; `400 unknown remote`; `502` when git failed otherwise (offline). Never pushes. |
| GET | `/api/instances/{title}/branches` | `{branches, current, default}` — the branch list backing the **Open PR** dialog's base picker. `branches` are `origin`'s remote heads (falling back to local heads when origin is unreachable, so it's never blank); `current` is the session's own branch (never a valid PR target); `default` is the pre-selected base (the repo's `repository.repo_branches` block → `repository.pr_base_branch` → the session's fork base). 404 unknown title, 409 workspace not ready |
| POST | `/api/instances/{title}/make-pr` | Opens a PR → `{ok: true, url}` (or `note: "PR already open"`). Three tiers, in order: `gh pr create --base <base> --fill` when `gh` is installed **and** authenticated; else the GitHub REST API with a token from the usual resolution chain; else **`200 {ok: false, compare_url}`** — a prefilled compare URL the UI opens in the browser, plus the remedy sentence "add a GitHub token in Settings → Connections → GitHub → Connect, or install the GitHub CLI". A missing `gh` is never an error status. The UI's Make-PR dialog collects `<base>` from the branch picker above (and the frontend remembers the last base per repo — `prBaseByRepo` in `localStorage`); an omitted base falls back to the session's base branch. Same **red-zone gate** as push-branch, over `<base>...origin/<branch>` when that ref exists (the pushed branch is what the PR carries, diffed the way the forge diffs it — so upstream merges never read as breaches; `fork..origin/<branch>` when no base ref resolves), else `fork..HEAD`; `override_red_zones: true` in the body overrides. Optional `title` / `body` (strings, capped at 256 / 60000 chars) replace the commit-derived title / body on both the gh rung (then `--title … --body …`, the other half filled from the commits as `--fill` would) and the REST rung; 400 when either is not a string. The MindFlock MCP's `ship_session` sends a worker's `report_result` summary as the body |
| POST | `/api/instances/{title}/merge-pr` | Merges the branch's PR, same three tiers: `gh pr merge <branch> --merge`; else the REST API with a token — success answers `{ok: true, base}`, the branch it merged into (the UI's *Merged <name> into <base>*); else **`200 {ok: false, pr_url}`** so the UI can send you to the PR page to merge it yourself. Optional body `{override_red_zones}` — the same red-zone gate as make-pr (409 + `red_zone_breaches`). Fast-track never sends the override, so a chain halts on a breach with the error as its reason |
| POST · DELETE | `/api/instances/{title}/fast-track` | ⏩ **arm / disarm Fast-track** for this session: carry it to `depth` (`commit`, `push`, `pr`, `merge`; default from Settings → Workspace) and stop. Arm-and-*wait* — pressing it while the agent is still working records the target and lets the driver act once the agent is verifiably done, rather than committing a half-written tree. Body `{depth?, message?, base?}`. **An omitted `message` inherits the previously armed one along with its `message_auto` placeholder flag** — an intake-armed run carries the ticket / PR / issue name, and re-arming used to overwrite that with a generated *"Work on `<slug>`"*; carrying the flag across is what stops a re-arm from freezing a placeholder into the commit. With no message anywhere, a *pending* on-disk one from a blocked commit is adopted (the rule `GET /commit-message` applies), else a default subject is generated from the tree and marked a placeholder. Every placeholder is replaced at commit time by a message written from the final diff (see [web-ui.md](web-ui.md#intake)); a message a human typed never is. → `{ok: true, autopilot}`, the authoritative record, so the client settles its toggle without a follow-up read (arming touches a JSON file and does no PR lookup — the press must not wait on the network). 400 unknown depth or the `agent` rung (that one is for intake, where the session does not exist yet), 404 unknown title, 409 workspace not ready. `by` (`"agent:<title>"`, sent by the MCP's `set_autopilot`) records who chose the lane; anything else is the user. **The alias never lowers a guard**: an "ask first" someone chose stays on (unticking it is a `/lane` call). The same refusals as `/lane` (copy window, a group's lead or one-for-all member — 409; a paused group's member — 409 `held`). DELETE stops the driver taking the *next* step — anything already typed into the shell keeps running → `{ok, stopped}`. The row's `autopilot` block carries `fix_output`: the failed pre-commit hook's output a hand-off quoted to the agent (as `failed_output` on `/ship-status`), `""` once the fix turn is over or the run finishes |
| POST | `/api/instances/{title}/lane` | **Set the Fast-track target**: body `{lane: "leave"\|"commit"\|"push"\|"pr"\|"merge", ask_first?: bool, message?, base?}` → `{ok: true, lane, autopilot}`. The target is carried out by Fast-track — this is `/fast-track` with the lane vocabulary (`/fast-track` stays as its compatible alias; both go through `core/lanes.py`). `leave` disarms and answers `lane: null`. `ask_first` holds the run one rung short of its first outward step — a Commit target stops when the agent is done, a Push / Open PR / Merge target once it has committed — and the session then waits in the bell for your go (`ship-now`). 400 unknown lane or a non-boolean `ask_first`, 404 unknown title, **409 workspace not ready** while the session is still loading (retry). **409** `{error, shared_with}` on an in-place session whose folder another live session shares; **409** `{error, driver}` on a **copy window** whose branch another window drives (a record or a group; `leave` is always allowed — it only disarms), and on a group's **lead** (it ships once, through the release) or a **one-for-all member** (its work is the group's one PR). A member of an each-their-own group: the lane and `ask_first` are recorded on its task (every re-arm keeps them); while the group is **paused** nothing is armed — `{ok: true, held: true, lane, autopilot}`, armed on resume. Rows carry `lane: {target, ask_first, owner, by}` (`by`: `user` or `agent:<title>`) |
| POST | `/api/instances/{title}/ship-now` | **Ship what is there now** — the bell's Commit / Open PR on an ask-first fast-track, and "take what's there now": arms the target (`{lane}` — the UI sends the target the row showed — else the session's own record) without "ask first" and **without the 30 s idle dwell**, under this server's lease so the driver acts on its next pass. **Never the Settings fast-track rung**: a session with no fast-track of its own is refused (409 `this session has no fast-track of its own`). `{commit_message}` (the approval card's edited message) is committed as written. → `{ok: true, lane, autopilot}`. **409** `{error, activity}` while the agent is working (`the agent is mid-turn — stop it first`), on a dialog, or at its usage limit; **409** on a copy session (`{driver}`), a team run's lead, a one-for-all member, or a paused team run's member; 400 when the target ships nothing (`leave`); 404 unknown title |
| POST | `/api/instances/{title}/reset-stage` | ↺ **back to idle** — pins this session's guided ladder back to its start on a clean branch, so the header stops insisting on Push / Open PR / Merge while you keep working on the same branch. **Nothing git-facing happens**: no reset, no revert, no PR close, and the published `stage` is untouched — the pin (`backend/web/core/stage_reset.py`) rides the row as `stage_reset` and only the UI ladder reads it. It releases itself when the worktree moves (dirty tree or new commit), never on the stage label. Also takes down the finished cycle's leftovers: a **halted** fast-track record (a live chain is left strictly alone) and a **stale** verification result (a current failure is never touched — the push gate reads it). → `{ok: true, pinned, dirty, cleared[], row}`, where `row` is the recomputed session row so the presser's window flips now rather than on the next tick, and `pinned` is `false` on an already-dirty tree (that ladder is at its start already). 404 unknown title, 409 workspace not ready |

### Assigned tickets, PR auto-review + issue handling

| Method | Path | Behavior |
|---|---|---|
| GET | `/api/tickets` | Assigned tickets on the configured ticketing sources, each annotated with auto-ingest eligibility → `{tickets, sources, source_labels, buckets, done_buckets, ingest_states, errors[], stale, source_status}` (`source_status`: `{<source key>: {connect_error, last_test_ok, last_test_at}}` for EVERY configured ticket source, read fresh per request even when the listing is cached — `connect_error` is `"can't connect — <reason>"` or `null`: the listing's own error for that source, else, for a source the (cached) listing has not reached yet, its stored Test verdict's message when that test failed; a source the listing reached cleanly is `null` whatever an older Test said. Show it as the source's empty state instead of "No tickets are assigned to you". `last_test_ok` (`true`/`false`, `null` = never tested) and `last_test_at` (ISO, or `null`) restate the source's stored `last_test`; per ticket: `source`, `source_label`, `bucket`, `eligible`, `reasons`, `has_session`, and `elsewhere` `{device, label, kind}` when another of the user's paired devices already holds it — that row gets a *running on <device>* reason first and `eligible: false`; see `/api/tickets/claims`; and `claimed_by` `{name, why: "label"\|"started"}` when the TRACKER says a teammate is on it — assigned to someone else and labelled `<claim_label>:working`, or in a started state — with *<name> is on this* as the first reason and `eligible: false`; and, on a source with several repos, `route` `{repos, reason, method, summary, repo_url, choices}` — the repo(s) the ticket goes to and why, a repo picked on the row (`method: "manual"`), else the recorded decision or what its labels/title say, `method: "pending"` when only the start-time model question can decide; `choices` `[{name, url}]` is every repo of the source, for the row's **Repo** picker — see [ingestion-pipeline.md](ingestion-pipeline.md#a-source-spanning-several-repos)). The list is grouped by **source** and then by workflow-state bucket, so `source_labels` maps the key of EVERY configured source → its display label — including sources that returned nothing and ones that landed in `errors[]`, which deriving labels from the ticket rows alone would make vanish (`sources` is the subset that answered). The slowest of the three panel fan-outs (~3 s: one provider search per source + a `git ls-remote` per repo); per-source failures come back in `errors[]` rather than failing the call. Powers Intake → **Tickets** → **Assigned tickets** |
| POST | `/api/tickets/start` | Body `{source, id, agent?, repo?}` — `repo` (a source with several repos only): start in this repo — its URL or short name, one of the source's — instead of routing; recorded as the ticket's route (`method: "manual"`, *picked on the row*), which outranks label/title routing from then on. 400 a non-string `repo`, a one-repo source, or a repo the source doesn't have (checked before anything is claimed). The rest: force-start a coding session for one ticket, bypassing the auto-ingest filters. `agent` is the coding CLI for **this one launch** (the picker beside **Begin work**) and outranks the source's own; omit it — or send `""` — to use the configured chain (the source's Agent CLI, then `[mindflock].agent`, then the app default). 400 missing `source`/`id` or an `agent` no provider answers to, 404 ticket gone, 409 a session for it already exists — here, or on another paired device (`elsewhere` in the body names it) — or the tracker says a teammate is on it: `{error: "<title>: <name> is on this", title, claimed_by: {kind: "tracker", name, why, source}, override: "override_claim"}`. Re-POST with `override_claim: true` to start it anyway — the owner only: 403 for an agent-spawned start (`parent`/`spawned`) or a caller without what owner settings need (`auth.may_configure`). A started ticket is mirrored into the tracker (assigned when unassigned, labelled `<claim_label>:working`; the label comes off when the session closes or its PR merges). **Agent-spawned starts** (the MindFlock MCP's `spawn_ticket_session`) add `{parent?, spawned?, report_back?, note?, depth?}`: `parent` must be a live session (400) that is not over budget (409 `budget_locked`); `spawned` and `report_back` are strict booleans (400); `note` (≤ 4000 chars) is appended to the ticket prompt under *Note from session `<parent>`*; `report_back` appends the MCP's report-back footer when there is a parent and the ticket's CLI gets the MindFlock tools. The spawn limits (`MINDFLOCK_MAX_CHILDREN`, `MINDFLOCK_MAX_SPAWN_DEPTH`, `MINDFLOCK_MAX_SPAWNED`) are checked before the 202 (409 naming the knob) and again under the registry lock when the launch claims the title; a refusal there (or any other launch failure) is readable from `GET /api/create_failures`. The pending row and the launched session carry `parent` / `spawned`. Such a start answers `202 {started, title, branch, program, parent, spawned, report_back, reason?}` (`reason` says why a requested footer was not added); a start without these fields answers the plain `{started, title}`. An agent-spawned start (`spawned`) outside a team run is refused by the memory guard like `POST /api/instances`: **409** `{error, memory}` while memory reads low or critical. Intake panel starts and team-run tasks are never refused |
| GET | `/api/tickets/claims` | The tickets THIS device holds, for the user's other devices (`backend.web.core.fleet_claims`) → `{device, person, claims: {<slug>: {kind, since, person}}}` (`person`: the session's stamped person, else the device's own; a device from before people sends none and its claims read as `""`). `kind` is `session` (a live ticket session), `starting` (a launch not yet registered), `in_flight` (an ingestion-ledger marker under 2 h old with no session yet) or `reserved` (a team run's queued ticket, held regardless of age). Every start path asks the connected devices first, over the remote-control pairing: Begin work (409), the ingestion pipeline (backs off), team runs (left out with a warning) and the MCP's `list_tickets startable_only`. A device without this route is read from its session list; an unreachable one holds nothing |
| GET | `/api/tickets/fleet-holder?slug=&since=` | `{holder, reason}` — which other device holds `slug` (`holder: null` when none), asked fresh of every connected device. The ingestion pipeline's pre-launch check, asked of its own server after writing its `in_flight` marker: with `since` (that marker's epoch) a peer's `session`/`starting` always wins but a peer's marker wins only when it is older (device key breaks an exact tie), so of two devices racing for one ticket exactly one backs off. 403 for a request relayed by another device (one hop only) |
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
- `depth` — how far Fast-track carries it (`agent`, `commit`, `push`, `pr`,
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
  (`ticket_draft.TIMEOUT_DRAFT`; the New dialog's Describe box gives up at
  30 s, `session_plan.TIMEOUT_PLAN`, and offers its form instead). It runs on `asyncio.to_thread`, off the loop serving the
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
`needs_you` so the client can say what is left without a second fetch (plus
`before_merge: true` when it was a check of the branch before merge — the
checklist stays open and comes back after merge), and
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
branch: a second session on the same branch of the same repo (a duplicated
session, a repo adopted twice) writes nothing, and its later pushes refresh the
**first** session's plan — `tip_sha` and `refreshes` move on the owner, so the
shared checklist follows the branch rather than freezing at whichever commit the
owning session pushed last. The owner is the first-written plan; delete it and
the next push or button press from the other session writes its own. Two
exclusions: the same branch name in a *different* repo is a separate plan, and a
plan whose `repo_root` is blank covers nothing. Whether two sessions dedupe rests
entirely on their `repo_root` normalizing to the same path (worktree vs main
checkout, trailing slash, symlinks) — a mismatch degrades to two plans, not to an
error. One asymmetry to know about: the Verify dialog decides what to *offer*
by the repo's **basename** (all a live session carries), while the server keys
on the full normalized path — so two different repos with the same directory
name on the same branch show as *has a checklist* in the Ask-the-agent picker
even though the API would accept the second one.

| Method | Path | Behavior |
|---|---|---|
| GET | `/api/test-plans` | `{plans[], live_branch}` — every checklist, newest first. `live_branch` is the **flock-wide** default resolved fresh per request; each plan additionally carries `effective_live_branch`, the same question asked for *that plan's repo*. Compare a plan against its own, never against the top-level one, or every plan in a repo with an override reads as out of date. Per-plan: `state`, `steps[]` (`id`, `text`, `expect`, `actor`, `manual?`), `runs[]` (capped, newest kept; a run carries `prelive: true` when it was made **before merge** — once the plan has shipped such a run is history, not the current answer, so the plan comes back due), `run_session`, `branch`, `sha`, `tip_sha`, `refreshes`, `merged_at`, `live_at`, `merged_into`, `merged_into_at`, `merged_into_all`, `gen_started`, `gen_attempts`, `error`, `summary`, `intent`, `focus`, `notified_at`, `live_problem`. **`merged_into`** is the branch on `origin` the work has most recently reached (`""` while it is still only on the branch it was pushed to) — *not* `live_branch`, which is the branch the checklist is waiting for: in a repo that ships through a `staging` step the two disagree for most of a change's life, and the disagreement is the interesting part. `merged_into_at` is when it got there (`0` when the rung that answered could not say, i.e. the squash-merge case). `merged_into` keeps being refreshed — about every 5 minutes per plan — until a week after the work last landed anywhere, so it can climb *past* `effective_live_branch` (`main` when the live branch is `staging` and the work was promoted); `merged_into == effective_live_branch` is not a terminal state, and nothing in the server acts on it — it is display only. The week is measured from `merged_into_at`, falling back to `merged_at`, then `live_at`, then `generated_at` when it is `0`, so an undated squash-merge landing still stops being refreshed. `merged_into_all` is the trail, best first and one name per landing, so it is not "every branch that contains the commit" — every branch cut from `main` after a merge does. **`live_problem`** is why a checklist is not coming due when the answer is not "not yet" — origin has no such branch, or the branch's PR merged into a different one. Distinct from `error` (which means an operation you asked for failed); it clears itself. **`summary`** is the model's own one-sentence statement of what the change lets somebody do — what `title` (the session's name) could never be. **`intent`** is what the work was *asked* to do, snapshotted at push time from the ticket or the seed prompt; it is stored on the plan rather than read off the session, because plans outlive sessions and a rewrite that read it live ran with no ticket at all. **`focus`** is what you told the last rewrite it got wrong. The snapshotted session transcript is **not** on the wire: it is generation input, never UI. **`sha` vs `tip_sha`**: `sha` is the liveness anchor — written once and never moved, because ancestry is transitive and moving it forward could only make a checklist come due later or never. `tip_sha` is the newest commit seen pushed on the branch; it is what the diff is read at and what a pre-live run checks out, and it is recorded on every push whether or not a rewrite follows |
| GET | `/api/test-plans/repos` | `{repos: [{name, root}]}` — the local repos Verify can write a checklist in by branch: every checklist's `repo_root`, every open session's repo and every recently closed one's, once each, only checkouts still on disk. What **New checklist → Another branch…** lists |
| POST | `/api/test-plans/branch` | Body `{repo, branch}` — ask for a checklist for a **branch no session has been on** → **202** `{ok, plan, existing: false}`, or **200** `{ok, plan, existing: true, state}` naming the checklist that already covers that `(repo, branch)` (another session's title, or this route's own). `repo` is a `root` from `/api/test-plans/repos` (or its `name` when that is unambiguous). The plan is keyed `<repo name>@<branch>`, its commit is the local branch's, else `origin/<branch>` after a fetch of origin's heads, and it is written from the repo's own checkout by the default CLI with no `intent` (diff-only, like a closed session's). 400 no/unknown repo or a bad branch name (`git check-ref-format --branch`), 409 the branch the repo ships from or a branch it doesn't have (here or on origin), 409 `{error, detail: "Nothing to check yet — this branch has no commits.", reason: "no_commits"}` for a branch with no commits ahead of the live branch (unless `empty: true`) |
| POST | `/api/instances/{title}/test-plan` | Write a checklist for one session **by hand**, with no repo opted in → **202** `{plan, existing}`. Works for a **closed** session too, falling back to the recently-closed store for its branch and repo (409 when that branch is gone from the repo, 404 when neither store knows the name) — a checklist outlives its session everywhere else here, and creation demanding a live window made the button useless at the moment people ask for one. A closed session carries no seed prompt, so such a plan has no `intent` and is written from the diff alone. A headless one-shot reads the branch's diff and answers in up to three minutes; `existing: true` points at the plan that is already there rather than erroring (a **200**, not an error — the honest answer to "write one for this" when one exists is to point at it). `plan` then names the checklist that already **covers the branch**, which may be **another session's title** when the branch is open in more than one window (see "One checklist per (repo, branch)" above), and `state` is read from that owning plan — the same holds for the closed-session fallback. 404 unknown session, 409 no workspace yet / nothing committed on this branch / a detached HEAD. **409 `reason: "no_commits"`** (`detail` *"Nothing to check yet — this branch has no commits."*) when the session's branch has no commits ahead of its live branch and a clean tree — a session sitting ON the live branch is not refused. Body `{"empty": true}` creates the checklist **empty** instead (`origin: "manual"`, no model call, **201** `{ok, plan, existing: false, empty: true}`), for an open or a closed session and whatever its commits; an existing checklist is still pointed at (200 `existing: true`) |
| POST | `/api/test-plans/{plan_id:path}/run` | Start a real session that works the checklist's **agent** steps → `{session}`. On a plan that has **not shipped** this is the dialog's **Check now**: it checks the branch's newest commit, and when it finishes the plan goes back to `generated` (not `done`), so it comes back due after merge. Optional `{"steps": ["s3"]}` narrows it to those steps (the per-step re-check). 409 a run is already going / the plan has no steps / **every step is a person's** (an agent is forbidden from settling those, so a run would provision a workspace to hand the list straight back), 400 naming an unknown or `human` step |
| POST | `/api/test-plans/{plan_id:path}/fix` | **Fix…**: open an ordinary session (`fix-<plan id>`, in the checklist's repo) and hand it the failed steps → **202** `{ok, session, reclaimed}`; an open `fix-<plan id>` is sent the failures instead. Optional `{"steps": ["s3"]}` narrows it to those failed steps. **Every refusal carries `detail`** — one plain sentence for the toast, never an API path, a folder or a traceback — beside the older `error`: 404 *That checklist no longer exists.*; 400 a non-list/empty `steps` (*Pick at least one failed step to fix.*) or an unknown step (`unknown_steps`; *That step isn't on this checklist any more — refresh and try again.*); 409 git missing, nothing failed (*Nothing on this checklist failed, so there is nothing to fix.*), a named step that didn't fail, no repo recorded, the repo gone (`repo_root` carries the path), or the last fix session's workspace still holding uncommitted work (`worktree` carries the path). A refusal from session creation is passed through with `detail` = *Couldn't start a fix session — <its error>.* (or the generic sentence when that error names a path); an unexpected failure is **500** `{error, detail: "Couldn't start a fix session — MindFlock hit an error (details in the log)."}` |
| POST | `/api/test-plans/{plan_id:path}/result` | Body `{step_id, result, note}` — record one step's outcome as a **person** → `{plan}`. `result` is `pass` \| `fail` \| `blocked` \| `""` (empty un-answers it). Unlike the file a verify session writes, an unrecognised value is a **400** rather than being coerced. Answering the last outstanding step closes the plan (a person's `fail` that turns the checklist from not-failed to failed also emits `session.test_plan_step_failed` with `new: "failed"`, data `{plan, title, branch, step, text}` — the bell's row) — unless a run is still in flight, in which case the answer is recorded and `finish_run` performs the transition, so a mid-run answer cannot strand the agent working beside it — and unless the work **has not shipped**: answers before merge check the branch and leave the plan `generated`, so it still comes back due after merge (the first answer after merge opens a new run). 404 unknown plan or step |
| POST | `/api/test-plans/{plan_id:path}/steps` | Body `{text, expect?, actor?}` — append a step a person wrote → `{plan}`. Marked `manual`, so it survives a regenerate. A plan in `failed` (the model wrote nothing) that gains a step becomes an ordinary checklist — `generated`, or `due` once shipped — and its generation `error` is cleared. 400 empty text / bad `actor` / at the 25-step cap, 404 unknown plan |
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
`GET …/code-map/file`, the search box `GET …/code-map/search`.

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
`none` (no zones) — or `null` when there is nothing to say. `plan_pending`
(present only when non-zero) is the file count of a declared `mindflock-plan`
the agent is waiting on Go for — the plan `…/code-map/live` shows, computed by
the loop (never on this read) while the agent isn't working, for providers
with the plan loop; it goes to 0 once any tool call starts after the plan or a
Go is delivered (`…/code-map/go`), so the rail chip, the 🔔 and the phone can
say "Plan ready" without the Map open. ExitPlanMode plans don't count (they
are approved in the terminal). Committed
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
inside the scope** (from the Code Map import graph; "test" is the Code Map's own
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
| GET | `/api/instances/{title}/code-map?fp=` | `{root, repo: {id, label}\|null, fingerprint, files: [[rel, size, flags]], truncated, edges: [[src, dst]], graph_partial, langs}`. `flags`: 1 = zone-matched git-ignored file, 2 = test file; an edge means *src imports dst* (Python, JS/TS, CSS resolvers). `fingerprint` = worktree fingerprint + a digest of the zone set; when `fp` equals it → `{unchanged: true, fingerprint}` — except while the import graph is partial (`graph_partial: true`, the read budget ran out on a big/cold repo): then the route always rebuilds (resuming from the per-file memo, which is kept per worktree on disk in `.code-map-cache/` and so survives a restart — see [configuration.md](configuration.md)) and the snapshot's `fingerprint` carries a `.partial` suffix, so it differs from `/live`'s and the client refetches until the graph is complete. The route and the boot warmer share one builder, so a warmed snapshot answers the first GET without reading files, and a client already holding the warmed fingerprint gets `{unchanged: true}` |
| GET | `/api/instances/{title}/code-map/live?since=` | `{now, fingerprint, repo, changed: [{path, status, added, removed}], feed: [records with ts > since, writes/reads worktree-relative, outside paths dropped, deny/breach/artifact passed through, subagent fields `agent`/`agent_type` (a call made inside a subagent) and `desc`/`atype` (the parent's Agent call) passed through, `peek: [rel]` = reads outside the green scope; ≤300], plan: {source: "declared"\|"exitplan"\|null, ts, items: [{path, intent, new, outside?, blocked?}], thread}, off_plan: [rel], zones: [effective zones incl. waived: {id, pattern, name, note, created, scope, re, waived, kind}], breaches: [{path, pattern, zone_id, committed, kind}], guard: {state, detail, hard, mode}, mode: "green"\|"red"\|null, exempt: {rel: "sha [sha …]"}, companions: [{pattern, re, source: "default"\|"repo"\|"tests"}], companion_files: [rel], activity, plan_supported, ci, others: [{session, path, ts}]}` — a green breach has `pattern: "outside green"`, `zone_id: null`, `kind: "green"` — `guard.detail` is a full sentence for the pill's tooltip (what the state prevents and what it doesn't, e.g. "Claude Code is blocked before it edits a red zone (hook verified 2 min ago)"), never the label; `ci` = the worktree's filesystem is case-insensitive, so zones match with IGNORECASE (the hook does); `others` are edits by other live sessions on the same repo in the last 10 minutes, on paths that exist here |
| GET | `/api/instances/{title}/code-map/file?path=` | The file view → `{path, lang (family), loc, role, symbols: [{name, kind, line, end, public, sig, parent, children, changed?}], imports: {internal: [{path, name, folder, names}], external: [str]}, entry: [{kind: "http"\|"cli"\|"event"\|"main", method, route, line, handler, changed}], entry_groups: [{prefix, count, changed, items}] (only past 24 routes), used_by: [{path, name, folder, names}], tested_by: [path], changed_lines: [[a, b]] (new-side hunks vs the fork point), changed_symbols: ["Class.method"], zones: {red, green: bool\|null}, partial}`. `zones` goes through the shared classifier on the realpath AND the path as written (a symlink into a red zone is red): `red` = blocked, `green` = writable under the green scope (null = no green zone). An unreadable file (mode 000, EIO) is an empty view, never a 500. Paths are taken literally first (git tracks ` lead/` and `back\\slash/` on Linux), leniently (whitespace-trimmed, `\\` as `/`) only when the literal names nothing. 400 for an absolute / `..` / escaping / missing / directory path |
| GET | `/api/instances/{title}/code-map/search?q=&limit=40` | Files, folders, symbols and routes matching `q` (case-insensitive, camel/snake-aware, initials; tests rank lower) → `{items: [{path, name, kind, line, score}], partial}`; `kind` is a symbol kind, `"file"`, `"dir"` or `"route"` (a route matches by its route or by the `METHOD /route` label it shows); `limit` ≤ 200 |
| POST | `/api/instances/{title}/code-map/ask-plan` | Body `{mode: "plan"\|"remaining"}` → sends the plan prompt (list every file you intend to touch in a `mindflock-plan` block, then wait) or its mid-flight variant → `{ok, told: "sent"\|"queued"\|false, reason?}` |
| POST | `/api/instances/{title}/code-map/go` | Body `{zone_ids: [...], scope_to_plan?: bool}` (zones staged during plan review) → one "go ahead, and don't modify these" message → `{ok, told, reason?}`. `scope_to_plan: true` = **Go — only the planned files**: the plan's items become anchored worktree green zones (an existing path exactly; a new file's parent dir; a new root-level file itself — every path glob-escaped, so `pages/[id].tsx` is literal), earlier work outside is exempted, the guard synced, and the message names the scope → also `{zones: [created], exempt: [rel]}`; 409 when the plan names no usable path |
| GET | `/api/instances/{title}/red-zones` | `{repo: {id, label}\|null, plan_first, zones: [effective, with kind], mode, exempt: {rel: "sha [sha …]"}, companions: [repo patterns], sessions_here}` (`sessions_here` = sessions sharing this worktree — a green zone applies to all of them) |
| POST | `/api/instances/{title}/red-zones` | Body `{pattern, name?, note?, kind: "red"\|"green" (default red), scope: "repo"\|"worktree" (default repo for red, worktree for green), tell_agent?, exempt?: bool}` → adds the zone, syncs the guard of every live worktree it reaches (repo scope: every live session on that repo id plus the repo's `git worktree list`) **before returning**, records files already changed inside it as pre-existing → `{ok, zone, zones, told, already_changed: [rel], reason?}`. 400 invalid pattern (empty, `..`, absolute, > 400 chars) or scope; 409 when repo scope is asked for and git can't identify the repo, or when the same pattern is already the other kind. `tell_agent` sends the zone notice. **Green**: `scope: "repo"` → 400; paths already changed outside the new scope are exempted (unless `exempt: false`) → also `{exempt: [rel], committed_outside: n}`; no `already_changed` seeding. A `scope: "repo"` zone reaches every worktree of the repo (and settings sync spreads it): owner only (`auth.may_configure`, **403** otherwise), like `POST /api/red-zones` |
| POST | `/api/instances/{title}/red-zones/allow` | Body `{path, tell_agent?: bool (default true)}` — **[Allow this file]** on a scope request: an anchored worktree green zone for exactly `path` (glob-escaped by `red_zones.glob_escape`, so `app/[slug]/page.tsx` is literal, not a character class; synced now) and a "you may now edit it" notice → `{ok, zone, zones, told, reason?}`; 409 when the worktree has no green zone (a first one would scope the whole session to one file) or a red zone covers the path; 400 for an absolute / `..` path |
| POST | `/api/instances/{title}/red-zones/exempt` | Body `{exempt: bool, paths?: [rel]}` — `false` = "Treat as breaches" (drop those / all exemptions); `true` + `paths` = exempt them at their current content (working tree, `HEAD` and `origin/<branch>` blobs) → `{ok, exempt: {rel: "sha [sha …]"}}` |
| POST | `/api/instances/{title}/red-zones/preview` | Body `{pattern, kind?}` → dry run, nothing saved: `{re, count, sample: [≤20 rel], ignored_count, changed: [rel], truncated}`; 400 invalid pattern. `kind: "green"` adds `{writable_files, changed_outside: [rel], committed_outside: n, roots: [distinct matched top paths], unanchored: bool, anchored: "/<root>"\|null (offered when exactly one root), warnings: [str]}` — warns on an unanchored basename pattern (it matches at any depth) and on zero matches ("nothing exists here yet; the agent may only create new files under it") |
| DELETE | `/api/instances/{title}/red-zones/{zone_id}?tell_agent=` | Remove the zone (any scope) and re-sync the guards it reached → `{ok, zones, told}`; 404 unknown id. Removing a GREEN zone exempts the work already done inside it and tells the agent the scope narrowed (`?tell_agent=0` to skip). Only a zone scoped to THIS session's worktree is anyone-who-drives-the-session's to remove; any other (a repo zone, another worktree's) is owner only (`auth.may_configure`, **403**) |
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

#### Shared zones (committed `.mindflock/zones.toml`)

The zones a team shares live in the repo, read from each session's worktree
(so they follow its branch) and enforced on top of the local store: they come
back in the session's zones list with `scope: "shared"`, `author`, `added`,
`locked: true` and `source: ".mindflock/zones.toml"` (a local zone that the
file also has carries `shared: true` + `shared_author` instead of a second
row, unless it is waived here). The per-session zones read also answers
`shared`, `shared_error`, `shared_path`, `shared_exists`.

| Method | Path | Behavior |
|---|---|---|
| GET | `/api/instances/{title}/shared-zones` | `{shared: [{id, kind, pattern, name, note, author, added}], shared_error, shared_path, shared_exists}` — the file as this worktree has it. `shared_error` says why an unusable file is being ignored (`""` = fine or absent). Ids are stable (`sz_` + a hash of kind + pattern) |
| POST | `/api/instances/{title}/shared-zones` | **Add to repo**: `{pattern, kind?: "red"\|"green", note?, name?}` appended to the file in THIS worktree (created with a header comment; every other byte kept), `author` = the person's name (else git name), `added` = today; **not committed**. Re-syncs the worktree's guard at once. → the GET body + `{zone, created}` (`created: false` = already there). Owner only (403); 400 bad pattern or an unusable file (never overwritten); 409 the same pattern is the other kind there |
| POST | `/api/instances/{title}/shared-zones/remove` | `{id}` — removes that zone's block (its comment lines with it) → the GET body + `{zone}`. Owner only (403); 404 not there; 400 unusable file |

### IDE

| Method | Path | Behavior |
|---|---|---|
| POST | `/api/instances/{title}/ide` | Open/focus the workspace in the configured IDE (Settings → Advanced; Cursor by default). GUI editors get new windows maximized + existing ones focused; terminal editors open in a new terminal window → `{ok, opened_new}`; 400 if the IDE isn't launchable |
| GET | `/api/ides` | The known-IDE registry: `{ides: [{command, name, kind, installed}], current, current_name}` — for the Settings detected-IDE picker |
| GET/POST | `/api/cursor/autoadopt` | Get/set `{enabled}` — auto-adopt Cursor-opened workspaces as sessions |

### Send a message + prompt queue

| Method | Path | Behavior |
|---|---|---|
| POST | `/api/instances/{title}/send` | Body `{text, submit?, dialog_safe?, say?}` — or `{enter: true, say?}` (below). Types `text` into the **agent** window and (default) presses Enter, booting/resuming the agent tmux first if it isn't running — so one call kicks a fresh session into motion (max token use). `submit:false` types without submitting. Enter is a separate keystroke a beat after the text so an agent TUI doesn't read the burst as a paste. A **sleeping** agent is woken and the text put at the front of its prompt queue instead of typed into the booting CLI → `{sent: false, queued: true, submitted: false, woke: true}`; an unsubmitted paste (or a looping queue) only wakes it → **409** `{error, waking: true}` — send again in a moment. `dialog_safe:true` (the web UI's Thread "Send now" and every playbook paste) re-probes the agent's live, uncached activity first — and checks the screen for a dialog, which outranks the reading (see the screen-evidence guard under [Inter-agent messages](#inter-agent-messages)) — and never types into a prompt or the usage-limit menu (a digit or an Enter there would answer it): on `clarify`/`limit`, a dialog on screen (or an activity that can't be read) a submitted message is **queued** instead → `{sent: false, queued: true, submitted: false}` (the drain delivers it once the agent is free), and an unsubmitted paste is **409** `{error, in_dialog: true}` with nothing typed (queueing it would submit it later). A working agent is still typed into. **`say`** (boolean, default `false`; anything but JSON `true` reads as false) asks for a spoken reply to *this* prompt: the session ends this one reply with the `Say:` line [voice mode](#voice-talking-to-a-session-from-the-phone) speaks. The client decides per prompt — the phone's dictation sends `true`, typing sends nothing, the desktop mic sends `true` only while "read replies aloud" is on; the server never guesses from the device or from the voice flag, because the line costs tokens. It is applied right before the keystrokes, after every dialog/queue decision: for a CLI whose prompt hook injects the instruction (`provider.voice_prompt_hook()`: Claude) the delivery re-pins the worktree's hooks (a session launched by an older build gets the hook that knows the request; Claude Code hot-reloads it) and arms the one-shot `<voice dir>/<tmux session>.next` that hook consumes; for every other CLI (Codex, the config-driven ones), or when the file can't be written, `"\n\n"` + `core.spoken.INSTRUCTION` is typed after the text. A failed send disarms it again. A **queued** prompt (dialog_safe on a prompt, an asleep agent) keeps `say: true` on its queue item and is armed only when the drain or Send now types it; queue items without the field (all older entries) read as false. `submit:false` (a paste left for review) never asks. Absent/false: no instruction, whatever the voice flag says. → `{sent, submitted}` (409 if the workspace is gone, `{paused: true}` when the session is paused — nothing typed, the agent not relaunched — or `{budget_locked: true}` when the session is over budget, 502 if the send fails). **`{"enter": true}`** with no (or empty) `text` presses Enter on what is already in the agent's input field — live dictation: the desktop app types recognised phrases straight into the terminal over its own websocket (the server never sees that text), then submits through here so `say` is armed exactly as for a text send: Claude's one-shot `.next` right before the keystroke, or, for a hook-less CLI, `"\n\n"` + the instruction typed after the draft (as a paste) before the Enter. The Enter is the same separate keystroke a text send ends with. Always dialog-safe, never queued and never booting (there is no text to queue, and a fresh CLI has no draft): a dialog or the usage-limit menu on screen, or an activity that can't be read → **409** `{error, in_dialog: true}` with nothing pressed (an Enter would answer it); asleep → **409** `{error}` (not woken); agent not running → **409** `{error}` (not relaunched); paused, over budget and driven-elsewhere as above. A working agent is pressed (the CLI queues the prompt itself). → `{sent: true, submitted: true, entered: true}`; **400** for `enter` with a non-empty `text` or with `submit: false` (and `enter` is JSON `true` only — anything else is a plain send with no text, 400 `empty message`) |
| POST | `/api/instances/{title}/driver` | Take or release the session's keyboard ([people.md](people.md#owner-driver-viewers)). `{"take": true}` makes the caller's person the driver — anyone who can reach the session may; the previous driver hears it as a `session.driver_changed` event and their open terminals turn read-only. `{"release": true}` gives it up (**409** `{error: "driven by <name>", driver}` when another person holds it). → `{owner, driver, driver_name, viewers}` (+ `previous`, the old driver's id, on a take). **400** neither, **404** unknown. A remote row (`<device>::<title>`) is forwarded like every per-session route, carrying the caller's person |
| POST | `/api/instances/{title}/owner` | `{"person": "<id>"}` — give the session to another person. The owner otherwise never changes (it defaults to the person the session was created for). This device's owner only (`may_configure`, **403**; so not through a gateway). → the presence fields; **400** not a person id, **404** unknown |
| GET | `/api/instances/{title}/queue` | `{items: [{id, text, added}], pending, enabled, loop, loop_interval, wait_for_limit, limited_until, last_sent}` |
| POST | `/api/instances/{title}/queue` | Body `{text, index?}` — append a prompt, or insert it at a 0-based position (clamped) when `index` is given — or `{texts: [...]}` to bulk-append (one write; blank rows skipped, overflow past the queue cap dropped; response adds `added`/`skipped` counts). Enqueuing re-enables draining. → queue state |
| POST | `/api/instances/{title}/queue/flags` | Body `{enabled?, loop?, loop_interval?, wait_for_limit?}` — `enabled` gates auto-draining; `loop` re-queues each sent prompt so a self-improving prompt cycles forever; `wait_for_limit` holds draining until the usage window resets |
| POST | `/api/instances/{title}/queue/reorder` | Body `{id, index}` — move to an absolute 0-based position (clamped; the drag-and-drop path) — or `{id, direction}` (`up`/`down`) to nudge one slot |
| POST | `/api/instances/{title}/queue/edit` | Body `{id, text}` — rewrite a queued prompt in place |
| DELETE | `/api/instances/{title}/queue?item=<id>` | Remove one item; omit `item` to clear the whole queue |
| POST | `/api/instances/{title}/queue/send_now` | Body `{id}` — send one queued item now, skipping the drain's waits. On a **sleeping** agent: wakes it and moves the item to the front (the drain sends it once the CLI is ready) → the queue state plus `woke: true`; **409** `{error, waking: true}` when the queue is paused; **409** `{error, paused: true}` when the session is paused (the item stays queued) |

**One keyboard per session.** `/send`, the queue's add (`POST /queue`) and
Send now (`POST /queue/send_now`) — like `/answer` below and the terminal
websockets — type into the terminal for a person, so they are refused with
**409** `{"error": "driven by <name>", "driver": {"person", "name",
"device"}}` while **another person** holds the session's keyboard
([people.md](people.md#owner-driver-viewers)). `"take": true` in the body takes
the keyboard first (the previous driver is told); the driver, and anyone while
nobody drives, passes. All of one person's devices share an id, so a person is
never refused on their own session. The server's own typers (the drain, the
mailbox lane, the limit watcher) are not keyboard input and are never gated.

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
| GET | `/api/instances/{title}/output?view=last_reply\|transcript\|screen\|say&max_chars=N` | What the session's agent produced → `{view, text, truncated, activity}`, plus `fallback: true` when the view fell back. `last_reply` (the default) is the provider's newest assistant message. A provider with none (no readable transcript, or no reply yet) falls back to `screen`, answering `view: "screen", fallback: true`. `transcript` is the text `/history?pane=agent` serves (the provider transcript, else the pane's scrollback). `screen` is the visible pane only (`tmux capture-pane -p -J`, no scrollback), which is what a dialog looks like. `say` is the session's spoken line → `{view: "say", text, source, has_question, truncated: false, activity}` (`text` `""`, `source` `"none"` when there is nothing; see [Voice](#voice-talking-to-a-session-from-the-phone) — the MCP's `read_output` does not offer it). `max_chars` defaults to 6000 and is clamped at 50000; truncation keeps the **tail**. `activity` is the memoized probe. **400** for a bad view or a non-integer/non-positive `max_chars`, **404** for an unknown session, **409** `no live session` when the view needs the tmux pane and it is gone |
| POST | `/api/instances/{title}/answer` | Answer a dialog the agent is blocked on. Body `{text?, keys?, dialog_id?, by?}`. `text` (≤ 2000 chars, all C0/C1 control characters stripped) is typed literally **without** Enter, even when it starts with `-`. Then each of `keys` (≤ 20, allow-list `Enter Escape Up Down Left Right Tab BTab Space 1`–`9 y n`) is pressed in order. A digit pressed on a multi-select question (a `/dialog` body with `multi: true`) only ticks a box, so it is not a settling answer: the id stays, and a second tick seconds later is not refused as "just answered". Allowed while the agent's live, uncached activity is `clarify` or `limit`, or — whatever the reading (`offline` excepted) — while its provider **parses a dialog on the visible screen**: screen evidence beats the activity layer, so a click on a dialog that is plainly up is never refused because a background sub-agent's hooks made the session read `working` or `idle` (it then answers `activity_before: "clarify"`). Anything else answers **409** `session is not waiting on a prompt (activity: X)`, because typing into a working or idle agent is a prompt, not an answer (that is `/send`). `dialog_id` (optional, from `GET /dialog`) pins the answer to that dialog: when the dialog on screen now has a different id (it was answered meanwhile and the next one is up), or the screen can't be read, nothing is typed and the answer is **409** `{"error": "the prompt changed", "dialog_changed": true}`. One answer per dialog: the route holds a per-session lock from its screen read through its keys, and a settling answer (any of `1`–`9`, `Enter`, `Escape`, `y`, `n`) pinned to a `dialog_id` that got one less than 4 s ago (`agent_io.ANSWERED_HOLD_S`) is **409** `{"error": "that prompt was just answered", "dialog_answered": true}` — the CLI may not have redrawn yet. Seeing a different dialog in between (on `/dialog` or `/answer`) clears that memory, so an identical prompt asked again is answerable. `by` is `"agent"` (default) or `"user"`: an agent's answer doesn't count as human input; a person's click in the UI (`by: "user"`) is stamped as presence like `/send`. → `{ok: true, activity_before}`. **400** for a bad body (including a non-string or > 64-char `dialog_id`, or another `by`), **404** unknown, **409** a prose question's `q:` id (`{"error": "not a dialog", "no_dialog": true}` — a question asked in words is answered with `/send`), over budget (`budget_locked: true`), just answered (`dialog_answered: true`), or `driven by <name>` while another person holds the keyboard (`"take": true` takes it first — see [One keyboard per session](#send-a-message--prompt-queue)), **502** when tmux refuses the keys |
| POST | `/api/instances/{title}/asking/dismiss` | Dismiss the question the session's newest reply asks (its row's `asking` of kind `question`). `{}` → **200** `{ok: true, dismissed}` (`dismissed: false` when nothing was asking). The row stops carrying it, `/dialog` stops serving it and no push repeats it; a **newer** reply that asks shows again. The dismissal — and an answer typed into the session — survive a server restart (`~/.mindflock/asking.json`), so a question put off at 5 PM is not asking again after the 8 PM update. A dialog is not dismissable — it blocks the agent until answered. **404** unknown session |
| GET | `/api/instances/{title}/dialog` | The dialog the agent is blocked on, as data, for the UI's answer buttons → `{id, kind: "dialog", parsed, question, command, options: [{key, label, kind}], source?}` — or, while the session asked a question in **prose** (activity `idle`, the row's `asking.kind == "question"`), **200** `{id: "q:<ts>", kind: "question", parsed: false, question, options: [], ts}`, so one fetch covers both (answer it with `/send`; `/answer` on a `q:` id is **409** `no_dialog`). The session's provider parses the visible screen (`BaseProvider.parse_dialog`; Claude Code and Codex implement it, pinned against golden screens in `tests/unit/data/dialogs/`): `question` is one self-contained line (`"Bash command — Add redis as a dependency. Do you want to proceed?"`), `command` the command or MCP tool call it asks about (else `null`) — for Claude Code 2.x's Bash dialog the boxed command line itself (its description goes into `question`), for an MCP **Tool use** dialog the call led by the argument that names it, chosen by name — `title`, then `session` / `to` / `target`, else the first argument with a value (`hello-worker · mindflock — Spawn worker session`, also when Claude lists `prompt:` first; the arguments go into `question`) so a narrow strip still says WHICH call it is. A command the dialog's box hard-wrapped mid-token at the pane's edge (a long path) is joined back without a space; its own line breaks (a heredoc) are kept. A tab header on the heading (`· from the general-purpose agent 2 of 3`, a Claude background sub-agent's prompt) is cut off the question and reported as `source` (`"general-purpose agent"`, only when set). A side panel Claude draws to the right of the dialog (its diff view) is cut away before parsing. `options` the dialog's own numbered choices with `key` the digit to press and `kind` one of `yes` (approve once), `always` (a standing rule — "don't ask again", "allow all edits during this session"), `no` (refuse / exit) or `other`. **Claude's question tool** (AskUserQuestion — single- and multi-select questions, several in one call as tabs, the free-text "Type something" row, the review screen) answers `kind: "select"` with `question` the question line as asked and these additions: `heading` (the active tab's label), `multi` (checkboxes), `steps` (`[{label, done}]` from the tab row, the review tab `{label: "Submit", done: false, submit: true}`; one entry for a single question), `step` (the active tab's index — read from the tab's background colour, else inferred), `review` (on the review screen only: `[{question, answer}]`), `cursor` (the key under the cursor), `button` (a multi-select's own `"Next"` / `"Submit"` row, else `null`) and `nav` (`{next: ["Right"], prev: ["Left"], submit: ["Enter"]}`: the keys that switch tabs and press the cursor's row — post these, never hard-coded key names); each option adds `desc` (its description, `null` when it only repeats the label), `checked` (`true`/`false` on a checkbox row, else `null`) and `keys`, and its `kind` is `other` (a choice), `text` (the free-text row — its `label` is what was typed into it once typed), `chat` ("Chat about this"), or on the review screen `yes` (Submit answers) / `no` (Cancel). Answering it: single-select — post the option's `keys` (a digit picks and submits that question); multi-select — post each choice's `keys` (a digit ticks or unticks, the cursor stays; several digits in one post are fine), then `nav.next` (on the last question that is the review tab); free text — post the text row's `keys` (it takes the cursor), then `{text, keys: ["Enter"]}`, both with the same `id` (typing keeps it; while the text row holds the cursor the other choices' `keys` start with `Up`, since a digit there would be typed); chat — the chat row's `keys`; review — `1` (Submit answers) or `2` (Cancel), and `nav.prev` to go back. Whether Enter on a multi-select row ticks it is unverified: use the digits. A multi-select's id stays put while its boxes are ticked, so its digits don't count as settling for `/answer`'s 4 s just-answered memory. A screen no parser recognizes answers `parsed: false`, the best-effort question line and no options — a numbered list the agent merely printed carries no selection cursor and never parses, and an option list counts only when nothing but blank lines, rules and a key-hint footer follows it (a numbered prompt in the transcript has more transcript or the input box under it). Claude Code's parser takes only its `❯` cursor (`>` marks the user's own prompts) and needs the rule Claude draws above every dialog — a rule with text drawn on it still counts. `id` is a 12-hex digest of the dialog built **per component** — each paragraph above the options (heading, command box, question), then each option as `key:label` — with the selection cursor and **all whitespace** removed; what the width can cut (every option label, Claude's collapsed "About the … Tool:" description, any paragraph with a line cut short with `…`) counts only by its first 24 characters before the cut, `ctrl+o to expand` hints and the tab header's `N of M` not at all (for the question tool: the question, the review's answers and each choice as `key:label+description` with its checkbox removed — the free-text row by its key alone, the tab row not at all). Nothing is left out whole, so it holds while the same dialog is up — arrowing through options, a resize that re-wraps or re-cuts its lines (47 to 200 columns), another tab being answered — and changes with the next prompt (another command, tool, argument or question); the 2-option variant Claude draws at ≤ 80 columns has other keys and so another id. Send it back as `/answer`'s `dialog_id`. Served while the live, uncached activity is `clarify`, or whatever the reading while the provider parses a dialog on screen; `limit`/`offline`, or another reading with no parsed dialog, is **409** `session is not waiting on a prompt (activity: X)` — or, with `?quiet=1` (what the UI's answer strips send), **204** with no body: "not waiting" is the routine answer for a strip whose row reading is a poll behind, and a 4xx would be logged by the browser every time. **404** unknown, **409** `no live session` (also with `quiet`), **500** when the capture fails |

### Voice: talking to a session from the phone

What the phone page's voice mode calls
([web-ui.md](web-ui.md#voice-talking-to-a-session)). The words a session
speaks are written by the session itself: a prompt sent with **`say:
true`** (`/send`, decided per prompt by the client — see [Send a
message](#send-a-message--prompt-queue)) carries one instruction
(`backend.web.core.spoken.INSTRUCTION`, exposed for the phone to parse
against) asking it to end that reply with one final line starting `Say:` —
up to three short plain sentences under about 50 words, no code, paths or
markdown: first the context (what was being worked on, the feature or area,
and the project when more than one could be meant), then what it did or will
do, then any question it has. Only those prompts: the line costs
tokens. Claude Code gets the sentence from its own `UserPromptSubmit` hook:
the delivery arms the one-shot `<voice dir>/<tmux session>.next` (the arming
unix time as text) right before typing the prompt, and the activity hook
MindFlock installs per worktree prints the sentence as injected context when
that file is younger than 120 s, deleting it either way — so a prompt that
never got submitted cannot lend its line to the next one. A CLI without that
channel gets the sentence typed after the prompt. The sticky **voice flag**
(`<voice dir>/<tmux session>.on`, below) no longer injects anything: it marks
"a phone is in a voice conversation with this session". When the turn
ends, the same hook's `Stop` branch files the payload's
`last_assistant_message` beside the activity marker as
`<marker dir>/<tmux session>.reply.json` (`{"ts", "text"}`, text cut at
8000 chars) and `core/spoken.py` lifts the `Say:` line out of it — the exact
text the session just said, no transcript scan. The **spoken record** every
reader serves is `{say, source, has_question, ts}`: `say` the text after the
marker (the LAST `Say:` line of the reply; `**Say:**`, `SAY:` and `Say —`
count, a `Say:` inside a code fence does not), markdown stripped, ≤ 320
chars, or `null`; `source` is `hook` (a `Say:` line from the reply the
Stop hook filed), `transcript` (no reply file, but the provider's newest
assistant text ends with a `Say:` line — `ts` is then `null`), `digest` (no
`Say:` line: a deterministic digest of the reply — fenced and inline code,
URLs, paths, tables and headings dropped, the first sentence plus every
sentence ending in `?` kept, at most two sentences and ~45 words) or `none`;
`has_question` whether the line asks the listener something (a `?`, or a
question word up front); `ts` the reply file's stamp when the words came
from it, else `null`. The digest is made from the reply file's text, and
only without a reply file from the provider's own newest assistant text
(`last_assistant_text`, Claude only) — so a CLI whose hook never files a
reply and has no transcript reader speaks nothing. Nothing on this path
calls a model; the one route that does is `condense`, and only when a
listener asks. The hook branches are in
[providers.md](providers.md#activity-signal-activity_markerspy).

| Method | Path | Behavior |
|---|---|---|
| GET | `/api/instances/{title}/voice` | `{on, say, has_question, source, ts}` — the session's voice flag plus its current spoken record. **404** unknown session |
| POST | `/api/instances/{title}/voice` | Body `{"on": true\|false}` → the same shape, read back after the write (never assumed). **On** writes `<flag dir>/<tmux session>.on` — `MINDFLOCK_VOICE_FLAG_DIR`, else `~/.mindflock-assistant/.voice`, keyed by the tmux session name the hook resolves (never by title) — and, for a CLI whose prompt hook injects the instruction itself (`provider.voice_prompt_hook()`: Claude), re-runs `install_activity_hooks` on the worktree so a session launched by an older build gets the voice-aware hook text; Claude Code hot-reloads its hooks file, no relaunch. Best effort: the flag is set either way. **Off** removes the flag file. The flag is the "a phone is in a voice conversation" marker (the row's `voice`): it asks nothing of the session — each spoken prompt asks for its own `Say:` line with `/send`'s `say` (the one-shot `.next`, also dropped with the session). It outlives the phone page and is dropped with the session, together with its reply file, so a namesake inherits neither. **400** `{error: "on must be true or false"}` when the body is not an object with a boolean `on` (a non-JSON body included), **404** unknown, **500** `{error}` when the flag file can't be written |
| POST | `/api/instances/{title}/voice/condense` | Condense the newest reply for a listener → `{text, source: "model"}`. **One** headless model call (`core.commit_message.pick_argv` + `_run`, the `POST /api/session-plan` pattern, cwd `$HOME` so no repo's `AGENTS.md` reaches a prompt whose answer is read aloud): the provider's newest assistant text, else the reply file, else the pane's visible screen — its tail (≤ 6000 chars) followed by *"Condense this for someone listening, not reading: at most two short plain sentences, no code, paths or markdown; include any question for them."* — asked of the session's own CLI (else the flock's default), 45 s timeout. The answer is markdown-stripped and cut at 600 chars. Never cached and never taken on a poll: this is the one voice step that costs money, and the phone sends it only for "summarize that". **409** `{error}` when there is nothing to condense (`nothing to condense — the session has not replied yet`) or no installed CLI, **504** when the CLI doesn't answer in time, **502** when it fails or returns nothing, **404** unknown |
| GET | `/api/instances/{title}/output?view=say` | The spoken record as an output view → `{view: "say", text, source, has_question, truncated: false, activity}` (`text` `""` and `source` `"none"` when there is nothing). The MindFlock MCP's `read_output` keeps its own view list and does not offer it |

The row fields `last_say` / `voice` ([`GET /api/instances`](#get-apiinstances))
and the `say` / `has_question` / `source` keys on `session.activity_changed`
(`new == "idle"`) and `session.turn_ended` ([Session
events](#session-events-websocket)) carry the same record; the two
pocket-mode pushes put it in the push body ([Addon routes](#addon-routes)).

**Server speech** (`backend/web/routes/speech.py` over `core/speech.py`). The
desktop app's mic always uses it (Electron's Chromium has no working Web
Speech recognition: every start fails with `network`); the phone page uses
its own engines (Web Speech recognition and `speechSynthesis`) unless
Settings → Voice says server (`voice.stt` / `voice.tts` = `"server"`;
[configuration.md](configuration.md#web-exposed-settings)). Every page
records **`audio/wav`**, the one clip format every backend accepts. Three
backend ids, each in a recognition (`stt`) and a voice (`tts`) flavour, in
preference order:

- `system` — **the operating system's own speech engine**
  (`core/speech_system.py`), first because it needs nothing installed: on a
  fresh Windows or macOS install it is what "the first ready one" picks.
  Windows (natively, or from WSL through `powershell.exe` interop): .NET
  `System.Speech` through Windows PowerShell — `SpeechSynthesizer` writes a
  WAV (`audio/wav` back), `SpeechRecognitionEngine` with a dictation grammar
  reads a WAV clip phrase by phrase (`?lang=en` picks an installed culture,
  `en` → `en-US`); scripts travel base64-encoded with every value a quoted
  literal, 20 s per call, files in the Windows temp dir (deleted after).
  A non-WAV clip is **502** *the built-in recognizer needs a WAV clip
  (audio/wav): reload the page*. macOS: `say` for the voice (WAV,
  16-bit 22.05 kHz) and Apple's Speech framework (PyObjC,
  `pyobjc-framework-Speech`, a main dependency on macOS) for recognition —
  on-device when supported, WAV or MP4/M4A clips, run in a short-lived helper
  process. Its one precondition is **Dictation** (System Settings → Keyboard
  → Dictation): the probe reads it with `defaults read
  com.apple.assistant.support "Dictation Enabled"` (or `com.apple.HIToolbox
  AppleDictationAutoEnable`) = 1, and when it is off — or a run fails with
  kLSRErrorDomain 201 *Siri and Dictation are disabled*, then remembered for
  a minute — recognition is not ready: *Turn on Dictation: System Settings →
  Keyboard → Dictation — macOS recognition uses it* (`system.detail`, and
  **502** from a transcribe that hit it). With Dictation on, macOS
  authorizes the server without a dialog; a denied / restricted status reads
  *needs permission: System Settings → Privacy & Security → Speech
  Recognition*. The voice (`say`) needs neither. Plain Linux has none (*no built-in speech engine
  on Linux: local models (Settings → Voice installs them) or an
  OpenAI-compatible host*). The platform is probed once per server process (voices,
  recognizer cultures), never per request; a failed probe is retried after a
  minute. `voice.tts_voice` is used only when it names an installed OS voice
  (`Microsoft Zira Desktop`, `Samantha`), else the OS default.

- `openai` — any OpenAI-compatible host with a key: `{stt_url}/audio/transcriptions`
  (multipart `file` + `model`, `voice.stt_model`, default `gpt-transcribe`) for
  recognition and `{tts_url}/audio/speech` (JSON `model` / `input` / `voice`,
  `voice.tts_model` default `gpt-4o-mini-tts`, `voice.tts_voice` default
  `alloy`, mp3 back) for the voice. The URL defaults to
  `https://api.openai.com/v1`; the model fields are free text because every
  host has its own names (Groq wants `whisper-large-v3-turbo`). Its readiness
  check only asks whether a key is set — no network. 30 s / 20 s timeouts.
- `local` — faster-whisper (`small`, int8 on the CPU, unless `voice.stt_model`
  names another size, for a phrase sent whole; the live transcript runs the
  ~5× faster `voice.stt_stream_model` — `base.en` for English, `base` for
  any other language) and piper (`en_US-lessac-medium` unless `voice.tts_voice`
  names another) on this machine, installed from Settings → Voice
  (`POST /api/voice/install-local` below; `uv sync --group voice` in a source
  checkout — the `voice` dependency group). Not installed reads *local models
  not installed: Settings → Voice installs them*. Both are imported lazily inside the call, never
  at server start; both are refused while the
  [memory guard](session-engine.md#memory-guard) reads low; models are
  downloaded from Hugging Face on first use into `~/.mindflock/voice/`
  (`$MINDFLOCK_VOICE_DIR`: `whisper/`, `piper/<voice>.onnx`). With
  `voice.stt_backend = local` the whisper models (and the accurate pass's,
  `voice.stt_final`, when it names a third) are **resident**: loaded
  ahead of use (each primed with one silent pass) 3 s after boot and again
  whenever a client fetches `GET /api/voice`, on a daemon thread, and never
  idle-unloaded while `local` stays configured (`unload_all` and the memory
  guard's refusal to load still apply); a piper voice is unloaded after 30
  idle minutes. webm/mp4
  clips decode through
  faster-whisper's bundled PyAV, so `ffmpeg` is not required (readiness still
  says when it is missing, as the first thing to try when a clip is refused).
  `local` recognition is the only backend that **streams** (the
  `/api/voice/stream*` routes below; `server_ready.stt_stream`).

**The voice is warm too.** 6 s after boot, and whenever a client fetches
`GET /api/voice`, the voice that would speak is warmed on a daemon thread
(`speech.warm_in_background("tts")`): `local` loads the piper voice and says
`Ready.` into nothing; `openai` on a **local** host (loopback, private
network, `.local`/`.ts.net`, plain `http` — Voice Lab, Kokoro-FastAPI) asks
it to synthesise `Ready.` with the configured voice and discards the audio,
so the host loads its model (a cloned-voice model takes ~40 s on a CPU; the
warm call may take 3 minutes) — a remote host is never called (a paid API,
and no cold start to hide); `system` runs the built-in engine's probe. A
host or the OS engine is warmed again at most every 10 minutes unless its
backend, model, voice or URL changed. A warm never blocks or raises;
`GET /api/voice`'s `warm` reports the last result per kind.

The configured backend (`voice.stt_backend` / `voice.tts_backend`) is the
only candidate when set — its own detail says what is missing; unset, the
first ready one is used. Settings are read on every call, so a save applies
at once. Any signed-in client may transcribe and speak **whenever a backend
of that kind is ready** — `voice.stt` / `voice.tts` only say which engine the
phone page prefers and no longer gate these routes (the old 409 *Server
recognition is off* is gone); only the owner clears a key. Engine calls run
on a worker thread, so a local model, a PowerShell round trip or a slow host
never stalls the event loop.

| Method | Path | Behavior |
|---|---|---|
| GET | `/api/voice` | Where speech runs and what the server offers: `{stt: "phone"\|"server", tts: "phone"\|"server", approve_dialogs, agent_words, ask_popup, backends: {stt: [ids], tts: [ids]}, server_ready: {stt, tts, detail, stt_backend, stt_detail, tts_backend, tts_detail, stt_stream}, system: {available, platform: "windows"\|"wsl"\|"mac"\|"none", detail, voices: [names], recognizers: [culture tags], stt_quality: "basic"\|"good"\|"none"}, stream_model, warm: {stt, tts}, words: {send, end, clear, stop, hush: [phrases], wake: str}, words_default: {…the same shape…}, pronounce: [{say, as}], final: {samples, typical_ms, p95_ms, expect_ms, timeout_ms} (how long `/end` has lately waited for the accurate pass over the last 20 phrases — the median and the 95th percentile, 0 before the first — `expect_ms` what `server_ready.final_ms` carries, and the server's own cap: wait for `/end` about that long, not a fixed worst case), configure_allowed}` — `approve_dialogs` is `voice.approve_dialogs`, `words` the effective voice keywords (`core.voice_words.effective`: the defaults overlaid with the person's `voice.words`, every kind present — see **Voice words** below) and `words_default` ours (`voice_words.DEFAULTS`, for Settings' reset), `pronounce` the person's own pronunciations (`voice.pronounce`, normalised by `core.spoken_text.normalize_pronounce`; `[]` when there are none) which every client applies first when it shapes a line for its own voice (`lib/spokenText.ts` `shapeForSpeech(text, pronounce)`, the phone page's `window.MindFlockSpokenText.shape`) — the server does the same inside `speak`, `agent_words` is `notifications.ntfy_agent_words`, `ask_popup` is `notifications.ask_popup` (the phone pops a session's question up when on), `backends` lists `system` first, `server_ready` the offline verdict on the saved fields (whether each kind has a ready backend, which, and why not; `detail` is the one-paragraph summary; `stt_stream` whether the ready recognition backend streams — only `local` does — so a client knows before `POST /api/voice/stream`), `stream_model` the faster-whisper model the live transcript runs for English (`"base.en"` by default; `""` when the ready backend does not stream), `warm` each kind's last warm-up (`"loaded"`, `"loading"`, `"skipped: <why>"` — not local, a remote host, not installed, memory low, the host failed — or `""` before the first), `system` the built-in engine's own report (`available` when it can recognise or speak; `voices` the OS voice names a Settings picker offers, `recognizers` the recognition cultures such as `en-US`, `stt_quality` a per-platform constant — `"basic"` on Windows/WSL, where the only recognizer that reads a recorded file offline is SAPI 5's desktop dictation, `"good"` on macOS — that the Settings screen uses to recommend the local models; on Windows/WSL `server_ready.stt_detail` for the `system` backend also ends with `basic accuracy — install local models for better recognition`), `configure_allowed` whether **this caller** may use the owner-only routes (`install-local`, `clear-key`; the same answer as `auth.may_configure`) so a page can disable those buttons — the route itself stays readable by everyone — no network, no model load, the built-in engine probed once per process. Readable by any signed-in client; the desktop app reads it for its mic, the phone page reads it when voice is turned on and when its ⚙ sheet opens, and treats a missing route as the defaults (phone, phone, approval on) |
| POST | `/api/voice/transcribe?lang=&hint=` | Recognise one recorded clip. `?hint=<words>` (≤ 80 printable characters) names words the clip likely contains — local faster-whisper's `initial_prompt`; the other backends ignore it. The **raw audio is the body** — not multipart — and `Content-Type` names its format: `audio/wav` (what every page sends; the only format the built-in Windows recognizer reads), `audio/webm`, `audio/mp4`, `audio/ogg` or `audio/mpeg` (a `;codecs=` suffix is fine). `?lang=` is a BCP-47-ish hint for the backend (`en`, `pt-BR`; anything else is dropped). → `{text, backend, ms}`. Status codes in the order they are checked: **415** `{error}` the body is not `audio/*`; **413** over 10 MB (from `Content-Length`, then the body itself); **400** an empty clip; **409** `{error}` when no recognition backend is ready (the detail says which and why — `voice.stt` is not consulted); **502** `{error}` when the backend failed (the built-in recognizer refuses a non-WAV clip this way); **200** |
| POST | `/api/voice/stream` | Open a **streaming recognition** (words while the person speaks; below): body `{"lang": "en-US"}` (optional; a BCP-47-ish tag, anything else dropped; Whisper gets the primary subtag) → `{id, ready: true, hints}` (`id` opaque; `hints` the number of characters of vocabulary this stream's accurate pass is biased towards — the fast tier gets none; `core.speech.recognition_hints`, below). **409** `{error}` when no recognition backend is ready (`core.speech.select`'s reason), when the ready one does not stream (`<id> recognition does not stream: send one clip per phrase` — the built-in and host backends: the client falls back to `/transcribe`, one clip per phrase), or when 4 streams are already open on this server |
| POST | `/api/voice/stream/{id}/chunk` | Feed the next stretch of audio: the **raw body** is `audio/wav`, 16 kHz mono 16-bit PCM (a few hundred ms to a few seconds; read with the stdlib `wave`, no PyAV) → `{committed, partial, ms, window_s, audio_s}`: `committed` the words newly committed by this pass (possibly `""`; type them — they never change; join non-empty pieces with one space), `partial` the current unstable tail (show it, don't type it), `ms` the inference time, `window_s` the seconds of audio this pass decoded (at most 8), `audio_s` the seconds of audio the window holds after it. A quiet chunk (RMS under 0.01) with no unstable tail runs **no pass**: `ms` and `window_s` are 0 and `partial` is `""` (the audio is kept, so a soft start of speech is decoded with the next chunk). Send the next chunk only after this one returned (a chunk that arrives during a pass waits for it, then gets its own). **404** unknown or expired id; **415** not `audio/wav` (also `audio/x-wav`, `audio/wave`) or a WAV of another rate / channel count / width; **413** over 10 MB; **502** `{error}` the pass failed (the chunk is kept for the next one) |
| POST | `/api/voice/stream/{id}/end` | Finish: one last fast pass over the held audio (skipped when nothing new arrived since the previous one and no tail waits), all of it committed, then the **accurate pass** over the whole phrase (below; usually started already, on the quiet chunk that ended the speech) → `{committed: <everything not committed before>, partial: "", final, final_ms, final_wait_ms, final_early, same}`: `final` the accurate model's text of the whole phrase — replace the phrase you typed with it — `final_ms` that pass's time, `final_wait_ms` how long this call waited for it (0 when the early pass was done), `final_early` whether it was the early one, `same` true when `final` is the fast words (everything committed) up to case, spacing and punctuation, so a client can skip a no-op retype. `final` / `final_ms` / `final_wait_ms` / `final_early` / `same` are **absent** when the accurate pass is off (`voice.stt_final = "off"`), skipped (under 0.2 s of speech, or a phrase over 60 s), heard nothing, failed or ran past 1.5 s of waiting: the fast text stands. The stream is gone afterwards. **404** unknown or expired; **502** the last fast pass failed (the stream is gone either way) |
| DELETE | `/api/voice/stream/{id}` | Abandon without a last pass → **204** (also for an unknown id) |
| POST | `/api/voice/speak` | `{text, voice?, shape?}` → the audio bytes, `Content-Type` the backend's (`audio/mpeg` for a host, `audio/wav` for piper and the built-in engine), `X-Voice-Backend` its id, `X-Voice-Cache` `hit` or `miss`, `Cache-Control: no-store`. `voice` (≤ 64 chars) overrides `voice.tts_voice` for this one call. The text is **shaped for the voice** first (`core.spoken_text.shape`): the person's `voice.pronounce` rows, then markup dropped, paths and files spoken (`backend/web/core/speech.py:404` → *backend, web, core, speech dot pie line 404*), versions / PR numbers / durations / units said the engineers' way (`v0.10.2` → *version 0 point 10 point 2*, `PR #275` → *P R 275*, `1m23s` → *1 minute 23 seconds*), identifiers split into words (`askMicTap` → *ask mic tap*), the tech table (`SQL` → *sequel*, `JSON` → *jayson*, `k8s` → *kubernetes*) and the heteronyms by context (*everything goes live* → *lyve*, the adjective; *I read it* → *red*) — the caller's text, what a caption shows, is never changed; `shape: false` sends it as written (Settings → Voice's **Try** hears a respelling itself). A repeat of the same shaped text in the same voice is a **cache hit**: an in-memory LRU of 64 clips / 8 MB keyed by voice and shaped text, dropped whenever the backend, its host, model or voice setting changes, so "Sent.", "Listening." and a session's name cost a neural host (≈ 1 s a line) once. **400** no text (or nothing left once shaped), **413** over 1200 chars as sent (a `Say:` line is ≤ 320, a dialog read-out a few hundred), then **409** / **502** as above |
| POST | `/api/voice/install-local` | One click for the accurate offline engine (`backend/web/core/speech_install.py`): in a background thread, (1) `installing packages` — `uv pip install --python <server interpreter> faster-whisper piper-tts` (`uv` on PATH, else beside the interpreter, else `<interpreter> -m pip install`); (2) `downloading recognition model (<name>)` — the faster-whisper model of `voice.stt_model` (`small` unless it names a faster-whisper size) into `<voice dir>/whisper`, the cache the `local` backend loads from, so the first phrase is not a download, then `downloading streaming model (<name>)` — the live transcript's (`voice.stt_stream_model`, `base.en` by default, ~75 MB; skipped when it is the same model); (3) `downloading voice (<name>)` — the piper voice of `voice.tts_voice` (`en_US-lessac-medium` unless it names a piper voice) into `<voice dir>/piper`. Steps 2 and 3 run in a helper process of the same interpreter. On success `voice.stt_backend` becomes `"local"` (the voice backend is left as it was) and the import caches are dropped so `GET /api/voice` reports `local` ready without a restart. **The packages survive a release:** step 1's success is recorded in `~/.mindflock/voice/packages.json` (`{version: 1, packages: ["faster-whisper", "piper-tts"], installed_at, python}`, under `MINDFLOCK_HOME`), the engine's own updater and `mindflock update` carry each recorded package through the rebuild the way the Database explorer's drivers ride (`--with` on the uv line, `pipx inject` after pipx), and a rebuild that did not carry them (install.sh, a hand-run `uv tool install --force`) is healed: at boot and on every `GET /api/voice` the server reinstalls the recorded packages when `faster_whisper` / `piper` no longer import — once per process, as this install's step `reinstalling the local models' packages after the update` (packages only; the models on disk are untouched), so the Settings screen follows it like a click. A heal whose package no longer exists on the index drops the record. `mindflock doctor` lists the record as *Local speech models*. → **200** the status below (`state: "running"`); **403** not the owner (`auth.may_configure`); **409** the status with `error` when an install is already running |
| GET | `/api/voice/install-local` | The install's progress: `{state: "idle"\|"running"\|"done"\|"failed", step: "installing packages"\|"reinstalling the local models' packages after the update"\|"downloading recognition model (small)"\|"downloading streaming model (base.en)"\|"downloading voice (en_US-lessac-medium)"\|"done"\|"", log: [the last 20 output lines], started, finished (epoch seconds or null), error}`. A step that exits non-zero, cannot start or runs past 30 minutes fails the install with `error` = the step and its last output line; the settings are then untouched. Per server process (a restart forgets it). Readable by any signed-in client |
| POST | `/api/voice/wake?lang=` | Is this short clip the wake word on its own — `voice.words.wake`, **"MindFlock"** by default? Body and `Content-Type` exactly as `/transcribe`; the clip is recognised with the word as the decoder's hint (`MindFlock` for ours, else the word capitalised: `Hey Jarvis`) — on `local` by the fast streaming model (`base.en`, greedy, no VAD: ~0.6 s for a 1 s clip where `small` takes ~1.8 s), other backends their `/transcribe` — and judged by `core.wake.is_wake` (the word alone, in at most one token more than it has, two letters of slack for a word of 8+ letters — how whisper hears a made-up word: *Mind flock*, *Mindlock*, *mind block* — one for 5–7, none below; a sentence never wakes, however it starts) → `{wake: bool, text, word: "mindflock" (the effective word), backend, ms}`; `/transcribe`'s status codes. The clients keep the mic in a standby that watches the sound level and send only a short lone utterance bounded by silence, so the model sees a second of audio at a time, never the room |
| POST | `/api/voice/say-all` | `{"on": bool}` → `{say_all: bool}` (the state read back) — ask **every** prompt in **every** session for its `Say:` line, or stop. A client whose replies choice is *Everything* turns this on: the per-prompt `say` on `/send` only reaches prompts the app sends, and a prompt typed straight into a terminal would otherwise end with no spoken line (the listener hears a digest). It is the standing `<voice dir>/say-all` marker the Claude prompt hook honours like a young `.next` (printed on every `UserPromptSubmit` while it exists; a `.next` beside it is still consumed, and the line prints once), and `/send` appends the instruction for the other CLIs. Turning it on re-pins every live Claude session's hooks (so one launched by an older build learns the marker); the server does the same at boot while it is on. `GET /api/voice` reports it as `say_all`. Owner only (**403**); **400** without a boolean `on`; **500** when the marker can't be written |
| POST | `/api/voice/help` | `{"text": "<a question about MindFlock>"}` (the router's `help` answer carries it) → `{say, source: {doc, heading} \| null, model: bool}` — answered from `docs/*.md` (`core.voice_help`): the docs split at every heading into passages of a few paragraphs (a table row on its own, code dropped), ranked BM25-style over the words plus the headings (the index is built once per process and rebuilt when a doc's mtime or size changes); the best four go to ONE headless model call (`claude --model claude-haiku-5-5 -p`, MCP servers stripped, the neutral one-shot dir, 20 s) told to answer in at most two plain spoken sentences or say *I don't know*. `say` names the section (`"From Fast-track: …"`); `model` false when the model was missing, failed, timed out or said nothing — then `say` is the best passage's first two sentences, markdown stripped. Nothing matching → `"I couldn't find that in the MindFlock docs."`; the model saying it doesn't know → `"I don't know — the MindFlock docs don't say."` (both `source: null`); no docs folder at all (an installed copy run away from its checkout) → `"I can't find the MindFlock docs on this computer."`. Answers are kept per question (64 per process), so asking again costs nothing. **400** `{error}` without a non-empty string `text` (cut to 400 characters). Readable by any signed-in client |
| POST | `/api/voice/clear-key` | `{"kind": "stt"\|"tts"}` → `{ok: true, kind}` — forget a saved API key. `POST /api/settings` keeps a secret whenever its field arrives blank (the app-wide convention, so a form resubmit never wipes a token), which leaves no way back to *no key*: this is it. Owner only (`auth.may_configure`, **403**); **400** any other `kind`; **500** when the settings file can't be read |

**Streaming recognition** (`backend/web/core/speech_stream.py`): Whisper is
not a streaming model, so each stream keeps the audio since its last
committed word and re-transcribes that growing window on every chunk
(greedy, word timestamps, the last ~200 committed characters as the
prompt — and nothing else: see the vocabulary hints below) with the
streaming model (`voice.stt_stream_model`, `base.en` for
English), committing the words two consecutive passes agree on — the
longest common prefix, compared lower-cased without punctuation, with the
current pass's casing — while the unstable tail waits (the "LocalAgreement-2"
policy of whisper_streaming). Past 4 s of held audio the window drops what
lies before the last committed word (keeping 0.3 s), never audio under an
uncommitted word, and **no pass decodes more than 8 s**: before a pass, the
unsettled tail words that start before the last 8 s are committed as they
leave the window (the one forced commit). A quiet chunk with nothing
unsettled runs no pass at all; streams expire 60 s after their last chunk (one sweep
timer, no thread per stream), and `/transcribe` is unchanged.

**Vocabulary hints** (`core.speech.recognition_hints`, built on every
`POST /api/voice/stream` so a new session's title counts at once): one
comma-separated string of at most 400 characters — the wake word and every
voice phrase of every kind first (`voice.words`: *over*, *over and out*,
*disregard*, *stop*… — these must be heard right), then up to 12 live
session titles spelled as they are said (`sitecheck-bot7` → *sitecheck bot
7*; split on `-`/`_`/`/`, camel-case and letter/digit boundaries; the most
recently active sessions first), then a fixed MindFlock vocabulary
(*MindFlock, worktree, pull request, autopilot, Claude, Codex, pytest*…).
The **accurate pass** gets it as faster-whisper's `hotwords` (1.1+; an
older faster-whisper gets it at the front of the `initial_prompt` instead —
detected from the `transcribe` signature, never a crash), and never the
fast words (those may be the wrong ones). The **fast tier gets none**: its
cost is the decoder's, and the bench puts 400 characters of `hotwords` on
`base.en` at 1.24 s a pass against 0.43 s bare, while even 120 characters
of voice words leading its prompt trip whisper's temperature fallback often
enough to triple the median pass (1386 ms against 467 ms) — and the clients
read the radio words off `final` anyway. `/stream`'s `hints` is the
string's length, for diagnostics. A word a pass places in audio that was
never loud (more than 0.4 s after the last loud 100 ms frame) is dropped:
whisper fills a quiet tail with "Never." or "for.".

**Two tiers: fast while speaking, accurate at the end.** The passes above
are the fast tier — what the client types as the person speaks
(`base.en`: ~0.5 s a pass, but it mishears: *run the tick* for *run the
tests*, *Top* for *stop*). Beside the window, a stream keeps the **whole
phrase's audio** (at most the last 60 s), and decodes it once more with the
accurate model — `voice.stt_final`, by default the one-shot `stt_model`
(`small`): greedy, the hints as `hotwords`, no prompt, the VAD on — and
`/end` answers `final`, the text the client corrects the typed phrase to.
Both tiers come from the recognition backend: `Backend.stream_transcriber(lang, hints="")`
and `Backend.final_transcriber(lang, hints="")` (`core/speech.py`; only
`LocalSTT` implements them). A custom `Backend` subclass overriding either
must accept the `hints` keyword — the server always passes it to
`final_transcriber`, `""` to the fast tier.
Whisper's encoder costs the same for one second of speech as for thirty,
so on this box the pass takes ~1.1–1.3 s on `small` whatever the phrase's
length — which is why it **starts early**: a quiet chunk (RMS under the
silence level) means the person paused, usually for good, so the pass
starts right then on the phrase so far, while the fast tier still settles
the last word and the client decides what to do with it. `/end` takes that
result when no speech arrived after it started (`final_early: true`,
`final_wait_ms` what it still had to wait) and runs a fresh pass otherwise
(a pause mid-sentence costs one wasted pass). `/end` also skips its last
fast pass when nothing loud arrived since the previous one and no tail
waits. The accurate pass has 1.5 s of waiting (`FINAL_TIMEOUT_S`): the
bench's `/end` wait is 0.7 s at the median and 1.35 s at p95, against 2.0
and 6.5 s before the early start; past that, or when it fails, the fast
text stands. It is skipped for under 0.2 s of speech (100 ms frames louder than
the silence level — two frames, so a lone *stop* still gets corrected and
only a click is skipped) and for a phrase longer than 60 s (the final would
not cover the words already typed). `GET /api/voice`'s
`server_ready.final_ms` (and `final.expect_ms`) is what to budget for
`/end`: the p95 of the last 20 measured waits once there are three, the
bench's 1.4 s before — the phone and the desktop seed their wait from it.
`tests/bench/voice_bench.py` (run with the tool venv's python, which has
faster-whisper and piper; not collected by pytest) is the reproducible
measurement behind these numbers: 48 piper-synthesised command phrases
through a real `Stream`, chunked like the clients. With the hints the
accurate pass hears the control word in 46 of 48 (41 before) at 4.4 % WER
(10.3 % before); the fast tier, bare, 32 of 48 (25 before, the silence
filter's doing).

**The voice router** (`backend/web/routes/voice.py` over the pure
`core/voice_router.py`) — one server route that decides whether a finished
phrase is words for the session or a command, so the phone page and the
desktop mic share one grammar (the phone keeps its own copy, mobile.js
`VOICE_CMD`, only as an offline fallback). The default is that the person is
still talking to the session they are on: anything that is not plainly a
command is `{"kind": "prompt"}`, and every pattern matches the WHOLE phrase —
a false command is worse than a missed one.

`POST /api/voice/route` with `{"text": "<one finished phrase>", "session":
"<the title being talked to, or ''>", "title": "<optional: the client's
mapping of a numbered target>", "pending": <optional: the last answer's
`pending`, sent back — see **Asking back**>}` → **200** with one of the shapes below
(**400** `{error}` only for a body that is not an object; an unknown
`session` reads as none). Matching is case- and punctuation-insensitive
(`voiceNorm`: lowercase, anything but letters, digits and `'` a space), and a
leading `ok`/`okay`/`hey`/`please` and a trailing `please` are dropped.

| Phrase | Answer |
|---|---|
| anything else, or blank | `{"kind": "prompt"}` — send it to the session (`/send`). No session rows are read. |
| `(switch\|go\|change\|jump\|hop)( me)? (over )?to <name>`, `(take\|bring) me (over )?to <name>`, `(put\|get) me (on\|onto\|in) <name>`, also `(focus\|open\|show\|select) (over )?to <name>` | `{"kind": "switch", "title", "name", "say": "Now on <name>, <state word>[: <task_headline>]. [<its last_say>]"}` — the last reply only while it is news: the row is `idle` and its `activity_since` (the turn's end) is at most 120 s old (`FRESH_REPLY_S`); else just where you are. `<name>` is matched against each row's display `name` and its title (`find_sessions`): the words around a name are dropped (`the`, `my`, `a`, `session(s)`, `window`, `one`, `called`; `bot` / `agent` when they match nothing), and every word left must name a word of the name — across hyphens, underscores and letter/digit runs (`sitecheck` ↔ sitecheck-bot7, `bot seven` ↔ bot7, `site check` ↔ sitecheck), a plural, its start (3+ letters) or one wrong letter (5+) allowed; one session's whole name said wins, then whole words over starts; when no row matches every word, the unique row matching most (at least half) of them. A tie → `{"kind": "ambiguous", "say": "Which one: <A> or <B>?", "candidates": [{title, name}], "pending": {"kind": "pick", "candidates": […]}}`; nothing → `{"kind": "no-session", "say": "No session called <words>. Sessions: <A>, <B>, <C>."}` (at most 6 names; `"There are no sessions."` with none). After a polite lead (`can you`, `could you`, `let's`, `I want to`, …) a miss is `{"kind": "prompt"}`. Other devices' `<device>::<title>` rows count — the client selects the title it gets. |
| `(open\|show( me)?\|focus( on)?\|select\|pull up\|bring up\|talk to\|go back to\|back to) <name>` | The same `switch` / `ambiguous`, but only for a session whose whole name or title is said, or its start in whole words (`open sitecheck`); with `session` / `window` said (`pull up the settings session`) any match as above. No match → `{"kind": "prompt"}` (`open the readme`, `show me the logs`, `go back to the old approach`). |
| `what did <it\|name> (say\|reply\|answer\|write)`, `what was (the\|its) (last )?(reply\|answer\|message)`, `read (me )?(it\|that\|it out\|the (last )?reply\|its (last )?reply\|what it said)` | `{"kind": "read", "title", "name", "say": "<name> said: <its last_say>"}` — whatever its age (`"<name> is still working. Before that it said: …"`, `"<name> hasn't said anything yet."`, `"No session is open."`). A name only as `<name>, yes` takes one (whole name or its start); an unknown one is a prompt. |
| `(what\|which) sessions (do i have\|are (up\|open\|running))`, `list (the \|my )?sessions`, `what('s\| is) (up\|open\|running)` | `{"kind": "list", "say": "Sessions: <A> (working), <B> (waiting on you), <C> (idle)."}` — at most 10, then `, and N more`. |
| `(what\|who) needs (me\|attention\|my attention)` | `{"kind": "needs", "say": …}` — the rail bell's attention set (a question, a failed setup or pre-commit, failing checks, an offline terminal, idle 20 min on uncommitted work), most urgent first, three at most: `"<A> is asking: <its asking.text, else its last_say>"` (a question asked in prose counts — its row's `asking.kind == "question"` reads as `Question` here and in `status`/`list`) / `"<B> is waiting on failing checks."` / `"Nothing needs you."` |
| `status`, `what's it doing`, `what's happening`, `what's going on` | `{"kind": "status", "say": "<name> is working for 5 minutes. <its last_turn>"}` — the CURRENT session (`"No session is open."` / `"<title> is not listed."`). |
| `(switch\|go\|…) (over )?to [session\|window\|number] <N>`, `(switch\|go\|…) to the <Nth> (one\|session\|window)`, `session <N>`, `number <N>`, `window <N>`, `the <Nth> session` | `{"kind": "switch_index", "index": N}` — 1-based, in the CLIENT's numbering (the desktop's rail order, the phone's list order), so the client maps it. `<N>`: `one`…`twenty` or digits; `<Nth>`: `first`…`tenth` or `3rd`. `the <Nth> one` alone is this too — unless the session being talked to is asking (`activity: clarify`), when it picks that option (an `answer`, below). |
| `next (session\|one\|window)`, `(previous\|last) (session\|one\|window)` (optionally after `(switch\|go\|…) to the`), `go back` | `{"kind": "switch_rel", "delta": 1\|-1}` — the client moves along its own order. |
| An answer to a dialog — bare: `yes`, `no`, `always allow`, `approve`, `deny`, `allow it`, `go ahead`, `option two`, `the second option`, `answer <answer>`, `tell it <answer>`; targeted: `<target>, <answer>`, `<answer> to <target>`, `(answer\|say) <answer> (to\|on\|for) <target>`, `tell <target> (to )?<answer>` | `{"kind": "answer", "choice", "number"?, "words", …}` — see **Answers** below. |
| `what('s\| is) <it\|target> asking`, `what does <it\|target> want`, `what's the question`, `read (me )?the question (again)?`, `read that again` | `{"kind": "question", "title", "name", "say": "<name>: <the dialog's question, what it runs and the choices>"}` — the dialog read out the phone's way (`voiceDialogSpeech`); with no parsed dialog the dialog's question line (a question asked in prose included: `/dialog` serves it as `kind: "question"`), else the session's `Say:` line when it `has_question`, else `"<name>: Nothing is being asked."`. A numbered target → `{"kind": "question", "index": N}` (map it, send `title`); no session → `{"kind": "question", "say": "No session is open."}`. `what did it say` is `read` (above). |
| A question about MindFlock itself: question-shaped (`how (do\|does\|can\|…)`, `what`, `where`, `which`, `why`, `when`, `can i`, `is there`, `is it possible`, `explain`, `tell me about`) AND either said after `mindflock` / `hey mindflock` / `help`, or naming MindFlock (`in mindflock`) or one of its features (sessions, templates, schedules, fences, zones, autopilot / Fast-track, team runs, devices, the phone page, voice, tickets, PRs, worktrees, settings, peers, extensions, the rail, notifications, the code map, …) with no word of the agent's work (`fix`, `bug`, `test`, `code`, `file`, `error`, `this`, `my project`, …), the session's state (`doing`, `said`, `status`, `how many`, …) or `you` (but the generic `how do you`) | `{"kind": "help", "text": "<the question, normalised, the mindflock/help lead dropped>"}` — the client says *Looking that up…* and asks [`POST /api/voice/help`](#voice-talking-to-a-session-from-the-phone) `{text}`, then speaks its `say`. `how do I fix the login bug` stays a prompt; `mindflock, how do I fix the login bug` is help. Checked after `question`, before the answers. |
| `[I want to\|can you\|let's\|…] [start\|open\|create\|make\|spin up\|launch\|give me\|I want\|…] [a\|another] new (session\|agent)` or `another session` or `spin (one\|another one\|a new one) up` — then `[called\|named <name>] [in\|for\|on <repo>] [to\|and\|that <task>]`; a plain `a session` with a verb and one of those (`create a session to fix the login bug`); the bare `new session <task>` | Creates a session → `{"kind": "new", "title", "name", "say": "Started <name> in <repo name>."}`; or asks first → `{"kind": "ask", "say": "In which project? …" \| "What should it do in <repo>?", "pending": {"kind": "new", …}}`; or `{"kind": "new-failed", "say"}`. See below. `start a session` alone is a prompt. |
| `cancel`, `never mind`, `scratch that`, `forget it` / `stop`, `stop (listening\|recording\|dictating\|the mic\|the microphone)`, `mic off`, `turn off the mic`, `pause listening`, `that's all`, `done talking` / `stop talking`, `stop speaking`, `stop reading`, `be quiet`, `quiet`, `hush`, `shut up`, `(that's) enough` / `voice off` / `say that again`, `repeat` / `summarize (that)`, `summary` | `{"kind": "cancel"\|"stop"\|"hush"\|"off"\|"repeat"\|"condense"}` with no `say` — the client acts on its own (`stop`: it stops listening; typed text stays; `hush`: it cuts what is being said and drops everything queued, and keeps listening if it was). The `stop` and `hush` phrases are the person's `voice.words` (**Voice words** below); those listed are the defaults'. |

Every `say` is a sentence the client speaks as is. Rows are
[`GET /api/instances`](#get-apiinstances)'s own (`name`, `activity`,
`activity_since`, `last_say`, `last_turn`, …), read only once the phrase is a
command; rows hidden off the rail (`hidden_by`) are never named. Words that
are not one of these forms stay a prompt: `yes, but run the tests first`,
`stop the server`, `go back to the old approach`, `the answer is no`.

**Asking back.** Two answers ask something and carry `pending`, which the
client keeps (≈30 s: the phone's `VOICE_PENDING_MS`, the desktop's
`PENDING_MS`) and sends back verbatim with the next phrase; any answer
without a `pending` drops it.

- `ambiguous` → `{"kind": "pick", "candidates": [{title, name}]}` (at most
  6, in the order read out). The next phrase is a pick when it names one:
  its place (`the first one`, `second`, `number two`, `the last one`; past
  the end → `ambiguous` again, `"There are 2. Which one: …"`), its state
  (`the one that's working`, `the idle one`, `the one asking`; none →
  `"None of them is working. Which one: …"`), or a fragment of its name
  (`copy two`, `bot seven`; `I mean` / `go to` before it dropped) — a tie
  among candidates goes to the one the words END, and a fragment still
  shared asks again, narrowed. The pick is a `switch`. A candidate no longer
  listed is ignored. Anything else is routed as usual (`never mind` is
  `cancel`, `switch to docs` a switch, `fix the test` a prompt). A pick
  comes before Claude's question tool's answer (`two` said to a session
  showing one).
- `ask` (a new session) → `{"kind": "new", "await": "repo" | "task",
  "name", "task", "choices"?: [repo names as read out], "repo_name"?,
  "repo_path"?}` — the fields gathered so far. Awaiting the **project**: a
  known repository the words name (`parser`, `the sitecheck repo`, `site
  check bot six`), `this one` / `the same project` (the current session's),
  `the second one` (in `choices`), or the project and then the task
  (`parser, add tests`); anything else asks again (`"I don't know a project
  called <words>. In which project? …"`), but a command (`switch to docs`,
  `what needs me`, `stop listening`) is routed as usual. Awaiting the
  **task**: the phrase is the task as said (`to` / `it should` / `have it`
  before it dropped); `nothing`, `no task`, `just start it` start it with
  none; only `stop` / `hush` / `voice off` are commands. Both: `never mind`,
  `cancel`, `don't start it` → `{"kind": "new-failed", "say": "Okay, no new
  session.", "cancelled": true}`. A `repo_path` this server does not know
  (the current session's, or a known repository) is not trusted — the
  project is asked for again. Once nothing is missing the session is
  created as below and the answer is `new`.

```json
POST /api/voice/route {"text": "switch to the sitecheck session", "session": "docs"}
→ {"kind": "ambiguous", "say": "Which one: sitecheck-bot7 or sitecheck-bot7-copy-2?",
   "candidates": [{"title": "sitecheck-bot7", "name": "sitecheck-bot7"}, …],
   "pending": {"kind": "pick", "candidates": […]}}
POST /api/voice/route {"text": "copy two", "session": "docs", "pending": {"kind": "pick", "candidates": […]}}
→ {"kind": "switch", "title": "sitecheck-bot7-copy-2", "name": "sitecheck-bot7-copy-2",
   "say": "Now on sitecheck-bot7-copy-2, idle."}

POST /api/voice/route {"text": "start a new session", "session": "docs"}
→ {"kind": "ask", "say": "In which project? MindFlock, sitecheck-bot5 or parser?",
   "pending": {"kind": "new", "await": "repo", "name": "", "task": "", "choices": […]}}
POST /api/voice/route {"text": "parser", "session": "docs", "pending": {…}}
→ {"kind": "ask", "say": "What should it do in parser?",
   "pending": {"kind": "new", "await": "task", …, "repo_name": "parser", "repo_path": "/r/parser"}}
POST /api/voice/route {"text": "add tests for the queue", "session": "docs", "pending": {…}}
→ {"kind": "new", "title": "add-tests-queue", "name": "add-tests-queue",
   "say": "Started add-tests-queue in parser."}
```

**Numbers are the client's.** `switch_index` / `switch_rel` and a numbered
target (`session three, yes`, `what is number 2 asking`) carry `index` /
`delta` and no `say`. To finish one, the client maps it to a title and calls
again — either the same `text` with `"title": "<that title>"` (a switch then
comes back as the full `switch` answer with its `say`; an answer or question
as below), or the answer's own `words` with `"session": "<that title>"`. No
other route is needed.

**Answers.** An answer is a phrase made only of answer words (yes, yeah, yep,
yup, sure, ok/okay, approve, allow, go ahead, do it, no, nope, deny, reject,
don't, skip, cancel, stop, always, don't ask again, … — any other word and
it is a prompt), or `option <N>` / `the <Nth> one`. `choice` is what it
says, the phone's order (mobile.js `voiceParseAnswer`): `always` only for a
whole-answer always form (`always`, `yes, always`, `always allow that`,
`yes and don't ask again`), else `no` for a NO anywhere (`no, not always`,
`no, option 2`), else `yes`, else `number` with `number: N`. `words` is the
answer part as heard (normalised, the target removed). The **target** is the
session being talked to (bare forms, `it`, `the agent`), a number (`session
<N>`, `number <N>`, `window <N>`, `the <Nth> session` → `index: N`, see
above), or a name. After `tell` / `answer … to` a name is matched like
`switch` (a tie → `ambiguous`, nothing → `no-session`); in `<name>, yes` and
`yes to <name>` only a session's whole name or title, or its start in whole
words (`invoice` for invoice-parser), counts, and an unknown name leaves the
phrase a prompt (`update the docs, yes`).

For a resolved local session the server reads its live dialog — the same
body [`GET /api/instances/{title}/dialog?quiet=1`](#another-agents-view-output--answer)
serves — and adds:

- a parsed dialog: `"dialog_id"` and `"keys": ["<key>"]`, the option the
  phone's `voiceParseAnswer` picks (permission dialogs take words only —
  numbers never pick one, and `always` without an always option is the yes;
  dialogs of `other` options take a number, by the option's key), with
  `"say": "Answered <yes|no|always|the option's label> on <name>."`. When
  nothing is picked: `"keys": []` and `"say": "I didn't catch that for
  <name>: say yes, no, or always."`.
- no parsed dialog: `"no_dialog": true`, `"say": "<name> is not asking
  anything."` (`"… is waiting on you: open the screen."` for a dialog it
  can't parse; `"… asked in words, not a dialog: switch to it and say your
  answer."` for a question asked in prose — `/dialog`'s `kind: "question"`;
  `"… is on another device: answer it there."` for a
  `<device>::<title>` row, whose dialog is never read here). A BARE answer
  with no parsed dialog is `{"kind": "prompt"}` instead — the agent may have
  asked in words, and `yes` reaches it typed, as before.

Nothing is pressed here: the client posts `{"keys", "dialog_id", "by":
"user"}` to [`/answer`](#another-agents-view-output--answer) — it owns
`approve_dialogs` — and speaks the `say` once that succeeds. Example:

```json
POST /api/voice/route {"text": "docs, always", "session": "parser"}
→ {"kind": "answer", "title": "docs", "name": "API docs", "choice": "always",
   "words": "always", "dialog_id": "a1b2c3", "keys": ["2"],
   "say": "Answered always on API docs."}

POST /api/voice/route {"text": "session three, yes", "session": "parser"}
→ {"kind": "answer", "choice": "yes", "index": 3, "words": "yes"}
POST /api/voice/route {"text": "session three, yes", "session": "parser", "title": "docs"}
→ (the full answer for docs, as above)
```

**`new`**: examples — `new session to fix the login bug`, `open a new
session called parser in mindflock`, `start a new session in sitecheck and
add tests for the queue`, `new session`. The **repository** is the one
named, when the words after `in`/`for`/`on` (or their longest leading run —
`in mindflock fix the tests` is repo `mindflock`, task `fix the tests`) name
one MindFlock knows: what the New dialog suggests
(`GET /api/repos/suggest`) plus every local session's
repository, by folder name with spacing, case and punctuation ignored
(`site check bot 5` is `sitecheck-bot5`; an unambiguous start like
`sitecheck` picks the most recent match; number words count, `bot six` is
`bot6`), or a known one the task ends on (`create a session to fix the login
bug in mindflock`). Words after `for`/`on` that name no repository are the
task (`new session for the login bug`); after `in` they were meant as a
repository and nothing is guessed: `ask` with `"I don't know a project
called <words>. In which project? …"`. Named none → the only one known, else
`ask` `"In which project? <the current session's first, then the most
recent, at most 6>?"`; none known → `"I don't know any projects yet: open
one from the New dialog first."` (`new-failed`). No task → `ask` `"What
should it do in <repo>?"`. The **title** is the `called` name hyphenated (≤ 32 chars), else
the first four words of the task named the New dialog's way
(`title_from_prompt`: `fix the login bug` → `fix-login-bug`), else
`voice-session`; made unique with `-2`, `-3`… against every session and
rename (`core.session_create.free_title`). The session is created exactly
like the New dialog's (`core.session_create.create_result`, the same code as
`POST /api/instances`, so the provider, branch, launch flags and plan-first
are the defaults), with the task as its first `prompt` (seeded or queued as
that route decides), and its **voice flag is turned on** so its first reply
ends with a `Say:` line. Only local sessions are created. A refused create
→ `{"kind": "new-failed", "say": "Couldn't start it: <its error>."}`, still
200; a create that fails later in the background is reported the way every
create's is (`session.create_failed`, [`GET /api/create_failures`](#get-apicreate_failures)).

### Voice words

The keywords every voice client listens for are **ours by default and the
person's own once changed** in Settings → Voice: the setting `voice.words`
(synced across their devices, owner-only through `POST /api/settings`
`{"voice": {"words": {...}}}`; `{}` = all defaults). `GET /api/voice`
serves the effective set as `words` — every kind present, plus `heard` (below) — and every client
builds its matchers from it:

| Kind | Default | Who acts on it |
|---|---|---|
| `send` | `over` | the client: types the pending words and presses Enter, keeps listening |
| `end` | `over and out`, `over, out` | the client: sends the pending words and stops listening |
| `clear` | `disregard` | the client: drops the pending words |
| `stop` | `stop`, `stop listening`, `mic off`, `pause listening`, `that's all`, `done talking` | the router (`stop`): the client stops listening, typed text stays |
| `hush` | `stop talking`, `be quiet`, `quiet`, `hush`, `shut up`, `enough` | the router (`hush`): the client stops speaking, keeps listening |
| `wake` | `mindflock` (one string) | `POST /api/voice/wake` |

Each phrase is lower case, 1–40 characters of letters, digits, spaces,
apostrophes and commas, at most 6 per kind; `wake` is one or two words of
letters, 3–20 characters. A save is normalised (`core.voice_words.normalize`):
anything invalid is dropped, duplicates collapse, and a kind left empty — or
equal to ours — is not stored, so it follows the default.

**What the recogniser writes** (`words.heard`). Whisper does not always
write the word that was said — "over" comes out as *ova*, "disregard" as
*dis regard*, "over and out" as *over an out*, "mindflock" as *mind lock* —
so `words` also carries `heard: {send: [...], end: [...], clear: [...],
stop: [...], hush: [...], wake: [...]}`: for each kind, the forms the
recogniser is known to write for the phrases in its list
(`core.voice_words.HEARD`, a curated table keyed by phrase — every entry a
mishearing seen in practice and *not* a plausible last word of dictation, so
*hover* and *ever* are not "over"), at most 24 per kind, empty for a kind
made only of the person's own phrases. The forms follow their phrase: drop
"quiet" from `hush` and *quite* goes with it; move it to `clear` and *quite*
clears. Every client folds `heard` into its matchers beside the words (the
desktop's `lib/voiceWords` `heardOf`, the phone's `voiceWordsWide`) and the
router hears them as the phrase (`stop listing` stops, `be quite` hushes);
`POST /api/voice/wake` takes the wake word's forms. `heard` is derived, never
saved: `normalize` drops it, and `words_default` has none.

**A letter off** (`core.voice_words.sound_key` / `sounds_like`). A phrase
the person added has no table, so every client and the router also take a
phrase heard a little off — *rodger* for "roger", *pipe town* for "pipe
down", *scrap dat* for "scrap that" — by one rule, implemented identically
in Python, the desktop (`lib/voiceWords.ts` `soundKey` / `soundsLike`) and
the phone (`mobile.js` `voiceSoundKey` / `voiceSoundsLike`) and pinned by
the shared fixture `tests/unit/data/voice_sounds.json` (pytest and vitest
both run it): the letters of what was said and of the phrase, run
together, must be within a margin of edits that shrinks with the phrase —
two from eight letters, one from five, none below, so a four-letter word
("over", "stop", "halt") is only ever itself or a `heard` form — *and* share
a compact metaphone-like key (voiced consonants folded onto unvoiced,
vowels after the first letter dropped: "over" → `ofr`, "hover" → `hfr`,
"oven" → `ofn`), one key edit allowed from five key letters; a phrase of
eight letters or more also takes a same-key homophone one letter further
off (*sight check* for sitecheck). It is a WHOLE-phrase comparison, only at
the end of an utterance (the send / end / clear words, with their tail
word) or as the whole utterance (the hush words; the router's one-word
`stop` / `hush` commands): a sound-alike never fires mid-sentence or with
more words after it — *over the bridge*, *this regard is important*, *the
summit meeting* are dictation. The stop phrases at the end of dictation
(`stop listening`, `mic off` …) stay word for word (the words or a `heard`
form). `core.voice_words.control_kind(text, words)` is the reference
reading of one finished phrase that both clients' matchers follow.

**The regex contract** (`voice_words.phrase_regex(phrases)`): one
non-capturing alternation, longest phrase first, each escaped, a comma
optional — `["over and out", "over, out"]` → `(?:over and out|over,? out)`.
No anchors, flags, lookbehind or named groups: the same source compiles in
Python's `re` and in JavaScript (`new RegExp("^" + src + "$", "i")`, with or
without `u`); no phrases → `(?!)`, which never matches. The router keeps a
default phrase's spoken variants while that phrase is in the list (`stop
listening` also hears *stop recording / dictating / the mic / the
microphone*; `mic off` *turn off the mic*; `that's all` *that is all*;
`done talking` *I'm done talking*; `stop talking` *stop speaking / reading /
saying that*; `enough` *that's enough*, *enough of that*) and its `heard`
forms; a phrase the person adds is matched whole, or a letter off
(`sounds_like`, above).

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
    there), and never while someone has typed in that session in the last
    45 s. In those cases the message stays `pending` for the lane, with a
    `detail` saying why.
- `kind`: `message` (the default) or `result`. `result` is what the MCP's
  `report_result` sends, with `data: {status, branch, head_sha, diff_stat}`.
  For a `result` from a session the server replaces `data.diff_stat` with one
  measured from the sender's worktree as the message is posted.
  A third kind, `note`, is posted only by MindFlock itself (the fast-track
  failure note, `from: ""`): this route refuses it (400 `bad kind`), so a
  client can't fake one. A note is delivered as `[MindFlock note <id>]
  <text>`, with no sender framing and no reply hint.
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
{"id": "m1759600000123_42", "kind": "message", // message | result | note (note: from "")
 "from": "orch", "to": "orch-w1",
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
| `kind` | — | `message` or `result` (`note` is not a valid filter: 400) |
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
- its tmux session is alive; the lane **never** boots an agent — except a
  **sleeping** one ([session-engine.md](session-engine.md#sleeping-agents)),
  which it wakes (a message is exactly what should wake it) and then types
  into once the gates above pass on the relaunched CLI; `delivery: "now"` to
  a sleeper wakes it the same way and answers `pending` (*the recipient was
  asleep — it is waking up*);
- an agent CLI holds the pane: its executable (any provider's binary, or the
  session's own program, also as `node …/claude`) is in the pane's process
  tree, so a shell the agent quit to, or `vim`/`ssh`/`psql` started there,
  never receives the line;
- no usage-limit banner is showing;
- nobody has typed in that session in the last 45 s (the web terminal, or a
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
    known (remembered at create time, else the session's own prompt, else a
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

A **team run** is "work on these things together":
one session per ticket or task line, at most `concurrency` at a time with the
rest queued, each carried as far as its Fast-track target, with only what
needs you surfaced. The server keeps one record per run
(`~/.mindflock/runs/<id>.json`) and drives it with a 5 s loop. See
[team-runs.md](team-runs.md) for the model; the exact JSON is below.

`GET /api/outbox` still answers all four groups, though the web UI no longer
has an Outbox: the route feeds the bell's waiting list. The bell renders
`waiting` in its **Needs you** list (merged with the per-session
attention rows, so there is one badge), and a finished team run's header ⋯ menu
copies its entry from `summaries` (**Copy summary**; a team run finished more
than a week ago, or a split's lead Thread, reads `summary.text_md` from
`GET /api/runs/{id}` instead). Nothing else in the UI reads it; `shipping`, `shipped` and `queued` are there for other clients (a
team run's queued lines reach the UI through `GET /api/runs`).

| Method | Path | Behavior |
|---|---|---|
| POST | `/api/runs/preview` | Parse only, no side effects. `{text, repo_path?, program?}` → `{items, name_suggestion, lane_default, warnings}`. One thing per line; a line of only ticket-shaped tokens (`PAY-412`, `sc-9`, a URL, `org/repo#12`) is that many tickets. A ticket item: `{kind: "ticket", source, id, ref, title, repo, title_hint, has_session, error}` — an unresolvable one has `error: "not found in any source"` and is never turned into a task. A task item: `{kind: "task", text, repo, title_hint}`. A ticket that already has a session warns that it will be added, not restarted |
| POST | `/api/runs` | Create and start → **201** `{run: RunDTO, warnings}`. Body `{name?, items: [{kind: "ticket", source, id} \| {kind: "ticket", ref} \| {kind: "task", text}], policy: {lane?, ask_first?, grouping?, release?}, concurrency? (1–8, 3), program?, repo_path (required for task lines), budget_usd?, split?, split_optional?, max_pieces?}`. `split_optional` (with `split`; New's "Auto-split into up to N"): the lead may answer `POST /plan` with `pieces: []` to decline — the run is removed, the lead becomes an ordinary session armed with the team run's fast-track, and `run.changed` fires with `state: "dissolved"` (`data: {run, lead, by, why}`); its lead is titled `<slug>`, not `<slug>-lead`. `max_pieces` (2+, with `split`) caps the plan, clamped to `MINDFLOCK_MAX_CHILDREN`. Both are ignored without `split: true`. `lane` defaults to the fast-track rung. A live session for an item is **adopted** (a warning), not a 409; an item another team run owns is left out (a warning), and so is a ticket already in flight in the ingestion ledger for someone else (the pipeline is starting it). Queued tickets are **reserved** in the ledger (`in_flight`, `reserved_by: "run:<id>"`) so the pipeline never also spawns them; a reservation is handed back — only that entry — on every path that drops a queued ticket. Task-line titles never take a title whose branch already exists. **400**: `nothing to do`, `unknown lane`, `one PR for all commits each line …` (`grouping: "together"` with lane `leave`; `ask_first` there becomes `release: "ask"`), `repo_path is required for task lines`, `unknown agent …`; for `grouping: "together"` (one-for-all) `one-for-all needs a single repository` (no `repo_path`, or a ticket of another repository) and `one-for-all takes at most N lines` (`MINDFLOCK_MAX_CHILDREN`); for `split: true` `split needs exactly one line`, `this CLI doesn't get the MindFlock tools (…)`, `max_pieces must be a number`, `max_pieces must be at least 2`, and `lead is only for a split`. `lead: "<title>"` (split only) makes a live session the lead — any session in a git folder, also one that works directly in its folder or sits on its base/trunk branch (it plans; the approval decides where the pieces run, see `/plan/approve`; `lead.in_place` / `lead.trunk` say which) — 404 unknown, 409 workspace not ready / already in a team run. A one-for-all team run (and a split) creates its **lead** first (`<slug>-lead`, or `<slug>` for an optional split; a worktree of `repo_path`); when that create fails nothing is stored and the create's own status and sentence come back |
| GET | `/api/runs` | `{runs: [RunSummary]}`, newest first; `?active=1` unfinished only. RunSummary = `{id, name, state, paused, pause_reason, policy, counts: {queued, active, needs_you, shipped, failed, total}, cost_usd, created_at}` |
| GET | `/api/runs/{id}` | `{run: RunDTO}` — the run file minus bookkeeping, plus `counts`, `cost_usd`, `rev`, `waiting_for_usage`, `waiting_for_memory` (the planner is holding queued tasks for the memory guard) and per-task `row_present`. With `?wait=<s ≤ 60>&until=change\|needs_you\|done&rev=<n>` it long-polls and adds `reason` (that, or `timeout`). 404 unknown |
| POST | `/api/runs/{id}/pause` | `{reason?: "user"}` → `{run: RunSummary}`. Nothing new starts, merges or ships (every member's fast-track is held, a commit message you edited is kept for the resume; a releasing lead is disarmed and the team run goes back to `release_ready`); a start or re-arm already under way lands held. Agents keep working. 409 when finished |
| POST | `/api/runs/{id}/resume` | `{budget_usd?}` → `{run: RunSummary}`: re-arms what the pause held (never a member already merging back), with each member's own fast-track / ask-first; `budget_usd` raises the budget in the same call (the bell's Raise) |
| POST | `/api/runs/{id}/cancel` | → `{run: RunSummary, kept_sessions: [title]}`. Stops starting and shipping: queued tasks are cancelled (their tickets go back to ingestion), running ones disarmed; every session and branch stays |
| POST | `/api/runs/{id}/tasks` | `{items: [...as at create]}` → `{run: RunDTO}` (adds lines; reopens a finished team run). For a one-for-all team run the lines fork off the lead (one repository, the child cap); **409** while its plan is pending or its release is running; a `checking` / `release_ready` team run goes back to `running` (check and release card redone) |
| POST | `/api/runs/{id}/tasks/{task}/retry` | `{fresh?: false}` → `{task}`. A failed task, or one that needs you: re-armed (or re-created); `fresh: true` starts `<title>-2` on a new branch and keeps the old one — disarmed, so it ships nothing more. **409** in any other state, and (non-fresh) when its title now belongs to an unrelated session (its incarnation differs — never armed). A vanished one-for-all member whose branch has commits goes back to the merge queue instead of being re-created |
| POST | `/api/runs/{id}/tasks/{task}/start-now` | → `{task}`: a queued task starts on the next pass, past the concurrency cap once. 409 unless queued, or while paused |
| POST | `/api/runs/{id}/tasks/{task}/skip` | → `{task}`: a queued task is removed; a running one is **detached** (its session keeps running, its fast-track unchanged, it leaves the team run) |
| POST | `/api/runs/{id}/adopt` | `{title}` → `{task}`: add a live session (not restarted; the team run's fast-track is armed on it). 404 unknown session; **409** `branch already in team run <name>` when a team run owns its `(repo, branch)` — a copy session is the same work |
| POST | `/api/runs/{id}/plan` | A split's plan: `{pieces: [{title, prompt, paths: [glob]}], why?, from?}` → `{plan, problems: []}`. Validated against the lead worktree's `git ls-files` and red zones: **422** `{error, plan: null, problems: [{piece, error}]}` for fewer than 2 / more than the run's `max_pieces` (else `MINDFLOCK_MAX_CHILDREN`) pieces, a piece without title/prompt/paths, a bad glob, a repeated title, two pieces sharing a file (`overlaps <other> on <file>` — over existing files, and over a new literal path one piece names and the other's globs cover), or a piece whose every file sits in a red zone. **409** when not a split, past `plan_ready`, or `from` names a session other than the lead (the lead's MCP sends `from`; the UI does not). Stores `plan.state: "proposed"`, run state `plan_ready`. On an optional split (`split_optional`), `pieces: []` declines instead → `{plan: null, problems: [], dissolved: true, lane}` (`lane` armed on the lead, `""` when none) and the run is gone. A decline is taken in `planning` and in `plan_ready` (a lead may withdraw a plan it proposed) and is checked against `from` like any plan (409 for a non-lead); on any other split it is a 422 like any plan under 2 pieces |
| POST | `/api/runs/{id}/plan/approve` | `{mode?: "worktrees" \| "same_folder"}` (default `worktrees`, recorded as `run.mode`) → `{run: RunDTO}`: every piece becomes a task (`kind: "piece"`, title `<lead base>-<piece>`) and starts at once as a worker of the lead (`parent` = lead, `spawned`). **`worktrees`**: `base_ref` = the lead's HEAD, `base_branch` = its branch, fenced to its `paths` with worktree green zones, armed at `commit`, merged back. A lead that works directly in its folder or sits on its base/trunk branch first gets a **new lead**: `<lead>-split` (numbered), a plain worktree of the same repository cut from the original's HEAD (`base_ref`), `base_branch` = the original's base, the integration brief; `run.lead` becomes it and `run.origin` = `{title, branch, head, in_place, trunk}` records the original, which is never merged into, switched or pushed. **`same_folder`**: each piece an in-place session in the lead's folder (`in_place: true`, no `base_ref`), fenced per session (`worktrees[<folder>].sessions` in the zone store, guard `sessions`), never armed; MindFlock commits each done piece's changed paths itself (plumbing, no hooks, trailer `MindFlock-Piece: <run>/<task>`), one commit per piece. **400** another mode. **409** without a proposed plan, the lead gone, uncommitted tracked changes in the lead (`code: "lead_dirty"`) or — for a new lead — in the original (`code: "origin_dirty"`, "they would not be in the split"), a `same_folder` lead on its trunk (`code: "trunk"`, `branch`: start a branch there first), or (`approve_plan` called directly) an in-place / trunk lead in `worktrees` (`code: "needs_lead"`) |
| POST | `/api/runs/{id}/lead/branch` | "Start a branch here first": `{}` → `{run: RunDTO}`. A split whose lead's folder is on its trunk (or detached): `git switch -c <branch prefix><lead>-split` (numbered when taken) in that folder — only on this call; uncommitted changes come along — and `lead.branch` is the new branch, `lead.trunk` false. **409** past `plan_ready`, the lead gone or already on its own branch, or a git operation in progress |
| POST | `/api/runs/{id}/plan/reject` | `{note?}` → `{run: RunDTO}` — "Ask for a different split": back to `planning`; the lead is messaged (with the note) to propose again |
| POST | `/api/runs/{id}/integrated` | The lead's `report_integrated`: `{task_id, head_sha, from}` → `{ok, verified}`. `from` is required and must be the lead (a person uses Retry); only a member waiting to merge (`integrating`, or needs-you on a conflict) with commits beyond its base is accepted (409 otherwise). Verified only when the member's branch head is in `head_sha` and `head_sha` is in the lead's HEAD; then it is `integrated` (`conflict_fixed`). `verified: false` leaves it in the merge queue |
| POST | `/api/runs/{id}/check` | `{}` → `{run: RunDTO}`: run the check on the merged branch again (409 unless `checking`) |
| POST | `/api/runs/{id}/release` | `{merge_when_green?: false}` → `{run: RunSummary}`. Only in `release_ready` (409 otherwise): arms the lead's fast-track — `pr` (`push` for a push team run; `merge` **only** with `merge_when_green`: "Open the PR" never merges) — through `ship_now`. It ships exactly what the card and the check showed: **409** while the team run is paused, the lead is mid-turn / on a dialog / at its limit, has uncommitted tracked changes (a same-folder split: **any** change in the lead's folder, untracked included — fast-track would commit it into the PR), a merge or rebase in progress, or is off the team run's pinned branch; when its HEAD moved since the check the team run goes back to `checking` (409 says so) and the card is rebuilt. Armed with the server-built PR title and body on the Fast-track record (`pr_title`/`pr_body`, passed to make-PR). The team run then follows the lead's fast-track to `done` (`release.pr_url`), or to a handoff (`release.state: "handoff"`, `compare_url`) when no gh/token can file the PR |
| GET | `/api/outbox?group=<run id\|own\|all>` | What needs you, shipping, shipped today and queued — for **every** session, de-duplicated on `(repo, branch)` → `{counts: {waiting, shipping, shipped, queued}, groups: {waiting, shipping, shipped, queued}, summaries}`. `waiting[]`: `{key, title, run, text, kind, reason, since, preview, actions}` with `kind` `prompt` (a dialog: `["answer","open"]`), `approve` (an ask-first fast-track parked for your go, with `step`, `lane` and `preview: {commit_message, pr_title, files, add, del}`: `["ship","diff","edit_message"]`), `stuck` / `blocked` (`["open","retry","skip"]`), `ship_halted` (`["retry","open","skip"]`), `restart` / `failed` (`["retry","retry_fresh","skip"]`), `budget` (`["raise_budget","stop"]`), `conflict` (`["retry","open","skip"]`), and a one-for-all team run's own asks, titled with its lead: `plan` (`["approve","open"]`, `preview: {pieces: [{title, paths}]}`), `release` (`["release","open"]`, `preview: {pr_title, base, branch, files, add, del, check}`), `check_failed` (`["open","retry_check"]`), `lead_gone` (the lead lost for 10 minutes: `["cancel_group"]`), `stray` (a same-folder split: changes in the lead's folder no piece owns, or a commit no piece made: `["open"]`, `preview: {paths, commits}`). An `approve` item also carries `armed_at` (its identity: an edited message belongs to that card); its `preview.commit_message` is the FULL message — a person's, or the one drafted from the diff when a Commit target parked for approval (exactly what approving it unedited commits) — never a placeholder or a subject a model wrote for an earlier commit; for a commit approval `files/add/del` count the working tree's change (`diff_stat.uncommitted`) and `preview.pr_title` only a title someone set. A `release` item's preview carries the team run's `lane` (the buttons follow it). `shipping[]`: `{key, title, run, text, step: commit\|check\|push\|make_pr\|merge\|integrate, note, lane}` (`integrate` = a one-for-all member waiting in the merge queue, or handed to the lead on a conflict). A one-for-all member never appears in `shipped[]` on its own — the team run ships once, as its lead's PR. `shipped[]`: `{key, title, run, text, pr_url, pr_state, checks: pass\|fail\|pending\|none\|null, commit_subject, lane, files, verify: {id}\|null}`. `queued[]`: `{run: {id, name, task}, ref, text, title, retry_at}`. `summaries[]`: finished team runs of the last 7 days `{run, name, state, finished_at, text_md}` |

**Events** (on `/api/events`, emitted only by the run driver): `run.changed`
`{run, state, counts}` (refetch; no notification) — or, when an auto-split's
lead declines, `{run, state: "dissolved", counts: {}, lead, by, why}` (the run
no longer exists; the web UI toasts it, skipping replayed events); `run.needs_you` `{run,
name, task, title, ref, reason, text, incarnation, key, detail}` once per (run,
task, reason, incarnation) — `key` is that announce key (per round for a
team run's own asks), which the bell dedupes on — never for a dialog. The boot
reconcile seeds only what was already standing; what it finds (a member gone
after a restart) is announced once after the boot quiet window; `run.task_shipped` `{run,
name, task, title, ref, pr_url, incarnation, detail}`; `run.finished` `{run,
name, shipped, failed, cost_usd, duration_s, grouping, outcome, detail}` once,
across restarts (`outcome`: what the team run did, e.g. "one PR opened").
Notify rules `run_needs_you` and `run_finished` are on by default,
`run_task_shipped` off. A one-for-all team run also says `run.needs_you` once per
round for its own asks — `reason` `plan` (the lead proposed pieces),
`release` (one PR is ready to open), `check_failed` and `lead_gone` — with `session` and
`title` the lead and `task` `""`; its members are not announced as shipped.

RunDTO for a one-for-all team run or a split adds `split`, `optional` (an
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
- session terminals (`/terminal`, `/shell`) only — one keyboard per session
  ([people.md](people.md#owner-driver-viewers)): on attach and on every change
  of driver, `{"type":"driver","driving":bool,"driver":"<id>"|"",
  "driver_name"}`. A terminal whose person doesn't drive is **read-only**: its
  input is dropped and answered (at most every 3 s) with
  `{"type":"readonly","message":"<name> is driving — take control to type"}`,
  which the UI shows as one line over the terminal. The first person to
  attach to a session nobody drives takes the keyboard, and a keystroke while
  nobody drives takes it too. The terminal emulator's own replies to tmux's
  probes (cursor position, device attributes, OSC colours, focus reports —
  `core/terminal.py::is_terminal_reply`) always pass and are never presence.
  Old clients ignore both frames (they are JSON without `type: "error"`)
- close codes: **4404** session gone, **4409** workspace not ready, **4423**
  session paused (agent terminal only: the agent is never relaunched for a
  paused session — Resume does that — preceded by an error frame with
  `paused: true`), **4500** spawn failure — clients stop reconnecting on
  4404/4409/4423, otherwise retry ~2.5 s
- auth close codes (any websocket, from the middleware): **4401**
  unauthenticated — the SPA/mobile head reload to the login page; **4403**
  cross-origin refusal — a hard refusal, not a login prompt (don't retry or
  re-prompt)

Detaching a socket never kills the tmux session.

## Workspaces, recently closed

| Method | Path | Behavior |
|---|---|---|
| GET | `/api/recent?sizes=1&days=7` | The **Recently closed** page — closed sessions AND what is left on disk, in one list (this is what the UI reads; the two used to be separate dialogs). `{rows: [{id, source, title, branch, name, path, folder, kind, worktree, in_place, provisioned, exists, closed_at, mtime, last_used, size_bytes, active_session, stale}], stale_days, hidden: {protected, protected_names, protected_bytes, active, active_titles}, roots}`. `source` is `closed` (a reopen target from the undo store, id = its entry id) or `disk` (a directory no closed entry accounts for, id = `disk:<path>`). `worktree` is true only for a directory git generated as a *linked* worktree; `last_used` is epoch seconds, the max of the dir's mtime and the worktree's own git index/HEAD/reflog; `stale` marks the rows the sweep below would take, at `days` (default 7). NOT rows, but counted in `hidden`: protected base clones / cache refreshers, and anything a **live** session is working in (that one is in the sidebar). `size_bytes` and `protected_bytes` are computed only with `?sizes=1` (a `du` per row) |
| POST | `/api/workspaces/prune-worktrees` | **Delete unused worktrees — and nothing else.** Body `{days=7, dry_run=true, include_dirty=false}`. A candidate must pass every rule: strictly under MindFlock's own worktrees root; its `.git` is a gitdir **FILE** whose pointer runs through the repo's `worktrees/` dir and carries git's back-pointer to it — i.e. a linked worktree `git worktree add` generated, never a `--separate-git-dir` repository or a submodule checkout, which also have a `gitdir:` file (a repository, a clone, a `_base_*` mirror, a `pr-*` review clone and any plain folder are all excluded by that one test, re-checked immediately before each delete); not a mount point; no live session using or sharing it; and nothing has touched it for `days`. The candidate set is always recomputed **server-side** — no path a client sends is ever deleted. `dry_run` (the default) returns `{candidates: [{name, path, branch, size_bytes, last_used, dirty, titles}], candidate_count, total_bytes, dirty_count, kept: {active, recent, not_worktree, outside_root, protected}}` without touching anything. A real run deletes the **clean** candidates only, unless `include_dirty` — `dirty` means the directory holds something the base repo has never seen (uncommitted or untracked changes, or a detached HEAD whose commit no ref contains — deliberately NOT git-ignored content, which every worktree has, and which the confirmation says goes with the directory); a worktree's branch and commits live in the repo it came from and survive. It also forgets the closed entries whose worktree it removed, prunes each stale registration from the repo the worktree's own gitdir names, and collects the empty branch-slug dirs left behind → `+{removed, removed_count, failed, forgot, kept_dirty, freed_bytes, empty_dirs_removed}`. 400 on a negative or non-numeric `days` |
| GET | `/api/workspaces?sizes=1` | The raw disk listing (`/api/recent` is the filtered, annotated one the UI reads): `{workspaces: [{name, path, root, kind, worktree, gitdir, size_bytes, mtime, last_used, active_session}], roots}`; `kind` ∈ `base·refresher·pr·tmp·worktree·workspace` (`size_bytes` computed only with `?sizes=1`). **`last_used` is the max of the directory's mtime and the worktree's own git index / HEAD / reflog mtimes** — a directory whose root was merely touched is not thereby "used", and a worktree worked in through git shows it even when the root's mtime never moved. That definition is what `stale` and the sweep are computed from, so it is the number to reason about, not `mtime` |
| POST | `/api/workspaces/delete` | Body `{path}`. Must be under a managed root **and** a directory this server lists as a workspace — a flat child of a provisioning root, or a worktree leaf; a deeper path (e.g. inside a base clone) is refused, which is the "direct child" rule this row always claimed. Cache refreshers always protected. A base clone (`_base_*`) is deletable ONLY when nothing references it — no attached worktrees and no active session based on it; otherwise 400 naming the holders. Ends the active session on the dir first, through `end_session` (recorded in Recently closed; its children move up and are returned as `children`); **409** `{error: "folder in use", sessions}` and nothing changes when two or more live sessions share the dir. Prunes the stale registration from the repo named by the worktree's own `gitdir:` pointer (which is what makes it work for a *nested* worktree path). |
| GET | `/api/recently-closed` | `[{id, title, branch, folder, in_place, provisioned, closed_at, exists, wiped, reason, branch_exists}]` — `reason` is `""` for an ordinary close and `"workspace missing"` for a session the engine dropped at load because its folder and tmux were both gone (then `wiped: true`, and `branch_exists` says whether its branch is still in the repo; `/api/recent`'s closed rows carry the same three fields) — the undo store itself (Ctrl+Z / Ctrl+Shift+T and the Verify dialog's closed-session targets read this; the page reads `/api/recent`) |
| POST | `/api/recently-closed/{id}/reopen` | Recreate a session on the preserved worktree (410 if the dir is gone) |
| POST | `/api/recently-closed/{id}/forget` | Body `{wipe}` — drop the entry, optionally delete the worktree. **409** when a still-running session shares that directory (a copy and its origin keep one worktree); an in-place session's folder is the user's own repo and is never deleted |

## Remote devices (tailnet)

Multi-device control (gated by the `general.remote_control` setting — Settings →
Your devices → Advanced → **Allow remote control**, next to **Pair by pasted
token**; both used to sit under Security and in the sidebar's Connect… dialog): other
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

**Who it is for.** Every proxied request (both proxies, discovery and the
fleet's own calls) also carries `X-MindFlock-Person: <person id>` beside the
device key in `X-MindFlock-Remote` ([people.md](people.md)). The receiving
server records it on the request state — read it with
`backend.web.core.auth.request_person(request)`, which falls back to that
server's own person for a request without the remote marker. It is an
identifier only: it never gets a request past the gate.

**Whose credential goes out.** Both proxies (`<device>::<title>` and
`fwd/`) attach this device's credential for the target (the fleet key, or
the token pasted for it) only when this device's own gate is on — the
caller already signed in — or the caller is `privileged()`: this machine
(loopback, a loopback `Host`, unproxied), the phone or a browser holding
the fleet-key cookie, a trusted Tailscale account. An anonymous caller of a
gate-off device is forwarded **without** a credential, so the target's own
gate decides (a gated target answers 401); otherwise holding the fleet key
would let a gate-off member launder strangers into gated ones.

| Method | Path | Behavior |
|---|---|---|
| GET | `/api/remote/hello` | Identity/permission handshake target for other devices: `{app, version, commit, install, shell_version, device, host, remote_control, auth, shared_link, shared_link_live, fleet, fleet_proto, automation}`. `commit` is the commit the engine was installed from (its dist-info's `direct_url.json`, read once at boot — `""` for an editable or local install), so two builds that both say the same version can be told apart; `install` is how it is installed (`uv-tool`·`editable`·`other` — whether "Update all my devices" can update it); `shell_version` the desktop app version this machine's shell last reported (its local engine check sends `X-MindFlock-Shell`, honored only from this machine; `""` when none). `fleet` is the id of the "Your devices" group this device belongs to (`""` for none — the id names the group, it admits nothing), `fleet_proto` the join protocol it speaks (0 = a MindFlock from before fleets) and `automation` whether this device is the one that runs PR review and issue handling (`settings_hooks.automation_here`: always true on a lone device; in a group, the device `github.automation_device` names, else the lowest-keyed live member — see [configuration.md](configuration.md#web-exposed-settings)). `shared_link` is the Tailscale Service name this device answers the shared phone link on (`""` for none), and `shared_link_live` whether this process's `tailscale serve` for it is up (the phone-link host table in Settings → Your devices). `/api/devices` echoes it per device, and `/m` reads `device` to resolve `<device>::<title>` deep links |
| GET | `/api/devices` | Tailnet devices running MindFlock + their connection state: `{self: {device, host, os}, remote_control, devices: [{device, host, os, ip, version, shared_link, reachable, remote_control, auth, has_token, needs_token, connected, error, sessions, member, fleet_proto, in_fleet, same_fleet}]}`. `member` = on this device's "Your devices" roster, `in_fleet` = in any group, `same_fleet` = in this one; a member never `needs_token` (the fleet key opens it) |
| POST | `/api/devices/refresh` | Sweep the tailnet now instead of on the next 20 s tick (Settings → Your devices' Refresh); returns the `GET /api/devices` payload |
| POST | `/api/devices/{device}/connect` | Pair with a device (token exchange, persisted in `~/.mindflock/remote_devices.json`) |
| POST | `/api/devices/{device}/disconnect` | Drop the pairing. **409** `{error: "<host> is one of your devices — remove it in Settings → Your devices"}` for a member: the fleet key keeps it connected whatever happens to a pasted token |
| * | `/api/devices/{device}/fwd/<path>` | Forward to that device with its stored token — for a caller this device vouches for (see **Whose credential goes out** above; bare otherwise) — (502 when not connected). Allow-listed to what New Session asks: `GET /api/config`, `/api/settings`, `/api/templates`, `/api/providers`, `/api/providers/manage`, `/api/repos/suggest`, `/api/repos/search`, `/api/repos/check`, `/api/browse`; `POST /api/mkdir`, `/api/session-plan`, `/api/instances` — anything else 404s. A forwarded create refreshes that device's session list before answering, so the next `GET /api/instances` already carries `<device>::<title>` |

### Your devices (fleet)

One person's computers as one group (`backend.web.core.fleet`, routes in
`backend/web/addons/fleet.py`, roster in `fleet.json` beside `settings.json`).
Every member holds the shared **fleet key**, which is a full credential on
every member (bearer, cookie, `?token=`, websocket), and only members exchange
settings (sync) or rosters. A device joins with an 8-character code made on a
member, by asking a member that then approves it (both screens show the same
6-digit code), or in one click when this device already holds the other's
pasted access token. Joining or admitting a device turns
`general.remote_control` on there, since members talk through relayed
requests, and a request bearing the current fleet key passes the
remote-control gate even if the toggle is switched off later.

**Removing** a member gives the rest a new key: members reachable now take it
at once, and one that is offline gets it automatically the next time a member
sees it (old keys are kept up to 30 days, `prev_keys`, to hand it over under
the key it still holds). By default every member that took the new key, and
this device, also replaces its **own access token**, so tokens the removed
device held stop working and phones signed in before must scan the QR again.
Each removal records who made it (`removed[].removed_by`): "rig removed laptop"
when you didn't is how a forged removal shows. One shared key can't tell a
removed device from a member that hasn't heard yet, so until an offline member
is handed the new key the removed device can still reach it with the old one:
a lost or stolen device must **also** be removed from the tailnet (Tailscale
admin console), which cuts it off everywhere at once. Every removal answer
carries that advice (`advice`).

A removal is **sticky**: a roster from another member never brings back a
device this one knows another device removed (every member holds the same
key, so a roster entry can't prove who wrote it). If another member lets it
back in anyway, it stays out here and is listed under `readmitted_elsewhere`
until you allow it here (`POST /api/fleet/members/{key}/allow`). A device that
**left** on its own (its tombstone names itself) isn't held off: a later join
through any member, learned by gossip like any change, brings it back
everywhere. When a device rejoins through a member that still lists a device
this one removed (two removals made apart, a missed key change), the joiner
removes that device again and then replaces the shared key and the members'
own tokens without it (`rotate_key`), since the member it joined through may
have handed it the key.

**Every member is only as locked as the least locked one.** A member with the
access gate off holds the fleet key and relays `<device>::<title>` routes and
settings edits to the others with it, so anyone who can reach that member
controls your other devices through it. `gate_warning` is true when the gate is
off and this server is reachable beyond this machine (a non-local
`CS_WEB_MODE`, or local mode fronted by `tailscale serve` / the shared link);
Settings → Your devices and `mindflock devices` warn then. The Your devices warning has a
**Turn the gate on** button: it signs that browser in first (`GET
/api/settings/auth-token`, then `POST /api/auth`) and saves
`general.auth_mode = "on"`; other members keep working on the fleet key. An
anonymous tailnet caller of such a member can still drive its sessions, but it
can no longer change settings that sync would spread (see **Changing what runs
on your devices** below), nor read the token that would let it.

Browser routes are **privileged**: the caller must present a credential, be
this machine itself (loopback with no forwarding headers), or be a trusted
Tailscale account, and the request must not be relayed by another MindFlock.
Anything else gets 403 `{"error": "open this on the device itself"}`.

| Method | Path | Behavior |
|---|---|---|
| GET | `/api/fleet` | `{in_fleet, id, epoch, self: {key, host, ip, port}, self_reachable, listening, gate_on, members: [{key, host, added_at, self, reachable, version, same_fleet, error, reason, last_seen, key_conflict, automation}], invites: [{code, expires_at, device, command}], requests: [{id, device, host, code, created_at, expires_at, ip, via, via_host}], join, stale_key, gate_warning, candidates: [{device, host, version, fleet_proto, reachable, member, in_fleet, same_fleet, has_token}], removed: [{key, host, removed_at, removed_by, removed_by_host, left}], readmitted_elsewhere: [{key, host, by, by_host}], admitted: [{device, host, at, state, reason}], tailnet_peers: [{device, host, os, online, tagged, outcome, last_seen, reason}], policy_grant, match, phone_link}`. **Reachability:** `listening` is `local` (bound to 127.0.0.1 — no other device can reach this one), `tailnet`, or `""` (unknown: nothing exported the mode); `self_reachable` is false only for `local` (a device that only answers the shared phone link doesn't count — members probe `ip:port` and the node's MagicDNS name); `gate_on` is the access gate as it is now. `admitted` lists devices let in here in the last hour, re-probed a few seconds after the admit: `state` `checking` → `reachable`, or `unreachable_joiner` with the probe's `reason` (joined, but this device can't reach it — it is local-only over there). **Why a device isn't listed:** member `reason` and each `tailnet_peers` row say what discovery's probe found — `outcome` `refused` (nothing listens there: not running, or local-only), `timeout` (packets dropped: usually a tailnet policy that doesn't open the port — `policy_grant` is the lines to paste, scoped to the owner: `autogroup:member`→`autogroup:self`, tag↔tag, the owner's Tailscale login↔tag; the login is named only for a privileged caller, else a `YOUR-TAILSCALE-LOGIN@example.com` placeholder under a `// REPLACE` comment), `tls`, `not_mindflock`, `http_<n>`, `asleep` (offline in Tailscale; `last_seen` is Tailscale's) or `unreachable`; `last_seen` on a member is when MindFlock last answered there. `tailnet_peers` holds every non-mobile tailnet device that is neither a member nor a joinable MindFlock, tagged ones first. **Match:** `match` is `{reachable, shared_link}` — what "Match my other devices" would change here (become reachable; answer the phone link the other members answer) — or null; `phone_link` is `{name, hosts: [{key, host, self, state}]}` with `state` `hosting` (live) · `waiting` (named, not live: awaiting approval) · `off` · `unknown` (offline, or too old to say), or null when no device names one. `requests` holds the ones waiting here (`via` `""`) and copies of the ones waiting on another member (`via` its key — see "approve from wherever you are" below). `automation` is whether that member runs PR review and issue handling — derived from the synced `github.automation_device` the same way on every member, so exactly one is `true`. `left` is true when the device took itself out (`removed_by` is itself) rather than being removed by another device; only the latter is flagged as a removal you may not have made. `readmitted_elsewhere` lists devices removed here that another member's roster has let back in since: they stay out here until you allow them (below). `key_conflict`: same group and epoch but a different key (two removals made apart) — one side must rejoin the other; `stale_key` is true on this device when it is the side that must (or a member answered with a newer epoch, or every reachable one rejects its key). `removed` lists tombstones newest first. `join` is the outgoing join state (below). An unprivileged caller gets empty `invites`/`requests` and a blank `join.code` |
| POST / DELETE | `/api/fleet/invite` | Make a single-use code (`{code: "XXXX-XXXX", expires_at, device, command}`; TTL 10 min, at most 3 live; starts the group if there is none) / cancel every live code → `{ok}`. On a local-only device the code is still made but carries `warning: "local_only"`: nothing can reach this one to redeem it (Settings → Your devices blocks it with Make reachable first) |
| POST | `/api/fleet/join` | `{device, code}` or `{text}` (the pasted `mindflock devices join …` line) → redeem the code on `device` and join. Returns the join state; 400 with the join state plus `error` when it fails |
| POST / GET / DELETE | `/api/fleet/request` | Ask `{device}` to let this one in (→ `{state: "waiting", code: "123 456", …}`; polls it in the background) / this device's join state `{state, device, host, code, error, id}` (`state`: `idle·waiting·joining·joined·denied·expired·error`; once `joined` it also carries `self_reachable` — false when this device is local-only, so the others can't reach it yet) / cancel (withdraws the request on the other device too; refused once `joining` — the state comes back unchanged) |
| POST | `/api/fleet/requests/{id}/approve` · `/deny` | Answer a pending request → approve `{ok, device, host, sync_error}` (settings sync is started here first, `sync_error` `""` when it did) · deny `{ok, device, host}`; 404 once it's gone. Body (optional) `{via, code}`: `via` names the member the request waits on (a copy listed with that `via`) — the answer is sent there under the fleet key (`member-approve`, below) → its answer plus `via`; 502 when that member can't be reached. `code` is the 6-digit code the person looked at: 403 when it isn't the request's. The phone's approve card and the bell's Approve use the same route |
| POST | `/api/fleet/add-paired` | `{device}` → add a device this one holds a pasted token for → `{ok, device, host, sync_error, direction: "theirs_take_mine"}`: the added device takes THIS device's shared settings where this one has them (the other way round from joining). 400 without a pasted token, 502 when it refuses |
| POST | `/api/fleet/members/{key}/remove` | Body `{rotate_tokens?: bool = true}` → `{ok, rekeyed[], missed[], rotated[], rotate_failed[], advice}`: `rekeyed` took the new key now, `missed` get it when they're next seen, `rotated` replaced their own access token (this device included), `rotate_failed` didn't — a missed member's token is never replaced later, so do it there (Settings → Security). `rotate_tokens: false` only re-keys. The response re-issues this browser's cookie when this device's token changed. Removing **this** device is leaving → `{ok, left: true}` (no new key, no token change). When the removed device was the one running PR review and issue handling (`github.automation_device`), they move to this device. 404 for a key that isn't a member |
| POST | `/api/fleet/members/{key}/allow` | Let a device removed here back in on this device (Settings → Your devices → **Allow it here**, offered for a `readmitted_elsewhere` row): the tombstone here goes and it is a member again, added now by this device (fires `device.joined`) → `{ok, device, host}`. 404 `{error}` for a device that wasn't removed here by another device |
| POST | `/api/fleet/leave` | Leave the group → `{ok}`: when this device runs PR review and issue handling, `github.automation_device` first moves to the remaining live member with the lowest key (and the others are nudged to pull it); then the members get a tombstone for this device and settings sync turns off here. On its own again, this device runs PR review and issue handling itself. Leaving changes no key and no token, and isn't held against the device: a later join through any member brings it back everywhere |
| POST | `/api/fleet/bootstrap` | `{code?}` → one copyable line that installs MindFlock on a brand-new computer and joins it (`backend.web.core.bootstrap`): `{line, ref, version, pinned, device, code, expires_at, desktop: {download, paste}}`. `line` is `curl -LsSf https://raw.githubusercontent.com/<repo>/<ref>/install.sh \| MINDFLOCK_INSTALL_REF=<ref> sh -s -- --join '<device> <code>'` with `ref` = `v<this device's version>` (`main`, `pinned: false`, for a source build), so the joiner runs the same version. `code` names a live invite to use; without one (or an unknown one) a fresh invite is made. `desktop` is the app's twin: the release page for `ref` and the `<device> <code>` to paste into Settings → Your devices → **Paste a code** (`fleet.classify_code` reads the device from it). Privileged |
| GET | `/api/fleet/readiness` | `{members: {<key>: summary \| {error}}}` — each live member's own readiness, computed BY that member about itself (Settings → Your devices shows it read-only; fixes run on that device). A summary is `{version, deps_ok, missing[], agent: {provider, signed_in: bool\|null}, deferred[], push: {ok: bool\|null, message}, tailscale: {running, key_expiry_days, key_expired, key_warn}, ready, fixes[], at}`; `error` is `offline`, `didn't answer`, or `update MindFlock there to see this` (an older member). This device's own summary is cached 2 min; `?refresh=1` recomputes it. Privileged |
| GET | `/api/fleet/readiness/self` | This device's summary of itself, for another member — the fleet key as bearer (or a privileged caller); 401 with the fleet's `{error, id, epoch, kfp}` otherwise |

Device-to-device, **public** (the auth middleware exempts exactly these four
method + path shapes). Each is validated, rate-limited per client IP (20 a
minute; 90 for the poll and the cancel; 429 when over), and answered only to a
caller on the tailnet (a direct tailnet address, or one `tailscale serve`
forwarded and this machine vouches for) or this machine itself, unproxied —
anything else gets 403 `{"error": "join from one of your devices on the
tailnet"}`:

- `POST /api/fleet/redeem {code, device, host, dns?, fleet_proto,
  runs_automation?}` → the bundle `{id, key, epoch, members, removed}`. 403
  for a wrong or expired code (or a tailnet address that isn't `device`'s).
  5 wrong codes from one address within 10 min lock that address out for a
  minute, doubling with each repeat lockout up to an hour (429 "too many wrong
  codes — wait N minutes"); it never costs anyone else their code. Every live
  code is cancelled only after 20 wrong codes within 10 min from at least 3
  different addresses.
- `POST /api/fleet/requests {device, host, dns?, secret_hash, fleet_proto,
  runs_automation?}` → `{id, code}` (fires `device.join_requested`, and hands
  every reachable member a copy — see below).
- `GET /api/fleet/requests/{id}?secret=…` → `{state[, bundle]}` (the bundle is
  served once).
- `POST /api/fleet/requests/{id}/cancel {secret}` → `{ok: true, state:
  "withdrawn"}`, or `{ok: false, state}` once it was answered; 404 unknown id,
  403 wrong secret.

A caller that came through a local `tailscale serve` proxy this machine can't
vouch for (loopback with forwarding headers) gets a distinct 403 that points
at the fix on THIS device ("only reachable through tailscale serve … choose
Make reachable").

`runs_automation` is the joiner saying it runs PR review / issue handling now
(they run there and one of them is set up). Only when the group hasn't chosen
(`github.automation_device` unset, or naming a device that isn't a live
member) do both sides set it — to the joiner when it runs them, else to the
device that admitted it; the joiner settles it again after its first settings
pull, so a choice the group already made stands.

Member to member, with the fleet key as the bearer: `GET` / `POST
/api/fleet/roster` (gossip of `{id, epoch, members, removed}`, every 30 s),
`POST /api/fleet/rekey` (the next epoch's `{id, epoch, key, members, removed}`
→ `{ok}`; only `epoch == mine + 1` is taken, and it replaces the roster) and
`POST /api/fleet/rotate-token` (replace this device's own access token → `{ok}`,
or `{ok: false, error}` when `MINDFLOCK_AUTH_TOKEN` pins it or saving fails —
the CURRENT key only). The middleware lets these (and the sync `nudge` /
`export` routes) past the token and remote-control gates, because each checks
the key itself and refuses with **401** `{error, id, epoch, kfp}`: this
device's group id, key epoch and key fingerprint (non-secret), so the caller
can tell "I missed a key change" from "it did" from "we hold different keys".
**Approve from wherever you are.** A request waiting on one member is copied
to the others, so the person can approve it on whichever device (or phone) is
in front of them:

- `POST /api/fleet/pending {from, host, requests: [{id, device, host, code,
  created_at, expires_at}]}` — the asked device hands every reachable member
  the CURRENT list of requests waiting on it (on a new request, an answer, a
  withdrawal; an answered one disappears from the copies). The receiver
  replaces what it held for `from` (a live member, not itself), keeps each copy
  no longer than the request TTL, and fires `device.join_requested` with
  `via`/`via_host` for each new one (the bell, a toast, the desktop
  notification — never a second ntfy push) → `{ok, held}`.
- `POST /api/fleet/member-approve {id, code, decision: "approve" | "deny",
  by}` — the answer given on another member (its privileged approve/deny
  route sent it on). The 6-digit `code` must match the request's (403
  otherwise; the request stays pending) → the approve/deny answer; 404 once
  it's gone.

Both take ONLY the fleet key and answer **403** without it — a relayed request
that merely got past remote control, or this device's own token, is refused.
The browser-facing approve/deny routes stay privileged and refuse relayed
requests: there is no generic relayed approve, only this member-to-member one.

`POST /api/fleet/adopt {bundle, from: {key, host, dns}}` (one-click add) takes
only this device's OWN access token, never the fleet key → `{ok,
runs_automation}`; 401 without it, 409 when this device is in another group.

**Update all my devices** (`web/core/fleet_update.py`):

- `POST /api/fleet/update {tag?}` — the owner's browser or CLI (`privileged()`,
  never relayed; 403 otherwise). Starts a rollout to `tag` (a published
  release at or above this device's version; default the newest release):
  every other live member **one at a time**, then this device last. Each
  member is asked through its `update/apply`, then the rollout waits until
  that member's own hello reports the new commit (or, without one, the new
  version) and only then moves on; the first member that fails, rolls back
  (its installer's health check) or doesn't come back halts the rollout.
  Members that are offline, already current, a dev checkout or not installed
  by install.sh (hello `install`), or too old for the route (404) are skipped
  with the reason. → the rollout; 409 while one runs, 400 outside a group or
  for a refused tag, 502 when GitHub can't name the newest release.
- `GET /api/fleet/update` → `{state: idle·running·done·halted, tag, version,
  error, started_at, finished_at, members: [{key, host, self, step, detail,
  shell_version}]}` — `step` is `queued·updating·restarting·done·current·
  skipped·failed·not_started`; a member whose desktop app lags says "desktop
  app on X (vY) updates on its next launch" in `detail`. Progress persists in
  `<config dir>/fleet_update.json`. The rollout stays `running` through this
  device's own install and restart (its row follows `update.json`: `done` once
  the new build answers, `failed` and the rollout `halted` when it failed or
  rolled back); one that this device's restart cut off before the other
  members finished reads as `halted`. The fleet key goes to a member only
  while it still matches its recorded name, re-checked on every poll. `GET /api/fleet` carries the same object as `update`, and
  each member row adds `commit`, `install` and `shell_version` from its hello.
- Member to member, **fleet key only** (never this device's own token, never
  the browser `fwd/` relay — `/api/update/*` stays off `_FWD_ALLOWED`):
  `POST /api/fleet/update/apply {tag?}` installs `tag` (default the newest
  release) through the same detached installer as `/api/update/start` → `{ok,
  ref, commit}` or `{ok: true, current: true}` when already on it; 400 for
  anything but a published release tag at or above its own version, 409
  `{blocked: true, install, error}` for a dev checkout / non-install.sh engine
  (or an install already running). `GET /api/fleet/update/state` → `{version,
  commit, install, blocked, state, ref, error, restart_pending}`. Both answer
  401 `{error, id, epoch, kfp}` to anything but the current fleet key.

### People

The person all of your devices share — an identifier, never a credential
([people.md](people.md)).

| Method | Path | Behavior |
|---|---|---|
| GET | `/api/person` | `{id, name, git_name, git_email}` (the id is made at startup if the store has none) |
| POST | `/api/person` | `{name?, git_name?, git_email?}` — `""` clears, an absent field is left alone; `id` is ignored (devices agree on it: pairing, then settings sync). Needs `may_configure` like any synced setting (403 otherwise); 400 `{error}` for a git email that isn't one; 409 while settings.json can't be read. → the person. `POST /api/settings` drops `person.id` the same way |

The pairing payloads carry the inviter's person: `bundle_for(...)` (the redeem
answer and the approved request's bundle) gains `"person": {id, name, git_name,
git_email}`, and so does the `POST /api/fleet/adopt` body; the joiner adopts the
id. The settings-sync export (`GET /api/settings/sync/export`) carries
`"person": {"id": …}` for the tie-break.

### Peer links — the safety number, unlinking, the off switch, export

The routes are specified in
[peer-link.md](peer-link.md#engine-integration--contract-servicepy--existing-modules);
the rules a client must expect from them:

- **Before *It matches*** (`sas_verified` on the link), `POST
  /api/peer/links/{id}/share`, `…/share/reopen`, `…/mirror` and `…/export`
  answer **409** `{"error": "Confirm the safety number with <name> first"}`.
  Their side refuses `…/diff`, `…/their-screen`, `…/their-files` and
  `…/their-file` until *they* confirmed it: **403** `"<name> hasn't confirmed
  the safety number yet — compare it with them, then you both press It
  matches"`. Messages work either way. A re-pair (a fresh invite from someone
  already linked) un-confirms it on both sides and emits `peer.state` with
  `sas_changed: true`.
- A refusal by their permissions names the switch: **403** `"<name> turned
  off “see my changes” on this link — ask them to turn it on in People"`
  (`read my files` for the files routes).
- `DELETE /api/peer/links/{id}` and `DELETE /api/peer/links/{id}/share`
  **close** the shared session (agent stopped, folder kept — `unshare`
  answers `{link, deleted: false, folder}`); only `?delete_files=1` deletes
  the session and its folder. When the *peer* unlinks, this side's shared
  session is closed the same way, never deleted; `peer.link_removed` carries
  `by` and `kept_folder`.
- `POST /api/peer/disable` → `{enabled: false, closed: [titles], links: n}`:
  peer links off — listener, relay and connections stop, every shared session
  is closed (kept); every link and share record stays and `GET /api/peer`
  still lists them (`connected: false`). Turning the `peer.enabled` setting
  off and calling any `/api/peer` route does the same. On again, nothing
  reopens by itself: a link whose share is kept shows `shared: true,
  share_open: false`, and `POST /api/peer/links/{id}/share/reopen
  {program?}` → **201** `{link, session}` starts a new sandboxed session in
  that folder (409 when nothing is shared, it's open, or the folder is gone).
- `POST /api/peer/links/{id}/export` → `{branch, repo_path, commits, sha,
  target}`; `commits` is how many commits the branch brought that the repo
  didn't have (0 = nothing new). Every refusal is one sentence, e.g. "That
  repo doesn't have the history the shared folder started from — pick the
  repo you shared from, or a clone of it."
- `GET /api/peer` → `listen.bound`: where the listener really is now
  (`"host:port"`, or `null`); a changed `peer.listen_port` re-binds it.

### Peer links — joint tasks

Peer-link routes live under `/api/peer` and are specified in
[peer-link.md](peer-link.md#engine-integration--contract-servicepy--existing-modules)
(403 for a request relayed by another device; writes need `may_configure`).
A link's joint task — one feature, both agents, one contract
([peer-link.md → Joint tasks](peer-link.md#joint-tasks--contract-jointpy)):

| Method | Path | Behavior |
|---|---|---|
| GET | `/api/peer/links/{id}/joint` | `{joint: null \| {id, brief, proposed_by: "me"\|"peer", created, state: "proposed"\|"active"\|"done"\|"cancelled", reason, mine: {repo_path, repo_name, branch, program, share_id, session_title, error}, theirs: {repo_name, branch, program, shared}, contract: {text, rev, updated, by}, proposals: [{id, by, text, at, state: "open"\|"accepted"\|"rejected"}]}}` |
| POST | `/api/peer/links/{id}/joint` → **201** | `{brief, repo_path?, branch?, program?, contract_text?}` — propose (an empty `contract_text` gets the starter headings; an empty `repo_path` = use the folder the link already shares, or none). → `{joint}`. **409** while one is proposed/active, before the safety number matches, a folder without a working sandbox, a folder while the link already shares one, or `"<name>'s MindFlock is older — ask them to update"`; **400** missing brief / bad folder or branch; **502** peer offline |
| POST | `/api/peer/links/{id}/joint/accept` | `{repo_path?, branch?, program?}` → `{joint, session?}` — goes active on both sides and starts my half's shared session (a failure is `joint.mine.error`, still 200). **409** no proposal from them |
| POST | `/api/peer/links/{id}/joint/cancel` | `{reason?}` — decline / cancel; `reason: "done"` finishes it → `{joint, synced}` |
| PUT | `/api/peer/links/{id}/joint/contract` | `{text}` → `{joint, synced}` — a new revision (rev + 1), written to both shared folders' `CONTRACT.md` |
| POST | `/api/peer/links/{id}/joint/proposals` → **201** | `{text}` (the whole new contract) → `{proposal_id, synced, joint}` |
| POST | `/api/peer/links/{id}/joint/proposals/{pid}/accept` · `…/reject` | → `{joint, synced}`; accept makes it the contract (rev + 1). **404** unknown, **409** already decided |

`synced: false` = the peer was offline; the reconnect resync catches up. Each
link in `GET /api/peer` carries `joint: {id, state, brief_head, contract_rev,
open_proposals, mine_shared, theirs_shared, check?: {verdict, steps, passed}} | null`
(`check` once the joint checklist has steps). Events:
`peer.joint_proposed`, `peer.joint_state`, `peer.joint_contract`, `peer.joint_check`.

The joint checklist ([peer-link.md → Joint check](peer-link.md#joint-check--contract-joint_checkpy));
every route needs an **active** joint (**409** otherwise, **404** without one)
and answers `check` = `{id: "joint:<jid>", rev, by, written, steps: [{id, text,
expect, actor: "agent"|"human", side: "mine"|"theirs"}], mine: {results: {<step
id>: {result: "pass"|"fail"|"blocked"|"", note, at, by: "agent"|"human"}},
tested_sha, at}, theirs: {…}, verdict: "verified"|"failed"|"needs_you"|"in_progress"|"empty"
(`in_progress` until every agent step has a result, unless one is `blocked`),
ask: {state: ""|"asking"|"failed", at, until, error}, theirs_unsupported, link_id}`
(also `joint.check` on `GET …/joint`):

| Method | Path | Body → result |
|---|---|---|
| POST | `/api/peer/links/{id}/joint/check` | `{steps: [{text, expect?, actor?, side?}]}` → `{check, synced}` — a new revision (every result cleared); **400** no usable step; **409** before the safety number matches |
| POST | `/api/peer/links/{id}/joint/check/ask` | → `{check}` with `ask.state: "asking"` — my shared agent writes it (`peer_check write`) within 3 minutes; **409** my half has no shared session |
| POST | `/api/peer/links/{id}/joint/check/answer` | `{step_id, result: "pass"\|"fail"\|"blocked"\|"", note?}` → `{check, synced}` — one of the person's steps on MY side (`""` clears it), or one of MY agent's steps whose current result is `blocked` (its agent couldn't check it: `pass`/`fail` only, recorded `by: "human"`, the agent's note kept as `Agent: …` when none is given); **409** a step on their side, or an agent's step that isn't `blocked`, **404** unknown step, **400** bad result (or `blocked`/`""` on an agent's step) |
| POST | `/api/peer/links/{id}/joint/check/run` | → `{check, steps, synced}` — my shared agent runs my agent steps (their results cleared) and records them with `peer_check`; **409** none of mine, or no shared session |

## Config, providers, usage, settings

| Method | Path | Returns / accepts |
|---|---|---|
| GET | `/api/config` | `{default_program, provisioning_available, caps: {git, tailscale, ticketing, github, agent_mcp, team_runs}, home, repo_root, ide_name, onboarded, auth_mode, auth_enabled, fasttrack_depth, fasttrack_default}` — `fasttrack_default` is THE fast-track default batches start on — a list in New, Intake's Start together, and `POST /api/runs` with no `policy.lane` (Settings → Workspace "Fast-track goes as far as": a rung, or `off`, which is also what UNSET reads as; a single new session always starts Off and never reads it); `fasttrack_depth` is the rung an explicit depth-less `POST /fast-track` arms (never `off` — unset or Off reads as `pr` there, an explicit-arm convenience only). `caps` reports which optional integrations are usable right now; the UI hides absent features and shows "connect X" guidance wherever they are configured (a Settings screen, or an Intake tab via its `data-caps-need`). `caps.github` is true when **either** credential exists (`gh` authenticated **or** a token resolves) and is cached ~60 s (unlike its PATH-stat siblings it shells out to `gh auth status`, and this endpoint is hit on every page load); it gates one-click **Open PR** / **Merge**, and when false those buttons take the browser-URL path rather than disappearing — pushing is unaffected either way. `caps.agent_mcp` is the one non-boolean cap: `{"enabled": bool, "providers": ["claude", "codex"]}`. `enabled` says whether new launches attach the MindFlock MCP (`general.agent_mcp` and the `MINDFLOCK_AGENT_MCP` kill switch). `providers` lists the CLIs that get it automatically. On any error it reads `{enabled: false, providers: []}`. The MCP's `spawn_session` reads it to decide whether a worker can report back (see [mcp.md](mcp.md)). `caps.team_runs` is `{"split": bool, "together": bool, "max_pieces": int}` — `split` / `together` say which team-run shapes `POST /api/runs` accepts yet (both `false` until splits and one-for-all land; the create refuses them with a 400 off the same table); `max_pieces` (an int ≥ 2) is the most pieces a split may have (`MINDFLOCK_MAX_CHILDREN`, the cap on New's "Auto-split into up to N"). The UI shows the One-for-all PR choice, the split box and the Split into parallel pieces… action disabled with "coming next" while they are false |
| GET | `/api/providers` | `{providers: [{name, aliases, profiles: [{id, label}], default_selector}], default}` |
| GET | `/api/usage` | Rolling day/week/month/year token+cost totals per provider (Claude, Codex, …) |
| GET/POST | `/api/scroll-speed` | `{speed}` 1–20, applied live to tmux |
| GET/POST | `/api/window-refresh` | The scheduled coding-CLI keepalive: config + per-provider `last_fired` (Settings → Agent CLI) |
| GET | `/api/repos/suggest` | `{suggestions, home}` — folders to offer instead of a bare tree: the recency ladder (`general.last_repo_path` → live sessions newest-touched → the closed-session undo store), the folder the server was launched from, then a shallow sweep of the usual code directories. `POST /api/session-plan` builds its numbered folder menu from the **same** `server._recent_repo_paths` ladder (plus up to three name lookups drawn from the sentence), so the dialog's suggestion chips and a plan can never offer different folders |
| GET | `/api/repos/branches?path=` | The New-session dialog's **Base branch** menu for the folder in its Folder field: `{path, is_git, current, default, branches: [{name, remote, current}], truncated}` — local branches first (the checked-out one on top, then newest-first), then remote-tracking refs (`origin/HEAD` left out), capped at 200 (`truncated: true` past that); memoised 10 s per folder. `default` is what the server cuts from when nothing is chosen (origin's HEAD, else `main`/`master`, else the checked-out branch). A folder that is not a repo answers `is_git: false` with an empty list; only a blank path is **400**. The dialog sends a pick as `base_ref` + `base_branch` (same name) on `POST /api/instances`, and only for a plain new-worktree session — the server refuses the pair for in-place and provisioned ones |
| GET | `/api/browse?path=` | Directory listing `{path, parent, is_git, entries}` for the New-session dialog |
| POST | `/api/mkdir` | Body `{path, name}` (single path segment) → `{path}` |
| POST | `/api/paste-image?session=&name=` | Save a file pasted or dropped in the browser (raw bytes as the body) → `{path}`, the **absolute** path the UI then types into the PTY. `?session=<title>` stores it in that session's workspace under `.mindflock_pastes/` (git-excluded, so the agent needs no out-of-tree read); **omitting** `session` — the Assistant window, which has no worktree, and the phone UI — stores it under `~/.mindflock/pastes`. `?name=` keeps a sanitized copy of the original filename so the agent sees `report.pdf` rather than a blob; without it the extension comes from the content type (`image/png|jpeg|gif|webp|bmp`, else `.png` for any other `image/*`, else `.bin`). 400 on an empty body, 413 over 20 MB. Transient: each write prunes its own directory to the newest few pastes |
| GET | `/api/logs` | Tail of the server log (the UI's System-logs pane, 3 s poll) |
| GET | `/api/addons` | Addon manifests `{addons: [{id, label, managed, frontend}], failed: [{id, file, error, at}]}` — `failed` lists the user extensions skipped at startup (Settings → Extensions and Connections show them) |
| GET | `/api/doctor` | Dependency preflight: git/tmux/agent-CLI/uv checks with per-platform fixes, plus `gh` reported as **optional** (status `info`, detail "not found (optional — only PR create/merge and PR review need it; pushing uses plain git)" — never `fail`, so it can't trip the required-dependency exit); cached ~30 s, `?refresh=1` re-probes — after first re-reading the server's PATH (`pathenv.refresh()`), so a tool an installer just put in a directory created since boot (Homebrew, `~/.opencode/bin`) is found without a restart. Each check is `{id, label, status, detail, fix, docs, cmd, pkg, install, provider, required}`; `required` (added by the doctor addon, `setup_steps.tag_required`, unless the row already carries one) is whether the row can block getting started — every row but an optional extra (Tailscale, tailnet MTU, bubblewrap, cloudflared, local model, cache seeds, clipboard, gh); the setup checklist's *All checks (N)* lists only these. `provider` names the agent on the `agent-cli` / `assistant-cli` / `agent-auth` rows (what the UI's **Sign in to <agent>** button opens a login terminal for) and is `""` elsewhere. An installed agent CLI's `detail` carries its version (`/path/claude — 2.1.295 (Claude Code)`, from the provider's declared `version_args`). A `node` row appears only when a missing agent CLI installs with npm (cline). A `git-identity` row warns when git has no `user.name`/`user.email` (Setup → Connect GitHub fills them in). One `synced-<name>-cli` row (`warn`, `install: true`, so it joins the install plan) appears per agent CLI that settings sync is holding a synced value back for because it isn't installed here (`settings_sync.deferred_providers()`; Settings → Your devices → *Not applied here* → **Install …**). Also carries `version` (the running engine's version) and `state_notice` |
| POST | `/api/doctor/ack-state-notice` | Dismiss the downgrade notice; clears it and the cached payload |
| GET | `/api/doctor` → `install` | `{steps: [{id, label, cmd}], packages: [...]}` — everything missing that this machine needs, as ONE install: every system package in a single package-manager run, then each tool's own installer (`backend.doctor.install_plan`). On a Mac without Homebrew a `homebrew` step (Homebrew's official installer) comes first whenever there is a package to install. Empty `steps` = nothing to install. The script itself is never sent |
| WS | `/api/doctor/install-terminal` | A terminal running that install (rebuilt server-side from a fresh probe; the client sends no command) in the `mindflock_setup_install` tmux session — or, on a host without tmux (usually because tmux is one of the things being installed), under a plain PTY with the same contract: closing the window detaches without killing it, and a reopen replays its output. Reattaches to a run in progress; an error frame when there is nothing to install. **Privileged**: refused (error frame, close 4403) unless the request comes from this machine, carries a credential, or is from a trusted Tailscale device — and never when relayed by another MindFlock |
| GET | `/api/doctor/install-state` | `{running, exit_code}` of that install (`exit_code` null until it finishes). The first read that sees it finished refreshes the server's PATH, so the doctor's re-probe finds what was just installed |
| POST | `/api/doctor/install-close` | `{closed}` — tears the terminal down once the install finished; a run still going keeps going (`closed: false`) |
| GET | `/api/tailscale/health` | Read-only "Tailscale on this device" (Settings → Your devices card; `backend.tailscale_cli.health`): `{installed, path, kind: native\|app-bundle\|wsl-windows-host, source, os, windows_host, version, backend_state, running, auth_url, tailnet, user, tagged, tags, device: {name, dns, ips, os, id}, magicdns, magicdns_suffix, https, key_expiry: {at, days, expired, warn}, messages, issues: [{id, level: fail\|warn\|info, message, fix?, docs?}], admin}`. `issues` is worst-first, each with its one fix; ids `missing`, `wsl_windows_only`, `not_running`, `sign_in`, `stopped`, `machine_auth`, `starting`, `key_expiry` (warn at ≤ 30 days, fail once expired), `magicdns_off`, `https_off`, `wsl_two_nodes`. `user` and `auth_url` are blanked unless the caller is privileged (`auth.privileged`: this machine, a credential, or a trusted tailnet device) — a pending sign-in URL would let whoever opens it put this device on their tailnet; privileged callers also get `auth_qr_svg`. Reads the shared `status --json` snapshot (≈5 s); `?refresh=1` re-reads |
| POST | `/api/tailscale/login` | Privileged only (403 otherwise, and when relayed by another MindFlock). Starts `tailscale login` (or `tailscale up` when signed in but stopped) in the background and returns within seconds: `{ok, state, auth_url, auth_qr_svg?, error, fix}`. Never waits for the person — poll `/api/tailscale/health`. 409 with `error` + `fix` when it can't (not installed; Windows-only Tailscale from WSL; Linux without operator rights → `sudo tailscale up --operator=$USER`) |
| GET | `/api/mobile` | Mobile URLs + QR payload (Settings → Your phone): `{urls, qr_target, qr_svg, token, local_only, serve_mode, note, shared, phone_app}`. `phone_app` is step 1 on the phone, `{url, qr_svg, login}`: the Tailscale app download page and its QR, and the account to sign in as (this device's login, or on a tagged device the tailnet's one untagged-device owner; `""` for a caller that may not see this device's token). `shared` is the shared phone link (`general.shared_link`): `{enabled: false}`, or `{enabled, name, service, url, advertised, port, tagged, tags, tag, defined, approved, routed, machine, error, error_kind, operator_fix, policy, admin, steps, devices}` (`policy` is one HuJSON block for the policy file — `tagOwners`, `autoApprovers.services`, grants on this server's port and 443 scoped to the owner (`autogroup:member`→`autogroup:self`, tag↔tag, the owner's Tailscale login↔tag, login→the service — never `autogroup:member`→`autogroup:member`; the login only for a caller that may see this device's token, else a marked placeholder), a `tests` stanza — with comments on merging into existing keys): `tagged` / `defined` / `approved` / `routed` are tri-state (`null` = this device can't tell), `approved` is the node's `service-host` capability (not `services/<name>`, which a pending host also has), `routed` whether the service VIPs are in its `AllowedIPs`, and `steps` is the setup checklist `[{id, title, state: ok|fail|unknown, reason}]` (ids `operator`, `tag`, `define`, `policy`, `approval`, `phone`). Once `advertised`, the shared URL leads `urls` (this device's own URLs follow, labelled **This device**) and is what `qr_target` encodes, with one `token=` per device: this one's plus every paired device's. Saving `general.shared_link` through `POST /api/settings` validates the name (one DNS label, `svc:` prefix optional; 400 otherwise), applies it immediately and returns `shared_link` (the same object) beside `settings` |
| POST | `/api/mobile/shared/recheck` | Settings → Your devices → One link for all devices **Re-check**: re-reads the shared link's setup from Tailscale and re-applies whatever drifted (serve config gone, port or tags changed, last attempt failed). It also re-advertises a tagged host that still isn't approved. Returns the same payload as `GET /api/mobile`. Best-effort, never errors |
| GET | `/m` | The mobile UI page. `?s=<title>` opens it on that session; `&voice=1` is the voice deep link the two pocket-mode pushes carry — the page offers **Tap to hear <name>** and one tap speaks the session's line and listens ([web-ui.md](web-ui.md#voice-talking-to-a-session)) |

### First-run plan and Connect GitHub

`backend/web/addons/onboarding.py`. Every route that writes — a choice, a
token, the git config, a sign-in terminal — is **privileged** (this machine, a
credential, or a trusted tailnet device; never a relayed request): 403
`{"error": "do this on the device itself (or a signed-in one)"}` otherwise.

| Method | Path | Behavior |
|---|---|---|
| GET | `/api/setup` | **The first-run checklist** (`backend/web/core/setup_steps.py`; the empty grid's card, the Setup dialog, the "Setup 3/5" chip and `mindflock init` all draw it): `{steps: [{id, title, required, group, done, at, skipped, status: done\|skipped\|todo}], summary: {done, total, required_done, complete, next}, doctor: {ok, checks, install} \| null, tailscale: {status, detail, fix, docs} \| null, logins: [{name, installed, logged_in, login_cmd, default}], default_provider, report}` in the fixed order `tools → agent_login → repo → github → tracker → phone` (titles *Install Claude Code* — the default agent's product name, `setup_steps.tools_title`; *Install the tools* only when the agent is installed and something else is missing — *Sign in to your agent*, *Start your first agent*, *Connect GitHub*, *Connect your tracker*, *Use it from your phone*). `group` is `start` (the three `required` steps — *Get started*), `connect` (`github`, `tracker` — optional, counted) or `later` (`phone` — optional, folded in the UI and left out of `summary`). State lives in `general.setup` (per device) and is written only by the backend, only forwards: the doctor run marks `tools` (no check fails other than an optional extra — Tailscale, bubblewrap, cloudflared, local model, cache seeds, clipboard, gh) and `agent_login` (the default agent CLI is installed and its auth row is not a warning); the first session create only STARTS `repo` (`started`, carried on that step's row) and the first corroborated *working* reading of any session's agent (`setup_steps.note_agent_worked`, from the activity classifier) marks it done; this route marks `github` when a GitHub credential resolves (a Settings / env token, or the doctor's gh row signed in); saving the first ticket source (`PUT /api/settings/ticketing/sources` with a non-empty list) marks `tracker`; the owner fetching the QR (`GET /api/mobile`) or a phone loading `/m` from off this machine marks `phone`. A store from before `github` existed reads it as skipped once `tracker` was done or skipped. `summary` counts the `start` + `connect` steps only: `done` counts skipped steps; `required_done` retires the grid card, `complete` retires the chip, `next` is the first `todo`. `doctor` is the cached `/api/doctor` body (its checks refresh the first two steps before they are read; each check carries `required`, as on `/api/doctor`); `?refresh=1` re-probes. `tailscale` is the doctor's Tailscale row (status ≠ `ok` → the phone step offers "Install Tailscale" with `fix`). `logins` has one row per registered agent CLI: `logged_in` is `true` on evidence, `false` when the CLI names credential locations and none exist, `null` when it names nothing. `report` is the text a first `mindflock serve` printed at boot (`""` on a later boot) — shown under Settings → System logs → *First-start report*, not in the checklist. `general.onboarded` (in `/api/config`) is now DERIVED: `true` exactly when `repo.done`; a store that still carries the standalone flag is migrated on load |
| POST | `/api/setup/{step}/skip` | Skip an OPTIONAL step (`github` / `tracker` / `phone`): `{steps, summary}` after. 400 for a required or unknown step; **privileged** (`auth.may_configure`, 403). A done step stays done. There is deliberately no route that marks a step done — every step is marked by the hook that observes the thing itself |
| POST | `/api/setup/{step}/unskip` | Undo a skip (the step is `todo` again); same rules |
| GET | `/api/onboarding` | The first-run plan (`backend/onboarding.py`; Setup and `mindflock init` both draw it): `{steps: [{id, title, status: ok\|todo\|skip, reason, …}], next, done, os}` in the fixed order `deps → devices → agent → tailscale → github → repo`. `devices` carries `choice` (`first`/`join`/`""`) and `ask: true` while unanswered; `agent` carries `provider` and `sign_in` (a declared login flow); `tailscale` is `skip` unless joining or joined, else carries `fix`; `github` is `todo` until a token resolves and git has a name and email. While a join is still to come, `agent` and `github` are `skip` (joining brings the default agent and the token; never the sign-in). `next` is the first `todo` — Tailscale when it is what blocks a chosen join. Doctor results are reused for 20 s; `?refresh=1` re-probes. An unprivileged caller never sees the GitHub login, in any step (`connected` for a done `github` step; "your GitHub token" in a still-todo one) |
| GET | `/api/github/status` | `{connected, source: settings\|env:GH_TOKEN\|env:GITHUB_TOKEN\|gh-cli\|none, login, scopes[], user_error, gh: {installed, authenticated}, methods: [device?, gh?, token], device_flow_configured, token_url, identity: {name, email}, identity_suggested: {name, email}, credential_helper, flow, push_target: {repo, name, remote}}` — never the token. `push_target` is what **Check I can push** will ask, said before the click: the remembered repo (`general.last_repo_path`), its folder name and its `origin` URL (`""` for any that is unknown; local git reads only). `methods` is what this computer can offer, best first: `device` when an OAuth App client id is configured (`$MINDFLOCK_GITHUB_CLIENT_ID` / `github.oauth_client_id` / the build's `DEFAULT_GITHUB_OAUTH_CLIENT_ID`; `device_flow_configured` says so explicitly, so the dialog can print "Device flow is not configured in this build" instead of a dead button), `gh` when gh is installed, `token` always. `identity` is the GLOBAL git config; `identity_suggested` the account's name and `<id>+<login>@users.noreply.github.com`. An unprivileged caller gets only `{connected, source}` |
| POST | `/api/github/device/start` | Start GitHub's device flow → `{ok, state: "pending", user_code, verification_uri, expires_at, interval}` (the device code stays on the server). 409 `{ok: false, error}` without a client id or when GitHub refuses |
| POST | `/api/github/device/poll` | Advance it by at most one request to GitHub, never faster than its `interval` (`slow_down` adds to it) → `{state: pending\|done\|expired\|denied\|error\|idle, login, error, …}`. On `done` the token is saved as `github.token` (and stamped for settings sync) |
| DELETE | `/api/github/device` | Forget the flow → `{state: "idle"}` |
| POST | `/api/github/token` | `{token}` → checked with GitHub (`GET /user`) then saved as `github.token` → `{ok, login, error}`; 400 when GitHub rejects it (an unreachable GitHub saves it and says so) |
| WS | `/api/github/gh-login-terminal` | `gh auth login --web --git-protocol https --hostname github.com && gh auth setup-git` in a terminal (tmux, or a plain PTY); the command is fixed server-side. Closes 4403 for an unprivileged caller, 4500 without gh |
| POST | `/api/github/gh-login-close` | Tear that terminal down → `{ok}` |
| POST | `/api/github/import-gh` | Copy `gh auth token` into `github.token` → `{ok, login}`; 409 while gh isn't signed in |
| POST | `/api/github/identity` | `{name, email}` → `git config --global user.name/user.email` → `{ok, name, email}`; 400 for a blank name or an email without `@` |
| POST | `/api/github/git-credential` | Let git run **outside MindFlock** push to github.com over HTTPS with the connected sign-in (inside it, git already uses the sign-in as a per-process helper — see *GitHub credential* in [configuration.md](configuration.md)): `gh auth setup-git` when gh is signed in, else `credential.https://github.com.helper = !mindflock git-credential` in the global git config — refused (409) when git already has a helper for GitHub → `{ok, helper: gh\|mindflock, error}` |
| POST | `/api/github/push-check` | Body `{repo}` (default `general.last_repo_path`; a POST so the Origin check applies). `repo` must be the remembered repo or a session's folder or worktree — **400** for any other path → `{ok: bool\|null, repo, remote, id, message, fix}`: a `git push --dry-run` of HEAD to a scratch ref on origin with `GIT_TERMINAL_PROMPT=0` and, while a sign-in is connected, `mindflock git-credential` as git's helper for github.com (changes nothing; authenticates exactly as a session's push will, same helper). `id` names the cause when it fails (`https_auth`, `ssh_auth`, `identity`, `forbidden`, `not_found`, `other`); `ok: null` without a repo (*No project folder yet — start a session, then this checks its repo*) or without an origin — then `id: "no_origin"`, the message names the repo by its folder (*repo has no remote called origin yet, so there's nowhere to push*, never an absolute path) and `fix` is `git remote add origin <url>` |

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
usage_restored|turn_ended|question|message|presence_changed|driver_changed|
pr_state_changed|pr_review_changed|test_plan_ready|
test_plan_failed|test_plan_due|test_plan_checked|test_plan_step_failed|
test_plan_gave_up|
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
**finished** (`data: {"idle_for": <float>, "say", "has_question", "source",
"question"}` — the dwell, the spoken line, and the question it ended on) —
see below. `session.question` says a turn **ended on a question asked in
prose** (`data: {"title", "text", "ts", "kind": "question", "say"?}` —
`text` is the row's `asking.text`, `ts` the reply's stamp, `say` its spoken
line when it has one): once per distinct reply, on the settled-idle pass
that first sees it (the normal working→idle transition, a turn the poll
missed, or a reply file that landed a tick late), after the boot quiet
window; a reply that asked before the server first saw the session (a
restart) is never announced, nor one a person answered straight in tmux. A
**dialog** is still `session.activity_changed` with `new == "clarify"`.
Shell hooks (`~/.mindflock/hooks/session.question/`) get it like any event.
`session.presence_changed` fires when someone opens or closes a terminal on
the session, starts typing, takes or releases its keyboard, or it changes
owner (`data: {owner, driver, driver_name, viewers}` — the row fields).
`session.driver_changed` fires when the keyboard changes hands: `old`/`new`
are person ids (`""` = nobody), `data: {reason, name, previous_name}` with
`reason` one of `take`, `attach` (first terminal on a free session), `input`
(a keystroke on a free session), `release`, `left` (the driver's last
terminal closed) or `idle` (10 minutes without their input) — it is how the
previous driver hears they lost it.
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

**The spoken line rides the two "it finished" events.** A
`session.activity_changed` into `idle` carries `data: {say, has_question,
source, question}` — the session's [spoken record](#voice-talking-to-a-session-from-the-phone)
(the `Say:` line of the reply that just ended, else its digest); every other
transition keeps `data: {}`. `session.turn_ended` carries the same keys
beside `idle_for`. `question` is the question the session is asking right
now (the row's `asking.text` when its kind is `question`), else `null` — on
both events, whether or not `say` is. Each event is its own **channel** with its own memory: a
channel remembers the `(ts, say)` it last announced per session, and `say`
is `null` (`has_question: false`, `source: "none"`) when no reply newer than
that exists — a session that goes idle again without a new reply (a `/clear`,
a boot to a bare prompt, a wake, a CLI whose hook files no reply) never
re-speaks its last words, while the two events, which fire for the same turn
seconds to a minute apart, each carry it once. The phone page listens to the
fast `activity_changed` (its instant cue); the ntfy push body reads
`turn_ended`. The memory is dropped with the session.

A **sleeping** session's `offline` is not announced at all
([session-engine.md](session-engine.md#sleeping-agents)): the emitter records
it as `sleeping` and stays silent on the way in and on a wake back to `idle`,
so no channel says "agent offline" for an agent stopped on purpose. Any other
move out of it is announced with `old: "sleeping"` (a woken agent that starts
working, or lands on a dialog).

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
in-flight window), and fast-track (held exactly while the Fast-track record
reads `running` *and its driver lease is live* — a wedged chain the driver
cannot step goes lease-stale and announcements resume). The working→idle
marker transition also forces a **fresh limit-screen probe** (a forced miss
never refreshes the probe throttle, so a banner that paints late is caught by
the next tick's re-probe), closing the old 15s probe-cache blind window.

An **authoritative idle/clarify skips the 3s activity settle** entirely — a
hook marker cannot misread a frame, so the chip and the default-on
**Needs you** push react within one tick. `limit` never skips: both
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
| GET | `/api/mindflock/status` | `{running, pid, since, log, available, …}` — also `pr_enabled` / `issues_enabled` (switched on AND run on this device), `automation_here` (this device runs PR review and issue handling: always on a lone device; in a group, the member the synced `github.automation_device` names, else the lowest-keyed live member) and `automation_device` (where it doesn't: that member's host, `""` when this device runs them). Intake shows *runs on <device>* from these. `last_error` is the current problem or `null`: `{kind, message, at, source}` with `kind` one of `auth` (a tracker or GitHub refused the token — `source` names the ticket source id, the provider, or `GitHub owner/repo`), `config` (a source the pipeline skipped over a missing/invalid field — `source` + `field`, e.g. `sc` / `member_id`), `network` (a connectivity failure; `net_error` is its boolean twin) or `crash` (the child died). While running it is the skipped source first, else the newest auth/network line in the log tail from the last 180 s (a standing failure re-logs every poll and holds; it clears on its own after recovery). `config_errors` lists every skipped source (`[{source, field, message}]`). Once the child this server started has exited, `exit_reason` says why (`configuration error`, `crashed`, `exited with code N`, `killed by signal N`, `exited (another pipeline holds the lock)`) and `last_error` carries the pipeline's own exit record: a `config` error adds `fields` (the paths it listed, e.g. `ticketing.member_id`). Both are `null` while it runs and reset by the next start. `env_override` is `{provider, hides_ui_sources: true}` while `MINDFLOCK_TICKET_PROVIDER` pins the pipeline to an env-built source (every source saved in Intake is ignored), else `null` |
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
| GET/POST | `/api/settings` | The masked settings store (secrets never echoed). POST **rejects** `coding_cli.default_provider` when that CLI is not installed (a `ValueError`-derived 400) — an absent CLI can never become the launch default. Two `github.*` keys are maps, not scalars: `repo_settings` and `issue_repo_settings`, keyed by `owner/name`, hold the PER-REPO overrides the Intake tab's repo cards write — `agent`, `base_branch` (PR review only; accepted but dropped for issues, whose work branches off the repo's own default), `min_age_minutes`, `skip_authors`. An absent repo key, or an absent field inside one, inherits the flat `github.*` value; a blank is dropped rather than stored, which is how a card field means "inherit the default" instead of "set it to empty" (`_repo_overrides` / `REPO_OVERRIDE_KEYS` in `backend/config/settings.py`). `github.automation_device` (a device key, synced) picks which of your devices runs PR review and issue handling; a save that changes whether this device runs them, or flips `github.enabled` / `github.issues_enabled`, reconciles the pipeline (restarted, started or stopped — its halves are wired at boot). A change that arrives by sync or a roster change is picked up within 30 s |
| POST | `/api/settings/test/shortcut` | Validate a Shortcut token (body `{api_token}` or the stored one) → `{ok, member_id, name, mention_name}` for auto-fill, or `{ok: false, error}` |
| GET | `/api/settings/workspace-paths` | Settings → Workspace's *where do my worktrees live?* Query `dir` (optional) = the folder being typed; without it, the saved `repository.workspace_dir`. → `{sessions_dir, tickets_dir, tickets_default, tickets_state: "ok·create·not_dir·not_writable", tickets_warning, env_override}` — `sessions_dir` is where sessions you start get their worktree (`<config dir>/worktrees`), `tickets_dir` where ticket/PR sessions and fresh clones go (`MINDFLOCK_WORKSPACE_DIR`, else `dir`/the saved field, else `./workspaces`; a relative one anchored at the ticket pipeline's root, `~` expanded), both absolute. `tickets_warning` is one plain sentence when the folder is missing ("MindFlock creates it") or can't be written. A `POST /api/settings` that sets `repository.workspace_dir` to a `~/…` path stores it expanded (the field is machine-local, never synced) |
| POST | `/api/settings/test/github` | The Connect GitHub dialog's **Test** — a real `GET /user` with the token the one chain resolves (settings → `$GH_TOKEN` / `$GITHUB_TOKEN` → gh CLI → config.toml, deprecated) plus one `GET /repos/{repo}` against the first configured repo (`github.repos`, else `github.issue_repos`) → `{ok, token_source: "settings·env:GH_TOKEN·env:GITHUB_TOKEN·gh-cli·config.toml·none", login, scopes[], user_error, repo, repo_ok (null when no repo is configured), repo_error, can_push, private, default_branch, detail, gh_installed, gh_authenticated, gh_detail}`. `ok` is the whole verdict (token resolved, GitHub accepted it, the repo check passed or there was none); `detail` is its one-line reading. Never the token. **Always 200.** Backs the dialog's Test (no other surface has a GitHub test button any more) |
| POST | `/api/settings/test/github-repo` | Body `{repo: "owner/name"}` — the per-repo twin of the row above: that one answers "is there a credential", this one answers "does it reach THIS repo", which is the failure people actually hit (a typo'd slug, a private repo the token has no scope for). One `GET /repos/{repo}` with the resolved token → `{ok: true, name, private, default_branch, can_push}` — `name` is GitHub's own `full_name`, `can_push` the token's push permission (reviewing pushes nothing, issue handling needs a branch, so read-only is worth saying out loud). Otherwise `{ok: false, error}`: a slug that isn't `owner/name`, no token available, an unreachable `api.github.com`, or GitHub's own `message` — a 404 reads as "no such repo, or this token cannot see it", because that is also what a private repo returns. **Always 200**, like the other probes, so branch on `ok`. Backs the **Test access** button on every repo card in Intake → Pull requests / Issues |
| POST | `/api/connections/test/agent` | An agent card's **Test** → `{ok, detail, cli, auth}`: the same probe as `/api/settings/test/agent`, plus `detail`, one plain sentence (*Signed in as you@example.com*, *Signed in*, *Signed in with an API key*, *Installed, not signed in yet*, *Claude Code isn't installed*) — never the file the login was found in. Body `{provider}`; none = the default |
| POST | `/api/settings/test/agent` | Probe an agent CLI → `{ok, cli, auth}` (binary resolvable + login evidence). Body `{provider}` picks any known provider (Settings → Agent CLI's **Test agent CLI** picker); no body = the default. The rows keep the `agent-cli` / `agent-auth` ids whichever provider was probed, so the **Sign in** button reads them as usual |
| POST | `/api/settings/test/local-model` | Probe a local model server (body `{runtime, base_url, model}`, each falling back to the stored value — so it can be tested *before* saving) → `{ok, runtime, base_url, models, error, supported_agents, default_base_urls}`. `models` turns the model field into a dropdown; `supported_agents` lists the installed CLIs that can actually be pointed at it (never `claude`) |
| GET | `/api/settings/providers/ticketing` | The ticketing-provider registry (fields per provider for the Intake → Tickets source cards) |
| POST | `/api/settings/test/ticketing` | Validate a ticketing source's connection with its provider's own `test_connection` → `{ok: true, message, member_id, name, states?, project?, project_name?, workspaces?}` or `{ok: false, error, message}` (always 200; branch on `ok`). `states` is the provider's live workflow-state list (Asana: its sections), fetched right after a pass so the card's pickers fill without a second call — a failed listing does not fail the test, the message says so. Asana with no workspace set: the token's workspaces are looked up, one fills `project` (+ `project_name`), several come back as `workspaces` for the form to offer. With the STORED token (none, or the mask, in the body) only for a caller `auth.may_configure` allows (403 otherwise): the body can name the `base_url`, and a stranger mustn't have this device send its saved token there. Inline credentials stay testable by anyone. **`record: true`** (owner only) stamps the verdict onto the stored source named by `id` as `last_test: {ok, at, message, member_id?, states?, workspaces?}`, fills a blank `member_id` (and a blank Asana `project` when there is one workspace), stamps for sync, and returns the masked source as `source` — this is what "connected" is derived from (see Connections below), and the Intake card sends it on every Test, by click or automatically after a save |
| POST | `/api/settings/ticketing/states` | Live workflow-state list for a ticketing source (the stored token owner only, as Test above). The card's pickers open on `last_test.states` and call this on focus to refresh |
| GET/PUT | `/api/settings/ticketing/sources` | The multi-source ticketing config (per-source provider/repo/state). PUT replaces the list and **validates every new or changed source against the provider catalog** (`backend.ticket_ingestion.providers.validate_source`): a required field left blank, a value of the wrong shape (`validate: url·email·key·gid`), or `assignee_scope: "anyone"` with no ingest state (no label either, on Shortcut) is a **400** `{error, errors: {<id>: {<field>: <message>}}}` and nothing is written. Fields the catalog marks `auto` (Shortcut's `member_id`, Asana's workspace) are required before the source counts as *connected*, not before it saves — Test fills them. A source identical to its stored copy is never re-validated, so one saved before a rule existed cannot block edits to its neighbours. Secrets: `api_token` absent, `""` or the mask keeps the stored token; an explicit **`null` clears it** (the card's Clear control). `last_test` in the body is ignored; the stored verdict is carried over while the provider and its catalog fields are unchanged and dropped otherwise, so a new token is untested until Test runs again |
| GET/PUT | `/api/settings/auth-profiles` | The auth-profiles list (multiple Claude accounts / OpenRouter keys — see [accounts.md](accounts.md)) plus `default_profile` and the `kinds` catalog. Same masked round-trip as the ticketing sources: `api_key` reads back as `•••set`, and a PUT that sends `""`/the mask keeps the stored key (matched by `id` — so **renaming** an id counts as a new profile and must re-send the real key; a key-kind profile that would land keyless is a 400, not a silent no-auth store). PUT validates everything **before writing anything** (a 400 always means nothing changed): ids (slug, unique), kinds, and a body `default_profile` against the incoming list. `account`-kind profiles get their isolated config dir created (0700). GET reports an env-pinned default as `default_profile` with `default_profile_env` + `default_profile_locked: true` when `$MINDFLOCK_AUTH_PROFILE` is set in the server's environment (it wins over the stored value at launch, so reporting the stored one would have the screen name one identity while every session runs as another). GET also derives `resolved_config_dir`, `login_command` and `supported_agents` per profile for the Settings → Accounts cards and the New dialog's agent steering. Removing a profile that live sessions are **pinned** to is a **409** naming them (`{error, in_use: [title]}`) — they would fall back to the CLI's own login without being told; resend with `force: true` to proceed anyway. Sessions that merely *inherit* the app default never block a removal |
| POST | `/api/settings/test/openrouter` | Validate an OpenRouter key (body `{api_key}` or `{profile_id}` for the stored one — owner only, `auth.may_configure`, 403 otherwise — optional `base_url`) → `{ok, label, usage, limit, models, error}` — the key's real spend from OpenRouter's `/key` plus the model list that turns the profile's model field into a picker. Always 200; branch on `ok` |
| GET | `/api/providers/manage` | Custom coding-CLI providers (user TOMLs) for the Settings CRUD screen. Each entry carries `plan_supported` — whether the Map can show the CLI's plan and send it Go (the New Session "Plan first" box is offered only then) |
| POST/PUT/DELETE | `/api/providers` · `/api/providers/{name}` | Create / update / delete a custom provider TOML. The body may carry `launch_args` (a list of saved flag tokens) alongside `resume_flag`/`skip_perms_flag`/`trust_patterns`/…; it is validated (400 on invalid) and all string values are TOML-escaped via `json.dumps`, so quotes in names/flags/patterns can't corrupt the file. **PUT patches the file in place**: only the keys the body carries change (an empty string / list removes that key), and everything else — `prompt_arg`, `oneshot_args`, the effort keys, `[usage]`, `[activity]`, `[connect]`, `[peer]`, comments, order — survives byte-for-byte (`providers/config.py` `patch_toml_tables`; a TOML with no writer in the stdlib is edited as text). A file that doesn't parse is refused with **409** naming the file and the error rather than overwritten. A PUT to a name with no file creates it. |
| PUT | `/api/providers/{name}/binary-path` | `{path}` sets THIS device's binary for any known provider, built-ins included — `coding_cli.binary_paths[name]`, which the launcher honours before PATH; `{path: ""}` clears it. Device-local: `settings_sync.LOCAL` never carries it. A path that isn't an executable file is saved with a `warning` (install it next), not refused; an env pin (`MINDFLOCK_PROVIDER_BIN_<NAME>`) is named in the warning because it still wins. Answers `{ok, binary_path, warning, status}` — `status` is the provider's fresh `/api/providers/status` row. **404** for an unknown provider (or `generic`); **403** for a caller `auth.may_configure` refuses. |
| GET | `/api/providers/status` | Per-provider connection status → `{providers: [{name, aliases, binary, installed, path, authenticated, auth_detail, auth_known, auth_probe, login_command, has_login, install_hint, launch_hint, is_default, binary_override, binary_override_env}], default, errors: [{file, error}]}`. The catch-all `generic` provider is omitted. Settings → **Agent providers** renders one row per entry: `installed`/`path`, the sign-in chip (`authenticated` + `auth_detail` = signed in; `has_login: false` = *no sign-in needed*, the provider declares no login step; `auth_probe` says whether a miss means anything — a CLI that named its credential files/env and has none is *not signed in*, one that named nothing is *unknown*), the **Sign in** button (`has_login`, not yet authenticated), the install command, and the **Binary path** field (`binary_override` is this device's saved `coding_cli.binary_paths[name]`; `binary_override_env` the env var pinning one instead). `errors` lists the custom TOMLs in the providers dir that failed to load (file name + parse/validation error — also logged at load time), shown as red rows. Settings → **Agent CLI** reads it for the default-provider picker (every provider, the absent ones disabled) and self-corrects a missing default. |
| WS | `/api/providers/{name}/login-terminal` | PTY↔websocket bridge to a throwaway tmux session (a plain PTY on a host without tmux) running the provider's login flow in `$HOME`. Opened by the **Sign in to <agent>** button in Setup's *Sign in to your agent* step and on the doctor's `agent-auth` row. Takes `?profile=<id>` to run the login under an auth profile's isolation env (the credential lands in that account's config dir — see [accounts.md](accounts.md)). Closes 4500 with an `{type:"error"}` frame for an unknown provider/profile or a spawn failure. **Privileged** like the install terminal (close 4403 otherwise): a sign-in lands credentials on this device. |
| POST | `/api/providers/{name}/login-close` | Best-effort teardown of a login session (`?profile=<id>` matches the terminal above); the sign-in window calls it on close. `200 {ok: true}`; **403** for a caller `auth.may_configure` refuses. |

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
every outside thing MindFlock talks to, one row each, for the Settings →
Connections dashboard. Cached ~4 s (`refresh=1` bypasses); no uncapped probe runs on
load — `gh auth status` and `tailscale status` are capped at 5 s and cached,
every other row reads an existing signal, and a row's `test` is the live check. Response `{connections: [row…], summary}`:

```jsonc
{
  "id": "agent",                 // stable; see the id list below
  "name": "Coding agent · Claude",
  "kind": "agent",               // agent | github | tracker | devices | mcp | database | notifications | extension | system
  "status": "connected",         // connected | needs_attention | off
  "detail": "signed in (…) · /usr/bin/claude",
  "action": {"label": "Sign in", "kind": "dialog", "target": "agent-sign-in", "provider": "codex"},
                                 // kind: dialog | screen | route | copy
  "test": {"endpoint": "/api/connections/test/agent", "method": "POST", "body": {"provider": "claude"}},  // or null
  "fix_command": "",             // a copyable command when one fixes it ("" otherwise)
  "fix": "", "docs": "",
  "updated_at": "2026-10-10T12:00:00+00:00",   // the last test, else when the row was built
  // kept for one release (the old client's fields):
  "legacy_status": "connected",  // connected | attention | not_connected (projection of status)
  "connection": "connected",     // = status (the name the ticket-source rows used)
  "settings_screen": "coding", "test_endpoint": "/api/settings/test/agent",
  "purpose": "…", "required": true
}
```

`summary` = `{connected, needs_attention, off, total, attention}` (`attention`
= `needs_attention`, the old key). Rows arrive grouped by `kind` in the order
above. One broken source drops its own rows, never the list.

| Row (id) | connected | needs_attention | off | action / test |
|---|---|---|---|---|
| Agent CLI, one per provider (`agent` = the default, `default: true`; `agent:<name>`) | installed and signed in, or no sign-in needed, or the login can't be probed | installed, login probe found nothing (a CLI with no login flow that names the API keys it reads and has none — aider — gets the doctor's fix, *set one of the API keys … reads (…)*, and a **Set up** screen action instead of **Sign in**) — or the **default** CLI is not installed | not installed (`fix_command` = install command) | `dialog agent-sign-in` (`provider`, `profile` when the default auth profile is an account of it) / `dialog agent-install` (`provider`; the default agent not installed — the UI runs the doctor's one-click install) / `copy` (another agent's install command) / `screen coding` (**Change agent**, the connected default) / `null` (a connected other agent: Test is its action); `account` = the signed-in account when the CLI's config names one (Claude Code's `oauthAccount.emailAddress`); Test `POST /api/connections/test/agent {provider}` |
| `agent-config:<file>` | — | a custom provider TOML failed to load | — | `screen providers` |
| GitHub (`github`) | a token (or a signed-in gh) and the last Test did not fail; `account` = its login | the last `POST /api/settings/test/github` with this credential failed (bad token, repo it can't see) | no credential | `dialog connect-github`; `configure_dialog: "connect-github"`, `token_source`, `pr_review_enabled` |
| Ticket source (`ticketing`, `ticketing:<id>` with >1) | its `last_test` passed within 24 h | see below | no credentials | `dialog intake` (`tab: "tickets"`, `source_id`); Test `POST /api/settings/test/ticketing {id, record: true}` |
| Your devices (`devices`) | ≥1 paired device reachable | every paired device unreachable (named) | none paired | `screen devices` |
| Your phone (`phone`) | a phone opened `/m` in the last 7 days (in-memory last load, else the setup step's `phone.at`) | — | otherwise | `screen mobile` |
| People (`peers`) | ≥1 peer link up | every link down (named) | no links | `screen peer` |
| MCP (`mcp`) | auto-attach on and the last launch attached (or none yet) | on, but the last launch got no tools (names the provider) | `general.agent_mcp` off | `screen orchestration` |
| Database (`database:<id>`, or `databases` when none) | last Test passed | last Test failed, or never tested | no saved profiles | `dialog db-explorer`; Test `POST /api/dbclient/connections/<id>/test` |
| ntfy (`ntfy`) | topic set, enabled, last push ok (or none yet) | the last push failed | no topic, or turned off | `screen notifications`; Test `POST /api/notify/ntfy/test` |
| Extension (`extension:<id>`; `extension-failed:<dir>`) | loaded (off when disabled) | failed to load at startup (file + error) | — | `screen extensions` |
| Git (`git`), Tailscale (`tailscale`) | present | — | absent | `copy` the install command, else `screen doctor` |

The GitHub row's `fix` when there is no credential is the one remedy sentence
above. A GitHub Issues source that still carries a per-source token from an
older release reads *needs attention* with a "move it to Settings →
Connections → GitHub → Connect" detail.

A ticketing row's `status` is derived from its stored test, never from the
mere presence of a token (`backend.web.addons.connections.source_test_state`):
`connected` when `last_test.ok` is within the last 24 h and every field Test
fills in is filled, `off` when the source has no credentials, else
`needs_attention` — with `message` saying why (the failed test's own message,
*not tested yet — run Test*, *last tested over a day ago — retest*, or the
field Test still has to fill) and `tested_at` (ISO-8601, or `""`). `detail`
carries the message. The Intake → Tickets card shows the same three states
from the same rule (`frontend/src/components/intake/sourceForm.ts`).

Where the verdicts come from: `POST /api/settings/test/github` records its
answer for the GitHub row (in memory); `POST /api/dbclient/connections/<id>/test`
records into `dbclient_tests.json` beside `dbclient.json` (no credential; an
edit or delete of the profile clears it); `/m` loads from off this machine
stamp the phone row; `mcp_attach.last_launch()` is the last `note_launch`.
The client re-reads every 30 s while the screen is open and on a
`mf:connections-changed` window event.

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
new-session templates (`~/.mindflock/session_templates.json`), the ONE list
behind the New dialog's Templates strip, its Task box's "Insert a saved
prompt…" list and the sidebar Prompts bar (a template that is only a prompt is a saved
prompt). Settings sync carries it between devices as the `store:templates`
unit, keyed by name. Managed by the SPA's **Schedule** surface (the top
bar's Schedule, `Alt+S`; New's "All templates…"; the palette's "Schedule…" /
"Templates…"); the addon ships no
frontend module any more (`module: null`, `builtin_ui: true`). A browser's
legacy per-browser saved prompts (`localStorage mindflock.prompt_presets`)
are pushed here once on its first load, deduped by name+body (a same-name,
different-body clash is kept as `<name> (saved prompt)`):

| Method | Path | Returns |
|---|---|---|
| GET | `/api/templates` | `{templates: [{name, program, repo_path, prompt, provisioned, workspace_strategy, in_place, init_repo, schedule?, autopilot?}], runs: {<name lower>: {next_at, last_at, last_session, last_trigger, last_error, schedule_label, history, last_skip?, last_skip_at?, paused?, skipping_at?, tz?}}, runs_here, runs_on, tz}` — `tz` is this device's time-zone abbreviation (`EDT`), which a schedule's `at` is in (on a scheduled template's row, as of its next run); `runs` is THIS device's record of each template's runs (epoch seconds; only templates with a schedule or a past run appear); `schedule_label` is the schedule in words ("Every 30 minutes", "Weekdays at 21:00", "Once on Tue 28 Oct at 21:00", else `Cron <expr>`); a one-off whose minute is behind it on this device is **spent** — `next_at` null, `once_done: "ran" \| "skipped" \| "failed"` (how its one slot went, from the history), and the label reads "Ran once on …" / "Skipped once on …" / "Failed once on …" (the template keeps its schedule; spent-ness is this device's run record, so the badge counts it only when its run actually failed); `history` the last 20 runs, newest first (`{at, trigger, status, session?, autopilot?, error?, reason?, skipped?}` — `status` is `started` \| `failed` \| `skipped`; a run refused before it could start carries `reason`); `last_status` the newest history entry's `status` (`""` with no history) and, when that is `failed`, `last_reason` (`agent_missing` \| `no_folder` for a launch refused up front, `launch_failed` for one the create refused) — "Last run failed: <last_error>" and the top-bar badge read these; each template in `templates` also carries `agent_installed` (bool: its agent — `program`, else the default — can launch on this machine; a response field, never stored); `last_skip` is set when a scheduled run was skipped after the last start or failure; `paused: true` for a paused schedule (its `next_at` is null); `skipping_at` names the slot "Skip next" will pass over (`next_at` is then the one after it); `runs_here` says whether schedules fire on this device, `runs_on` names the device that does when it isn't this one |
| POST | `/api/templates` | Save/overwrite a template (upsert by name, ignoring case) → `{template, templates}`; owner-only (`may_configure`). `schedule` (optional): `{at: "HH:MM", days: "daily" \| "weekdays"}`, `{cron: "<minute hour day month weekday>"}` (standard 5-field cron, names, ranges, steps and `@daily`-style macros) or `{once: "YYYY-MM-DDTHH:MM"}` (a one-off: one run on that local minute, then the schedule is spent — see `once_done` above; it stands alone, never with `at`/`days`/`cron`), all local time, any one optionally with `paused: true` (keeps the cadence, starts nothing) and `skip_until: <epoch>` (every slot at or before it passes without a session — the Schedule surface's "Skip next"); both are stored only when set; 400 for a bad time/days/expression/date, one that fires more often than every 15 minutes, a NEW one-off whose minute has already passed (the one-off a template already carries re-saves as is, spent or not), and for a scheduled template with no folder (`repo_path`, unless `provisioned`) or no prompt. `autopilot` (optional): `commit` \| `push` \| `pr` — each Run now / scheduled run is armed to carry its work that far when its turn ends (400 for anything else). A template without a schedule / autopilot stores no such key. **409** `{error, detail, reason}` (nothing saved) for a template that couldn't do its job: a **scheduled** template whose agent isn't installed → `reason: "agent_missing"` (+ `agent`), *"codex isn't installed — install it or pick another agent before scheduling this template"*; Fast-track `pr` (or `merge`) while GitHub isn't connected → `reason: "github_required"`, *"Fast-track “Open a PR” needs GitHub — connect GitHub, or pick Commit or Push"* |
| GET | `/api/templates/cron-preview?expr=` | What a cron field means, with the save's own rules: `{ok: true, cron, label, next: [3 epochs]}` or `{ok: false, error}`. The Schedule editor asks it in every mode — Every day / Weekdays at 07:30 ask as `30 7 * * *` / `30 7 * * 1-5`, Once asks as its minute (`2026-10-28T21:00` → `{ok: true, once, label: "Once on Wed 28 Oct at 21:00", next: [its epoch]}`, or `{ok: false, error: "Pick a date and time that are still ahead."}` once it is behind the clock) — so each shows its next runs. `label` words common shapes ("Every day at 07:30", "Weekdays at 21:00", "Mondays…", "Monthly on day 1 at 09:00", "Every Feb 29 at 00:00", "Every 30 minutes"), else `Cron <expr>` |
| DELETE | `/api/templates/{name}` | Remove a template → `{deleted: bool}` (false for a name that wasn't there); owner-only |
| POST | `/api/templates/{name}/run` | **Run now** (also `mindflock run <name>`): create a fresh session from the template, its prompt already sent, through the same path as `POST /api/instances` → its **202** body (the new row + `prompt_delivery`), then arm its `autopilot` (a template with one never runs in place: it gets its own worktree). Titled `<template slug>-<mon><day>` (`rivian-update-oct13`; `-2`, `-3`… when a session of that name exists or, for a worktree template, its branch is still in the repo from a closed run). **409** `{error, detail, reason}` when it can't launch, checked before any session is made and recorded in its `history` as a failed run (`status: "failed"`, `reason`): its agent isn't installed → `reason: "agent_missing"` (+ `agent`), `detail` *"Couldn't start <name>: <agent> isn't installed"*; no folder (it would run in the server's own directory) → `reason: "no_folder"`. Whatever the create itself refuses comes back with its own status plus `detail` *"Couldn't start <name>: <error>"* and `reason` (the create's, else `launch_failed`); **404** unknown template, with `detail` *"There's no template named <name>."*. Not owner-only — starting a session isn't configuring anything |

**Schedules** (`core/template_schedule.py`; cron math in `core/cronspec.py`).
A loop (registered by the addon's startup, every 30 s; off under pytest)
starts each scheduled template's session the same way Run now does,
recording `last_trigger: "schedule"`; a schedule that can fire more than once
a day titles its runs `<slug>-<mon><day>-<HHMM>`. A slot is identified by its
local date (`{at, days}`) or local wall-clock minute (`{cron}`), so a DST
change neither skips nor repeats one (the 01:30 a fall-back night has twice
is one slot); a scheduled run is **skipped** — recorded in the history,
`template.run_skipped` emitted — while the template's previous run is still
working, waiting on a question or at a usage limit (Run now always starts);
a scheduled run that can't start emits `template.run_failed` — one refused
up front (its agent isn't installed, no folder) makes no session and is
recorded as a failed run (`status: "failed"`, `reason`, the same refusal Run
now answers 409 with), so the card says *Last run failed* and the top-bar
badge counts it, every night until it is fixed; the event's data carries
`error`, `reason` and the refusal's `detail`; the slot is recorded (`last_slot`) before the session is
created, so a slot never fires twice; after downtime only the most recent
missed slot fires; a **paused** schedule spends its slots without firing,
and one with `skip_until` spends every slot up to it the same way (the
history notes *you skipped this run*), so resuming never replays a slot that
fell inside the pause — neither switch re-arms the schedule; a schedule counts only from when it was saved (saving 21:00
at 22:00 waits for tomorrow; changing the time or days re-arms it). In a group
of devices only the automation device (the one running PR review and issue
handling) fires — the others mark each slot spent without firing, so a device
that takes the role over never replays one. The schedule syncs with the
template; the run record (`template_runs.json`, `$MINDFLOCK_TEMPLATE_RUNS_FILE`)
is per device.

**Notify** (addon id `notify`, prefix `/api/notify`) — the reference addon for
the generic extension path (see [docs/extensions.md](extensions.md)):

| Method | Path | Returns |
|---|---|---|
| GET | `/api/notify/config` | `{rules: [{id, label, event, old, new, title, body, enabled}]}` — the event → notification rules, applied client-side by `static/addons/notify.js` and server-side by the ntfy channel |
| POST | `/api/notify/rules/{rule_id}` | Enable/disable one rule (for **both** channels) |
| GET | `/api/notify/ntfy` | The ntfy channel's state: `{enabled, server, server_default, topic, has_token, click_url, agent_words, ask_popup, prose_questions, configured, active, public_server, subscribe_url, qr_svg, suggested_topic, last}`. Never the token — only `has_token`; `agent_words` is `notifications.ntfy_agent_words` (pocket mode, below); `ask_popup` is `notifications.ask_popup` (pop a session's question up mid-screen — desktop and phone follow it); `suggested_topic` is a fresh random name, `last` is `{ts, ok, error}` of the most recent push |
| POST | `/api/notify/ntfy` | Save `{enabled?, server?, topic?, token?, click_url?, clear_token?, agent_words?, ask_popup?, prose_questions?}` (only the keys present are touched); returns the `GET` view, plus a `note` when something was rewritten. `clear_token: true` removes the saved token — the escape hatch from "empty = keep", and it wins over a `token` in the same payload. `agent_words` is the pocket-mode toggle, saved as `notifications.ntfy_agent_words` (synced like the rest of the channel); `ask_popup` is saved as `notifications.ask_popup`. `400 {error}` on an invalid topic/server URL |
| POST | `/api/notify/ntfy/test` | Send one test push — `{ok, error}`. Takes its config from the body when supplied, so a topic can be verified before it is saved; exempt from the rate cap |
| POST | `/api/notify/act/{token}` | A dialog push's **Yes** / **No** button, pressed on the phone. **Public** — no bearer token, since the ntfy app holds none of ours: the gate opens POST on exactly `/api/notify/act/<32 lower-case hex>` and nothing else under `/api/notify` ([Authentication](#authentication)). The token is the credential (`core/voice_actions.py`): minted when the push is sent, it stands for one `(session, dialog id, key)`, is good **once** and for **ten minutes**, lives in memory only (a restart forgets every outstanding button — the right failure for a button older than the server), at most 200 live with the oldest evicted. Consuming it also spends its siblings (the other buttons of the same dialog), so after a Yes the No can't land on an identical prompt that comes up later under the same id. It then does exactly what `POST /api/instances/{title}/answer {keys: [key], dialog_id, by: "user"}` does, through the same helper — so the dialog-id pin, the per-session answer lock, the budget lock and the "just answered" guard all apply — and answers the same way: **200** `{ok: true, activity_before}`; the answer route's own **409**s passed through (`dialog_changed` when the prompt on screen is no longer the one the button was made for, `dialog_answered`, not waiting, over budget); **502** when tmux refused the key. **404** `{error: "that button has expired — open MindFlock to answer"}` for a token that is unknown, already used or older than ten minutes — the same answer for all three, on purpose — and for a session that is gone |

The ntfy channel is a **server-side** delivery path for the same rules
(`web/core/ntfy.py`): the server publishes to the topic over ntfy's JSON publish
API (POST to the server root, topic in the body — session titles are arbitrary
UTF-8, which an `X-Title` header could not carry), so an alert arrives with no
browser tab open. It is off until configured, resolves through
env → `settings.json` → defaults (`MINDFLOCK_NTFY_ENABLED` / `_SERVER` /
`_TOPIC` / `_TOKEN` / `_CLICK_URL`, where an env-supplied topic is an implicit
opt-in for headless boxes), and is capped at 60 pushes/hour per process.

**Pocket mode: the agent's words in the push.** While the phone is locked
nothing on the web can speak, so for the two pushes that say "it finished"
or "it is asking" the push body itself is what a phone that reads
notifications aloud (iOS Announce Notifications, a read-aloud app on Android)
says through the earbuds. With `notifications.ntfy_agent_words` on (the
default; the **Include the agent's words in pushes** toggle), the
`session_idle` body is the event's `data.say` — the session's
[spoken line](#voice-talking-to-a-session-from-the-phone) — and the
`needs_input` body is the dialog read off the screen at fire time (the same
parse `GET /dialog` serves): its `question`, then ` — ` and the `command` it
asks about when there is one, capped at 300 chars — for a dialog no parser
recognizes, its best-effort question line — or, for a question asked in
prose (`session.question`, which `needs_input` also matches: the rule's
`also` list in `GET /api/notify/config`), the question itself. A turn that
ends on a question pushes **once**: the `session_idle` ("has finished") push
is skipped for a `turn_ended` whose `data.question` is set while
`needs_input` is on. Either falls back to the
rule's own template when there is nothing to say; titles never change, and
with the setting off every body stays generic (the words would travel through
the ntfy server — the Settings toggle says so, and names the public `ntfy.sh`
when that is the server). When the body is the agent's words the phone URL is
**not** appended to the text (a read-aloud app would spell it out); the click
action carries it anyway. For these two rules the click URL is the phone
page's **voice deep link**, `/m?s=<title>&voice=1`
(`mobile_announce.click_for(session, voice=True)`), whether or not the words
are on: the page opens on the session, and one tap speaks the line and
listens. A `needs_input` push whose dialog parsed with a `yes` option also
carries up to three ntfy **action buttons** (`actions` in the JSON publish —
ntfy shows at most three), independent of the words toggle: **Yes** and,
when the dialog offers one, **No** as `http` POSTs to
`<base>/api/notify/act/<token>` (`clear: true`, so the notification goes
when pressed), then **Open** as a `view` of the click URL. `always` is never
offered — a standing permission must not be one tap away — and a free-text
question gets no buttons. `<base>` is this device's own phone URL with `/m`
stripped (`mobile_announce.act_base`); with no tailnet, or while the shared
phone link is the advertised URL (it is answered by whichever device is up,
and a POST landing on another device finds no such token), there are no
`http` buttons and the push carries Open alone. Access tokens never travel
in a button URL (`strip_token_param`). On Android the buttons fire from the
lock screen; iOS runs an `http` action only after unlock, and neither
platform can take a spoken reply from a notification.

Both channels collapse repeats per **(session, rule id)** for 5 s
(`notify.py::_DEDUPE_SECONDS` / `notify.js::DEDUPE_MS`), not per (session,
event). Three rules ride `session.activity_changed`, and an event-keyed window
let whichever fired first swallow the rest — a default-on **ran out of usage**
push eating a default-on **Needs you** push, and, since the same key is
also the browser `Notification` tag, replacing its still-visible popup. The
window is a *flap* collapser and nothing more; it is meaningless at turn
cadence, which is why `session.turn_ended` is deduped by spending its work
evidence instead — once per observed work cycle — rather than by a timer.
The ntfy channel dedupes `needs_input` on **what is asked** instead:
(session, dialog id) or (session, question `ts`) for 5 minutes
(`_NEEDS_INPUT_MEMORY_S`), so a second, different dialog seconds after the
first still pushes and the same dialog read again does not (without an id —
the screen could not be read — the 5 s window applies).

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
- **Trusted Tailscale accounts (opt-in; Settings → Your devices → Advanced).** When
  `general.tailnet_trusted_logins` is non-empty, a request whose tailnet peer
  `tailscale whois` reports as an **untagged** node owned by a listed login
  skips the token — HTTP and websockets alike (`web/core/tailnet_trust.py`).
  Tagged nodes, nodes shared in from another account, other accounts' nodes,
  non-tailnet addresses and `X-MindFlock-Remote` relays never qualify. A
  direct tailnet peer that carries `X-Forwarded-For`, `Forwarded` or
  `X-Real-IP` is refused. Requests through `tailscale serve` (the shared phone
  link) arrive on loopback with `X-Forwarded-For`. Only the last hop is
  believed, and only on Linux, when `/proc/net/tcp{,6}` shows the client-side
  loopback socket ESTABLISHED and owned by root (tailscaled) or the server's
  own uid. Elsewhere those requests still need the token. Whois answers,
  misses included, are cached 60 s per IP. The Origin and Host checks below
  still run first. **Rotating the token does not sign these devices out.**
  Revoke by unticking the login, or by removing or tagging the device in
  Tailscale (up to 60 s to apply).
- **Several devices, one origin.** The shared phone link is one hostname
  answered by any of your devices, each with its own token. So every sign-in
  sets `mf_auth_<12 hex of sha256(token)>` beside the plain `mf_auth`, and the
  gate accepts the request when any `mf_auth*` cookie (up to 16) is this
  server's token. `?token=` may repeat (the shared QR carries one per paired
  device). The request passes if one of them is this server's, and the
  redirect then stores all of them, each held to `[A-Za-z0-9_-]{8,256}`. A
  server never accepts another device's own token.
- **Your devices' key.** Once devices are joined ("Your devices", see
  [above](#your-devices-fleet)) every member also accepts the shared fleet key
  beside its own token, in each of the forms above. A device's phone QR
  carries its own token plus the fleet key (none at all with the gate off),
  and the sign-in cookie it sets belongs to that device's origin, so a phone
  that scanned one device's QR is signed in there, not on the others' own
  URLs. Only the shared phone link, one origin answered by any of your
  devices, signs it in wherever the link lands. A caller signed in with the
  fleet key is never shown a device's own token: `GET
  /api/settings/auth-token`, `GET /api/mobile` (its QR comes back as the bare
  URL, `token: null`) and the rotate answer withhold it. `GET /api/mobile` and
  the rotate answer hand it only to a caller presenting that own token or to
  this machine itself (unrelayed).
- **One-tap dialog buttons.** `POST /api/notify/act/<token>` passes the gate
  with no credential of ours — it is fired by the ntfy app on the phone, which
  holds none. The token **is** the credential: 32 lower-case hex, minted when
  the dialog push was sent, single use, ten minutes, bound to one dialog and
  one key (`core/voice_actions.py`). POST only, exactly that path shape
  (`auth._PUBLIC_VOICE_ACT`), so nothing else under `/api/notify` opens up.
  See [Addon routes](#addon-routes).
- **Changing what runs on your devices (gate on or off).** Routes that write
  what agents run with — `POST /api/settings` (every field except
  `general.onboarded`, `general.last_repo_path`, `ui.surface` and `ui.accent`; `general.setup` is written only by the server's own hooks and `POST /api/setup/<step>/skip`), `PUT
  /api/settings/ticketing/sources`, `PUT /api/settings/auth-profiles`,
  `POST/PUT/DELETE /api/providers*`, `POST/DELETE /api/templates*`, `POST
  /api/prefs` with `keymap` or `prompt_presets` (those fields only: the rest
  of the batch is saved, and the answer names them in `refused`), `POST /api/notify/ntfy`,
  `POST /api/notify/rules/{id}`, the repo-scope `/api/red-zones` writes
  (add, delete, companions, plan-first), `POST /api/cursor/autoadopt`, the
  `/api/peer` writes (invites, join, unlink, address, perms, share, export,
  mirror)
  joint tasks)
  and `POST /api/settings/sync{,/now,/resume,/pin}`, plus `POST
  /api/settings/auth-token/rotate`, `POST /api/providers/{name}/login-close`,
  the stored-secret Test routes and a session's repo-scope red-zone writes —
  answer **403** `{error: "this changes how your devices run agents, so that
  device only takes it from you: make the change on that computer itself or
  from one of your devices, or sign in with the access token shown there…"}`
  unless the caller presents this device's token or the fleet key,
  is this machine itself (loopback, unproxied), or is a trusted Tailscale
  account — and never when another MindFlock relays it
  (`auth.may_configure`). The one exception is a server nothing beyond this
  machine can reach (`CS_WEB_MODE` local or unset, no forwarding header). Why:
  on a gate-off device reachable on the tailnet any node gets past the gate,
  and a synced field it wrote (`coding_cli.default_launch_args`, a template's
  program, a custom agent) would be stamped as this device's edit and spread
  by settings sync to every device holding the fleet key. With the gate on
  this refuses only relayed requests. None of these routes is on the
  remote-control forward allow-list.

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
- **Host check (tailscale mode).** A tailnet-exposed server answers only
  its own names: loopback, this node's tailnet addresses and MagicDNS names
  (full and short — from the shared `tailscale status` snapshot, re-read in
  the background every 30 s; a failed read keeps the last names), the
  shared link's service hostname, and — only while bound to every interface
  (`MINDFLOCK_BIND_ALL`, or the fallback when tailscaled wasn't up or its
  addresses can't be bound here) — this machine's LAN addresses and
  hostname. Anything else is a 403, so a page rebound onto the tailnet IP
  (or 127.0.0.1) gets nowhere even with the gate off. The phone over
  MagicDNS, `tailscale serve`, the shared link, and another member calling
  by IP or by name all pass.
- **"From this machine" names this machine.** The loopback shortcut
  (`privileged`, the token reveal, `may_configure`) needs the `Host` header
  to be a loopback name too (`localhost`, `127.x`, `::1`): a rebound page
  reaches 127.0.0.1 from a loopback peer with its own name as the `Host`.

| Method | Path | Behavior |
|---|---|---|
| POST | `/api/auth` | Body `{token}` — validate + set the `mf_auth` cookie (login-page target; always allowed through the gate). `200 {ok}` or `401`; never echoes the token |
| GET | `/api/settings/auth-token` | This device's own token in the clear for Settings → Security (generated and persisted on first use) → `{token, auth_enabled}` — but only to a caller that already presents that own token (cookie or bearer), to this machine itself (loopback, unproxied, not relayed), or, with the gate off, to any direct caller of a server nothing beyond this machine can reach (`CS_WEB_MODE` local or unset, no forwarding header) — never to an anonymous tailnet caller of a gate-off, reachable server, which the token would make privileged. Anyone else — a phone or browser signed in with your devices' key, a member relaying — gets `200 {token: null, auth_enabled, reason: "signed in with your devices' key"}`: the fleet key must not collect every device's own token, which would outlive a removal |
| GET | `/api/settings/tailnet-trust` | Settings → Your devices → Advanced's **Trusted Tailscale accounts** (moved there from Security): `{available, self_login, self_tagged, logins, shared_link_supported, trusted}` — `logins` are the Tailscale logins owning at least one UNTAGGED node on the tailnet (the choices); `trusted` mirrors `general.tailnet_trusted_logins`; `self_login` is `""` when this node is tagged; `available:false` = no `tailscale` binary or the daemon is stopped. Reads the shared `tailscale status --json` snapshot (re-read at most every ~5 s, `backend/tailscale_cli.py`). The list is saved through `POST /api/settings` (`{"general": {"tailnet_trusted_logins": [...]}}`), lower-cased and de-duplicated on load. What trust grants: see the Trusted Tailscale accounts bullet above |
| GET | `/api/settings/sync` | Settings → Your devices → **Settings sync**: `{enabled, error, paused, choices[], device, joined_from, in_fleet, devices: [{key, label, syncing, last_sync, withheld[], error}], pinned[], separate: {<pin>: <label>}, deferred: [{path, value, reason}], warnings[], syncable: [{path, label, group}]}` — one row per member of "Your devices" (`backend.web.core.settings_sync`). `error` is `"settings.json couldn't be read — sync paused"` while the file won't parse. `paused` is `"This device's settings look reset — sync paused"` (with `choices: ["theirs", "mine"]`) when one background scan — a change nobody made through MindFlock, such as a replaced or restored file — would clear 3 or more settings and at least half of what is set here, with 8 or more set: nothing is stamped or exported until `resume`, and `settings.sync_paused` fires once. A save through a route (`POST /api/settings`, `/api/prefs`, ticket sources, provider TOMLs) explains only what it wrote: settings cleared under the paths that save wrote (a field, a group's fields, a store or its entries) don't count, anything else cleared in the same scan still does — a reset file followed by one save still pauses. `pinned` holds `group.field`, `store:<name>` or single entries (`ticketing.sources#<id>` — a source whose id means something different on the device you joined is kept separate this way; `separate` maps each such pin to that device's label, which the Unpin confirm names). `warnings` include a member whose stamps were skipped because they can't be a real time — more than a year ahead of this clock (`"<host>'s clock is far ahead — its changes are ignored…"`; a merely fast clock is never warned about, since a later edit wins either way), sources kept separate, and ticket sources without an id (never synced). `devices[].error` reads `"<host> paused sync — its settings look reset; answer it in Settings → Your devices there"` for a member whose own sync is paused (its export answers 503). `devices[].withheld` is always empty (kept for older clients) |
| POST | `/api/settings/sync` | `{enabled: true, from?}` turns sync on — from THIS device (`from` empty: its values are stamped now and lead) or by first adopting member `from`'s values (→ adds `{adopted[], deferred[]}`); `{enabled: false}` turns it off. 409 when this device isn't in a group of devices or `from` isn't a reachable member; 403 for a relayed request |
| POST | `/api/settings/sync/now` | Run one pass now → the status plus `adopted[]` (403 relayed) |
| POST | `/api/settings/sync/resume` | `{keep: "theirs" \| "mine"}` while `paused` → the status plus `adopted[]`. `theirs` takes your other devices' values back (what's here is stamped older than any real edit, then a pass runs); `mine` treats what's here as the change and spreads it. 400 for another `keep`, 409 while settings.json can't be read, 403 relayed. Turning sync on again (joining a device, or leading from this one) or off — leaving included — also clears the pause |
| POST | `/api/settings/sync/pin` | `{path, pinned}` — keep `path` (`group.field`, `store:<name>`, or one entry such as `ticketing.sources#<id>`) different on this device, or stop (un-pinning lets the group's value win on the next pass) → the status. 400 for a path that doesn't sync, and for `github.automation_device` (which device runs PR review is one answer for the whole group); 403 relayed |
| POST | `/api/settings/sync/nudge` | A member changed something: pull in about a second → `{ok, scheduled}`. Fleet key bearer only (401 `{error, id, epoch, kfp}` otherwise) |
| GET | `/api/settings/sync/export` | What members pull: `{protocol: 2, fleet, device, enabled, stamps: {<unit>: {ts, by, h[, deleted]}}, values, withheld: []}`. Units are `group.field`, `group.field#<key>` for list entries (ticket sources by id, prompt presets by name) and `store:<name>#<key>` (templates, red zones, user provider TOMLs); pinned units are left out. Last edit wins per unit; deletes travel as tombstones. Stamps are hybrid-logical (`max(now, previous + 1 ms)`), so an edit made after seeing a value always wins over it whatever the clocks say; nothing is held back for a clock, and only a stamp that can't be a real time (not finite, negative, or more than 365 days in the puller's future) is skipped. Secrets are included, so the request itself must carry the fleet key or this device's token (401 `{error, id, epoch, kfp}` otherwise), and a puller ignores an export from another group or protocol. **503** `{error}` while settings.json can't be read or sync is paused here — never an empty export, which would read as "everything deleted" |
| GET / POST | `/api/prefs` | UI preferences that follow the person (keymap, prompt presets, theme, diff mode/base, bars, break/idle/hints, `map_tour` = the Map tour was seen, `settings_advanced` = Settings shows its advanced screens, `pane_header` = `{order, hidden}` item ids for Customize → Pane header, `null` = the default set): GET returns every field with defaults filled; POST takes a partial update (`null` clears a field, unknown fields are ignored), is stamped for sync; `keymap` / `prompt_presets` from a caller `auth.may_configure` refuses are left out of the save (not the whole batch) and named in the answer — `{…prefs, refused: [field], error}`, or **403** `{error, refused}` when nothing else was asked for — and the browser keeps them as its own edit (still unsent, never overwritten by a pull) and says so; and emits `settings.synced` with `from: ""` so other open UIs on this server re-pull. Both answer **409** `{error: "settings.json couldn't be read — fix or delete it"}` while the file won't parse (nothing is saved over it, and no defaults are served in its place); so do `POST /api/settings`, `PUT /api/settings/ticketing/sources`, `PUT /api/settings/auth-profiles`, `POST /api/notify/rules/{rule_id}`, `POST /api/notify/ntfy`, `POST /api/ide/open-on-ticket` and `POST /api/settings/auth-token/rotate` (the old token stays valid) |
| POST | `/api/settings/auth-token/rotate` | Mint + persist a NEW token (compromise recovery) — owner only (`auth.may_configure`; **403** otherwise: it signs every phone and paired device out): every issued cookie/QR/paired device is invalidated (trusted Tailscale accounts are not — untick them). In a group of your devices the devices' shared key is replaced too, and so is every reached member's own access token (`/api/fleet/rotate-token` under the new key; the shared-link QR carries the members' own tokens), so a phone signed in before is signed out of all of them. → `{token, auth_enabled, rekeyed[], missed[], rotated[], rotate_failed[], in_fleet, note[, fleet_error]}`: `rekeyed` members took the new key now, `missed` get it when they're back, `rotated` replaced their own token, `rotate_failed` didn't — `missed` ones included, since a member's own token is never replaced later: `note` says phones must scan the QR again and names those members ("Not reached: <hosts> — rotate the token on it/each too (Settings → Security there)…"). `fleet_error` when the key change failed (the token rotated anyway). `token` (and a re-issued cookie for this caller) only for a caller that presents this device's own token or is this machine itself, unrelayed; anyone else — a caller signed in with the fleet key, or any remote caller of a gate-off server — gets `token: null` and no cookie. `409` when `MINDFLOCK_AUTH_TOKEN` pins the token, or (`{error: "settings.json couldn't be read — fix or delete it"}`) while settings.json won't parse; `500` when persisting the new token fails (the old token stays valid in both cases) |

## Server lifecycle

Startup (`lifespan`): a 4 s reload loop adopts sessions created by other
processes; the Cursor auto-adopt loop starts (disable initial state with
`CS_CURSOR_AUTOADOPT=0`); the prompt-queue drain loop starts (feeds queued
prompts to idle agents); the persisted scroll speed is applied; a banner with
the local + tailnet mobile URLs (the access token + a QR code if `segno` is
installed) is printed; each addon's `on_startup` runs; the Code Map warmer
builds every open session's `GET …/code-map` snapshot in the background (45 s
after boot, past the quiet window and the revive's start; one worktree at a time, within 90 s; off with
`MINDFLOCK_CODE_MAP_WARM=0` and always off under pytest), logging `code map
warmed for N session(s)`. Shutdown reverses addon
hooks and cancels background tasks. tmux sessions are *not* touched — they
outlive the server.

**Background revive.** After the 30 s boot quiet window the server relaunches,
in the background, every session whose agent was running when it stopped — two
at a time, oldest first, through the same `_ensure_agent_session` path as
`POST /api/instances/{title}/revive` — skipping paused, provisioning,
peer-shared, workspace-missing, usage-limited and sleeping sessions (see
[session-engine.md](session-engine.md#after-a-restart-background-revive-and-nothing-dropped-silently)).
Each revive emits the usual `session.status_changed` / `session.activity_changed`
as it lands. `general.revive_on_start` (default on) turns the startup sweep off,
and the auto-revive of an agent the OOM killer took: each session row carries
`auto_revive` — `null`, `{state: "pending", reason}` (killed, coming back once
memory recovers; its Offline is not announced meanwhile) or
`{state: "gave_up", reason}` (killed too often this hour, or the relaunch
failed; left Offline).

| Method | Path | Returns / accepts |
|---|---|---|
| POST | `/api/server/revive-all` | Run the revive sweep now and wait for it → `{revived: [title], continued: [title], skipped: {title: reason}, already_running: n, summary}` (`summary` = `"revived N, skipped M: 1 paused, …; K already running; told J to continue"`; a launch that failed is listed in `skipped` with its error; `continued` = the agents that died mid-turn and were queued a *continue where you left off*). Body `{titles: [...]}` (the rail's **Restart all**) sweeps just those sessions, agents the user quit included; 400 when it isn't a list of strings. While memory is low each launch waits up to 120 s (`MEMORY_WAIT_SECONDS`) for headroom, then the rest are skipped as `low memory` — so the call can block for that long plus launch time, and a scripted caller needs a longer timeout. Runs regardless of `general.revive_on_start`. Owner only (`auth.may_configure`, **403** otherwise) — it launches agents. One sweep at a time: a second call waits for the first |
| GET | `/api/system/memory` | The machine's memory headroom (`backend.memory_pressure`) → `{level: "ok"\|"low"\|"critical"\|"unknown", available, total, available_pct, swap_total, swap_free, psi: {some_avg10, some_avg60, full_avg10, full_avg60} \| null, at, message}`. While `low`/`critical`, agent spawns are refused, team runs start no new lines and the revive sweep waits ([session-engine.md](session-engine.md#memory-guard)); `message` is the one-line reason, `""` when ok |
| GET | `/api/system/slots` | Heavy-job slots (`mindflock slot`, `backend/slot.py`) → `{slots, source: "env"\|"setting"\|"default", pools: [{name, holders: [{slot, pid, cmd, cwd, since, max, session}], waiters: [{…, position}]}], running, waiting}`. Read from the lock dir (`~/.mindflock/run/slots/`), so jobs started with no server show too; `session` is the MindFlock session the job runs in (by tmux pane, then working folder), `""` when none; `pid` is null for an instant after a slot is taken. See [cli.md](cli.md#mindflock-slot---pool-name---max-n---timeout-secs---no-wait-memory----cmd-args) |

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
| GET | `/api/update/check` | `{current, commit, latest, tag, release_url, notes, checked, available, kind, blocked, repo, state, restart_pending, last}`. `state` is the last update's (`idle·started·done·failed·rolled_back`); `restart_pending` = installed but this process doesn't run it yet (the server restarts itself onto it within seconds — the screen says "Installed — restarting…" instead of offering the install again); `last` = `{ref, version, from_version, error, code, healthy}` of the last update (`error: "interrupted"` when its installer died). `?refresh=1` bypasses the 15-minute `RELEASE_TTL_S` cache (the **Check again** button) — the releases endpoint is polled by every open settings screen and GitHub's unauthenticated limit is 60/hour. An unreachable GitHub answers an empty `latest` with `checked: false`, and is **never** reported as up-to-date: "couldn't tell" and "you're current" are different answers. `kind` is how this engine is installed (`uv-tool` \| `pipx` \| `editable` \| `other` — install.sh's `$MINDFLOCK_HOME/install.json` names the method for a release install, the running package's path decides otherwise, and a checkout always reads `editable` whatever a stale install.json says), `method_source` where that came from (`install.json` \| `detected`), and `blocked` is the human sentence for why it can't update here (empty = it can; a `pipx` install updates with `pipx install --force`, an `editable` one is refused). For an `editable` install `checkout` is the working tree and `editable_commands` the exact git lines that update it (`cd …`, `git fetch --tags origin`, `git checkout <tag>`, `uv sync --group web`, `mindflock restart`) — Settings → Advanced shows them instead of a button; both are empty otherwise |
| POST | `/api/update/start` | Body `{}` (or `{"ref": "v0.3.2"}`). **Owner only** (`privileged()`: a credential, this machine, or a trusted Tailscale account — never a request another MindFlock relays): **403** otherwise, so a gate-off device's tailnet neighbours can't reinstall it. The newest tag is resolved **server-side** by default — the button says "update to the newest version", and a stale settings screen doesn't get to decide what that is — then resolved to a commit and handed to the install command for this engine's method (`uv tool install --force` or `pipx install --force`, the same line `mindflock update` runs). An explicit `ref` from anywhere but this machine must be a **published release tag at or above the running version** (400 for a branch, a commit, an unpublished tag or a downgrade); from this machine any ref still goes, for developers. → `{ok: true, ref, commit}`. **400** is a refusal with a reason: a dev/editable checkout, an engine not installed by `uv tool` or pipx, no `uv`/`pipx` on PATH, an install already running, a ref that resolves to nothing. **502** = GitHub unreachable, so there is no newest release to install |
| GET | `/api/update/state` | The progress file plus `restarting` (and a `log` tail for the UI's detail fold). The server's own watcher (`web/core/update_watch.py`, every 5 s) re-execs onto a finished install with no client involved; this route does the same on the first poll that sees one first — once either way, and never in a process that already runs the installed build |

The operational contract behind them:

- The installer runs **detached** (its own session, `start_new_session`), so it
  survives the restart that ends the update rather than being killed halfway
  through replacing its own venv. Its PID is recorded: a `started` marker whose
  installer is gone reads as `failed` / `error: "interrupted"` at once.
- The restart needs **no browser**: a lifespan watcher re-execs once the state
  is `done` (held while Setup's install terminal runs — the hold lives in
  `finish_state`, so this route honors it too). It comes back **in the mode it
  runs in** (`keep_mode`): a server started with `mindflock serve tailscale`
  stays on the tailnet. `restart_pending` ends once that restart happened, even
  if a different engine was installed afterwards some other way (install.sh). The ingestion pipeline
  records its build in its lock file and is restarted at boot when it is a
  server-started pipeline on an older build.
- After a good install the installer watches the server's public hello on
  loopback (only when it answered just before): back on the new commit =
  `healthy`; still the old build for 90 s = not restarted yet, left alone;
  gone for 90 s from the moment it went down (the clock restarts at that edge,
  so a held restart still gets the whole window) and at least
  `HEALTH_DOWN_PROBES` consecutive misses = the new engine can't boot, so it
  reinstalls the previous commit (`prev_commit`, from `direct_url.json`),
  starts the server again with the command line and `CS_WEB_MODE` it ran with,
  and writes `rolled_back`.
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
that binds `127.0.0.1` plus this node's Tailscale addresses, so the port is on
your tailnet but not your LAN (`web/core/tailnet_bind.py`; it falls back to
`0.0.0.0` with a warning while Tailscale is down — restarting onto the narrow
bind once it is up — or when those addresses can't be bound here, and
`MINDFLOCK_BIND_ALL=1` asks for `0.0.0.0` on purpose) — every non-local bind is protected by the auth token
printed at startup (unauthenticated clients get 401). Nothing is exposed to
the public internet unless you forward the port yourself. For HTTPS run
`tailscale serve --bg 8765` once and use `./run.sh local`.
