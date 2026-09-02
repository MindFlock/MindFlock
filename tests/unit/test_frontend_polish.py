"""UI polish round 3 (sidebar addon bars, header repo line, session-row chips).

Same structural-contract style as test_frontend_wave2.py — the live UI is
verified with screenshots; these pin the CSS rules and markup hooks the fixes
introduced so they can't silently regress.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from backend.web import server

client = TestClient(server.app)


# --------------------------------------------------------------------------- #
# Generic addon bars (core/slots.js) are styled like the bespoke bars.
# --------------------------------------------------------------------------- #


def test_style_css_styles_generic_addon_bars():
    css = client.get("/style.css").text
    # The bar itself mirrors #mindflock-bar / #assistant-bar (padding, border,
    # 12px type) instead of falling back to giant browser defaults.
    for sel in (
        "#addon-bars .addon-bar",
        "#addon-bars .addon-label",
        "#addon-bars .addon-toggle",
    ):
        assert sel in css, sel
    # Toggle states: active (On), hover, and a disabled affordance for
    # unsupported environments.
    assert "#addon-bars .addon-toggle.active" in css
    assert "#addon-bars .addon-toggle:disabled" in css


def test_slots_js_renders_addon_bar_markup():
    js = client.get("/core/slots.js").text
    assert 'bar.className = "addon-bar"' in js
    assert "bar.dataset.addon" in js
    assert 'label.className = "addon-label"' in js


# --------------------------------------------------------------------------- #
# Notify addon toggle: styled + honest about why it can't work.
# --------------------------------------------------------------------------- #


def test_notify_js_toggle_contract():
    js = client.get("/addons/notify.js").text
    # State persists in localStorage; permission is requested lazily on enable.
    assert "mindflock.notify.enabled" in js
    assert "requestPermission" in js
    # notify.js has no visible surface of its own anymore — the on/off toggle
    # lives in the bell dropdown + Settings. It exposes the shared API those
    # UIs drive, and a browser-side denial reports tri-state "blocked".
    assert '"blocked"' in js
    assert "window.mindflockAddons.notify.enable = enable" in js
    assert "window.mindflockAddons.notify.disable = disable" in js
    assert "window.mindflockAddons.notify.state = state" in js
    # Both mirrors (bell + Settings) stay in sync via this broadcast.
    assert "mf-notify-state" in js
    # No-Notification-API environments (plain-http origins) expose an
    # "unsupported" state + the reason WHY, so the toggles render disabled.
    assert "isSecureContext" in js
    assert "unsupported" in js


def test_bell_dropdown_has_notify_toggle():
    """The on/off toggle lives in the bell dropdown head (not the left panel),
    driving the shared notify addon API and syncing via mf-notify-state."""
    js = client.get("/app.js").text
    assert '"notif-toggle"' in js  # rendered in the .notif-head row
    assert "mindflockAddons" in js  # _notifApi() drives the addon
    assert "mf-notify-state" in js  # stays in sync with Settings


# --------------------------------------------------------------------------- #
# Session rows: title / foreign glyph / stage chip never collide.
# --------------------------------------------------------------------------- #


def test_session_row_chip_layout_rules():
    css = client.get("/style.css").text
    # Title keeps a legible floor instead of collapsing to zero width.
    assert "min-width: 34px" in css
    # The stage chip ellipsizes inside its own box instead of overlapping.
    assert "max-width: 96px" in css


def test_app_js_foreign_chip_removed():
    js = client.get("/app.js").text
    # The "from another repo" ⇄ chip was removed (not wanted).
    assert '">⇄</span>' not in js
    assert "From another repo: " not in js
    # Truncated titles still reveal the full name on hover (alias-aware: an
    # aliased row shows "alias · real-title", a plain row shows the real title).
    assert "aliased" in js


# --------------------------------------------------------------------------- #
# Provisioning fields: the frontend reads/sends only the current field names,
# the copy is repo-neutral, and no sitecheck-era spellings remain anywhere.
# --------------------------------------------------------------------------- #


def test_app_js_uses_new_provisioning_fields():
    js = client.get("/app.js").text
    assert "provisioning_available" in js
    assert "body.provisioned = true" in js
    assert "body.workspace_strategy" in js
    assert "e.provisioned" in js  # recently-closed badge
    # Old spellings are gone from all API wiring.
    for old in (
        "sitecheck_available",
        "sitecheck_mode",
        "body.sitecheck",
        "inst.sitecheck",
        "e.sitecheck",
    ):
        assert old not in js, old


def test_app_js_has_no_sitecheck_mentions():
    js = client.get("/app.js").text
    assert "sitecheck" not in js


def test_new_dialog_provision_copy_and_ids():
    html = client.get("/").text
    assert '"new-provision-row"' in client.get("/app.js").text
    assert '"new-provision"' in client.get("/app.js").text
    assert '"provision-opts"' in client.get("/app.js").text
    assert '"new-workspace-strategy"' in client.get("/app.js").text
    assert "Provision workspace" in client.get("/app.js").text
    assert (
        "run repo setup" in client.get("/app.js").text
        and "warm test caches" in client.get("/app.js").text
    )
    assert "Workspace strategy" in client.get("/app.js").text
    assert "shared base clone (worktree)" in client.get("/app.js").text
    assert "full clone" in client.get("/app.js").text
    assert "sitecheck" not in html.lower()
    css = client.get("/style.css").text
    assert "#provision-opts" in css
    assert "#sitecheck-opts" not in css


def test_backend_selector_removed():
    # There is no per-session backend selector in the UI.
    html = client.get("/").text
    assert "Backend for new sessions" not in html
    assert 'id="backend-mode"' not in html


def test_api_config_has_no_deprecated_alias():
    cfg = client.get("/api/config").json()
    assert "provisioning_available" in cfg
    assert "sitecheck_available" not in cfg


def test_workspace_manager_copy_is_repo_neutral():
    js = client.get("/app.js").text
    assert "shared base clone" in js
    # The disk manager's per-row confirmations went with it — the merged
    # Recently-closed page does not show protected dirs at all. What replaced
    # them is the unused-worktree sweep's copy, which has to be just as
    # repo-neutral: it names kinds of directory, never a repo or a cache.
    assert "never a repository" in js
    assert "testmon refresher" not in js


def test_provisioning_placeholder_hint_is_repo_neutral():
    js = client.get("/app.js").text
    assert "The first provisioned run clones the base repo" in js
    assert "sitecheck-bot" not in js


# --------------------------------------------------------------------------- #
# Pane header: a long session title must cost its OWN characters, never the
# controls beside it.
# --------------------------------------------------------------------------- #


def test_pane_header_shrinks_the_title_not_the_controls():
    """A 60-character session name used to slice the Commit button, the fast-track
    toggle and the live-step indicator off the right edge, because `.actions` was
    made shrinkable so a verbose step label could not push the usage chip out. The
    title is a LABEL whose full text is one hover away; those are CONTROLS."""
    css = client.get("/style.css").text
    # The title is the designated shrink target — factor 3 against the context
    # line's 1 — and may truncate hard rather than hold its width.
    title = _rule(css, ".pane-head .title")
    assert "flex: 0 3 auto" in title, title
    assert "text-overflow: ellipsis" in title
    # EVERY .pane-head .actions rule must refuse to shrink; one that yields is
    # what clipped the controls.
    blocks = _rules(css, ".pane-head .actions")
    assert blocks, "expected .pane-head .actions rules in the built CSS"
    for b in blocks:
        assert "flex: none" in b, b
        assert "flex: 0 1 auto" not in b, b
    # Its container can no longer shrink, so the step label bounds itself.
    assert "max-width: 15ch" in _rule(css, ".pane-head .actions .stepnow-text")


def _rules(css: str, selector: str) -> list:
    """Every top-level rule body for an exact selector in the (unminified) CSS."""
    out = []
    needle = "\n" + selector + " {"
    at = css.find(needle)
    while at != -1:
        end = css.find("\n}", at)
        out.append(css[at : end + 2])
        at = css.find(needle, end)
    return out


def _rule(css: str, selector: str) -> str:
    got = _rules(css, selector)
    assert got, "no rule found for " + selector
    return got[0]


def test_the_ingestion_dot_cannot_be_deformed_by_the_global_error_rule():
    """The red state used to carry a bare `error` class. `.error` is the app-wide
    rule for error TEXT and sets `min-height: 16px`; `.dc-dot` sets `height: 9px`
    but no min-height, so the global won uncontested and rendered a 9x16 OVAL — in
    exactly one state, because it was the only one borrowing that name."""
    js = client.get("/app.js").text
    css = client.get("/style.css").text
    # The state class is scoped, and no bar emits a bare "error" state any more.
    assert "dc-error" in js
    assert '"dc-dot " + (netIssue ? "error"' not in js
    # The styled selector matches the scoped name.
    assert ".dc-dot.dc-error" in css
    # And the circle pins its own box, so a future global leak cannot deform it.
    # Every automation bar shares the one rule, so the selector list grows by one
    # line each time a bar is added (Verify was the fourth). Spelled out in full
    # rather than matched loosely: the point of the assertion is that ALL of them
    # pin the box, and a fuzzy match would pass while a new bar's dot went
    # unpinned.
    dot = _rule(
        css,
        "#mindflock-bar .dc-dot,\n#pr-review-bar .dc-dot,\n"
        "#git-issue-bar .dc-dot,\n#verify-bar .dc-dot",
    )
    for decl in ("min-height: 0", "min-width: 0", "border-radius: 50%"):
        assert decl in dot, decl


def test_a_booting_window_can_be_dragged():
    """Provisioning is the LONGEST a window is ever in one state — a cold clone runs
    for minutes — and it was the one state you could neither move nor drop onto, so
    arranging the grid meant waiting it out. The drag is pure layout; it touches
    nothing about the session."""
    js = client.get("/app.js").text
    # The loading pane renders the same grip affordance as every other pane.
    assert "loading-pane" in js
    # Both halves of the drag contract reach it: the header starts a drag, the
    # section accepts a drop. Asserted via the rendered class + the shared handler
    # names surviving into the bundle.
    assert "Drag to move this window" in js
    # A dragged loading pane dims like any other (the .dragging class is applied).
    assert "pane loading-pane" in js
