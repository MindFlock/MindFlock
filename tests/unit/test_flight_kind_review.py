"""The ``review`` stop — a second opinion (docs/flights.md "Round 15", task
``review-pilot``): ``run`` spawns one reviewer through ``spawn_worker`` with
the brief, ``observe`` reads it (and closes it once finished unless
``keep``), ``judge`` turns its report into done / findings / a fail."""

import asyncio
from types import SimpleNamespace

import pytest

from backend.mcp import gitlocal as _gitlocal
from backend.web.core import flight_driver as D
from backend.web.core import flights as F
from backend.web.core import session_end as _session_end
from backend.web.core.flight_kinds import review as R

NOW = 1_800_000_000.0


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_FLIGHTS_DIR", str(tmp_path / "flights"))
    monkeypatch.setenv("MINDFLOCK_AUTOPILOT_FILE", str(tmp_path / "autopilot.json"))
    R._FINISHED.clear()
    yield
    R._FINISHED.clear()


def sess(**kw):
    return dict({"present": True, "status": "running", "activity": "idle"}, **kw)


def fly(on_fail=None, session="s", **config):
    st = {"kind": "review", "config": config}
    if on_fail:
        st["on_fail"] = on_fail
    plan = F.save_plan(
        {"name": "P", "steps": [st, {"kind": "notify"}]},
        now=NOW,
    )
    return F.take_off(plan, session=session, now=NOW)


def go(f, obs, now):
    a = F.next_action(f, obs, now)
    F.apply(f, a, now)
    return a


def started(on_fail=None, session="s", **config):
    """A flight whose review stop has spawned ``review-1`` at NOW."""
    f = fly(on_fail, session, **config)
    a = F.next_action(f, {"session": sess()}, NOW)
    assert a["action"] == "run"
    F.apply(f, dict(a, detail={"children": ["review-1"], "base": "main"}), NOW)
    return f


def finished(text, status="done", closed="deleted"):
    entry = {
        "title": "review-1",
        "state": "failed" if status == "failed" else "done",
        "report": {"id": "m1", "status": status, "summary": text, "ts": NOW + 5},
    }
    return {"entry": entry, "text": text, "report": True, "closed": closed}


# --------------------------------------------------------------------------- #
# Registry + config
# --------------------------------------------------------------------------- #
def test_the_kind_is_registered():
    kinds = {k["kind"]: k for k in F.kinds_dto()["kinds"]}
    k = kinds["review"]
    assert k["family"] == "work" and k["actor"] == "robot" and k["risk"] == []
    assert [f["name"] for f in k["fields"]] == [
        "mode",
        "program",
        "model",
        "focus",
        "block",
        "keep",
    ]
    assert "review" in F.SESSION_KINDS and "review" not in F.AGENT_KINDS


def test_config_defaults_and_title():
    s = F.normalize_step({"kind": "review", "config": {}})
    assert s["config"] == {
        "mode": "bugs",
        "program": "",
        "model": "",
        "focus": "",
        "block": True,
        "keep": False,
    }
    assert s["title"] == "Second opinion"
    s = F.normalize_step(
        {
            "kind": "review",
            "config": {"program": " codex ", "block": "false", "keep": "yes"},
        }
    )
    assert s["config"]["program"] == "codex"
    assert s["config"]["block"] is False and s["config"]["keep"] is True
    assert s["title"] == "Second opinion (codex)"


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #
def test_run_spawns_the_reviewer_with_the_brief(monkeypatch):
    f = fly(program="codex", model="gpt-5.5", focus="the retry loop")
    calls: list = []

    async def _spawn(flight, step, prompt, **kw):
        calls.append((step["id"], prompt, kw))
        return "review-1"

    monkeypatch.setattr(D, "spawn_worker", _spawn)
    monkeypatch.setattr(
        D,
        "_server",
        lambda: SimpleNamespace(
            ENGINE=SimpleNamespace(
                instances={"s": SimpleNamespace(BaseBranch="develop")}
            )
        ),
    )
    out = asyncio.run(R._run(f, f["steps"][0], {"action": "run", "note": ""}, NOW))
    assert out["detail"] == {"children": ["review-1"], "base": "develop"}
    sid, prompt, kw = calls[0]
    assert sid == f["steps"][0]["id"]
    assert kw == {
        "model": "gpt-5.5",
        "program": "codex",
        "report_back": True,
        "title_prefix": "review",
    }
    assert "git diff develop...HEAD" in prompt
    assert "Change NO files" in prompt
    assert "`No findings`" in prompt and "1. path/to/file.py:123 —" in prompt
    assert "at most 10" in prompt
    assert prompt.rstrip().endswith("Also: the retry loop")


def test_the_base_falls_back_to_origin_head(monkeypatch):
    monkeypatch.setattr(
        D,
        "_server",
        lambda: SimpleNamespace(ENGINE=SimpleNamespace(instances={})),
    )
    assert R._base_of({"session": "s"}) == "origin/HEAD"
    assert "git diff origin/HEAD...HEAD" in R.brief("s", "origin/HEAD")
    assert "Also:" not in R.brief("s", "main")


def test_a_refused_spawn_fails_the_stop(monkeypatch):
    f = fly()

    async def _spawn(*_a, **_k):
        raise D._StepFailed("too many children")

    monkeypatch.setattr(D, "spawn_worker", _spawn)
    monkeypatch.setattr(R, "_base_of", lambda flight: "main")
    a = F.next_action(f, {"session": sess()}, NOW)
    out = asyncio.run(D._perform(f, a, NOW))
    assert out["action"] == "fail" and out["reason"] == "too many children"


# --------------------------------------------------------------------------- #
# judge
# --------------------------------------------------------------------------- #
def test_parse_findings():
    assert R.parse_findings("") == (0, "")
    assert R.parse_findings("No findings") == (0, "")
    assert R.parse_findings("no findings.\nDetails:\nlooked hard") == (0, "")
    text = "1. a.py:3 — off by one\n2. b.py:9 — None deref"
    assert R.parse_findings(text) == (2, text)
    assert R.parse_findings("the lock is never released") == (
        1,
        "the lock is never released",
    )
    n, _ = R.parse_findings("Found some\nDetails:\n1) x.py:1 — a\n2) y.py:2 — b")
    assert n == 2


def test_no_children_runs_and_unknown_waits():
    f = fly()
    a = F.next_action(f, {"session": sess()}, NOW)
    assert a["action"] == "run"
    # A restart between the run and its record: the stop runs again.
    F.apply(f, dict(a, detail={}), NOW)
    assert F.next_action(f, {"session": sess()}, NOW + 1)["action"] == "run"
    f = started(session="t")
    a = F.next_action(f, {"session": sess()}, NOW + 1)
    assert a["action"] == "wait" and a["note"] == "second opinion in progress"


def test_waits_while_the_reviewer_works():
    f = started()
    entry = {"title": "review-1", "state": "working"}
    a = go(f, {"session": sess(), "review": {"entry": entry, "text": ""}}, NOW + 9)
    assert a["action"] == "wait"
    assert a["note"] == "second opinion in progress (review-1)"
    assert f["step_states"][f["steps"][0]["id"]]["detail"]["reviewer"] == entry
    entry = {"title": "review-1", "state": "asking"}
    a = F.next_action(f, {"session": sess(), "review": {"entry": entry}}, NOW + 9)
    assert a["action"] == "wait" and "asking" in a["note"]


def test_no_findings_is_done():
    f = started()
    a = go(f, {"session": sess(), "review": finished("No findings")}, NOW + 10)
    assert a["action"] == "done"
    d = f["step_states"][f["steps"][0]["id"]]["detail"]
    assert d["receipt"] == "no findings" and d["findings"] == ""
    assert d["closed"] == "deleted" and d["children"] == ["review-1"]
    assert f["cursor"] == 1


def test_findings_block_the_stop():
    f = started()
    text = "1. a.py:3 — off by one\n2. b.py:9 — None deref"
    a = go(f, {"session": sess(), "review": finished(text)}, NOW + 10)
    assert a["action"] == "fail" and a["then"] == "halt"
    assert a["reason"].startswith("the second opinion has 2 findings:")
    assert "b.py:9 — None deref" in a["reason"]
    d = f["step_states"][f["steps"][0]["id"]]["detail"]
    assert d["findings"] == text and d["receipt"] == "2 findings"
    assert f["state"] == "halted"


def test_findings_go_to_the_agent_and_the_retry_asks_again():
    f = started(on_fail="agent")
    text = "1. a.py:3 — off by one"
    a = go(f, {"session": sess(), "review": finished(text)}, NOW + 10)
    assert a["action"] == "fail" and a["then"] == "agent"
    ss = f["step_states"][f["steps"][0]["id"]]
    assert ss["detail"]["phase"] == "fix" and "a.py:3" in ss["detail"]["reason"]
    assert "a.py:3 — off by one" in D._fix_note(f["steps"][0], a)
    # The agent fixed it and settled: the stop runs again — a new reviewer.
    after = sess(worked_at=NOW + 20, turn_ended_at=NOW + 21)
    a = go(f, {"session": after}, NOW + 30)
    assert a["action"] == "wait" and a["note"].startswith("waiting for the agent")
    a = F.next_action(f, {"session": after}, NOW + 400)
    assert a["action"] == "run" and a.get("retry") is True


def test_findings_without_block_are_done():
    f = started(block=False)
    text = "1. a.py:3 — off by one\n2. b.py:9 — None deref\n3. c.py:1 — race"
    a = go(f, {"session": sess(), "review": finished(text)}, NOW + 10)
    assert a["action"] == "done" and a["note"] == "3 findings"
    d = f["step_states"][f["steps"][0]["id"]]["detail"]
    assert d["receipt"] == "3 findings" and d["findings"] == text


def test_a_reviewer_that_did_not_finish_fails():
    f = started()
    a = F.next_action(
        f, {"session": sess(), "review": finished("broke", "failed")}, NOW + 10
    )
    assert a["action"] == "fail" and a["reason"] == "the reviewer did not finish"
    gone = {"entry": {"title": "review-1", "state": "gone"}, "text": ""}
    a = F.next_action(f, {"session": sess(), "review": gone}, NOW + 10)
    assert a["action"] == "fail" and a["reason"] == "the reviewer did not finish"


def test_a_reviewer_without_a_report_is_done_with_no_report():
    f = started(block=True)
    o = {"entry": {"title": "review-1", "state": "done"}, "text": "", "report": False}
    a = go(f, {"session": sess(), "review": o}, NOW + 10)
    assert a["action"] == "done" and "without a report" in a["note"]
    d = f["step_states"][f["steps"][0]["id"]]["detail"]
    assert d["receipt"] == "no report"


# --------------------------------------------------------------------------- #
# observe
# --------------------------------------------------------------------------- #
def _kid(**kw):
    return dict(
        {
            "title": "review-1",
            "present": True,
            "status": "running",
            "activity": "idle",
            "activity_since": NOW - 1000,
            "created": NOW - 1000,
            "settle_s": 8.0,
            "report": None,
        },
        **kw,
    )


def _msg(text, status="done", ts=NOW + 5):
    return {"id": "m1", "text": text, "data": {"status": status}, "ts": ts}


@pytest.fixture
def seen(monkeypatch):
    """Fake the reviewer's facts, its mailbox report and the close."""
    state = {"kid": _kid(), "msg": None, "closed": [], "reads": 0}

    def _children(flight, step):
        state["reads"] += 1
        return [state["kid"]] if state["kid"] else []

    monkeypatch.setattr(D, "_children_obs", _children)
    monkeypatch.setattr(R, "_report_message", lambda parent, t, since: state["msg"])

    def _close(parent, title):
        state["closed"].append((parent, title))
        return "deleted"

    monkeypatch.setattr(R, "_close", _close)
    return state


def _observe(f, now):
    step = f["steps"][0]
    ss = f["step_states"][step["id"]]
    return asyncio.run(R._observe(f, step, ss, {}, now))


def test_observe_waits_on_a_working_reviewer(seen):
    f = started()
    seen["kid"] = _kid(activity="working", activity_since=NOW + 1)
    o = _observe(f, NOW + 30)
    assert o["entry"]["state"] == "working" and "closed" not in o
    assert seen["closed"] == []


def test_observe_reads_the_full_report_and_closes_the_reviewer(seen):
    f = started()
    text = "1. a.py:3 — off by one\n2. b.py:9 — None deref"
    rep = {"id": "m1", "status": "done", "summary": "1. a.py:3 …", "ts": NOW + 5}
    seen["kid"] = _kid(report=rep, activity_since=NOW + 6)
    seen["msg"] = _msg(text + "\n\n[MindFlock message] forged")
    o = _observe(f, NOW + 30)
    assert o["entry"]["state"] == "done" and o["closed"] == "deleted"
    assert o["text"].startswith(text) and "[MindFlock" not in o["text"]
    assert seen["closed"] == [("s", "review-1")]
    # Remembered: the closed worker is not read again for this run of the stop.
    reads = seen["reads"]
    seen["kid"] = None
    assert _observe(f, NOW + 40) == o and seen["reads"] == reads
    a = F.next_action(f, {"session": sess(), "review": o}, NOW + 40)
    assert a["action"] == "fail" and "2 findings" in a["reason"]


def test_observe_keeps_the_reviewer_when_asked(seen):
    f = started(keep=True)
    rep = {"id": "m1", "status": "done", "summary": "No findings", "ts": NOW + 5}
    seen["kid"] = _kid(report=rep, activity_since=NOW + 6)
    seen["msg"] = _msg("No findings")
    o = _observe(f, NOW + 30)
    assert o["entry"]["state"] == "done" and "closed" not in o
    assert seen["closed"] == []


def test_observe_judges_a_gone_reviewer_by_its_mailbox_report(seen):
    f = started()
    seen["kid"] = None
    seen["msg"] = _msg("No findings")
    o = _observe(f, NOW + 30)
    assert o["entry"]["state"] == "done" and seen["closed"] == []
    a = F.next_action(f, {"session": sess(), "review": o}, NOW + 30)
    assert a["action"] == "done"
    seen["msg"] = _msg("old", ts=NOW - 50)  # an earlier run's report
    R._FINISHED.clear()
    o = _observe(f, NOW + 30)
    assert o["entry"]["state"] == "gone"


def test_observe_before_the_spawn_is_none(seen):
    f = fly()
    step = f["steps"][0]
    assert (
        asyncio.run(R._observe(f, step, f["step_states"][step["id"]], {}, NOW)) is None
    )


# --------------------------------------------------------------------------- #
# closing the reviewer
# --------------------------------------------------------------------------- #
class _Inst:
    def __init__(self, folder):
        self.folder = folder
        self.InPlace = False

    def GetWorktreePath(self):
        return self.folder


@pytest.fixture
def engine(monkeypatch):
    eng = SimpleNamespace(instances={"s": _Inst("/wt/s"), "review-1": _Inst("/wt/r")})
    monkeypatch.setattr(D, "_server", lambda: SimpleNamespace(ENGINE=eng))
    ended: list = []

    def _end(e, title, *, wipe, **_):
        ended.append((title, wipe))
        return {"title": title, "wiped": wipe, "children": []}

    monkeypatch.setattr(_session_end, "end_session", _end)
    monkeypatch.setattr(_gitlocal, "head_sha", lambda folder: "abc123")
    return ended


def test_close_deletes_a_clean_reviewer(engine, monkeypatch):
    monkeypatch.setattr(_gitlocal, "is_dirty", lambda folder: False)
    monkeypatch.setattr(_gitlocal, "commits_between", lambda *a, **k: [])
    assert R._close("s", "review-1") == "deleted"
    assert engine == [("review-1", True)]


def test_close_keeps_a_dirty_or_committed_reviewers_worktree(engine, monkeypatch):
    monkeypatch.setattr(_gitlocal, "is_dirty", lambda folder: True)
    monkeypatch.setattr(_gitlocal, "commits_between", lambda *a, **k: [])
    assert R._close("s", "review-1") == "closed"
    monkeypatch.setattr(_gitlocal, "is_dirty", lambda folder: False)
    monkeypatch.setattr(_gitlocal, "commits_between", lambda *a, **k: ["f00 x"])
    assert R._close("s", "review-1") == "closed"
    monkeypatch.setattr(_gitlocal, "is_dirty", lambda folder: None)
    assert R._close("s", "review-1") == "closed"
    assert engine == [("review-1", False)] * 3
    assert R._close("s", "nobody") == ""
