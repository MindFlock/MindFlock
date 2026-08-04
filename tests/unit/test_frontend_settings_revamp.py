"""Settings revamp: dedicated Notifications / Connections / IDE / Security
screens, browser-notification opt-in, the budget-lock overlay, and the removals
(sidebar checkbox, foreign-repo chip, pane maximize, repo-path line)."""

import re

from starlette.testclient import TestClient

from backend.web import server

client = TestClient(server.app)


def test_every_settings_screen_leads_with_a_heading():
    """Cohesion: each settings screen opens with a section-title heading, so no
    screen starts abruptly on a form field."""
    html = client.get("/").text
    # Each screen section: capture its inner HTML up to the next section/close.
    js = client.get("/app.js").text
    # Every screen component leads with a .set-section-title heading — the React
    # bundle carries at least one per screen (14 since ticketing / PR review /
    # git issues became Intake tabs, where the tab strip is the heading).
    assert js.count("set-section-title") >= 14, "screens are missing headings"


def test_switch_flips_only_on_the_switch_not_the_whole_row():
    """Correctness: clicking a switch row's label text must NOT flip the toggle —
    only the switch itself. So the row is a <div> and the .ca-switch is its own
    <label> (which still gives the slider its click-to-toggle). The inverse of
    the original layout, where the whole row was one <label>."""
    js = client.get("/app.js").text
    assert "set-switch-row" in js
    # The switch keeps its click target: every .ca-switch renders as a <label>…
    assert re.search(
        r'jsxs?\("label",\s*\{\s*className:\s*"ca-switch"', js
    ), "the .ca-switch is no longer a <label> — the slider won't toggle on click"
    # …and none renders as a bare span/div (which wouldn't toggle at all).
    assert not re.search(
        r'jsxs?\("(?:span|div)",\s*\{\s*className:\s*"ca-switch"', js
    ), "a .ca-switch is a span/div — the slider won't toggle on click"
    # No switch row is a <label> any more: a row-wide label flips the toggle
    # from anywhere on the row, which is exactly the behaviour being removed.
    assert not re.search(
        r'jsxs?\("label",\s*\{[^}]*set-switch-row', js
    ), "a set-switch-row is a <label> — clicking the row text flips the toggle"


def test_forced_start_failure_is_surfaced_in_notifications():
    """A forced PR review / ticket start that dies during background provisioning
    (clone, comment fetch) only emits session.create_failed. The bell must map it
    to a visible notification — without a case it was dropped, so the user saw an
    optimistic 'starting…' toast and then nothing (the coworker's silent review)."""
    js = client.get("/app.js").text
    assert "session.create_failed" in js
    assert "couldn't start" in js


def test_settings_has_new_screens():
    html = client.get("/").text
    js = client.get("/app.js").text
    for screen in ("connections", "notifications", "ide", "security"):
        assert '"%s"' % screen in js, screen
    # Auth-mode picker bound to the generic settings wiring.
    assert '"auth_mode"' in js
    # Browser (Chrome) notification opt-in.
    assert '"notif-browser-toggle"' in client.get("/app.js").text


def test_notify_addon_exposes_enable_api():
    js = client.get("/addons/notify.js").text
    assert "window.mindflockAddons.notify.enable" in js
    assert "mf-notify-state" in js  # sync event between the bar + Settings toggle


def test_connections_render_inline_in_settings():
    # Connections is an inline settings screen now — no separate modal module.
    html = client.get("/").text
    assert '"settings-connections-list"' in client.get("/app.js").text
    assert '"connections"' in client.get("/app.js").text
    assert (
        "settings-open-connections" not in client.get("/app.js").text
    )  # old "open manager" button gone
    js = client.get("/app.js").text
    assert "conn-configure" in js
    assert '"/api/connections' in js  # reads the status endpoint
    # The old modal module is removed entirely.
    assert client.get("/addons/connections.js").status_code == 404


def test_system_logs_screen_present_and_wired():
    """Settings → System logs: nav item, screen markup, JS loader + endpoint."""
    html = client.get("/").text
    assert "System logs" in client.get("/app.js").text  # nav item
    assert "logs-toolbar" in client.get("/app.js").text
    for el in ('"logs-view"', '"logs-source"', '"logs-refresh"', '"logs-follow"'):
        assert el in client.get("/app.js").text, el
    js = client.get("/app.js").text
    assert "/api/logs?name=" in js
    assert '"/api/logs' in js
    assert "Open as pane" in js  # wired into nav switch


def test_system_logs_path_is_clickable_to_reveal_or_copy():
    """The selected log's full path renders as a clickable control — reveal in
    Finder inside the desktop shell (window.mfshell.showItem), copy otherwise —
    so users can jump to or grab the file when filing a bug."""
    js = client.get("/app.js").text
    assert '"logs-path"' in js
    assert "showItem" in js  # the desktop reveal bridge
    css = client.get("/style.css").text
    assert ".logs-path" in css


def test_settings_nav_scrolls_so_all_items_reachable():
    """The settings nav column must scroll independently, else the bottom items
    (System logs, Advanced) get clipped on a short window."""
    css = client.get("/style.css").text
    nav = css.split("#settings-nav {")[1].split("}")[0]
    assert "overflow-y: auto" in nav and "min-height: 0" in nav


def test_ctrl_r_reloads_page():
    """Ctrl/Cmd+R reloads the app via a capture-phase handler (wins over the
    terminal's reverse-i-search)."""
    js = client.get("/app.js").text
    assert "location.reload()" in js
    assert "reverse-i-search" in js  # the handler's rationale comment


def test_terminal_passes_zoom_keys_to_native_zoom():
    """Zoom uses the browser's native (centered) zoom: the terminal's custom key
    handler returns false for Ctrl/Cmd +/-/0 (matched on .code, so zoom-in via
    Ctrl+Shift+= works too) instead of sending them to the shell. The rejected
    CSS-zoom approach (which shifted the layout off-center) must NOT be present."""
    js = client.get("/app.js").text
    assert 'c === "Equal"' in js and 'c === "Minus"' in js and 'c === "Digit0"' in js
    assert "NumpadAdd" in js
    assert "documentElement.style.zoom" not in js  # no CSS zoom


def test_shortcuts_sheet_documents_zoom_and_reload():
    """The '?' keyboard-shortcuts sheet lists zoom in/out/reset and reload so
    they're discoverable."""
    js = client.get("/app.js").text
    assert '"Zoom in (desktop app)"' in js and '"Zoom out (desktop app)"' in js
    assert '"Reset zoom (desktop app)"' in js
    assert '"Reload the app (when a terminal isn’t focused)"' in js
    assert '"Reload the desktop shell"' in js


def test_system_logs_pane_opens_from_settings_only():
    """The System logs pane opens from Settings → System logs ("Open as pane"),
    NOT a top-level sidebar button — it should stay out of the way."""
    html = client.get("/").text
    assert '"logs-open-pane"' in client.get("/app.js").text  # the Settings launcher
    assert "syslogs-btn" not in client.get("/app.js").text  # no up-front sidebar button
    js = client.get("/app.js").text
    for sym in ("syslogs-pane", "syslogs-view", "/api/logs?name="):
        assert sym in js, sym
    assert '"grid-row"' in js  # opens as a grid pane like the others
    css = client.get("/style.css").text
    assert ".syslogs-view" in css


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


def test_system_logs_has_copy_button():
    """A Copy-log button copies the shown tail to the clipboard."""
    js = client.get("/app.js").text
    assert '"logs-copy"' in js


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


def test_app_js_budget_lock_overlay_wired():
    js = client.get("/app.js").text
    assert '"budget-lock"' in js
    assert "/budget/raise" in js
    assert "Budget reached" in js


def test_app_js_first_run_gate_uses_onboarded():
    js = client.get("/app.js").text
    assert "onboarded" in js
    assert "mf_setup_done" in js


def test_style_has_budget_lock_rules():
    css = client.get("/style.css").text
    for sel in (".budget-lock", ".budget-lock .bl-card", ".budget-lock .bl-dur"):
        assert sel in css, sel


def test_app_js_provider_login_ui_removed():
    """The one-click provider login flow is gone: each CLI prompts for sign-in
    itself, so the UI carries no login terminal, login modal, or the now-dead
    absolutePath branch of makeTerm.connect()."""
    js = client.get("/app.js").text
    # No login-terminal / login-close wiring and no login modal remain.
    for gone in (
        "makeLoginTerm",
        "/login-terminal",
        "/login-close",
        "ProviderLoginModal",
        "loginFor",
    ):
        assert gone not in js, "leftover login-flow reference: %s" % gone
    # makeTerm's connect() no longer branches on absolutePath — the instance
    # terminal path is built unconditionally from /api/instances/.
    assert 'absolutePath ?? "/api/instances/"' not in js
    assert '"/api/instances/" + encodeURIComponent(title) + wsPath' in js


def _screen_keys(js: str) -> list[str]:
    """Ordered list of settings-screen keys from the SCREENS array."""
    block = js.split("const SCREENS = [", 1)[1].split("];", 1)[0]
    return re.findall(r'key: "([^"]+)", label:', block)


def test_settings_screen_labels_renamed():
    """The coding + providers screens are relabelled to 'Agent CLI' / 'Agent
    providers' (keys unchanged so deep-links still resolve)."""
    js = client.get("/app.js").text
    assert '{ key: "coding", label: "Agent CLI"' in js
    assert '{ key: "providers", label: "Agent providers"' in js
    # The old labels are gone.
    assert 'label: "Coding CLI"' not in js
    assert 'label: "Providers"' not in js


def test_settings_screen_order_moves_appearance_mobile_to_end():
    """Appearance + Mobile now sit after Security (near the end), not up top."""
    keys = _screen_keys(client.get("/app.js").text)
    for k in ("security", "appearance", "mobile", "doctor"):
        assert k in keys, k
    assert (
        keys.index("security")
        < keys.index("appearance")
        < keys.index("mobile")
        < keys.index("doctor")
    )
    # Appearance/Mobile trail General rather than following it immediately.
    assert keys.index("appearance") > keys.index("general") + 1


def test_coding_cli_reads_status_and_persists_default_correction():
    """Settings → Agent CLI reads /api/providers/status (carries `installed`),
    lists only installed CLIs as default candidates, and persists a fallback
    when the stored default is missing."""
    js = client.get("/app.js").text
    assert '"/api/providers/status"' in js  # not /manage, so `installed` is present
    assert "filter((p) => p.installed)" in js  # default candidates = installed CLIs
    # A missing stored default falls back to the first installed CLI AND persists
    # the correction so the default is never a CLI that isn't there.
    assert 'saveField("coding_cli", "default_provider"' in js
    assert "installed[0]" in js


def test_providers_screen_has_no_login_controls():
    """Settings → Agent providers shows install status only — no 'Log in' button
    or login modal; the header is relabelled."""
    js = client.get("/app.js").text
    assert "Agent providers" in js
    # Install command + installed status still render.
    assert "install_hint" in js
    assert "not installed" in js
    # No login affordance survives on this screen.
    assert "ProviderLoginModal" not in js
    assert "login-terminal" not in js


def test_new_session_ctrl_enter_submits_at_dialog_level():
    """Ctrl/Cmd+Enter submits from anywhere in the New-session dialog: the
    handler lives on the dialog div's onKeyDown (not the prompt textarea), and
    Escape still closes the folder browser overlay before the dialog."""
    js = client.get("/app.js").text
    # The submit shortcut is wired into the dialog's keydown handler.
    marker = 'e.key === "Enter" && (e.ctrlKey || e.metaKey)'
    assert marker in js
    i = js.index(marker)
    handler = js[i - 300 : i + 120]
    assert "submit();" in handler  # Ctrl/Cmd+Enter submits
    # ...within the same keydown that closes the browser overlay first, then the
    # dialog — i.e. the dialog-level handler, not the prompt textarea's.
    assert 'e.key === "Escape"' in handler
    assert "browserOpen" in handler and "closeDialog()" in handler
    # The prompt textarea carries no onKeyDown, so a plain Enter still newlines.
    prompt = js.split('id: "new-prompt"', 1)[1][:400]
    assert "onKeyDown" not in prompt
    # The hint advertises the shortcut.
    assert "Ctrl+Enter creates" in js


# --------------------------------------------------------------------------- #
# Settings panels (tickets / open PRs / open issues) held in the query cache   #
# --------------------------------------------------------------------------- #
# The dialog unmounts on close and each screen unmounts on switch, so per-screen
# useState meant every visit started from an empty panel plus a slow upstream
# fan-out. These pin the client half of the stale-while-revalidate contract the
# server's ``_cached_fanout`` implements (see test_server_routes).
def test_settings_panels_read_from_the_shared_query_cache():
    js = client.get("/app.js").text
    # One key→path map, so no screen can drift from its endpoint.
    assert "function usePanelQuery(key)" in js
    assert 'tickets: "/api/tickets"' in js
    assert '"github-prs": "/api/github/prs"' in js
    assert '"github-issues": "/api/github/issues"' in js
    # All three screens read through it with those keys…
    for key in ('usePanelQuery("tickets")', 'usePanelQuery("github-prs")'):
        assert key in js, key
    assert 'usePanelQuery("github-issues")' in js
    # …and none of them keeps the list in component state any more (that state
    # is exactly what the dialog's unmount used to throw away).
    for gone in (
        "setPrs(",
        "setIssues(",
        "setTickets(",
        "setPrsNote(",
        "setIssuesNote(",
    ):
        assert gone not in js, gone


def test_settings_panel_keeps_its_rows_while_refetching():
    """Reopening a screen shows the last list immediately instead of blanking —
    and a failed refetch leaves those rows up with an error banner (the old
    handlers set the list to [] on failure)."""
    js = client.get("/app.js").text
    panel = js.split("function usePanelQuery(key)", 1)[1][:600]
    assert "placeholderData: (prev) => prev" in panel
    # The failure path is a banner, not an emptied panel.
    assert "Could not list PRs: " in js
    assert "Could not list issues: " in js
    assert "Could not list tickets: " in js
    # Each panel distinguishes "first load" from "reloading what you can see".
    # One shared implementation now (intake/kit.tsx `panelNote`), driven three
    # times — the three hand-rolled copies were the drift risk this replaced.
    assert js.count('? "Refreshing…" : "Loading…"') == 1
    assert js.count("panelNote({") == 3


def test_settings_panel_refresh_asks_the_server_to_skip_its_cache():
    """The Refresh button (and a force start, which re-lists afterwards) means a
    real upstream sweep: ``?fresh=1`` with the client cache bypassed."""
    js = client.get("/app.js").text
    # Sliced (not a bare `in js`) to prove these belong to the hook itself.
    panel = js.split("function usePanelQuery(key)", 1)[1][:1600]
    assert 'PANELS[key] + "?fresh=1"' in panel
    assert "staleTime: 0" in panel  # …and not answered from the query cache
    assert '"gh-prs-refresh"' in js
    # A force start does NOT force a sweep: has_session is annotated live on
    # every response, so the cheap re-list already shows the new session.
    # PR review and Git issues call these inside the shared WorkItemRow's
    # onStart (screens/automation.tsx) rather than through an `onStarted` prop,
    # so assert the re-list is still wired, not how it is passed. The negative
    # below is the actual contract: the refresh-only `loadOpen*` sweep must not
    # be what a force start triggers.
    assert "relistPrs" in js
    assert "relistIssues" in js
    assert "relistTickets" in js
    for sweep in ("loadOpenPrs", "loadOpenIssues"):
        # Referenced exactly once — by the Refresh button, not by a force start.
        assert js.count(sweep) == 2, f"{sweep} should only be the Refresh handler"


def test_settings_panel_repolls_only_while_the_server_reports_stale():
    """``stale`` in the payload is the server's "I'm already replacing this" —
    the client comes back for the fresh copy shortly, and otherwise does not
    poll the fan-out at all."""
    js = client.get("/app.js").text
    assert "PANEL_STALE_RETRY_MS = 2e3" in js
    assert "PANEL_STALE_RETRY_MS : false" in js  # not stale → no interval
    assert ".stale) ? PANEL_STALE_RETRY_MS" in js  # driven by the payload flag
    # The client's freshness window matches the server's TTL: inside it, a
    # mount is answered from the query cache instead of making a round trip to
    # be told the same thing.
    assert "PANEL_STALE_MS = 2e4" in js  # milliseconds, the same 20s window
    assert server._FANOUT_TTL == 20.0, "keep PANEL_STALE_MS in step with the TTL"
    # Panels must outlive the default 5min gcTime, or "cached across opens"
    # quietly becomes a cold load again after a short break.
    assert "PANEL_GC_MS = 60 * 6e4" in js
    assert 2.0 < server._FANOUT_TTL


def test_opening_work_warms_all_three_panels():
    """Switching to a tab shouldn't start with a spinner: the Intake dialog
    prefetches all three on open, once per open (not per render), and a panel
    whose data is still fresh is skipped. It is also what fills in the tab
    strip's counts before you get there."""
    js = client.get("/app.js").text
    assert "if (open) prefetchIntakePanels();" in js
    dialog = js.split("if (open) prefetchIntakePanels();", 1)[1][:80]
    assert "[open]" in dialog  # the effect keys on `open` alone
    prefetch = js.split("function prefetchIntakePanels()", 1)[1][:400]
    assert "Object.keys(PANELS)" in prefetch  # every panel, no hand-kept list
    assert "staleTime: PANEL_STALE_MS" in prefetch  # a no-op while still fresh
    # `void`: an unconfigured integration 502s here, and a warm-up must not
    # surface as an unhandled rejection.
    assert "void queryClient.prefetchQuery(" in prefetch


def test_ticket_source_errors_outrank_the_refresh_note():
    """A per-source failure is the useful message, so it wins over
    "Refreshing…" — including on a stale payload: the server can hand back a
    cached list that carries source errors. The precedence lives in the shared
    `panelNote` helper (a `detail` outranks the progress note) rather than in
    each panel's own ternary."""
    js = client.get("/app.js").text
    note = js.split("function panelNote(opts)", 1)[1][:400]
    assert "opts.error || opts.detail" in note
    assert "opts.detail ||" in note
    # …and the tickets panel is what passes the per-source errors in as detail.
    assert '(sourceLabels[s] || s) + ": " + e' in js
    assert 'join(" · ")' in js
