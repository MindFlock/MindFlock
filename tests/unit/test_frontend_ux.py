"""Progressive-disclosure UX round: structural contract checks.

Covers the sidebar session filter and the client-side session rename (alias).
Live behaviour verified with CDP; these pin the markup/JS/CSS hooks so they
can't silently regress. Same style as test_frontend_wave4.py.
"""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from backend.web import server
from tests._bundle import in_bundle, squash

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


# --------------------------------------------------------------------------- #
# Drop / paste a file onto a terminal (assistant window included)
# --------------------------------------------------------------------------- #
# The agent CLIs run on this machine and cannot see the browser, so every
# terminal that TAKES INPUT turns a dropped file into an upload and types the
# saved path back into the PTY. The gesture used to live inline in the
# session-terminal factory, which is why the assistant window went without it —
# that reads as broken rather than missing, since the same drop on the pane
# beside it has always worked. It is now one helper (``clipboard.attachFileDrop``)
# with two callers, and these pin the bundle where the two halves meet.


def test_app_js_assistant_window_takes_dropped_files():
    js = client.get("/app.js").text
    # The assistant is a ``useWsTerm`` window, and it is the interactive one.
    assert in_bundle('useWsTerm(hostRef, "/api/assistant/terminal", true)', js)
    # Gated on ``interactive`` — written as a regex because the bundler decides
    # the spacing (and spells the else branch ``void 0``), not us.
    assert re.search(
        r"interactive\s*\?\s*attachFileDrop\(\s*host\s*,\s*term\s*\)",
        squash(js),
    ), "useWsTerm must wire attachFileDrop(host, term) behind the interactive gate"
    assert "Drop or paste a file to hand over its path" in js


def test_app_js_assistant_upload_carries_no_session():
    """The assistant has no worktree, so its uploads are sessionless.

    ``attachFileDrop(host, term)`` — two arguments, no ``session`` — is what
    sends the bytes to ``/api/paste-image`` with no ``?session=``, landing them
    in ``~/.mindflock/pastes`` (shared with the phone UI, and with its
    retention prune) rather than in some session's workspace.
    """
    js = squash(client.get("/app.js").text)
    # ")" right after `term` is what makes this the two-argument CALL rather
    # than a prefix of the helper's own three-parameter signature.
    assert "attachFileDrop(host, term)" in js  # the assistant: no session
    # ...and the ";" keeps this one off that signature, which names `session`.
    assert "attachFileDrop(host, term, session);" in js  # a session terminal


def test_app_js_session_terminal_still_uploads_into_its_workspace():
    js = client.get("/app.js").text
    # The extraction must not have dropped the workspace destination. (The
    # ";" pins the call site: the helper's signature names `session` too.)
    assert in_bundle("attachFileDrop(host, term, session);", js)
    # Session terminals APPEND to an existing title; useWsTerm overwrites its
    # own. Pinned so a future unification of the two is a deliberate edit.
    assert in_bundle('host.title += " · drop / paste files to upload"', js)


def test_app_js_file_drop_is_wired_in_exactly_one_place():
    """One gesture, one implementation — the point of the extraction.

    The old inline copy in the session-terminal factory is gone, so a change to
    the gesture now changes both windows at once and the two cannot drift.
    """
    js = squash(client.get("/app.js").text)
    assert js.count("function attachFileDrop(") == 1
    # Every host-level drag listener belongs to that helper. (The two window-
    # level ones are installGlobalDropGuards', on ``window``.)
    assert js.count('host.addEventListener("dragover"') == 1
    assert js.count('host.addEventListener("drop"') == 1
    # Three mentions total: the definition plus its two callers. Any fourth is
    # an unguarded call site — e.g. a read-only window claiming a drop.
    assert js.count("attachFileDrop(") == 3


def test_app_js_read_only_windows_leave_a_drop_alone():
    """A log tail and a verify watch have no PTY to paste a path into.

    A drag over one must fall through to ``installGlobalDropGuards`` and the
    grid's own pane-rearrange drop handler; the helper's ``stopPropagation`` on
    an interactive host is what keeps a file drop off the pane-move path.
    """
    js = client.get("/app.js").text
    assert in_bundle('useWsTerm(hostRef, "/api/mindflock/logs", false)', js)
    # The only call reachable from useWsTerm is the guarded one (asserted
    # above); nothing wires a drop for the interactive=false windows.
    assert squash(js).count("attachFileDrop(") == 3
    assert in_bundle("function installGlobalDropGuards()", js)


def test_app_js_drop_listeners_are_unwired_on_teardown():
    """The host div is React's and OUTLIVES the effect.

    Without the disposer, every reconnect-triggered re-run would stack another
    set of listeners on the same element and upload each dropped file twice.
    """
    js = squash(client.get("/app.js").text)
    assert "detachFiles?.();" in js
    # ...in the cleanup, just before the socket is closed (that neighbourhood is
    # the cleanup: a bare `ws?.close()` occurs elsewhere in the bundle too).
    after = js[js.index("detachFiles?.();") :][:160]
    assert "ws?.close();" in after, after
    # ...and the effect really does re-run on these, which is why it matters.
    assert re.search(
        r"\[\s*hostRef,\s*wsPath,\s*interactive,\s*reconnect\s*\]", js
    ), "useWsTerm's effect must still depend on wsPath/interactive/reconnect"


def test_style_css_has_the_file_drop_cue():
    css = client.get("/style.css").text
    # The class the helper paints while a file is over a terminal.
    assert ".file-drop" in css


def test_style_css_does_not_restyle_an_alias():
    # A renamed row reads exactly like every other row — no italic, no
    # separate ".aliased" styling (the real title lives in the tooltip).
    assert "aliased" not in client.get("/style.css").text


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
    # Git/workspace options live behind a "Git & workspace" fold, and
    # fillFromTemplate() opens it when a template turns any of them on.
    assert 'id: "new-advanced"' in client.get("/app.js").text
    js = client.get("/app.js").text
    assert '"new-in-place"' in js  # work-in-place toggle
    assert '"new-init-repo"' in js  # git init lives here
    # "Git & workspace" stands OPEN by default — hiding the workspace strategy
    # behind a click had people launching with the wrong one rather than
    # finding it. The prompt and launch-flags folds below it stay closed.
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
