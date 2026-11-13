"""Journeys: a route (flight), flown on a real server and watched in the UI.

The unit suites pin the route model and the driver piece by piece; nothing
flew one end to end. These do: a real server and its driver loop, a real
session (stub agent), the committed bundle in headless Chrome. They assert on
what a person sees — the bell's count, the row under "Needs you", the Done
button — and on the route's state the API reports.

The second journey is the one the 2026-11-12 stability sweep was asked for:
"there are often things that say need you, with no open sessions."
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

import pytest

expect = pytest.importorskip("playwright.sync_api").expect

from tests.e2e.test_journeys import (  # noqa: E402
    SESSION_UP_TIMEOUT_MS,
    _create_session,
    _titles,
)

ROUTE_TIMEOUT_S = 60.0


def _call(srv, method: str, path: str, body=None):
    """One API call → (status, parsed body). A 4xx/5xx is returned, not
    raised: the journeys assert on it with the server's own words."""
    req = urllib.request.Request(
        srv.base_url + path,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as err:
        raw = err.read().decode("utf-8", "replace")
        try:
            return err.code, json.loads(raw)
        except ValueError:
            return err.code, {"raw": raw}


def _flight(srv, fid: str) -> dict:
    status, body = _call(srv, "GET", "/api/flights/" + fid)
    assert status == 200, (status, body)
    return body.get("flight") or body


def _wait_state(srv, fid: str, want, timeout: float = ROUTE_TIMEOUT_S) -> dict:
    """Poll the route until its state is one of ``want``; the failure says
    where it was and why, in the route's own words."""
    want = (want,) if isinstance(want, str) else tuple(want)
    deadline = time.time() + timeout
    f: dict = {}
    while time.time() < deadline:
        f = _flight(srv, fid)
        if f.get("state") in want:
            return f
        time.sleep(0.5)
    raise AssertionError(
        "route %s never reached %s: state=%r reason=%r cursor=%r step_states=%r"
        % (
            fid,
            "/".join(want),
            f.get("state"),
            f.get("reason"),
            f.get("cursor"),
            f.get("step_states"),
        )
    )


def _start_wait_for_me(srv, title: str, stops: int = 1) -> str:
    """A route of ``stops`` Wait for me stops on ``title`` → its id, once it
    is waiting on the person at the first."""
    steps = [
        {"kind": "you", "title": "Look it over" if i == 0 else "Look again"}
        for i in range(stops)
    ]
    status, body = _call(
        srv, "POST", "/api/flights/start", {"title": title, "steps": steps}
    )
    assert status == 202, (status, body)
    fid = body["flight"]["id"]
    _wait_state(srv, fid, "waiting_you")
    return fid


def _badge(page):
    return page.locator("#notif-badge")


def _is_retry(entry: dict) -> bool:
    """A history line a Retry press wrote (``op_retry``: resumed / retry)."""
    return entry.get("event") == "resumed" and entry.get("note") == "retry"


def _retries(flight: dict) -> int:
    return sum(1 for h in flight.get("history") or [] if _is_retry(h))


# --------------------------------------------------------------------------- #
# a route waits for me, I press Done, it lands
# --------------------------------------------------------------------------- #
def test_a_route_waits_for_me_then_lands(mf_server, page, open_app):
    """A Wait for me stop holds the route and shows under Needs you on the
    bell, with the session's name; Done carries it on to the next stop, and
    after the last one the route has landed and nothing needs you."""
    title = "journey-route"
    _create_session(mf_server, title)
    fid = _start_wait_for_me(mf_server, title, stops=2)
    open_app(mf_server)

    badge = _badge(page)
    expect(badge).to_have_text("1", timeout=SESSION_UP_TIMEOUT_MS)
    expect(badge).not_to_have_class("notif-badge attn hidden")
    badge.locator("xpath=..").click()
    needs = page.locator("#notif-pop .notif-attn")
    expect(needs).to_be_visible(timeout=10_000)
    expect(needs).to_contain_text("Needs you")
    row = needs.locator("[data-outbox-row='%s']" % title)
    expect(row).to_be_visible(timeout=10_000)
    expect(row).to_contain_text("Look it over")

    row.get_by_role("button", name="Done").click()
    # The second stop: the same route, waiting on me again, one row still.
    deadline = time.time() + ROUTE_TIMEOUT_S
    while time.time() < deadline:
        f = _flight(mf_server, fid)
        if f.get("state") == "waiting_you" and int(f.get("cursor") or 0) == 1:
            break
        time.sleep(0.5)
    else:
        raise AssertionError("Done did not carry the route to its second stop: %r" % f)
    expect(row).to_contain_text("Look again", timeout=15_000)
    expect(badge).to_have_text("1")

    row.get_by_role("button", name="Done").click()
    landed = _wait_state(mf_server, fid, "done")
    assert not landed.get("reason"), landed.get("reason")
    expect(badge).to_have_class("notif-badge attn hidden", timeout=15_000)
    # The session is still there: a route landing never closes it.
    assert title in _titles(mf_server)


# --------------------------------------------------------------------------- #
# closing the session a route waits on
# --------------------------------------------------------------------------- #
def test_closing_the_session_clears_its_route_from_needs_you(mf_server, page, open_app):
    """A route waiting on me, and I close its session instead. Nothing is
    left open, so nothing may say it needs me: the bell's count goes, and the
    route is no longer waiting on a person who has nothing to press."""
    title = "journey-route-close"
    _create_session(mf_server, title)
    fid = _start_wait_for_me(mf_server, title)
    open_app(mf_server)

    badge = _badge(page)
    expect(badge).to_have_text("1", timeout=SESSION_UP_TIMEOUT_MS)

    row = page.locator("li.inst[data-title='%s']" % title)
    expect(row).to_be_visible(timeout=SESSION_UP_TIMEOUT_MS)
    row.hover()
    row.locator("button.kill").click()
    expect(row).to_have_count(0, timeout=SESSION_UP_TIMEOUT_MS)
    assert title not in _titles(mf_server)

    expect(badge).to_have_class("notif-badge attn hidden", timeout=30_000)
    f = _flight(mf_server, fid)
    assert f.get("state") not in (
        "waiting_you",
        "halted",
    ), "the route still waits on a person after its session was closed: %r" % {
        k: f.get(k) for k in ("state", "reason", "session", "gone_since")
    }


# --------------------------------------------------------------------------- #
# a stop that fails halts the route; Retry runs it again, Skip carries on
# --------------------------------------------------------------------------- #
def test_a_failed_stop_halts_the_route_and_skip_carries_on(mf_server, page, open_app):
    """A command that exits non-zero halts the route at that stop: the bell
    lists it with Retry and Skip. Retry runs it again (and it halts again);
    Skip moves on to the next stop, and the route still lands."""
    title = "journey-route-halt"
    _create_session(mf_server, title)
    status, body = _call(
        mf_server,
        "POST",
        "/api/flights/start",
        {
            "title": title,
            "steps": [
                {"kind": "command", "title": "Build", "config": {"command": "exit 3"}},
                {"kind": "you", "title": "Look it over"},
            ],
        },
    )
    assert status == 202, (status, body)
    fid = body["flight"]["id"]
    halted = _wait_state(mf_server, fid, "halted")
    assert int(halted.get("cursor") or 0) == 0
    assert halted.get("reason"), "a halted route says why"
    open_app(mf_server)

    badge = _badge(page)
    expect(badge).to_have_text("1", timeout=SESSION_UP_TIMEOUT_MS)
    badge.locator("xpath=..").click()
    row = page.locator("#notif-pop .notif-attn [data-outbox-row='%s']" % title)
    expect(row).to_be_visible(timeout=10_000)
    expect(row.get_by_role("button", name="Retry")).to_be_visible()
    expect(row.get_by_role("button", name="Skip")).to_be_visible()

    retries_before = _retries(halted)
    row.get_by_role("button", name="Retry").click()
    # It runs again and fails again: halted at the same stop, never "running"
    # for good, and still one row. Read from the route's own history, not by
    # catching it "running": `exit 3` is over well inside one poll, and a
    # test that had to see the state in between failed whenever it missed it
    # (CI, 2026-11-13: "saw running: False" on a route that had done exactly
    # the right thing).
    deadline = time.time() + ROUTE_TIMEOUT_S
    f: dict = {}
    while time.time() < deadline:
        f = _flight(mf_server, fid)
        hist = f.get("history") or []
        last_retry = max((i for i, h in enumerate(hist) if _is_retry(h)), default=-1)
        if (
            _retries(f) == retries_before + 1
            and f.get("state") == "halted"
            and last_retry < len(hist) - 1  # it failed again AFTER the retry
        ):
            break
        time.sleep(0.2)
    else:
        raise AssertionError(
            "Retry did not run the stop again and halt: %r"
            % {k: f.get(k) for k in ("state", "reason", "cursor", "history")}
        )
    assert int(f.get("cursor") or 0) == 0
    expect(badge).to_have_text("1", timeout=15_000)

    expect(row.get_by_role("button", name="Skip")).to_be_visible(timeout=15_000)
    row.get_by_role("button", name="Skip").click()
    deadline = time.time() + ROUTE_TIMEOUT_S
    while time.time() < deadline:
        f = _flight(mf_server, fid)
        if f.get("state") == "waiting_you" and int(f.get("cursor") or 0) == 1:
            break
        time.sleep(0.5)
    else:
        raise AssertionError("Skip did not carry the route on: %r" % f)
    expect(row).to_contain_text("Look it over", timeout=15_000)
    row.get_by_role("button", name="Done").click()
    _wait_state(mf_server, fid, "done")
    expect(badge).to_have_class("notif-badge attn hidden", timeout=15_000)


# --------------------------------------------------------------------------- #
# pause, carry on, stop
# --------------------------------------------------------------------------- #
def test_pause_carry_on_and_stop(mf_server):
    """A paused route stays where it is and waits on nobody; Carry on puts it
    back; Stop ends it, and an ended route refuses every further press with
    a 4xx that says so — never a 500, never a silent 200."""
    title = "journey-route-pause"
    _create_session(mf_server, title)
    fid = _start_wait_for_me(mf_server, title, stops=2)

    status, body = _call(mf_server, "POST", "/api/flights/%s/pause" % fid)
    assert status == 200, (status, body)
    assert _flight(mf_server, fid).get("state") == "paused"
    time.sleep(3)  # a few driver passes: paused stays paused
    assert _flight(mf_server, fid).get("state") == "paused"

    status, body = _call(mf_server, "POST", "/api/flights/%s/resume" % fid)
    assert status == 200, (status, body)
    f = _wait_state(mf_server, fid, "waiting_you")
    assert int(f.get("cursor") or 0) == 0

    status, body = _call(mf_server, "POST", "/api/flights/%s/cancel" % fid)
    assert status == 200, (status, body)
    assert _flight(mf_server, fid).get("state") == "cancelled"
    assert title in _titles(mf_server), "Stop keeps the session"

    step = (f.get("steps") or [{}])[0].get("id") or "s1"
    for path in ("steps/%s/done" % step, "steps/%s/skip" % step, "pause", "resume"):
        status, body = _call(mf_server, "POST", "/api/flights/%s/%s" % (fid, path))
        assert 400 <= status < 500, (path, status, body)
    assert _flight(mf_server, fid).get("state") == "cancelled"


# --------------------------------------------------------------------------- #
# the server restarts under a waiting route
# --------------------------------------------------------------------------- #
def _restart_server_only(srv) -> None:
    """Stop the server PROCESS and start it again over the same home, state
    and tmux (the sessions stay up, as they do when MindFlock updates)."""
    import os
    import signal
    import subprocess
    import sys

    from tests.e2e import _server

    proc = srv.proc
    os.killpg(proc.pid, signal.SIGTERM)
    proc.wait(timeout=20)
    srv.proc = subprocess.Popen(
        [sys.executable, "-m", "backend.web.run", "local"],
        cwd=str(srv.repo),
        env=srv.env,
        stdout=srv.log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    assert _server._wait_http(srv.base_url + "/api/remote/hello", 60.0), (
        "the server did not come back:\n" + srv.log_text()[-3000:]
    )


def test_a_waiting_route_survives_a_server_restart(mf_server):
    """MindFlock restarts (an update, a crash) while a route waits on me: it
    is still waiting at the same stop afterwards, exactly once, and Done
    still lands it."""
    title = "journey-route-restart"
    _create_session(mf_server, title)
    fid = _start_wait_for_me(mf_server, title)

    _restart_server_only(mf_server)
    assert title in _titles(mf_server)
    time.sleep(5)  # the driver's first passes after boot
    f = _flight(mf_server, fid)
    assert f.get("state") == "waiting_you", {
        k: f.get(k) for k in ("state", "reason", "cursor")
    }
    status, body = _call(mf_server, "GET", "/api/flights")
    assert status == 200
    live = [x for x in body["flights"] if x.get("session") == title]
    assert [x["id"] for x in live] == [fid], "one route for the session, not a copy"

    step = f["steps"][0]["id"]
    status, body = _call(
        mf_server, "POST", "/api/flights/%s/steps/%s/done" % (fid, step), {}
    )
    assert status == 200, (status, body)
    _wait_state(mf_server, fid, "done")
