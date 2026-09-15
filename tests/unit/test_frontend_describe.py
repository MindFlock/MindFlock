"""The "Describe it" box: bundle assertions.

``backend/web/static/{app.js,style.css}`` are committed build output — neither
``uv build`` nor electron-builder ever runs vite — so a change to
``NewSessionDialog.tsx`` that is not followed by ``cd frontend && npm run build``
ships a dialog with no Describe box at all while every TypeScript test still
passes. These pin the three things the affordance cannot exist without: the
input, the sentence that tells a first-time reader what to type into it, and the
button, plus the ``type="button"`` that keeps that button from creating a session
out of whatever the form happens to be holding.

Behaviour lives in ``frontend/src/__tests__/newSession.test.ts`` (the three pure
helpers) and ``tests/unit/test_session_plan.py`` (the route and the resolver);
these only answer "did it get built". Written against tokens rather than layout
via :mod:`tests._bundle` — the bundler's indentation is its own business.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from backend.web import server
from tests._bundle import in_bundle

client = TestClient(server.app)


def test_the_describe_box_ships_in_the_bundle():
    js = client.get("/app.js").text
    # The strip itself, first child of .nf-body and above the Templates row.
    assert in_bundle('id: "new-describe"', js)
    # The input the sentence is typed into.
    assert in_bundle('id: "new-describe-text"', js)
    # The placeholder is the whole tutorial: it is the only place that says a
    # sentence may name a folder AND ask for a worktree, and a box labelled
    # "Describe it" with nothing in it teaches neither.
    assert in_bundle('placeholder: "e.g. fix the login bug in acme-api"', js)


def test_the_fill_button_ships_and_still_cannot_submit_the_form():
    js = client.get("/app.js").text
    assert in_bundle('id: "new-describe-go"', js)
    # type="button" is load-bearing, not tidiness: <button>'s default type is
    # "submit", and this one sits inside <form id="new-form" onSubmit={submit}>,
    # so without it clicking "Fill it in" CREATES a session in whatever folder
    # the suggestion pre-fill happened to leave behind. Asserted as the pair,
    # because an id that survives a refactor which dropped the type would pass
    # a check for either one alone.
    assert in_bundle('type: "button", id: "new-describe-go"', js)
    # A button with no label is not an affordance, and both labels ship: the
    # ring alone reads as a hang long before the server's 75s budget expires.
    assert '"Review details first"' in js
    assert '"Reading…"' in js
    assert '"Still reading…"' in js
    # Cancel only exists mid-turn, and it is the only way out of a slow CLI
    # start that does not involve closing the dialog.
    assert in_bundle('id: "new-describe-cancel"', js)


def test_the_describe_strip_is_styled_in_the_bundle():
    css = client.get("/style.css").text
    # Same rebuild, other half of the build output: without these the row is a
    # stacked input and a raw browser-chrome button in a card that styles
    # everything else.
    assert ".nf-describe-row" in css
    # min-width:0, or the input refuses to shrink and pushes the button off the
    # end of the card.
    assert "#new-describe-text" in css
    # The server's note and the inline failure, both asides under the row.
    assert ".nf-describe-note" in css
    assert "#new-describe-error" in css


def test_the_workspace_mode_is_a_choice_with_something_always_selected():
    """A plan that picks a worktree must SHOW that it picked one.

    Reported against a form that had chosen correctly: the note read "Work
    happens in a new worktree, not in the folder itself" while the Git &
    workspace fold below it showed one cleared checkbox and nothing else — and a
    cleared checkbox cannot distinguish "the other mode" from "nobody decided".
    Encoding the worktree mode as the ABSENCE of a tick is what made a decision
    look like an omission, so the pair are radios: whatever the plan chose, and
    whatever a hand-filled form defaults to, exactly one of them is on.
    """
    js = client.get("/app.js").text
    # Both options ship, and both are radios — a single radio would be a
    # checkbox wearing a circle, with the same unreadable off state.
    assert in_bundle(
        'type: "radio", name: "new-workspace-mode", id: "new-worktree"', js
    )
    assert in_bundle(
        'type: "radio", name: "new-workspace-mode", id: "new-in-place"', js
    )
    # Provisioning is the THIRD answer to the same question, not a checkbox
    # beside it. Asked, about the earlier shape: "isn't provision workspace the
    # same exact thing as new worktree" — a fair reading of a radio and a
    # checkbox that both produced a separate checkout and never said how they
    # were related. Sharing the `name` is what says it: one question, and
    # provisioning is the worktree answer plus setup.
    assert in_bundle(
        'type: "radio", name: "new-workspace-mode", id: "new-provision"', js
    )
    # The shared `name` is what makes them exclusive, and the group label is what
    # makes the pair read as one question rather than two unrelated toggles.
    assert '"Where the work happens"' in js
    assert '"New worktree"' in js
    css = client.get("/style.css").text
    assert ".nf-mode" in css
    # Without a radio rule these render as raw browser chrome beside the
    # accent-styled checkbox directly under them.
    assert 'input[type="radio"]' in css


def test_the_dialog_is_two_pages_with_the_sentence_first():
    """Page 1 is the sentence; page 2 is the form it fills in.

    Asked for in these words: "i would actually rather new menu be two pages
    with autofill be the first page and then the normal mode being page 2 ...
    the user on page 1 can choose to use the prompt to fill out page 2 or just
    immediately provision the session without validating on page 2."

    So page 1 ships three ways out, and all three have to survive a refactor:
    fill the form and check it, start the session without looking, or skip the
    model entirely. The last one is the one a test has to hold down — it is the
    least used and the only route to the form for someone with no coding CLI
    installed, which since page 1 became the landing page is also the only route
    to the dialog's original behaviour.
    """
    js = client.get("/app.js").text
    assert in_bundle('id: "new-describe-go"', js)
    assert in_bundle('id: "new-describe-start"', js)
    assert in_bundle('id: "new-describe-skip"', js)
    # Back, so page 1's sentence is never a thing you have to retype.
    assert in_bundle('id: "new-back"', js)
    # The immediate path is a BUTTON, never the Enter key: Enter in the box has
    # meant "read this" since the box existed, and a key that creates a session
    # in a folder nobody has looked at is the one gesture this feature has been
    # careful not to build.
    assert in_bundle('type: "button", id: "new-describe-start"', js)
    assert '"Create session"' in js
