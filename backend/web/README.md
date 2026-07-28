# MindFlock — Server + UI

The FastAPI server and the UI it serves: a sidebar of sessions plus a live
`xterm.js` terminal per session, each wired straight to the underlying tmux
session over a WebSocket. The clients are the **desktop app**
([../../electron/README.md](../../electron/README.md) — the one
supported desktop client, which auto-starts this server) and the **phone UI**
at `/m` (tailnet QR).

**Full documentation:** [../../docs/web-api.md](../../docs/web-api.md)
(every HTTP/WS endpoint) and [../../docs/web-ui.md](../../docs/web-ui.md)
(frontend guide: grid, tabs, workflow stages, shortcuts, mobile UI, addons).

## Run (manual / headless)

```bash
mindflock serve            # local mode (default): binds 127.0.0.1
mindflock serve tailscale  # binds 0.0.0.0, prints /m URL + QR (auth gate on)
./run.sh                   # same as default (local), from a source checkout
./run.sh tailscale         # tailnet access
PORT=9000 ./run.sh         # custom port (default 8765)
python -m backend.web.run [local|tailscale] [port]   # same thing
```

Prerequisites from a source checkout: `uv sync --group web` (engine deps +
FastAPI), and `tmux` + `gh` on PATH.

## Layout

- `server.py` — FastAPI app assembly (`backend.web.server:app`): every
  HTTP/WS route, the always-on background loops, lifespan + middleware. Its
  module docstring maps which helper lives in which `core/` module.
- `core/` — one module per concern: engine singleton (`engine.py`), git
  queries (`git_ops.py`), the PTY↔WebSocket bridge (`terminal.py`), agent
  activity detection (`agent_state.py`), tmux pane lifecycle
  (`agent_sessions.py`), session JSON descriptors (`snapshot.py`), token/cost
  telemetry (`session_stats.py`), budgets (`budget.py`), usage descriptors
  (`usage_api.py`), phone access/QR (`mobile_access.py`), and more — see
  `server.py`'s docstring for the full table. Two rules keep the split safe:
  **no `core/` module defines a route** (every `APIRouter`/`@app` handler lives
  in `server.py`; the modules are pure helpers), and **test-patched names are
  re-imported into `server.py`**, so `monkeypatch.setattr(server, "_foo", …)`
  keeps working wherever `_foo` actually lives.
- `addons/` — self-registering addons: Ticket Ingestion (runs the pipeline as
  a managed subprocess, `/api/mindflock/*`) and Assistant (`/api/assistant/*`).
- `static/` — `index.html`, `app.js`, `style.css`, `mobile.*`,
  `core/{slots,ws-xterm}.js`, vendored xterm.js. No build step.

## Tests

```bash
uv run pytest tests/unit/test_webui.py tests/unit/test_route_precedence.py -q
```

These cover the REST contract, route precedence, and static serving; the full
create→terminal path is verified by running the server against a real repo.
