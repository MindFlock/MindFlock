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
    # The screen KEY, not the word "Accounts" — that already appeared in the
    # bundle before this feature existed, so asserting on it proves nothing.
    assert '"accounts"' in js


def test_accounts_screen_talks_to_the_profiles_api():
    js = _js()
    assert "/api/settings/auth-profiles" in js
    assert "/api/settings/test/openrouter" in js


def test_new_session_dialog_has_an_account_picker():
    js = _js()
    assert '"new-account"' in js, "the New dialog's Account select is missing"
    # The unset option must NAME the fallback, so it is never a mystery value
    # (same contract as the agent picker's). "App default" alone is NOT the
    # assertion to make — the bundle carried that string before this feature —
    # so key on the picker's own affordance instead.
    assert "Manage accounts in Settings" in js


def test_new_session_dialog_has_a_model_picker():
    """With an OpenRouter/key account the model is the choice that matters —
    the dialog must offer it per session, fed by the key's own model list."""
    js = _js()
    assert '"new-account-model"' in js, "the per-session Model field is missing"
    assert "profile_model" in js
    # And a no-route agent/account combination warns instead of silently
    # launching on the CLI's own login.
    assert "has no route for" in js


def test_pane_header_has_the_account_swap_chip():
    js = _js()
    assert "acct-chip" in js
    # The swap posts to the per-instance profile endpoint.
    assert "/profile" in js
    # Its rows are styled and hoverable rather than bare table cells with an
    # inline cursor (the class is what usage.css hangs the hover state on).
    assert "acct-pop-row" in js


def test_swap_menu_offers_the_inherit_row():
    """The row a session that FOLLOWS the app default must be able to keep.

    Without it the only clickable row for such a session pins it to whatever
    the default currently resolves to, so the session silently stops tracking
    the default — the swap menu's one genuinely lossy misclick."""
    js = _js()
    assert "the app default account" in js


def test_pane_chip_explains_the_per_account_conversation():
    """A window keeps one thread per identity, so the copy must promise the
    thing that is actually true — and not the older, wrong claim that a swap
    resumes whatever conversation happened to be open."""
    js = _js()
    assert "one conversation per account" in js
    assert "conversations live per account" not in js
