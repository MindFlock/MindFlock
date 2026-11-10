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

**`MoveWorkspace(dest)`** — moves the session's workspace folder to `dest`
and carries on there (no Go original; the web side is `POST
/api/instances/{title}/move`). In order: `CheckMoveDest` (absolute — a leading
`~` is expanded —, not inside the current workspace, nothing there yet, parent
folder exists, not in the peer-link folder; a refusal is a `MoveError` whose
`code` the route answers with) and `_git_layout` (a folder that is a
sub-folder of its repo is refused: moving it would carry tracked files out of
their repo); stop the agent the way `Pause()` does (`Paused` first, then the
tmux session); move the folder — a **linked worktree** with `git worktree move`
then `git worktree repair` from the main repo (across filesystems, where git's
rename fails: a plain move then `git worktree repair <new>`), a **main clone**
or a **plain folder** with `shutil.move`, then for a repo `git worktree repair`
in the new place, naming any worktrees that lived inside it, so every linked
worktree finds it again; re-point every path the instance keeps (`Path`, the
worktree's `repoPath` / `worktreePath`); call the provider's
`carry_workdir(old, new)`; relaunch through Resume's path
(`_relaunch_in_worktree`: the provider's resume command) when the agent was
running — a paused session stays paused. After any failure the paths name
wherever the folder really is (a move that failed leaves them, and the agent
relaunched, in the old place); an agent that could not be relaunched leaves
the session `Paused`. The module-level **`MoveWorkspace(instances, title,
dest, save)`** is the engine's entry (Go style: it returns the error):
`unknown_session` for an unknown title, the move, then the same re-pointing for
every OTHER instance that kept the old folder (window copies sharing it in
place, worktrees cut from a main clone that moved), then `save()` — also after
a failure, so state.json names where the folder is.

**`carry_workdir(old, new)`** (provider hook, `providers/base.py`, default
no-op) — the CLI's per-directory state, carried after a move. **Claude** copies
`<config root>/projects/<slug of old>/` to `<slug of new>/` in every config
root that has one (`~/.claude*`, `$CLAUDE_CONFIG_DIR`, account-profile dirs;
the slug replaces every non-alphanumeric character with `-`) — a copy, so the
old folder's history survives a move undone by hand — and `--resume <id>` in
the new folder finds the thread; trust and hooks are re-pinned by the relaunch
itself. **Codex** carries nothing: its rollouts live under
`~/.codex/sessions/<date>/` keyed by session id (the folder is only a `cwd`
field inside) and the thread marker is keyed by the window, so `codex resume
<id>` still finds it. The generic (TOML) providers inherit the no-op.

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
- **One name.** A session has exactly one name, its title: what the rail
  shows is what tmux, the MCP identity, messages, flights and the CLI use.
  Renaming (`POST /api/instances/{title}/rename`, `core.rename`) renames the
  title everywhere at once — the engine key and state.json (old title
  tombstoned), `tmux rename-session` for the agent and its `_sh` shell (the
  agent keeps running; its hooks resolve the tmux name at fire time), the
  files named after the tmux name, and every store in the
  `core.rename_stores` registry (`tests/unit/test_rename.py` fails when a new
  JSON store is neither registered nor listed as not session-keyed). The
  worktree folder and branch keep their names. A running agent still carries
  the old title in its MCP config and exit-marker wrapper, so
  `session_renamed_from.json` (old → new) routes old-title requests to the
  renamed session and lets the MCP identity resolve (rows carry
  `renamed_from`); an entry goes with its session or once the agent's pane pid
  changes (a relaunch bakes the new name in). Aliases from before this are
  migrated to titles at server start, except sessions whose agent is running (their MCP server holds the old name; they migrate at a later start with the agent down; `rename_migration.json`) and from each
  browser's seed; one that is not a free, valid name stays a display alias.
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

### Other people's keystrokes

The owner's own browsers attach a writable `tmux attach-session` client per
terminal socket — their own machine. Anyone else who types into a pane never
holds one: a guest link's browser (the agent terminal and shell sockets,
[people.md](people.md) "Guest links") and a peer typing into a session shared
over a peer link (`peer/sessions_bridge.py`). On a writable client the tmux
prefix key hands the typist tmux itself — `prefix c` opens a new window running
a plain shell outside the session's agent, `prefix s` switches to any other
session on the server, `prefix :` runs `run-shell`. So their terminal is split
(`web/core/guest_terminal.py`, shared by both):

- **Output** comes from a READ-ONLY client (`attach-session -r`), which tmux
  also marks `ignore-size`: it sizes the window while it is the only client and
  leaves the owner's size alone while the owner's client is attached. The only
  bytes written to it are the terminal's own answers to tmux's probes
  (`terminal.is_terminal_reply`: DA1/DSR/CPR…), so the pane never waits on a
  probe.
- **Input** goes into the exact pane (`=<name>:`) with `tmux send-keys -l`,
  straight to the program and never through tmux's key table; NUL goes as the
  key `C-Space`, and a long paste is cut into calls under tmux's ~16 KiB
  command cap. Mouse reports (the web terminal's wheel) are routed the way the
  root wheel binding would: to the program when it has the mouse (SGR only),
  else the wheel scrolls copy mode; clicks are dropped.

Peers type under the session's **one keyboard** too
([people.md](people.md) "Owner, driver, viewers"): the bridge's term holds a
seat as the person `peer:<link id>`, named after the peer, so the owner's rail
shows the peer as driver or viewer by name. The peer drives only when nobody
else does; a refused keystroke is dropped with a notice drawn on the peer's
bottom row (the opener renders only the byte stream, so the notice is output:
cursor saved, last row cleared, reverse video, cursor restored). The seat goes
when the term closes.

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

## After a restart: held offline, background revive, and nothing dropped silently

Agents revive **lazily** — `_ensure_agent_session` (`web/core/agent_sessions.py`)
relaunches a dead agent tmux when a terminal attaches, a prompt is sent, or the
rail's Offline → **Restart** chip calls `POST /api/instances/{title}/revive` —
except for a session that is **held offline** (below), which only a person's
Start brings back.

### Held offline (the default after a crash)

On 2026-11-04 the machine restarted twice under memory pressure: on each boot
the revive sweep relaunched every agent that had been running, the auto-revive
relaunched the ones the OOM killer took, and the grid's panes relaunched every
agent they attached to — and memory ran out again. So with
`general.revive_on_start` **off** (the default since then) a session whose
agent **died** — not a clean quit, not Paused, not asleep — is **held**
(`web/core/held.py`): nothing starts it by itself, so it uses no memory (no
tmux, no agent) until someone presses Start.

- **When a session becomes held.** (a) At server start —
  `revive.hold_on_boot`, awaited in the lifespan *before* the server answers
  anything, so a pane reattaching a moment later is already refused: every
  session the old sweep would have revived (persisted status running, not
  paused / provisioning / peer-shared / asleep, workspace present, no
  clean-quit exit marker — a usage-limited one included) whose agent tmux is
  gone. Its reason is `out of memory (killed)` when its exit marker says
  SIGKILL (137), else `the server restarted`. Holds already on disk stay
  (with their first reason); those of sessions no longer in the engine go.
  (b) While the server runs — `revive.observe`: an agent this server saw live
  that dies uncleanly — SIGKILL (137, or no marker while its hooks said
  `working`) → `out of memory (killed)`; any other unnatural exit code →
  `the agent crashed`. A clean quit (0/130), a sleep and a pause are never
  held; neither is a death with no marker and no sign of a turn in flight.
- **What refuses it.** `_ensure_agent_session` returns `HELD_REFUSED`
  (`"offline since a crash — press Start to bring it back"`, next to
  `PAUSED_REFUSED`) for a held session with no live agent, so every
  relaunch-on-touch path refuses it exactly like a Paused one: the terminal
  socket closes with **4424** (4423 is Paused) after an error frame carrying
  `held: true` and the row's hold fields; `/send` puts a submitted prompt on
  the prompt queue — never lost — and answers **409** `{error: HELD_REFUSED,
  held: true, queued: true}` (an unsubmitted paste, an Enter or dictation is
  409 `held: true, queued: false`); MindFlock's own composed messages queue
  the same way; Send now is 409 `held: true` with the item kept; the queue
  drain and the limit watch keep their items; a wake from sleep refuses; the
  sweeps skip it (`held offline`), the auto-revive never queues it.
- **What releases it.** A person's Start: `POST /api/instances/{title}/revive`
  (the hold is released, then the agent launched; a launch that fails puts the
  hold back) and `POST /api/server/revive-all`, plain or with `{titles}` (each
  hold released right before its launch; one skipped for `low memory` stays
  held). Also removing or closing the session, pausing it (Resume is a
  person's start), moving its workspace, and an agent found running again
  (started outside MindFlock — a live reading or an attach drops the stale
  hold). Resume of a Paused session is unaffected.
- **Persisted.** `<config dir>/held.json` (`title -> {since, reason}`),
  through `title_store` like `sleeping.json`, so a hold survives the next
  crash; `MINDFLOCK_HELD_FILE` points it elsewhere (the test suite does).
- **The row.** `_instance_json` gains `held_offline: true`, `held_since`
  (epoch s) and `held_reason` while held; the fields are omitted otherwise.
- **One notice.** When anything is held at boot, `revive.announce_held` waits
  out the boot quiet window and emits ONE `system.sessions_held` event
  (`data: {count, titles, detail}`), which the notify addon's default-on
  `sessions_held` rule turns into "N sessions stayed offline after the
  restart — press Start on the ones you need." — not an Offline alarm per
  session.
- **Opt out.** `general.revive_on_start: true` restores the old behaviour:
  nothing is held (the boot drops any holds on disk), the startup sweep and the
  auto-revive below run. The boot hold itself is off under pytest
  (`_HOLD_ON_START_ENABLED`) and, launching nothing, stays on under
  `MINDFLOCK_REVIVE_ON_START=0`.

### The background revive sweep (opt-in)

With `general.revive_on_start` on, the server also runs a **background revive
sweep** (`web/core/revive.py`):

- **When.** Once per boot, registered by the lifespan, *after* the boot quiet
  window (`_BOOT_QUIET_SECONDS`, 30 s) so the notification settle logic sees the
  revives as real transitions instead of being flooded during startup. Gated by
  `general.revive_on_start` (unset = **off**); `POST /api/server/revive-all`
  runs the same sweep on demand — a person's Start all, which also starts
  sessions held offline. Never registered under pytest — `ENGINE` is the
  developer's real state there, and the sweep launches agents.
- **Who.** Every session whose persisted status says its agent was running
  (`Running` / `Ready`). Skipped, each with a reason in the summary: `paused`,
  `still provisioning` (`Loading`), `peer-shared` (relaunched by the peer
  service), `workspace missing`, and `usage limit` (a hold the prompt queue
  armed, or the provider's own live meter reading a spent window — the queue
  resumes those when the window reopens), and `sleeping` (put to sleep on
  purpose, see [Sleeping agents](#sleeping-agents): a reboot must not wake
  every sleeper at once), and `held offline` (unless the sweep is a person's
  Start all). A session whose agent tmux is still alive (a server restart that
  left tmux up) is counted as *already running*.
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
  off the everyday sleeping). **Under memory pressure** — `memory_pressure`
  low or critical — it is `general.memory_sleep_minutes` (unset = 5; never
  above the everyday wait when that is on; `0` = never sleep for memory),
  which applies even with everyday sleeping off. Those sleeps record
  `reason: memory`; longest-idle first is what frees the most stale memory
  soonest. A pass that sleeps any for memory emits ONE `system.sessions_slept`
  event — the notify rule **Idle agents are put to sleep to free memory**
  (on by default): *"Memory is low: 2.1 GB free of 20.0 GB (10%), so 2 idle
  agents were put to sleep: a, b. They wake when you open or message
  them."* An agent mid-turn is never a candidate at any level (the *turn not
  ended* / *not idle* vetoes). `POST /api/instances/{title}/sleep` and `mindflock
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
imports it too) returns `ok` / `low` / `critical` (`unknown` where there is
nothing to read — non-Linux — which never gates). The level reads **distress**,
not a share of "available": until 2026-11-10 it was mostly MemAvailable as a
share of RAM (low at 15 %, critical at 7 %) plus Windows' raw available, and it
read critical at 9.7 of 24 GB used with PSI at 0.2 % and no swap-in. `low`
means distress is near (automatic starts wait); `critical` means the machine
is in trouble (held, it lets the shield put idle agents to sleep).

| Signal | low at | critical at |
|---|---|---|
| PSI `full avg60` (`/proc/pressure/memory`) — every non-idle task stalled | ≥ 5 % | ≥ 15 % |
| PSI `some avg60` — at least one task stalled | ≥ 20 % | ≥ 50 % |
| Swap-in rate (`pswpin` in `/proc/vmstat`, over the last ~60 s) | ≥ 256 pages/s (~1 MB/s) | ≥ 2048 pages/s (~8 MB/s) |
| OOM kills (`oom_kill` rising in the last 5 min) | any | any, plus another low signal |
| `MemAvailable` (`/proc/meminfo`) | ≤ 8 % of `MemTotal` (`general.memory_low_pct`) or ≤ 1.5 GB | ≤ 4 % (`general.memory_critical_pct`) or ≤ 1 GB |

Swap merely **in use** is not a signal (cold pages parked there cost
nothing); reading them back at a rate is. An OOM kill alone is only `low`:
the counter also counts kills inside a memory-capped cgroup (a test suite in
its own scope), which say nothing about the machine. The available floor is
genuinely small: under ~1 GB one agent starting or a test run pushes the
kernel into direct reclaim and swap whatever the box; the share keeps the line
proportional on a big box. The two shares are settings (per device, never
synced; `0` drops the share, leaving the floors and the other signals); the
rest is fixed.

**Sustained.** A reading past a line raises the level only once every reading
for 30 s (`SUSTAIN_SECONDS`) has pointed at least that high; until then the
snapshot carries `pending` (the level it is heading for) and `why`, and
`level` stays where it was. One bad sample moves nothing. Stepping back down
is immediate once every reading clears a recovery line a margin beyond the
entry one (share + 2 points / 2 GB / some 10 / full 2 / swap-in 64 pages/s to
leave `low`; share + 2 points / 1.5 GB / some 30 / full 8 / swap-in 512
pages/s to leave `critical`), so a box hovering at a line doesn't flap.

**Explained.** The snapshot's `why` lists the signals that set the level in
plain words, and `describe()` (every refusal, the banner's message, the
shield's log) joins them: *memory is critical: processes stalled on memory
34% of the last minute*, *memory is low: swapping 1.6 MB/s back in from
disk*, *memory is critical: Windows commit charge is at 93% of its limit
(55.8 GB of 60.0 GB)*. A reading is cached for 2 s, and the level is per
process: the ticket pipeline keeps its own, so it and the server can briefly
disagree near a line.

`memory_pressure.replay(rows)` runs the rule over `~/.mindflock/logs/memory.jsonl`
history (each line's `system` dict) for a back-test; the sampler logs every
signal the rule reads (`psi_*_avg60`, `swap_in_mb_s`, `oom_kills`,
`host_headroom_mb`, `host_commit_pct`, `host_pages_in_median`, `pending`,
`why`), so the replay of newer history is exact. Older lines lack MemFree,
commit and paging, so the back-test of 2026-11-10 approximated (PSI avg60
from the logged avg10s; WSL reusing half its available, where 81 % was
measured that day). Over 2026-11-08 to 11-10 (4,596 samples) the old rule
read 1,032 `low` and 421 `critical`; the new one 99 `low` and 44 `critical`
(the 44 on 11-08: PSI `full` at 50 %+ with 1.5 GB available, a real thrash).
Of 11-10's 243 samples the old rule read 96 `low` and 44 `critical`; the new
one 239 `ok` and 4 `low` (a real 12–19 % full stall during a 3 GB
`memory.reclaim`).

**Windows under WSL.** A WSL2 guest sees only its own VM: Windows can be
paging its own apps out while `/proc/meminfo` still reads plenty, because the
VM has grown into everything `.wslconfig`'s `memory=` allows and Windows
cannot take memory back from it. Inside WSL the web server starts
`backend/host_memory.py`: ONE long-lived `powershell.exe` printing, every
10 s, Windows' available physical memory
(`Win32_OperatingSystem.FreePhysicalMemory`, free + standby) and total, and
from `Win32_PerfFormattedData_PerfOS_Memory` the commit charge, the commit
limit and `PagesInputPersec` (`mem <avail kB> <total kB> <committed kB>
<limit kB> <pages/s>`; without perf counters the short `mem <avail> <total>`
line still goes, and the parser takes both). A daemon thread reads it. The
host has its own level, sustained on its own, and the level is the **worse**
of the guest's and the host's, so everything below (spawns waiting, the
launch gate's budget, the [memory shield](#memory-shedding-before-it-bricks)
shedding idle agents) acts on a Windows in distress too. Windows' raw
available is **not** a signal — it swung between 0.7 and 3.4 GB within
minutes on 2026-11-10 with nothing short. The host's signals:

| Signal | low at | critical at | leaves low / critical |
|---|---|---|---|
| Commit charge / commit limit (at the limit an allocation fails) | ≥ 85 % | ≥ 90 % | ≤ 80 % / ≤ 87 % |
| Hard paging: the **median** `PagesInputPersec` of the last 6 readings (≥ 3), only while Windows' own available is under 2 GB | ≥ 3000 pages/s | ≥ 8000 pages/s | ≤ 1500 / ≤ 4000 |
| Headroom (below) | ≤ 2 GB | ≤ 1 GB | ≥ 3 GB / ≥ 1.5 GB |
| Windows' own available | — | ≤ 256 MB | > 512 MB |

The median, not the mean: one 20000 pages/s spike (an app starting, a scan)
would hold a mean over the line for the whole minute. With GBs available,
page-ins are cold file reads, not memory being evicted and read back. The
**headroom** (`memory_pressure.host_headroom`) is a floor only:
Windows' available plus what the guest can reuse out of memory the VM already holds
(MemAvailable − MemFree: its page cache). Windows counts those pages as used,
but a new agent that reuses them costs Windows nothing — on 2026-11-10
Windows read 0.8 GB free of 31.4 GB with ~11 GB of WSL page cache, and every
start and the accurate voice pass were held back with nothing short. Freeing
that cache does not hand it back to Windows soon (a 3 GB `memory.reclaim`
moved Windows' available by nothing in 30 s). Only when Windows' own
available is at or under 256 MB does it read critical whatever the guest
holds. The shield frees the bigger of the two shortfalls, the host's measured
on its headroom under the 2 GB floor (`host_low_line`); the launch budget is
capped by that headroom over the same floor. The snapshot carries `host`
(with `guest_reusable`, `headroom`, `headroom_pct`, `commit_pct`,
`committed`, `commit_limit`, `pages_in_median`, its own `level`, `pending`
and `why`) and `guest_level`; refusals name the Windows signal when Windows
is the cause (*Windows has only 0.9 GB to give (0.9 GB available, 0.0 GB WSL
can reuse)*). A reading older than 45 s is ignored (never a
gate); the loop exits when its pipe breaks and restarts with a backoff that
doubles from 15 s to at most 300 s. If no `powershell.exe` is found the reader
stops for the life of the process. `MINDFLOCK_HOST_MEMORY=0` turns it off; the
ticket pipeline and the tests never start it, so the pipeline's level is
always the guest's alone — while Windows is short it can read `ok` where the
server reads `low`, for as long as the shortage lasts, not just near a line.
Each server process runs its own loop: two servers on one state dir (`:8765`
and `:9000`) mean two resident `powershell.exe`. The loop only notices its
pipe is gone on its next write, so after a hard kill (`SIGKILL`) of the server
it lingers up to one more interval (~10 s).

While the level is `low` or `critical`:

- **Agent spawns wait** — `POST /api/instances` with `spawned: true` (the
  MCP's `spawn_session`) and an agent's `POST /api/tickets/start` (the MCP's
  `spawn_ticket_session`) are queued in the [launch gate](#launch-gate) and
  answered 202 `queued: waiting for memory`. A person's New asks first
  (below); a team run's own members are paced by the run's planner.
- **Team runs start no new lines** — the planner holds queued tasks exactly
  as it does for a usage limit (a task marked `start_now` still starts); the
  run's `waiting_for_memory` puts a *memory* badge on its rail header.
- **The ticket pipeline holds its next launch** (`session_runner`), logging why
  every 5 minutes; after 30 minutes it starts anyway with a warning, so a
  misreading can never stall intake for good. The reading is taken only after
  the launch spacing (60 s from the previous automatic launch coming up,
  `MINDFLOCK_LAUNCH_SPACING`): a new session takes most of a minute to reach
  full size, so a reading right after a launch says nothing about it. On
  2026-11-04 one PR scan launched a review per PR back to back, every one past
  an `ok` reading, and ran WSL out of memory.
- **The revive sweep waits**, as above, and hands what it can't start to the
  launch gate's queue. A person's single Start asks first (below).
- **Idle agents sleep sooner** — after `general.memory_sleep_minutes` (5)
  idle instead of `general.sleep_idle_minutes` ([Sleeping agents](#sleeping-agents)),
  longest idle first, giving back their CLI's and MCP servers' memory, with
  one notice per pass naming them.
- **The server sheds its own background work** (`server._memory_low()`):
  the per-session probe pool narrows from 8 threads to 2; the PR, merge-state
  and review lookups serve their cached answer past its TTL instead of
  forking `gh` per branch per minute (a forced lookup — a person pressing
  something — still asks); Ctrl+F's background pre-indexing skips its pass
  (Ctrl+F still indexes on demand); and the memory watch unloads the local
  speech models unused for 2 minutes (`speech.shed_for_memory` — the resident
  whisper models too, ~1 GB of the server's own memory, then `malloc_trim`).
  Model loads are refused while the level is low, so nothing reloads until it
  recovers.

### Launch gate

Every reading above is of memory *now*. An agent reads near zero the second
it starts and 350-800 MB a minute later, so a burst of starts passes every
check before any of them has grown. On 2026-11-04 that happened three ways:
Start all over 18 held sessions, an orchestrator fanning out a dozen workers,
and a PR scan launching a review per PR. The OOM killer fired three times and
WSL wedged. `backend/web/core/launch_gate.py` is the one admission gate every
agent start asks first. It answers **go / wait / refuse** from:

- **memory** (the level above);
- **agents still warming up** — every MindFlock agent tmux session created
  in the last 90 s (read from tmux's own `session_created`, so starts from
  any path and any process count), plus starts this server admitted that
  have no tmux session yet. Each still has the learned peak (below) minus
  what it reads now to come (all of it when unmeasured; ~700 MB, i.e.
  `general.agent_warmup_mb` / `MINDFLOCK_AGENT_WARMUP_MB`, with no
  measurements yet). The gate works out what memory would read once they
  have grown, and that projection counts like the level;
- **the budget** — how many more sessions fit, at what one has been
  *measured* to cost (below). An automatic start waits while the room is 0,
  checked after the cap; a person's start asks;
- **live agents** against a machine-wide cap (`general.max_live_agents`,
  the `MINDFLOCK_MAX_LIVE_AGENTS` env var wins; default one per 1.2 GB of
  RAM, 16 on 20 GB; `0` = no cap). The cap applies to automatic starts only.
  Both settings are per device and never synced.

#### Memory: admission by measured cost

The budget (`launch_gate.budget`, `GET /api/memory/capacity`) replaces a guess
per agent with what this machine's sessions really take:

```
room = floor((available − reserve − penalties − to_come) / session_mb)
```

- **`session_mb`** — the 75th percentile of the steady size the memory
  sampler measures per session (a session's whole tree: CLI, MCP servers,
  what it runs; the sampler is `core.memory_watch`, read through the
  `launch_gate.set_sizes_source` seam, else the rows' `memory.mb`). Sessions
  still warming and ones reading under 50 MB (asleep, closing) are not
  samples. Fewer than 3 samples: 700 MB. Never under 200 MB.
- **`peak_mb`** — the 75th percentile of the sessions' peaks, never under
  `session_mb` nor over 2.5× it (a peak from an 8-worker test run is not what
  a CLI starting grows to). Warming agents count at it.
- **`reserve`** — the low floor: `max(2 GB, general.memory_low_pct of RAM)`
  (8 % by default: 2 GB on 20 GB, 2.1 GB on 26 GB). The room is what fits
  *above* it.
- **`penalties`** — swap in use while it is being **swapped back in**
  (`swap_in_mb_s` ≥ 1 MB/s; up to 10 % of RAM: pages the agents are faulting
  back, and on WSL a disk-backed swap that drags the Windows host — swap
  merely parked costs nothing; a snapshot without the rate keeps the
  penalty), and a **rising** PSI stall (`some avg10` ≥ 5 % and above `avg60`):
  `avg10 / 200` of RAM, at most 15 % (20 % stalled → 10 % of RAM).
- **`to_come`** — the warming agents' growth still to come.

The room is 0 while the level reads low or critical, and never past the
live cap. On 20 GB with 15.6 GB available, no swap-in, no stall, nothing
warming and no measurements yet: (15.6 − 2.0) GB / 0.7 GB → room 19 (the live
cap, 16 by default, then bounds it). Measured at 1 GB per session it is 13.

What the room changes: an agent's `spawn_session` past it queues (`queued:
waiting for memory`, the reason saying what one more needs: *one more
session needs ~1.0 GB; only 0.4 GB is free above the reserve*); a team
run's pass starts at most `room` new lines; the MCP `whoami`,
`spawn_session` and `start_team_run` results carry `capacity: {room,
session_mb, hint}` so an orchestrator plans its worker count from it; a
person's own start asks first (`Memory is tight: … No room for another
session: one needs ~1.0 GB, 0.4 GB is free above the 3.0 GB this machine
keeps in reserve.`).

#### Memory: shedding before it bricks

Holding new agents back does nothing for the ones already running. On
2026-11-05 the owner's PC had to be rebooted: ~10 sessions, a 5 GB app and
Voice Lab's F5 voice (4.1 GB + 1 GB swap) on 19.5 GB of WSL. The **memory
shield** (`core/memory_shield.py`, called by the server's memory watch every
10 s) acts:

- **When**: only on **sustained** critical distress. The level itself turns
  `critical` only after a distress signal held 30 s (above; inside WSL that
  includes Windows alone — see [Windows under WSL](#memory-guard)); it must
  then read `critical` for 60 s more — or, already critical, PSI `full avg10`
  is at 25 % or past it (every runnable task stalled on memory a quarter of
  the last 10 s: a thrash under way, where waiting the minute would cost
  ~15 s of total stall). A 10 s burst with the level not critical sheds
  nothing. At most once per 90 s, so the next shed acts on readings taken
  after the memory came back. The log line names the signal: *acting on
  memory has been critical for 61s (swapping 9.5 MB/s back in from disk)*.
- **Which**: idle agent sessions that `sleep.veto` allows with a two-minute
  idle window — never one working, asking or on a dialog, open in a
  terminal, driven by a person, typed into or woken in the last two minutes,
  holding a background job, in a team run, or with working workers.
  Largest-and-longest-idle first (measured MB × idle time, capped at an hour).
- **How many**: only enough to bring available memory back over the `low`
  floor, counting each at its measured size (else the learned cost); at least
  one (critical from a stall, a swap-in storm or Windows' commit charge has
  no byte target: one at a time, the cooldown letting the next pass see the
  effect), at most six. Inside WSL the need is the larger of the guest's
  shortfall and Windows' shortfall under its 2 GB headroom floor
  (`host_low_line`).
- They are put to sleep exactly as *Put idle agents to sleep* does (Wake,
  opening or messaging one brings it back with its conversation). One notice
  (`system.sessions_slept`): *Memory critical — put 3 idle sessions to sleep
  (Wake brings them back): a, b, c*. Every decision is logged (`memory
  shield:` lines: the trigger, what was needed, the picks, what was kept
  awake and why).
- `general.agent_memory.shed_idle: false` (or `MINDFLOCK_SHED_IDLE=0`)
  turns it off. `GET /api/memory/capacity` → `shield` shows the last shed.

#### Memory: Voice Lab yields

A local voice host (Voice Lab, or any OpenAI-compatible model server on this
machine or network) is never woken or called while the level reads low or
critical: `TtsChain.pick` hands the line to piper, else the OS voice (piper
itself is held back while low), else the client's own voice; warms, prewarms
and health probes are not sent; the host call itself refuses
(`speech.host_held_back`). The log says why once per episode, and again when
it may be used. When the level turns `critical` and Voice Lab's backend
(`voicelab-backend.service`, `MINDFLOCK_VOICELAB_UNIT`; `0` = never) runs, it
is stopped with `systemctl --user stop`. It is socket-activated, so the next
voice request would start it again, and none reaches it until memory allows.
No unit, no systemd, no Voice Lab: nothing to do.

**Automatic starts wait in a FIFO queue.** That means an agent-spawned
create, a queued prompt relaunching or waking its agent, a mailbox wake, a
revive sweep's rest, and auto-revive after a kill. A newcomer never jumps the
queue. The queue drains by itself, one launch at a time, at least 20 s apart
(`MINDFLOCK_AUTO_LAUNCH_SPACING`), as the gate allows. Past 32 waiting
(`MINDFLOCK_LAUNCH_QUEUE_MAX`) a new one is refused. A waiting session's row
carries `waiting_for_memory` (`{id, title, kind, source, parent, since,
reason, position, creates}`); the rail says **Waiting for memory** on it, with
**Start anyway** on hover. An agent-spawned create that waits has no session
yet. It is listed as a provisioning placeholder row (`pending`, the one
force-starts use), so the orchestrator's `wait_for_session` and the rail both
see it. The queue is saved to `<config dir>/launch_queue.json`
(`MINDFLOCK_LAUNCH_QUEUE_FILE`) on every change. At boot each saved entry
comes back through its kind's factory (`launch_gate.register_kind`): a queued
create (`spawn` / `flight` / `schedule`, with its placeholder row), a queued
prompt's relaunch, a wake, a revive. Each waits for the gate again. An entry
whose session is gone (or no longer asleep) is dropped. An agent's queued
ticket start is not saved: its pending row does not survive a restart
either, so the orchestrator's `wait_for_session` reads it as `gone`.
Renaming a session (`core.rename_stores`, `launch_gate.rename`) moves its
waiting entry to the new name, rebuilt through the same factory (a launch
closes over the title it was queued with), and re-parents any worker still
waiting to be created under it, in the saved file too.

**A person's start asks first.** That covers New session, Duplicate, Start
(`/revive`), Start all (`/revive-all`), Resume and Wake. While the gate reads
low (now or projected), these answer **409** `{memory_confirm: true, level,
message, free, warming, live}`. The app shows its own dialog: *Start anyway*
(resend with `start_anyway: true`), *Put idle agents to sleep first* (`POST
/api/server/sleep-idle`, then try again), *Cancel*. Critical is the same
dialog with stronger words. A person is never queued and never capped. Start
all still paces its launches through the gate after the confirm.

**Paths.** Every place that launches an agent CLI, and how it is gated:

| Path | Gate |
|---|---|
| New session (`POST /api/instances`, person) | confirm |
| MCP `spawn_session` (`POST /api/instances` `spawned: true`) | queue (202 `queued`) |
| MCP `spawn_ticket_session` (an agent's `/api/tickets/start`) | queue (202 `queued`, `memory_reason`; its launch waits its turn) |
| A flight step starting a session (`create_result(auto="flight")`) | queue (the flight reads the pending row as provisioning) |
| A scheduled template fire (`auto="schedule"`) | queue. A person's Run now is not queued |
| Duplicate (`/copy`), Start (`/revive`), Resume, Wake, Start all | confirm |
| A spoken "new session" (voice) | says why instead of starting while memory is low |
| Revive sweep (boot / Start all's rest) | paced by warming; the rest queued |
| Auto-revive after a kill (`revive.auto_tick`) | holds while the gate says wait |
| Prompt-queue drain relaunching or waking its agent | queue |
| Mailbox delivery waking a sleeper | queue |
| Team runs | the planner holds new lines on memory, warming or the cap (`decide(...)["why"]`), and a pass starts at most `launch_gate.room()` new ones |
| Ticket pipeline (`ticket_ingestion.session_runner`, another process) | its own wait loop on `launch_gate.decide` (memory, warming, cap), capped at 30 min |
| The phone (`/m`): Restart, Wake, Resume, New session | confirm (its ask sheet, Start anyway) |
| A terminal attach / Send waking a sleeper | not gated (a person's action) |

A queued create runs *gated*: its own older memory check
(`session_create.memory_refusal`) is skipped, so **Start anyway** on a queued
worker starts it even while memory still reads low.

**A squeeze must not wedge the server.** On 2026-11-04 the server twice kept
its listen sockets but answered nothing for minutes (the accept backlog full,
~190 % CPU, every thread on a futex): the signature of the process thrashing
— whichever thread holds the GIL takes a major page fault, and every other
thread, the event loop's included, waits on the GIL behind it. Three things
keep it answering, and say why when it doesn't:

- **The ticks have their own pool.** The always-on loops (instances, state,
  dialog watch, Ctrl+F index, red zone, sleep) run on `server._BG_POOL` (6
  threads, `mf-tick-*`) via `_in_bg_pool`, not on asyncio's default executor
  (12 threads) that every request handler's `await asyncio.to_thread` waits
  on. A tick stuck on a slow child can delay other ticks, never a request.
- **Less to page in** — the shedding above: fewer threads and children
  touching memory at once, and the server's largest allocation released.
- **A stall writes itself down** (`core/loop_watchdog.py`). The loop stamps
  a heartbeat every second; a daemon thread checks it, and when it is 10 s
  old (`MINDFLOCK_LOOP_STALL_S`) appends to `<config dir>/logs/stacks.log` a
  header — time, how long, memory level / available / PSI, load, the
  server's RSS and thread count — then every thread's stack (`faulthandler`),
  again every minute of a long stall, and an "answered again after N s" line
  when it recovers. `MINDFLOCK_LOOP_WATCHDOG=0` turns it off. `kill -USR1
  <server pid>` still dumps on demand (no header) into the same file.

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
server keeps its own score. The server never goes **below 0**: that needs
`CAP_SYS_RESOURCE`, which a normal install never has, and if it did run
privileged the lowered score would be inherited by everything it starts that
doesn't raise its own (the tmux server, git, the pipeline child) — raising the
agents already puts the server behind them. At boot it does come back *down*
to the lowest non-negative value the kernel allows (`run.protect_from_oom`):
nothing to do when started from a shell or WSL's init (0), 100 instead of 200
under a systemd user manager. `MINDFLOCK_SERVER_OOM_PROTECT=0` skips it.

#### Before the OOM killer: each agent in its own memory-limited scope

**What crashed WSL on 2026-11-04 was the swap, not the victim choice.** The
kernel log of the dead boot (`journalctl -k -b -1`) shows RAM and all 16 GB of
swap full (~35 GB of anonymous memory across ~18 agent trees) before the OOM
killer fired; it did take agent-tree processes (score 500), but only after
minutes of swap thrash, and WSL never recovered. The biggest processes were
`gitnexus analyze --force` runs from a repo's pre-commit hook — 3.8, 4.3 and
4.6 GB each, several at once (every agent that commits starts one, and
GitNexus sizes its V8 heap to 0.75x RAM: `--max-old-space-size=16384`) —
then `pytest -n 8` suites (8 workers x ~0.7 GB) in other sessions.

So every agent launch (the exit-marker wrapper and `TmuxSession.start`) now
runs inside `systemd-run --user --scope` (`backend.providers.agent_limits`):

- **Per agent**: `MemoryHigh` 30% of RAM (the kernel throttles and reclaims
  that tree above it), `MemoryMax` 40% (above it the OOM killer acts *inside
  that agent's cgroup* — its biggest process, the test worker or the indexer —
  long before the machine is out), `MemorySwapMax` 15%.
- **All agents together**: every scope sits in `app-mindflock.slice`, which
  gets `MemoryHigh` 65% / `MemoryMax` 75% / `MemorySwapMax` 40%
  (`general.agent_memory.fleet_high` / `fleet_max` / `fleet_swap`), so the
  kernel throttles and reclaims the agents *collectively* before the whole
  machine thrashes: ten agents each under their own cap would still swap the
  box. The properties are set with `systemctl --user set-property --runtime`
  (idempotent) at server start (`agent_limits.ensure_fleet_slice`) and again
  on every fresh scope probe (at most every 10 min), so a restarted user
  manager gets them back; the server, the tmux server, the editor and the OS
  keep the rest whatever the agents do.
- The scope is named `mindflock-<session>-<ms>.scope`
  (`systemctl --user status 'mindflock-*'` shows each agent's memory). The
  pane's terminal, environment, cwd and exit status are the agent's own:
  `systemd-run --scope` execs `sh -c <launch>` in place.
- Availability is probed with a throwaway scope (cached 10 min, a "no" for
  1 min — WSL starts the user manager lazily, so a boot-time "no" is picked up
  a minute later). No `systemd-run`, no user bus, macOS: the launch string is
  exactly what it was.

**`gitnexus analyze` takes turns.** When GitNexus is installed, launches put
`<run dir>/bin` first on the agent's `PATH`; its `gitnexus` shim runs
`analyze` through `mindflock slot --pool gitnexus --max 1 --timeout 1800`
(one at a time machine-wide, first come first served, waits for memory) with
`NODE_OPTIONS=--max-old-space-size=4096` (GitNexus honours a user heap instead
of its 0.75x RAM default; a heap already in `NODE_OPTIONS` is kept) and
`oom_score_adj` 800. Everything else (`gitnexus mcp`, `augment`, `status`…)
execs the real binary untouched. That covers a pre-commit `gitnexus analyze`,
`node .gitnexus/run.cjs analyze` (it looks `gitnexus` up on `PATH`) and an
agent running the command itself; `npx gitnexus` bypasses it.

All of it is `general.agent_memory` in settings.json (see
[configuration.md](configuration.md)); `MINDFLOCK_AGENT_SCOPE=0` turns the
scopes off and `MINDFLOCK_GITNEXUS_GATE=0` the shim (the test suite sets
both). It applies to agents launched after the change; running ones keep
the cgroup they started in.

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
  own" switch — unset = off, and then the killed agent is
  [held offline](#held-offline-the-default-after-a-crash) instead) and
  `MINDFLOCK_REVIVE_ON_START=0`; off under pytest;
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
