"""Sessions an agent on another device starts here (core.remote_origin).

The run that found this: an agent on the laptop started six sessions on
ML-Rig-2023; there they said only "agent" and sat idle forever. Now they say
where they came from, close themselves once done (agent-started only), and
the session that started one may wait for it, message it and close it —
nothing else on that device, checked on the receiving side.

Security refusals pinned here: another device's session, a forged origin
title, a person-started session, a guest link, a request without the device
link's credential, a relayed request (the routes are not reachable through
another device's proxy), and a report from a device that was not the one the
session was started on.
"""

from __future__ import annotations

import datetime
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse

from backend import client as api_client
from backend.mcp.protocol import ToolError
from backend.session.storage import Status
from backend.web import server
from backend.web.core import auth as auth_mod
from backend.web.core import auto_close as ac
from backend.web.core import mailbox
from backend.web.core import remote
from backend.web.core import remote_origin as ro
from backend.web.core import session_create
from tests.unit import test_auto_close as tac
from tests.unit._mcp_fakes import FakeCtx, make_box, row

client = TestClient(server.app)
TOKEN = "fleet-or-device-token"
LAPTOP = "ethanlaptop2026"
NOW = tac.NOW
GRACE = tac.GRACE
_quiet = tac._quiet  # auto_close: no tmux, no team runs, no routes


class _Inst:
    def __init__(self, title, created=None):
        self.Title = title
        self.CreatedAt = datetime.datetime.fromtimestamp(
            created if created is not None else __import__("time").time()
        ).astimezone()
        self.Parent = ""
        self.Spawned = True
        self.InPlace = True
        self.PeerShare = ""
        self.Branch = ""
        self.Status = Status.Running

    def Started(self):  # noqa: N802
        return True

    def GetWorktreePath(self):  # noqa: N802
        return ""


@pytest.fixture
def link(monkeypatch):
    """This device as the RECEIVER: remote control on, the caller's
    credential valid, the laptop discovered at its tailnet address."""
    monkeypatch.delenv("CS_WEB_MODE", raising=False)
    monkeypatch.setenv("MINDFLOCK_AUTH", "0")
    monkeypatch.setattr(remote, "remote_control_enabled", lambda: True)
    monkeypatch.setattr(auth_mod, "token_valid", lambda t: t == TOKEN)
    monkeypatch.setattr(remote, "_DEVICES", {})
    dev = remote._device_state(LAPTOP)
    dev.update(host="EthanLaptop2026", ip="100.64.0.7", reachable=True)
    monkeypatch.setattr(
        remote, "device_for_ip", lambda ip: LAPTOP if ip == "testclient" else ""
    )
    instances: dict = {}
    monkeypatch.setattr(server.ENGINE, "instances", instances)
    monkeypatch.setattr(server.ENGINE, "save", lambda **kw: None)
    return SimpleNamespace(instances=instances, dev=dev)


def _hdr(device=LAPTOP, token=TOKEN):
    h = {remote.REMOTE_HEADER: device}
    if token:
        h["Authorization"] = "Bearer " + token
    return h


def _started_here(link, title="tripwire-send-remote", session="tripwire-send"):
    """A session the laptop's agent session ``session`` started here."""
    ro.note_created(
        title,
        {"device": LAPTOP, "device_name": "EthanLaptop2026", "verified": True},
        {"session": session, "by": "agent"},
    )
    inst = _Inst(title)
    link.instances[title] = inst
    return inst


# --------------------------------------------------------------------------- #
# provenance on create
# --------------------------------------------------------------------------- #
class TestCreate:
    @pytest.fixture
    def created(self, link, monkeypatch):
        seen = []

        async def inner(payload):
            seen.append(dict(payload))
            return JSONResponse({"title": payload.get("title")}, status_code=202)

        async def fake(payload, *, person="", link=None):
            # The real provenance step, around a stub create.
            return await ro.create_over_link(payload, link, inner)

        monkeypatch.setattr(session_create, "create_requested", fake)
        return seen

    def test_an_agent_create_over_the_link_records_where_it_came_from(
        self, link, created
    ):
        r = client.post(
            "/api/instances",
            json={
                "title": "w",
                "repo_path": "/x",
                "prompt": "do it",
                "origin": {"session": "tripwire-send", "by": "agent"},
            },
            headers=_hdr(),
        )
        assert r.status_code == 202, r.text
        (payload,) = created
        assert "origin" not in payload  # never a create key
        assert payload["prompt"].startswith("do it")
        assert "from tripwire-send on EthanLaptop2026" in payload["prompt"]
        assert "report_result" in payload["prompt"]
        link.instances["w"] = _Inst("w")
        f = ro.row_field(link.instances["w"])
        assert f["label"] == "from tripwire-send on EthanLaptop2026"
        assert f["device"] == LAPTOP and f["verified"] is True and f["by"] == "agent"

    def test_the_device_is_what_the_link_says_never_a_field(self, link, created):
        client.post(
            "/api/instances",
            json={
                "title": "w",
                "repo_path": "/x",
                "origin": {"session": "s", "by": "agent", "device": "the-boss-pc"},
            },
            headers=_hdr(device="the-boss-pc"),  # the marker claims another
        )
        link.instances["w"] = _Inst("w")
        f = ro.row_field(link.instances["w"])
        assert f["device"] == LAPTOP and f["verified"] is True

    def test_a_create_by_a_person_on_the_other_device(self, link, created):
        client.post(
            "/api/instances", json={"title": "p", "prompt": "hi"}, headers=_hdr()
        )
        assert created[0]["prompt"] == "hi"  # no footer for a person
        link.instances["p"] = _Inst("p")
        f = ro.row_field(link.instances["p"])
        assert f["label"] == "you, from EthanLaptop2026" and f["by"] == "person"

    def test_an_unverified_device_is_labelled_so(self, link, created, monkeypatch):
        monkeypatch.setattr(remote, "device_for_ip", lambda ip: "")
        client.post(
            "/api/instances",
            json={"title": "u", "origin": {"session": "s", "by": "agent"}},
            headers=_hdr(device="tunnelbox"),
        )
        link.instances["u"] = _Inst("u")
        assert ro.row_field(link.instances["u"])["label"] == (
            "from s on tunnelbox (unverified)"
        )

    def test_a_local_create_ignores_origin(self, link, created):
        client.post(
            "/api/instances",
            json={"title": "l", "origin": {"session": "x", "by": "agent"}},
        )
        assert "origin" not in created[0]
        link.instances["l"] = _Inst("l")
        assert ro.row_field(link.instances["l"]) is None

    def test_a_reused_title_inherits_nothing(self, link):
        _started_here(link, "w")
        assert ro.record("w", link.instances["w"]) is not None  # pins it
        later = _Inst("w", created=__import__("time").time() + 999999)
        assert ro.record("w", later) is None

    def test_the_origin_title_is_one_clean_line(self):
        o = ro.clean_origin({"session": "a\nb\x1b[31m" + "x" * 500, "by": "agent"})
        assert "\n" not in o["session"] and "\x1b" not in o["session"]
        assert len(o["session"]) <= ro.SESSION_CHARS
        assert ro.clean_origin({"by": "boss"}) == {"session": "", "by": "person"}
        assert ro.clean_origin("nope") is None


# --------------------------------------------------------------------------- #
# control from the origin (enforced HERE)
# --------------------------------------------------------------------------- #
def _control(title, action="status", origin_session="tripwire-send", **kw):
    headers = kw.pop("headers", None)
    return client.post(
        "/api/origin-control",
        json=dict(
            {"title": title, "action": action, "origin_session": origin_session},
            **kw,
        ),
        headers=_hdr() if headers is None else headers,
    )


class TestControl:
    def test_status_message_and_close_by_the_origin(self, link, monkeypatch):
        _started_here(link, "rw")
        monkeypatch.setattr(
            server,
            "_agent_output",
            lambda i, t, v: ("last_reply", "Installed.", False, None, 200),
        )
        r = _control("rw")
        assert r.status_code == 200, r.text
        assert r.json()["last_reply"] == "Installed." and r.json()["closed"] is False
        r = _control("rw", "message", text="now start it")
        assert r.status_code == 200, r.text
        msgs = mailbox.fetch("rw", unread_only=False)["messages"]
        assert msgs and msgs[-1]["from"] == LAPTOP + "::tripwire-send"
        assert msgs[-1]["text"] == "now start it"
        closed = []
        from backend.web.core import session_end

        monkeypatch.setattr(
            session_end,
            "end_session",
            lambda engine, title, wipe=False: closed.append((title, wipe)),
        )
        r = _control("rw", "close")
        assert r.status_code == 200 and closed == [("rw", False)]

    def test_another_devices_session_is_refused(self, link, monkeypatch):
        _started_here(link, "rw")
        monkeypatch.setattr(
            remote, "device_for_ip", lambda ip: "otherbox" if ip else ""
        )
        r = _control("rw", "close")
        assert r.status_code == 403 and r.json()["code"] == "not_origin"
        assert "rw" in link.instances

    def test_a_forged_origin_title_is_refused(self, link):
        _started_here(link, "rw", session="tripwire-send")
        for forged in ("orchestrator", "tripwire-send ", ""):
            r = _control("rw", "message", origin_session=forged, text="x")
            assert r.status_code == 403 and r.json()["code"] == "not_origin", forged

    def test_a_person_started_session_is_refused(self, link):
        ro.note_created(
            "mine",
            {"device": LAPTOP, "device_name": "EthanLaptop2026", "verified": True},
            None,
        )
        link.instances["mine"] = _Inst("mine")
        r = _control("mine", "close", origin_session="")
        assert r.status_code == 403 and r.json()["code"] == "not_origin"

    def test_a_session_nobody_elsewhere_started_is_refused(self, link):
        link.instances["local"] = _Inst("local")
        r = _control("local", "close")
        assert r.status_code == 403 and r.json()["code"] == "not_origin"

    def test_an_unverified_caller_for_a_verified_record(self, link, monkeypatch):
        _started_here(link, "rw")
        monkeypatch.setattr(remote, "device_for_ip", lambda ip: "")
        r = _control("rw", "close")  # only the marker's claim, same name
        assert r.status_code == 403 and r.json()["code"] == "not_origin"

    def test_a_guest_link_is_refused(self, link, monkeypatch):
        _started_here(link, "rw")
        monkeypatch.setattr(server._guests, "scope_guest", lambda request: {"id": "g"})
        r = _control("rw", "close")
        assert r.status_code == 403 and r.json()["code"] == "guest"

    def test_no_credential_or_no_marker_is_refused(self, link):
        _started_here(link, "rw")
        r = _control("rw", "close", headers=_hdr(token=""))
        assert r.status_code == 401
        r = _control("rw", "close", headers=_hdr(token="wrong"))
        assert r.status_code == 401
        r = _control("rw", "close", headers={})
        assert r.status_code == 403 and r.json()["code"] == "not_a_device"

    def test_remote_control_off_is_refused(self, link, monkeypatch):
        _started_here(link, "rw")
        monkeypatch.setattr(remote, "remote_control_enabled", lambda: False)
        r = _control("rw", "close")
        assert r.status_code == 403

    def test_a_relayed_request_cannot_reach_it(self, link):
        # Not on the fwd/ allow-list, not under /api/instances/: another
        # device's proxy never forwards it.
        assert remote._fwd_target("POST", "/api/origin-control") is None
        assert remote._fwd_target("POST", "/api/origin-inbox") is None
        assert remote._split_proxy_path("/api/origin-control") is None
        r = client.post(
            "/api/devices/%s/fwd/api/origin-control" % LAPTOP,
            json={"title": "rw", "action": "close", "origin_session": "x"},
        )
        assert r.status_code in (404, 502) or "not forwarded" in r.text
        # A remote-flagged request is never proxied onward at all.
        r = client.post(
            "/api/devices/%s/fwd/api/instances" % LAPTOP,
            json={},
            headers=_hdr(),
        )
        assert r.status_code == 400 and "not relayed" in r.text

    def test_the_origin_side_route_refuses_a_relayed_request(self, link):
        link.instances["orch"] = _Inst("orch")
        r = client.post(
            "/api/instances/orch/remote-control",
            json={"target": "dev::x", "action": "close"},
            headers=_hdr(),
        )
        assert r.status_code == 400 and r.json()["code"] == "remote"


# --------------------------------------------------------------------------- #
# reports back: the origin side accepts only what it started
# --------------------------------------------------------------------------- #
def _inbox(to="orch", frm="rw", **kw):
    headers = kw.pop("headers", None)
    return client.post(
        "/api/origin-inbox",
        json=dict({"to": to, "from": frm, "kind": "result", "text": "done"}, **kw),
        headers=_hdr() if headers is None else headers,
    )


class TestOriginInbox:
    def test_a_report_from_the_device_it_was_started_on(self, link):
        link.instances["orch"] = _Inst("orch")
        ro.note_started(LAPTOP, "rw", "orch")
        r = _inbox(data={"status": "done", "evil": "x" * 10})
        assert r.status_code == 201, r.text
        msgs = mailbox.fetch("orch", unread_only=False)["messages"]
        assert msgs[-1]["from"] == LAPTOP + "::rw"
        assert msgs[-1]["kind"] == "result"
        assert msgs[-1]["data"] == {"status": "done"}

    def test_refusals(self, link, monkeypatch):
        link.instances["orch"] = _Inst("orch")
        ro.note_started("mac", "rw", "orch")  # started on ANOTHER device
        assert _inbox().json()["code"] == "not_started_here"
        ro.note_started(LAPTOP, "rw", "orch")
        assert _inbox(to="someone-else").status_code == 403
        assert _inbox(frm="x::rw").status_code == 400
        assert _inbox(headers=_hdr(token="")).status_code == 401
        assert _inbox(headers={}).status_code == 403
        monkeypatch.setattr(server._guests, "scope_guest", lambda request: {"id": "g"})
        assert _inbox().status_code == 403


def test_a_send_records_what_it_started_there(monkeypatch):
    from backend.web.core import send_session

    async def post(dev, path, body, timeout=10.0, **kw):
        assert body["origin"] == {"session": "tripwire-send", "by": "agent"}
        return 202, {"title": body["title"]}

    monkeypatch.setattr(remote, "post_json", post)
    tok = send_session.ORIGIN.set({"session": "tripwire-send", "by": "agent"})
    try:
        import asyncio

        name, _ = asyncio.run(
            send_session._create({"key": "ml-rig-2023-1"}, "tw", {}, "")
        )
    finally:
        send_session.ORIGIN.reset(tok)
    assert ro.started_by("ml-rig-2023-1", name) == "tripwire-send"


def test_a_person_send_is_not_recorded_as_started(monkeypatch):
    from backend.web.core import send_session

    async def post(dev, path, body, timeout=10.0, **kw):
        return 202, {}

    monkeypatch.setattr(remote, "post_json", post)
    tok = send_session.ORIGIN.set({"session": "tw", "by": "person"})
    try:
        import asyncio

        asyncio.run(send_session._create({"key": "m"}, "tw", {}, ""))
    finally:
        send_session.ORIGIN.reset(tok)
    assert ro.started_by("m", "tw") == ""


def test_a_forwarded_create_naming_its_origin_is_recorded(link):
    link.instances["orch"] = _Inst("orch")
    body = json.dumps(
        {"title": "rw", "origin": {"session": "orch", "by": "agent"}}
    ).encode()
    remote._note_fwd_create({"key": "rig"}, body)
    assert ro.started_by("rig", "rw") == "orch"
    # Naming a session that is not live here records nothing.
    body = json.dumps({"title": "x", "origin": {"session": "ghost"}}).encode()
    remote._note_fwd_create({"key": "rig"}, body)
    assert ro.started_by("rig", "x") == ""


# --------------------------------------------------------------------------- #
# finishing: closes once done and idle; never a person's
# --------------------------------------------------------------------------- #
def _remote_world(by="agent"):
    srv, _ = tac._world()
    inst = tac._Inst("rw", parent="", spawned=True, created=__import__("time").time())
    srv.ENGINE.instances["rw"] = inst
    ro.note_created(
        "rw",
        {"device": LAPTOP, "device_name": "EthanLaptop2026", "verified": True},
        {"session": "tripwire-send" if by == "agent" else "", "by": by},
    )
    return srv, inst


def _j(srv, inst, **row):
    return tac._judge(srv, inst, tac._row("rw", **row))


class TestFinishing:
    def test_done_and_idle_past_the_grace_closes(self):
        srv, inst = _remote_world()
        verdict, why, _ = _j(srv, inst)
        assert verdict == ac.CLOSE and why == ro.R_FINISHED

    def test_within_the_grace_it_says_when(self):
        srv, inst = _remote_world()
        verdict, why, at = _j(srv, inst, activity_since=NOW - 60)
        assert verdict == ac.WAIT and at == NOW - 60 + GRACE

    def test_never_a_person_started_one(self):
        srv, inst = _remote_world(by="person")
        assert _j(srv, inst)[:2] == (ac.KEEP, ro.R_PERSON)

    def test_never_once_a_person_typed_into_it(self):
        srv, inst = _remote_world()
        srv._HUMAN_INPUT_AT["rw"] = NOW - 10 * GRACE
        assert _j(srv, inst)[:2] == (ac.KEEP, ro.R_TYPED_EVER)

    def test_never_while_asking_busy_or_keep(self):
        srv, inst = _remote_world()
        assert _j(srv, inst, asking={"q": "?"})[0] == ac.KEEP
        assert _j(srv, inst, activity="working")[0] == ac.KEEP
        ac.set_keep_open("rw", True, ro._created(inst))
        assert _j(srv, inst)[:2] == (ac.KEEP, ac.R_KEEP_OPEN)

    def test_never_while_its_own_todo_is_open(self, monkeypatch):
        from backend.web.addons.todo import store as todos

        srv, inst = _remote_world()
        monkeypatch.setattr(
            todos, "list_items", lambda: [{"session": "rw", "done": False}]
        )
        assert _j(srv, inst)[:2] == (ac.KEEP, ro.R_TODO)
        monkeypatch.setattr(
            todos, "list_items", lambda: [{"session": "rw", "done": True}]
        )
        assert _j(srv, inst)[0] == ac.CLOSE

    def test_the_row_says_it_will_close(self):
        srv, inst = _remote_world()
        with ac._LOCK:
            ac._VERDICTS["rw"] = {"verdict": ac.WAIT, "why": "", "closes_at": 123.0}
        f = ac.row_field(inst)
        assert f is not None and f["closes_at"] == 123.0

    def test_turn_ends_are_found_once_settled(self):
        srv, inst = _remote_world()
        rows = [tac._row("rw", activity_since=NOW - 120)]
        (item,) = ro.turn_ends(srv, rows, now=NOW)
        assert item["title"] == "rw" and item["since"] == NOW - 120
        assert ro.turn_ends(srv, rows, now=NOW - 100) == []  # not settled
        ro._update("rw", turn_sent=NOW - 120)
        assert ro.turn_ends(srv, rows, now=NOW) == []
        ro._update("rw", turn_sent=0.0, report_sent=NOW - 60)
        assert ro.turn_ends(srv, rows, now=NOW) == []


# --------------------------------------------------------------------------- #
# the MCP side
# --------------------------------------------------------------------------- #
def _mcp(me="orch"):
    rows = [row("orch"), row("w1", parent="orch", spawned=True), row("rig::rw")]
    return make_box(rows, me=me)


class TestMcp:
    def test_message_and_close_go_through_the_provenance_route(self):
        box, api, clock = _mcp()
        seen = []
        api.top_routes[("POST", "/api/instances/orch/remote-control")] = (
            lambda payload, q: seen.append(payload) or {"ok": True, "id": "m1"}
        )
        out = box.send_message({"to": "rig::rw", "text": "go"}, FakeCtx(clock))
        assert out["sent"][0]["remote"] is True
        out = box.kill_session({"title": "rig::rw"}, FakeCtx(clock))
        assert out["mode"] == "close"
        assert [p["action"] for p in seen] == ["message", "close"]
        assert seen[0]["target"] == "rig::rw" and seen[0]["text"] == "go"

    def test_delete_and_now_are_refused_for_a_remote_session(self):
        box, api, clock = _mcp()
        with pytest.raises(ToolError, match="close"):
            box.kill_session({"title": "rig::rw", "mode": "delete"}, FakeCtx(clock))
        with pytest.raises(ToolError, match="delivery auto"):
            box.send_message(
                {"to": "rig::rw", "text": "x", "delivery": "now"}, FakeCtx(clock)
            )

    def test_the_device_refusal_reads_plainly(self):
        box, api, clock = _mcp()
        api.errors[("POST", "/api/instances/orch/remote-control")] = (
            api_client.ApiError(
                403, "rw was started by another session", {"code": "not_origin"}
            )
        )
        with pytest.raises(ToolError, match="unless YOU started them"):
            box.kill_session({"title": "rig::rw"}, FakeCtx(clock))

    def test_report_result_goes_to_the_origin(self):
        box, api, clock = make_box(
            [
                row(
                    "rw",
                    spawned=True,
                    origin={"by": "agent", "session": "tripwire-send", "label": "x"},
                )
            ],
            me="rw",
        )
        seen = []
        api.routes[("POST", "report-origin")] = lambda title, payload, q: seen.append(
            payload
        ) or {"ok": True, "sent_to": "tripwire-send on EthanLaptop2026", "id": "m2"}
        out = box.report_result(
            {"status": "done", "summary": "installed"}, FakeCtx(clock)
        )
        assert out["sent_to"] == "tripwire-send on EthanLaptop2026"
        assert seen[0]["text"] == "installed" and seen[0]["data"]["status"] == "done"

    def test_whoami_and_get_session_show_the_origin(self):
        origin = {"by": "agent", "session": "s", "label": "from s on Laptop"}
        box, api, clock = make_box([row("rw", origin=origin)], me="rw")
        assert box.whoami({}, FakeCtx(clock))["origin"] == origin
        assert box.whoami({}, FakeCtx(clock))["session"]["origin"] == origin

    def test_spawn_on_a_device(self):
        box, api, clock = _mcp()
        api.top_routes[("GET", "/api/devices")] = {
            "devices": [
                {
                    "device": "ml-rig-2023-1",
                    "host": "ML-Rig-2023",
                    "member": True,
                    "same_fleet": True,
                    "connected": True,
                },
                {"device": "ml-rig-2023", "host": "ml-rig-2023", "connected": True},
            ]
        }
        seen = []
        api.top_routes[("POST", "/api/devices/ml-rig-2023-1/fwd/api/instances")] = (
            lambda payload, q: seen.append(payload)
            or {"title": payload["title"], "status": "loading"}
        )
        out = box.spawn_session(
            {
                "device": "ML-Rig-2023",
                "repo_path": "/home/e/tripwire",
                "prompt": "install it",
                "title": "tw-install",
            },
            FakeCtx(clock),
        )
        assert out["title"] == "ml-rig-2023-1::tw-install" and out["remote"] is True
        assert seen[0]["origin"] == {"session": "orch", "by": "agent"}
        assert seen[0]["spawned"] is True and seen[0]["repo_path"] == "/home/e/tripwire"

    def test_spawn_on_a_device_needs_a_folder_there(self):
        box, api, clock = _mcp()
        with pytest.raises(ToolError, match="repo_path"):
            box.spawn_session({"device": "rig", "prompt": "x"}, FakeCtx(clock))


def test_a_turn_end_is_delivered_once_and_only_by_the_lease_holder(monkeypatch):
    import asyncio

    from backend.web.core import events as events_mod
    from backend.web.core import sleep as sleep_mod

    srv, inst = _remote_world()
    srv._agent_output = lambda i, t, v: (
        "last_reply",
        "All installed.",
        False,
        None,
        200,
    )
    rows = [tac._row("rw", activity_since=__import__("time").time() - 120)]
    monkeypatch.setattr(events_mod, "sessions_snapshot", lambda: rows)
    sent = []

    async def deliver(rec, title, text, data):
        sent.append((rec["session"], title, text, data))
        return 201, {"ok": True}

    monkeypatch.setattr(ro, "deliver", deliver)
    monkeypatch.setattr(sleep_mod, "hold_lease", lambda name, ttl, now=None: False)
    asyncio.run(ro._tick(srv))
    assert sent == []  # another server holds the lease
    monkeypatch.setattr(sleep_mod, "hold_lease", lambda name, ttl, now=None: True)
    asyncio.run(ro._tick(srv))
    asyncio.run(ro._tick(srv))
    assert len(sent) == 1
    session, title, text, data = sent[0]
    assert session == "tripwire-send" and title == "rw"
    assert "All installed." in text and data == {"status": "idle", "turn_end": True}
