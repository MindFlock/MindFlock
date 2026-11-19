"""Error polish: the sentences a person reads when something goes wrong.

* Closing / deleting / cleaning up a session that is already gone: the web
  client shows ``detail`` ("That session is already gone."), while ``error``
  keeps the ``instance not found`` text the MCP tools and CLI match on.
* A duplicate name on create: ``detail`` reads "Another session is already
  named foo", ``error`` keeps ``instance foo already exists`` for the drivers.
* A doctor check that throws says the check itself couldn't run, not
  "check errored: <exception>".
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from backend import doctor
from backend.web import server
from backend.web.core import session_create as sc

client = TestClient(server.app)

GONE = "no-such-session-polish-errors"


def test_ending_a_session_that_is_gone_reads_as_a_sentence():
    for method, path in (
        ("delete", "/api/instances/" + GONE),
        ("post", "/api/instances/%s/close" % GONE),
        ("post", "/api/instances/%s/cleanup" % GONE),
    ):
        r = getattr(client, method)(path)
        assert r.status_code == 404, path
        body = r.json()
        assert body["detail"] == "That session is already gone.", path
        # The machine text stays for the MCP tools / CLI that match on it.
        assert body["error"] == "instance not found: " + GONE, path


def test_the_end_session_helper_shares_the_sentence():
    r = server._session_gone("x")
    assert r.status_code == 404
    assert json.loads(r.body) == {
        "error": "instance not found: x",
        "detail": server.SESSION_GONE_DETAIL,
    }


def test_a_taken_name_reads_as_a_sentence(monkeypatch):
    title = "polish-errors-taken"
    monkeypatch.setitem(server.ENGINE.instances, title, object())
    r = sc._title_refusal(title)
    assert r is not None and r.status_code == 409
    body = json.loads(r.body)
    assert body["detail"] == (
        "Another session is already named %s. Pick a different name." % title
    )
    # The drivers and MCP tools retry on this text: it must survive.
    assert "already exists" in body["error"]
    assert body["error"] == "instance %s already exists" % title


def test_no_bare_instance_already_exists_body_is_left():
    """Every live-name 409 in session_create goes through _name_taken, so none
    reaches a person as the bare ``instance foo already exists``."""
    import inspect

    src = inspect.getsource(sc)
    assert src.count('"instance %s already exists" % title') == 1  # the helper
    assert src.count("_name_taken(title)") == 4


def test_a_check_that_throws_says_it_could_not_run(monkeypatch):
    def check_flaky_probe():
        raise RuntimeError("boom: the probe fell over\nTraceback noise")

    monkeypatch.setattr(doctor, "_ALL_CHECKS", [check_flaky_probe])
    monkeypatch.setattr(doctor, "_note_setup_progress", lambda checks: None)
    (c,) = doctor.run_checks()
    assert c.id == "flaky-probe" and c.status == "warn"
    assert c.detail == (
        "This check couldn't run, so its result is unknown. Try again. "
        "(boom: the probe fell over)"
    )
    assert "check errored" not in c.detail


def test_a_crashed_check_detail_is_short_and_never_empty():
    long = doctor._check_crashed_detail(ValueError("x" * 500))
    assert long.endswith("...)") and len(long) < 200
    assert doctor._check_crashed_detail(KeyError()).endswith("(KeyError)")
