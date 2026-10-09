# Providers

Package: `backend.providers`. A **provider** encapsulates everything specific to
one coding-agent CLI: how to launch it (fresh vs resume), which exit codes mean a
clean quit, how to recognize its trust/idle/waiting prompts in a tmux pane, and
how to read its token usage. The engine's `Instance.Start` and the web server's
terminal restarts both resolve a provider from the session's program string and
drive it through this one interface, so launch behavior can't drift between them.

## Resolution

`providers.resolve(program)` returns the first registered provider whose
`matches(program)` accepts it (matching on the program's basename against each
provider's aliases). Registration order:

1. **`claude`** (`ClaudeProvider` in `claude.py`) — matches `claude` and empty
   programs. The default.
2. **Bundled providers** — `codex`, `antigravity`, `aider`, `opencode`, `cline`,
   `goose` (`BUILTIN_CONFIGS` in `config.py`). `codex` and `antigravity` get
   dedicated Python subclasses (`CodexProvider`, `AntigravityProvider`) for live
   usage/telemetry; the rest are data-only configs.
3. **User TOML providers** — one per file in the providers dir.
4. **`generic` fallback** — matches anything; runs the program bare.

`resolve()` is hot (it runs its matcher loop several times per session per
poll tick), so results are memoized per program string in a small bounded
cache, invalidated whenever the registry changes (register / rebuild).

### `normalize_program(program)`

Canonicalizes a *stored or displayed* program string to a provider **name** where
one applies — `/opt/homebrew/bin/claude` → `claude`. Detection reports an
absolute path (it shells out to `which`), and storing that verbatim leaks an
install detail into everything that shows or matches a program, most visibly the
New Session dialog, which renders any program it doesn't recognize as an extra
agent-dropdown entry. The rules:

- empty, or already a registry key → returned unchanged (whitespace stripped)
- more than one whitespace-separated token → unchanged; it's a command line, not
  a binary to identify
- already a bare basename → unchanged
- otherwise the basename is matched against each provider's aliases in
  registration order, **skipping `generic`** (the catch-all claims everything, so
  it identifies nothing), and the first match's name is returned
- a path no provider claims → unchanged, because for a custom agent the exact
  string *is* the launch command

Unlike `resolve()` it is **not memoized** and it **is registry-state dependent**:
a user TOML provider whose aliases claim a basename starts folding paths that
previously passed through, and results can change after `rebuild_registry()`.
Matcher exceptions are swallowed (one broken provider can't break normalization),
so a `matches()` implementation must stay cheap and side-effect free.

Two call sites: `config.DefaultConfig()` at write time (so a first run stores
`claude`, not the `which` output) and `GET /api/config` at read time (so an older
`config.toml` holding an absolute path is *displayed* as the provider name). The
read-time pass does not rewrite the file, so the stored launch value and the
displayed/preselected one can differ — see [web-api.md](web-api.md).

## The default: `claude`

The Claude provider launches plain `claude` (Claude Code). It:

- resumes a crashed session with `--resume <thread-id>` when this window's
  conversation id was recorded (via the activity hooks), else `--continue`;
  a failed resume is retried once, then falls back to a plain unseeded launch
  (the seed prompt is never re-sent on resume)
- owns the workspace launcher script (see
  [session-engine.md](session-engine.md)) for non-in-place sessions
- recognizes Claude Code's trust prompts ("Do you trust the files in this
  folder?" etc. — answered with Enter), its idle prompt, and its
  "waiting for you" patterns (numbered-choice cursor, permission box,
  AskUserQuestion) used for the `clarify` activity state
- reports activity from `claude agents --json` first (Claude Code ≥ 2.x —
  real-time, so it's reported with age 0 and trusted without pane
  re-verification), falling back to the per-session hook marker (below) when
  the live signal is unavailable — binary too old, no conversation id recorded
  yet, or the session not listed
- reads token usage from Claude Code transcripts (below)

## The launcher vocabulary (`LauncherSpec`, `launch_script.py`)

MindFlock starts an agent from three places — the provisioned workspace launcher
(`.mindflock_launch.sh`, written by `session.provisioned.write_launcher`), the
standalone tmux launcher used by ticket ingestion when the engine bridge is off,
and the web relaunch path — and all three need the same four CLI-specific
spellings. `BaseProvider.launcher_spec()` supplies them:

| Field | What it is | Empty means |
|---|---|---|
| `skip_perms_flag` | the CLI's skip-all-prompts flag | append nothing (goose, cline) |
| `prompt_arg` | template passing the seed prompt (`{prompt}`) | the CLI takes no prompt argument |
| `resume_flag` | how to continue the prior conversation | the CLI cannot resume; relaunch fresh |
| `resume_fallback` | retry-once-then-plain-launch on a failed resume | use the bare resume (aider) |
| `natural_codes` | exit codes meaning a deliberate quit | — |
| `command` | the real entry command when it isn't the bare binary | launch the program verbatim |

`Claude` states its historical values as data (so the generated script is
byte-identical), `GenericProvider` reads them straight from its
`ProviderConfig`, and `BaseProvider` returns a **provider-neutral** default: no
flags invented, `--continue` for resume. That neutrality matters — the previous
launcher hardcoded Claude's four spellings, so a provisioned session on any other
CLI was started with flags that CLI rejects (`aider
--dangerously-skip-permissions "<prompt>"`). This is what made ingestion
Claude-only in practice; see [ingestion-pipeline.md](ingestion-pipeline.md).

`command` covers the CLIs whose interactive entry point is a subcommand —
`goose session`, `cline -i` — or whose binary differs from its name (`antigravity`
→ `agy`). It replaces the executable token and keeps any trailing args.

### Seeding a prompt into a CLI that takes no argument

aider, opencode, cline and goose all take their first instruction
interactively, so a seeded session on one of them would start idle and the
ticket would sit unread. For those, `launch_script.seed_by_keys_function()`
emits a shell function that waits for the pane to stop changing (capped at
~60s), then pastes the prompt through a **named tmux buffer with bracketed
paste** and sends `Enter`. The bracketed paste is the important part: a
multi-line prompt sent as literal keys would have every newline read as
"submit", firing the ticket at the agent one line at a time.

Passing the prompt as argv is still strongly preferred — no race, no readiness
wait — and is what every `prompt_arg` provider does. The keystroke path is
best-effort at every step: no tmux, no `TMUX_PANE`, or a refused paste all leave
the session running with the prompt still on disk at `.mindflock_prompt.md`. It
runs **only on a first launch**, never on a resume (re-seeding a resumed session
restarts the whole ticket in a fresh thread).

## Local models (`local_models.py`)

Runs a session against a model served on this machine: no subscription, no API
key, and nothing typed or edited leaves the box. It is a **runtime overlay**, not
a provider — a local model is a property of where the CLI points, not of which
CLI it is — so the registry is untouched and user TOML providers keep working.

`local_models.launch_overlay(program)` returns `(env, launch_args)` for the
user's `[local_model]` settings, and every launch path applies it
unconditionally; `({}, ())` when the feature is off, unconfigured, or unsupported
for that CLI. Verified mappings:

| CLI | Overlay | Verified against |
|---|---|---|
| `codex` | `--oss --local-provider {ollama\|lmstudio} -m <model>` | `codex --help` |
| `aider` | `OLLAMA_API_BASE` + `ollama_chat/<model>`; `LM_STUDIO_API_BASE`/`_API_KEY` + `lm_studio/<model>` | aider's bundled `docs/llms/{ollama,lm-studio}.md` |
| `goose` | `GOOSE_PROVIDER`/`GOOSE_MODEL` (+ `OLLAMA_HOST`); no model flag | strings in the goose binary |

A `custom` runtime (llama.cpp, vLLM, a LiteLLM proxy) is driven through the
OpenAI-compatible path with a placeholder API key — those servers ignore the key,
but a client sending an empty `Bearer` header is rejected before the request is
served.

**Claude Code is deliberately unsupported**: it speaks only the Anthropic API, so
a local model needs a translating proxy. Rather than invent env for it,
`unsupported_note()` says so, and both the Local model settings screen and
`mindflock doctor` surface it — a session silently using its hosted API is the
one outcome the privacy story cannot afford to be quiet about.

## Activity signal (`activity_markers.py`)

The working/idle/clarify **activity** state shown in the UI comes, wherever
possible, from the CLI's own lifecycle hooks rather than pane scraping. The
machinery is provider-agnostic (`providers/activity_markers.py`): at every
launch, MindFlock idempotently merges hook commands into the CLI's hooks
config; each hook fires and writes a per-session `{state, ts}` JSON marker to
`<marker dir>/<session>.json`, which the web layer trusts over pane inspection.
Both halves of that path are resolved **inside the firing hook**, never baked
in at install time: the session name from `MINDFLOCK_SESSION_NAME` (falling
back to the `#{session_name}` of the hook's own pane, `-t $TMUX_PANE`; with no
pane, or a pane that is already gone, the hook writes nothing, because a bare
`display-message` answers with whichever window the user last focused), and
the directory from the firing
CLI's own environment — `MINDFLOCK_ACTIVITY_MARKER_DIR`, else the real
`~/.mindflock-assistant/.activity-markers`. The install-time alternative was a
live incident: sessions sharing a repo share one hooks file, and a sandboxed
MindFlock (a Verify run with `HOME` redirected) re-pinning that shared file
baked its sandbox path in — after which every cohabiting session's markers
went into a dead sandbox and their chips froze. Resolving at fire-time gives
each world its own markers, whoever installed last; `hook_command`'s
`marker_dir` parameter is accordingly accepted and **ignored**. Every MindFlock-written hook command carries a
`# mindflock-activity` tag so a re-install recognizes and replaces **only**
MindFlock's own entries; user-authored hooks are never touched. Hook install is
best-effort and can never break a launch. (The Claude provider re-exports these
primitives — they historically lived in `claude.py` — so existing call-sites and
tests keep working.)

**A marker has to be both fresh and current.** Age is the first gate: markers
older than 6 h are ignored outright, and a `working`/`clarify` one older than
45 s is re-verified against the live pane, while an `idle` one is trusted at any
age (a Stop hook from two hours ago on a still-running CLI is genuinely idle).
Age alone is not enough, though — nothing ever deletes a marker file, and it is
keyed by tmux session name, so a window you closed and re-opened would keep
reporting the *dead* run's state for as long as its age allowed. So the web
layer also checks the marker against the **current tmux incarnation**
(`web/core/agent_state._marker_is_current`): a marker written before the running
tmux session was created is discarded whatever state it names — a stale `idle`
would announce a turn that ended before the session existed, and a stale
`working` would paint a freshly relaunched CLI as busy and stamp it with work
evidence it never earned. Unknowable inputs still trust the CLI: no creation
stamp, or an unreadable marker age, and the marker stands. Claude's live
`claude agents --json` path reports age `0.0` — it is real-time by construction
— so it always passes.

**`reports_activity()` is the capability question**, distinct from any one
reading: whether this CLI can announce its own state at all (hooks installed,
or a live query) — not whether a marker happens to be fresh right now.
Returning True is a promise the web layer leans on: a turn-end *announcement*
for such a CLI is never built out of pane guesswork alone (see the arming
ladder in [web-api.md](web-api.md)). Claude returns True outright;
`GenericProvider` derives it from whether the TOML declares an
`activity.hooks_file`; the base default is False (pane inspection only).

Two built-in wirings:

- **Claude** installs into the worktree's `.claude/settings.local.json`; the
  marker is Claude's fallback behind the live `claude agents --json` signal
  (above).
- **Codex** installs into the repo-local `.codex/hooks.json` (Codex's hooks
  config shares Claude's schema and payload fields): `Stop → idle`,
  `UserPromptSubmit`/`PreToolUse`/`PostToolUse → working`, and
  `PermissionRequest → clarify`. On hook-capable codex builds this
  authoritative signal supersedes the version-fragile pane regexes, which are
  kept only as a fallback for older builds.

User TOML providers opt in with the `[activity]` table (below).

## Adding a CLI with a TOML file — no Python

Drop a file in `$MINDFLOCK_PROVIDERS_DIR` (default
`~/.mindflock-assistant/providers/`):

```toml
[provider]
name = "mycli"
program = ["mycli", "my-cli"]   # basenames that select this provider
command = "mycli"                # optional; defaults to name

[launch]
args = ["--dangerously-skip-permissions"] # saved flags, appended on every start/resume
resume_flag = "--continue"       # omit if the CLI can't resume
skip_perms_flag = "--yolo"       # appended when the session skips permissions
resume_fallback = true           # emit "<cmd> --continue || <cmd>"
effort_args = ["--brain", "{level}"]      # optional: reasoning-effort flag
effort_levels = ["low", "high"]           # the level names it accepts, cheapest first
effort_ultra_level = "galaxybrain"        # optional: flag value for the top rung
effort_keyword = "megathink"              # ...or a prompt keyword, when it has no flag

[exit]
natural_codes = [0, 130]         # clean-quit codes (no auto-resume)

[classify]
trust_patterns = ["Do you trust"]  # pane substrings that mean a trust prompt
trust_keystroke = "enter"          # enter | d_enter | y_enter
idle_pattern = "What next?"        # pane substring meaning "waiting, idle"
working_patterns = ["(?i)esc to interrupt"]  # status line of a LIVE turn

[activity]                         # opt-in: activity via the CLI's own hooks
hooks_file = ".mycli/hooks.json"   # repo-local hooks config, merged into at launch
[[activity.events]]                # hook event -> state it records
event = "Stop"
state = "idle"
[[activity.events]]
event = "UserPromptSubmit"
state = "working"

# opt-in red-zone guard + tool feed (see "Red-zone guard" below)
red_zone_guard = false             # true = MindFlock manages a HARD block for this CLI
tool_hook_events = { PreToolUse = "pre", PostToolUse = "post" }
```

**Give your CLI a `working_patterns` regex if it shows an interrupt hint.** It
is the one signal that proves a turn is live *from a single frame*, and that
makes it load-bearing in two places. It rescues extended thinking, where the
work runs server-side and the local process blocks on a network read at ~0 CPU,
looking exactly like an idle prompt. And it is the only "working" evidence
available on the **first** captured frame of a pane: the classifier no longer
assumes a never-before-seen pane is busy (see
[session-engine.md](session-engine.md#layered-activity-classification)), so a
provider with no `working_patterns` and no usage-limit banner reads `idle` on
that frame and has to wait for the next poll (~4 s) to be classified from
movement. The built-ins are all short and version-stable: `esc to interrupt`
(claude), `esc to cancel`, `esc interrupt`, `(Ctrl+C to interrupt)`, `Waiting
for LLM`.

The optional `[activity]` table opts a CLI that has its own hooks engine into
the marker mechanism above: `hooks_file` names the repo-local hooks config
(e.g. Codex's `.codex/hooks.json`) and each `[[activity.events]]` maps a hook
event to the state it records (`working`/`idle`/`clarify`). Declaring a
`hooks_file` is also what flips the provider's `reports_activity()` capability
(above). An empty/omitted
`hooks_file` means pane-inspection only (unchanged behaviour); the `[classify]`
pane patterns remain as a fallback for CLI builds without hooks.

### Red-zone guard (`red_zone_guard`, `tool_hook_events`)

(The guard covers both zone kinds — red "keep out" and green "only here".)

A **red zone** is a path glob the agent may read but must never create, modify,
delete or move (`backend/config/red_zones.py`). It is enforced by a per-tool
hook MindFlock installs into the same hooks config as the activity markers, on
the events named by `tool_hook_events` (event → phase, one of `pre` / `post` /
`fail`). The hook's whole body is `backend/providers/_tool_hook_src.py`, baked
into the `python3 -c` command as a literal so it stays stdlib-only and needs no
`backend` import at fire time. On a `pre` event it reads a **guard file** —
one per worktree root, keyed `sha1(realpath(root))`, resolved from
`MINDFLOCK_RED_ZONE_DIR` at fire time — and prints a PreToolUse `deny` for an
edit (or an MCP write / a Bash write it can spot) that lands in a zone; a Bash
`post`/`fail` stat-diff backstop catches shell edits the pre-heuristic misses.
Because Claude Code hot-reloads its hooks, a zone added mid-flight applies on
the very next tool call.

- `red_zone_guard` (bool, default `false`): whether MindFlock manages a **hard
  block** for this CLI. Only Claude sets it — its PreToolUse `deny` blocks even
  under `--dangerously-skip-permissions` and inside subagents, and MindFlock
  owns its `settings.local.json` (writing `disableAllHooks: false` so a
  project-level `true` cannot silently disarm the guard).
- `tool_hook_events` (table, `Event = "phase"`): which activity events also
  carry the guard. These events must also appear in `[[activity.events]]`.

**Green zones ("only here").** The same hook enforces the inverse: while the
worktree has a green zone, an edit to anything outside every green zone (and
not a companion — lockfiles, snapshots, tests that import the scope, the
repo's derived outputs) is denied with a scope reason that tells the agent to
finish the in-scope work and list what else it needs. The guard file (`v: 2`)
keeps green in its own keys (`green_rules`, `companions`) — `rules`, `files`,
`dirs` and `sym` stay red-only — so a hook baked by an older build can never
read a green rule as red. Every hook branch is gated on "red OR green rules",
reads are never blocked, and a failed parse never denies for green. The Bash
backstop diffs `git --no-optional-locks status` before/after each command (see
[web-api.md](web-api.md#green-zones-only-here)) and its feedback never tells
the agent to revert a file that already had changes; a `git checkout -- p` /
`git restore p` / `rm p` of a path the session's own backstop just flagged as
an exact revert is let through.

**Subagents in the feed.** Claude fires the project's PreToolUse /
PostToolUse hooks for a subagent's tool calls too, with the parent's
`session_id` plus `agent_id` and `agent_type`; the hook records them on that
call's feed record as `agent` / `agent_type`. The parent's own `Agent` /
`Task` call records its `description` (≤ 120 chars) as `desc` and its
`subagent_type` as `atype` — never the prompt. The Map draws each `agent` as a
helper bird of its own, named after that call.

**Version stamp.** The hook tag is `# mindflock-activity tool-hook
v2.<rev> <hash8>` — the guard revision (`_tool_hook_src._MF_HOOK_REV`, bumped
whenever the guard's behaviour changes) and a hash of the embedded source.
`activity_markers.hooks_armed` accepts the CURRENT hash or a NEWER revision,
so a hooks file still carrying an older guard counts as not armed and the
reconcile loop reinstalls it (Claude hot-reloads the file), while two
MindFlock builds on one worktree (the uv-tool copy next to a dev server)
converge on the newer guard instead of rewriting the file every tick. Another
build's same-revision hook is healed only at the 30 s retry pace, and a
version swap is never reported as `session.red_zone_tampered` — only a
removed or disabled hook is. The `# mindflock-activity` substring keeps
uninstall recognising every version.

**"Detect-only"** means the CLI runs the hook (its feed records light up the
Code Map and register breaches) but nothing is *blocked*. Codex is detect-only:
it ships `tool_hook_events`, but `red_zone_guard = false`, because Codex
gates repo-local hooks behind a trust hash MindFlock does not manage, so the
deny is not guaranteed to fire. Every provider without a guard is detect-only by
default (`BaseProvider.red_zone_guard()` → `False`).

### Reasoning effort (`EffortSpec`, `providers/effort.py`)

Every modern coding CLI can be told to think harder, and no two spell it the
same way — `claude --effort xhigh`, `codex -c model_reasoning_effort=high`,
`agy --effort high` — while aider, goose, cline and opencode cannot do it at
all. So MindFlock offers **one neutral ladder** and each provider translates:

| Rung | `low` | `medium` | `high` | `xhigh` | `max` | `ultra` |
|---|---|---|---|---|---|---|
| claude | ✓ | ✓ | ✓ | ✓ | ✓ | `--effort ultracode` |
| codex | ✓ | ✓ | ✓ | ✓ | → `xhigh` | → `xhigh` |
| antigravity | ✓ | ✓ | ✓ | → `high` | → `high` | → `high` |
| aider / goose / cline / opencode | — | — | — | — | — | — |

Two rules make this safe. A rung **above** a CLI's ceiling *clamps* to its top
rung instead of being forwarded: claude warns and silently runs at its default
for an unknown level, and codex forwards the string to the API, which rejects it
— so "as hard as this CLI goes" is the only useful reading. And `ultra` is
whatever that CLI calls its beyond-the-ladder mode, not a sixth rung: Claude Code
takes `ultracode` on the same `--effort` flag (xhigh effort plus standing
multi-agent orchestration — note it is *beside* `max`, not above it), so the top
rung asks for it by name and holds for the whole session. A CLI that recognises
such a mode only as a word in the prompt gets that **keyword** appended to the
seed prompt after a rule instead; it is one or the other, never both.

A CLI that cannot do it says so rather than pretending: the plan carries a note
("aider has no effort setting — started at its own default"), `/api/providers`
publishes each CLI's real rungs so the picker can disable the control, and
`codex`'s `minimal` is deliberately off the ladder (the API refuses it while
codex's default web_search tool is on).

Requests come from the per-item **Effort** picker on the Intake work rows; the
resolved flags become the session's launch args, so a relaunch or a
reboot-resume keeps the effort.

### Launch args vs. `skip_perms_flag`

`[launch] args` are **saved launch flags** — argv tokens appended to the base
executable every time this provider starts *or* resumes a session, shell-quoted
(`shlex.quote`) as they are interpolated into the tmux command, and validated on
load (a list of non-empty tokens; no newlines/NULs; ≤512 chars each) so a bad
persisted provider never reaches command construction. They are **provider-
specific** — the same list lives on one provider's config and never leaks onto
another CLI — and they **precede per-session flags** (see
[session-engine.md](session-engine.md)) in the final command. This differs from
`skip_perms_flag`, which is a *single* flag added **only when a session opts into
skipping permissions**; `args` are unconditional. There are two further layers of
launch flags — a global per-provider default map
(`coding_cli.default_launch_args`, see [configuration.md](configuration.md)) and
per-session flags — both threaded through to the same command builder.

Malformed files are skipped silently, so a bad provider can never break startup.
The bundled configs (`codex`, `antigravity`, `aider`, `opencode`, `cline`,
`goose`) use the same mechanism; their flags are verified against pinned CLI
versions noted in `config.py` (upstream CLIs change flags), and a user TOML with
the same name overrides the bundled config.

### MindFlock MCP attach (`mcp_launch_args`)

`BaseProvider.mcp_launch_args(spec)` returns the argv tokens that attach the
MindFlock MCP server to a launch. `spec` is a
`providers.mcp_attach.McpSpec`: interpreter, `-P -m backend.mcp`, the env with
the session's title, the server port and the scope. The default is `()`, no
auto-attach. Two providers override it:

- **`claude`** writes a 0600 run file and returns
  `--mcp-config=<file>` and `--allowedTools=<the ten read, wait and report tools>`,
  both in the single-token form, since a spaced value would swallow the seed
  prompt.
- **`codex`** returns `-c mcp_servers.mindflock={…}`, one TOML inline table.

The launch sites prepend these tokens to the effective launch args. They
never go into `build_launch_command` or `write_launcher` themselves, so the
golden launcher scripts don't change. They are raw argv elements: every
launch path shell-quotes launch args itself. A provisioned launcher bakes them
in when it is written, so a relaunch from the web UI compares the baked tokens
with what `mcp_launch_args` returns now (`mcp_attach.launcher_attach_stale`)
and rewrites the launcher first when they differ. A TOML-only provider has no
hook and is not attached; its users register `mindflock mcp` with that CLI by
hand. See [mcp.md](mcp.md#how-it-gets-attached).

Each launch site also records whether that launch carried the tokens
(`mcp_attach.note_launch`), which is the session row's `mcp_attached`.

### Peer shared sessions (`sandbox_profile`, `peer_mcp_args`)

A CLI can run a peer shared session ([peer-link.md](peer-link.md)) when its
provider says what it needs inside the bubblewrap sandbox —
`sandbox_profile()` → `SandboxProfile` (binary, API hosts, config dir, login
files to copy, fixed env) — and how the peer-mode MCP attaches —
`peer_mcp_args(spec)` / `peer_mcp_env(spec)` with a `mcp_attach.PeerMcpSpec`
(command, args, the only env the server may get, a read-only run file
writer). `claude` and `codex` implement these in Python; the rest are data. A
TOML provider opts in with a `[peer]` table (the sandbox validates every value
again):

```toml
[peer]
egress = ["api.mycli.dev"]               # hosts it needs on 443
config_dir = ".mycli"                    # its config dir under ~
config_env = "MYCLI_HOME"                # …and the env var that moves it
seed_files = ["auth.json"]               # login files under that dir
env = { MYCLI_NO_UPDATE = "1" }          # fixed env inside
# How the MCP attaches — any of these; templates take {command},
# {command_json}, {args_json}, {argv_json}, {env_json}, {argv_shell},
# {env_shell}, {argv_env_shell}, {server}, {tools_csv}, {config_file}:
mcp_args = ["--mcp-config", "{config_file}"]
mcp_env = { MYCLI_MCP = "{env_json}" }
mcp_file = { name = "mycli-mcp.json", content = '{"servers":{"{server}":{"cmd":{argv_json},"env":{env_json}}}}' }
mcp_home_file = { path = ".mycli/mcp.json", content = "…" }   # only if it reads nothing else
```

Without `egress` and some way to attach, the CLI can't run a shared session
(refused before anything starts). Prefer an attach that keeps other MCP
servers — the user's, or one planted in the shared folder — from loading.

### Reading a dialog (`parse_dialog`)

`BaseProvider.parse_dialog(screen_text)` turns the visible screen of an agent
blocked in `clarify` into data for the UI's answer buttons, or returns
`None`. The default is `None` (the UI shows the question line and a button
that opens the pane). The shared pieces and both layouts live in
`providers/dialogs.py`:

- **`claude`** reads Claude Code's dialogs: a top rule, the heading (`Bash
  command`, `Edit file`, `Tool use` for an MCP tool, `Accessing workspace:`
  for the folder trust gate, plan approval), the command or path, the
  question and the `❯ 1.` options.
- **`codex`** reads the approval overlay (`Would you like to run the
  following command?` / `…make the following edits?`, an optional `Reason:`,
  the `$ command`) and the trust screen, with `› 1.` options.

A parse requires a numbered option list, numbered from 1 without gaps, with
the CLI's selection cursor on one of its lines. A numbered list the agent
merely printed never has the cursor, so it never parses. Options get a `kind`
from their label: `yes`, `always` ("don't ask again", "allow all edits
during this session", "auto-accept"), `no` or `other`. Both parsers are
pinned against golden screens in `tests/unit/data/dialogs/`, which are
checked against the same provider's `waiting_prompt_patterns` and trust
patterns so the fixtures can't drift from what the classifier knows. The
`claude2_*` screens are real Claude Code 2.1.289 captures: the 2.x Bash
dialog puts the description under the heading and the command in a dashed
box (`command` is the command, the description goes to the question); an MCP
`Tool use` call leads its `command` with the argument that names the call,
chosen by name (`title`, then `session` / `to` / `target`, else the first
argument with a value — Claude lists arguments in the order the agent wrote
them); a background sub-agent's tab header ("· from the general-purpose agent
2 of 3") becomes `source`; a side panel to the right of the dialog is cut
away. A command (or an option's path) the box hard-wrapped mid-token at the
pane's edge is joined back without a space; the command's own line breaks (a
heredoc) stay. The dialog id is built per component — each paragraph above
the options, then each option as `key:label` — with all whitespace removed;
an option label, a collapsed tool description and anything cut short with
`…` contribute only their first 24 characters before the cut
(`dialogs.ID_PREFIX_CHARS`), so the same dialog keeps one id from 47 to 200
columns (`claude2_stop_session_w*`, `claude2_spawn_w*`, plus synthetic
re-wraps checked line for line against those captures). The 2-option variant
Claude draws at ≤ 80 columns has different keys, so a different id.

Two capabilities sit on top of the parser:

- **`parses_dialogs()`** — True when the provider overrides `parse_dialog`.
  The activity layer then captures the screen once per probe and reads a
  parsed dialog as `clarify` **whatever the hook marker says** (Claude's
  background sub-agents rewrite it while one of them has a prompt up).
- **`dialog_on_screen(screen_text)`** — the evidence every automated typer
  (prompt queue, mailbox lane, `delivery: "now"`, `/send` with
  `dialog_safe`, the playbook render) checks right before typing: a parse, or
  the provider's waiting / trust phrases in the bottom 15 lines. Looser than
  the parse on purpose — a false positive only holds a message for a later
  pass, a miss types into the prompt and its Enter approves it.

Whether an agent CLI is **holding a pane** (the precondition for the mailbox
lane and `delivery: "now"` to type into it) is decided from the provider
registry too: a process in the pane's tree whose executable, or whose script
under `node`/`python`/a shell, is any provider's name, alias, `command` or
`binary_path`, or the session's own program. A custom TOML provider is
recognized without any extra configuration. The prompt queue uses a looser
test, since it types the user's own text: anything but a bare shell in the
foreground.

## Connection: install detection and sign-in

Settings → **Agent providers** surfaces a **connection** view for every
registered provider (built-in and custom): whether its binary is installed
(with the resolved path) and, when it's missing, a copy-paste install command
(see [web-ui.md](web-ui.md)). MindFlock never stores credentials. It does read
Claude Code's *existing* OAuth token read-only, solely to display live plan
usage (see **Live usage & limit state** below). Install detection is the same
`shutil.which` / explicit-path check the backend uses to gate the default
provider (below).

`BaseProvider.install_hint()` backs the install command the doctor RUNS (in the
one-shot install script and `doctor --fix`), best-effort and wrapped so one
provider can never break the list: a command that installs this CLI, or `""` to
fall back to a platform package-manager hint keyed on the program name. Each
built-in uses its vendor's own installer, never `pip` or `sudo npm`:

| CLI | install_hint |
|---|---|
| claude | `curl -fsSL https://claude.ai/install.sh \| bash` — always, even with npm on PATH (npm meant EACCES with a distro Node, and no auto-update) |
| codex | `curl -fsSL https://chatgpt.com/codex/install.sh \| CODEX_NON_INTERACTIVE=1 sh` |
| aider | `curl -LsSf https://aider.chat/install.sh \| sh` (brings its own uv; no PEP 668 failure) |
| opencode | `curl -fsSL https://opencode.ai/install \| bash` (lands in `~/.opencode/bin`) |
| goose | `curl -fsSL …/block/goose/releases/download/stable/download_cli.sh \| CONFIGURE=false bash` |
| cline | `npm install -g --prefix ~/.local cline` — npm is Cline's only channel; the doctor adds Node.js to the install plan first when npm is missing (`check_node`; a Windows npm under `/mnt/` on WSL doesn't count) |
| antigravity | none — `agy` ships with the Antigravity app |

A bash script is piped to `bash`, never `sh`: `/bin/sh` is dash on Debian,
Ubuntu and WSL, and dies parsing one. `tests/unit/test_install_hints.py` lints
every provider's hint for these rules (no pip, no sudo, the right shell per
installer, npm only into a user prefix with the Node check).
`GenericProvider` reads the hint straight from the TOML's `[connect]` table.

`BaseProvider.version_args()` (default `()` — never run with a guessed flag)
names the arguments that print the CLI's version; the doctor shows the answer
next to the path. Every built-in but antigravity declares `--version`; a custom
TOML opts in with `[connect] version_args = ["--version"]`.

**Sign-in.** `login_command()` (the command a login terminal runs; default: the
bare program) and `auth_evidence()` (a human string when the CLI *looks* logged
in, else `""`, reported as "login status unknown" rather than "logged out" so a
version-fragile credential probe never false-negatives) feed the doctor's
`agent-auth` row. When that row says no login was found and the provider
declares a login flow, Setup's *Sign in to your agent* step and the doctor row offer **Sign in to <agent>**,
which opens `WS /api/providers/{name}/login-terminal` (`web/core/provider_login.py`
— tmux, or a plain PTY when tmux is missing) in a window; closing it calls
`POST …/login-close` and re-runs the agent test and the doctor. This reverses
an earlier decision to leave sign-in to each CLI's first session: sign-in was
the last onboarding step that still needed a terminal. `doctor --fix` runs the
same command, and for a bare program (`claude`) first says how to get back
(`/exit`). The `[connect]` table's `auth_files`, `auth_env`, and
`login_command` keys configure these for a custom provider.

Custom providers configure the install hint (and the other connect fields) with
an optional `[connect]` table in their TOML (all keys optional):

```toml
[connect]
install_hint = "curl -fsSL https://example.com/install.sh | bash"  # "" -> platform package hint
version_args = ["--version"]          # the doctor shows the version ("" = no probe)
auth_files = ["~/.codex/auth.json"]   # first existing file = "looks logged in"
auth_env = ["OPENAI_API_KEY"]         # or a set API-key env var
login_command = "codex login"         # "" -> run the CLI bare
```

## Pricing (`pricing.py`)

Model prices for the UI's cost estimates, sourced from the AI Pricing Guru feed
(`https://www.aipricing.guru/api/pricing.json`, ~120 models). Degradation chain:
live feed (4 s timeout, 24 h TTL) → last-good disk cache
(`~/.mindflock-assistant/pricing.json`) → a built-in Claude fallback table →
Sonnet-class default. Cache-write price is derived as 1.25× input (the feed
doesn't carry it). Model names are normalized (case/punctuation-insensitive
longest-prefix match) so dated ids like `claude-opus-4-8-20260101` resolve.
API: `price_per_token(model)`, `context_window(model)`, `estimate_cost(tok, model)`.
**Estimates only — never billing.** Nothing in this module raises.

## Usage history (`usage_history.py`)

Rolling day/week/month/year token+cost totals across **all** Claude Code sessions
(powers `GET /api/usage` and the sidebar readout). One pass over every transcript
under `~/.claude*/projects/` (plus `$CLAUDE_CONFIG_DIR` — wrappers/alternate
installs may use separate config roots) sums each turn's incremental usage, priced per-turn by
its own model. Results are memoized for 60 s and folded into a durable daily
ledger (`~/.mindflock-assistant/usage-history.json`, atomic writes) so totals
survive Claude pruning old transcripts. The ledger self-prunes days older than
the longest rolling window (plus 5 days' slack), so long-lived installs don't
accumulate one entry per calendar day forever. Degrades to zeros on any error.

Per-session figures (the pane popup and `tokens_*` fields on
`GET /api/instances`) come from the provider's `session_tokens(workdir, since)`:
for Claude, the transcripts of the workspace's project directory — cumulative
in/out/cache tokens plus the newest turn's context-window fill and model.

## Live usage & limit state

Two provider methods drive the usage-limit hold on the prompt queue (see
[web-api.md](web-api.md) and [web-ui.md](web-ui.md)):

- `usage_live()` returns the current usage-meter reading (for Claude, the same
  OAuth endpoint as the CLI's `/usage` screen) as a dict with `percent_used`
  and an `end` reset epoch, optionally nested `weekly`. A window whose
  `percent_used` reads spent but whose `end` (its `resets_at`) is null, absent,
  or already in the past is treated as a **hold-worthy exhausted** state, not an
  open window — the queue holds on a bounded fallback rather than firing a
  prompt into a still-closed window. A window that reads open, or an
  unavailable reading (`None`), leaves the queue free to send.
- `usage_limit_state(pane_text, now)` parses the CLI's on-pane limit banner
  (`{limited, reset_at}`). The hold logic consults the live meter even when this
  reports no banner, so a rebooted session with a fresh idle prompt is still
  held when its meter shows a window genuinely spent.

### Credential sources for `usage_live()`

Claude's live reading needs the OAuth access token the Claude CLI already holds.
MindFlock reads it — read-only, never written, never logged — from, in order:

1. `$CLAUDE_CONFIG_DIR/.credentials.json`, else `~/.claude/.credentials.json`
   (Linux/WSL, and any install that keeps a file).
2. **macOS only:** the login Keychain item `Claude Code-credentials`, via
   `security find-generic-password -s "Claude Code-credentials" -w`. macOS Claude
   Code keeps its credentials here instead of in a file, so before this fallback
   live plan usage was permanently dark on every Mac (the UI fell back to the
   transcript estimate). This adds a macOS-only runtime dependency on the
   `security` binary.

Because the Keychain item was created by the Claude CLI and not by MindFlock, the
lookup can raise a one-time *"MindFlock wants to use your Keychain"* prompt from
the MindFlock (Python/Electron) binary. The call is bounded at **5 s** so an
unanswered prompt can't wedge `GET /api/usage`, and a denial — like a headless
session, a missing `security`, or a non-JSON item — simply yields no live reading,
which is exactly the pre-existing behavior. The result is **not cached**, so a
denial can re-prompt on later usage fetches. Failures are reported only as a
reason string on the `_creds_diag` debug channel; the token itself is never
logged.
