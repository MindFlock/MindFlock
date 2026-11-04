# Any built-in agent, off the rip: Codex and Antigravity (2026-11-03)

Branch `emandel2630/vz-agents-big`, forked from `release/0.11.0`. The owner:
"any user should be able to use any built-in provider off rip, not just
Claude." This is the share for **OpenAI Codex CLI** (`codex` 0.164.0) and
**Google Antigravity** (`agy`; the launcher reports 1.1.10, the build it runs
says "Antigravity CLI 1.4.12"), with a sanity pass on Claude. Aider, OpenCode,
Cline and Goose are another worker's; first-run setup (the agent picker, the
doctor's sign-in checks) is `vz-copilot-only`'s.

## How it was tested

Every run used a **sandboxed MindFlock server** (a copy of
`~/MindFlock/ux-lab/sandbox.py` on top of `tests/e2e/_server.py`): scratch
`HOME`, its own port and its own tmux socket (`$TMUX` unset on every call, each
session killed by name, never `kill-server`), Cursor adoption, tailnet and
peers off, the agent's binary as the **only** agent on `PATH` (no `claude`),
`coding_cli.default_provider` set to that agent, and a local bare repository as
`origin` for the push stop. Sessions were driven through the product's own API:
`POST /api/instances` (New session with a task), `POST /fast-track
{"depth":"push"}` (Goes as far as: Push), `/send`, `/answer`, `/dialog`,
`/api/usage`.

- **Codex**: keyless. The scratch `~/.codex/config.toml` defines a custom model
  provider pointing at a ~150-line stdlib mock of the OpenAI **Responses API**
  (`/v1/responses`, streamed; also `/v1/models` and `/v1/chat/completions`).
  Codex 0.164 speaks only Responses. A prompt marker drives it: `MOCKSH: <cmd>`
  makes one shell tool call, `MOCKMCP` calls the MindFlock `whoami` tool (Codex
  sends MCP tools as a `namespace` tool, `mcp__mindflock`, and the call names
  `namespace` + `name`), `MOCKASK:` asks for an escalated command. Its login is a
  dummy API-key `auth.json`. The real install's files were never read or copied.
- **agy**: a signed-out first run in a scratch `HOME` (nothing under the real
  `~/.gemini` touched). Then the one real run the brief allowed: a sandbox whose
  scratch `HOME` has `.gemini` **symlinked** to the real one, so agy uses its own
  login file where it lives (nothing copied, nothing to rotate against). That
  run wrote what any agy run writes into `~/.gemini/antigravity-cli/`: two
  conversations; agy's own refresh of its OAuth token, in place; agy updating
  its bundled components; history and state files. It also added the throwaway
  worktree to `settings.json`'s `trustedWorkspaces`, and that one entry was
  removed afterwards (the file is otherwise as agy left it).
- **Claude**: the launch goldens (`tests/unit/test_launch_parity.py`) plus a
  stub-`claude` sandbox.

## Results

| Check | Codex 0.164 | agy 1.4 | Claude (sanity) |
|---|---|---|---|
| 1. Default agent → New session with a task: launches in the worktree | **pass** (`codex -c mcp_servers.mindflock=… "<task>"`, cwd = the worktree) | **pass** (`agy --prompt-interactive "<task>"`) | **pass** (launch unchanged, goldens green) |
| 1. Trust / first-run gates | **FAIL → fixed**: 0.164's "Trust this folder?" gate was never answered, so the session sat on Needs you. Now auto-accepted (live: clarify for one poll, then working) | **pass**: trust gate auto-accepted. Signed out: **FAIL → fixed**, it read *idle* on the login menu; now Needs you, with "Select login method:" as a parsed dialog | n/a (stub) |
| 1. Task runs; working → idle | **pass** (hello.txt written; working, then idle) | **pass** (hello.txt written; working, then idle). Its model explored a lot and needed one nudge | **pass** (idle) |
| 2. Route Agent → Commit → Push | **pass**: commit `6331505` "Add hello.txt with a greeting", from `codex exec --sandbox read-only` (the mock's one-shot answer), pushed to the bare remote, route done, hands-free | **pass**: commit `ed6a2bf` "Add greeting", from `agy --print`, pushed, route done | not run (no model) |
| 2. What the commit holds | only the task's file; `.codex/` (MindFlock's activity hooks) stays git-excluded | only the task's file | — |
| 3. MindFlock MCP attaches, agent calls whoami | **pass**: all tools listed under `mcp__mindflock`, and `whoami` returned `cx1` / program codex with no approval prompt | **not attached, by design**: agy has no per-launch MCP flag or env var, only the global `~/.gemini/config/mcp_config.json`, which would leak into the user's own agy runs. Documented in `docs/mcp.md` ("Other CLIs") | **pass** (`--mcp-config` + `--allowedTools` unchanged) |
| 4. Needs you on question / permission dialogs | **pass**: approval reads clarify and parses into Yes / Always / No; answering through `/answer` ran the command. **Fixed**: 0.164's new "Environment: local" line had taken the Reason's place | **FAIL → fixed**: 1.4's permission menu ("Run this command?") wraps its labels to column 0 at the default 80 columns and went unparsed (no buttons). Its new "Allow access to this file?" prompt read **working** (the footer keeps "esc to cancel"). Both are now parsed and read Needs you | (Claude's own suites green) |
| 5. Claude-only features degrade | **FAIL → fixed**: the usage panel always listed Claude (and agy, Aider) with zeros; Claude's windowed plan also turned the panel's wording to plan mode for a Codex API-key user. Now only the default, installed agents or agents with usage on record get a tab. Codex's own tab shows metered (API key). No prompt-hook or thread-marker errors in the server log | same panel fix; agy's live quota needs a running agy (by design) | the panel lists `claude` alone on a Claude-only machine (the other agents are not installed there) |

## Fixes (commits on this branch)

1. `8cf9d2ec`: **Codex**: `trust_patterns` for both wordings of the folder-trust
   gate, plus `CodexProvider.trust_prompt` with `accept_words` (`trust`/`yes`).
   The shared trust-cursor regex learned Codex's `›`, so a moved cursor is
   walked back before Enter. `parse_codex` skips `Environment:` and lets a
   `Reason:` line win. `/api/usage` drops a panel agent that this machine never
   had.
2. `42e739b8`: **agy signed out**: waiting patterns plus a parsed dialog for the
   login-method menu (live capture `antigravity_signin_w100`).
3. `da5fca50`: **agy 1.4 dialogs**: `option_block(flush_wraps=True)` for
   column-0 label wraps, `↑/↓` as a footer hint, the file-access prompt
   (heading `File access`, `Read: <path>` as the command), and its waiting
   pattern (live captures `antigravity_command14_w80`,
   `antigravity_file_access14_w80`).

## Open (not fixed here)

- **No default agent chosen yet ⇒ `claude`** (sent to `vz-copilot-only`, which
  owns setup). `resolve_default_program`, `doctor._default_provider_name` and
  `settings._default_provider_name` all fall back to `claude` when
  `coding_cli.default_provider` is empty, and `DefaultConfig` logs `ERROR
  failed to get claude command` on every boot of a Claude-less machine. Unless
  the first-run picker always writes the chosen agent, a Codex-only user's New
  session launches `claude`.
- **agy MCP**: needs a global config entry; out of scope (see row 3).
- **agy permissions by default**: sessions start without
  `--dangerously-skip-permissions` (as Claude's do), and agy 1.4 asks before
  almost every command, even `pwd` and `ls`, so a hands-free route needs the
  person or the skip-permissions setting. That is agy's own behaviour.
- The live agy 1.4 fixes were proven on captured screens by unit tests, not
  re-run live: a second real run would have written into `~/.gemini` again.
- Idle settle before the commit stop takes ~30–40 s ("confirming the agent is
  done") for every agent; not specific to these two.
- `docs/mcp.md` says the auto-attach pre-approves "fifteen" tools; the launch
  pre-approves 17 (stale count, predates this branch).
