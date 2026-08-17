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
the escape hatch for user-defined providers the typed kinds don't know.
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
$ mindflock accounts login work                  # runs `claude /login` inside the work dir
$ mindflock accounts add or --kind openrouter --key sk-or-… --model anthropic/claude-sonnet-4.5
$ mindflock accounts                             # list; '*' marks the default
$ mindflock accounts use work                    # default for new sessions
$ mindflock new -p "fix the flaky test" --account personal
```

`mindflock accounts` goes through the running server when there is one (so the
app picks changes up immediately) and falls back to
`~/.mindflock/settings.json` otherwise. Secrets live in that file (mode 0600)
and are masked on every API read.

## Which profile a session runs under

The same tri-state as per-session launch flags:

- **unset** — the app-wide default profile (Settings → Accounts, or
  `mindflock accounts use`; env override `$MINDFLOCK_AUTH_PROFILE`);
- **`default`** — explicitly none: the CLI's own login;
- **a profile id** — pinned to that identity.

Pick it in the New dialog's Account select, per ticketing source you route to
different CLIs anyway, or per session after the fact: the pane header grows an
`@account` chip (once at least one profile exists) — click it to **hot-swap**.
A swap persists the pin and restarts the agent tmux session under the new
identity; the worktree, shell pane and diff survive. The restart takes the
CLI's own resume path, and conversations live **per account** — swapping back
to an account finds its old thread, while a first swap starts a fresh one (the
resume chain falls back to a plain start). Copies of a session inherit its
profile.

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

Per-session tokens/cost/context keep working regardless of identity: the
scanners read the CLI's transcripts across every config root, including each
account profile's dir. On top of that, **each Claude account gets its own
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
- Per-account rolling totals exist for claude accounts today; codex account
  dirs isolate the login but their usage is not yet split per account.
- Swapping an OpenRouter **model pin** on a *provisioned* session rewrites the
  workspace launcher best-effort; the env half of a swap never needs it.
