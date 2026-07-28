"""Frontend wave-2 wiring (A4, C2, D2/D5, E1–E3): structural contract checks.

Same style as test_frontend_slots.py — the live UI is verified manually in a
browser; these pin the load order, the public window.mindflock contract, and
the markup hooks app.js relies on.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from backend.web import server

client = TestClient(server.app)


def test_events_js_serves():
    r = client.get("/core/events.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]
    assert len(r.content) > 0


def test_events_js_public_contract():
    js = client.get("/core/events.js").text
    # The documented window.mindflock extension API (matched by docs/extensions.md).
    assert "window.mindflock = window.mindflock || {}" in js
    assert "subscribe" in js and "onStatus" in js
    assert "lastSeq" in js and "connected" in js
    assert "mf.sessions" in js and "__setSessions" in js
    assert "/api/events" in js
    assert "?since=" in js  # reconnect dedupe


def test_index_loads_events_before_app_and_slots():
    html = client.get("/").text
    assert "/core/events.js" in html
    # app.js is a deferred module and slots.js is injected after first paint,
    # so the classic events.js script still EXECUTES first.
    assert 'type="module"' in html and "/app.js" in html
    assert "/core/slots.js" in client.get("/app.js").text


def test_index_has_wave2_markup():
    html = client.get("/").text
    # E1 connection-lost banner
    assert '"conn-banner"' in client.get("/app.js").text
    # C2 setup dialog (reopened via the doctor-failure chip + command palette;
    # the old footer "Setup" button was deliberately removed)
    assert '"setup-dialog"' in client.get("/app.js").text
    # E3 settings test buttons (gh/agent are static; ticketing tests are
    # per-source, rendered inside the ticketing-sources list at runtime)
    for el_id in (
        "gh-test-btn",
        "agent-test-btn",
        "ticketing-sources",
        "ticketing-add",
    ):
        assert f'"{el_id}"' in client.get("/app.js").text, el_id
    # Doctor screen in Settings
    assert '"doctor"' in client.get("/app.js").text
    assert '"settings-doctor"' in client.get("/app.js").text
    # D2 IDE picker: select + custom-command escape hatch (still persisted via
    # the generic data-group/data-field settings path)
    assert '"ide-select"' in client.get("/app.js").text
    assert '"ide-custom-row"' in client.get("/app.js").text
    assert '"ide_command"' in client.get("/app.js").text


def test_app_js_uses_new_apis():
    js = client.get("/app.js").text
    # D5: the open-in-IDE action hits the primary /ide route
    assert '"/ide"' in js
    # Activity is folded into the single phase chip (running/clarify/idle),
    # with client-side debounce so working↔idle flips don't flicker the label.
    assert "effectiveActivity" in js and "s-running" in js
    # Usage-limit gets its own (red) pill, distinct from clarify, so a
    # credit-limit menu reads as auto-resuming rather than "needs your answer".
    assert "s-limit" in js
    assert "noteActivity" in js and "forceActivity" in js
    # E2: event-driven notifications + tab-title badge
    assert "session.activity_changed" in js
    assert "session.stage_changed" in js
    assert "updateTitleBadge" in js
    # C2: doctor-backed first-run checklist
    assert "/api/doctor" in js
    assert "Create your first session" in js
    # E3: account test endpoints
    for path in (
        "/api/settings/test/shortcut",
        "/api/settings/test/github",
        "/api/settings/test/agent",
    ):
        assert path in js, path
    # D2: detected-IDE picker
    assert "/api/ides" in js
    # E1 + events bus feed: keep the poll, track failures, feed sessions()
    assert "__setSessions" in js
    assert "conn-banner" in js


def test_style_css_has_wave2_rules():
    css = client.get("/style.css").text
    for sel in (
        ".stagechip.s-running",
        ".stagechip.s-limit",
        "#conn-banner",
        ".setup-card",
        ".doctor-check",
        ".test-result",
    ):
        assert sel in css, sel


# --------------------------------------------------------------------------- #
# Wave 3 (F3/F4/F5/F8): same structural-contract style as above.
# --------------------------------------------------------------------------- #


def test_config_has_no_repo_root():
    # F5 was retired: the server has no meaningful "managed repo" (it's a
    # global tool whose cwd is arbitrary; sessions pick their own repos), so
    # /api/config no longer advertises one.
    r = client.get("/api/config")
    assert r.status_code == 200
    assert "repo_root" not in r.json()


def test_index_has_wave3_markup():
    html = client.get("/").text
    # F5: the managed-repo path line under the title was removed (visual clutter).
    assert 'id="repo-root"' not in html
    # F8: dismissible doctor warning chip
    assert '"doctor-warn"' in client.get("/app.js").text
    assert '"doctor-warn-open"' in client.get("/app.js").text
    assert '"doctor-warn-dismiss"' in client.get("/app.js").text


def test_app_js_wave3_wiring():
    js = client.get("/app.js").text
    # F3: toast published on the public extension API
    assert ".toast = toast" in js
    # F4: merged-PR detection keys off leaving the "pr" stage; the dead
    # new==="merged" listener branch is gone (the server never emits it).
    assert 'env.old === "pr"' in js
    assert 'env.new === "merged"' not in js
    # F5 retired: neither the managed-repo label nor the foreign-repo ⇄ chip remain.
    assert "repo_root" not in js
    assert "foreign-chip" not in js
    # F8: doctor warning chip (lazy re-check) + inline Shortcut token input
    assert "doctor-warn" in js
    assert "setup-shortcut-token" in js


def test_style_css_has_wave3_rules():
    css = client.get("/style.css").text
    for sel in ("#doctor-warn", ".setup-shortcut-token"):
        assert sel in css, sel
