# Configuration

MindFlock reads configuration from four places:

1. **`config.toml`** (repo root, gitignored) — the ingestion pipeline and workspace-
   mode. Contains secrets; never commit it.
2. **`.mindflock.toml`** (in each *managed* repo, **committed**) — per-repo
   worktree setup + verification gate. No secrets; shared with the team.
3. **`~/.mindflock/`** — engine config and session state (JSON).
4. **`~/.mindflock-assistant/`** — provider definitions and small persisted UI/agent
   settings.

Plus a handful of environment variables.

## `.mindflock.toml` — per-repo workspace config

Commit this file to the root of a repo you run sessions on. It is read from
the source repo at session-create time and from the worktree afterwards
(re-run / check endpoints). Everything is opt-in — a repo without the file
behaves exactly as before.

```toml
[workspace]
# O2: run in every fresh worktree, in order, before queued prompts are
# delivered (a failed setup HOLDS the prompts instead of losing them; the
# session chip shows "setting up" / "setup ✗" with a re-run action).
# Output: .mindflock_setup.log in the worktree (git-excluded).
setup_commands = ["npm install"]

# O2: untracked files/dirs copied from the source repo into each new
# worktree before setup runs (never overwrites, skips missing).
copy_untracked = [".env", ".env.local"]

# O3: the verification gate. Auto-runs after each commit (when the session
# reaches the committed stage); its ✓/✗ chip shows on the session card and
# Push is soft-gated (409 + "push anyway" override) until it passes for the
# current HEAD. Output: .mindflock_check.log.
check_command = "npm test"

# Verify: opt this repo in to automatic checklists. The first push of each
# session branch gets a checklist written from its diff, which turns up in
# Verify (Alt+V) once that commit reaches the repo's live branch. See
# web-ui.md#verify.
#
# This is the team-wide, committed half of the opt-in, and the ONLY one
# available to a checkout with no GitHub origin — the other half is
# `repository.verify_repos` in settings, which lists repos by `owner/name`.
# The two are OR'd: neither can switch the other off. Must be a real boolean;
# anything else is ignored rather than guessed at.
verify_on_push = true
```

Verify's other per-repo settings are *not* here: they are keyed by GitHub slug
in `repository.verify_repo_settings["owner/name"]` and edited in Verify →
**Sources**, on each repo's card.

| Key | What it decides |
| --- | --- |
| `live_branch` | Which branch counts as shipped for this repo. A checklist goes due when its commit lands on it. Resolves first-non-empty through this override → `repository.live_branch` → `pr_base_branch` → `base_branch` → `main`. |
| `deploy_delay_minutes` | How long after merging the change is actually running. A checklist waits this long before it turns up to be checked — merged is not deployed, and checking too early records a failure against code that is fine. `0` when merging *is* shipping. Blank inherits the flock-wide default (5). |
| `target` | Where this repo's running product is — a URL, plus whatever is needed to reach it. **This is the key that decides what "it works" is checked against.** With it set, both the checklist and the agent that works it are aimed at that deployment. Blank is a real answer, not a missing one: a library or a CLI has no environment to point at, and its checklist is worked against a fresh checkout of the live branch on this machine. |
| `prompt` | Standing instructions for this repo — where the app runs, what to always check, what to ignore. Folded into **both** the prompt that writes a checklist and the one the agent runs it with. Steers *what* gets tested; it can never change the format either model must answer in. |

Each session also gets a **port block** (O4): 10 consecutive ports starting
at a deterministic base (persisted in `~/.mindflock/ports.json`), exported
as `PORT` / `MINDFLOCK_PORT_BASE` / `MINDFLOCK_PORT_COUNT` into both the
agent's tmux session and the environment `setup_commands` / `check_command`
run in. Point your dev server at `$PORT` and the sidebar's "Open preview ↗"
action opens it.

## `config.toml`

Search order (used by provisioned mode and the web UI): `$MINDFLOCK_CONFIG` →
`./config.toml` → the MindFlock repo root. The pipeline itself always reads
`./config.toml` from its working directory.

```toml
# --- Ticketing (generic) --------------------------------------------------- #
# The preferred, provider-agnostic schema. `provider` selects the source; the
# other keys are the credentials/scope that provider needs (see the per-provider
# table below).
[ticketing]
provider = "jira"               # shortcut | jira | linear | github_issues | asana
api_token = "…"                 # secret — the provider's token/API key/PAT
base_url = "https://you.atlassian.net"   # Jira only — your site URL
email = "you@company.com"       # Jira only — account email (basic auth with api_token)
member_id = "…"                 # "assigned to me" identity; auto-filled by Test in the UI
project = "owner/repo"          # scope: GitHub owner/repo, Asana workspace gid, ...
poll_interval_seconds = 20      # optional, default 20 — poll cadence
workflow_state_id = 500000007   # Shortcut only — restrict the story search to one state

# --- Multiple ticketing sources -------------------------------------------- #
# Connect several sources at once — different providers AND several of the same
# provider with different credentials — with an array of [[ticketing.source]].
# Each source is polled independently on its own cadence. `id` is an optional
# stable discriminator (also the branch/slug prefix + poll checkpoint key); it's
# auto-assigned when omitted (jira, jira-2, …). Use EITHER the single [ticketing]
# above OR this array, not both.
# [[ticketing.source]]
# id = "shortcut"
# provider = "shortcut"
# api_token = "…"
# member_id = "…"
# agent = "claude"             # which coding CLI this queue's tickets run on
# effort = "xhigh"             # how hard it thinks about them — see below
#
# [[ticketing.source]]
# id = "jira-eu"
# label = "Jira – EU"           # optional display name in the UI / Connections
# provider = "jira"
# base_url = "https://eu.atlassian.net"
# email = "you@company.com"
# api_token = "…"
#
# [[ticketing.source]]
# id = "jira-us"                 # a SECOND Jira site, distinct id => no collisions
# provider = "jira"
# base_url = "https://us.atlassian.net"
# email = "you@company.com"
# api_token = "…"

# `effort` is a per-source THINKING EFFORT: one of low | medium | high | xhigh |
# max | ultra, or omitted for "whatever the CLI does on its own". It applies to
# every ticket from that source — the ones the pipeline ingests on its own and
# the ones you start by hand from Intake — and an individual ticket can still
# override it on its row.
#
# The rungs are NEUTRAL. Whichever CLI ends up running the ticket translates them
# into its own spelling (`claude --effort ultracode`, `codex -c
# model_reasoning_effort=high`, …) and CLAMPS anything above its own ceiling, so
# a source may ask for more than its CLI can give without breaking the launch. A
# rung no provider knows is refused at load with a named problem rather than
# silently ignored. Edited in Intake → Tickets, directly under Agent CLI.
#
# There is deliberately no flock-wide `effort`: how hard to think is a property
# of the work, and one global rung would quietly re-price every queue.

[repository]
# OPTIONAL fallback repo. There is no global "default repo" in the UI anymore:
# each ticketing source names its own repo (`[[ticketing.source]].repo_url` /
# the Repo URL field per source). `url` here is only used when a source omits
# its own repo_url. Ingestion refuses to start unless SOME repo is resolvable.
# url = "git@github.com:Org/repo.git"
workspace_dir = "./workspaces"        # REQUIRED — where story/PR workspaces live
git_transport = "auto"                # "auto" (default) | "ssh" | "https"

[validation]
min_description_length = 20     # REQUIRED — shorter descriptions trigger a clarification session

[logging]
log_file = "./logs/pipeline.log"  # REQUIRED
log_level = "INFO"                # REQUIRED

[workspace]                      # all optional — generic workspace setup
# Shell commands run (in order) in every freshly provisioned workspace. When
# UNSET they are auto-detected: a uv/Python project gets `uv sync --all-groups`
# (plus `uv run pre-commit install` when .pre-commit-config.yaml exists); other
# project types get none. An explicit empty list disables setup entirely.
# setup_commands = ["npm ci", "npm run build"]

# Warm cache seeds: host-side artifacts copied into each fresh workspace so
# work starts warm (a testmon DB, a build cache, a compiled-deps tarball, ...).
# Seeding NEVER overwrites a file already present in the workspace. With a
# refresh_command, a background refresher rebuilds the artifact against
# refresh_branch every refresh_interval_seconds and publishes it back to
# seed_path.
# [[workspace.cache]]
# name = "testmon"
# seed_path = "./.cache/testmondata"       # host-side warm copy
# workspace_path = ".testmondata"          # where it lands in the workspace
# refresh_enabled = true                   # default true
# refresh_branch = "main"                  # default "main"
# refresh_interval_seconds = 3600          # default 3600
# refresh_command = "uv run pytest --testmon -q"
# env = { TESTMON_ENV = "shared" }         # exported for the cache's tooling

[github]                         # whole section optional; PR flow runs only if present
repos = ["Org/repo"]             # repos scanned by the PR flow (empty — PR flow off)
base_branch = "main"             # default "main" — PRs into this base are scanned
min_age_minutes = 15             # default 15 — ignore PRs younger than this
poll_interval_seconds = 60       # default 60
enabled = true                   # default true
skip_authors = []                # review comments by these authors are ignored
token = ""                       # secret; empty → $GH_TOKEN / $GITHUB_TOKEN / `gh auth token`
# Automated issue handling (opt-in, OFF by default): new issues in issue_repos
# each get a coding session on a fresh branch. issue_repos is separate from the
# PR-review `repos`; these knobs are independent of the PR ones above.
issues_enabled = false           # default false
issue_repos = ["Org/repo"]       # ["owner/name", ...] to watch for new issues
issue_min_age_minutes = 15       # default 15
issue_poll_interval_seconds = 60 # default 60
issue_skip_authors = []          # issue authors to ignore
# Per-repo overrides of the PR-review knobs above, keyed by the "owner/name"
# slug (quoted — a slash is not a bare key). Each watched repo is its own card
# in Intake → Pull requests, so each can carry its own agent CLI, base branch,
# grace period and skip list; the flat fields above stay the DEFAULTS, inherited
# by every table that omits the key. A slug that is not in `repos` is never
# consulted. Keep these tables LAST in the section — after this header every
# bare key belongs to IT, not to [github].
[github.repo_settings."Org/repo"]
agent = "codex"                  # only this repo's reviews run on codex
base_branch = "develop"          # its PRs target develop, not `main` above
min_age_minutes = 0              # review it as soon as the PR opens
skip_authors = ["renovate[bot]"] # whose review comments to ignore here
# The issue-handling twin, keyed by a repo in `issue_repos`. Same keys minus
# `base_branch`, which issue work never honours (an issue session branches off
# the repo's own default).
[github.issue_repo_settings."Org/repo"]
agent = "aider"                  # only this repo's issues run on aider
min_age_minutes = 60             # let issues here settle for an hour

[mindflock]                        # engine routing — see WARNING below
enabled = true                   # default true — route pipeline sessions through the engine
mode = "worktree"                # "worktree" (default) or "clone"
open_cursor = true               # default false — open each provisioned workspace in Cursor
skip_permissions = true          # default true — launch claude with --dangerously-skip-permissions
```

> **WARNING — section must be named `[mindflock]`.** Both the pipeline
> (`ticket_ingestion/config.py`) and the engine (`session/provisioned.py`) read the
> engine-routing section as `raw.get("mindflock")`. A leftover section from an
> earlier project name is **silently ignored** — every key in it, including an
> `enabled = false` you meant to apply, reverts to its default. If you migrated
> an old config, rename the section.

### Ticketing providers

`[ticketing].provider` selects the source; each provider reads the subset of
`[ticketing]` keys it needs. The web Intake → **Tickets** tab renders exactly
these fields and a "Test connection" button that auto-fills `member_id`.

| Provider | Required keys | Notes |
|---|---|---|
| `github_issues` | **none** | The zero-config on-ramp, and the catalog's first entry. `api_token` falls back to the GitHub connection (`[github].token` / `$GH_TOKEN` / `gh auth token`); `project` falls back to the source's `repo_url`, then `[repository].url`, then this checkout's `origin`. No workflow states. |
| `shortcut` | `api_token`, `member_id` | `workflow_state` optional (workflow-state id; integer `workflow_state_id` also works). |
| `jira` | `base_url`, `email`, `api_token` | Jira Cloud, `assignee = currentUser()`. `member_id` (accountId) optional. `workflow_state` optional (status id → `status = <id>`). |
| `linear` | `api_token` | GraphQL `viewer.assignedIssues`. `workflow_state` optional (state id). |
| `asana` | `api_token`, `project` (workspace gid) | Tasks with `assignee = me`. No workflow states. |

Every source also takes an OPTIONAL-in-TOML-but-required-in-the-UI `repo_url`
(the repo that source's tickets clone into) — set it per source; there is no
global default repo. `workflow_state` gates ingestion so a ticket only gets a
session once it reaches the chosen state (blank = any state). The Intake →
**Tickets** tab loads the live state list per source (Shortcut/Jira/Linear).

Each source also takes an optional **`agent`** — the coding CLI its sessions run
(`claude`, `codex`, `aider`, `goose`, `opencode`, `cline`, `antigravity`, or a
provider you defined yourself). Unset falls back to `[mindflock].agent` and then
the app's default program, so omitting it changes nothing; set it to route
different queues to different CLIs (one to a hosted CLI, another to a fully local
model). An unknown name is rejected at load time with the valid list. See
[ingestion-pipeline.md](ingestion-pipeline.md#which-agent-cli-a-ticket-runs).

Every ticket becomes a branch `feature/<slug>/<name>` and a session titled
`<slug>`, where `<slug>` is source-scoped (`sc-123`, `jira-PROJ-1`,
`lin-ENG-5`, `gh-42`, `asana-<gid>`) so workspaces never collide. With multiple
sources the slug prefix is the source `id`, so two of the same provider stay
distinct (`jira-PROJ-1` vs `jira-us-PROJ-1`). Dedup state in `state.json` keys on
the slug and each source keeps its own poll checkpoint under
`last_run_timestamps`, and processed tickets are recorded under their
provider-scoped slugs (`sc-<id>`, `jira-PROJ-1`, …).

Layered/env resolution (productization path, no `config.toml` needed):
`MINDFLOCK_TICKET_PROVIDER`, `MINDFLOCK_TICKET_TOKEN`, `MINDFLOCK_TICKET_BASE_URL`,
`MINDFLOCK_TICKET_EMAIL`, `MINDFLOCK_TICKET_MEMBER_ID`, `MINDFLOCK_TICKET_PROJECT`,
`MINDFLOCK_TICKET_AGENT` override the settings store, which overrides
`[ticketing]`. `MINDFLOCK_INGESTION_AGENT` does the same for `[mindflock].agent`.

### Local model (`[local_model]`, settings store only)

Runs sessions against a model served on this machine — no subscription, no API
key, and nothing typed or edited leaves the box. Configured in the Settings →
**Local model** screen (not `config.toml`, because the screen probes your server
to list its models), stored in `~/.mindflock/settings.json`:

| Key | Values | Notes |
|---|---|---|
| `enabled` | bool | Off (the default) is an exact no-op on every launch path |
| `runtime` | `ollama` \| `lmstudio` \| `custom` | `custom` = any other OpenAI-compatible server (llama.cpp, vLLM, a LiteLLM proxy) |
| `base_url` | URL | Blank = that runtime's documented default (`:11434`, `:1234/v1`). Point it at a LAN box to share one GPU across a flock |
| `model` | string | Exactly as the server names it; MindFlock adds whichever prefix the CLI needs |

Supported by **codex**, **aider** and **goose**. Claude Code speaks only the
Anthropic API, so a session on it keeps using that — `mindflock doctor`'s
`local-model` check says so explicitly rather than letting it pass silently. See
[providers.md](providers.md#local-models-local_modelspy) for the per-CLI mappings
and where each was verified.

Local models **outrank** an auth profile. Where both would route the same CLI,
the local overlay wins on env and the profile's routing flags are dropped, so a
session configured to stay on this machine cannot be pulled off it by an account
pin.

### Auth profiles (`[auth_profiles]`, settings store only)

Which identity a session's CLI runs as — a second Claude subscription, an API
key, an OpenRouter key. Configured in Settings → **Accounts** or
`mindflock accounts`, stored in `~/.mindflock/settings.json` (mode 0600) and
masked on every API read. Full guide: [accounts.md](accounts.md).

| Key | Values | Notes |
|---|---|---|
| `default_profile` | profile id \| `default` \| `""` | The identity new sessions inherit when they pin none. `default` = the CLI's own ambient login; `""` = the same. Overridden by `$MINDFLOCK_AUTH_PROFILE` |
| `profiles[].id` | slug | Lowercase letters/digits/`-`/`_`, max 64, unique. `default` is **reserved** (it is the "no profile" sentinel) |
| `profiles[].kind` | `account` \| `api_key` \| `openrouter` | `account` = a second login of the CLI itself, isolated in its own config dir; the other two inject a key at launch |
| `profiles[].provider` | `claude` \| `codex` \| … | Which CLI the profile is for. Blank = inferred |
| `profiles[].label` | string | What the pickers and the pane chip show |
| `profiles[].api_key` | string | Key kinds only. Reads back as a mask; a PUT that sends the mask keeps the stored value |
| `profiles[].base_url` | URL | `openrouter` only; blank = OpenRouter's own endpoint |
| `profiles[].model` | string | The profile's model pin, overridable per session |
| `profiles[].config_dir` | path | `account` only; blank = `~/.mindflock/accounts/<id>` |
| `profiles[].env` | table | Raw env overrides applied to **any** CLI — the escape hatch for user-defined providers. No UI or CLI field yet: edit `settings.json` directly |

With no profiles configured every overlay is empty and every launch path is
byte-identical to before the feature existed.

Notes on individual keys:

- `workflow_state` (per source) — the state a ticket must be in to be ingested.
  In the web UI this is a live dropdown (Intake → Tickets) populated from the
  provider, so you rarely need the raw id. For Shortcut you can also list ids with
  `uv run python scripts/list_workflows.py`. The integer `workflow_state_id`
  (integer) is still honoured when `workflow_state` is empty.
- `repository.workspace_dir` — resolved relative to the config file's directory.
- `repository.git_transport` — `auto` (default), `ssh` or `https`. The
  ingestion monitors discover work by `owner/repo` **slug**, not by remote URL,
  so at some point the pipeline has to turn a slug into a clone URL. This
  setting decides which spelling it picks — and *only* that. It is not a
  rewriter:

  - `auto` — prefer your own `[repository].url`, **verbatim**, whenever it names
    the same repo. So a config repo of `git@github.com:Org/repo.git` means slugs
    for that repo clone over SSH, with your key, no HTTPS credential needed. The
    match is transport-independent (`same_repo()` in
    `backend/session/git/remote_url.py`), so `https://github.com/Org/repo` and
    `git@github.com:Org/repo.git` count as the same repo. When nothing you
    configured names the repo, it falls back to the API's HTTPS clone URL and
    then to a synthesized one.
  - `ssh` / `https` — an explicit instruction, respelling whatever URL we
    started with. Always wins over `auto`'s inference.

  Resolved in the usual layered order: `$MINDFLOCK_GIT_TRANSPORT` → the settings
  store → this key → `auto`. An unrecognised value logs a warning and degrades
  to `auto` rather than taking the pipeline down.

  **URLs you supply are used verbatim.** A remote that already exists on a repo
  is never rewritten, converted or "normalised" by MindFlock, and a `url` you
  set here is passed to git exactly as written — so git's own
  `url.<base>.insteadOf` / `pushInsteadOf` rewrites still apply and still win,
  the same way they do when you run `git clone` yourself. (Which is why an SSH
  user with an `insteadOf` rule needs nothing here at all: git rewrites our
  HTTPS URL before it dials out.) Pushing never consults this setting either —
  it is plain `git push` over the remote the repo already has.
- `github.base_branch` — also used by provisioned mode as the base branch
  worktrees fork from (default `main`).
- `github.repo_settings` / `github.issue_repo_settings` — the per-repo override
  tables, spelled as nested tables whose key is the quoted repo slug
  (`[github.repo_settings."owner/name"]`). Each block accepts `agent`,
  `min_age_minutes`, `skip_authors` and — PR review only — `base_branch`; any
  other key is dropped, and so is any key whose value is blank or empty, which
  is how a card's cleared field means "inherit the flat field above" rather than
  "set it to nothing". `0` is a real value (no grace period at all), not blank.
  Both tables go through the same normalizer the Intake tab's cards save
  through (`_repo_overrides` in `config/settings.py`, reached from the pipeline
  as `_repo_override_map`), so a hand-written table and a saved one are cleaned
  identically — and a bad slug or block is dropped rather than raising. Layering
  is per TABLE, not per key: a non-empty `repo_settings` in `settings.json`
  replaces this file's table whole (like `repos` and `skip_authors` do), because
  the card is the only editor either table has and a per-key merge would make
  "I cleared that field" indistinguishable from "I never set it".
- `[mindflock].enabled` — **default `true`, including when the whole `[mindflock]`
  section (or the entire `config.toml`) is absent.** On, each ingested ticket
  becomes a real MindFlock session: worktree + branch + seeded agent, listed in
  the app grid with the stage badge and the guided commit → push → PR bar. The
  bridge is in-process (`session_runner` imports `backend.session` directly), so
  it needs no running server and works headless; sessions land in
  `~/.mindflock/state.json` and a running server adopts them within ~4s. Set
  `false` for the standalone path instead: a detached tmux session plus an OS
  terminal tab, no app session. Also settable from Settings → **Advanced →
  Engine → Ticket sessions**, which overrides this file.
- `[mindflock].mode` — `worktree`: sessions are git worktrees off a single canonical
  blobless clone at `<workspace_dir>/_base_<repo-slug>` (fast, disk-cheap, native
  to pause/resume). `clone`: a full standalone clone per session (strongest
  isolation; preserved across pause). Anything else is a `ConfigError`.
- `[mindflock].skip_permissions` — suppresses Claude's per-folder trust prompt
  (each worktree is a new path, which would otherwise prompt every time).

## `~/.mindflock/` — engine config and state

Created on first use. All JSON is written 2-space-indented, byte-compatible with
the original Go engine.

### `config.json`

| Field | Default | Meaning |
|---|---|---|
| `default_program` | auto-detected `claude` command | Program new sessions run. Detection sources `~/.zshrc`/`~/.bashrc` and resolves aliases via `which claude`, falling back to a plain `$PATH` lookup — including if the rc-sourcing shell hangs past a 15 s timeout (`_CLAUDE_LOOKUP_TIMEOUT_SECONDS` in `config/config.py`). (At startup the server also **enriches `PATH`** from the login shell so GUI-launched backends see the same CLIs the terminal does — see the `MINDFLOCK_NO_PATH_ENRICH` env var below and [architecture.md](architecture.md).) |
| `auto_yes` | `false` | Reserved; currently forced off for all instances. |
| `daemon_poll_interval` | `1000` | Reserved (TUI daemon heritage). |
| `branch_prefix` | `<username>/` (or `session/`) | Prefix for default session branches. |
| `profiles` | omitted | Optional named `{name, program}` presets. |

### `state.json`

`{"help_screens_seen": <bitmask>, "instances": [...]}` — sessions are embedded
here (there is no separate `instances.json`). Managed by the engine; the web
server merges on save so multiple processes can share it.

### Other files

- `worktrees/` — worktree-mode session directories,
  `<sanitized-branch>_<hex-timestamp>`.
- `recently_closed.json` — closed-but-reopenable sessions (cap 50).
- `run/<tmux-session>.env` — the credentials one running session needs, as
  sourceable `export` lines (mode 0600, dir 0700). Written at launch, replaced
  on every relaunch, removed when the session closes; absent for a session with
  no credentials. Exists so a key never lands in `/proc/<pid>/cmdline`. Override
  the directory with `$MINDFLOCK_RUN_DIR`.
- `accounts/<id>/` — an `account`-kind auth profile's isolated CLI config dir
  (mode 0700), created on save and pointed at by `CLAUDE_CONFIG_DIR` /
  `CODEX_HOME` when a session runs under that profile. **Holds real
  credentials** — the CLI's own login lands here. Under the app's config dir so
  an uninstall `--purge` sweeps it with everything else; override per profile
  with `profiles[].config_dir`. See [accounts.md](accounts.md).

## `~/.mindflock-assistant/` — providers and small settings

Override the directory with `MINDFLOCK_ASSISTANT_DIR`.

| File | Purpose |
|---|---|
| `CLAUDE.md`, `todos.json` | The Assistant addon's instructions and todo list |
| `providers/*.toml` | User-defined coding-agent providers, including their launch/classify/activity and `[connect]` install-and-login hints ([providers.md](providers.md)) |
| `pricing.json` | Cached model-pricing feed (24 h TTL) |
| `usage-history.json` | Durable daily token/cost ledger |
| `scroll-speed` | Terminal wheel speed, 1–20 (also settable in the UI) |
| `.exit-markers/<session>.code` | Last exit code per session (clean-quit vs crash detection) |
| `prompts/` | Generated ticket prompts (pruned after 1 h) |

## Environment variables

| Variable | Default | Used by |
|---|---|---|
| `MINDFLOCK_CONFIG` | — | Overrides the `config.toml` search path (engine/web) |
| `MINDFLOCK_REPO_ROOT` | — | Where the web server resolves the pipeline's repo root (`config.toml`, `state.json`); unset → nearest ancestor with `config.toml` → cwd. Set it for installed (uv-tool/pipx) copies — a wrong root splits the processed-story ledger |
| `MINDFLOCK_REPO_URL` | — | Overrides `[repository].url` — the repo provisioning clones/worktrees from (engine + pipeline) |
| `MINDFLOCK_WORKSPACE_DIR` | `./workspaces` | Overrides `[repository].workspace_dir` — where per-session workspaces are created |
| `MINDFLOCK_GIT_TRANSPORT` | `auto` | Overrides `[repository].git_transport` — `auto` \| `ssh` \| `https`, the URL form used when the pipeline must build a clone URL from an `owner/repo` slug. Never affects pushing, and never rewrites a URL you configured |
| `MINDFLOCK_BASE_BRANCH` | `main` | Overrides `[github].base_branch` — the fork point for new branches |
| `MINDFLOCK_GITHUB_REPO` | — | Single-repo override for `[github].repos` (the PR monitor's `owner/name`) |
| `MINDFLOCK_INGESTION_AGENT` | — | Overrides `[mindflock].agent` — the coding CLI ingested sessions run when their source names none. The headless/CI knob for a cron pipeline that must not touch `settings.json` |
| `MINDFLOCK_TICKET_AGENT` | — | The `agent` of the single ticketing source built from the `MINDFLOCK_TICKET_*` env override |
| `SHORTCUT_API_TOKEN` | — | Fallback Shortcut API token when the Settings/ticketing store has none — used by the Settings connection test and Shortcut ingestion |
| `MINDFLOCK_IDE` | `cursor` | Editor CLI that opens workspaces (`code`, `windsurf`, …); also settable in Settings → Advanced |
| `MINDFLOCK_ASSISTANT_DIR` | `~/.mindflock-assistant` | Providers, assistant, settings files |
| `MINDFLOCK_PROVIDERS_DIR` | `$MINDFLOCK_ASSISTANT_DIR/providers` | User provider TOMLs |
| `MINDFLOCK_SCROLL_SPEED_FILE` | `$MINDFLOCK_ASSISTANT_DIR/scroll-speed` | Terminal bridge |
| `MINDFLOCK_EXIT_MARKER_DIR` | `$MINDFLOCK_ASSISTANT_DIR/.exit-markers` | Exit markers |
| `MINDFLOCK_NO_PATH_ENRICH` | unset | Set (any non-empty value) to **disable** the startup `PATH` enrichment (`backend.pathenv`) — the login-shell probe + well-known-bin-dir union that lets a GUI-launched backend find user CLIs. Left unset, enrichment runs once before serving; it only *adds* directories, so a tool already on `PATH` resolves exactly as before. See [architecture.md](architecture.md) |
| `MINDFLOCK_PATH_PROBE` | — | **Internal** reentrancy sentinel set on the shell subprocess the `PATH` probe spawns, so the probed shell doesn't recursively re-enrich. Not meant to be set by hand |
| `CS_WEB_MODE` | `local` | `run.py` — `local` binds 127.0.0.1 (and refuses non-loopback `Host` headers), `tailscale` binds 0.0.0.0 and auto-enables the auth-token gate |
| `PORT` / `UVICORN_PORT` | `8765` | Web server port |
| `CS_CURSOR_AUTOADOPT` | on | `0` starts the Cursor auto-adopt loop disabled |
| `CLAUDE_CONFIG_DIR` | — | Extra Claude config root — scanned for token usage, and probed for login evidence (`.claude.json` / `.credentials.json`) by the provider auth probe (legacy/backend-only; see [providers.md](providers.md)) |
| `GH_TOKEN` / `GITHUB_TOKEN` | — | GitHub auth fallback for the PR flow |
| `SHELL` | — | Shell used by each session's Terminal tab |
| `MINDFLOCK_AUTH` | unset | Web auth gate override — `1` forces it on, `0` off (wins over settings) |
| `MINDFLOCK_AUTH_TOKEN` | — | Web auth token; setting it enables the auth gate |
| `MINDFLOCK_AUTH_PROFILE` | unset | App-wide default **auth profile** id — the identity new sessions run under when they pin none. Wins over `auth_profiles.default_profile` in settings; `default` means the CLI's own ambient login. `GET /api/settings/auth-profiles` reports it (`default_profile_env`, `default_profile_locked`) and the Accounts screen disables its picker while it is set. See [accounts.md](accounts.md) |
| `MINDFLOCK_RUN_DIR` | `~/.mindflock/run` | Where a session's per-run credential file is written (mode 0600, removed on close) so API keys reach the CLI without passing through argv — see [accounts.md](accounts.md#where-the-credentials-go) |
| `MINDFLOCK_HOST` / `MINDFLOCK_PORT` | `127.0.0.1` / `8765` | CLI client — where to find the running server (after `--host`/`--port` flags) |
| `MINDFLOCK_WSL_DISTRO` | — (your default distro) | Pins the WSL distro used for terminal/server launches. Unset, `wsl.exe` picks the default one — which is where the Windows installer puts the CLI. `wsl -l -v` lists them |
| `MINDFLOCK_WT_COMMAND` | `wt.exe` | Windows Terminal executable used to open session terminals |
| `MINDFLOCK_TERMINAL` | — | Preferred Linux terminal emulator (else gnome-terminal/konsole/… autodetect) |
| `MINDFLOCK_REPO` | — | Electron desktop shell only (nothing in `backend/` reads it) — **developer mode**: path of a MindFlock *source checkout* (inside WSL on Windows); the shell then launches `.venv/bin/python backend/web/run.py` from it. Unset (the default), the shell launches the installed CLI instead: `mindflock serve` from the login PATH, falling back to `~/.local/bin/mindflock` |
| `MINDFLOCK_URL` | `http://localhost:8765` | Electron desktop shell — server URL the window loads (also the only origin the window may navigate to) |
| `MINDFLOCK_WSL_LOG` | `~/.mindflock/desktop-server.log` | Electron desktop shell only (nothing in `backend/` reads it) — file the auto-started server's stdout+stderr append to (inside WSL on Windows; 2 MB cap) |
| `MINDFLOCK_LOG_MAX_BYTES` | `5242880` (5 MB) | Engine/web log (`$TMPDIR/mindflock.log`, Settings → System logs) rotation cap — past it the file rotates to `<file>.1` (one backup kept); `0` disables rotation |
| `MINDFLOCK_LOG_QUIET` | unset | Web server request log — by default **every** request is logged; set `1` to drop the UI's high-frequency poll endpoints (`/api/instances`, `/api/events`, `/static/…`, `/vendor/…`, favicon), which are still logged when they error or take ≥ 1.5 s |
| `MINDFLOCK_PIPELINE_LOG_MAX_BYTES` | `51200` (50 KB) | Ingestion pipeline log (`[logging].log_file`) rotation cap — current file ≤ this plus one rollover backup |
| `MINDFLOCK_SETTINGS_FILE` | `~/.mindflock/settings.json` | Path of the web settings store (tests point it at a tmp file) |
| `MINDFLOCK_NTFY_TOPIC` | — | ntfy topic session notifications are pushed to. Setting it is an **implicit opt-in** (a headless box has no Settings screen to flip the switch in); wins over `notifications.ntfy_topic` |
| `MINDFLOCK_NTFY_SERVER` | `https://ntfy.sh` | ntfy server the pushes go to — point it at your own instance to keep session titles off the public one |
| `MINDFLOCK_NTFY_TOKEN` | — | ntfy access token, for a protected topic or an authenticated self-hosted server |
| `MINDFLOCK_NTFY_ENABLED` | unset | Master switch for the ntfy channel (`1`/`0`). Redundant when `MINDFLOCK_NTFY_TOPIC` is set, which already implies on — but not powerless against it: setting this **explicitly** wins either way, so `MINDFLOCK_NTFY_ENABLED=0` silences a box whose topic var stays exported |
| `MINDFLOCK_NTFY_CLICK_URL` | — | URL a tapped ntfy notification opens (e.g. your tailnet `/m` URL). Never put an access token in it — it is stored on the ntfy server |
| `MINDFLOCK_TEMPLATES_FILE` | `~/.mindflock/session_templates.json` | Path of the session-template store |
| `MINDFLOCK_PROMPT_QUEUE_FILE` | `~/.mindflock/prompt_queues.json` | Path of the per-session prompt-queue store (queued prompts, loop/enabled flags) |
| `MINDFLOCK_PORTS_FILE` | `~/.mindflock/ports.json` | Path of the session port-block allocation store (the O4 `PORT`/`MINDFLOCK_PORT_BASE` blocks) |
| `MINDFLOCK_WINDOW_REFRESH_FILE` | `~/.mindflock/window_refresh.json` | Path of the scheduled window-refresh keepalive's config + per-provider `last_fired` state |
| `MINDFLOCK_SESSION_NAME` | — | Read by the injected CLI hook commands at fire time to attribute activity/thread markers to a MindFlock window; unset, the hooks fall back to the live tmux `#{session_name}` |
| `MINDFLOCK_PROVIDER_BIN_<NAME>` | — | Per-provider binary override (provider name uppercased, non-alphanumerics → `_`; e.g. `MINDFLOCK_PROVIDER_BIN_CLAUDE=/opt/claude`). Wins over Settings → `coding_cli.binary_paths` and the provider TOML's `binary_path` |
| `MINDFLOCK_ACTIVITY_MARKER_DIR` | `~/.mindflock-assistant/.activity-markers` | Per-session `{state, ts}` markers the CLI activity hooks write (working/idle/clarify detection — Claude, Codex, and opt-in TOML providers; see [providers.md](providers.md)). Note: does **not** follow `MINDFLOCK_ASSISTANT_DIR` |
| `MINDFLOCK_THREAD_MARKER_DIR` | `~/.mindflock-assistant/.thread-markers` | Per-window conversation-id markers so sessions sharing a directory each resume their *own* thread. Note: does **not** follow `MINDFLOCK_ASSISTANT_DIR` |
| `CODEX_HOME` | `~/.codex` | Codex CLI data dir; usage is read from `$CODEX_HOME/sessions` |
| `ANTIGRAVITY_CLI_DIR` | `~/.gemini/antigravity-cli` | Antigravity CLI state dir (conversation DBs, usage) |
| `MINDFLOCK_CLAUDE_JSON` | `~/.claude.json` | Path of the `.claude.json` used for pre-trust seeding of workspaces |
| `MINDFLOCK_SEED_PROMPT_DIR` | `~/.mindflock-assistant/.seed-prompts` | Where generated seed prompts are written |
| `MINDFLOCK_UV_VERSION` | pinned in `install.sh` | `install.sh` only — uv version to install; overriding the pin **skips the sha256 verification** (a warning is printed) |
| `MINDFLOCK_NONINTERACTIVE` | — | `install.sh` only — set to `1` to force the read-only `mindflock doctor` report instead of the guided `--fix` prompts. The desktop app sets it for its in-window install (a GUI process has no terminal to answer prompts on) |
| `MINDFLOCK_INSTALL_SCRIPT` | bundled `install.sh` | Desktop app only — path to the installer the **Install the engine** button runs. Point it at a stub to exercise that flow without reinstalling anything |

> Naming note: launcher variables kept their historical `CS_` prefix

> **`MINDFLOCK_NTFY_*` wins over Settings → Notifications.** Each of the five
> resolves `env → settings.json → default` **per field** (the standard
> `config/settings.py` resolver: a non-empty env var short-circuits, an empty one
> counts as unset). The consequence is worth stating plainly because it is
> invisible from the UI: on a box where these are exported the Notifications
> screen still renders as editable and still saves, but the saved value has no
> effect while the env var is set — and since `GET /api/notify/ntfy` reports the
> *resolved* value, the screen shows the env value rather than what was typed.
> Precedence being per field, exporting only `MINDFLOCK_NTFY_SERVER` pins the
> server while topic and token still come from the UI. Pick one source per field.

## Web-exposed settings

Settable from the UI settings dialog (⚙) and persisted server-side:

- **IDE** (`platform.ide_command`, Settings → Advanced) — the editor CLI used to
  open workspaces (default `cursor`; e.g. `code`, `windsurf`, `zed`, or a full
  command like `flatpak run com.visualstudio.code`). Window focus/close and
  auto-adopt work best with VS Code-family editors; resolved by
  `backend/config/ide.py`.
- **IDE auto-adopt** (`/api/cursor/autoadopt`) — automatically adopt workspaces
  opened in the linked IDE as sessions (requires a VS Code-family editor).
- **Scroll speed** (`/api/scroll-speed`) — tmux wheel lines per notch, 1–20,
  applied live to all sessions.
- **Default provider** (`coding_cli.default_provider`, Settings → Agent CLI) —
  the provider new sessions launch by default. It **must reference an installed
  CLI**: the Settings picker lists only installed providers and a `POST
  /api/settings` that names an absent CLI is rejected (see
  [web-api.md](web-api.md)). If the stored default goes missing (its CLI is
  uninstalled), the Settings screen falls back to the first installed CLI and
  **persists the correction**, so the default is never a CLI that isn't there.
- **Default launch flags** (`coding_cli.default_launch_args`, Settings → Agent
  CLI / provider management) — a **provider-name-keyed map** of default flag
  strings applied to every new session of that provider (e.g.
  `{"claude": "--dangerously-skip-permissions"}`). Each value is a raw flag
  string; at session-creation time it is split into argv tokens (shell rules) and
  validated with the same guard as provider `[launch] args`. Flags are
  provider-specific — a default set for `claude` never applies to a `codex`
  session. The New-session dialog pre-fills its launch-flags field from this map
  (see [web-ui.md](web-ui.md)); an explicit per-session value (even empty) is used
  verbatim rather than re-applying the default. Persisted shapes: normally a dict
  (empty entries dropped); a **bare string** left by an older build is coerced to
  the current `default_provider` key on load (dropped if no default provider is
  set).
- **Notification rules** (`notifications.muted_rules` / `enabled_rules`,
  Settings → Notifications) — which session events notify you. Default-on rules
  are opt-*out* (their id lands in `muted_rules`), noisier ones are opt-*in*
  (`enabled_rules`), so a rule added in a later release starts in the state its
  author intended. One list governs every delivery channel.
- **ntfy push** (`notifications.ntfy_enabled` / `_server` / `_topic` / `_token` /
  `_click_url`, Settings → Notifications) — the optional server-side push channel
  that reaches a phone with no browser tab open (see
  [web-ui.md](web-ui.md#notifications-) for the UI and
  [web-api.md](web-api.md) for the endpoints). Off until configured. `_server`
  defaults to the public `https://ntfy.sh`; `_topic` is the address your phone
  subscribes to and, on a public server, the *only* thing keeping strangers out —
  so let the UI generate a random one. `_token` is a secret (masked on read, kept
  on an empty write, and dropped if `_server` is retargeted at a different host).
  `_click_url` is opened when you tap the notification; a `token=` parameter is
  stripped from it, since it is stored on the ntfy server.

The whole `notifications` group as it lands in `settings.json`:

```jsonc
"notifications": {
  "muted_rules": ["pr_closed"],          // default-on rules switched OFF
  "enabled_rules": ["session_idle"],     // default-off rules switched ON
  "ntfy_enabled": true,
  "ntfy_server": "https://ntfy.sh",      // omitted = the public default
  "ntfy_topic": "mindflock-xTPq…",       // on a public server this IS the credential
  "ntfy_token": "tk_…",                  // secret: masked on read as "•••set"
  "ntfy_click_url": "https://box.tailnet.ts.net/m"
}
```

**Absent means default, not corrupt.** Every key here is omitted from the written
file when its value is falsy (`NotificationSettings.to_dict` writes only what is
set), so a freshly-configured install shows `"notifications": {"ntfy_enabled":
true, "ntfy_topic": "…"}` and nothing else — no empty strings, no `false`. A group
missing entirely means "all defaults". Editing the file by hand works, but the
server caches the store per process, so a hand edit needs a restart to be seen.
