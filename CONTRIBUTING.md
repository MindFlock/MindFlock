# Contributing to MindFlock

Thanks for wanting to make MindFlock better! Bug reports, docs fixes, and
code contributions are all welcome.

## Sign off your commits (no CLA)

There's no Contributor License Agreement to sign. MindFlock is
[Apache-2.0](LICENSE), and your contribution arrives under the same license —
inbound equals outbound, so nothing needs assigning. You keep the copyright to
your work.

All we need is provenance: certify that you wrote the patch and are allowed to
submit it, per the [Developer Certificate of
Origin](https://developercertificate.org/). Add the `-s` flag when you commit:

```bash
git commit -s -m "your message"
```

That appends a `Signed-off-by: Your Name <you@example.com>` line. The **DCO**
workflow checks every commit in a PR and tells you the exact fix-up command if
one is missing:

```bash
git commit --amend -s --no-edit                  # just the last commit
git rebase --signoff origin/main                 # every commit on the branch
git push --force-with-lease
```

Tip: `git config format.signoff true` makes it automatic in this repo.

## Development setup

```bash
git clone https://github.com/MindFlock/MindFlock
cd MindFlock
uv sync --group web --group dev   # web = FastAPI server deps, dev = pytest
uv run mindflock doctor           # checks git/tmux/gh/agent CLI
uv run mindflock serve            # localhost:8765, from a repo you want to manage
```

Runtime deps: `git`, `tmux`, and at least one agent CLI (`claude` by
default). `mindflock doctor --fix` offers to install what's missing.

Optional but recommended — the repo's hooks, both stages:

```bash
uvx pre-commit install --hook-type pre-commit --hook-type pre-push
```

`pre-commit`: black + a secret scan. `pre-push`: the version manifests agree
(the version lives in **four** places — `pyproject.toml`, `electron/` and
`frontend/package.json`, and `uv.lock`) and the frontend bundle is current
(below). Both mirror CI, so you find out before the push rather than after.

## Frontend changes

The web UI is React + TypeScript under `frontend/`, but **the built bundle is
committed and is what ships**: `uv build` copies `backend/web/static/app.js`
into the wheel and electron-builder packages the same tree — neither ever runs
vite. So a change under `frontend/src/` is only half a change until its build
is committed with it:

```bash
cd frontend
npm ci                     # not `npm install` — the lockfile is the contract
npm test                   # vitest, node env
npm run build              # typechecks, then writes backend/web/static/
cd .. && git add backend/web/static
```

CI's `frontend bundle is current` job rebuilds and fails when the committed
tree differs, so a forgotten build is a red build rather than a shipped UI that
silently lags its own source. `scripts/check-bundle-fresh.sh` is the same check,
and what the pre-push hook runs.

One pin is load-bearing beyond its version: **`@vitejs/plugin-react` must stay
≤ 5.x** while the project pins vite 6 — the plugin's v6 imports `vite/internal`,
which only exists in vite 8, and bumping it breaks both `npm ci` resolution and
`vite build`. Bump the plugin and vite together or not at all.

## Tests

```bash
uv run pytest              # full suite (unit + property + integration)
uv run pytest tests/unit   # fast path while iterating
cd frontend && npm test    # the frontend suite is NOT part of pytest
```

- Add tests for what you change. The suite runs with the auth gate off and a
  per-test isolated settings store (see `tests/conftest.py`) — a test that
  needs the gate sets `MINDFLOCK_AUTH_TOKEN`/`CS_WEB_MODE` itself.
- CI runs the suite plus a cold-install job on every PR.

## Pull requests

- Keep PRs focused; separate refactors from behavior changes.
- Match the surrounding code's style and comment density — modules carry
  design rationale in docstrings; keep those truthful when you change
  behavior.
- Update the relevant `docs/*.md` page when you change a CLI flag, endpoint,
  or config field.

## Security issues

Do **not** open a public issue for anything exploitable — see
[SECURITY.md](SECURITY.md) and email **security@mindflock.ai**.
