# Accounts (auth profiles)

Run different sessions as different identities — a personal Claude
subscription next to a work one, or an OpenRouter key with its own model —
without logging any CLI out. An **auth profile** names one identity; every
session runs under exactly one (or under none, which is each CLI's own
ambient login — the pre-feature behaviour, and still the default).

## The three kinds

| Kind | What it is | How it reaches the CLI |
|---|---|---|
| `account` | A second login of the CLI itself, isolated in its own config dir (`~/.mindflock/accounts/<id>` unless you point it elsewhere) | claude: `CLAUDE_CONFIG_DIR`; codex: `CODEX_HOME` |
| `api_key` | A vendor API key injected at launch (metered, no subscription) | claude: `ANTHROPIC_API_KEY`; codex/aider/goose: `OPENAI_API_KEY` (+ the CLI's own model flag when a model is pinned) |
| `openrouter` | An OpenRouter key, optionally pinned to a model | claude: `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN` (OpenRouter's Anthropic-compatible endpoint); codex: its OpenAI-compatible path; aider/goose: native OpenRouter support |

Every profile can also carry raw `env` overrides, which apply to **any** CLI —
the escape hatch for user-defined providers the typed kinds don't know. There is
no field for these in the UI or the CLI yet: add them by hand to the profile's
entry in `~/.mindflock/settings.json`. They survive a UI save (the masked
values are restored on write-back).
A combination with no route (e.g. an OpenRouter profile on `cline`) is
reported out loud — the session keeps the CLI's own login and the UI/CLI says
so — rather than launched with invented env.

> **Claude account isolation on macOS** needs Claude Code ≥ 2.1.144, which
> scopes its Keychain entry per `CLAUDE_CONFIG_DIR`. Older builds share one
> Keychain item, so two subscription logins would clobber each other. Linux
> stores credentials in the config dir and isolates on every version.

## Setting it up

**Web UI** — Settings → **Accounts**: add a profile, pick the default, test an
OpenRouter key (the Test button reports the key's real spend and lists the
models it can reach, turning the model field into a picker). For an
`account`-kind profile the card shows the login command to copy into a
terminal — the CLI's own OAuth flow is interactive, so it runs there.

**CLI** — the same store, scriptable:

```console
$ mindflock accounts add work --label 'Work'     # a second Claude login
$ mindflock accounts login work                  # the CLI's own login, in that account's dir
$ mindflock accounts add or --kind openrouter --key sk-or-… --model anthropic/claude-sonnet-4.5
$ mindflock accounts                             # list; '*' marks the default
$ mindflock accounts use work                    # default for new sessions
$ mindflock new -p "fix the flaky test" --account personal
```

`mindflock accounts` goes through the running server when there is one (so the
app picks changes up immediately) and falls back to
`~/.mindflock/settings.json` otherwise. Secrets live in that file (mode 0600)
and are masked on every API read.

MindFlock's own **Assistant** window is a session too: with an app-wide default
account configured it runs as that account rather than spending the CLI's
ambient login.

## Which profile a session runs under

The same tri-state as per-session launch flags:

- **unset** — the app-wide default profile (Settings → Accounts, or
  `mindflock accounts use`). `$MINDFLOCK_AUTH_PROFILE` in the server's own
  environment overrides both, and says so: the Accounts screen reports the env
  value and disables the picker rather than letting you save a default every
  session would then ignore;
- **`default`** — explicitly none: the CLI's own login;
- **a profile id** — pinned to that identity.

Pick it in the New dialog's Account select, or per session after the fact: the
pane header grows an `@account` chip (once at least one profile exists) — click
it to **hot-swap**. Sessions started from a ticket, an issue or a PR take the
app-wide default; there is no per-ticketing-source account setting.
A swap persists the pin and restarts the agent tmux session under the new
identity; the worktree, shell pane and diff survive.

**One conversation per account, per window.** A conversation belongs to the
account that created it — its transcript lives under that identity's config dir
and the other one cannot open it. So each window keeps a thread per identity:
`backend/providers/thread_markers.py` writes `<window>@<account>.thread`
alongside the current marker (the in-session activity hook reads
`$MINDFLOCK_PROFILE_ID` to know which account it is running as), and a swap
re-points the current marker at the incoming identity's own thread before the
relaunch. Swapping back reopens the conversation you left; a first swap to an
identity has nothing to restore and starts fresh. The chip's toast says which
of the two happened. Copies of a session inherit its profile.

## Where the credentials go

A key lives in three places, all mode 0600: `~/.mindflock/settings.json` (the
store), an `account` profile's own config dir once its CLI has logged in, and —
for the moment a session runs — `~/.mindflock/run/<tmux-session>.env`.

That last one exists because of `ps`. Every launch path builds one shell string
and hands it to `tmux new-session … sh -c <string>`; a value inlined there lands
in `/proc/<pid>/cmdline`, which is world-readable, both in the tmux client's argv
and in the `env KEY=… <cmd>` child that lives as long as the session does. So
any env var whose name looks like a credential (`*KEY*`, `*TOKEN*`, `*SECRET*`,
`*PASSWORD*`, `*CREDENTIAL*`) is written to that file instead and sourced by
path; the rest keep the inline `env(1)` prefix. `tmux set-environment` is
skipped for the same values, which also means a shell pane you open later does
not carry the agent's API key. The file is removed when the session closes, and
a session with no credentials writes none at all.

This is not secrecy from root, or from anyone who can already read
`settings.json`. It closes the case that mattered: on a shared machine, another
local account could read a key straight out of the process table.

## How it reaches the launch (for the curious)

A profile resolves to a `(env, launch_args)` **overlay**
(`backend/providers/auth_profiles.py`), composed exactly like the local-model
overlay at all four launch paths. Profile env is deliberately **never baked
into** the provisioned `.mindflock_launch.sh`: it rides the tmux environment on
first start and an `export` preamble on every relaunch, which is what makes a
swap take effect with nothing but an agent restart. With no profile in play
every overlay is `({}, ())` and every launch artifact is byte-identical to
before — the golden tests pin that.

## Usage, cost, context

For **claude**, per-session tokens/cost/context keep working regardless of
identity: the scanners read its transcripts across every config root, including
each account profile's dir. On top of that, **each Claude account gets its own
rolling day/week/month/year totals** — the usage ledger attributes every
transcript to the root it lives under — shown in the cost panel's Claude tab
("By account") and served in `/api/usage` (`providers[].accounts`). OpenRouter
profiles report account-level spend live from OpenRouter itself via the Test
button (`/api/settings/test/openrouter`).

## Picking the model (OpenRouter / key accounts)

Two pickers, one catalog. The New dialog's Model field and the account chip's
Model picker are fed by the key's own live catalog — the **full** OpenRouter
list (`/api/settings/test/openrouter`) — and a pick reaches the CLI as a
per-session override of the profile's pin (`ANTHROPIC_MODEL` for claude, the
CLI's model flag elsewhere); changing it from the chip restarts the agent on
the new model. For claude the overlay also sets
`CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1`, so Claude Code's own `/model`
menu shows the gateway's **curated** top-ranked set (fetched at startup —
which every relaunch and swap is). Claude Code's own precedence applies: a
pinned `ANTHROPIC_MODEL` bypasses that menu, so leave the session on "Account
default" (no pin) when you want to drive the model from `/model` instead.
When an account is selected, the Agent picker auto-steers to a CLI the
account can route and warns instead of silently launching on the CLI's own
login.

## Limits worth knowing

- Claude Code keeps some state global (`~/.claude.json`: per-project trust,
  MCP servers), so those are shared across accounts.
- **Per-account rolling totals are claude-only.** Both CLIs' scanners read every
  account dir, so a codex session under an account profile reports its tokens,
  context and thread normally — but only claude's ledger attributes a transcript
  to the account it came from, so only claude gets the "By account" split.
- The **plan-usage pill** describes the login the *server* itself is signed in
  as. Its percentage comes from that subscription's meter, so once account
  profiles exist the dollar estimate beside it is scoped to the same identity
  rather than to every account's turns; the others are in "By account".
- Swapping an OpenRouter **model pin** on a *provisioned* session rewrites the
  workspace launcher best-effort; the env half of a swap never needs it.
- **Local models win.** If Settings → Local model is configured for the CLI a
  session runs, the local overlay takes the model flag and the profile's routing
  flags drop out. A session kept on this machine cannot be pulled off it by an
  account pin; the profile's identity env still applies.
- Removing an account that sessions are pinned to is refused (409) until you
  swap them off it, or confirm — otherwise they would come back on the CLI's own
  login with nothing said.
- Usage limits are read from the meter of whichever login the *server* sees. A
  session on an auth profile is metered on a different subscription, so its
  queue holds on the pane's own limit banner alone rather than on a reading that
  is about somebody else's quota.
