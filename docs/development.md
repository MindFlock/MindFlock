# Development guide

## Setup

Python **3.12+** (`.python-version` pins 3.12), managed with **uv**. The package
is a flat layout (`backend/` at the repo root), built with `uv_build`.

```bash
uv sync --group dev --group web   # tests + everything the web UI needs
```

Contributing from outside the team? Read [CONTRIBUTING.md](../CONTRIBUTING.md)
first — outside PRs are gated on the CLA ([CLA.md](../CLA.md), checked
automatically by the CLA-assistant workflow).

Dependency groups (`pyproject.toml`): base (`aiohttp`, `tomli`), `dev` (pytest,
pytest-asyncio, hypothesis, httpx), `engine` (pyperclip, ptyprocess), `web`
(engine + fastapi, uvicorn, segno).

External tools the code shells out to: `git`, `tmux`, `gh`, `uv`, plus the agent
CLI (`claude`) and optionally `cursor`, `tailscale`, `wt.exe`.

## Dev build alongside the installed app

The desktop shell can run an **isolated dev copy** next to the installed prod
app — its own config/logs/window-state, a red **dev**-badged icon, and a
`MindFlock-DEV` wordmark, while the server (and your sessions) stays shared.
Turn it on with `MINDFLOCK_DEV=1` or the `--mindflock-dev` flag:

```bash
MINDFLOCK_DEV=1 npm start      # from electron/ (macOS/Linux; Windows uses PowerShell)
```

It is a no-op in the packaged prod build. Full per-OS instructions, the
fully-isolated (separate-server) variant, and how to pin the dev icon to the
Windows taskbar are in [`electron/README.md`](../electron/README.md).

## Pre-commit hooks

Shared, deterministic hooks live in `.pre-commit-config.yaml` (black,
detect-secrets against `.secrets.baseline`). Opt in once per clone:

```bash
uv run pre-commit install
```

Agent workspaces get this automatically — workspace provisioning runs
`pre-commit install` whenever the config file is present.

Two conventions to know:

- **Hooks that modify files fail the commit on purpose.** If black
  rewrites something, the commit stops so you can review; re-stage
  (`git add -u`) and run `git commit` again — the second attempt passes.
  This is standard pre-commit behavior, not a quirk of this repo.
- **CI enforces the same checks incrementally.** The `format` job runs
  `black --check` only on files a PR touches (the tree predates formatting
  enforcement, so files converge as they're edited).

Refresh the secrets baseline after intentionally adding something
high-entropy. Use the **same** exclude regex the hook scans with
(`.pre-commit-config.yaml`'s `detect-secrets` `exclude:`), so a regenerated
baseline matches what gets checked:

```bash
uvx detect-secrets scan --exclude-files '^(uv\.lock|\.gitnexus/|.*\.lock)$' > .secrets.baseline
```

## Versioning & releases

The release version lives in **three** manifests that must never disagree:
`pyproject.toml`, `electron/package.json`, and `frontend/package.json`
(`backend.__version__` reads the Python one back from installed metadata at
runtime). `scripts/bump-version.py` is the **single writer** of all three —
editing them by hand is how they drift.

```bash
python3 scripts/bump-version.py 0.1.4     # set an explicit MAJOR.MINOR.PATCH
python3 scripts/bump-version.py patch      # or bump major|minor|patch
python3 scripts/bump-version.py --check    # verify the three agree (CI guard)
```

Writing a version also rolls the CHANGELOG `[Unreleased]` heading into a dated
`[<version>]` section. The script is stdlib-only.

CI enforces this in a dedicated **`versions`** job (`.github/workflows/ci.yml`):
every push runs `bump-version.py --check` so a drifted manifest fails the
build, and a **tag push** additionally runs `--check --expect <tag>`, so a
`v0.1.4` tag pushed against `0.1.3` manifests fails rather than shipping a
mislabelled release. `.github/workflows/release.yml` runs the same guard as
its first step.

One pin in `frontend/package.json` is load-bearing beyond the version:
**`@vitejs/plugin-react` must stay ≤ 5.x** while the project pins vite 6. The
plugin's v6 imports `vite/internal`, which only exists in vite 8 — bumping it
breaks `npm ci` resolution *and* crashes `vite build` on config load, taking the
whole frontend build with it. Bump the plugin and vite together or not at all.

### Cutting a release

First, if you touched `electron/build/installer.nsh` or the electron-builder
config, dry-run the desktop matrix — the NSIS script and the macOS universal
build only compile on their own runners, and a tag makes the result public:

```bash
gh workflow run release.yml        # builds all three, publishes nothing
gh run watch                       # installers land as run artifacts
```

Then:

```bash
python3 scripts/bump-version.py <version>   # manifests + CHANGELOG
$EDITOR CHANGELOG.md                        # fill in the new section
git commit -am "Release <version>"
git tag -a v<version> -m "Release <version>"
git push origin main --follow-tags
```

The tag push runs `release.yml`, which publishes to one GitHub release:

| Asset | Built by | Notes |
|---|---|---|
| `mindflock-<v>-py3-none-any.whl`, `.tar.gz`, `SHA256SUMS` | `uv build` on Linux | The wheel is smoke-installed before publishing. |
| `MindFlock-Setup.exe` (+ `.sha256`) | electron-builder, Windows runner | NSIS; also installs the CLI into WSL (`electron/build/installer.nsh`). |
| `MindFlock.dmg` (+ `.sha256`) | electron-builder, macOS runner | Universal (arm64 + x64). |
| `MindFlock.AppImage` (+ `.sha256`) | electron-builder, Linux runner | |

The three desktop filenames carry **no version on purpose**: the README's
download buttons use
`https://github.com/MindFlock/MindFlock/releases/latest/download/<name>`,
which GitHub resolves only for an exact filename. They're set by
`artifactName` in `electron/package.json` — renaming one silently breaks the
front-page buttons, so change the README in the same commit.

Desktop builds are **unsigned** (no Developer ID / Authenticode cert yet), so
the desktop matrix sets `CSC_IDENTITY_AUTO_DISCOVERY=false` to build rather
than fail, and `fail-fast: false` keeps one OS's failure from cancelling the
others — a release missing one installer beats a release missing all three.

## Project layout

```
backend/
├── cmd/                  # Go-style command executor (mockable)
├── config/               # ~/.mindflock config.json + state.json (Go byte-compatible)
├── log/                  # Go-style file logger ({tempdir}/mindflock.log)
├── providers/            # coding-agent CLI abstraction + pricing + usage history
├── session/              # Instance lifecycle
│   ├── git/              #   worktrees, diff, commit/push
│   ├── tmux/             #   tmux sessions, PTY, resize handling
│   └── provisioned.py    #   workspace provisioning + launcher
├── ticket_ingestion/   # the Shortcut/GitHub → Claude pipeline
└── web/
    ├── server.py         # FastAPI app assembly: routes, background loops, terminal WS
    ├── core/             # one module per concern: engine, terminal (PTY↔WS),
    │                     # git_ops, agent_state, agent_sessions, snapshot,
    │                     # session_stats, budget, usage_api, mobile_access, …
    ├── addons/           # addon framework + Ticket Ingestion + Assistant
    ├── static/           # frontend (no build step; vendored xterm.js)
    └── run.py / run.sh   # launchers (local by default, tailscale opt-in; QR banner)

tests/{unit,property,integration}/
scripts/list_workflows.py  # print Shortcut workflow/state ids for config.toml
docs/                      # this documentation
```

## Running tests

```bash
uv run pytest                       # everything
uv run pytest tests/unit -q         # just unit
uv run pytest tests/property -q     # hypothesis property tests
```

Run with the web auth gate **off**: `CS_WEB_MODE` unset and `MINDFLOCK_AUTH=0`
in your shell, or the API-contract tests 401. `tests/conftest.py` isolates the
settings store per test; a test that needs the gate on sets
`MINDFLOCK_AUTH_TOKEN`/`CS_WEB_MODE` itself.

Pytest config lives in `pyproject.toml` (`testpaths=["tests"]`,
`asyncio_mode="auto"`). `tests/conftest.py` adds the repo root to `sys.path`
(so the top-level `backend` package imports without a pip install) and
redirects `tempfile` + `MINDFLOCK_ASSISTANT_DIR` into pytest's tmp dir so tests
never touch your real caches — important because the pipeline's testmon
refresher re-runs suites automatically.

### Test map

| Area | Files |
|---|---|
| Engine: instances, storage, worktrees, tmux | `test_worktree_ops`, `test_storage_contract`, `test_tmux_naming`, `test_state` |
| Launch behavior (golden files) | `test_launch_parity` (compares generated launchers to `tests/unit/data/*.golden.sh`), `test_provider_framework`, `test_claude_provider` |
| Providers: pricing, usage | `test_pricing`, `test_usage_history` |
| Web: routes, git ops, terminal, addons | `test_route_precedence`, `test_git_ops`, `test_terminal_extras`, `test_scroll_speed`, `test_addons`, `test_frontend_slots`, `test_cursor_window`, `test_webui` |
| Pipeline: config, state, scan, validate | `test_config`, `test_state`, `test_backfill`, `test_filter`, `test_ticket_validator`, `test_logging_config` |
| Pipeline: provisioning + launch | `test_environment_provisioner`, `test_session_runner`, `test_orchestrator`, `test_clarification`, `test_workspace_cleanup` |
| PR flow | `test_pr_pipeline` |
| Property (hypothesis) | backfill ordering, prompt construction, validation-with-context, assignee filter, timestamp persistence |
| Integration | `test_claude_runner` (async ClaudeCodeRunner) |
| Frontend (vitest) | `frontend/src/__tests__/*.test.ts` — the pure logic modules (layout, diff, keymap, ordering, stage, format, barDefs, usageModel) |

The launch-parity golden files pin the workspace launcher byte-for-byte
(backend rolling, markers, resume loop) — update them deliberately when changing
launch behavior.

The frontend tests are **not** part of `uv run pytest`. `vitest` is a
`frontend/` devDependency and its config is `frontend/vitest.config.ts`
(`environment: "node"`, separate from `vite.config.ts` on purpose so the
production build is untouched by test settings). Nothing in CI runs them today:

```bash
cd frontend && npm test           # 8 files, 118 tests
```

## Conventions

- **Go parity is intentional.** PascalCase methods (`Start`, `Pause`,
  `Kill`) with snake_case aliases, error-return executors, and byte-exact JSON
  all mirror the original Go engine. Keep new engine code consistent with the
  surrounding style.
- **Providers own CLI-specific behavior.** Don't hardcode launch flags or prompt
  strings in the engine or web server — extend a provider (or add a TOML config).
- **Secrets stay out of git**: `config.toml`, `state.json`, `logs/`,
  `workspaces/` are gitignored.

## Known issues

Verified against the codebase 2026-07-15; useful when touching these areas:

1. **Old config section names are not aliased.** All code reads `[mindflock]`; a
   local `config.toml` still carrying a section under an earlier project name
   silently disables engine routing and the `open_cursor`/`skip_permissions`
   overrides.
2. Ingestion is polling-only — there is no webhook listener.
3. The clarification handler has no `skip` path — it always returns
   `provide_context`.
4. Story dedup is id-only — skipped stories are never retried automatically.
5. Base clones (`_base_*`) are *not* in the workspace-cleanup preserve list; if the
    pipeline sits idle for 3+ days the canonical clone can be pruned (it will be
    re-cloned on next use).

## Useful commands

```bash
uv run python scripts/list_workflows.py   # Shortcut workflow/state ids (needs config.toml)
./backend/web/run.sh local            # web UI on localhost:8765
python -m backend.ticket_ingestion      # pipeline (foreground)
tmux ls | grep mindflock_                   # see live agent sessions
```
