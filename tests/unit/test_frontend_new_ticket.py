"""New → Ticket, and the folder row that replaced a dead window.prompt.

``backend/web/static/{app.js,style.css}`` are committed build output — neither
``uv build`` nor electron-builder ever runs vite — so a change to
``NewSessionDialog.tsx`` / ``NewTicketPane.tsx`` that is not followed by
``cd frontend && npm run build`` ships a dialog with no Ticket tab at all while
every TypeScript test still passes. These answer "did it get built", nothing
more; the behaviour lives in ``tests/unit/test_ticket_compose.py``.

Written against tokens rather than layout via :mod:`tests._bundle` — the
bundler's indentation is its own business.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from backend.web import server
from tests._bundle import in_bundle

client = TestClient(server.app)


def test_the_new_dialog_ships_two_tabs():
    js = client.get("/app.js").text
    assert in_bundle('{ key: "session", label: "Session" }', js)
    assert in_bundle('{ key: "ticket", label: "Ticket" }', js)
    css = client.get("/style.css").text
    # The SPECIFIC selector, not just ".nf-tab": `.ws-head button` in
    # smallDialogs.css paints every button in a dialog header as a bordered
    # pill and outranks a bare class, so the tabs shipped once as two more
    # buttons in a row of buttons with nothing marking which one you were on.
    # A weaker rule would still satisfy a ".nf-tab" substring check.
    assert ".nf-head .nf-tabs .nf-tab" in css
    # Underline tabs, the same idiom Intake uses — the app has one tab style.
    assert ".nf-head .nf-tabs .nf-tab.active" in css


def test_the_card_has_one_top_edge_and_grows_downward():
    """Every pane is its own natural height and they all start at the same y.

    The two ways to stop a tab click moving the card are equal heights and a
    fixed top. Equal heights was tried and is what put a dead band above the
    buttons on the shorter pane, so the top is fixed instead and each pane is
    as tall as it needs to be.

    The assertions are about the SHAPE of the rule, not about pixels: that the
    top is pinned rather than centred, that neither landing pane is given a
    height, and that a card is capped so sagging stops at the window edge. A
    pane that quietly regained a fixed height would look right in a screenshot
    and bring the dead band back.
    """
    css = client.get("/style.css").text
    block = css.split("#new-dialog")[1][:400]
    assert "align-items: flex-start;" in block
    assert "padding-top: var(--nf-top);" in block
    # The top is centred on a LANDING pane's height, not the full form's —
    # anchoring on 620px is what parked the short panes high on the screen.
    assert in_bundle(
        "--nf-top: max(16px, calc((100vh - var(--nf-anchor-h)) / 2));", css
    )
    assert in_bundle("--nf-anchor-h: min(86vh, 400px);", css)
    # Neither landing pane is handed a height; both are natural.
    assert in_bundle("#new-form.nf-ask {\n  height: auto;\n}", css)
    assert "height: var(--nf-tab-h)" not in css
    # ...and a card stops sagging at the bottom of the window.
    assert in_bundle("--nf-max-h: calc(100vh - var(--nf-top) - 16px);", css)
    assert in_bundle(
        "#new-form,\n#new-ticket-form {\n  max-height: var(--nf-max-h);\n}", css
    )
    # Each literal is written once, so the anchor and the cap cannot drift.
    assert css.count("min(86vh, 620px)") == 1
    assert css.count("min(86vh, 400px)") == 1


def test_the_ticket_pane_ships_its_box_its_picker_and_its_button():
    js = client.get("/app.js").text
    assert in_bundle('id: "nt-brief"', js)
    assert in_bundle('id: "nt-source"', js)
    assert in_bundle('"Create ticket"', js)
    # The placeholder is the whole tutorial here, exactly as the Describe box's
    # is: it is the only thing that shows the shape of a brief worth filing.
    assert "the login page hangs for SSO users" in js


def test_the_pane_says_there_is_no_ticket_form_on_purpose():
    """The one sentence that keeps the missing fields from reading as an
    unfinished feature. Without it the pane looks like a form somebody forgot
    to write the rest of."""
    js = client.get("/app.js").text
    assert "There is no form: the tracker already has one" in js


def test_the_filed_ticket_ships_its_link():
    """The link IS the deliverable — the whole reason the pane has a third
    state rather than just closing on success."""
    js = client.get("/app.js").text
    assert in_bundle('className: "nt-filed-link"', js)
    assert in_bundle('rel: "noopener noreferrer"', js)
    css = client.get("/style.css").text
    assert ".nt-filed-link" in css


def test_the_folder_browser_creates_folders_without_window_prompt():
    """The + Folder button had never worked in the desktop app: Electron does
    not implement ``prompt()``, so it returned null and the button did nothing,
    silently, for every user who wasn't in a browser tab.

    The old call site is pinned as ABSENT rather than the new row merely being
    present, because both can be true at once — a re-added prompt beside the
    inline row would pass a presence-only check and still be dead in Electron.
    """
    js = client.get("/app.js").text
    assert "New folder name (created in " not in js
    assert in_bundle('className: "rb-new"', js)
    assert in_bundle('placeholder: "New folder name"', js)
    css = client.get("/style.css").text
    assert ".rb-new" in css
