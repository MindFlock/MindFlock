"""The UI surfaces for auth profiles (Settings → Accounts + the per-session
account picker/chip).

These read the built bundle the server actually serves, so a change to
``frontend/src`` that was never rebuilt into ``backend/web/static/app.js`` fails
here — the same contract the other ``test_frontend_*`` suites use.
"""

from starlette.testclient import TestClient

from backend.web import server

client = TestClient(server.app)


def _js() -> str:
    return client.get("/app.js").text


def test_accounts_screen_is_registered():
    js = _js()
    assert '"accounts"' in js and "Accounts" in js


def test_accounts_screen_talks_to_the_profiles_api():
    js = _js()
    assert "/api/settings/auth-profiles" in js
    assert "/api/settings/test/openrouter" in js


def test_new_session_dialog_has_an_account_picker():
    js = _js()
    assert '"new-account"' in js, "the New dialog's Account select is missing"
    # The unset option must NAME the fallback, so "App default" is never a
    # mystery value (same contract as the agent picker's).
    assert "App default" in js


def test_pane_header_has_the_account_swap_chip():
    js = _js()
    assert "acct-chip" in js
    # The swap posts to the per-instance profile endpoint.
    assert "/profile" in js
