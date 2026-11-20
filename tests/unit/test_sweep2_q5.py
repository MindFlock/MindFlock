"""UI sweep round 2, package Q5: server request paths and the phone page.

- Make PR, the Make-PR dialog's branch list and push-branch run their git and
  file reads through ``asyncio.to_thread``, never inline on the event loop
  every pane's output is served from.
- New → Ticket's Retry sends the shown draft back and the server files it
  without a second model turn.
- The phone's Diff tab clears the old diff while a new one loads, Recently
  closed's Reopen says "Reopening…", and a dropped connection reads as
  "Couldn't reach MindFlock" in the ask sheet and update banner.

No live session is touched: every route here runs against a stubbed instance.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from backend.ticket_ingestion.config import TicketProviderConfig
from backend.web import server
from backend.web.core import ticket_compose, ticket_draft
from tests.unit.test_ticket_compose import _Cfg, _Recorder

client = TestClient(server.app)

ROOT = Path(__file__).resolve().parents[2]
MOBILE = (ROOT / "backend" / "web" / "static" / "mobile.js").read_text()


# --------------------------------------------------------------------------- #
# #3 Make PR / branches / push-branch: blocking reads go off the event loop
# --------------------------------------------------------------------------- #
class _Inst:
    Title = "q5-fake"
    Branch = "feature"
    BaseBranch = "main"
    InPlace = False

    def GetWorktreePath(self):
        return "/nonexistent/q5-wt"


@pytest.fixture
def threaded(monkeypatch):
    """Route stubs in, ``asyncio.to_thread`` recorded: ``calls`` lists each
    blocking helper with whether it ran inside a to_thread hop."""
    state = {"in_thread": False, "calls": []}

    async def _to_thread(fn, *a, **k):
        prev = state["in_thread"]
        state["in_thread"] = True
        try:
            return fn(*a, **k)
        finally:
            state["in_thread"] = prev

    def _rec(name, value):
        def _f(*_a, **_k):
            state["calls"].append((name, state["in_thread"]))
            return value

        return _f

    async def _no_gate(*_a, **_k):
        return None

    monkeypatch.setattr(asyncio, "to_thread", _to_thread)
    monkeypatch.setattr(server, "git_available", lambda: True)
    monkeypatch.setattr(server, "_inst_or_404", lambda t: (_Inst(), None))
    monkeypatch.setattr(server._peer_guard, "refusal", lambda *a, **k: None)
    monkeypatch.setattr(server, "_red_zone_gate", _no_gate)
    monkeypatch.setattr(server, "_configured_pr_base", _rec("pr_base", ""))
    monkeypatch.setattr(server, "_session_base_branch", _rec("session_base", "main"))
    # On the base branch: make-pr answers its 409 before any network rung.
    monkeypatch.setattr(server, "_current_branch", _rec("current_branch", "main"))
    monkeypatch.setattr(
        server,
        "_run_capped",
        lambda *a, **k: subprocess.CompletedProcess(a, 1, b"", b""),
    )
    state["rec"] = _rec
    return state


def test_make_pr_reads_base_and_branch_off_the_event_loop(threaded):
    r = client.post("/api/instances/q5-fake/make-pr", json={})
    assert r.status_code == 409, r.text
    names = [n for n, _ in threaded["calls"]]
    assert {"pr_base", "session_base", "current_branch"} <= set(names)
    assert all(on for _, on in threaded["calls"]), threaded["calls"]


def test_make_pr_dialog_base_wins_and_still_reads_the_branch_off_the_loop(threaded):
    r = client.post("/api/instances/q5-fake/make-pr", json={"base": "main"})
    assert r.status_code == 409
    assert [n for n, _ in threaded["calls"]] == ["current_branch"]
    assert all(on for _, on in threaded["calls"])


def test_the_make_pr_branch_list_reads_its_default_base_off_the_loop(threaded):
    r = client.get("/api/instances/q5-fake/branches")
    assert r.status_code == 200, r.text
    assert r.json()["default"] == "main"
    assert ("pr_base", True) in threaded["calls"]
    assert ("session_base", True) in threaded["calls"]
    assert all(on for _, on in threaded["calls"]), threaded["calls"]


def test_push_branch_reads_its_check_gate_off_the_loop(threaded, monkeypatch):
    rec = threaded["rec"]
    monkeypatch.setattr(
        server._wt_setup,
        "load_config",
        rec("load_config", SimpleNamespace(check_command="make test")),
    )
    monkeypatch.setattr(server._wt_setup, "check_summary", rec("check_summary", None))
    r = client.post("/api/instances/q5-fake/push-branch", json={})
    assert r.status_code == 409
    assert r.json()["check_required"] is True
    assert ("load_config", True) in threaded["calls"]
    assert ("check_summary", True) in threaded["calls"]


def test_push_branch_skips_the_check_read_when_no_check_is_declared(
    threaded, monkeypatch
):
    rec = threaded["rec"]
    monkeypatch.setattr(
        server._wt_setup,
        "load_config",
        rec("load_config", SimpleNamespace(check_command="")),
    )
    monkeypatch.setattr(server._wt_setup, "check_summary", rec("check_summary", None))
    monkeypatch.setattr(server, "_has_origin", lambda *a, **k: False)
    r = client.post("/api/instances/q5-fake/push-branch", json={})
    # Past the gate: the next refusal is the missing origin.
    assert r.status_code == 400
    assert ("load_config", True) in threaded["calls"]
    assert "check_summary" not in [n for n, _ in threaded["calls"]]


# --------------------------------------------------------------------------- #
# #11 New → Ticket: Retry files the shown draft, no second model turn
# --------------------------------------------------------------------------- #
_SHOWN = {
    "name": "Time out SSO logins",
    "description": "It hangs for SSO users.\n\n## Acceptance Criteria\n- It times out",
    "criteria": ["It times out"],
}


def _retry(provider, draft, text="the login page hangs for sso users"):
    src = TicketProviderConfig(provider="rec", id="rec", label="Recorder")

    def _no_model(*_a, **_k):
        raise AssertionError("drafted again on a retry")

    with (
        patch.object(ticket_compose, "_sources", return_value=(_Cfg(), [src])),
        patch("backend.ticket_ingestion.providers.get_provider", return_value=provider),
        patch.object(ticket_draft, "draft", _no_model),
    ):
        return client.post(
            "/api/tickets/compose",
            json={"source": "rec", "text": text, "draft": draft},
        )


def test_a_retry_files_the_shown_draft_without_a_model_turn():
    provider = _Recorder()
    r = _retry(provider, _SHOWN)
    assert r.status_code == 200, r.text
    assert provider.filed == [(_SHOWN["name"], _SHOWN["description"])]
    body = r.json()
    assert body["url"] == "https://tracker/1"
    assert body["criteria"] == ["It times out"]


def test_a_retry_that_fails_again_still_hands_the_draft_back():
    r = _retry(_Recorder(fail="token expired"), _SHOWN)
    assert r.status_code == 502
    assert "token expired" in r.json()["error"]
    assert r.json()["draft"]["name"] == _SHOWN["name"]


def test_a_retried_draft_is_validated_like_a_drafted_one():
    provider = _Recorder()
    r = _retry(provider, {"name": "", "description": "some body text here please"})
    assert r.status_code == 400
    assert "title" in r.json()["error"]
    r = _retry(provider, "not a draft")
    assert r.status_code == 400
    assert provider.filed == []


def test_a_retried_draft_is_cleaned_and_capped_like_a_drafted_one():
    provider = _Recorder()
    long_name = "x" * (ticket_draft.MAX_TITLE + 50)
    r = _retry(
        provider,
        {
            "name": long_name,
            "description": "It hangs for SSO users.\n\n## Acceptance Criteria\n- a\n- b",
            "criteria": ["**a**", "- b"],
        },
    )
    assert r.status_code == 200, r.text
    name, desc = provider.filed[0]
    assert len(name) == ticket_draft.MAX_TITLE
    # The criteria section is rendered once, from the cleaned criteria.
    assert desc.count(ticket_draft.AC_HEADING) == 1
    assert desc.endswith("- a\n- b")


def test_without_a_draft_the_route_still_drafts():
    seen = {}

    async def _ok(source, text, *, program=""):
        seen["text"] = text
        return {"source": source, "id": "1", "url": "https://t/1"}

    with patch.object(ticket_compose, "compose", _ok):
        r = client.post(
            "/api/tickets/compose", json={"source": "rec", "text": "fix the thing"}
        )
    assert r.status_code == 200
    assert seen["text"] == "fix the thing"


# --------------------------------------------------------------------------- #
# #10 / #36 the phone page
# --------------------------------------------------------------------------- #
def _whole(js: str, name: str) -> str:
    start = js.index("  function " + name + "(")
    end = js.index("\n  }\n", start) + len("\n  }\n")
    return js[start:end]


def test_the_phone_diff_clears_the_old_diff_while_loading():
    body = _whole(MOBILE, "loadDiff")
    assert 'diffNote("Loading diff…");' in body
    assert body.index('diffNote("Loading diff…")') < body.index("fetch(")
    assert '"loading…"' not in body


def test_the_recent_sheet_says_loading_in_sentence_case():
    body = _whole(MOBILE, "openRecentSheet")
    assert 'folderNote("Loading…")' in body
    assert 'folderNote("loading…")' not in body


def test_reopen_says_reopening_until_it_settles():
    body = _whole(MOBILE, "reopenClosed")
    assert 'btn.textContent = "Reopening…";' in body
    # A failure puts the label back so the row can be tried again.
    fail = body[body.index(".catch(") :]
    assert 'btn.textContent = "Reopen";' in fail


def test_no_browser_words_in_the_ask_sheet_or_the_update_banner():
    for line in (
        'askErr("Couldn\'t answer: " + String(',
        'askErr("Couldn\'t send: " + String(',
        'showUpdate("Couldn\'t update: " + err.message',
        "j: { error: String((err && err.message) || err) } };",
    ):
        assert line not in MOBILE, line
    assert 'askErr(failLine("Couldn\'t answer", errText(err, String(err))));' in MOBILE
    assert 'askErr(failLine("Couldn\'t send", errText(err, String(err))));' in MOBILE
    assert (
        'showUpdate(failLine("Couldn\'t update", errText(err, String(err))),' in MOBILE
    )
    assert MOBILE.count("j: { error: errText(err, String(err)) } };") == 2


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_fail_line_says_unreachable_whole_and_prefixes_everything_else():
    var = next(
        ln.strip()
        for ln in MOBILE.splitlines()
        if ln.lstrip().startswith("var UNREACHABLE = ")
    )
    script = "\n".join(
        [
            var,
            _whole(MOBILE, "errText"),
            _whole(MOBILE, "failLine"),
            "var dropped = new TypeError('Load failed');",
            "console.log(JSON.stringify([",
            '  failLine("Couldn\'t answer", errText(dropped, String(dropped))),',
            "  failLine(\"Couldn't send\", errText(new Error('busy'), 'x')),",
            "  failLine(\"Couldn't update\", 'HTTP 500'),",
            "]));",
        ]
    )
    out = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, check=True
    )
    assert json.loads(out.stdout) == [
        "Couldn't reach MindFlock. Is it still running?",
        "Couldn't send: busy",
        "Couldn't update: HTTP 500",
    ]
