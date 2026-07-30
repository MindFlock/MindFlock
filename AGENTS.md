# MindFlock — agent onboarding

MindFlock manages fleets of coding-agent sessions: **a session = a git
workspace + a tmux session running an agent CLI** (claude, codex, antigravity,
aider, opencode, cline, goose), observable and steerable from a web UI, a
mobile page, an Electron desktop app, and the `mindflock` CLI.

## Setup and tests

```bash
uv sync --group dev --group web        # Python 3.12+, flat layout
uv run pytest                          # full suite (tests/{unit,property,integration})
```

Tests assume the web auth gate is off: run with `CS_WEB_MODE` unset and
`MINDFLOCK_AUTH=0` if your shell exports either, or API-contract tests 401.

Run the server with `mindflock serve` (or `python -m backend.web.run local`).
`mindflock doctor --fix` preflights/installs runtime deps (git, tmux, agent
CLIs; `gh` is reported but **optional** — pushing is plain `git push` over the
user's own remote, SSH or HTTPS, and PR create/merge fall back to the GitHub
REST API with a token, then to a browser URL).

## Map

| Where | What |
|---|---|
| `backend/session/` | Engine: Instance lifecycle, git worktrees, tmux/PTY, provisioning |
| `backend/providers/` | Per-CLI launch/resume/classification/usage; add a CLI with a TOML, no Python |
| `backend/web/` | FastAPI server (`server.py` = routes + loops only; helpers in `core/`, one module per concern — see server.py's docstring map), `addons/`, `static/` (no-build frontend) |
| `backend/ticket_ingestion/` | Ticket/PR ingestion pipeline (Shortcut/Jira/Linear/GitHub Issues/Asana) |
| `docs/` | architecture, session-engine, web-api, web-ui, providers, extensions, configuration, cli, development, ingestion-pipeline |

## Conventions

- Engine code mirrors its Go ancestor: PascalCase methods with snake_case
  aliases, error-return executors, byte-exact JSON. Keep new engine code
  consistent with the surrounding style.
- Providers own CLI-specific behavior — never hardcode launch flags or prompt
  patterns in the engine or web server.
- `tests/unit/test_launch_parity.py` pins launcher scripts byte-for-byte
  against golden files; update goldens deliberately.
- Secrets stay out of git: `config.toml`, `state.json`, `logs/`, `workspaces/`
  are gitignored.

See `docs/development.md` for the full dev guide and known issues. Developers
using GitNexus code intelligence get a locally generated `CLAUDE.md` and
`.claude/skills/gitnexus/` from `gitnexus analyze` — both are gitignored.
