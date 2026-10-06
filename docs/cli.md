# The `mindflock` CLI

The console entry point (`backend/cli.py`) has two kinds of commands. **Host
commands** run things locally: `serve`, `doctor`, `uninstall`, and `mcp`, the
MindFlock MCP stdio server. **Session commands** (`new`, `ls`, `attach`, `rm`,
`open`, `events`, `msg`, `inbox`) are thin clients over a *running* server's
HTTP API (`backend/client.py`). Session commands never spawn an engine of
their own: the terminal and the web UI drive the same server, so a session
created from either shows up in both.

## Host commands

```bash
mindflock serve             # start the web UI (localhost only, port 8765)
mindflock serve tailscale   # bind 0.0.0.0 for phone/tailnet access (URL + QR + token)
mindflock serve --port 9000 # custom port
mindflock doctor            # dependency preflight; exit 1 if a required dep is missing
mindflock doctor --fix      # offer to install/repair missing dependencies interactively
mindflock uninstall         # undo MindFlock's writes to your repos (see below)
mindflock mcp               # the MindFlock MCP server on stdio (for an agent CLI to start)
mindflock mcp --print-config  # how to register it with your own Claude Code / Codex
mindflock --version         # print the installed version
```

`serve tailscale` binds all interfaces (0.0.0.0) — the port is reachable from
your LAN as well as your tailnet — and auto-enables the access-token gate:
unauthenticated clients get 401, and the token + QR code are printed in the
startup banner. The default `serve` (local) binds 127.0.0.1 only.

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
behind the server's back, and `mindflock uninstall` counts such a server as
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

`attach` requires a real terminal: when stdout isn't a TTY (a pipe, a script,
CI) it exits 1 with ``attach needs a real terminal (running in a script? use
`mindflock ls --json`)`` instead of letting tmux fail cryptically.

### `mindflock rm TITLE [--yes]`

End a session on the running server (`DELETE /api/instances/{title}`). The
**worktree stays on disk** — recover or wipe it from Recently closed, under the session list in the web
UI. Prompts `End session '<title>'? Its worktree is kept. [y/N]` unless
`--yes`/`-y` is passed (for scripts). `TITLE` may be any unambiguous prefix,
exactly like `attach`; an unknown title prints ``no session named '<title>'
(run `mindflock ls`)`` and exits 1.

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
