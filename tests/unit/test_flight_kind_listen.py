"""The ``listen`` stop and the public hook route (docs/flights.md "Round 15",
task ``hooks``): a token minted at run (found again after a restart), the
inbound ``POST /api/hooks/flight/{token}`` storing the body, ``match``
filtering, the timeout, and the auth rule that opens exactly these paths."""

import asyncio
import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.web.core import auth
from backend.web.core import flight_driver as D
from backend.web.core import flights as F
from backend.web.core.flight_kinds import _hooks as H
from backend.web.core.flight_kinds import get as get_kind
from backend.web.core.flight_kinds import listen as L

NOW = time.time()
TOK = "0123456789abcdef0123456789abcdef"  # pragma: allowlist secret


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_FLIGHTS_DIR", str(tmp_path / "flights"))
    monkeypatch.setenv("MINDFLOCK_AUTOPILOT_FILE", str(tmp_path / "autopilot.json"))
    monkeypatch.setattr(D, "wake", lambda: None)
    monkeypatch.setattr(H, "public_base", lambda: "")
    yield


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(H.router)
    return TestClient(app)


def _listening(config=None, session="s"):
    """A stored flight whose listen stop is running with its token armed, as
    the driver leaves it (intent applied, ``run``'s outcome recorded)."""
    plan = F.adhoc_plan([{"kind": "listen", "config": config or {}}], name="t")
    f = F.take_off(plan, session=session, now=NOW)
    sid = f["steps"][0]["id"]
    F.mutate(f["id"], lambda x: F.apply(x, {"action": "run", "note": ""}, NOW))
    f = F.get(f["id"])
    out = asyncio.run(L._run(f, f["steps"][0], {"action": "run", "note": ""}, NOW))
    F.mutate(
        f["id"],
        lambda x: x["step_states"][sid]["detail"].update(out["detail"]),
    )
    return F.get(f["id"]), sid, out


def _judge(fid, now=NOW):
    f = F.get(fid)
    step = f["steps"][f["cursor"]]
    return L._judge(f, step, f["step_states"][step["id"]], {}, now)


# --------------------------------------------------------------------------- #
# The auth rule
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "method,path,public",
    [
        ("POST", "/api/hooks/flight/" + TOK, True),
        ("POST", "/api/hooks/flight/%s/done" % TOK, True),
        ("GET", "/api/hooks/flight/%s/done" % TOK, True),
        ("get", "/api/hooks/flight/%s/done" % TOK, True),
        ("GET", "/api/hooks/flight/" + TOK, False),
        ("PUT", "/api/hooks/flight/" + TOK, False),
        ("DELETE", "/api/hooks/flight/%s/done" % TOK, False),
        ("POST", "/api/hooks/flight/" + TOK.upper(), False),
        ("POST", "/api/hooks/flight/" + TOK[:-1], False),
        ("POST", "/api/hooks/flight/%s0" % TOK, False),
        ("POST", "/api/hooks/flight/%s/done/x" % TOK, False),
        ("POST", "/api/hooks/flight/%s/" % TOK, False),
        ("POST", "/api/hooks/flyway/" + TOK, False),
        ("POST", "/api/flights", False),
    ],
)
def test_public_flight_hook_route(method, path, public):
    scope = {"type": "http", "method": method, "path": path}
    assert auth._public_flight_hook_route(scope) is public


def test_public_flight_hook_is_http_only():
    scope = {"type": "websocket", "path": "/api/hooks/flight/" + TOK}
    assert auth._public_flight_hook_route(scope) is False


def test_regex_is_the_contracts():
    assert auth._PUBLIC_FLIGHT_HOOK.pattern == r"/api/hooks/flight/[a-f0-9]{32}(/done)?"


# --------------------------------------------------------------------------- #
# The kind
# --------------------------------------------------------------------------- #
def test_kind_is_listed_and_registers_the_router():
    row = next(k for k in F.kinds_dto()["kinds"] if k["kind"] == "listen")
    assert row["family"] == "outside" and row["risk"] == [] and row["actor"] == "robot"
    assert "listen" not in F.SESSION_KINDS
    assert get_kind("listen").router is H.router
    assert sum(1 for k in ("call", "someone") if get_kind(k).router is not None) == 0


def test_config_and_title():
    step = F.normalize_step({"kind": "listen"}, 0)
    assert step["config"] == {"match": "", "timeout_minutes": 0}
    assert step["title"] == "Listen for a hook"
    step = F.normalize_step(
        {"kind": "listen", "config": {"match": "resolved", "timeout_minutes": "30"}}, 0
    )
    assert step["config"] == {"match": "resolved", "timeout_minutes": 30}
    assert step["title"] == "Listen for resolved"


def test_run_mints_once_and_a_restart_finds_the_same_token():
    f, sid, out = _listening()
    tok = out["detail"]["token"]
    assert len(tok) == 32 and int(tok, 16) >= 0
    assert out["detail"]["url"] == "/api/hooks/flight/" + tok
    assert out["note"] == "waiting at /api/hooks/flight/" + tok
    again = asyncio.run(L._run(f, f["steps"][0], {"action": "run", "note": ""}, NOW))
    assert again["detail"]["token"] == tok
    stored = json.load(open(H._path()))["tokens"]
    assert stored[tok]["flight"] == f["id"] and stored[tok]["step"] == sid


def test_full_url_when_the_base_is_known(monkeypatch):
    monkeypatch.setattr(H, "public_base", lambda: "http://box:8765")
    _f, _sid, out = _listening()
    assert out["detail"]["full_url"] == "http://box:8765" + out["detail"]["url"]


def test_judge_waits_then_hears(client):
    f, sid, out = _listening()
    verdict = _judge(f["id"])
    assert verdict["action"] == "wait"
    assert verdict["note"] == "waiting at " + out["detail"]["url"]
    r = client.post(out["detail"]["url"], json={"status": "resolved", "n": 1})
    assert r.status_code == 200 and r.json() == {"ok": True, "heard": True}
    d = F.get(f["id"])["step_states"][sid]["detail"]
    assert d["payload"] == {"status": "resolved", "n": 1}
    verdict = _judge(f["id"])
    assert verdict["action"] == "done" and verdict["detail"] == {"receipt": "heard"}
    # Spent: a second post is a 404, and the stored payload stays.
    assert client.post(out["detail"]["url"], content=b"again").status_code == 404
    d = F.get(f["id"])["step_states"][sid]["detail"]
    assert d["payload"] == {"status": "resolved", "n": 1}


def test_text_body_and_the_8k_cap(client):
    f, sid, out = _listening()
    big = "x" * (H.MAX_PAYLOAD + 100)
    assert client.post(out["detail"]["url"], content=big.encode()).status_code == 200
    d = F.get(f["id"])["step_states"][sid]["detail"]
    assert d["payload"] == "x" * H.MAX_PAYLOAD and d["truncated"] is True


def test_too_large_is_refused(client):
    _f, _sid, out = _listening()
    r = client.post(out["detail"]["url"], content=b"x" * (H.MAX_READ + 1))
    assert r.status_code == 413


def test_a_body_without_the_match_is_ignored_and_keeps_listening(client):
    f, sid, out = _listening({"match": "resolved"})
    r = client.post(out["detail"]["url"], json={"status": "firing"})
    assert r.status_code == 202 and r.json() == {"ok": True, "ignored": True}
    d = F.get(f["id"])["step_states"][sid]["detail"]
    assert "payload" not in d and "firing" in d["ignored"]
    assert _judge(f["id"])["action"] == "wait"
    r = client.post(out["detail"]["url"], json={"status": "resolved"})
    assert r.status_code == 200
    assert _judge(f["id"])["action"] == "done"


def test_a_match_edited_after_arrival_listens_again():
    f, sid, out = _listening()
    F.mutate(
        f["id"],
        lambda x: (
            x["steps"][0]["config"].update(match="resolved"),
            x["step_states"][sid]["detail"].update(payload={"status": "firing"}),
        ),
    )
    assert _judge(f["id"])["action"] == "run"


def test_unknown_token_and_a_stop_no_longer_listening(client):
    assert client.post("/api/hooks/flight/" + TOK, content=b"{}").status_code == 404
    f, sid, out = _listening()
    F.mutate(f["id"], lambda x: F.op_cancel(x))
    assert client.post(out["detail"]["url"], content=b"{}").status_code == 404


def test_a_listen_token_is_not_a_done_link(client):
    _f, _sid, out = _listening()
    assert client.get(out["detail"]["url"] + "/done").status_code == 404
    assert client.post(out["detail"]["url"] + "/done").status_code == 404


def test_timeout_fails_and_zero_waits_forever():
    f, _sid, _out = _listening({"timeout_minutes": 10})
    assert _judge(f["id"], NOW + 9 * 60)["action"] == "wait"
    verdict = _judge(f["id"], NOW + 11 * 60)
    assert verdict == {"action": "fail", "reason": "nothing arrived in 10 min"}
    g, _sid, _out = _listening(session="s2")
    assert _judge(g["id"], NOW + 365 * 86400)["action"] == "wait"


def test_judge_without_a_token_arms_again():
    plan = F.adhoc_plan([{"kind": "listen"}], name="t")
    f = F.take_off(plan, session="s", now=NOW)
    step = f["steps"][0]
    ss = f["step_states"][step["id"]]
    ss["state"] = "running"
    assert L._judge(f, step, ss, {}, NOW)["action"] == "run"


def test_the_planner_runs_it_without_a_session():
    plan = F.adhoc_plan([{"kind": "listen"}], name="t")
    f = F.take_off(plan, session="", now=NOW)
    assert F.next_action(f, {"session": {}}, NOW)["action"] == "run"
