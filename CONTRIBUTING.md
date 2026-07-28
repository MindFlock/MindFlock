# Contributing to MindFlock

Thanks for wanting to make MindFlock better! Bug reports, docs fixes, and
code contributions are all welcome.

## Before your first PR: sign the CLA

MindFlock's code is licensed under [Apache-2.0](LICENSE), and some MindFlock
services are commercial. So that the project can keep that model legally clean
— and so the maintainer can dual-license or relicense future versions — every
contributor signs a one-time [Contributor License Agreement](CLA.md).

It's automated: open your PR, and the CLA bot will ask you to post

> I have read the CLA Document and I hereby sign the CLA

as a comment. That's it — the signature covers all your future contributions
too. You keep full rights to use your own contributions elsewhere for any
purpose.

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

## Tests

```bash
uv run pytest              # full suite (unit + property + integration)
uv run pytest tests/unit   # fast path while iterating
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
