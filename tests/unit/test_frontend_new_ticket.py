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


def test_the_card_is_centred_and_one_height_for_every_pane():
    """The New card is centred like every other modal, and holds still by
    having ONE fixed height for every pane rather than a fixed top edge.

    The fixed-top shape (``align-items: flex-start`` + ``--nf-top`` anchored on
    a landing pane's height) left the card hanging high on the screen, and the
    owner asked for it back in the middle: "Recenter it". A centred card moves
    by half of any height change, so the form, page 1 and the Ticket pane now
    share one height (scrolling inside) — switching tabs or opening a fold
    moves nothing — and ``86vh`` keeps it inside a short window.
    """
    css = client.get("/style.css").text
    block = css.split("#new-dialog {")[1].split("}")[0]
    # Nothing overrides the .modal centring any more.
    assert "align-items: flex-start" not in block
    assert "padding-top" not in block
    assert "--nf-top" not in css and "--nf-anchor-h" not in css
    assert in_bundle("--nf-card-h: min(86vh, 620px);", block)
    # One height for both cards; no pane goes back to its natural height.
    assert in_bundle(
        "#new-ticket-form { background: var(--panel); border: 1px solid "
        "var(--border); border-radius: 12px; padding: 20px 22px; width: "
        "min(660px, 94vw); height: var(--nf-card-h);",
        css,
    )
    assert "#new-form.nf-ask" not in css
    # The Ticket pane fills the card and keeps its actions on the bottom edge.
    assert in_bundle(".nt-pane > .modal-actions { margin-top: auto; }", css)
    # The literal is written once, so the two cards cannot drift apart.
    assert css.count("min(86vh, 620px)") == 1


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
