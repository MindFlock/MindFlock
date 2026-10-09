"""Every writer refuses an unreadable settings.json the same way.

``settings.update_settings`` raises ``SettingsUnreadable`` (and saves nothing)
when settings.json exists but doesn't parse — saving defaults over it would
lose it, and settings sync would spread "everything deleted" to every device.
The Settings routes answered that with a 409 and the hint; the ntfy and
notification-rule saves, the IDE open-on-ticket switch and the access-token
rotate answered a bare 500 instead, and ``mindflock accounts`` a traceback.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.config import settings as store

BROKEN = '{"general": {"onboarded": true}, "github": {"token": "ghp_keep",}'


@pytest.fixture(autouse=True)
def _isolate(isolate_settings_store):
    yield


@pytest.fixture
def broken(tmp_path, monkeypatch):
    monkeypatch.delenv("CS_WEB_MODE", raising=False)
    monkeypatch.delenv("MINDFLOCK_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("MINDFLOCK_AUTH", "0")
    path = store.settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(BROKEN)
    store.invalidate()
    yield path
    assert path.read_text() == BROKEN  # never saved over
    store.invalidate()


@pytest.mark.parametrize(
    "url,body",
    [
        ("/api/notify/rules/needs_input", {"enabled": False}),
        ("/api/notify/ntfy", {"enabled": True, "topic": "my-topic-x1"}),
        ("/api/ide/open-on-ticket", {"enabled": True}),
        ("/api/settings/auth-token/rotate", None),
    ],
)
def test_a_save_on_an_unreadable_file_is_a_409_with_the_hint(broken, url, body):
    from backend.web import server

    c = TestClient(server.app, raise_server_exceptions=False)
    r = c.post(url, json=body) if body is not None else c.post(url)
    assert r.status_code == 409, (url, r.status_code, r.text[:200])
    assert r.json()["error"] == store.UNREADABLE_HINT


@pytest.mark.parametrize(
    "argv",
    [
        ["accounts", "add", "work"],
        ["accounts", "use", "default"],
    ],
)
def test_cli_accounts_says_what_to_do(broken, monkeypatch, capsys, argv):
    monkeypatch.setenv("MINDFLOCK_HOST", "127.0.0.1")
    monkeypatch.setenv("MINDFLOCK_PORT", "1")  # no server -> the local store
    from backend.cli import main

    assert main(argv) == 1
    assert "error: %s" % store.UNREADABLE_HINT in capsys.readouterr().err
