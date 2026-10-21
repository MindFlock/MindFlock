# Session engine

Packages: `backend.session`, `backend.config`, `backend.cmd`, `backend.log`.

## Instance lifecycle (`session/instance.py`)

An `Instance` is one session: title, program, git workspace, tmux session, status.
(`session/preflight.py` checks the required binaries before a session launches.)

**Statuses** (defined in `session/storage.py`, serialized as ints, Go wire contract):
`Running(0)` — agent working · `Ready(1)` — waiting for input · `Loading(2)` —
starting up · `Paused(3)` — agent stopped; worktree and branch kept as they were.

**Creation options** (`InstanceOptions`): `title`, `path`, `program`, `branch` /
`new_branch` (explicit branch instead of `<branch_prefix><title>`), `prompt`
(seeds the agent on first launch), `launch_args` (see below), and three workspace
strategies:

| Strategy | Option | Workspace |
|---|---|---|
| Default | — | New git worktree + branch off HEAD, under `~/.mindflock/worktrees/` |
| Provisioned | `provisioned=True`, `workspace_strategy="worktree"\|"clone"` (+ optional `provision_repo=<local repo>`) | Fully provisioned workspace (below) |
| Adopt | `workspace_path=<dir>` | Adopts an already-provisioned directory (used for PR workspaces) |
| In place | `in_place=True` | Runs directly in `path` — no worktree, no branch; several sessions can share one working copy; never deleted on cleanup |

`auto_yes` is accepted but **forced off** (matching the Go engine).

**Lineage and fork point**:

- `parent` (title) and `spawned` (bool) become `Instance.Parent` /
  `Instance.Spawned`. They persist in `InstanceData` under the keys `parent`
  and `spawned`, which are written only when set, so older entries serialize
  byte-identically. `Spawned` is set once, at creation.
- `base_ref` cuts a default-strategy (new-branch) worktree from that
  commit-ish instead of HEAD.
- `base_branch`, given with `base_ref`, is recorded as `BaseBranch` instead
  of the inferred one. Without it, `BaseBranch` is `base_ref` when that is a
  local branch, else the repo's current branch.
- `carry_ref`, given with `base_ref`, is a snapshot commit of another
  worktree's uncommitted changes (`git.snapshot_changes`, below). The first
  `Start` lays it over the new worktree right after creating it and before
  the agent launches (`git.apply_snapshot`); a failure fails the start, which
  removes the worktree. Not persisted. Branch session passes it.
- Both refs are read only by the first `Start` and are not persisted. With
  `base_ref`, a branch that already exists is refused rather than reused: the
  start fails, and the branch is marked pre-existing first, so the failed
  start's cleanup never deletes it. The web server checks for the branch
  before creating anything and answers 409, so this refusal is a backstop.

**Continuing a pushed branch** needs no new field: the web server's
`continue_branch` readies the local branch (created tracking `<remote>/x`, or
fast-forwarded) and creates the session with `branch: "x"` — the
existing-branch path (`NewGitWorktreeFromBranch`), so `Branch` and
`BaseBranch` are both `x`, `isExistingBranch` is set and the branch is never
deleted on cleanup. The "shared" flag lives in web metadata
(`core/shared_branch.py`), not in `InstanceData`.

The web server owns the rules around them (spawn limits, orphaning); see
[mcp.md](mcp.md#lineage-parents-workers-limits).

**Launch args** — `launch_args` are extra CLI flags interpolated (shell-quoted)
into the launch command on **every** (re)start, after the provider's own saved
`[launch] args` (see [providers.md](providers.md)). Semantics turn on `None` vs.
list: `launch_args=None` means "not specified", so `new_instance` fills it from
the global per-provider default (`coding_cli.default_launch_args`, resolved by
the session's provider name); an explicit list/tuple — even empty — is used
verbatim, so a default toggled off for one session is honored rather than
re-applied. The resolved flags are stored on the instance as `LaunchArgs`,
**persisted in `InstanceData` (`launch_args`)**, and replayed verbatim on every
relaunch/resume — the global default is looked up once at creation, not re-read
later. The stored list is de-duplicated by **flag group** (a `-`-prefixed token
with the non-flag tokens that follow it), keeping first-seen order: a group
repeated exactly appears once, while the same flag with a different value is
kept in both places, so `--model sonnet` plus `--model opus` stays two pairs
and the CLI's last-flag-wins rule applies. (De-duplicating single tokens used
to tear such pairs apart into `--model sonnet opus`.)

`Resume()` rebuilds the launch command each time (the provider's resume
form, from the persisted `LaunchArgs`, the profile and local-model overlays,
and the MindFlock-MCP attach flags), so a resume after a server restart keeps
them too. There is one known exception: a session paused by a build **up to
0.7.5** (its worktree was removed) and resumed after a server restart takes
the legacy path, which reruns whatever command the tmux session holds — none
is persisted, so it is the bare program, without its `LaunchArgs`, profile and
local-model args or MindFlock-MCP flags. A relaunch from the web UI keeps
them. The MindFlock-MCP flags (see [mcp.md](mcp.md)) are added in front of the
launch args at each launch, and are never stored in `LaunchArgs`.

**`Start(first_time_setup)`** — builds the worktree (strategy above), resolves the
launch command through the **provider framework** (`providers.resolve(program)`),
writes a launcher script for provisioned sessions, and starts tmux in the worktree.
The tmux `launch_command` is set separately from `program` so the human-readable
program name still drives trust-prompt and idle detection. On any failure the
partial worktree/tmux is torn down before the error propagates.

**Launch environment.** The tmux session's env is the server's plus
`ExtraEnv`: an auth profile's env, split by name — credentials into a 0600
`~/.mindflock/run/<tmux name>.env` file the launch string sources, settings
on the env itself (`backend/session/secret_env.py`, so no key ever shows in
`ps`) — and, while a GitHub sign-in is connected (a Settings token or
`$GH_TOKEN`/`$GITHUB_TOKEN`; see *GitHub credential* in
[configuration.md](configuration.md)), git's per-process config
(`GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_n`/`GIT_CONFIG_VALUE_n`) that makes
`mindflock git-credential` the one credential helper for https://github.com,
so the agent's own `git push` uses the sign-in with no keychain prompt and no
global git config. `GIT_CONFIG_KEY_<n>` is explicitly **not** a secret despite
its name: count, key and value must travel together, and a key routed to the
file while the others stayed on the env would break every later pane's git
(*missing config key*). Without a sign-in nothing is added and the launch is
byte-identical. The same overlay goes on provisioning's clone and fetch
(`provisioned._noninteractive_env`, beside `GIT_TERMINAL_PROMPT=0` and SSH
`BatchMode=yes`) and is exported by the wrapper of a session's shell tab
(`agent_sessions._ensure_shell_session`), so the push the Push button types
there uses it too. The helper command embeds the interpreter that launched
the session, so after moving or reinstalling MindFlock a running session
should be relaunched before its next push.

**`Pause()`** — stops the agent (kills its tmux session and PTY) and keeps the
worktree and branch **exactly as they are**: no commit, no removal. The status
becomes `Paused` (set before the kill, so a terminal socket reconnecting the
instant its tmux dies already sees it) and the branch name is copied to the
clipboard. (Until 0.7.5 a pause committed the dirty tree as `[mindflock]
update from '<title>' on <date> (paused)` under the user's identity and
removed the checkout.)

A pause **sticks**: nothing but `Resume()` starts the agent again. The web
server's relaunch-on-touch helper (`agent_sessions._ensure_agent_session`,
which every lazy relaunch goes through — the agent terminal socket, `/send`,
Send now, the queue drain, a wake, a revive) refuses a paused session whose
agent is not running with `session is paused — resume it first` instead of
launching it; `sleep.ensure_awake` never wakes one; `/send` and Send now
answer **409** `{paused: true}`; the agent terminal socket closes with
**4423** and the pane shows a Paused placeholder with ▶ Resume rather than
reconnecting. The **shell** tab keeps working: Pause stops only the agent.
(Until the worktree was kept, the socket's "workspace no longer exists" masked
this; with it kept, an open pane relaunched a just-paused agent within a
second while the row still read Paused.)

**`Resume()`** — relaunches the agent in the same worktree: reattaches the tmux
session if one is still alive, else starts a new one; a failed launch leaves
the worktree alone. **Legacy:** a session paused under the old scheme is
recognised by its missing worktree (the `(paused)` commit on the branch is the
other fingerprint, but a clean tree left none) and takes the old path — refuse
if the branch is checked out elsewhere, re-run `Setup()` (provisioned
workspaces are re-provisioned), then reattach or restart tmux, cleaning the
rebuilt worktree up again if the launch fails.

**`Kill()`** — closes tmux, then removes the worktree and (unless the branch
pre-existed) deletes the branch.

**Persistence** — instances serialize into `~/.mindflock/state.json` (embedded
`instances` array). `from_instance_data(..., attach=False)` lets the web server
surface sessions created by another process (e.g. the pipeline) without opening a
server-side PTY. `Storage.DeleteInstance`/`UpdateInstance` splice the one
matching entry in the raw persisted JSON array (under the state lock; the other
entries pass through byte-untouched, same byte format as `SaveInstances`)
instead of reconstructing every stored instance — the old path re-attached
every stored tmux session as a side effect of changing one field.

## Git worktrees (`session/git/`)

- `GitWorktree` composes ops/git/diff/branch mixins. Worktree dirs live at
  `~/.mindflock/worktrees/<sanitized-branch>_<hex-nanotime>`.
- Branch names are sanitized (lowercase, spaces→dash, `[a-z0-9-_/.]` only).
  Default branch = `<branch_prefix><title>`; `branch_prefix` defaults to your
  lowercased username + `/`.
- **Diff** — `git add -N .` (so untracked files count) then
  `git --no-pager diff <baseCommitSHA>`; numstat variant for cheap +/- counts.
  Errors are returned in `DiffStats.error`, not raised.
- **Push** — `PushChanges` commits dirty changes (`--no-verify`) and pushes with
  a bare `git push -u origin <branch>` (cwd = the worktree, no `-C`). No `gh`,
  ever: the push goes over whatever remote the repo already has — SSH or
  HTTPS — used verbatim, with your own git credentials. MindFlock never rewrites
  a remote URL, so `url.<base>.insteadOf` and friends still apply as you
  configured them. (The Go original pushed through `gh repo sync`, which made
  the GitHub CLI mandatory; that is the one deliberate divergence from Go's
  argv. The web UI's guided flow uses its own commit/push endpoints instead —
  see [web-api.md](web-api.md).)
- **Remote URLs** (`session/git/remote_url.py`) — one transport-independent
  parser for every spelling git accepts: `https://`, `ssh://`,
  `ssh://host:22/…`, scp-style `git@host:owner/repo(.git)` and `git://`.
  `parse_remote()` yields a `RemoteRef(host, owner, repo)` with `.slug`
  (`owner/repo`, the form the GitHub API and `gh -R` take) and `.web_url`;
  `same_repo(a, b)` compares two remotes regardless of transport, so an SSH
  remote and an HTTPS config URL for the same repo match. `is_local_path()`
  recognises the path-style remotes provisioning itself creates (a base clone
  is cloned from your own checkout), and `branch_url` / `compare_url` /
  `pr_list_url` build the browser fallbacks the PR flow uses when there is no
  `gh` and no token. Parsing is read-only: nothing here writes a remote back.
- Existing branches (`isExistingBranch`) are never deleted on cleanup.
- **Carrying uncommitted changes** (`session/git/carry.py`, no Go original).
  `snapshot_changes(worktree)` records the working tree — tracked edits,
  deletions and untracked files `.gitignore` doesn't exclude — as one dangling
  commit on top of `HEAD` ("" when nothing differs). It stages into a *copy*
  of the index (`GIT_INDEX_FILE` in a temp dir, seeded from the real index so
  only changed files are re-hashed), so the source's index, files and branch
  are never written; the commit uses a fixed MindFlock identity and
  `--no-gpg-sign` (nobody pushes it; gc drops it). `apply_snapshot(worktree,
  sha)` runs `git restore --source=<sha> --worktree -- :/`: files match the
  snapshot, the index stays at `HEAD`, so everything arrives unstaged (a file
  staged in the source is an untracked file here).

## tmux + PTY (`session/tmux/`)

- Session name: `mindflock_` + title with whitespace stripped and `.`→`_`
  (`to_mindflock_tmux_name`). Killing all engine sessions matches the `mindflock_`
  prefix.
- `start()` runs `tmux new-session -d -s <name> -c <workdir> <launch>`, waits for
  the session with exponential backoff, then sets `history-limit 10000` and
  `mouse on`.
- Terminal I/O goes through a real PTY (`ptyprocess`); resizes are `TIOCSWINSZ`
  ioctls. Capture (`capture-pane -p -e -J`) powers previews, trust-prompt
  detection, and activity classification.
- Trust prompts, idle prompts, and "waiting for user" patterns are supplied by the
  session's provider, not hardcoded.
- CLI attach (`Attach()`): Ctrl-Q detaches; a resize monitor uses `SIGWINCH` on
  Unix and 250 ms polling on Windows (WSL follows the Unix path).

### Layered activity classification

`working` / `clarify` / `idle` / `limit` / `offline` is decided by
`web/core/agent_state._agent_activity`, which consults the cheapest
*authoritative* signal first and only falls back to looking at pixels:

1. **Exit marker** — the agent command ended in this tmux session → `idle`.
2. **The CLI's own report** — the hook marker (or Claude's live `claude agents
   --json`), returned as-is; it outvotes what the pane's foreground command
   looks like. It is gated on age *and* on belonging to the current tmux
   incarnation, and an `idle` marker is re-checked against the pane for a
   usage-limit banner, since the hook fires identically whether the turn ended
   or was cut off (see
   [providers.md](providers.md#activity-signal-activity_markerspy)).
3. **Process tree** — a bare shell holds the pane and no non-shell descendant is
   alive → the agent isn't running → `idle`.
4. **Pane inspection** — a normalized hash of the captured pane, the pane
   process's CPU jiffies, the provider's interrupt hint, and the turn-token
   counter, combined with asymmetric hysteresis so one changed frame can't flip
   the badge every poll.

**Never guess on a first sighting.** The pane layer used to seed a
never-before-seen pane as `working` ("a fresh agent usually is"). Two of its
four signals need two samples — a CPU *rate* and a *climbing* token counter —
so on frame one only two facts are available, and both are single-frame: a
usage-limit banner, and the provider's `working_patterns` interrupt hint, which
means a turn is live right now. A pane showing neither is reported `idle`, with
no idle-settle clock started, because nothing on that screen says otherwise.
`clarify` is deliberately not attempted there either: the waiting-prompt match
requires a *stable* pane, and one frame cannot establish stability — the next
poll catches it 4 s later.

**The per-title rolling record has a lifecycle, and it is short.** It holds the
pane hash, the CPU baseline, the hysteresis clocks and the layer-wide
*provenance* (`reported` / `state_since` / `worked_at` / `reading` — the
`(value, source)` pair as ONE atomic store, since it is read as one fact — /
`worked_evidence`, written on every return path, so a session reporting through
its CLI's hooks leaves a trail too), plus what the pane layer used to *prove*
its current busy run (`hard_since` / `proof`, cleared by every non-working
reading so a proven-busy run can never straddle an agent death or a relaunch).
`worked_at` is stamped only by a reading that CORROBORATES work — the CLI's own
report, or its live-turn status line; a busy process tree on its own moves the
chip and arms nothing (see `_verdict`'s `arms`, and the ladder in
[web-api.md](web-api.md)). `source` names the layer that produced the current
reading ("marker" / "exit" / "proc" / "trust" / "pane");
`reading_is_authoritative` is how the settle skip and the queue drain's fast
tier ask about it. Arming decides only that a turn-end *may* be announced;
whether it actually fires also passes the web layer's exact gates — recent
human input (either terminal socket, `/send`, send-now, raw tmux client
activity), the queue's send-grace, and fast-track's own record — see
[web-api.md](web-api.md). It is reset:

- when the **tmux incarnation** changes — the record is keyed to tmux's own
  `session_created`, so a relaunched session starts clean (its `_LIMIT_PROBE`
  verdict goes with it) while one whose tmux merely hiccuped keeps its history;
- on the **delete / close / cleanup routes** (`core.session_end.end_session` →
  `server._forget_session_state`),
  since titles come straight back and a namesake must not inherit a dead
  session's state;
- by a **per-tick sweep** (`server._prune_session_state`) for the removals that
  never reach a route — a workspace deleted from Settings, a tombstone converged
  from another MindFlock.

It is explicitly **not** reset on an offline poll (dropping it there guaranteed
the first-sighting branch the moment the session came back — the phantom
"running" flash after clicking a closed session), and **not** by the git workflow
verbs. Commit / push / make-PR / merge call `server._forget_probes`, which drops
only memoized probe *results* so nobody is served a stale stage; forgetting the
rolling record there would throw away `worked_at`, and with it the evidence
behind this session's next "the agent has finished" — pressing **Commit** must
not erase the fact that the agent worked.

## Provisioned mode (`session/provisioned.py`)

Opt-in provisioning that turns a session into a **fully-loaded workspace**
(setup commands + warm caches). Enabled per session (`provisioned=True`) or via
the pipeline. Works against **either** target:

- the configured `[repository].url` from `config.toml`
  (see [configuration.md](configuration.md)) — `load_provision_settings()`;
- **any local git repo** — `provision_repo=<path>` / `local_settings_for()`:
  setup commands are auto-detected from the workspace contents and no shared
  cache seeds are applied (they belong to the configured repo).

**Branch naming** — with a story id: `feature/sc-<id>/<slug>` (slug ≤ 40 chars);
without: `mindflock/<slug-of-title>`.

**Workspace strategies**

- `worktree` (default) — a git worktree off a canonical **per-repo** clone at
  `<workspace_dir>/_base_<repo-slug>`.
  The base is created once with a fast blobless single-branch clone and
  refreshed (fetch + hard reset) on later use, tolerating offline. Disk-cheap,
  fast, and native to pause/resume. When the clone source is your own checkout
  (the universal flow, or a ticket routed to a local repo), `origin` is
  re-pointed at that checkout's own origin (the forge) and the checkout is kept
  as the `mindflock-source` remote, so pushes and PRs go to GitHub. The base
  then fast-forwards to the forge's `<base_branch>` whenever your checkout's
  copy is merely behind it; a checkout with commits of its own keeps them.
- `clone` — a full standalone `git clone --depth=1` per session. Strongest
  isolation; the clone survives pause.

**Provisioning sequence** (idempotent, mirrors the pipeline's provisioner):

1. Run the `[workspace].setup_commands` (auto-detected when unset: `uv sync
   --all-groups` + `uv run pre-commit install` for a uv/Python project)
2. Pin each cache's `env` (e.g. `TESTMON_ENV=shared`) into the pre-commit hook
3. Seed each `[[workspace.cache]]` warm artifact — e.g. `.testmondata` from the
   testmon cache — only where the workspace doesn't already have one
4. Install the pre-commit log wrapper if the target repo ships one
5. Add scratch artifacts (`.mindflock_*`, `.testmondata`)
   to `.git/info/exclude` (`workspace_setup.exclude_artifacts`, also used by the
   web commit endpoint for plain sessions)
6. Optionally open the workspace in Cursor (`[mindflock].open_cursor`)

Cache env pinning is the linchpin for testmon: it fingerprints by interpreter
path, which differs per-workspace `.venv`; pinning a constant key keeps the
shared seed valid everywhere, so `pytest --testmon` only runs diff-impacted
tests.

**Launcher** — provisioned sessions run a generated `.mindflock_launch.sh`. It is
**provider-neutral**: every CLI-specific spelling comes from the launching
provider's `LauncherSpec` (see
[providers.md](providers.md#the-launcher-vocabulary-launcherspec-launch_scriptpy)),
so an ingested ticket runs codex/aider/goose correctly instead of being handed
Claude's flags. For `claude` the spec reproduces the original spellings exactly,
so the generated bytes are unchanged (pinned by
`tests/unit/test_launch_parity.py`'s goldens, one per provider shape).

- exports each cache's env var (`export TESTMON_ENV=shared` etc.; generic —
  one export per `[[workspace.cache]].env` entry), plus any `[local_model]` env
- first launch seeds the agent with the prompt at `.mindflock_prompt.md` — as a
  launch argument via the provider's `prompt_arg` (`claude "$(cat …)"`, `codex
  "$(cat …)"`, `agy --prompt-interactive …`), or, for a CLI that takes no prompt
  argument, by pasting it into the pane once its TUI has drawn. A
  `.mindflock_started` marker makes later launches **resume** instead (the seed is
  never re-sent — re-seeding restarts the whole ticket in a fresh thread)
- resumes with the CLI's own flag: `--continue`, `resume --last`, `-r`,
  `--restore-chat-history`; a CLI with none relaunches fresh
- adds the CLI's own skip-all-prompts flag when `[mindflock].skip_permissions`
  (default on — every worktree is a new path and would otherwise trust-prompt each
  time). Claude's `--dangerously-skip-permissions`, codex's
  `--dangerously-bypass-approvals-and-sandbox`, aider's `--yes-always`; goose and
  cline have none, so nothing is appended
- the provider's natural exit codes (0/130 for most CLIs) drop to a shell; any
  other exit auto-resumes after 3 s (crash/reboot recovery)

## Command executor (`cmd/`) and logging (`log/`)

- `cmd.Executor` mirrors Go's `exec`: `run(cmd)` returns `None` on success or an
  exception; `output(cmd)` returns `(bytes, err)`. `MockCmdExec` injects fakes in
  tests.
- `log.Initialize()` opens `{tempdir}/mindflock.log` with Go-style
  `INFO:2026/07/02 12:00:00 file.py:10: msg` lines; `log.NewEvery(seconds)`
  throttles repeated errors (used by the resize monitor).

## After a restart: background revive, and nothing dropped silently

Agents revive **lazily** — `_ensure_agent_session` (`web/core/agent_sessions.py`)
relaunches a dead agent tmux when a terminal attaches, a prompt is sent, or the
rail's Offline → **Restart** chip calls `POST /api/instances/{title}/revive`.
On its own that left every session not on screen Offline after a reboot, so the
server also runs a **background revive sweep** (`web/core/revive.py`):

- **When.** Once per boot, registered by the lifespan, *after* the boot quiet
  window (`_BOOT_QUIET_SECONDS`, 30 s) so the notification settle logic sees the
  revives as real transitions instead of being flooded during startup. Gated by
  `general.revive_on_start` (unset = on); `POST /api/server/revive-all` runs the
  same sweep on demand. Never registered under pytest — `ENGINE` is the
  developer's real state there, and the sweep launches agents.
- **Who.** Every session whose persisted status says its agent was running
  (`Running` / `Ready`). Skipped, each with a reason in the summary: `paused`,
  `still provisioning` (`Loading`), `peer-shared` (relaunched by the peer
  service), `workspace missing`, and `usage limit` (a hold the prompt queue
  armed, or the provider's own live meter reading a spent window — the queue
  resumes those when the window reopens), and `sleeping` (put to sleep on
  purpose, see [Sleeping agents](#sleeping-agents): a reboot must not wake
  every sleeper at once). A session whose agent tmux is still alive (a server
  restart that left tmux up) is counted as *already running*.
- **How.** Through `_ensure_agent_session` itself — the same path as the
  Restart chip, so there is one launch rule (resume after a crash, fresh after a
  clean quit). Oldest-first by last activity, at most **two** launches in
  flight, with a 1.5 s gap per slot; the skip rules are re-checked right before
  each launch. Each landed revive republishes the session's row, which emits
  `session.status_changed` / `session.activity_changed`, so the rail turns from
  Offline to Working/Idle by itself.
- **Memory.** Relaunching every agent straight after the OOM killer took them
  down is how it happens again, so before each launch the sweep reads
  `backend.memory_pressure` (see [Memory guard](#memory-guard)). While it reads
  low it waits up to 120 s for headroom; if none comes, the rest of the sweep is
  skipped as `low memory` and those rows stay Offline for a later Restart.
- **Continue.** A relaunch resumes the conversation, not the turn the agent was
  in: it comes back parked at its prompt. When the agent **died mid-turn** —
  not asleep, no live tmux, an unnatural exit (the same provider rule that
  makes the relaunch resume), a CLI that can resume (`resume_flag`), not in
  place, and
  its hook marker last said `working` (a turn began and no `Stop` /
  `SessionEnd` followed; a SIGKILL fires neither; over 6 h old reads as no
  signal) — `revive.relaunch` puts a short *continue where you left off* at the
  **front** of its prompt queue. The queue sends it once the CLI has drawn its
  prompt and settled idle, never over a dialog or a person typing; it is an
  ordinary item, visible and removable before it goes. A queue the user paused
  with prompts in it is left alone (enqueueing would switch it back on). The
  Restart chip and the sweep share `relaunch`; the lazy revive (terminal
  attach, a sent prompt) does not continue on its own.
- **Logs.** One line per revive (`revive <title>: agent relaunched (1.2s)`,
  `… — died mid-turn, queued a continue`, or `not started (<reason>)`), one
  summary per sweep (`revive sweep (startup): revived 3, skipped 2: 1 paused,
  1 usage limit; 4 already running; told 1 to continue`).

### Sleeping agents

An open agent window holds its CLI and one copy of every MCP server the user
configured — measured at ~250 MB of Claude plus ~500 MB of MCP servers per
window — for as long as it is open, idle for hours included.
`web/core/sleep.py` gives that memory back and brings the agent back when
someone needs it.

- **Sleep.** The session's *agent* tmux session is killed (the CLI, its MCP
  servers, everything in that pane; the shell pane is left alone), and
  whatever of that process tree outlives the hangup gets SIGTERM after 3 s,
  then SIGKILL (a pid is signalled only while its start time still matches).
  The row, worktree, branch and conversation stay. A sleep record —
  `~/.mindflock/sleeping.json`, `title -> {since, reason: idle|memory|manual,
  freed_mb}` (`freed_mb` = the tree's PSS just before the kill) — is written
  *before* the kill, so no reader ever sees the dead agent unrecorded, and the
  exit code the hangup left is cleared. Killing the whole tmux session (not
  just the pane's process) makes the wake the ordinary path: a dead agent
  session is what `_ensure_agent_session` already brings back.
- **What the record changes.** `_ensure_agent_session` relaunches a sleeper
  **resuming** (claude: `--resume <this window's id>` from thread markers —
  never "the newest transcript in the folder", which a sibling window on the
  same folder could own; a provisioned launcher's `--continue` in its own
  worktree), whatever exit code is on disk, and its launch ends the sleep. The
  revive sweep skips it (`sleeping`), `died_mid_turn` is False (no
  *continue* prompt: it was not mid-turn), and the state emitter records its
  `offline` reading as `sleeping` and announces neither the sleep nor the
  wake back to an idle prompt (no "agent offline" bell row, toast or push);
  any other move out of it (a dialog after the wake, a turn starting) is
  announced with `old: "sleeping"`. The row's `sleeping` field carries the
  record; its `activity` stays `offline`.
- **When.** A background sweep every 60 s (after the boot quiet window; off
  under pytest and with `MINDFLOCK_SLEEP=0`) sleeps up to three sessions per
  pass, longest idle first. All of these must hold (each refusal has a name,
  `sleep.V_*`):
  - the CLI can resume (`launcher_spec().resume_flag`) and the session owns
    its workspace (not in place, not peer-shared); started, not paused or
    provisioning; its workspace exists; and when another window works in
    the same folder, the wake can name *this* window's conversation (a
    recorded thread id, and no provisioned launcher, whose resume is always
    `--continue`) — otherwise it would resume the folder's newest
    conversation, which may be the sibling's;
  - its turn has **ended**: the row reads `idle`, the CLI's own hook marker
    is not `working`/`clarify`, an uncached re-probe still reads `idle`, an
    agent (not a bare shell) holds the pane and no dialog is on screen;
  - no background job (the row's `background`, re-read live) — a job the
    agent left running past its turn keeps it awake until the job ends,
    however long that is, rather than killing the job;
  - nothing waiting for it: an empty prompt queue (any items, even in a
    paused queue), no pending mailbox message, setup not running;
  - nothing depending on it: not a team-run member or lead, no fast-track
    chain mid-flight, no child of it busy (working, on a prompt or a limit);
  - nobody looking: no terminal websocket attached (`ownership.terminals`)
    and no tmux client attached from outside this server (`tmux
    list-clients`, minus the server's own children — its web terminals,
    counted above, and the PTY the engine keeps attached to every session it
    started — so `mindflock attach`, a raw `tmux attach`, SSH all count);
  - quiet for the threshold, by every clock: since the activity reading last
    changed, the last human keystroke (web or raw tmux client), the last
    queue or mailbox send, and the last wake.

  The threshold is `general.sleep_idle_minutes` (unset = 45; `0` turns
  sleeping off entirely). **Under memory pressure** — `memory_pressure` low
  or critical — it drops to 5 minutes (never above the setting), and those
  sleeps record `reason: memory`; longest-idle first is what frees the most
  stale memory soonest. `POST /api/instances/{title}/sleep` and `mindflock
  sleep` sleep one now: every rule above except the timing ones.
- **Wake.** `sleep.ensure_awake(inst, title, why)` is the one helper: it
  relaunches through `_ensure_agent_session` and opens the prompt queue's
  20 s post-boot grace, so nothing is typed into a CLI that has not drawn its
  prompt yet. Every path that types into a session goes through it when the
  session is asleep: the terminal websocket (opening the session — web,
  phone, desktop), `/send` and MindFlock's own composed messages
  (`_deliver_to_agent`: the text goes on the queue — at the front for a
  person's send — instead of being typed; an unsubmitted paste, or a looping
  queue, just wakes it and answers 409 `waking`), the queue's Send now (the
  item moves to the front), the queue drain (a queued prompt reads `offline`
  and is relaunched), the mailbox lane and `delivery: "now"` (the one boot
  the lane does: an MCP `send_message` to a sleeper wakes it, and the lane
  types it once the CLI is idle), `POST /api/instances/{title}/wake` (the
  rail's **Wake**) and `mindflock wake` / `mindflock attach`. Each wake logs
  `wake <title>: <why> (asleep 52m)`; each sleep `sleep <title>: idle after
  47m idle — freed ~780 MB`.
- **Keys.** Everything is keyed by session title, so duplicate windows on one
  folder and branch (`foo`, `foo-copy`) sleep and wake independently, each
  back into its own conversation. Remote `device::title` rows are never swept
  from here; opening one wakes it on its own device.

### Memory guard

A big job — a team run, an orchestrator fanning out workers, a ticket queue
draining — can start agents faster than the machine holds them: each is a node
CLI plus whatever it runs, and once RAM runs out the box swaps (on WSL a
disk-backed swap that stalls everything) and the OOM killer takes agents down
mid-turn. `backend/memory_pressure.py` (stdlib only; the pipeline process
imports it too) reads two signals and returns `ok` / `low` / `critical`
(`unknown` where there is nothing to read — non-Linux — which never gates):

| Signal | low at | critical at |
|---|---|---|
| `MemAvailable` (`/proc/meminfo`) | ≤ 15 % of `MemTotal` or ≤ 2 GB | ≤ 7 % or ≤ 1 GB |
| PSI `some avg60` (`/proc/pressure/memory`) | ≥ 20 % | ≥ 40 % |
| PSI `full avg60` | ≥ 5 % | ≥ 15 % |

Stepping back down needs every reading past a recovery line a margin beyond the
entry one (20 % / 2.5 GB / some 10 / full 2 to leave `low`; 10 % / 1.5 GB /
some 30 / full 10 to leave `critical`), so a box hovering at the line doesn't
flap. A reading is cached for 2 s, and the level is per process: the ticket
pipeline keeps its own, so it and the server can briefly disagree near a line.
While the level is `low` or `critical`:

- **Agent spawns are refused** — `POST /api/instances` with `spawned: true`
  (the MCP's `spawn_session`) and an agent's `POST /api/tickets/start` answer
  **409** with `memory` in the body. A person's New and Intake starts, and a
  team run's own members, are never refused here.
- **Team runs start no new lines** — the planner holds queued tasks exactly
  as it does for a usage limit (a task marked `start_now` still starts); the
  run's `waiting_for_memory` puts a *memory* badge on its rail header.
- **The ticket pipeline holds its next launch** (`session_runner`), logging why
  every 5 minutes; after 30 minutes it starts anyway with a warning, so a
  misreading can never stall intake for good.
- **The revive sweep waits**, as above. A person's single Restart is never
  held; its response carries `memory_warning` instead.
- **Idle agents sleep sooner** — after 5 minutes idle instead of
  `general.sleep_idle_minutes` ([Sleeping agents](#sleeping-agents)), longest
  idle first, giving back their CLI's and MCP servers' memory.

The server's memory watch polls every 10 s, logs each level change and emits
`system.memory_changed`. It starts from `ok`, so a server that boots already
low emits `ok → low` on its first reading; `unknown` never emits. The web UI
polls `GET /api/system/memory` and shows a notice while the level is low.
`MINDFLOCK_MEMORY_GUARD=0` forces `unknown`, which turns it all off.

**The guard gates agents, not their work.** The OOM it was built for turned out
(measured 2026-10) to come mostly from inside sessions that were already
running: pre-commit hooks running `pytest -n auto` suites (~1.2-1.5 GB per
xdist worker), a documentarian starting more agent CLIs, an indexer — ~8 GB per
commit, with several agents committing at once. Nothing above holds those.
[`mindflock slot`](cli.md#mindflock-slot---pool-name---max-n---timeout-secs---no-wait-memory----cmd-args)
does: a repo prefixes its heavy hooks with it, and those commands take turns
for N machine-wide slots (`general.heavy_slots`; 1 below ~32 GB of RAM) and,
once holding one, wait (capped at 10 min) for this same reading to leave
`low`. It is a flock-based semaphore in `~/.mindflock/run/slots/` that needs no
server; `GET /api/system/slots` shows who holds and who waits, and the memory
notice adds a line for them while it is up.
#### When the OOM killer fires anyway: agents go first, and come back

**Agents are the preferred victims.** On 2026-10-14 the kernel killed the
MindFlock *server* (2.1 GB, the biggest single process, `oom_score_adj` 0)
rather than the 1.3 GB pytest workers agents had started — and every agent
went down with it. Every agent launch now raises its own `oom_score_adj` to
`launch_script.OOM_SCORE_ADJ` (500) before it starts the CLI, with one
statement (`launch_script.oom_score_line()`:
`{ echo 500 >/proc/self/oom_score_adj; } 2>/dev/null || :` — silent where
`/proc` is missing, i.e. macOS). The score is inherited across fork and exec,
so the CLI, its MCP servers and everything the agent runs carry it. The kernel
picks the process with the highest *badness* — its memory plus `adj/1000` of
RAM+swap — so at 500 any agent-tree process outranks the server, and the
largest of them (the runaway test run, usually) goes first; often the agent
itself survives and just sees its tool call fail. Raising needs no privilege.
Every launch path carries it: the exit-marker wrapper (`_wrap_launch_cmd`,
every web relaunch and the Assistant), the engine's first start
(`TmuxSession.start`), the provisioned launcher (before its `exec bash -ilc`),
the ticket pipeline's standalone launch (`launch_script.launch_command`, used
by `AgentCliRunner` and `PRClaudeRunner`), a session's shell tab, and the
usage-refresh windows. `MINDFLOCK_OOM_SCORE_ADJ` overrides the value (`0`
turns it off; the launch strings are then byte-identical to before). The tmux
server keeps its own score. The server does **not** lower its own: going below
0 needs `CAP_SYS_RESOURCE`, which a normal install never has, and if it did
run privileged the lowered score would be inherited by everything it starts
that doesn't raise its own (the tmux server, git, the pipeline child) —
raising the agents already puts the server behind them.

**A SIGKILL is not a failed resume.** A resume chain's fallbacks (`claude
--continue || { sleep 3; retry } || fresh`, a generic CLI's `resume || fresh`,
which re-seeds the task for a CLI that takes its prompt as an argument) used to
fire on *any* non-zero exit — including an OOM kill an hour into the run, which
relaunched the CLI three seconds later straight back into the squeeze, and on a
second kill started a fresh conversation. Each fallback is now wrapped in
`launch_script.unless_killed`, which passes exit 137 through (`(exit 137)`,
so the wrapper still records it) and runs the fallback on any other failure.
The provisioned launcher ends with 137 straight after the launch instead of
falling to its in-session loop, so the pane closes and the server takes over.
(That loop's `cs_code=$?` reads the status of `while true`, i.e. 0, so as
written it always breaks to the shell — pinned as-is by the goldens.)

**Auto-revive.** The other half (`revive.observe` / `revive.auto_tick`, driven
by the 4 s instances tick — no loop of its own): an agent this server saw live
that then reads Offline, and died to SIGKILL — exit marker 137, or no marker at
all (the engine's first start runs without the wrapper) while its hooks last
said `working` (a SIGKILL fires no Stop/SessionEnd hook; a quit does) — is
queued, and relaunched through `revive.relaunch` (so a mid-turn death gets its
*continue*) once memory reads `ok`:

- one at a time, `GAP_SECONDS` apart, never while a sweep runs, on its own
  thread (never blocking the tick); while memory is low it waits without
  spending the agent's budget, logging the hold once;
- at most `AUTO_MAX_PER_WINDOW` (3) per session per `AUTO_WINDOW_SECONDS`
  (1 h) — an agent whose own work OOMs the box every time is not relaunched
  into it forever; after that it stays Offline and its row's `auto_revive`
  says why (`{state: "gave_up", reason}`, shown as the Offline chip's hint);
- the sweep's skip rules are re-checked at launch (paused, provisioning, peer,
  workspace missing, quit by user, usage limit); a session already Offline when
  the server booted is the startup sweep's, never this;
- switched by `general.revive_on_start` (the same "agents come back on their
  own" switch) and `MINDFLOCK_REVIVE_ON_START=0`; off under pytest;
- quiet: while an agent is queued (and for `AUTO_SETTLE_SECONDS` after its
  relaunch) `_emit_state_changes` keeps announcing its previous activity, so
  the trip through Offline rings no bell, toast, ntfy push or hook, and the
  row (`auto_revive: {state: "pending"}`) keeps it out of the bell. One given
  up on is announced like any Offline;
- one log line per auto-revive (`auto-revive <title>: agent relaunched after
  it was killed (exit 137; 1/3 this hour, 1.2s) — queued a continue`).

Not covered: a CLI with no activity hooks that dies on its first start (no
marker, no `working`) stays Offline as before, and an agent killed while idle
on its first start likewise; the queue driver's own reboot of an Offline
session with queued prompts is not memory-gated.

**Nothing disappears silently.** The engine still drops, at load, a session
whose worktree folder **and** tmux session are both gone (outside the 120 s
grace window; paused sessions are exempt) — but it now records each one in
Recently closed first (`recently_closed.record_missing_workspace`, called from
`Engine.__init__`) with `reason: "workspace missing"`, `wiped: true` (nothing
left to reopen) and `branch_exists` — one `git show-ref --verify` against the
recorded repo, so the entry can say whether the branch survives and the work
can be recovered by hand — plus a log line. The adopt loop's and merge-on-save's
own dead-instance culls (entries written by *another* process) do not record:
they run every few seconds and were never this server's sessions.

## Known quirks

- `config.toml`'s engine section must be `[mindflock]`; sections under older
  project names are silently ignored (see
  [configuration.md](configuration.md)).
- `InstancesFileName = "instances.json"` is declared for Go parity but unused —
  instances live inside `state.json`.
- `provisioned.write_launcher` is called directly by `Instance._configure_launch_command`
  (it resolves the launching provider's spec itself); the resolved provider is still
  asked to `install_activity_hooks` first, which is where Claude also pre-trusts the
  worktree. `ClaudeProvider.write_launcher` remains as the provider-framework seam.
