"""The phone (/m) lists a peer's shared sessions the way it lists another
device's — pinned for mobile.js / mobile.css (docs/peer-link.md "Shared
sessions — CONTRACT", docs/web-ui.md).

A session someone shares with you over a peer link arrives in
``GET /api/instances`` as ``<peer>::<title>`` with ``remote_peer: true``,
``device`` / ``device_label`` (the peer), ``display_title`` (the SHARER's name
for it), ``peer_mode`` (``full`` | ``gated``) and ``peer_share``. On the phone:

1. **Listed like a device row**, after your own, one group per peer under
   "<peer> · shared with you", named by the sharer's name — never the raw
   ``<peer>::<title>`` — with the mode on the sub-line.
2. **Opened the same way**: the terminal is the ordinary
   ``/api/instances/<row>/terminal`` websocket (the server bridges it over
   the link); a withdrawn one says "no longer shared", not an error.
3. **The owner's actions are hidden**, as the rail hides them for
   ``peer_share`` rows: no Commit / Push / PR / Merge bar, no Terminal or
   Diff tab, nothing but a note on the ⋯ sheet (no Restart, Close, Delete).
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "backend" / "web" / "static"


def _js() -> str:
    return (STATIC / "mobile.js").read_text()


def _fn(js: str, head: str) -> str:
    """The body of ``  function <head>`` up to its closing brace."""
    return js.split("  function " + head)[1].split("\n  }\n")[0]


# --------------------------------------------------------------------------- #
# 1. Listed like a device row, named by the sharer
# --------------------------------------------------------------------------- #
def test_a_peer_row_is_known_by_remote_peer():
    js = _js()
    assert "return !!(inst && inst.remote_peer);" in _fn(js, "isPeerRow(inst) {")
    assert "inst.device_label || inst.device" in _fn(js, "peerWho(inst) {")
    mode = _fn(js, "peerModeWords(inst) {")
    assert '"gated to its folder"' in mode and '"full join"' in mode


def test_rows_are_named_by_the_sharer_never_the_raw_title():
    js = _js()
    # The rename, else the sharer's display_title, else the title.
    assert "return inst.name || inst.display_title || inst.title;" in _fn(
        js, "rowName(inst) {"
    )
    assert "return rowName(instances[i]);" in _fn(js, "nameOf(title) {")
    row = _fn(js, "sessionRow(inst) {")
    assert "if (isPeerRow(inst)) name.textContent = rowName(inst);" in row


def test_peer_rows_group_under_the_peer_after_your_own():
    js = _js()
    rows = _fn(js, "renderRows() {")
    # Your own rows first, then each peer's in first-seen order.
    assert "ranked.filter(function (inst) { return !isPeerRow(inst); })" in rows
    assert "peerDevs.indexOf(inst.device) < 0" in rows
    # A header before each peer's group.
    assert (
        "if (group && group !== lastGroup) sessionListEl.appendChild(peerGroupHead(inst));"
        in rows
    )
    head = _fn(js, "peerGroupHead(inst) {")
    assert 'peerWho(inst) + " · shared with you"' in head
    assert (
        "h.textContent" in head and "innerHTML" not in head
    )  # their name is untrusted text


def test_the_sub_line_says_the_mode_not_their_folder():
    row = _fn(_js(), "sessionRow(inst) {")
    assert (
        'if (isPeerRow(inst)) sub.appendChild(document.createTextNode("  ·  " + peerModeWords(inst)));'
        in row
    )
    assert (
        'else if (inst.repo) sub.appendChild(document.createTextNode("  ·  " + inst.repo));'
        in row
    )
    assert '(isPeerRow(inst) ? " sess-peer" : "")' in row
    css = (STATIC / "mobile.css").read_text()
    assert ".sess-group {" in css and ".sess-row.sess-peer .sess-main" in css


# --------------------------------------------------------------------------- #
# 2. Opened the same way a device row is
# --------------------------------------------------------------------------- #
def test_the_terminal_is_the_ordinary_route():
    js = _js()
    conn = _fn(js, "connect() {")
    assert '"/api/instances/" + encodeURIComponent(current) + path' in conn
    # No peer-specific socket: the server bridges the same path.
    assert "remote_peer" not in conn and "/api/peer/" not in conn
    # A withdrawn session says so plainly, not as an error.
    assert '(isPeerRow(currentInst()) ? "no longer shared" : "session gone")' in conn


def test_a_peer_row_opens_on_its_agent():
    sel = _fn(_js(), "select(title) {")
    assert 'if (isPeerRow(instByTitle(title)) && view !== "agent") {' in sel
    assert 'view = tab = "agent";' in sel
    # …before the terminal is built.
    assert sel.index("isPeerRow(instByTitle(title))") < sel.index("buildTerm();")


# --------------------------------------------------------------------------- #
# 3. The owner's actions are hidden
# --------------------------------------------------------------------------- #
def test_no_git_bar_terminal_or_diff_tab_on_a_peer_row():
    upd = _fn(_js(), "updateActions() {")
    assert "var peer = isPeerRow(inst);" in upd
    assert 'actionsEl.classList.toggle("hidden", peer);' in upd
    assert """'.tab[data-tab="shell"], .tab[data-tab="diff"]'""" in upd
    assert "var next = peer ? null : nextAct(inst);" in upd


def test_the_actions_sheet_offers_nothing_of_the_owners():
    sheet = _fn(_js(), "renderActSheet() {")
    peer = sheet.split("if (isPeerRow(inst)) {")[1].split("} else {")[0]
    assert "folderNote(" in peer and "it runs on their machine" in peer
    for verb in ('"Restart"', '"Wake"', '"Resume session"', '"Close"', '"Delete…"'):
        assert verb not in peer, verb
        # …all of them live only in the owner's branch.
        assert re.search(r"\} else \{[\s\S]*" + re.escape(verb), sheet), verb
