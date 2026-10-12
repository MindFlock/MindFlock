"""Frontend wave 5 (round-4 findings L5–L9): the parts the server owns.

The keymap, replay gating, missing-workspace, no-origin, light-theme, gh-optional
and commit-dialog UI checks of this wave are pinned against their source modules
in frontend/src/__tests__/wave5Wiring.test.ts. What stays here:

- L5: the docs describe the Ctrl+K chord trade-off.
- L6: core/events.js (hand-written, served as-is) exposes backlog replay
      detection (mindflock.events.isReplay), stamped per connection.
- ssh-friendly-github: mobile.js (hand-written, not bundled) offers the
      browser link instead of a raw gh error, with the same remedy sentence the
      app bundle carries.
- the commit dialog's routes: the endpoints the bundle calls exist.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from backend.web import server

client = TestClient(server.app)


# --------------------------------------------------------------------------- #
# L5 — Ctrl+K is the VSCode chord prefix for the outward-facing git verbs.
# --------------------------------------------------------------------------- #


def test_web_ui_docs_describe_the_chord_tradeoff():
    text = open("docs/web-ui.md", encoding="utf-8").read()
    # The chord bindings, the readline kill-line cost, and the AltGr rationale
    # for retiring Ctrl+Alt are all documented.
    assert "Ctrl+K P" in text
    assert "kill-line" in text
    assert "AltGr" in text


# --------------------------------------------------------------------------- #
# L6 — backlog replay must not re-fire one-shot toasts on every page load.
# --------------------------------------------------------------------------- #


def test_events_js_exposes_is_replay():
    js = client.get("/core/events.js").text
    # Connect epoch: client clock at open, overridden by the server's hello
    # frame (feature-detected via a numeric server_time field).
    assert "_connectEpoch" in js
    assert "server_time" in js
    assert 'env.event === "hello"' in js
    assert "REPLAY_SKEW_S" in js
    # Public API: mindflock.events.isReplay(envelope).
    assert "isReplay: isReplay" in js
    assert "isReplay(envelope)" in js  # documented in the contract comment


def test_events_js_epoch_set_per_connection():
    js = client.get("/core/events.js").text
    # The fallback epoch is (re)stamped in onopen so reconnects re-classify.
    open_block = js[js.index("ws.onopen") : js.index("ws.onmessage")]
    assert "Date.now() / 1000" in open_block


# --------------------------------------------------------------------------- #
# ssh-friendly-github — the GitHub CLI is optional in every UI surface.
#
# The contributor bug: `gh` absent -> a blocking modal echoing "GitHub CLI (gh)
# is not installed", from four independent entry points, for someone whose only
# sin was an SSH remote.
# --------------------------------------------------------------------------- #

# The user-facing remedy, verbatim. The backend, mobile.js, the docs and the
# app bundle (lib/sessionActions.ts PR_REMEDY, pinned by wave5Wiring.test.ts)
# all say this one.
PR_REMEDY_OLD = (
    "add a GitHub token in Intake → Pull requests, or install the GitHub CLI"
)
PR_REMEDY = (
    "add a GitHub token in Settings → Connections → GitHub → Connect, "
    "or install the GitHub CLI"
)


def test_mobile_js_offers_a_link_not_a_raw_gh_error():
    js = client.get("/mobile.js").text
    assert PR_REMEDY in js
    assert PR_REMEDY_OLD not in js
    assert "j.ok === false" in js
    assert "j.compare_url" in js
    assert "j.pr_url" in js


def test_commit_dialog_recovers_a_message_the_hooks_rejected():
    """The dialog remembered the last message in a JS Map, which a page reload —
    or the server restart that prompts one — empties, while the worktree file it
    was a copy of sat right there. It now asks the server for it.

    Asserting on the endpoint path, not on compiled JSX: a URL literal survives a
    change of bundler, and the shape of emitted JSX does not."""
    js = client.get("/app.js").text
    assert '"/commit-message"' in js
    assert "/api/instances/{title}/commit-message" in server.app.openapi()["paths"]


def test_commit_dialog_offers_a_model_written_message():
    """✨ — the button that reads the diff and writes the message: the endpoint
    the bundle calls exists. The control's id, label and style are pinned in
    wave5Wiring.test.ts."""
    js = client.get("/app.js").text
    assert '"/commit-message/suggest"' in js
    assert (
        "/api/instances/{title}/commit-message/suggest" in server.app.openapi()["paths"]
    )
