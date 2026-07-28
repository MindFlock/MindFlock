# mindflock (package)

The MindFlock Python package: session engine, provider framework, web UI, and the
Shortcut ticket-ingestion pipeline.

```
mindflock/
├── cmd/                  # command executor abstraction (Go-style)
├── config/               # ~/.mindflock config.json + state.json
├── log/                  # engine logging ({tempdir}/mindflock.log)
├── providers/            # coding-agent CLIs (claude, aider, …) + pricing/usage
├── session/              # instance lifecycle + storage + git worktrees + tmux
│   ├── git/
│   ├── tmux/
│   └── provisioned.py    # workspace provisioning
├── ticket_ingestion/   # Shortcut/GitHub → Claude pipeline
└── web/                  # FastAPI server + browser/desktop/mobile UI
```

Full documentation lives at the repo root: see [../../README.md](../../README.md)
and [../../docs/](../../docs/) (architecture, configuration, session engine,
web API, web UI, providers, ingestion pipeline, development).

Quick pointers:

- Web UI: `./web/run.sh` (or `python -m backend.web.run`), default port 8765.
- Pipeline: `python -m backend.ticket_ingestion` from the repo root.
- Engine/session state: `~/.mindflock/`. Provider + assistant state:
  `~/.mindflock-assistant/`.
- Requires `git`, `tmux`, and `gh` on PATH; sessions are tmux sessions
  (`mindflock_<title>`) with their own git worktrees.
