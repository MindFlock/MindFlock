"""Progressive-disclosure UX round: structural contract checks.

Covers the sidebar session filter and the client-side session rename (alias).
Live behaviour verified with CDP; these pin the markup/JS/CSS hooks so they
can't silently regress. Same style as test_frontend_wave4.py.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from backend.web import server

client = TestClient(server.app)


# --------------------------------------------------------------------------- #
# Sidebar filter
# --------------------------------------------------------------------------- #
def test_index_has_sidebar_search():
    html = client.get("/").text
    assert '"sidebar-search"' in client.get("/app.js").text
    assert '"session-filter"' in client.get("/app.js").text


def test_app_js_filter_wiring():
    js = client.get("/app.js").text
    assert "session-filter" in js
    assert "function matchesFilter(" in js
    # Progressive: box hidden until enough sessions (or a filter is active).
    assert "SEARCH_MIN" in js
    # "/" focuses the filter via the global keymap (guarded: not while typing).
    assert 'key: "/"' in js
    # Filtering only re-renders the sidebar (grid untouched).
    assert "matchesFilter" in js


def test_style_css_has_filter_rules():
    css = client.get("/style.css").text
    for sel in ("#sidebar-search", "#session-filter", ".filter-empty"):
        assert sel in css, sel


# --------------------------------------------------------------------------- #
# Session rename (alias)
# --------------------------------------------------------------------------- #
def test_app_js_rename_wiring():
    js = client.get("/app.js").text
    assert "Rename session" in js
    assert "mf_aliases" in js
    # Aliases persist client-side; the real title still keys operations.
    assert "mf_aliases" in js
    # Rename surfaced in the row menu and the palette.
    assert "Rename…" in js
    assert "Rename… — " in js
    # Sidebar rows carry the stable title as a data attr (so focus/lookup don't
    # depend on the displayed text, which may be an alias). Rows are keyed —
    # built once by _createSidebarRow(title) and reused across polls.
    assert '"data-title"' in js


def test_style_css_has_alias_rule():
    assert ".title.aliased" in client.get("/style.css").text


# --------------------------------------------------------------------------- #
# Keyboard shortcut cheat-sheet
# --------------------------------------------------------------------------- #
def test_app_js_shortcut_sheet():
    js = client.get("/app.js").text
    assert "shortcuts-overlay" in js and "Keyboard shortcuts" in js
    # '?' toggles it (when not editing) via the global keymap; palette action too.
    assert 'key: "?"' in js
    assert "toggleShortcuts()" in js
    assert "Keyboard shortcuts" in js
    # The sheet is generated from the live keymap + chord table, not hand-kept.
    assert "KEYMAP" in js and "CHORDS" in js
    # Built with textContent (no HTML injection of shortcut strings), one
    # <kbd> chip per combo in an aligned two-column grid.
    assert "kbd-sep" in js
    assert "shortcut-desc" in js
    assert "shortcut-keys" in js


def test_style_css_has_shortcut_rules():
    css = client.get("/style.css").text
    for sel in (".shortcuts-overlay", ".shortcuts-card", ".shortcut-row"):
        assert sel in css, sel


# --------------------------------------------------------------------------- #
# Undo toasts (reversible hide)
# --------------------------------------------------------------------------- #
def test_app_js_hide_offers_undo():
    js = client.get("/app.js").text
    # Single + bulk hide both surface a clickable Undo toast.
    assert "— click to undo" in js
    assert "— click to undo" in js


# --------------------------------------------------------------------------- #
# Favicon attention dot
# --------------------------------------------------------------------------- #
def test_app_js_favicon_attention():
    js = client.get("/app.js").text
    assert "function updateFavicon(" in js
    # Driven by the same clarify-count that badges the title.
    assert "updateFavicon(n)" in js
    # Only redraws when the dot state changes (not every poll).
    assert "faviconState" in js


# --------------------------------------------------------------------------- #
# Pane focus / maximize mode
# --------------------------------------------------------------------------- #
def test_app_js_has_no_maximize_ui():
    # The pane maximize/fullscreen affordance was removed — it added no value
    # in the single-/few-pane layouts people actually use.
    js = client.get("/app.js").text
    assert 'class="maximize-btn"' not in js  # no header button
    assert 'head.addEventListener("dblclick"' not in js  # no double-click-to-maximize
    assert "Maximize — " not in js  # no command-palette action


# --------------------------------------------------------------------------- #
# New-session dialog progressive disclosure
# --------------------------------------------------------------------------- #
def test_new_dialog_folds_git_workspace_options():
    html = client.get("/").text
    # Git/workspace options live behind a "More options" fold — the defaults
    # suit the common case, and fillFromTemplate() auto-opens the fold when a
    # template turns any of them on. Name/folder/prompt stay up top, unfolded.
    assert 'id: "new-advanced"' in client.get("/app.js").text
    js = client.get("/app.js").text
    assert '"new-in-place"' in js  # work-in-place toggle
    assert '"new-init-repo"' in js  # git init lives here
    # "More options" stands OPEN by default — hiding the workspace strategy
    # behind a click had people launching with the wrong one rather than
    # finding it. The launch-flags fold below it stays closed.
    assert "nf-advanced" in js


def test_new_dialog_agent_is_a_select_from_providers():
    html = client.get("/").text
    # Agent is a dropdown (was a free-text "Program" input), with a link to
    # manage the provider list in Settings → Coding CLI.
    assert 'id: "new-program"' in client.get("/app.js").text
    assert '"new-agent-manage"' in client.get("/app.js").text
    js = client.get("/app.js").text
    assert "/api/providers/manage" in js  # fills the dropdown
    assert '"/api/providers/manage"' in js  # from the configured providers
    assert '"coding"' in js  # Manage link jumps to Coding CLI


def test_new_dialog_has_templates_strip_and_drops_git_requirement():
    html = client.get("/").text
    # Templates are part of + New (a "Start from a template" strip).
    assert '"new-templates"' in client.get("/app.js").text
    assert '"new-templates-list"' in client.get("/app.js").text
    assert '"new-templates-manage"' in client.get("/app.js").text
    # A plain folder works — git is optional (offered as an init-repo toggle,
    # never required).
    assert '"new-init-repo"' in client.get("/app.js").text
    js = client.get("/app.js").text
    assert "/api/templates" in js  # populates the strip
    assert "fillFromTemplate" in js  # a chip prefills the form
    assert "mindflockAddons" in js  # Manage… opens the editor


def test_queue_panel_shows_order_and_next_marker():
    """The prompt queue reads as an ordered run-list: a status line, position
    numbers, and a "next" marker on the front item."""
    js = client.get("/app.js").text
    assert "queue-status" in js  # what-happens-next status line
    assert "queue-item-pos" in js  # position numbers
    assert "queue-next" in js  # front item marked as next
    # Usage-limit hold: countdown driven by the snapshot's limited_until.
    assert "limited_until" in js
    assert "queue-limited" in js
    css = client.get("/style.css").text
    assert ".queue-item.queue-next" in css
    assert ".queue-status.queue-limited" in css


def test_style_css_has_session_options_rules():
    css = client.get("/style.css").text
    assert ".nf-advanced" in css  # the folded git/workspace options block
    assert ".new-templates" in css  # the template strip


# --------------------------------------------------------------------------- #
# Hidden-session state persists across reloads
# --------------------------------------------------------------------------- #
def test_app_js_hidden_state_persisted():
    js = client.get("/app.js").text
    # Hidden set is loaded from and saved to localStorage.
    assert '"mf_hidden"' in js
    assert "mf_hidden" in js
    assert "setHidden" in js
    # Expanded menus stay transient (not persisted) on purpose.
    assert "inst-actions" in js


# --------------------------------------------------------------------------- #
# Keyed sidebar rendering + visibility-aware polling (P1 perf round)
# --------------------------------------------------------------------------- #
def test_app_js_keyed_sidebar_render():
    js = client.get("/app.js").text
    # Rows persist across polls in a title -> <li> map (like the grid's
    # `panes` map); the 4s poll updates mutable bits in place instead of
    # rebuilding the list (the old innerHTML wipe ate in-flight clicks).
    assert "SidebarRow" in js
    assert "SidebarRow" in js
    assert "stagechip" in js
    # Reordering moves existing nodes instead of recreating them.
    assert '"instance-list"' in js
    # Menu clicks are delegated off the persistent row, keyed by data-act.
    assert "Duplicate session" in js


def test_app_js_terminal_ondata_registered_once():
    js = client.get("/app.js").text
    # onData is bound once per terminal and writes to the CURRENT socket —
    # binding inside connect() leaked one xterm disposable per 2.5s reconnect.
    assert "ws.send(data)" in js


def test_polls_back_off_when_tab_hidden():
    js = client.get("/app.js").text
    # The two 4s polls (/api/instances + MindFlock status) are self-
    # rescheduling timeouts that stretch to 30s while the tab is hidden and
    # refresh immediately on return.
    assert "POLL_VISIBLE_MS" in js and "POLL_HIDDEN_MS" in js
    assert "document.hidden" in js
    assert "setInterval(refreshMindFlock" not in js
    mjs = client.get("/mobile.js").text
    assert "document.hidden" in mjs
    assert "setInterval(poll, 4000)" not in mjs
