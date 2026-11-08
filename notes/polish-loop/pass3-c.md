# Pass 3 — area (c): agent CLI / provider setup

Walked in a ux-lab sandbox (`sandbox.py`, port 9430, fresh HOME, private tmux,
stub `claude`, no other agent CLI on PATH) through headless Chrome on the real
bundle: Settings → Agents (default picker, "Not installed — how to install"
fold, flags), Custom agents (add / re-save / binary path), Accounts (add),
Local model; then started a session on a custom agent that is a "shell alias"
(`claudekiro`, not on PATH) to see the launch failure in the pane, fixed it
with the Binary path field and pressed Start (it ran `ccc --kiro`). Fixes were
checked by building the bundle into a scratch dir and routing app.js/style.css
to it (the committed bundle is untouched).

| # | Sev | Finding | Repro / where | Status |
|---|---|---|---|---|
| 1 | high | Custom agents: adding an agent the server warns won't start (the shell-alias case) left the form on **Add agent** with its values filled. Doing what the warning says — fill the binary path, click again — POSTs the same name: 409 *provider 'x' already exists*. Dead end. | Custom agents → + Add → name `claudekiro`, Add, fill Binary path, Add again; `Providers.tsx` save() | FIXED f5381ff6 (a save with a warning turns the form into an Edit of that agent → Save changes = PUT; verified in the sandbox: the second click saved `binary_path` and the card went Connected) |
| 2 | med | The alias trap was only explained AFTER saving: the add form was six placeholder-only inputs (typed values had no names), the binary path said only "optional", and "program (default: name)" read as the command to run though it only adds names that select the agent (launch = binary_path → command → name). Nothing pointed at the TOML for the other keys. | Custom agents add form | FIXED f5381ff6 (labelled fields with hints; Binary path names the alias/function case; "Also matches" says it doesn't change what runs; TOML-file note) |
| 3 | med | The add form sat below eight built-in rows (a long scroll on the screen named for it); a new agent's card stayed blank until the shared agent rows went stale; the alias hint printed twice per row (card + row). | Custom agents | FIXED f5381ff6 (form first; add/save/delete notify every agent card; one hint) |
| 4 | low | Jargon: source badges "BUILTIN"/"USER", toasts "Added agent claudekiro"/"Binary path saved for codex" with raw CLI ids. | Custom agents | FIXED f5381ff6 ("built in"/"custom", product names) |
| 5 | med | Accounts → Add account asked for "Id" before "Label" with no hint; filling in only the label failed with *id must be lowercase letters/digits/-/_ (max 64)*. The Agent picker and badge said `claude`/`codex`. | Accounts → Add account, type a label, Add | FIXED b8b11628 (label first; optional "Short name" made from the label: *Personal Max* → `personal-max`; product names) |
| 6 | low | Agents › Keep usage windows warm listed "claude · 5h window" / "codex: next ~…" among product names everywhere else. | Agents → Advanced | FIXED 51cee620 |
| 7 | low | Docs disagree with the UI: "Settings → Agent providers" / "Agent CLI" (now Custom agents / Agents), "Set up another agent" said to be in the Advanced fold (it is under the picker), default launch flags said to be on Custom agents (they are Agents' "Extra flags"). | docs/providers.md, web-ui.md, web-api.md, accounts.md | FIXED 02c68cc1 |
| 8 | med | A session whose agent isn't installed (custom agent named after an alias) is HELD as "Offline since the agent crashed"; the rail banner says "1 session stayed offline after a crash … Start all" — it never ran (exit 127), and Start all just fails again. The red "isn't installed — Install X…" bar above it is right; the hold text contradicts it. | start a session on `claudekiro` with no binary path; `backend/web/core/revive.py:744` `hold_reason_after_death` (127/126 → REASON_CRASH), `frontend/src/components/grid/HeldBody.tsx` | DEFERRED (outside fence: don't hold, or a "couldn't start" reason, for exit 126/127) |
| 9 | low | A not-installed agent card offers only Copy (install command) — no Test / "Check again" after you install it in a terminal, and "Not installed" shows twice (subtitle + chip). Docs say Install → Sign in → Test. | Agents fold / Custom agents, Codex card; `components/settings/screens/Connections.tsx` ConnectionRowView | DEFERRED (outside fence) |
| 10 | low | The TOML note names the default dir (`~/.mindflock-assistant/providers/`) because `/api/providers/status` doesn't return the resolved providers dir. | `backend/web/addons/settings.py` `providers_status` | DEFERRED (outside fence: add `providers_dir` and show it) |
| 11 | low | Local model's intro says "codex, aider and goose" in raw ids. | `LocalModel.tsx` copy | DEFERRED (nit) |
