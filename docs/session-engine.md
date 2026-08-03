# Session engine

Packages: `backend.session`, `backend.config`, `backend.cmd`, `backend.log`.

## Instance lifecycle (`session/instance.py`)

An `Instance` is one session: title, program, git workspace, tmux session, status.
(`session/preflight.py` checks the required binaries before a session launches.)

**Statuses** (defined in `session/storage.py`, serialized as ints, Go wire contract):
`Running(0)` — agent working · `Ready(1)` — waiting for input · `Loading(2)` —
starting up · `Paused(3)` — worktree removed, branch preserved.

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
later.

**`Start(first_time_setup)`** — builds the worktree (strategy above), resolves the
launch command through the **provider framework** (`providers.resolve(program)`),
writes a launcher script for provisioned sessions, and starts tmux in the worktree.
The tmux `launch_command` is set separately from `program` so the human-readable
program name still drives trust-prompt and idle detection. On any failure the
partial worktree/tmux is torn down before the error propagates.

**`Pause()`** — commits dirty changes locally
(`[mindflock] update from '<title>' on <date> (paused)`, `--no-verify`), detaches
tmux (output preserved), removes the worktree but keeps the branch, and copies the
branch name to the clipboard. Clone-strategy provisioned workspaces are preserved
on disk across pause.

**`Resume()`** — refuses if the branch is checked out elsewhere; re-runs `Setup()`
(provisioned workspaces are re-provisioned), reattaches or restarts tmux.

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
  fast, and native to pause/resume.
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
