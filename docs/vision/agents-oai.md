# Any agent off rip: aider, OpenCode, Cline, Goose (2026-11-03)

Branch `emandel2630/vz-agents-oai` (vision round). The owner: *"any user should
be able to use any built-in provider off rip, not just Claude — make sure this
is so."* This share: a person whose **only** agent is aider, OpenCode, Cline or
Goose uses MindFlock end to end. (Codex/Antigravity and first-run setup are
other workers' shares.)

## How it was proven (keyless)

No API key, no real login, nothing copied from the owner's installs:

- **A mock model.** ~150 lines of stdlib Python: an OpenAI-compatible
  `/v1/models` + streaming `/v1/chat/completions`. It answers ONE tool call
  chosen by regex from the request's `tools` (write `hello.txt`; `git status`;
  the question tool; `whoami`), plain text after a tool result ("Done."), and a
  one-line subject when asked for a commit message (deliberately **without**
  the `<commit>` tags, the harder case). For aider, which sends no tools, it
  answers a whole-file edit block. Each answer is held 6 s (2 s in the first
  runs), as a real model call would be.
- **The CLIs' own user config, in a scratch HOME**, exactly what a user has
  after setting the CLI up: `~/.aider.conf.yml` (model `openai/mock`, API base,
  key); `~/.config/opencode/opencode.json` (an `@ai-sdk/openai-compatible`
  provider); `cline auth -p openai -b <mock>/v1`; `~/.config/goose/config.yaml`
  (`GOOSE_PROVIDER: openai`, `OPENAI_HOST`). A real user who points a CLI at
  their model does the same, so there is no MindFlock-side "agent env" to
  invent. (For aider and goose, Settings → Local model, the `custom` runtime, is
  a second, product-side way.)
- **A sandboxed MindFlock** (the e2e `SandboxServer` with the ux-lab tailnet
  stubs): its own port, tmux socket, HOME and settings. **Only** the four CLIs
  are on its PATH (no `claude`), plus cursor adoption, tailnet and peers off.
  The default agent is set through the product: `POST /api/settings
  {coding_cli: {default_provider}}`. Sessions are started with `POST
  /api/instances` and **no** `program`; routes with `POST /api/flights/start`
  (stops Agent → Commit → Push to a local bare remote). Dialogs are answered
  through `POST /api/instances/{t}/answer`, the way a person's click is.

## Pass/fail per agent

After the fixes below (✗→✓ marks a stop that failed before them):

| | aider 0.86.2 | OpenCode 1.17.15 | Cline 3.0.74 | Goose 1.41.0 |
|---|---|---|---|---|
| **Default agent** set via the API | ✓ | ✓ | ✗→✓ (400 "not installed") | ✗→✓ (400 "not installed") |
| **1. New session**: first-run gates pass | ✗→✓ (`.gitignore` question held the task; doc-url gate answered with `D`⏎ *as the task*) | ✓ (opens straight into the TUI) | ✗→✓ (ClinePass promo swallowed the task) | ✓ |
| task delivered (prompt queue) and done | ✓ | ✓ | ✓ | ✓ |
| reads **working** during the turn | ✗→✓ (pattern never matched) | ✓ | ✓ | ✓ |
| settles to **idle**, not stuck | ✗→✓ (an answered question read as Needs you for good) | ✓ | ✓ | ✓ |
| **2. Route** Agent → Commit → Push | ✗→✓ (Agent stop never saw it start) | ✓ done | ✓ done | ✓ done |
| commit message | aider's own commit (its model) — the Commit stop reads "done by the agent — already committed" | opencode's one-shot | cline's one-shot | ✗→✓ goose's one-shot (subject was its banner) |
| push to the remote | ✓ | ✓ | ✓ | ✓ |
| **3. MindFlock MCP** | n/a: aider has no MCP client | not attached (env-only) | not attached (env-only) | ✗→✓ attached per launch |
| **4. Needs you** on a dialog | ✓ parsed ("Create new file?"), answered via the API | ✓ (goldens) | ✓ (goldens) | ✓ (goldens) |
| **5. Claude-only features** | no "Claude" on its rows; no errors in the server log | same | same | same |

Evidence: the flights `done` with all three stops `done`; `git log` on the
bare remote shows each branch with `Add hello.txt greeting`; each pane tail and
the mock's request log were checked at every step. Run logs:
`r2-aider.out`, `route-opencode`, `route-cline`, `r3-cline.out`,
`route-goose` in the rig dir (not committed).

## What was broken, and the fixes

1. **cline and goose "not installed"** — and so impossible to make the default
   agent (`POST /api/settings` 400), red in Settings → Providers and in the
   doctor, `logins[].installed: false` in setup. `resolve_provider_binary`
   returned the *command line* (`cline -i`, `goose session`) and that went to
   `shutil.which`. Now `ProviderConfig.resolved_executable()` gives the program
   itself; `resolved_binary()` still launches the subcommand. A binary-path
   override now keeps the subcommand too (before, `/opt/goose` launched
   `goose`'s one-shot mode instead of `goose session`).
2. **aider: an answered gate was answered again.** aider prints its gates into
   the scrollback; the "Open documentation url for more info?" warning (shown
   for any model aider has no metadata for: every local or custom model) stayed
   on screen, and the trust dismisser typed `D`⏎ every few seconds into
   whatever came next. The session's task became `D`. New
   `TrustSpec.tail_lines` / `ProviderConfig.trust_tail_lines` (TOML `[classify]
   trust_tail_lines`): aider's gate counts only on the last 3 non-blank lines.
   The same check now also gates `dialog_on_screen`, so the prompt queue is not
   held by a gate that is gone.
3. **aider: Needs you for good.** `waiting_patterns` `\(Y\)es/\(N\)o` matched
   an answered question still in the scrollback. It is now anchored to the end
   of the screen (`[Yes]: ` / `[No]: `, a wrap inside allowed).
4. **aider: never "working".** The pattern was `Waiting for LLM`, but aider
   0.86.2's spinner names the model: `█░ Waiting for openai/mock`
   (`base_coder.py`). A quick turn read idle → Needs you → idle, and an Agent stop
   sat at "waiting for the agent to start". The pattern now matches the spinner
   frame plus `Waiting for <x>`.
5. **aider: its first-run `.gitignore` question held every new session's task**
   (`Add .aider* to .gitignore (recommended)?`), and its `.aider*` files would
   be committed by a Commit stop. New `ProviderConfig.git_excludes` (TOML
   `[launch] git_excludes`): `.aider*` goes into the repository's
   `info/exclude` before each launch (aider checks `git check-ignore`). The
   question is never asked and the project's `.gitignore` is untouched.
6. **cline: the "Try ClinePass" promo on first launch** ("Press Enter to open,
   any other key to close"). The typed task vanished into it (the first key
   closed it, the rest went nowhere), and Enter would open a browser. It is now a
   trust gate with a new `esc` keystroke; cline records it as shown.
7. **goose: the commit subject was goose's banner** (`__( O)> ● new session ·
   openai mock … goose is ready Add hello.txt greeting`): without `<commit>`
   tags the parser falls back to stdout, and `goose run` prints its banner
   there. The one-shot now runs `goose run -q -t` (quiet: the reply only).
8. **goose: no MindFlock MCP.** New `ProviderConfig.mcp_args` (TOML `[launch]
   mcp_args`, the peer placeholders): goose gets `--with-extension "env K=V …
   python -P -m backend.mcp"`, one more stdio extension next to the user's own.
   `GET /api/config` → `caps.agent_mcp.providers` now lists goose.
9. **Sign-in evidence** pointed only at env vars / `auth.json`, so aider with its
   key in `~/.aider.conf.yml` and opencode with a provider in `opencode.json`
   read **not signed in**, and cline and goose read *unknown*. They now name
   their real files (`~/.aider.conf.yml`, `opencode.json(c)`, cline's
   `providers.json`, goose's `config.yaml`); cline's sign-in is `cline auth`.

Tests: `tests/unit/test_byo_agent_journey.py` (all of the above), real screens
added under `tests/unit/data/dialogs/` (`aider_docgate_answered_w80`,
`aider_create_answered_w80`, `aider_working_w200`, `cline_clinepass_w120`),
and updated pins in `test_dialogs_cli.py`, `test_provider_activity_patterns.py`,
`test_commit_message.py`, `test_mcp_attach.py`, `test_doctor.py`,
`test_provider_rows.py`.

## Not fixed (open)

- **opencode and cline MindFlock MCP.** opencode only takes extra servers
  through `OPENCODE_CONFIG_CONTENT`, and cline only through
  `CLINE_MCP_SETTINGS_PATH`, which *replaces* the user's MCP settings file. The
  everyday attach (`attach_args`) is argv-only. An env-capable attach (an
  `mcp_env` beside `mcp_args`, applied as the session's `ExtraEnv` at the four
  launch sites) would cover opencode safely. Flock mode's workers request
  typed into these sessions names tools they don't have.
- **aider asks before each edit** (`Create new file?`, `Allow edits to file…?`)
  unless it runs with `--yes-always`, MindFlock's skip-permissions flag, which a
  plain session doesn't pass. Each one reads Needs you correctly and is
  answerable from the UI or phone, but an unattended route on aider stops at
  every new file. Users who want that set `--yes-always` under Settings →
  Agent CLI → default flags.
- **Setup (the vz-copilot-only worker's share):** with `[local_model]` on, aider
  and goose need no key, but "Sign in to your agent" stays to-do. Sent to that
  worker.
- The Commit and Push stops each wait out the 30 s idle settle again, so a
  three-stop route takes ~2–3 min even on a 5 s task. This is not
  provider-specific.
