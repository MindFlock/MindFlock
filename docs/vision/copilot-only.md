# A GitHub Copilot-only machine (2026-11-03)

The question: **"Will my friend be able to use GitHub Copilot for the whole
thing with no Claude account at all on setup?"**

**Answer: yes, with this branch.** On 0.11.0 as released it is "almost":
setup offers no way to choose Copilot, and once a session runs, Copilot's
approval dialogs read as idle (they don't show as Needs you). This branch
fixes both, plus four smaller gaps. One gap stays open on Copilot's side:
its tool search sometimes doesn't surface a MindFlock MCP tool (see Open
gaps).

## How it was run

* A fresh-install sandbox server per run (`~/MindFlock/ux-lab/sandbox.py
  --fresh --no-agent`, from this branch): scratch `HOME` and settings, its own
  tmux socket, no tailnet, no Cursor adoption, every `PATH` entry holding a
  `claude` dropped, no `~/.claude*`. So this is a machine with no Claude at all.
* GitHub Copilot CLI 1.0.105 was installed by MindFlock's own setup ("Install
  GitHub Copilot", the doctor's install terminal: `npm install -g --prefix
  ~/.local @github/copilot`, exit 0 in 25 s). It was signed in two ways:
  a token in `COPILOT_GITHUB_TOKEN` (lab2), and the state `copilot login`
  leaves on a machine without a keychain (lab3/lab5: `copilot login
  --with-token`, plain-text store, with no token in the environment).
* A scratch repo with a local bare remote (push works; there is no GitHub
  remote, so Open PR can't work).
* Driven like the friend would: the UI through Playwright + Chrome (the
  committed bundle, plus a scratch build of this branch's frontend for the
  picker), and the HTTP API where scripting the UI is slow.

## Steps

| # | Step | Before (0.11.0) | After (this branch) | Evidence | Fix |
|---|---|---|---|---|---|
| 1a | First launch: can you choose Copilot? | **FAIL**: step ① says "Install Claude Code", and the checklist has no agent choice. Settings → Agents greys out every agent that isn't installed, and the server refuses an uninstalled default. | **PASS** | Shots 01, 02, 16, 17: "Your agent" lists Claude Code … GitHub Copilot; picking Copilot makes ① "Install GitHub Copilot", its button "Install GitHub Copilot", and ② "Install GitHub Copilot first (①)." | `POST /api/setup/agent` (`setup_steps.choose_agent`: any agent that is installed or installable); `AgentPicker` in step ①, which re-probes the checks after a pick |
| 1b | Doctor installs it | PASS once the default is Copilot | **PASS** | Shots 03–05: install terminal "==> agent CLI (copilot) … added 3 packages … everything installed"; ① and ② ticked | — |
| 1c | Copilot already installed, nothing chosen | **FAIL**: the default stays `claude` ("Install Claude Code", sessions launch `claude`) | **PASS**: the default is Copilot (the first installed agent when `claude` is missing) | lab3/lab5 `/api/setup`: `default_provider: copilot`, ① "Install GitHub Copilot ✓ done" | `config/program.default_provider_name` / `installed_fallback`, used by the doctor, Settings, Connections and the launch path; config.json's seeded `claude` yields the same way |
| 1d | Boot report | **FAIL**: "✗ 1. Install the tools  missing Claude Code" while Copilot was installed and the default | **PASS**: "✓ 1. Install the tools  git, tmux, GitHub Copilot / ✗ 2. Sign in to your agent  run copilot login" | lab5 `report` | `run.py` preflight enriches `PATH` before the report (it raced the server's startup) |
| 1e | Sign-in detected, no Claude wording | **FAIL**: `~/.copilot/config.json` was the sign-in evidence, but Copilot writes it on its first run signed in or not (`{"firstLaunchAt": …}`), so a signed-out Copilot read as signed in | **PASS** | Shot 10: "Sign in to GitHub Copilot" terminal running `copilot login` (browser flow). After a login, `agent-auth ok · signed in as emandel2630` and ② done; before it, "run `copilot login`" | `CopilotProvider.auth_evidence`: env token, else `loggedInUsers` / `lastLoggedInUser` |
| 2 | New session with a task | PASS (apart from 6) | **PASS** | Shots 07, 08, 11: the New dialog defaults to `copilot`; it launched `copilot --additional-mcp-config … -i "<task>"` in the worktree, the trust gate passed, working → idle, ③ ticked when the agent worked | — |
| 3 | Route: Agent → Commit → Push → Open PR | PASS | **PASS** | lab2: the agent stop settled; the commit message came from Copilot's one-shot ("Add hello script and output test"; `/commit-message/suggest` 7 s: "Add a function to uppercase text and append an exclamation mark"); pushed to the bare remote. lab3: Copilot committed itself, Commit ✓ (nothing left to commit), Push ✓. The PR stop halted: "MindFlock could not open the pull request for you — add a GitHub token in Settings → Connections → GitHub → Connect, or install the GitHub CLI. The branch is pushed — open the pull request on its host." Shot 15 | — |
| 4a | "Describe a route" box | PASS | **PASS** | `/api/flights/describe` → `"model": "copilot"`, 3 stops, 6.6 s | — |
| 4b | MCP: the Copilot agent calls describe_flight / start_flight | **FAIL**: "I can't call the MindFlock tools in this session: only their descriptions were discoverable, not callable". All 34 tools were connected (`/mcp`) | **mostly PASS**: lab5, interactive: describe_flight → start_flight (dry run) both returned, and Copilot's MCP approval ("Do you want to use this tool?") read as Needs you and was answered with `/answer` | Headless `start_flight` / `flight_status`: without the flag 2 of 5 calls went through, with `--allow-all-mcp-server-instructions` 4 of 4. Copilot's events log shows its server-side `tool_search` returning nothing for "start_flight" | `CopilotProvider.mcp_launch_args` adds `--allow-all-mcp-server-instructions` (MindFlock's tool map reaches the prompt). Still open: see Open gaps |
| 5 | Behaviors (Verify) written by Copilot | PASS for a visible change; a confusing failure otherwise | **PASS** | lab2 (`hello.py`): "From the directory containing `hello.py`, run `python hello.py`" → "prints exactly `hello from copilot`". lab3 (`calc.py`, a bare helper): Copilot wrote `"steps": []` on purpose, which failed as "held no usable steps" | `test_plans.parse_answer`: a deliberately empty answer now says "the agent wrote no behaviors: it found nothing in this change a person could try in real use. Add one yourself, or rewrite with a focus" |
| 6 | Needs you / Ask anywhere | **FAIL**: sessions launch without `--yolo` (skip-permissions is off by default), so Copilot asks before every shell command and file write. MindFlock read those as `activity: idle, asking: null`, and its ask-user form too. A seconds timer above the dialog keeps the pane changing, so the waiting patterns alone could never fire, and the automated typers (queue drain, mailbox, `/send`) had no dialog guard | **PASS** | lab3: 5 approvals in a row, each `clarify` with its question ("Create file — calc.py +3 -0. Do you want to create …", "Run unit tests and commit changes. Do you want to run this command?"), answered from the bell's **1 Yes / 2 Always / 3 No…**, then back to working. Shots 12, 13. `/dialog` serves the options; `/answer` works | `dialogs.parse_copilot` (the approval box + the question form; golden screens `tests/unit/data/dialogs/copilot_*`), `dialog_layout="copilot"`, Copilot `waiting_patterns` |
| 7a | Usage display | PASS | **PASS**: nothing Claude-shaped shows for a Copilot session. No usage bar or limit chip; tokens stay 0 and aren't drawn. Copilot's own footer shows "Session: 1.59 AIC used" | Shot 18, page text scan | — |
| 7b | "Claude" wording | **FAIL** (1a, 1d) | **PASS**: no Claude text on the main view, session menu or Setup. Settings → Agents lists "Claude Code — not installed" as an option, which is honest | Page text scans | Connections' agent row is named "Coding agent · GitHub Copilot" (was "· Copilot") |
| 7c | Flock mode (Claude's prompt hook) | Degrades by design: the request is typed after each `/send` prompt (verified: Copilot answered "OK" under it) | **PASS** | lab3 pane | Its default request named "(model claude-opus-5-5)", which Copilot workers can't run. Dropped when the default agent isn't Claude and the model wasn't set by hand |
| 7d | Voice | PASS | **PASS** | `/voice/condense` → Copilot one-shot, 8 s | — |

## Open gaps

1. **Copilot's MCP tool search is intermittent.** Copilot loads MCP tools on
   demand through a server-side search (`tool_search_call` in its events log).
   Even with MindFlock's instructions in the prompt, one interactive turn
   still reported "save_flight_plan … isn't among the exposed MindFlock tools,
   and searching for it returned no result". Asking again usually works.
   `--disable-builtin-mcps` also helped in one headless run, but it switches
   off Copilot's own GitHub tools, so it isn't applied. This needs a fix on
   Copilot's side, or fewer or more searchable MindFlock tool descriptions.
2. **The Setup picker needs the bundle rebuilt.** This branch is fenced out of
   `backend/web/static` (0.11.0 is in flight), so `npm run build` must run when
   it ships as 0.11.1. Until then the backend fixes already apply: the
   installed-agent default, step ① named after the chosen agent, Needs you,
   the sign-in probe, and the MCP flag. The picker itself is not in the
   served UI yet.
3. **Sign-in from another device.** `copilot login` uses a browser flow with a
   loopback callback on the machine running MindFlock. From a phone or
   another computer that callback can't complete; `copilot login
   --device-code` would. Not changed.
4. **"Other (type your answer)"** in Copilot's question form selects Copilot's
   own text box. The answer then has to be typed in the terminal (or sent with
   `/answer` `text`).
5. **Long paths in file approvals.** Copilot names the absolute path ("Do you
   want to create /…/worktrees/…/calc.py?"), so the bell line is mostly path.
   That is cosmetic.
6. **A trust gate after the startup window** parses as a dialog and shows as
   Needs you instead of being auto-accepted (the startup safety net, which runs
   first, still accepts it on launch).

## Screenshots

In the worker's scratchpad
(`/tmp/claude-1000/-home-emandel2630--mindflock-worktrees-emandel2630-vz-copilot-only-18e4a9002a8fef74/526ecab3-45f9-4fb6-a782-8c267e227162/scratchpad/shots/`):

* `01-first-launch.png`: welcome over "Install Claude Code" (0.11.0)
* `02-checklist.png`: step ① with no agent choice (0.11.0)
* `03-step1-install-copilot.png`, `04-install-terminal.png`, `05-after-install.png`: doctor install of Copilot
* `06-new-session-dialog.png`, `07-new-session-setup-myself.png`: the New dialog, agent `copilot`
* `08-session-launched.png`, `11-started.png`: a Copilot session running
* `09-step2-sign-in.png`, `10-copilot-login-terminal.png`: "Sign in to GitHub Copilot"
* `12-needs-you.png`, `13-bell-needs-you.png`: a Copilot approval as Needs you, with its own buttons
* `14-after-answers.png`, `15-route-tab.png`: the route (GitHub Copilot → Commit ✓ → Push ✓ → Open PR halted)
* `16-picker-first-launch.png`, `17-picked-copilot.png`: this branch's "Your agent" picker
* `18-copilot-session-main.png`, `19-session-menu.png`, `20-settings-Agents.png`: no Claude-only UI on a Copilot session
* `21-mcp-session.png`: the session that ran describe_flight → start_flight
