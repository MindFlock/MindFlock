"""The ``call`` stop (docs/flights.md "Round 15", task ``hooks``): one HTTP
request on a thread, judged by its status prefix; ``{receipt}`` fills from
the previous flown stop."""

import asyncio
import http.server
import json
import threading
import time

import pytest

from backend.web.core import flight_driver as D
from backend.web.core import flights as F
from backend.web.core.flight_kinds import StepFailed
from backend.web.core.flight_kinds import call as C

NOW = time.time()


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_FLIGHTS_DIR", str(tmp_path / "flights"))
    monkeypatch.setenv("MINDFLOCK_AUTOPILOT_FILE", str(tmp_path / "autopilot.json"))
    yield


@pytest.fixture
def server():
    """A local HTTP server: ``/ok`` answers 200 "fine", ``/boom`` 500 "boom";
    ``seen`` records each request's method, headers and body."""
    seen = []

    class H(http.server.BaseHTTPRequestHandler):
        def _answer(self):
            n = int(self.headers.get("Content-Length") or 0)
            seen.append(
                {
                    "method": self.command,
                    "path": self.path,
                    "headers": dict(self.headers),
                    "body": self.rfile.read(n).decode() if n else "",
                }
            )
            code, text = (500, "boom") if self.path == "/boom" else (200, "fine")
            self.send_response(code)
            self.send_header("Content-Length", str(len(text)))
            self.end_headers()
            self.wfile.write(text.encode())

        do_GET = do_POST = do_PUT = _answer

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:%d" % srv.server_address[1], seen
    srv.shutdown()


def _flight(steps):
    return F.take_off(F.adhoc_plan(steps, name="t"), session="s", now=NOW)


def _until_done(f, step, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        job = asyncio.run(C._observe(f, step, {}, {}, NOW))
        if job and not job["running"]:
            return job
        time.sleep(0.02)
    raise AssertionError("the call never finished")


def test_kind_is_listed():
    row = next(k for k in F.kinds_dto()["kinds"] if k["kind"] == "call")
    assert row["family"] == "outside" and row["risk"] == ["remote"]
    assert row["actor"] == "robot"
    assert [x["name"] for x in row["fields"]] == [
        "method",
        "url",
        "headers",
        "body",
        "expect",
        "timeout_minutes",
    ]
    assert "call" not in F.SESSION_KINDS


def test_config_strict_and_lenient():
    step = F.normalize_step({"kind": "call", "config": {"url": "https://x.dev/a"}}, 0)
    assert step["config"] == {
        "method": "POST",
        "url": "https://x.dev/a",
        "headers": "",
        "body": "",
        "expect": "2",
        "timeout_minutes": 5,
    }
    assert step["title"] == "Call x.dev"
    with pytest.raises(F.PlanError) as err:
        F.normalize_step({"kind": "call", "config": {}}, 0)
    assert err.value.field == "steps[0].config.url"
    with pytest.raises(F.PlanError) as err:
        F.normalize_step(
            {"kind": "call", "config": {"url": "https://x", "method": "DELETE"}}, 0
        )
    assert err.value.field == "steps[0].config.method"
    with pytest.raises(F.PlanError):
        F.normalize_step({"kind": "call", "config": {"url": "ftp://x"}}, 0)
    lax = F.normalize_step(
        {"kind": "call", "config": {"method": "DELETE"}}, 0, strict=False
    )
    assert lax["config"]["method"] == "POST" and lax["title"] == "Call a URL"


def test_parse_headers():
    assert C.parse_headers("A: 1\n\nno colon\nB:  two: parts \n: v") == [
        ("A", "1"),
        ("B", "two: parts"),
    ]


def test_success_is_done_with_the_status_receipt(server):
    base, seen = server
    f = _flight(
        [
            {"kind": "notify", "config": {"text": "x"}},
            {
                "kind": "call",
                "config": {
                    "url": base + "/ok?b={branch}",
                    "headers": "Authorization: Bearer s3cret\nX-Thing: 1",  # pragma: allowlist secret
                    "body": '{"after": "{receipt}"}',
                },
            },
        ]
    )
    first, step = f["steps"]
    f["step_states"][first["id"]].update(state="done", detail={"receipt": "sent"})
    f["branch"] = "feat"
    out = asyncio.run(C._run(f, step, {"action": "run", "note": ""}, NOW))
    assert out["action"] == "run" and out["detail"] == {"host": "127.0.0.1"}
    job = _until_done(f, step)
    req = seen[0]
    assert req["method"] == "POST" and req["path"] == "/ok?b=feat"
    assert (
        req["headers"]["Authorization"] == "Bearer s3cret"
    )  # pragma: allowlist secret
    assert req["headers"]["Content-Type"] == "application/json"
    assert json.loads(req["body"]) == {"after": "sent"}
    ss = f["step_states"][step["id"]]
    ss.update(state="running", detail=out["detail"])
    verdict = C._judge(f, step, ss, {"call": job}, NOW)
    assert verdict["action"] == "done"
    assert verdict["detail"] == {"status": 200, "body": "fine", "receipt": "200"}


def test_wrong_status_fails_with_the_tail(server):
    base, _seen = server
    f = _flight([{"kind": "call", "config": {"url": base + "/boom", "method": "GET"}}])
    step = f["steps"][0]
    asyncio.run(C._run(f, step, {"action": "run", "note": ""}, NOW))
    job = _until_done(f, step)
    ss = f["step_states"][step["id"]]
    verdict = C._judge(f, step, ss, {"call": job}, NOW)
    assert verdict["action"] == "fail"
    assert verdict["reason"] == "HTTP 500: boom"
    assert verdict["detail"]["status"] == 500


def test_expect_prefix(server):
    base, _seen = server
    f = _flight([{"kind": "call", "config": {"url": base + "/boom", "expect": "5"}}])
    step = f["steps"][0]
    asyncio.run(C._run(f, step, {"action": "run", "note": ""}, NOW))
    verdict = C._judge(
        f, step, f["step_states"][step["id"]], {"call": _until_done(f, step)}, NOW
    )
    assert verdict["action"] == "done" and verdict["detail"]["receipt"] == "500"


def test_unreachable_fails():
    f = _flight([{"kind": "call", "config": {"url": "http://127.0.0.1:1/x"}}])
    step = f["steps"][0]
    asyncio.run(C._run(f, step, {"action": "run", "note": ""}, NOW))
    job = _until_done(f, step)
    ss = f["step_states"][step["id"]]
    ss["detail"]["host"] = "127.0.0.1"
    verdict = C._judge(f, step, ss, {"call": job}, NOW)
    assert verdict["action"] == "fail"
    assert verdict["reason"].startswith("no answer from 127.0.0.1:")


def test_judge_reruns_a_lost_request_and_waits_on_a_running_one():
    f = _flight([{"kind": "call", "config": {"url": "https://x.dev"}}])
    step = f["steps"][0]
    ss = f["step_states"][step["id"]]
    assert C._judge(f, step, ss, {}, NOW)["action"] == "run"
    out = C._judge(f, step, ss, {"call": {"running": True, "method": "POST"}}, NOW)
    assert out["action"] == "wait" and out["note"].startswith("calling POST")


def test_a_filled_url_that_is_not_http_refuses():
    f = _flight([{"kind": "call", "config": {"url": "{subject}"}}])
    step = f["steps"][0]
    with pytest.raises(StepFailed):
        asyncio.run(C._run(f, step, {"action": "run", "note": ""}, NOW))


def test_the_driver_runs_it(server, monkeypatch):
    base, seen = server
    f = _flight([{"kind": "call", "config": {"url": base + "/ok", "method": "PUT"}}])
    assert F.next_action(f, {"session": {}}, NOW)["action"] == "run"
    out = asyncio.run(D._perform(f, {"action": "run", "note": ""}, NOW))
    assert out["note"].startswith("calling PUT")
    _until_done(f, f["steps"][0])
    assert seen[0]["method"] == "PUT"
