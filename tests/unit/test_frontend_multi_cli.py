"""Multi-CLI ingestion + the local-model path: the backend half and the
routes the UI calls.

The route checks are a backend↔frontend contract, so they read the bundle the
server actually serves AND the server's own route table. The UI surfaces (the
per-source Agent picker, the Local model screen) are pinned against the source
in ``frontend/src/__tests__/multiCliUi.test.ts``.
"""

from starlette.testclient import TestClient

from backend.web import server

client = TestClient(server.app)


def _js() -> str:
    return client.get("/app.js").text


def _paths() -> dict:
    # Included routers are opaque in app.routes; the OpenAPI map flattens them.
    return server.app.openapi()["paths"]


# --------------------------------------------------------------------------- #
# Per-source agent picker (multi-CLI ingestion).
# --------------------------------------------------------------------------- #
def test_agent_picker_reads_the_provider_list():
    """The choices come from the live provider registry, not a hardcoded list —
    otherwise a user-defined provider TOML could never be selected."""
    js = _js()
    assert "/api/providers" in js
    assert "/api/providers" in _paths()


# --------------------------------------------------------------------------- #
# Local-model screen.
# --------------------------------------------------------------------------- #
def test_local_model_screen_calls_the_probe_endpoint():
    assert "/api/settings/test/local-model" in _js()
    assert "/api/settings/test/local-model" in _paths()


def test_local_model_probe_endpoint_is_served():
    r = client.post("/api/settings/test/local-model", json={"runtime": "ollama"})
    assert r.status_code == 200
    body = r.json()
    # Shape the screen depends on. `ok` is False on this machine (no server
    # running) and that is a rendered state, not an error.
    for key in ("ok", "base_url", "models", "supported_agents", "default_base_urls"):
        assert key in body
    assert body["default_base_urls"]["ollama"].startswith("http")


def test_probe_endpoint_reports_supported_agents():
    r = client.post("/api/settings/test/local-model", json={})
    agents = r.json()["supported_agents"]
    # Only CLIs with a verified local route, and never claude.
    assert "claude" not in agents
    assert set(agents) <= {"codex", "aider", "goose"}


# --------------------------------------------------------------------------- #
# GitHub Issues as the flagship on-ramp.
# --------------------------------------------------------------------------- #
def test_ticketing_catalog_leads_with_github_issues():
    """GitHub Issues leads the Provider list and needs no input. It is no
    longer preselected: a new Intake source starts on "Choose a tracker…"
    (round 3: a preselected GitHub Issues read as the place for issues, beside
    the GitHub issues tab)."""
    r = client.get("/api/settings/providers/ticketing")
    providers = r.json()["providers"]
    assert providers[0]["id"] == "github_issues"
    assert "GitHub connection" in providers[0]["blurb"]
    assert not any(f.get("required") for f in providers[0]["fields"])
