"""A route never reaches its verdict with reviews not done (docs/flights.md
"Reviews: no review left undone"):

* Bug A — comments already open when the stop begins are its first round,
  unless an earlier stop of the flight handed them to the agent (intake's
  seeded first prompt on a PR: ``detail.pr_comments_at``);
* a PR whose comments can't be read is never "quiet" (the stored details
  with no ``seen`` / ``seeded`` at all);
* Bug C — a review stop's reviewer that leaves an empty report is not a pass;
* Bug D — the session's agent is told what the route did ("Route so far"
  on an Agent stop's prompt and a Reviews stop's first round): a review that
  passed ran in another session.

(Bug B, the required reviewer on the current head, is in
test_flight_review_modes.py.) GitHub is always faked."""

import asyncio
import datetime as dt
import time
from types import SimpleNamespace

import pytest

from backend.ticket_ingestion import pr_comments as _prc
from backend.ticket_ingestion.models import PRComment, PullRequest
from backend.web.core import flight_driver as D
from backend.web.core import flights as F
from backend.web.core.flight_kinds import review as R

NOW = 1_800_000_000.0
#: The three CodeRabbit comments a real stop marked seen and never sent.
OPEN = ["4540963470", "4540963528", "4540963542"]


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_FLIGHTS_DIR", str(tmp_path / "flights"))
    monkeypatch.setenv("MINDFLOCK_AUTOPILOT_FILE", str(tmp_path / "autopilot.json"))
    monkeypatch.setattr(D, "_REVIEWS", {})
    monkeypatch.setattr(D, "_JOBS", {})
    monkeypatch.setattr(D, "_CHECKS", {})
    R._FINISHED.clear()
    yield
    R._FINISHED.clear()


def sess(**kw):
    return dict({"present": True, "status": "running", "activity": "idle"}, **kw)


def plan(*steps):
    return F.save_plan({"name": "P", "steps": list(steps)}, now=NOW)


AGENT = {"kind": "agent", "config": {"prompt": "address the review"}}
REVIEWS = {"kind": "reviews", "config": {"quiet_minutes": 5}}


def go(f, obs, now):
    a = F.next_action(f, obs, now)
    F.apply(f, a, now)
    return a


def to_reviews(f, now=NOW):
    """Finish every stop before the Reviews stop and start it watching."""
    while f["steps"][f["cursor"]]["kind"] != "reviews":
        F.apply(f, {"action": "done", "note": ""}, now)
    F.apply(f, {"action": "run", "detail": {"phase": "watch"}}, now)
    return f


def reviews_detail(f):
    sid = next(s["id"] for s in f["steps"] if s["kind"] == "reviews")
    return f["step_states"][sid]["detail"]


PR = {"session": sess(pr_url="https://pr/1")}


# --------------------------------------------------------------------------- #
# Bug A: comments already open are the first round
# --------------------------------------------------------------------------- #
def test_comments_open_when_the_stop_begins_go_to_the_agent():
    """The real route: no earlier stop handed the PR's comments to the agent,
    so the stop used to seed them as seen and end "no rounds, quiet"."""
    f = to_reviews(F.take_off(plan(AGENT, REVIEWS), session="s", now=NOW))
    obs = dict(PR, reviews={"ids": list(OPEN)})
    a = go(f, obs, NOW + 1)
    assert a["action"] == "wait"
    assert reviews_detail(f)["seen"] == [] and reviews_detail(f)["seeded"] is True
    a = F.next_action(f, obs, NOW + 2)
    assert a["action"] == "run" and a["phase"] == "send" and a["comments"] == OPEN
    # Never "quiet" while they are open and unsent.
    assert F.next_action(f, obs, NOW + 3600)["action"] == "run"


def test_a_pr_intake_prompt_counts_as_handing_its_comments_over():
    """Intake built the session's first prompt from the comments open then:
    those are not sent twice; anything newer (or of unknown age) is."""
    f = F.take_off(
        plan(AGENT, REVIEWS),
        session="pr-o-r-1",
        subject={"kind": "pr", "source": "o/r", "id": "1"},
        seeded=True,
        intake=True,
        now=NOW,
    )
    lead = f["step_states"][f["steps"][0]["id"]]
    assert lead["state"] == "running"
    assert lead["detail"] == {"seeded": True, "pr_comments_at": NOW}
    to_reviews(f, NOW + 900)
    created = {"1": NOW - 3600, "2": NOW, "3": NOW + 60, "4": 0.0}
    obs = dict(PR, reviews={"ids": ["1", "2", "3", "4"], "created": created})
    go(f, obs, NOW + 901)
    assert reviews_detail(f)["seen"] == ["1", "2"]
    a = F.next_action(f, obs, NOW + 902)
    assert a["action"] == "run" and a["comments"] == ["3", "4"]


def test_a_ticket_intake_or_a_typed_first_prompt_hands_nothing_over():
    for title, kw in (
        ("tix-7", {"subject": {"kind": "ticket", "id": "7"}, "intake": True}),
        ("typed", {"subject": {"kind": "pr", "id": "1"}}),  # seeded, not intake
    ):
        f = F.take_off(plan(AGENT, REVIEWS), session=title, seeded=True, now=NOW, **kw)
        assert "pr_comments_at" not in f["step_states"][f["steps"][0]["id"]]["detail"]
        to_reviews(f)
        obs = dict(PR, reviews={"ids": ["1"], "created": {"1": NOW - 60}})
        go(f, obs, NOW + 1)
        assert reviews_detail(f)["seen"] == []


def test_arm_pending_marks_a_pr_intake_prompt():
    p = plan(AGENT, REVIEWS)
    f = F.arm_pending("pr-o-r-2", p["id"], {"kind": "pr", "source": "o/r", "id": "2"})
    d = f["step_states"][f["steps"][0]["id"]]["detail"]
    assert d["seeded"] is True and d["pr_comments_at"] > 0
    g = F.arm_pending("tix-1", p["id"], {"kind": "ticket", "id": "1"})
    assert "pr_comments_at" not in g["step_states"][g["steps"][0]["id"]]["detail"]


def test_the_driver_sends_the_open_comments_end_to_end(monkeypatch):
    """Through the driver: the first watch fetches the PR's open comments and
    the next pass types all of them to the agent as round 1."""
    sent = []
    monkeypatch.setattr(D, "_deliver", lambda title, text: sent.append(text))
    pr = PullRequest(
        1, "feat", "abc", "main", "T", "https://pr/1", "", dt.datetime.now(), repo="o/r"
    )
    got = [
        PRComment(
            id=int(i),
            kind="review",
            author="coderabbitai[bot]",
            body="fix %s" % i,
            url="u",
            created_at="2027-01-15T08:00:00Z",
        )
        for i in OPEN
    ]

    async def _pr_of(s, flight):
        return pr, "o/r"

    async def _fetch(p, cfg):
        return list(got)

    monkeypatch.setattr(D, "_pr_of", _pr_of)
    monkeypatch.setattr(_prc, "fetch_actionable_comments", _fetch)
    row = {
        "present": True,
        "pending": False,
        "status": "running",
        "sleeping": False,
        "activity": "idle",
        "branch": "feat",
        "folder": "/repo",
        "pr_url": "https://pr/1",
    }
    monkeypatch.setattr(D, "_session_obs", lambda flight, now, agent: dict(row))
    ns = SimpleNamespace(
        _SERVER_BOOT_ID="boot-1",
        ENGINE=SimpleNamespace(instances={}),
        _pending_rows=lambda: [],
        git_available=lambda: True,
        _peer_guard=SimpleNamespace(refusal=lambda inst, what: None),
        _lane_refusal=lambda title, lane: None,
        _publish_autopilot=lambda title: None,
        published=[],
    )
    monkeypatch.setattr(D, "_server", lambda: ns)
    f = F.take_off(plan(REVIEWS), session="s")
    for _ in range(3):
        asyncio.run(D.step_flight(f["id"]))
    assert len(sent) == 1
    assert all("fix %s" % i in sent[0] for i in OPEN)
    d = F.get(f["id"])["step_states"][f["steps"][0]["id"]]["detail"]
    assert d["phase"] == "agent" and d["seen"] == OPEN and d["review_rounds"] == 1


# --------------------------------------------------------------------------- #
# Comments that can't be read are never "quiet"
# --------------------------------------------------------------------------- #
def test_unreadable_comments_wait_then_fail_never_quiet():
    """The stored details with no seen / seeded: the comments were never
    read (no PR found, GitHub down), and the quiet clock ended the stop."""
    f = to_reviews(F.take_off(plan(REVIEWS), session="s", now=NOW))
    blind = dict(PR, reviews={"ids": None, "why": "found no pull request"})
    a = go(f, blind, NOW + 1)
    assert a["action"] == "wait"
    assert a["note"] == "can't read the review comments yet: found no pull request"
    a = go(f, blind, NOW + 299)
    assert a["action"] == "wait" and reviews_detail(f)["unknown_since"] == NOW + 1
    assert "seeded" not in reviews_detail(f)
    a = F.next_action(f, blind, NOW + 301)
    assert a["action"] == "fail"
    assert a["reason"] == (
        "can't read the PR's review comments: found no pull request — fix that "
        "and Retry, or Skip"
    )
    # Readable again: the spell is forgotten, then the stop seeds as usual.
    a = go(f, dict(PR, reviews={"ids": []}), NOW + 120)
    assert "unknown_since" not in reviews_detail(f)
    go(f, dict(PR, reviews={"ids": []}), NOW + 121)
    assert reviews_detail(f)["seeded"] is True
    # No observation at all (not fetched) reads the same way.
    g = to_reviews(F.take_off(plan(REVIEWS), session="t", now=NOW))
    assert F.next_action(g, PR, NOW + 3600)["action"] == "wait"


# --------------------------------------------------------------------------- #
# Bug C: a review stop's reviewer without a (real) report is not a pass
# --------------------------------------------------------------------------- #
def _review_started():
    f = F.take_off(
        plan({"kind": "review", "config": {"mode": "security"}, "on_fail": "agent"}),
        session="s",
        now=NOW,
    )
    F.apply(f, {"action": "run", "detail": {"children": ["review-2"]}}, NOW)
    return f


def test_an_empty_report_is_no_report():
    f = _review_started()
    o = {
        "entry": {"title": "review-2", "state": "done"},
        "text": "",
        "report": True,
    }
    a = F.next_action(f, {"session": sess(), "review": o}, NOW + 10)
    assert a["action"] == "run"
    assert a["note"] == (
        "the reviewer did not finish: review-2 ended without a report — asking "
        "another reviewer"
    )
    # Its last reply, when it holds a verdict, is read in the report's place.
    o = dict(o, reply="No findings.")
    a = F.next_action(f, {"session": sess(), "review": o}, NOW + 10)
    assert a["action"] == "done" and "last reply" in a["note"]


def test_no_report_is_never_handed_to_the_agent_and_halts():
    f = _review_started()
    sid = f["steps"][0]["id"]
    o = {"entry": {"title": "review-2", "state": "done"}, "text": "", "report": False}
    for n in range(R.MAX_REASKS):
        a = F.next_action(f, {"session": sess(), "review": o}, NOW + 10 + n)
        assert a["action"] == "run" and a["detail"]["reasks"] == n + 1
        # The driver's _run spawns the next reviewer, carrying the detail.
        F.apply(f, dict(a, detail=dict(a["detail"], children=["review-2"])), NOW)
    a = go(f, {"session": sess(), "review": o}, NOW + 30)
    assert a["action"] == "fail" and a["then"] == "halt"
    assert a["reason"] == "the reviewer did not finish: review-2 ended without a report"
    assert f["state"] == "halted" and f["step_states"][sid]["rounds"] == 0


def test_observe_reads_the_last_reply_of_an_empty_report(monkeypatch):
    f = _review_started()
    step = f["steps"][0]
    ss = f["step_states"][step["id"]]
    kid = {
        "title": "review-2",
        "present": True,
        "status": "running",
        "activity": "idle",
        "activity_since": NOW - 1000,
        "created": NOW - 1000,
        "settle_s": 8.0,
        "report": None,
    }
    msg = {"id": "m1", "text": "", "data": {"status": "done"}, "ts": NOW + 5}
    monkeypatch.setattr(D, "_children_obs", lambda flight, step: [kid])
    monkeypatch.setattr(R, "_report_message", lambda parent, t, since: msg)
    monkeypatch.setattr(R, "_close", lambda parent, title: "deleted")
    monkeypatch.setattr(R, "_last_reply", lambda title: "1. a.py:3 — injection")
    entry = {"title": "review-2", "state": "done", "started": True}
    monkeypatch.setattr(R, "_entry", lambda *a: dict(entry))
    o = asyncio.run(R._observe(f, step, ss, sess(), time.time()))
    assert o["report"] is True and o["text"] == ""
    assert o["reply"] == "1. a.py:3 — injection"


# --------------------------------------------------------------------------- #
# Bug D: the session's agent is told what the route did (a passed review ran
# in another session)
# --------------------------------------------------------------------------- #
VERDICT = {
    "kind": "agent",
    "config": {"prompt": "Tick 'Conducted security review' only when true."},
}


def _route_done_through(f, upto):
    """Record the stops before index ``upto`` as the real route left them."""
    marks = [
        ("done", "", {"receipt": "no findings"}),
        ("skipped", "not needed here", {}),
        ("failed", "failing checks: lint\nmore", {"moved_on": True}),
    ]
    for k, s in enumerate(f["steps"][:upto]):
        state, note, detail = marks[k % len(marks)]
        f["step_states"][s["id"]].update(state=state, note=note, detail=detail)
    f["cursor"] = upto
    return f


def test_route_so_far_lists_each_finished_stop_from_the_record():
    f = F.take_off(
        plan(
            {"kind": "review", "config": {"mode": "security"}, "title": "Security"},
            AGENT,
            {"kind": "checks", "config": {}},
            {"kind": "wait", "config": {"minutes": 1}},
            VERDICT,
        ),
        session="s",
        now=NOW,
    )
    _route_done_through(f, 3)
    text = F.route_so_far(f, f["steps"][4])
    assert text.splitlines() == [
        "Route so far (the stops before this one, as MindFlock recorded them):",
        "- Security: done, no findings",
        "- %s: skipped, not needed here" % f["steps"][1]["title"],
        "- %s: failed, moved on, failing checks: lint" % f["steps"][2]["title"],
    ]
    # Only stops BEFORE this one, and only finished ones (the wait is pending).
    assert F.route_so_far(f, f["steps"][0]) == ""
    assert "Wait" not in text
    # Appended after a blank line; an empty prompt stays empty.
    assert F.with_route_so_far("Do it.", f, f["steps"][4]) == "Do it.\n\n" + text
    assert F.with_route_so_far("", f, f["steps"][4]) == ""


def test_route_so_far_is_capped_keeping_the_newest_stops():
    steps = [
        {
            "kind": "agent",
            "config": {"prompt": "x"},
            "title": "Stop %d " % i + "y" * 300,
        }
        for i in range(30)
    ]
    f = F.take_off(plan(*steps), session="s", now=NOW)
    for s in f["steps"][:29]:
        f["step_states"][s["id"]].update(state="done", detail={"receipt": "z" * 300})
    text = F.route_so_far(f, f["steps"][29])
    assert len(text) <= F.ROUTE_SO_FAR_CHARS
    lines = text.splitlines()
    assert lines[1].startswith("- … ") and lines[1].endswith("stops before these")
    assert lines[-1].startswith("- Stop 28 ") and lines[-1].endswith("…")
    assert all(len(ln) <= F.ROUTE_LINE_CHARS for ln in lines)


def _driver_env(monkeypatch, sent, pr_url=""):
    monkeypatch.setattr(D, "_deliver", lambda title, text: sent.append(text))
    row = {
        "present": True,
        "pending": False,
        "status": "running",
        "sleeping": False,
        "activity": "idle",
        "branch": "feat",
        "folder": "/repo",
        "pr_url": pr_url,
    }
    monkeypatch.setattr(D, "_session_obs", lambda flight, now, agent: dict(row))
    ns = SimpleNamespace(
        _SERVER_BOOT_ID="boot-1",
        ENGINE=SimpleNamespace(instances={}),
        _pending_rows=lambda: [],
        git_available=lambda: True,
        _peer_guard=SimpleNamespace(refusal=lambda inst, what: None),
        _lane_refusal=lambda title, lane: None,
        _publish_autopilot=lambda title: None,
        published=[],
    )
    monkeypatch.setattr(D, "_server", lambda: ns)


def test_an_agent_stop_after_a_passed_review_is_told_it_passed(monkeypatch):
    """The real case: the security review stop passed with no findings in
    the reviewer's session; the verdict stop's agent left it unticked."""
    sent: list = []
    _driver_env(monkeypatch, sent)
    p = plan(
        {
            "kind": "review",
            "config": {"mode": "security"},
            "title": "Claude security review",
        },
        VERDICT,
    )
    f = F.take_off(p, session="s")
    sid = f["steps"][0]["id"]

    def _passed(x):
        # The review stop ran (its reviewer reported "No findings").
        x["step_states"][sid]["state"] = "running"
        done = {"findings": "", "receipt": "no findings"}
        return F.apply(
            x, {"action": "done", "note": "no findings", "detail": done}, time.time()
        )

    F.mutate(f["id"], _passed)
    asyncio.run(D.step_flight(f["id"]))
    assert sent == [
        "Tick 'Conducted security review' only when true.\n\n"
        "Route so far (the stops before this one, as MindFlock recorded them):\n"
        "- Claude security review: done, no findings"
    ]


def test_an_agent_stop_with_nothing_before_it_is_unchanged(monkeypatch):
    sent: list = []
    _driver_env(monkeypatch, sent)
    f = F.take_off(plan(VERDICT), session="s")
    asyncio.run(D.step_flight(f["id"]))
    assert sent == ["Tick 'Conducted security review' only when true."]


def test_a_reviews_stops_first_round_carries_the_route_so_far(monkeypatch):
    sent: list = []
    _driver_env(monkeypatch, sent, pr_url="https://pr/1")
    pr = PullRequest(
        1, "feat", "abc", "main", "T", "https://pr/1", "", dt.datetime.now(), repo="o/r"
    )
    c = PRComment(id=9, kind="review", author="coderabbitai[bot]", body="x", url="u")
    p = plan({"kind": "ship", "config": {"lane": "push"}, "title": "Push"}, REVIEWS)
    f = F.take_off(p, session="s")
    first = f["steps"][0]["id"]

    def _prep(x):
        x["step_states"][first].update(
            state="done", detail={"receipt": "pushed"}, note=""
        )
        x["cursor"] = 1
        F.apply(
            x,
            {"action": "run", "detail": {"phase": "watch", "seeded": True, "seen": []}},
            time.time(),
        )
        return x

    f = F.mutate(f["id"], _prep)
    D._REVIEWS[f["id"]] = {
        "at": time.time(),
        "ids": ["9"],
        "comments": {"9": c},
        "pr": pr,
    }
    asyncio.run(D.step_flight(f["id"]))
    assert len(sent) == 1
    assert sent[0].endswith(
        "\n\nRoute so far (the stops before this one, as MindFlock recorded "
        "them):\n- Push: done, pushed"
    )
    # A later round is only the comments.
    sid = f["steps"][1]["id"]
    F.mutate(
        f["id"],
        lambda x: x["step_states"][sid]["detail"].update(
            phase="watch", review_rounds=1, seen=[]
        )
        or x,
    )
    D._REVIEWS[f["id"]]["at"] = time.time()
    asyncio.run(D.step_flight(f["id"]))
    assert len(sent) == 2 and "Route so far" not in sent[1]
