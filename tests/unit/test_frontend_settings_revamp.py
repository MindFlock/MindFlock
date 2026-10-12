"""Settings revamp — the server half: the System logs API, the activity-log
middleware, the served notify addon, and the contract between the routes the
Settings UI calls and the ones the server serves.

The UI half (screens, nav groups, System logs markup, the budget lock, the
panels' query cache, ...) is pinned in vitest against the frontend source:
frontend/src/__tests__/settingsRevamp.test.ts and intakePanels.test.ts."""

import re
from pathlib import Path

from starlette.testclient import TestClient

from backend.web import server

FRONTEND_SRC = Path(__file__).resolve().parents[2] / "frontend" / "src"

client = TestClient(server.app)


def test_bundle_calls_settings_routes_the_server_serves():
    """Contract: each path the Settings screens call is in the shipped bundle
    AND is a route the server serves — a rename on either side fails here."""
    js = client.get("/app.js").text
    paths = server.app.openapi()["paths"]
    for called, route in (
        ('"/api/connections', "/api/connections"),
        ("/api/logs?name=", "/api/logs"),
        ('"/budget/raise"', "/api/instances/{title}/budget/raise"),
        ('"/api/setup"', "/api/setup"),
        ('"/api/providers/status"', "/api/providers/status"),
        ("/binary-path", "/api/providers/{name}/binary-path"),
        ('"/api/tickets"', "/api/tickets"),
        ('"/api/github/prs"', "/api/github/prs"),
        ('"/api/github/issues"', "/api/github/issues"),
    ):
        assert called in js, f"the bundle no longer calls {called}"
        assert route in paths, f"the server no longer serves {route}"


def test_connections_modal_module_is_gone():
    # Connections is an inline settings screen now — the old modal module is
    # removed entirely.
    assert client.get("/addons/connections.js").status_code == 404


def test_panel_stale_window_matches_the_server_fanout_ttl():
    """The Intake panels' client freshness window matches the server's fan-out
    TTL: inside it, a mount is answered from the query cache instead of making
    a round trip to be told the same thing. Read from the source, not the
    bundle, so a bundler's constant folding can't break it."""
    queries = (FRONTEND_SRC / "state" / "queries.ts").read_text(encoding="utf-8")

    def ms(name):
        m = re.search(rf"const {name} = ([\d_]+);", queries)
        assert m, f"{name} is no longer a plain constant in state/queries.ts"
        return int(m.group(1).replace("_", ""))

    assert ms("PANEL_STALE_MS") == server._FANOUT_TTL * 1000 == 20_000
    # A stale payload is re-polled well inside the window.
    assert ms("PANEL_STALE_RETRY_MS") < server._FANOUT_TTL * 1000
    assert 2.0 < server._FANOUT_TTL


def test_notify_addon_exposes_enable_api():
    js = client.get("/addons/notify.js").text
    assert "window.mindflockAddons.notify.enable" in js
    assert "mf-notify-state" in js  # sync event between the bar + Settings toggle


def test_api_logs_returns_server_tail():
    """/api/logs lists sources and returns the tail of the selected one; an
    unknown name falls back to the first source rather than erroring."""
    from backend import log as _log

    if _log.ErrorLog is not None:
        _log.ErrorLog.Printf("system-logs test marker %d", 7)
    d = client.get("/api/logs").json()
    assert {"sources", "selected", "text", "size", "exists", "truncated"} <= set(d)
    names = [s["name"] for s in d["sources"]]
    assert "server" in names
    assert d["selected"] == "server"
    # Unknown source name is coerced to the first available source.
    assert client.get("/api/logs?name=bogus").json()["selected"] == "server"


def test_ingestion_logs_surface_from_repo_root_not_cwd(tmp_path, monkeypatch):
    """The pipeline runs as a subprocess from the repo root, so its logs live at
    ``<repo root>/logs/`` — not the server's cwd. ``_log_sources`` must resolve
    them via that root (the same anchor the ingestion addon launches from), or
    the ingestion log goes missing and only 'server' shows in System logs."""
    from backend.web.core import system_logs

    (tmp_path / "config.toml").write_text("")
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "ticket-ingestion.log").write_text("hello ingestion\n")
    monkeypatch.setenv("MINDFLOCK_REPO_ROOT", str(tmp_path))

    srcs = {s["name"]: s["path"] for s in system_logs._log_sources()}
    assert {"server", "ingestion"} <= set(srcs)
    # One ingestion source — the raw capture the sidebar tails, not a confusing
    # second "structured" duplicate of the same lines.
    assert srcs["ingestion"].endswith("ticket-ingestion.log")
    assert "pipeline" not in srcs


def test_activity_log_records_requests_and_redacts_token(monkeypatch):
    """The HTTP activity-log middleware logs every request as
    ``METHOD path -> status ms client`` with the access token redacted."""
    import asyncio

    from backend import log as _log

    lines = []

    class _Cap:
        def Printf(self, fmt, *args):
            lines.append(fmt % args if args else fmt)

    monkeypatch.setattr(_log, "InfoLog", _Cap())

    class _U:
        path = "/api/foo"
        query = "token=SEEKRIT&x=1"

    class _C:
        host = "5.6.7.8"

    class _Req:
        method = "POST"
        url = _U()
        client = _C()

    class _Resp:
        status_code = 201

    async def _call_next(_req):
        return _Resp()

    asyncio.run(server._activity_log(_Req(), _call_next))
    assert len(lines) == 1, lines
    line = lines[0]
    assert "POST /api/foo" in line
    assert "-> 201" in line and "5.6.7.8" in line
    assert "SEEKRIT" not in line  # token never hits the log
    assert "token=<redacted>" in line
