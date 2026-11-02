"""The ``someone`` stop (docs/flights.md "Round 15", task ``hooks``): a perch
whose ask goes out — to a peer link by name or a webhook — with a one-tap
Done link (``GET``/``POST /api/hooks/flight/{token}/done``)."""

import asyncio
import http.server
import json
import threading
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.web.core import flight_driver as D
from backend.web.core import flights as F
from backend.web.core.flight_kinds import StepFailed
from backend.web.core.flight_kinds import _hooks as H
from backend.web.core.flight_kinds import someone as S

NOW = time.time()
BASE = "http://box:8765"


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_FLIGHTS_DIR", str(tmp_path / "flights"))
    monkeypatch.setenv("MINDFLOCK_AUTOPILOT_FILE", str(tmp_path / "autopilot.json"))
    monkeypatch.setattr(D, "wake", lambda: None)
    monkeypatch.setattr(D, "_announce", lambda before, after: None)
    monkeypatch.setattr(H, "public_base", lambda: BASE)
    yield


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(H.router)
    return TestClient(app)


@pytest.fixture
def hook():
    """A webhook receiver: records each POSTed JSON; answers ``status[0]``."""
    seen, status = [], [200]

    class Hd(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            seen.append(json.loads(self.rfile.read(n)))
            self.send_response(status[0])
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Hd)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:%d/hook" % srv.server_address[1], seen, status
    srv.shutdown()


class _Link:
    def __init__(self, link_id, peer_name):
        self.link_id, self.peer_name = link_id, peer_name


class _Peers:
    def __init__(self, fail=None):
        self.sent, self.fail = [], fail
        self.store = self

    def list(self):
        return [_Link("aa11", "Mauricio"), _Link("bb22", "Dana")]

    async def human_message(self, link_id, text):
        if self.fail:
            raise RuntimeError(self.fail)
        self.sent.append((link_id, text))
        return {"msg_id": "m1", "delivered": True}


@pytest.fixture
def peers(monkeypatch):
    from backend.peer import service as svc

    p = _Peers()
    monkeypatch.setattr(svc, "get_service", lambda: p)
    return p


def _flight(config, session="s"):
    plan = F.adhoc_plan([{"kind": "someone", "config": config}], name="Ship it")
    return F.take_off(plan, session=session, now=NOW)


def _fly(config, session="s"):
    """Take off and do what the driver does with the first pass: the run
    intent, ``_perform`` (the kind's run), the outcome applied."""
    f = _flight(config, session)
    action = F.next_action(f, {"session": {}}, NOW)
    assert action["action"] == "run"
    F.mutate(f["id"], lambda x: F.apply(x, action, NOW))
    out = asyncio.run(D._perform(F.get(f["id"]), action, NOW))
    F.mutate(f["id"], lambda x: F.apply(x, out, NOW))
    return F.get(f["id"]), out


def _ss(f):
    return f["step_states"][f["steps"][0]["id"]]


def test_kind_is_a_people_perch():
    row = next(k for k in F.kinds_dto()["kinds"] if k["kind"] == "someone")
    assert row["family"] == "people" and row["actor"] == "you"
    assert row["risk"] == []
    assert [x["name"] for x in row["fields"]] == ["who", "text", "items", "webhook"]
    assert "someone" not in F.SESSION_KINDS


def test_config_and_title():
    step = F.normalize_step(
        {"kind": "someone", "config": {"who": "Dana", "items": "a\n\nb"}}, 0
    )
    assert step["config"] == {
        "who": "Dana",
        "text": "",
        "items": ["a", "b"],
        "webhook": "",
    }
    assert step["title"] == "Ask Dana"
    assert F.normalize_step({"kind": "someone"}, 0)["title"] == "Ask someone"
    with pytest.raises(F.PlanError) as err:
        F.normalize_step({"kind": "someone", "config": {"webhook": "x.dev"}}, 0)
    assert err.value.field == "steps[0].config.webhook"


def test_nobody_to_ask_fails_the_stop():
    f = _flight({"text": "look"})
    with pytest.raises(StepFailed, match="nobody to ask"):
        asyncio.run(S._run(f, f["steps"][0], {"action": "run"}, NOW))
    out = asyncio.run(D._perform(f, {"action": "run", "note": ""}, NOW))
    assert out["action"] == "fail" and out["reason"] == "nobody to ask"


def test_a_peer_gets_the_ask_and_the_flight_perches(peers):
    f, out = _fly(
        {"who": "mauricio", "text": "Try {subject}", "items": ["login", "logout"]}
    )
    assert out["action"] == "you"
    ss = _ss(f)
    assert f["state"] == "waiting_you" and ss["state"] == "waiting_you"
    assert ss["note"] == "Try Ship it"
    tok = ss["detail"]["token"]
    assert ss["detail"]["done_url"] == "%s/api/hooks/flight/%s/done" % (BASE, tok)
    assert ss["detail"]["items"] == ["login", "logout"]
    assert ss["detail"]["sent_to"] == "mauricio"
    assert ss["detail"]["receipt"] == "done by you"
    assert F.step_view(f)["actor"] == "you"
    ((link, text),) = peers.sent
    assert link == "aa11"
    assert text == "Try Ship it\n• login\n• logout\n\nDone: " + ss["detail"]["done_url"]
    # Parked: the planner waits on you from here on.
    assert F.next_action(f, {"session": {}}, NOW) == {
        "action": "wait",
        "note": "waiting on you",
    }


def test_an_unknown_peer_or_a_failed_send_fails(peers, monkeypatch):
    f = _flight({"who": "Nobody"})
    with pytest.raises(StepFailed, match="no peer link named Nobody"):
        asyncio.run(S._run(f, f["steps"][0], {"action": "run"}, NOW))
    peers.fail = "Dana isn't connected right now"
    g = _flight({"who": "Dana"}, session="s2")
    with pytest.raises(StepFailed, match="could not reach Dana: Dana isn't connected"):
        asyncio.run(S._run(g, g["steps"][0], {"action": "run"}, NOW))


def test_the_webhook_gets_json(hook):
    url, seen, _status = hook
    f, out = _fly({"webhook": url, "text": "Approve the deploy", "items": ["prod"]})
    ss = _ss(f)
    assert ss["state"] == "waiting_you" and ss["detail"]["sent_to"] == "the webhook"
    (body,) = seen
    assert body["items"] == ["prod"]
    assert body["done_url"] == ss["detail"]["done_url"]
    assert body["text"].startswith("Approve the deploy\n• prod")
    assert body["text"].endswith(body["done_url"])


def test_a_refusing_webhook_fails(hook):
    url, _seen, status = hook
    status[0] = 500
    f = _flight({"webhook": url})
    with pytest.raises(StepFailed, match="the webhook answered HTTP 500"):
        asyncio.run(S._run(f, f["steps"][0], {"action": "run"}, NOW))


def test_the_done_link(peers, client):
    f, _out = _fly({"who": "Mauricio", "text": "Check <the> page", "items": ["a&b"]})
    tok = _ss(f)["detail"]["token"]
    page = client.get("/api/hooks/flight/%s/done" % tok)
    assert page.status_code == 200 and "text/html" in page.headers["content-type"]
    assert "Check &lt;the&gt; page" in page.text and "<li>a&amp;b</li>" in page.text
    assert '<form method="post">' in page.text and "Ship it" in page.text
    # GET has no side effect.
    assert _ss(F.get(f["id"]))["state"] == "waiting_you"
    done = client.post("/api/hooks/flight/%s/done" % tok)
    assert done.status_code == 200 and "Thanks" in done.text
    g = F.get(f["id"])
    assert g["state"] == "done"
    assert _ss(g)["state"] == "done"
    assert _ss(g)["detail"]["receipt"] == "done by Mauricio"
    assert _ss(g)["note"] == "done by Mauricio"
    # Spent.
    assert client.post("/api/hooks/flight/%s/done" % tok).status_code == 404
    assert client.get("/api/hooks/flight/%s/done" % tok).status_code == 404


def test_your_own_done_keeps_done_by_you_and_spends_the_link(peers, client):
    f, _out = _fly({"who": "Dana"})
    tok = _ss(f)["detail"]["token"]
    D.operate(f["id"], "done", f["steps"][0]["id"], note="")
    g = F.get(f["id"])
    assert _ss(g)["state"] == "done" and _ss(g)["detail"]["receipt"] == "done by you"
    assert client.get("/api/hooks/flight/%s/done" % tok).status_code == 404
    assert client.post("/api/hooks/flight/%s/done" % tok).status_code == 404


def test_a_someone_token_is_not_a_listen_hook(peers, client):
    f, _out = _fly({"who": "Dana"})
    tok = _ss(f)["detail"]["token"]
    assert client.post("/api/hooks/flight/" + tok, content=b"{}").status_code == 404
    assert _ss(F.get(f["id"]))["state"] == "waiting_you"


def test_judge_resends_only_when_nothing_was_sent():
    f = _flight({"who": "Dana", "text": "t", "items": ["x"]})
    step = f["steps"][0]
    ss = _ss(f)
    ss["state"] = "running"
    assert S._judge(f, step, ss, {}, NOW)["action"] == "run"
    ss["detail"]["token"] = "f" * 32
    assert S._judge(f, step, ss, {}, NOW) == {
        "action": "you",
        "note": "t",
        "detail": {"items": ["x"]},
    }
    # A restart mid-run (running, token recorded) parks it via the planner.
    assert F.next_action(f, {"session": {}}, NOW)["action"] == "you"


def test_relative_link_without_a_public_base(peers, monkeypatch):
    monkeypatch.setattr(H, "public_base", lambda: "")
    f, _out = _fly({"who": "Dana"})
    tok = _ss(f)["detail"]["token"]
    assert _ss(f)["detail"]["done_url"] == "/api/hooks/flight/%s/done" % tok
