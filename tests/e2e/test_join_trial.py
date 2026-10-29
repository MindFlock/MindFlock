"""The "join someone" walk between two real MindFlock servers on this machine.

A (Ana, the access gate on, owner token) invites B (Ben) with two sessions —
``alpha`` in full, ``beta`` gated — and B joins with the invite LINK. Then,
through each server's own HTTP and websocket API, exactly as the web UI and
the MindFlock MCP call them: the rows on B's rail, the shared terminal (both
ways, one keyboard, the tmux prefix opening nothing), presence, the chat both
ways, agent messages both ways (B shares one of its sessions back), a
withdrawal, A going down and coming back, and a guest link on A opened in a
headless Chrome. docs/peer-link.md "Field trial (shared sessions)" is what the
first run found.

Opt-in twice over: the e2e package runs only when targeted (``pytest
tests/e2e``), and this module only with ``MINDFLOCK_E2E_JOIN=1`` — it boots
two servers and a sandboxed agent (~1-2 minutes)::

    MINDFLOCK_E2E_JOIN=1 pytest tests/e2e/test_join_trial.py

Each server has its own scratch HOME, peer home, tmux server (``tmux -S``
only) and free ports; both are stopped and their tmux servers killed on exit.
"""

from __future__ import annotations

import json
import os
import threading
import time

import pytest

from tests.e2e._peer_pair import PeerServer, wait_until

pytestmark = pytest.mark.skipif(
    os.environ.get("MINDFLOCK_E2E_JOIN") != "1",
    reason="the two-server join walk runs only with MINDFLOCK_E2E_JOIN=1",
)

TOKEN = "join-trial-" + "a" * 40


class Events:
    """Everything one server's ``/api/events`` bus says, in a thread."""

    def __init__(self, srv: PeerServer) -> None:
        self.items: list = []
        self.reopen(srv)

    def reopen(self, srv: PeerServer) -> None:
        """Listen (again: after the server restarted)."""
        from websockets.sync.client import connect

        self._ws = connect(
            srv.ws_url("/api/events"), additional_headers=srv.ws_headers()
        )
        self._t = threading.Thread(target=self._read, daemon=True)
        self._t.start()

    def _read(self) -> None:
        try:
            for raw in self._ws:
                self.items.append(json.loads(raw))
        except Exception:  # noqa: BLE001 — closed
            pass

    def named(self, name: str, **data) -> list:
        return [
            e
            for e in self.items
            if e.get("event") == name
            and all((e.get("data") or {}).get(k) == v for k, v in data.items())
        ]

    def wait(self, name: str, timeout: float = 15.0, **data) -> dict:
        return wait_until(
            lambda: (self.named(name, **data) or [None])[-1], timeout, name
        )

    def close(self) -> None:
        self._ws.close()


class Term:
    """A terminal websocket: bytes in, everything it said kept."""

    def __init__(self, srv: PeerServer, title: str) -> None:
        from websockets.sync.client import connect

        self.out = b""
        self.texts: list = []
        self.closed = None
        self._ws = connect(
            srv.ws_url("/api/instances/%s/terminal" % title),
            additional_headers=srv.ws_headers(),
            max_size=None,
        )
        self._ws.send(json.dumps({"type": "resize", "cols": 100, "rows": 30}))
        self._t = threading.Thread(target=self._read, daemon=True)
        self._t.start()

    def _read(self) -> None:
        from websockets.exceptions import ConnectionClosed

        try:
            for m in self._ws:
                if isinstance(m, bytes):
                    self.out += m
                else:
                    self.texts.append(m)
        except ConnectionClosed as e:
            self.closed = e.rcvd.code if e.rcvd else 1006
        else:
            rcvd = getattr(self._ws.protocol, "close_rcvd", None)
            self.closed = rcvd.code if rcvd else 1000

    def type(self, data) -> None:
        self._ws.send(data)

    def close(self) -> None:
        self._ws.close()


@pytest.fixture
def pair(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    a = PeerServer(tmp_path / "a", "Ana", token=TOKEN)
    b = PeerServer(tmp_path / "b", "Ben")
    try:
        a.start()
        b.start()
        yield a, b
    finally:
        for srv in (a, b):
            try:
                srv.stop()
            except Exception:  # noqa: BLE001
                pass


def _rows(srv: PeerServer) -> dict:
    rows = srv.ok("GET", "/api/instances")
    rows = rows.get("instances", rows) if isinstance(rows, dict) else rows
    return {r["title"]: r for r in rows}


def test_join_someone_end_to_end(pair, page):
    a, b = pair
    for title in ("alpha", "beta"):
        a.new_bash(title)
    b.new_bash("bobfull")
    ev_a, ev_b = Events(a), Events(b)
    try:
        _walk(a, b, ev_a, ev_b, page)
    finally:
        ev_a.close()
        ev_b.close()


def _walk(a, b, ev_a, ev_b, page):
    # 1. The invite, with sessions, is a link; the link opens the join page.
    inv = a.ok(
        "POST",
        "/api/peer/invites",
        {
            "sessions": [
                {"title": "alpha", "mode": "full"},
                {"title": "beta", "mode": "gated"},
            ]
        },
    )
    assert inv["url"].startswith(a.base_url + "/join#mfp")
    page.goto(inv["url"])
    page.wait_for_selector("#invite:not(.hidden)")
    assert "invited to work together" in page.inner_text("body")
    assert inv["code"] in page.input_value("#code")

    # 2. B joins with the LINK; both sides know the sessions at once.
    joined = b.ok("POST", "/api/peer/join", {"code": inv["url"]}, timeout=60)
    lid = joined["link_id"]
    rows = wait_until(
        lambda: (
            lambda r: (
                r
                if "Ana::alpha" in r
                and len([t for t in r if t.startswith("Ana::")]) == 2
                else None
            )
        )(_rows(b)),
        30,
        "the invite's rows on B",
    )
    alpha = rows["Ana::alpha"]
    assert alpha["peer_share"] is True and alpha["peer_mode"] == "full"
    assert alpha["display_title"] == "alpha" and alpha["device"] == "Ana"
    (gated,) = [
        r for t, r in rows.items() if t.startswith("Ana::") and t != "Ana::alpha"
    ]
    assert gated["peer_mode"] == "gated"
    assert gated["display_title"] == "beta"  # the invite's name, not peer-ben-…
    gated_title = gated["title"].split("::", 1)[1]
    (link_a,) = a.ok("GET", "/api/peer")["links"]
    assert link_a["from_invite"] and link_a["sas_verified"] is False
    assert set(link_a["invite_shares"]) == {"alpha", gated_title}
    ev_a.wait("peer.session_shared", title="alpha", by="you")
    # The joiner hears of BOTH — also the one its first list already held.
    ev_b.wait("peer.session_shared", title="alpha", theirs=True)
    ev_b.wait("peer.session_shared", title=gated_title, theirs=True)

    # 3. The terminal: B types into A's pane and sees it come back.
    tb = Term(b, "Ana::alpha")
    time.sleep(1.0)
    tb.type("echo hello-from-b\r")
    wait_until(lambda: "hello-from-b" in a.pane("alpha"), 10, "B's line in A's pane")
    wait_until(lambda: b"hello-from-b" in tb.out, 10, "the echo back on B")
    tb.close()
    time.sleep(1.0)
    # One keyboard: A's own terminal first, then B is told who drives.
    ta = Term(a, "alpha")
    time.sleep(1.0)
    tb = Term(b, "Ana::alpha")
    time.sleep(1.0)
    tb.type("echo b-while-a-drives\r")
    wait_until(lambda: b"is driving" in tb.out, 10, "the read-only notice on B")
    assert "b-while-a-drives" not in a.pane("alpha")
    ta.close()
    time.sleep(1.5)
    tb.type("echo b-after-a-left\r")
    wait_until(lambda: "b-after-a-left" in a.pane("alpha"), 10, "B types once A let go")
    # The tmux prefix reaches the program, never tmux itself.
    windows = a.tmux("list-windows", "-a", "-F", "#{session_name}:#{window_index}")
    tb.type(b"\x02c")
    tb.type(b"\x02:")
    time.sleep(1.0)
    tb.type(b"\x03")
    assert (
        a.tmux("list-windows", "-a", "-F", "#{session_name}:#{window_index}") == windows
    )

    # 4. Presence: B's page on the row is a pill on A's, gone on leave.
    b.ok("POST", "/api/instances/Ana::alpha/focus", {})
    tb.close()
    b.ok("POST", "/api/instances/Ana::alpha/focus", {"left": True})
    got = ev_a.wait("guest.focus_changed", link="peer:" + lid, left=True)
    assert got["data"]["label"] == "Ben"
    assert ev_a.named("guest.focus_changed", link="peer:" + lid, joined=True)

    # 5. The chat, both ways.
    b.call("POST", "/api/instances/Ana::alpha/chat", {"text": "hi from Ben"})
    wait_until(
        lambda: [
            m
            for m in a.ok("GET", "/api/instances/alpha/chat")["messages"]
            if m["from"] == "peer:" + lid
        ],
        10,
        "B's line in A's chat",
    )
    a.ok("POST", "/api/instances/alpha/chat", {"text": "hello Ben"})
    wait_until(
        lambda: [
            m
            for m in b.ok("GET", "/api/instances/Ana::alpha/chat")["messages"]
            if m["text"] == "hello Ben"
        ],
        10,
        "A's line in B's copy",
    )
    assert [e for e in ev_b.named("session.chat") if e["session"] == "Ana::alpha"]

    # 6. Agent messages: before It matches only the invite's sessions; B
    # shares one back once both pressed It matches, and A's agent answers it.
    st, sent = b.call(
        "POST",
        "/api/instances/Ana::alpha/messages",
        {"text": "hello agent", "from": "bobfull"},
    )
    assert st == 202, sent
    (got,) = a.ok("GET", "/api/instances/alpha/messages")["messages"]
    assert got["from"] == "Ben::bobfull" and got["data"]["peer"] is True
    assert got["data"]["reply_ok"] is False
    assert (
        b.call(
            "POST",
            "/api/peer/links/%s/sessions" % lid,
            {"title": "bobfull", "mode": "full"},
        )[0]
        == 409
    )
    a.ok("POST", "/api/peer/links/%s/verified" % lid, {"verified": True})
    b.ok("POST", "/api/peer/links/%s/verified" % lid, {"verified": True})
    b.ok(
        "POST",
        "/api/peer/links/%s/sessions" % lid,
        {"title": "bobfull", "mode": "full"},
    )
    wait_until(lambda: "Ben::bobfull" in _rows(a), 20, "B's session on A's rail")
    st, sent = b.call(
        "POST",
        "/api/instances/Ana::alpha/messages",
        {"text": "reply please", "from": "bobfull"},
    )
    assert st == 202
    asked = a.ok("GET", "/api/instances/alpha/messages")["messages"][-1]
    assert asked["data"]["reply_ok"] is True
    st, _ = a.call(
        "POST",
        "/api/instances/Ben::bobfull/messages",
        {"text": "here you go", "from": "alpha", "reply_to": asked["id"]},
    )
    assert st == 202
    answer = b.ok("GET", "/api/instances/bobfull/messages")["messages"][-1]
    assert answer["from"] == "Ana::alpha" and answer["reply_to"] == sent["id"]
    assert answer["hop"] == 1
    st, _ = b.call(
        "POST",
        "/api/instances/Ana::%s/messages" % gated_title,
        {"text": "to the sandbox", "from": "bobfull"},
    )
    assert st == 202
    assert (
        a.ok("GET", "/api/instances/%s/messages" % gated_title)["messages"][-1]["data"][
            "mode"
        ]
        == "gated"
    )

    # 7. Withdraw: B's open terminal ends (4404) and the row goes.
    tb = Term(b, "Ana::alpha")
    time.sleep(1.0)
    a.ok("DELETE", "/api/peer/links/%s/sessions/alpha" % lid)
    wait_until(lambda: tb.closed == 4404, 10, "term_end withdrawn")
    ev_b.wait("peer.session_unshared", title="alpha", theirs=True)
    wait_until(lambda: "Ana::alpha" not in _rows(b), 10, "the row gone")
    a.ok(
        "POST", "/api/peer/links/%s/sessions" % lid, {"title": "alpha", "mode": "full"}
    )
    wait_until(lambda: "Ana::alpha" in _rows(b), 20, "shared again")
    # A goes down: B's rows go and B is told; A comes back: so do they.
    a.kill()
    ev_b.wait("peer.state", connected=False)
    wait_until(
        lambda: not [t for t in _rows(b) if t.startswith("Ana::")], 15, "rows gone"
    )
    a.relaunch()
    ev_a.reopen(a)
    wait_until(
        lambda: "Ana::alpha" in _rows(b), 120, "rows back after A restarted", 1.0
    )
    assert "alpha" in _rows(a)  # the session itself lived through it

    # 8. A guest link on A: the name dialog, only alpha, typing, no tmux.
    link = a.ok("POST", "/api/guests", {"sessions": ["alpha"]})
    assert link["url"] is None  # no tunnel in the sandbox: the local link only
    page.goto(link["local_url"])
    page.get_by_label("Your name").fill("Gita")
    page.get_by_role("button", name="Join").click()
    wait_until(
        lambda: a.ok("GET", "/api/guests")["links"][0].get("name") == "Gita",
        10,
        "named",
    )
    titles = page.locator("li.inst").evaluate_all(
        "els => els.map(e => e.dataset.title)"
    )
    assert titles == ["alpha"]
    a.ok("POST", "/api/instances/alpha/chat", {"text": "welcome Gita"})
    page.get_by_text("welcome Gita").wait_for(timeout=10_000)
    page.locator(".xterm").first.click()
    page.keyboard.type("echo hi-from-guest")
    page.keyboard.press("Enter")
    wait_until(lambda: "hi-from-guest" in a.pane("alpha"), 10, "the guest's line")
    # The guest drives as themselves, never as the owner.
    row = wait_until(
        lambda: (
            lambda r: r if str(r.get("driver") or "").startswith("guest:") else None
        )(a.row("alpha")),
        10,
        "the guest at the keyboard",
    )
    assert row["driver_name"] == "Gita"  # the name picked after attaching
    windows = a.tmux("list-windows", "-a", "-F", "#{session_name}:#{window_index}")
    page.keyboard.press("Control+b")
    page.keyboard.type("c")
    time.sleep(1.0)
    assert (
        a.tmux("list-windows", "-a", "-F", "#{session_name}:#{window_index}") == windows
    )
