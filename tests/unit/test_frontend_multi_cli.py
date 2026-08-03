"""The UI surfaces for multi-CLI ingestion + the local-model path.

These read the built bundle the server actually serves, so a change to
``frontend/src`` that was never rebuilt into ``backend/web/static/app.js`` fails
here — the same contract the other ``test_frontend_*`` suites use.
"""

from starlette.testclient import TestClient

from backend.web import server

client = TestClient(server.app)


def _js() -> str:
    return client.get("/app.js").text


# --------------------------------------------------------------------------- #
# Per-source agent picker (multi-CLI ingestion).
# --------------------------------------------------------------------------- #
def test_ticketing_source_has_an_agent_picker():
    js = _js()
    assert '"tk-agent"' in js, "the per-source Agent select is missing"
    assert '"agent"' in js
    # The unset option must NAME the fallback, so "App default" is never a
    # mystery value.
    assert "App default" in js


def test_agent_picker_reads_the_provider_list():
    """The choices come from the live provider registry, not a hardcoded list —
    otherwise a user-defined provider TOML could never be selected."""
    js = _js()
    assert "/api/providers" in js


# --------------------------------------------------------------------------- #
# Local-model screen.
# --------------------------------------------------------------------------- #
def test_local_model_screen_is_registered():
    js = _js()
    assert '"localmodel"' in js and "Local model" in js


def test_local_model_screen_has_its_controls():
    js = _js()
    for el in ("lm-enabled", "lm-runtime", "lm-base-url", "lm-model", "lm-test"):
        assert f'"{el}"' in js, f"{el} is missing from the Local model screen"


def test_local_model_screen_calls_the_probe_endpoint():
    assert "/api/settings/test/local-model" in _js()


def test_local_model_screen_is_honest_about_claude():
    """The one thing the privacy story cannot be quiet about: Claude Code has no
    local route, so a session on it still uses the hosted API."""
    js = _js()
    assert "Anthropic API" in js


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
    """The UI seeds a newly added source with the catalog's FIRST entry, so this
    is what makes GitHub Issues the default on-ramp."""
    r = client.get("/api/settings/providers/ticketing")
    providers = r.json()["providers"]
    assert providers[0]["id"] == "github_issues"
    assert "Zero config" in providers[0]["blurb"]
    assert not any(f.get("required") for f in providers[0]["fields"])
