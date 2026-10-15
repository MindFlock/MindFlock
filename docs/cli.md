# The `mindflock` CLI

The console entry point (`backend/cli.py`) has two kinds of commands. **Host
commands** run things locally: `serve`, `doctor`, `uninstall`, and `mcp`, the
MindFlock MCP stdio server. **Session commands** (`new`, `ls`, `attach`, `close`,
`delete`, `pause`, `resume`, `sleep`, `wake`, `open`, `events`, `msg`, `inbox`; `rm` is an alias) are thin clients over a *running* server's
HTTP API (`backend/client.py`). Session commands never spawn an engine of
their own: the terminal and the web UI drive the same server, so a session
created from either shows up in both.

`mindflock --help` lists the commands in four groups — **Get started** (`init`,
`serve`, `doctor`), **Sessions**, **This computer** (`update`, `restart`,
`devices`, `join`, `token`, `uninstall`) and **Advanced** — and ends with
"New here? Run mindflock init". `git-credential` (git's credential helper, not
a human command) works but is never listed.

## Host commands

```bash
mindflock serve             # start MindFlock (localhost only, port 8765); prints the three steps + the address to open
mindflock serve tailscale   # also bind this node's Tailscale IPs for phone/tailnet access (URL + QR + token)
mindflock serve --port 9000 # custom port
mindflock serve --setup     # run the guided first-run setup (= `mindflock init`) before binding the port
mindflock doctor            # grouped checks + ONE next step; exit 1 if a check fails
mindflock doctor --all      # …listing the optional checks too (Tailscale, peer links, caches…)
mindflock doctor --fix      # install everything missing in one go (asks once), then offer the sign-ins
mindflock init              # the three steps: install the tools, sign in to your agent, pick a project folder
mindflock uninstall         # undo MindFlock's writes to your repos (see below)
mindflock mcp               # the MindFlock MCP server on stdio (for an agent CLI to start)
mindflock mcp --print-config  # how to register it with your own Claude Code / Codex
mindflock token             # print this machine's access token (for sign-in / pairing)
mindflock update            # install the newest release (the way install.json says) and restart the server onto it
mindflock update --check    # …only print the installed and newest versions (exit 1 when behind)
mindflock restart           # restart the running server (onto whatever is installed)
mindflock slot -- CMD ...   # run a heavy command (tests, builds) once a machine-wide slot is free
mindflock --version         # print the installed version
```

`serve tailscale` binds 127.0.0.1 plus this node's Tailscale addresses — the
port is on your tailnet but not your LAN. When Tailscale isn't running at boot
it binds every interface (0.0.0.0) with a warning and restarts itself onto the
narrow bind once Tailscale is up; it also binds 0.0.0.0 when the Tailscale
addresses can't be bound on this machine (userspace networking, or WSL with
Tailscale on the Windows side). `MINDFLOCK_BIND_ALL=1` asks for every interface
on purpose (a phone on your LAN without Tailscale). It auto-enables the
access-token gate:
unauthenticated clients get 401, and the token + QR code are printed in the
startup banner. `mindflock token` prints the same token later (it reads the
settings store directly, so it works with no server running). The default `serve` (local) binds 127.0.0.1 only.

### The three steps (every terminal door)

`install.sh`, `mindflock init` and the top of `mindflock serve` print the same
block, rendered by one function (`backend/init_wizard.py` `steps_block`), and
every door — `mindflock doctor` included — ends on exactly ONE next-action line:

```
MindFlock is installed. Three steps to your first agent:
  ✓ 1. Install the tools      git, tmux, Claude Code
  ✗ 2. Sign in to your agent  run claude, sign in, then type /exit   ← you are here
  ✓ 3. Pick a project folder  /home/me/code/webapp
  Optional, later: GitHub, ticket sources, phone. Already use MindFlock elsewhere? mindflock join

Next: sign in to Claude Code — run claude, sign in, then type /exit. Then run mindflock init again.
```

The first line becomes **You're set.** only when all three are ✓ — the agent's
sign-in counts as required, the same reading as the web checklist's
"Needs attention" (`setup_steps.doctor_progress`), so a signed-out agent never
reads as ready. Steps 1 and 2 come from a doctor run; step 3 is the folder
`mindflock init` picked (`general.last_repo_path`). A folder with no git gets
"has no git yet — run git init there, or pick a repo".

- **install.sh** runs `mindflock init --steps` (read-only, hidden from
  `--help`) and ends on `Next: run mindflock init`. It no longer runs the
  doctor itself (init owns the tools, the sign-in and the folder); the desktop
  app's run (`MINDFLOCK_NONINTERACTIVE=1`) ends on one line instead — tmux and
  the agent come next, in the app.
- **`mindflock serve`** prints the steps with step 3 boxed — *Open
  http://127.0.0.1:PORT in your browser — it walks you through the rest* — or,
  once all three are done, just the boxed address. In local mode one line,
  `Phone: mindflock serve tailscale`, says how to reach it from a phone. The
  text is also kept for `GET /api/setup` (`report`) on a first run.

### `mindflock init [--yes]`

The guided first run — the three steps, in order. It offers to install
whatever step 1 is missing in one go and to run your agent's sign-in (the same
loop as `mindflock doctor --fix`, limited to what a first agent needs: nothing
optional — Tailscale, GitHub, the MTU — is offered here, and only those rows
answer Enter = yes). An agent closed before you signed in gets
"Claude Code closed before you signed in. [R]etry / [s]kip". Then it picks the
project folder (remembered as `general.last_repo_path`) and ends on the block
above.

Setup's "first computer, or join one you already have?" question (joining
brings the default agent, the GitHub token and ticket sources along) is asked
before the sign-in — but only when another MindFlock answers on this tailnet;
otherwise the block's last line points at `mindflock join`.

Nothing in the wizard ticks a web checklist step by hand: the doctor run it
performs marks *Install the tools* and *Sign in to your agent* when they pass,
and the first session that actually works marks *Start your first agent*.

`--yes` takes every default without asking and never runs an installer
unwatched (the block says what's missing). A piped stdin says so FIRST — "No
terminal attached, so this is a read-only summary" — then prints the block and
its next action. Exit code 1 when a required check still fails, like
`mindflock doctor`. `mindflock serve --setup` runs the same wizard before
binding the port and leaves the block to the serve banner.

### `mindflock doctor [--all] [--fix]`

The checks under three headings, with a legend (✓ good · ! needs attention ·
✗ missing · - not set up): **Needed to run an agent** (git, tmux, your agent,
its sign-in), **Needed to ship** (git identity, GitHub) and **Optional**,
folded into one line — "N optional checks — mindflock doctor --all" — unless
`--all` (a failing optional row, such as an opted-in local model that is down,
is still shown). It ends on one next step: `mindflock doctor --fix installs the
N missing tools`, the agent's sign-in, `mindflock init` for the folder, or
`mindflock serve`. Exit 1 when a check fails (a sign-in that is still to do is a
warning, not a failure). `GET /api/doctor` carries each check's heading as
`group` (`run` | `ship` | `optional`).

Once the Database explorer has installed a driver, an informational
**Database drivers** row under Optional lists them — `psycopg (postgres),
pymysql (mysql)` — the ones updates carry along.

`--fix` installs every missing `run` tool in one go (one confirmation, one sudo
prompt), then offers each remaining fix one at a time: a `run` row with
Enter = yes, a `ship` row (`gh auth login`) with Enter = no. Optional fixes are
offered only with `--fix --all`, and always with Enter = no.

### `mindflock update [--check] [--ref REF] [--yes] [--all-devices]`

One update path for every way MindFlock gets installed. The command reads
`$MINDFLOCK_HOME/install.json` (default `~/.mindflock/install.json`, written by
install.sh: `{"method": "uv-tool"|"pipx"|"editable", "ref", "sha", "python",
"at"}`) to learn how this engine was installed — with no file it detects the
method from where the running package lives (a `…/uv/tools/…` venv, a
`…/pipx/venvs/…` venv, or a checkout) — prints the installed version (and
commit), the method and the newest release, asks once (`--yes` skips the
question), then:

- **uv-tool** — runs the same pinned `uv tool install --force --python 3.12
  "mindflock[web] @ git+<repo>@<commit>"` the server's own updater runs (the
  tag is resolved to its commit first, so what gets installed can't move under
  it);
- **pipx** — runs `pipx install --force "mindflock[web] @ git+<repo>@<tag>"`;
- **editable** (a development checkout) — prints the git commands that update
  it (`cd <checkout>`, `git fetch --tags origin`, `git checkout <tag>`, `uv sync
  --group web`, `mindflock restart`) and changes nothing, exit 0. A checkout is
  recognised from the running package even when a stale install.json says
  otherwise, so a release install is never written over your working tree.

Database explorer drivers installed from the app (recorded in
`~/.mindflock/dbclient/drivers.json`) ride along: one `--with <package>` each
on the uv line (`--with 'psycopg[binary]' --with pymysql`), and `pipx inject
mindflock <packages…>` after a pipx install (a failed inject only warns — the
server reinstalls a missing recorded driver when it starts). The rollback
carries them the same way. See
[extensions.md](extensions.md#worked-example-the-database-client).

The install runs in the foreground with its output on your terminal. After a
good install the command refreshes install.json (`ref`, `sha`, `at`) and, if a
server is running, restarts it onto the new engine (`POST /api/server/restart`
with `{"keep_mode": true}` — a server started in tailscale mode stays in it;
sessions are tmux sessions and keep running), waits until the server's own
hello answers with the new build, and prints the version it now reports. With
no server running it just installs and says `start it with: mindflock serve`.
It exits 1 when the install fails (the installer's output is already on the
terminal), when the ref resolves to nothing, or when the server doesn't come
back on the new build in time (run `mindflock restart`).

`--ref` takes a release tag or a commit (a commit `git ls-remote` can't name is
installed as itself). `--check` only prints the two versions and exits **0 when
current, 1 when behind** (2 when GitHub couldn't be reached — "couldn't tell"
is not "current"). `--all-devices` is `mindflock devices update`. Settings →
Advanced in a browser does the same install through the server
(`/api/update/*`), and the desktop app's **Update** does it for the engine and
the app together — see [electron/README.md](../electron/README.md#updating).

### `mindflock restart`

Restarts the running server (`POST /api/server/restart` with
`{"keep_mode": true}`, so a server started in tailscale mode stays in it) and
waits until it answers again — use it after installing a new engine underneath a running
server (install.sh does this for you). `mindflock serve` on a port where an
older build is still running says so and points here.

### `mindflock uninstall [--purge] [--keep-worktrees] [--dry-run] [--yes]`

Reverses what MindFlock wrote **outside its own venv**. `uv tool uninstall
mindflock` removes the venv and the `~/.local/bin/mindflock` shim, but two
things survive it and cause real problems:

* **Session worktrees.** `~/.mindflock/worktrees/…` are live git worktrees
  *registered inside your repositories*. Deleting them with `rm -rf` leaves
  every affected repo with `git worktree list` entries pointing at paths that
  no longer exist. This command removes them through git (`worktree remove` →
  `branch -D` → `worktree prune`) so the repos stay consistent.
* **Activity hooks.** In-place sessions merge hook entries into your repo's
  `.claude/settings.local.json` / `.codex/hooks.json`. The hook body is
  self-contained inline `python3` with no dependency on the `mindflock`
  binary, so it keeps firing after the engine is gone — and re-creates
  `~/.mindflock-assistant/.activity-markers`, silently regrowing a directory
  you just deleted.

It also removes the `.mindflock_*` scratch files and the `.git/info/exclude`
lines that named them, and the per-session MindFlock-MCP run files
(`~/.mindflock/run/mcp-*.json`, reported as `MCP run files: N`). Those go even
without `--purge`, since they only describe sessions.

```bash
mindflock uninstall --dry-run   # print everything that would be removed, change nothing
mindflock uninstall             # worktrees, hooks, scratch files (keeps settings + history)
mindflock uninstall --purge     # …and delete ~/.mindflock and ~/.mindflock-assistant
```

What it deliberately will **not** do:

* delete any directory of yours — only MindFlock's own files inside one;
* touch a worktree outside `~/.mindflock/worktrees` (MindFlock didn't create
  it, so it's reported and left alone);
* remove hook entries you wrote — only ones carrying MindFlock's tag, even
  when they share a file;
* delete a branch that already existed before its session;
* run while a server is up (that would tear down worktrees under live
  sessions). `--dry-run` is still allowed then.

It also **leaves "Your devices"** first (best-effort, the way Settings →
Devices → Leave does): the other devices get this computer's tombstone, so
they don't keep an offline row for it forever, and PR review and issue
handling move off it when it ran them. Members that are offline (or a
Tailscale that is down) miss it: the command names them and the fix —
`mindflock devices remove <this computer>` on one of them. With `--purge`
that is a `warning:` on stderr, since the group's key is deleted with
`~/.mindflock` and only another device can take this one off its list
afterwards. `--dry-run` only says it would leave.

`--purge` is opt-in because `~/.mindflock` and `~/.mindflock-assistant` hold
your settings, session state and usage history — without it, a reinstall picks
up where you left off. The final step is *printed rather than run*, since this
process is executing out of the venv it deletes:

```bash
uv tool uninstall mindflock
```

### `mindflock accounts [ls|add|login|use|rm]`

Manage **auth profiles** — multiple Claude accounts, OpenRouter keys and other
identities sessions can run under, swappable without logging any CLI out (see
[docs/accounts.md](accounts.md)). Prefers a running server (so the app sees
changes immediately) and falls back to `~/.mindflock/settings.json` offline,
which is why it lives here rather than with the session commands.

```bash
mindflock accounts                     # list; '*' marks the default
mindflock accounts add work --label 'Work'          # a second Claude login
mindflock accounts login work          # runs the CLI's own OAuth flow, isolated
mindflock accounts add or --kind openrouter --key sk-or-… --model MODEL
mindflock accounts use work            # default for new sessions ('default' = none)
mindflock accounts rm or
```

`add` flags: `--kind account|api_key|openrouter` (default `account`),
`--agent CLI` (which CLI it authenticates; default claude), `--label`,
`--key`, `--model`, `--base-url`, `--config-dir`. Per-session selection is
`mindflock new --account ID`, the New dialog's Account select, or the pane
header's `@account` chip (which hot-swaps a live session).

### `mindflock mcp [--scope readonly|children|all] [--host H] [--port P] [--print-config]`

Runs the **MindFlock MCP server** in the foreground, speaking the Model
Context Protocol on stdin/stdout. That lets an agent CLI list, read, message,
spawn and steer MindFlock sessions (see [mcp.md](mcp.md)). It is meant to be
started *by* an MCP client, not typed at a prompt. It writes nothing but
protocol to stdout (logs go to stderr; `MINDFLOCK_MCP_LOG=DEBUG` for more),
and exits when stdin closes. MindFlock already attaches it to the Claude Code
and Codex sessions it launches, so you need this only for your own clients.

- `--scope` is how far the server may steer other sessions: `readonly`,
  `children` (the default: only sessions it, or its session, spawned and their
  descendants) or `all`. The default is `$MINDFLOCK_MCP_SCOPE`, else
  `children`.
- `--host` / `--port` pick the server, found as for the session commands
  below.
- `--print-config` prints the registration snippets for this install and
  exits:
  - a `claude mcp add mindflock --scope user -- <python> -P -m backend.mcp`
    line;
  - the same entry as `.mcp.json` / `--mcp-config` JSON;
  - a Codex `[mcp_servers.mindflock]` table with `tool_timeout_sec = 1620` and
    `startup_timeout_sec = 30`, sized for the tools' 1500 s waits.

  `--scope`, `--host` and `--port` given alongside are baked into the
  snippets' `env`.

```bash
mindflock mcp --print-config                 # paste the line for your client
mindflock mcp --print-config --scope all     # an external client allowed to manage every session
```

### `mindflock slot [--pool NAME] [--max N] [--timeout SECS] [--no-wait-memory] -- CMD [ARGS...]`

A machine-wide gate for heavy commands (`backend/slot.py`). The OOM that takes
a flock down is rarely agents *starting* — the [memory guard](session-engine.md#memory-guard)
holds those — but work inside running sessions: a repo whose pre-commit hook
runs two `pytest -n auto` suites, a documentarian that starts two more agent
CLIs and a code-graph index costs ~8 GB per commit, and four agents committing
at once is 32 GB. `mindflock slot -- CMD` runs CMD once it holds one of N
slots for the pool, so those jobs take turns.

- **N**: `--max`, else `$MINDFLOCK_HEAVY_SLOTS`, else `general.heavy_slots` in
  settings.json, else **1 below ~32 GB of RAM, 2 above** (one such commit plus
  a dozen resident agent CLIs fits a 24 GB box; two do not).
- **Pools**: `--pool` (default `heavy`) names an independent semaphore — e.g.
  `--pool gpu --max 1` for jobs that contend for something else.
- **Memory**: after taking the slot it also holds while memory reads `low` or
  `critical` (`backend.memory_pressure`), at most `--memory-timeout` seconds
  (default 600), then runs anyway with a warning — a misreading never wedges
  every commit. `--no-wait-memory` skips this.
- **`--timeout SECS`**: stop waiting for a slot after SECS and run anyway with
  a warning (default 0: wait for one).
- **While waiting** it prints ONE line to stderr, naming the holder: `mindflock
  slot: waiting for a heavy-job slot (held by session "fix-auth", pid 4242: env
  TESTMON_DATAFILE=… uv run … since 14:02:11) — it runs when one frees;
  MINDFLOCK_SLOT=0 skips the gate`. pre-commit captures hook output, so there
  the line shows when the hook finishes; `GET /api/system/slots` and
  `mindflock slot --status` show the queue live.
- **Passthrough**: stdin/stdout/stderr are CMD's own; its exit code comes back
  exactly (a signal death is re-raised as the same signal; 127 when CMD isn't
  found). SIGTERM and SIGHUP are forwarded to CMD, and SIGINT too unless the
  terminal already delivered it (Ctrl-C reaches the whole foreground group —
  forwarding it again would interrupt CMD twice).
- **No server needed**, and cheap when nobody else holds the slot (~50 ms,
  Python startup included). It is a counting semaphore over flock'd files in
  `~/.mindflock/run/slots/<pool>/` (`$MINDFLOCK_RUN_DIR` overrides the run
  dir): the kernel drops a flock when its holder dies, however it dies, so a
  crashed or SIGKILLed holder never leaks a slot. Waiters queue first come,
  first served. The wrapper process holds the slot while CMD runs as its
  child, so a daemon CMD leaves behind does not keep it.
- **Nesting**: CMD runs with `MINDFLOCK_SLOT_HELD=<pool>`; a `mindflock slot`
  inside it for the same pool runs straight through instead of waiting on its
  own parent.
- **`MINDFLOCK_SLOT=0`** bypasses the gate (CMD is exec'd directly).
- **`--status`** prints the holders and waiters and exits.

**Opting a repo in.** MindFlock never edits a repo's hooks; prefix the heavy
ones yourself. pre-commit shlex-splits `entry` and appends filenames, and a
bare `mindflock` entry fails the hook on a machine without it (or with a
MindFlock older than `slot`), so go through a probe that falls back to the
plain command:

```yaml
-   id: run_tests
    name: Run Tests
    entry: >-
        bash -c 'if mindflock slot --status >/dev/null 2>&1; then exec mindflock slot -- "$@"; else exec "$@"; fi' mindflock-slot
        env TESTMON_DATAFILE=.testmondata uv run --group test pytest tests --testmon -n auto -q
    language: system
    pass_filenames: false
```

`mindflock-slot` is the script's `$0`; everything after it — the command and
any filenames pre-commit appends — is `"$@"`, passed through verbatim (checked
against pre-commit 4.7 with spaces in arguments and filenames). The same
prefix works in a Makefile or a script. MindFlock does not set
`PYTEST_XDIST_AUTO_NUM_WORKERS` (or any other per-tool knob) in sessions: an
agent pane's shell sources your own rc files after the session env, and
changing how a repo's tests run behind its back is the repo's call — `-n auto`
inside a slot is bounded by the slot count instead.

## Session commands

All of them find the server the same way:

1. explicit `--host` / `--port` flags,
2. `MINDFLOCK_HOST` / `MINDFLOCK_PORT` environment variables,
3. probe the default `127.0.0.1:8765`.

The candidate must answer `GET /api/config` within ~1s with the MindFlock
config shape, so another service on the port isn't mistaken for a server. When
nothing is found the command prints
`no MindFlock server found — start one with `mindflock serve`` and exits 1.

**Auth.** Every HTTP request carries `Authorization: Bearer <token>` when a
token resolves: `MINDFLOCK_AUTH_TOKEN`, else `general.auth_token` from the
settings file (`MINDFLOCK_SETTINGS_FILE` honored). The token is read-only;
the CLI never mints or writes one. A 401 makes it re-read the token once (it
may have been rotated) and retry. That is what lets the session commands work
against a server with the access-token gate on. `mindflock events` (a
websocket) does not send it yet. The settings-file token goes only to a
loopback address: with `--host`/`MINDFLOCK_HOST` naming another machine, set
`MINDFLOCK_AUTH_TOKEN` to that server's token. Discovery probes without a
token and sends it only once MindFlock's own gate has answered, so another
program on the port never receives it.

A server that is up but refuses the token is reported as exactly that, not as
"no server found": the command prints `error: MindFlock server at http://…
rejected the auth token — set MINDFLOCK_AUTH_TOKEN …` and exits 1.
`mindflock accounts` stops there too rather than editing the settings file
behind the server's back (and when `settings.json` exists but won't parse it
prints `error: settings.json couldn't be read — fix or delete it` and exits 1
instead of saving defaults over it), and `mindflock uninstall` counts such a server as
running.

### `mindflock new [REPO_PATH]`

Create a session (`POST /api/instances`). `REPO_PATH` defaults to the current
directory; the title defaults to the repo basename with a `-2`/`-3`… suffix on
collision.

```bash
mindflock new                          # session on the CWD repo
mindflock new ~/code/webapp -p "fix the failing tests"
mindflock new -t hotfix --program codex
mindflock new --provision --strategy clone   # run repo setup / warm caches
```

| Flag | Meaning |
|---|---|
| `-p / --prompt` | seed prompt typed into the agent on start |
| `-t / --title` | session name (default: repo basename + suffix) |
| `--provision` | provisioned mode (repo setup commands, warm test caches) |
| `--strategy worktree\|clone` | workspace strategy for `--provision` |
| `--program` | agent program (default: the server's default provider) |
| `--account` | auth profile the session runs under (`mindflock accounts`; `default` = the CLI's own login) |

The server creates the workspace in the background; `new` polls for up to ~15s
and reports `ready (status: …)`, a start failure, or "still provisioning".

### `mindflock ls`

List sessions as a fixed-width table — `TITLE  REPO  STATUS  ACTIVITY  STAGE
DIFF  COST`. `REPO` is the session's source repository (its basename), so a
flock spanning several repos is legible at a glance. `DIFF` shows `+n −m` when
the server reports a `diff_stat`; `COST` shows the estimated USD spend when
token pricing is available (both blank otherwise). `--json` dumps the raw
`/api/instances` payload for scripting.

### `mindflock attach TITLE`

Replace the current process with `tmux attach-session -t <session>` for the
session's tmux (the `tmux_name` field of `/api/instances`, i.e.
`mindflock_<title>`), landing you in the agent's live terminal. `TITLE` may be
any unambiguous prefix; ambiguous prefixes list the candidates. Detach with the
normal tmux `Ctrl-b d`.

A sleeping session is woken first (`POST /api/instances/{title}/wake`), so
there is an agent to land in, back in its conversation.

`attach` requires a real terminal: when stdout isn't a TTY (a pipe, a script,
CI) it exits 1 with ``attach needs a real terminal (running in a script? use
`mindflock ls --json`)`` instead of letting tmux fail cryptically.

### `mindflock close TITLE... [--yes]` / `mindflock delete TITLE... [--yes]`

**Close** keeps your work: `close` calls `POST /api/instances/{title}/close`,
which stops the agent and moves the session to **Recently closed** (under the
session list in the web UI). The worktree and its branch stay on disk, so the
session can be reopened later as a fresh agent on the same files. It asks
`Close session '<title>'? The agent stops; the worktree and branch are kept
(Recently closed). [y/N]` once for all the titles unless `--yes`/`-y` is passed
(for scripts), and prints `closed session <title> (worktree and branch kept;
see Recently closed)`.

**Delete** is the destructive form: `delete` calls `POST
/api/instances/{title}/cleanup`, which deletes the worktree **and** the branch,
so unpushed work in them is gone. On a terminal it asks `Delete session
'<title>'? This stops the agent and DELETES the worktree and branch — unpushed
work is lost. [y/N]`; `--yes` skips the question. When stdin is **not** a
terminal (a pipe, a script, CI) and `--yes` is absent it refuses with exit 1
and says so, instead of guessing. If the server answers 409 because another
session still uses the same folder (a copy session, say), the refusal names
those sessions on stderr and the command exits 1; other titles on the same
command line are still deleted.

`mindflock rm TITLE...` is an alias of `close`, and `mindflock rm --wipe
TITLE...` an alias of `delete`; both keep working for existing scripts.

Each `TITLE` may be any unambiguous prefix, exactly like `attach`; every title
is resolved before anything is closed, so a typo closes nothing. An unknown title
prints ``no session named '<title>' (run `mindflock ls`)`` and exits 1;
an ambiguous prefix lists the candidates.

### `mindflock pause TITLE...` / `mindflock resume TITLE...`

**Pause** is not Close: `pause` calls `POST /api/instances/{title}/pause`,
which stops the agent and leaves the worktree and branch exactly as they are,
and the session **stays in the sidebar** (it does not move to Recently
closed). `resume` calls `POST /api/instances/{title}/resume`, which restarts
the agent in place in the same worktree. They print `paused session <title>` /
`resumed session <title>`; a title the server refuses (a peer shared session
can't be paused, say) is reported on stderr, the rest still run, and the
command exits 1. Titles resolve like `close` (unambiguous prefixes, all before
anything runs).

### `mindflock sleep TITLE...` / `mindflock wake TITLE...`

**Sleep** frees an idle agent's memory now: `sleep` calls `POST
/api/instances/{title}/sleep`, which stops the agent CLI and its MCP servers
and keeps the session, worktree and conversation — the row reads
**Sleeping** ([session-engine.md](session-engine.md#sleeping-agents)). It is
refused (stderr, exit 1, the other titles still run) while the agent is
mid-turn or on a dialog, runs a background job, has something queued or a
message waiting, belongs to a busy team run or family, can't resume, or has a
terminal attached. It prints `<title> is asleep (freed ~780 MB)`. Idle agents
also fall asleep on their own after `general.sleep_idle_minutes`. `wake` calls
`POST /api/instances/{title}/wake`, relaunching the agent into the same
conversation, and prints `woke <title>` (or `<title> was not asleep`).
Opening the session anywhere, or sending it anything, wakes it too — and
`attach` wakes a sleeper before landing in its tmux. `ls` shows a sleeper's
ACTIVITY as `sleeping`. Titles resolve like `close`.

### `mindflock open TITLE`

Open (or focus) the session's workspace in the configured IDE
(`POST /api/instances/{title}/ide` — Settings → Advanced picks the IDE).
Prefix matching works like `attach`.

### `mindflock msg TITLE TEXT`

Leave a message for a session's agent
(`POST /api/instances/{title}/messages`) from **outside the flock**
(`from: ""`). The agent sees it typed in as `[MindFlock message <id> from
outside the flock (CLI or external client) — …] <text>`, with no reply hint.
`TITLE` may be any unambiguous prefix. Several `TEXT` words are joined with
spaces, and a lone `-` reads the text from stdin.

```bash
mindflock msg orch "the staging DB is back, carry on"
mindflock msg orch-w1 --delivery now "stop: you are on the wrong branch"
git log -1 --format=%B | mindflock msg orch -
```

`--delivery` picks how it is delivered:

- `auto` (the default) types it in once the agent is stably idle;
- `inbox` only stores it, for the agent's `check_inbox`;
- `now` types it immediately, even mid-turn, but never into an open dialog,
  a usage-limit screen or a stopped agent, and not while someone is typing in
  that window.

It prints `sent <id> to <title> (<delivered|pending|held>)`, plus a `note:`
on stderr explaining a held or not-yet-typed message. Delivery rules are in
[mcp.md](mcp.md#messages).

### `mindflock inbox TITLE [--all] [--json]`

List a session's messages (`GET /api/instances/{title}/messages`)
**without marking anything read**, so a peek from the terminal never steals a
message from the agent. One line per message:

```
14:03:07  m1759600000123_42  [pending] from orch: Please also cover the empty-list case.
14:05:12  m1759600031877_43  [held] result from orch-w1: Added the endpoint; 14 tests pass
```

By default it lists unread messages (`pending` and `held`). `--all` also
lists messages already typed in or read. `--json` prints the raw response.
Prefix matching works like `attach`.

### `mindflock run TEMPLATE`

Run now from a shell: `POST /api/templates/{name}/run` starts a fresh session
from the template (any case), its prompt already sent and its autopilot depth
armed, and prints the new session's title. Handy from the system's own cron,
a CI job or a chat bot when a template's built-in schedule isn't the trigger
you want. A template with no folder, or no such template, is a one-line error
and exit 1.

### `mindflock templates [--json]`

The templates with their schedule in words, the next run (*paused* for a
paused schedule, *(skipping …)* when Skip next is set), the autopilot depth
and the last run (or why it failed / was skipped), plus which device fires
schedules when it isn't this one. The same list, with Run now, Pause and Skip
next, is the web UI's **Schedule** (top bar, `Alt+S`). `--json` prints `GET /api/templates` as is.

### `mindflock peer [status|invite|join|links|unlink|share|unshare|export|address]`

[Peer links](peer-link.md) over `/api/peer` (needs a running server; `invite`
and `join` turn peer links on):

| Command | Does |
|---|---|
| `peer status` | sandbox availability, identity fingerprint, listener, invites, links |
| `peer invite [--ttl S] [--advertise HOST] [--reach auto\|tunnel\|direct]` | prints a one-time `mfp1:…` code (≤ 10 min, single use); with a relay on, an `mfp2:…` code that goes through it. `--reach direct` names this machine (same network or tailnet only) whatever the relay; `tunnel` refuses without one. In `auto`, a tunnel that won't start gives a direct code and says so on stderr |
| `peer revoke INVITE` | cancels an unused invite (its id is in `peer status`) |
| `peer join CODE` | pairs, prints the safety number to compare with your peer. A fresh invite from someone you're already linked to reconnects that link (prints `reconnected to …`; the shared folder is kept) |
| `peer links` | links with their SAS, state and what the peer may do |
| `peer unlink LINK [--delete-files]` | stops the shared session, forgets the peer |
| `peer share LINK REPO [--branch B] [--program CLI]` | clones REPO into the link's shared folder and starts its sandboxed session |
| `peer unshare LINK [--delete-files]` | stops the shared session (keeps the folder unless told) |
| `peer export LINK TARGET_REPO peer/BRANCH` | checkpoints the shared folder and fetches it into your repo |
| `peer address LINK ADDRESS` | points a link you joined at the inviter's new address (`host:port`, or `wss://host/path` after their quick tunnel restarted — they see it in `peer status`). Simpler: have them send a fresh invite and `peer join` it |

`LINK` is a link id or a unique prefix of one.

### `mindflock join CODE`

Paste any code MindFlock hands out; it is routed by its format. One of your
devices' codes (`XXXX-XXXX`, `DEVICE CODE`, or the whole `mindflock devices
join DEVICE CODE` line) runs `devices join` — a bare code goes to the one
computer on your tailnet that can be joined, and with several it asks which
(`--yes` skips the settings question as there). Someone's peer invite
(`mfp1:`/`mfp2:`, also inside a pasted message) runs `peer join`. Anything
else exits 1 saying what the two formats look like.

### `mindflock devices [list|add|join|cancel|approve|deny|remove|leave|update|bootstrap]`

"Your devices" (Settings → Your devices in the app) over `/api/fleet`: group the
computers **you** own, found over Tailscale, so settings follow you between
them. The CLI only talks to its own server; that server does the
device-to-device part. Run these on the computer itself. A request sent from
another device with the access token is fine, but one relayed by another
MindFlock is refused.

| Command | Does |
|---|---|
| `devices` / `devices list [--json]` | your devices (`✓` this device / reachable, `-` offline, `!` reachable but in another group; `runs PR review & issues` marks the one that does — a `!` line warns when none or several do), computers asking to join and their codes, live invite codes, an outgoing request still waiting, and the other computers on your tailnet with the command that adds each. A `!` line also says when this device's key is out of date (re-join), when the access-token gate is off while serving beyond localhost (anyone who can reach this computer could then control your other devices through it), when your other devices can't reach this one (it only listens on 127.0.0.1), and when a device you just let in can't be reached from here. An offline member says why (what discovery found: connection refused, timed out, asleep with when Tailscale last saw it), and every other tailnet device that isn't a MindFlock you can join gets its own line with the reason — after a time-out, the tailnet policy lines that open the port are printed too |
| `devices add` | prints a one-time code (`XXXX-XXXX`, 10 min, single use) on stdout, and on stderr the command to run on the new computer: `mindflock devices join <this device> <code>`. On a local-only computer a `!` line on stderr says the new one can't reach it to use the code — make it reachable first |
| `devices add DEVICE [--yes]` | adds a device this one already holds an access token for (pasted under Remote control). No code needed. It first asks you to confirm, because it's the other way round from joining: DEVICE takes **this** computer's shared settings where this one has them (its own stay where this one has none); `--yes` skips the question, and answering no prints `not added` and exits 1. Refused for a device that is already one of yours. A `! settings sync:` line on stderr means the device was added but settings sync didn't start here |
| `devices bootstrap` | makes a fresh code and prints, on stdout, one line for a brand-new computer that has nothing installed: `curl -LsSf …/<ref>/install.sh \| MINDFLOCK_INSTALL_REF=<ref> sh -s -- --join '<this device> <code>'`, where `<ref>` is `v<this device's version>` (`main` from a source checkout) — so the new computer runs the same version and settings sync works at once. On stderr: the desktop-app twin (download the app, paste `<device> <code>` into Settings → Your devices → Paste a code) and how long the code lives |
| `devices bootstrap --join 'DEVICE CODE'` | the new computer's half (`install.sh --join` runs it): starts the server in the background when none is running (on `--port` / `MINDFLOCK_PORT` when given; its errors in `~/.mindflock/logs/serve.log` — never the startup banner, which carries the access token), signs this computer in to Tailscale when it isn't (prints the sign-in link and waits up to 10 min), makes it reachable when it only listens on 127.0.0.1 (what Settings → Your devices → Make reachable saves: Tailscale mode and the access gate on together, then the server's own restart; `mindflock token` shows the access token), sweeps the tailnet, and joins DEVICE with CODE. When the code expired while the computer was being set up, it asks DEVICE instead and waits for the approval there (compare the 6-digit code) |
| `devices join DEVICE CODE [--yes]` | joins DEVICE's group with a code made there. It first asks you to confirm, because this computer takes DEVICE's shared settings wherever DEVICE has them (your own stay where it has none); `--yes` skips the question |
| `devices join DEVICE [--yes]` | asks DEVICE (after the same confirmation), prints `Approve on <host>; check it shows code 123 456`, then waits up to 10 min for an answer (it polls every 2 s). Ctrl-C withdraws the request, on DEVICE too. Run it again while the request is still out and it picks the wait back up |
| `devices cancel` | stops asking to join, and withdraws the request on the other device. Once that device has approved and the join is under way it's too late; the command says so and exits 1 |
| `devices approve DEVICE\|ID [--yes]` | lets a computer that asked join — one that asked this computer, or another of your devices (the list says which it asked; the answer goes there, with the code). First it shows the 6-digit code and asks you to confirm that the other screen shows the same one. A `! settings sync:` line on stderr means it joined but settings sync didn't start here |
| `devices deny DEVICE\|ID` | refuses it |
| `devices remove DEVICE [--yes] [--keep-tokens]` | takes a device out. The rest get a new key; any that were offline get it automatically when they're back (they are listed). By default every device's own access token is replaced too, so tokens the removed device collected stop working: phones signed in before scan the QR again, and other places that hold a token need the new one. A device whose token couldn't be replaced (an offline one included) is listed on stderr — replace it there in Settings → Security. `--keep-tokens` only re-keys the group. If DEVICE ran PR review and issue handling, they move to this computer. The question and the output also say: if the device was lost or stolen, remove it from your tailnet in the Tailscale admin console too — that cuts it off everywhere at once, even from devices that are offline now. Naming **this** computer is `devices leave` |
| `devices update [--tag vX.Y.Z] [--yes]` | updates MindFlock on every one of your devices to the newest release (or `--tag`, a published release), **one at a time, this computer last** (`POST /api/fleet/update`). It prints each device's progress as it goes and moves to the next device only once that one's own server answers on the new version; it stops at the first device that fails, rolls back or doesn't come back (exit 1, with why). Devices that are offline, already current, a development checkout, not installed by install.sh, or too old to be updated remotely are skipped with the reason. A desktop app on a device updates itself on its next launch |
| `devices leave [--yes]` | takes **this** computer out. If it ran PR review and issue handling for your devices, they move first to the remaining device with the lowest device key. Settings sync stops here, and on its own this computer runs PR review and issue handling itself again. Leaving changes neither your devices' shared key nor any access token — remove this computer from another of your devices for that. A computer that left can join again through any of your devices |

`DEVICE` is the device name (its MagicDNS label) or the host name shown in the
list (case-insensitive). For `approve`/`deny`, a request id or a unique prefix
of one also works. Joining turns on remote control and settings sync on the
new computer. The device you joined leads the first sync: this computer takes
its shared settings wherever it has them, and keeps its own where it has none.
PR review and issue handling run on one of your devices (`runs PR review &
issues` in the list; the choice is the synced `github.automation_device`).
Joining keeps an existing choice; when there is none, they run on the joining
computer if it already runs them, else on the device it joined. To move them,
choose **Run here** in Settings → Your devices on the computer that should run
them.

### `mindflock events [--follow]`

Print the server's session-event stream (`WS /api/events`), one line per
envelope:

```
14:03:07  session.status_changed  fix-auth  loading -> running
14:05:12  session.activity_changed  fix-auth  working -> clarify
```

Without `--follow` it prints the backlog (the last ~100 events) and exits;
with `--follow` it keeps streaming until Ctrl-C. A stream that ends because the
peer/server closed the connection (e.g. the server restarts) is a **clean exit
0**, just like Ctrl-C — not a traceback. Handy for debugging shell hooks (see
[extensions.md](extensions.md)) — the same envelopes hooks receive on stdin.
Requires the `web` dependency group for the websocket client; everything else
in the CLI is stdlik-only. If it's missing, the command tells you to reinstall
with the web extra:

```bash
uv tool install --force "mindflock[web] @ git+https://github.com/MindFlock/MindFlock"
# (or, in a source checkout:  uv sync --group web)
```
