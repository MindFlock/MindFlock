"""Capability gating: only the coding agent is required; everything else hides.

Locks the contract that /api/config exposes a `caps` block (git / tailscale /
ticketing), that the git-only endpoints refuse cleanly (409, `error` field)
when git isn't installed instead of 500ing mid-subprocess, and that the static
frontend carries the data-caps hooks + "connect X" gate messages the body
classes (no-git / no-tailscale / no-ticketing) switch on.
"""

from __future__ import annotations

import types

from fastapi.testclient import TestClient

from backend.web import server
from backend.web.core import git_ops

client = TestClient(server.app)


# --------------------------------------------------------------------------- #
# /api/config `caps`
# --------------------------------------------------------------------------- #
def test_config_exposes_caps_booleans():
    caps = client.get("/api/config").json()["caps"]
    assert set(caps) == {"git", "tailscale", "ticketing"}
    assert all(isinstance(v, bool) for v in caps.values())


def test_caps_git_follows_binary_presence(monkeypatch):
    monkeypatch.setattr(server, "git_available", lambda: False)
    assert client.get("/api/config").json()["caps"]["git"] is False
    monkeypatch.setattr(server, "git_available", lambda: True)
    assert client.get("/api/config").json()["caps"]["git"] is True


def test_caps_tailscale_follows_binary_presence(monkeypatch):
    monkeypatch.setattr(server.shutil, "which", lambda name: None)
    assert client.get("/api/config").json()["caps"]["tailscale"] is False


def _settings_ns(sources):
    tk = types.SimpleNamespace(sources=sources)
    return types.SimpleNamespace(ticketing=tk)


def _source(**kw):
    base = dict(provider="shortcut", api_token="", project="", id="s1")
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_caps_ticketing_requires_a_usable_source(monkeypatch):
    from backend.config import settings as settings_store

    # No sources at all -> not connected.
    monkeypatch.setattr(settings_store, "load_settings", lambda: _settings_ns([]))
    assert server._ticketing_connected() is False
    # A source without credentials -> still not connected.
    monkeypatch.setattr(
        settings_store, "load_settings", lambda: _settings_ns([_source()])
    )
    assert server._ticketing_connected() is False
    # Token present -> connected.
    monkeypatch.setattr(
        settings_store,
        "load_settings",
        lambda: _settings_ns([_source(api_token="tok")]),
    )
    assert server._ticketing_connected() is True
    # github_issues needs no token but does need its project scope.
    monkeypatch.setattr(
        settings_store,
        "load_settings",
        lambda: _settings_ns([_source(provider="github_issues", project="")]),
    )
    assert server._ticketing_connected() is False
    monkeypatch.setattr(
        settings_store,
        "load_settings",
        lambda: _settings_ns([_source(provider="github_issues", project="o/r")]),
    )
    assert server._ticketing_connected() is True


def test_caps_ticketing_survives_settings_errors(monkeypatch):
    from backend.config import settings as settings_store

    def _boom():
        raise RuntimeError("corrupt settings")

    monkeypatch.setattr(settings_store, "load_settings", _boom)
    assert server._ticketing_connected() is False


# --------------------------------------------------------------------------- #
# git endpoints refuse cleanly without git (guard runs before instance lookup)
# --------------------------------------------------------------------------- #
def test_git_endpoints_409_without_git(monkeypatch):
    monkeypatch.setattr(server, "git_available", lambda: False)
    calls = [
        ("GET", "/api/instances/nope/diff"),
        ("GET", "/api/instances/nope/file-diff?path=x"),
        ("POST", "/api/instances/nope/commit"),
        ("POST", "/api/instances/nope/push-branch"),
        ("POST", "/api/instances/nope/make-pr"),
        ("POST", "/api/instances/nope/merge-pr"),
    ]
    for method, url in calls:
        r = client.request(method, url, json={} if method == "POST" else None)
        assert r.status_code == 409, url
        assert "git is not installed" in r.json()["error"], url


def test_provisioned_create_400_without_git(monkeypatch):
    monkeypatch.setattr(server, "git_available", lambda: False)
    r = client.post("/api/instances", json={"provisioned": True, "title": "x"})
    assert r.status_code == 400
    assert "git" in r.json()["error"]


def test_init_repo_rejected_without_git(monkeypatch, tmp_path):
    monkeypatch.setattr(git_ops, "git_available", lambda: False)
    monkeypatch.setattr(server, "git_available", lambda: False)
    try:
        server._prepare_plain_repo(str(tmp_path / "newrepo"), init_repo=True)
    except ValueError as err:
        assert "git is not installed" in str(err)
    else:
        raise AssertionError("init_repo without git must raise ValueError")


def test_plain_folder_without_git_disables_git_features(monkeypatch, tmp_path):
    # A plain folder stays a perfectly good workspace: (path, git_enabled=False).
    monkeypatch.setattr(git_ops, "git_available", lambda: False)
    monkeypatch.setattr(server, "git_available", lambda: False)
    d = tmp_path / "plain"
    d.mkdir()
    path, git_enabled = server._prepare_plain_repo(str(d), init_repo=False)
    assert path == str(d.resolve())
    assert git_enabled is False


# --------------------------------------------------------------------------- #
# git_ops helpers degrade (no OSError) when the git binary is gone
# --------------------------------------------------------------------------- #
def test_git_ops_helpers_degrade_without_git(monkeypatch):
    def _no_binary(args, **kw):
        raise FileNotFoundError("git")

    monkeypatch.setattr(git_ops.subprocess, "run", _no_binary)
    git_ops._HAS_ORIGIN_CACHE.clear()
    assert git_ops._has_origin("/tmp/x", force=True) is False
    assert git_ops._is_dirty("/tmp/x") is False
    assert git_ops._has_upstream("/tmp/x") is False
    assert git_ops._git_head_sha("/tmp/x") == ""
    assert git_ops._current_branch("/tmp/x") == ""
    assert git_ops._git_count("/tmp/x", "a..b") is None


# --------------------------------------------------------------------------- #
# Frontend: data-caps hooks, gate messages, and the body-class switch
# --------------------------------------------------------------------------- #
def test_index_has_caps_gates_and_tags():
    html = client.get("/").text
    # Settings screens that need an optional integration declare it and carry
    # a "connect X to get these features" gate.
    assert '"tailscale"' in client.get("/app.js").text
    assert '"data-caps-need"' in client.get("/app.js").text
    assert '"git ticketing"' in client.get("/app.js").text
    assert '"data-caps-gate": "tailscale"' in client.get("/app.js").text
    assert '"data-caps-gate": "git"' in client.get("/app.js").text
    assert '"data-caps-gate": "ticketing"' in client.get("/app.js").text
    # Feature entry points that vanish entirely without git.
    assert 'id: "new-advanced"' in client.get("/app.js").text
    assert 'id: "workspaces-btn"' in client.get("/app.js").text
    # Gate links jump to the screen where the integration is set up.
    assert '"data-goto-screen"' in client.get("/app.js").text


def test_app_js_caps_wiring():
    js = client.get("/app.js").text
    assert "no-tailscale" in js
    for cls in ("no-git", "no-tailscale", "no-ticketing"):
        assert cls in js, cls
    # Every git action funnels through the one guard (buttons, palette, chords).
    assert "function requireGit()" in js
    # The ingestion bar needs a connected ticketing source, and connecting one
    # re-probes caps without a reload.
    assert "caps.ticketing" in js
    assert "no-ticketing" in js
    # The Diff tab / git menu block only render with git present.
    assert ".git && " in js


def test_style_css_caps_rules():
    css = client.get("/style.css").text
    assert 'body.no-git [data-caps~="git"]' in css
    assert 'body.no-tailscale [data-caps~="tailscale"]' in css
    assert 'body.no-ticketing [data-caps~="ticketing"]' in css
    assert ".caps-gate" in css
