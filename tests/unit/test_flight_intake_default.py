"""An intake source's default stops, edited in the Sky (2026-11-02,
docs/flights.md "A source's default stops, edited in the Sky"): our built-in
stops are every source's default; ``GET/PUT /api/flights/intake-default``
read and change ONE source's (its plan field in settings), and a grounded
chain carrying ``edits_default`` edits it — and never flies."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.config import settings as S
from backend.web import server
from backend.web.core import flights as F

client = TestClient(server.app)


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_FLIGHTS_DIR", str(tmp_path / "flights"))
    monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setenv("MINDFLOCK_TEST_PLANS_FILE", str(tmp_path / "test_plans.json"))
    monkeypatch.setenv("MINDFLOCK_AUTOPILOT_FILE", str(tmp_path / "autopilot.json"))
    monkeypatch.setenv("MINDFLOCK_AUTH", "0")
    monkeypatch.delenv("CS_WEB_MODE", raising=False)
    monkeypatch.setattr(
        server.ENGINE,
        "instances",
        {
            "sess": SimpleNamespace(
                Path="/repo",
                Branch="b",
                GetWorktreePath=lambda: "/repo",
                GetGitWorktree=lambda: SimpleNamespace(GetRepoPath=lambda: "/repo"),
            )
        },
    )
    S.invalidate()
    S.set_ticketing_sources(
        [
            {"id": "sc", "provider": "shortcut", "depth": "pr"},
            {"id": "jr", "provider": "jira", "label": "Platform", "depth": "push"},
        ]
    )
    S.update_settings(
        github={
            "repos": ["o/app"],
            "issue_repos": ["o/app", "o/lib"],
            "repo_settings": {"o/app": {"depth": "commit"}},
            "issue_repo_settings": {"o/lib": {"depth": "pr", "agent": "codex"}},
        }
    )
    yield
    S.invalidate()


STEPS = [
    {"kind": "agent", "title": "Do it", "config": {"prompt": "Fix {title}"}},
    {"kind": "you", "config": {"text": "Approve"}},
]


def _get(kind, target):
    return client.get(
        "/api/flights/intake-default", params={"kind": kind, "target": target}
    )


def _put(body):
    return client.put("/api/flights/intake-default", json=body)


def _source(sid):
    S.invalidate()
    return next(s for s in S.load_settings().ticketing.sources if s.id == sid)


def _gh():
    S.invalidate()
    return S.load_settings().github


# --- GET -------------------------------------------------------------------- #
def test_get_is_the_builtin_cut_at_the_sources_depth():
    r = _get("tickets", "sc")
    assert r.status_code == 200
    d = r.json()
    assert d["kind"] == "tickets" and d["target"] == "sc"
    assert d["label"] == "Tickets · Shortcut"
    assert d["plan_id"] == "builtin:ticket@pr" and d["own"] is False
    assert d["steps"] == F.get_plan("builtin:ticket@pr")["steps"]
    assert d["name"] == F.get_plan("builtin:ticket@pr")["name"]
    # A source's label joins the provider's name.
    assert _get("tickets", "jr").json()["label"] == "Tickets · Jira · Platform"
    assert _get("tickets", "jr").json()["plan_id"] == "builtin:ticket@push"
    pr = _get("prs", "o/app").json()
    assert pr["label"] == "PRs · o/app" and pr["plan_id"] == "builtin:pr@commit"
    iss = _get("issues", "O/LIB").json()  # a repo matches case-insensitively
    assert iss["target"] == "o/lib" and iss["label"] == "Issues · o/lib"
    assert iss["plan_id"] == "builtin:ticket@pr"
    # No depth and no plan of its own: no flight — just the agent, which is
    # what the editor starts from (an empty chain has nothing to add after).
    none = _get("issues", "o/app").json()
    assert (none["plan_id"], none["own"], none["name"]) == ("", False, "Just the agent")
    assert [s["kind"] for s in none["steps"]] == ["agent"]


def test_get_is_the_sources_own_plan_when_it_exists():
    plan = F.save_plan({"name": "Ours", "steps": STEPS})
    rows = [s.to_dict() for s in S.load_settings_strict().ticketing.sources]
    rows[0]["plan"] = plan["id"]
    S.set_ticketing_sources(rows)
    d = _get("tickets", "sc").json()
    assert d["own"] is True and d["plan_id"] == plan["id"] and d["name"] == "Ours"
    assert [s["kind"] for s in d["steps"]] == ["agent", "you"]
    # A plan since deleted: back to the built-in at the depth.
    F.delete_plan(plan["id"])
    d = _get("tickets", "sc").json()
    assert d["own"] is False and d["plan_id"] == "builtin:ticket@pr"


def test_get_refuses_an_unknown_source_and_a_bad_kind():
    assert _get("tickets", "nope").status_code == 404
    assert _get("tickets", "nope").json()["error"] == "unknown_source"
    assert _get("prs", "o/elsewhere").status_code == 404
    assert _get("boards", "sc").status_code == 400


# --- PUT steps ------------------------------------------------------------- #
def test_put_steps_makes_a_new_own_plan_and_points_the_source_at_it():
    r = _put({"kind": "tickets", "target": "sc", "steps": STEPS})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["previous_plan_id"] == ""
    assert d["own"] is True and d["name"] == "Tickets · Shortcut stops"
    assert _source("sc").plan == d["plan_id"]
    plan = F.get_plan(d["plan_id"])
    assert plan["trigger"]["kind"] == "ticket" and not plan["builtin"]
    assert [s["kind"] for s in plan["steps"]] == ["agent", "you"]
    assert plan["steps"][0]["title"] == "Do it"
    # The source's other fields survive the write.
    assert _source("sc").depth == "pr" and _source("jr").plan == ""


def test_put_steps_replaces_an_unshared_own_plan_in_place():
    first = _put({"kind": "prs", "target": "o/app", "steps": STEPS, "name": "PR stops"})
    pid = first.json()["plan_id"]
    assert _gh().repo_settings["o/app"] == {"depth": "commit", "plan": pid}
    again = _put({"kind": "prs", "target": "o/app", "steps": STEPS[:1]})
    d = again.json()
    assert d["plan_id"] == pid and d["previous_plan_id"] == pid
    assert d["name"] == "PR stops"  # its name kept
    assert [s["kind"] for s in F.get_plan(pid)["steps"]] == ["agent"]
    renamed = _put({"kind": "prs", "target": "o/app", "steps": STEPS, "name": "New"})
    assert renamed.json()["plan_id"] == pid and F.get_plan(pid)["name"] == "New"


def test_put_steps_makes_a_new_plan_when_another_source_shares_it():
    shared = F.save_plan({"name": "Shared", "steps": STEPS})
    rows = [s.to_dict() for s in S.load_settings_strict().ticketing.sources]
    for row in rows:
        row["plan"] = shared["id"]
    S.set_ticketing_sources(rows)
    d = _put({"kind": "tickets", "target": "jr", "steps": STEPS[:1]}).json()
    assert d["plan_id"] != shared["id"] and d["previous_plan_id"] == shared["id"]
    assert _source("jr").plan == d["plan_id"]
    assert _source("sc").plan == shared["id"]
    assert len(F.get_plan(shared["id"])["steps"]) == 2  # untouched


def test_put_steps_never_rewrites_a_builtin():
    _put({"kind": "issues", "target": "o/lib", "plan_id": "builtin:ticket"})
    d = _put({"kind": "issues", "target": "o/lib", "steps": STEPS}).json()
    assert d["plan_id"].startswith("p_") and d["previous_plan_id"] == "builtin:ticket"
    assert F.get_plan("builtin:ticket")["steps"] != F.get_plan(d["plan_id"])["steps"]
    # The repo's other overrides survive.
    assert _gh().issue_repo_settings["o/lib"]["agent"] == "codex"


def test_put_steps_validates_strictly():
    r = _put({"kind": "tickets", "target": "sc", "steps": [{"kind": "nope"}]})
    assert r.status_code == 400 and r.json()["field"] == "steps[0].kind"
    r = _put(
        {
            "kind": "tickets",
            "target": "sc",
            "steps": [{"kind": "ship", "config": {"lane": "nowhere"}}],
        }
    )
    assert r.status_code == 400 and r.json()["field"].startswith("steps[0].config")
    assert _put({"kind": "tickets", "target": "sc", "steps": []}).status_code == 400
    assert _source("sc").plan == ""
    assert _put({"kind": "tickets", "target": "zz", "steps": STEPS}).status_code == 404


# --- PUT plan_id ----------------------------------------------------------- #
def test_put_plan_id_sets_and_clears_the_field_with_an_undo():
    plan = F.save_plan({"name": "Mine", "steps": STEPS})
    d = _put({"kind": "issues", "target": "o/lib", "plan_id": plan["id"]}).json()
    assert d["plan_id"] == plan["id"] and d["own"] and d["previous_plan_id"] == ""
    assert _gh().issue_repo_settings["o/lib"]["plan"] == plan["id"]
    back = _put({"kind": "issues", "target": "o/lib", "plan_id": ""}).json()
    assert back["previous_plan_id"] == plan["id"]
    assert back["own"] is False and back["plan_id"] == "builtin:ticket@pr"
    assert "plan" not in _gh().issue_repo_settings["o/lib"]
    # Clearing a repo whose block held only the plan drops the block.
    _put({"kind": "prs", "target": "o/app", "plan_id": plan["id"]})
    _put({"kind": "issues", "target": "o/app", "plan_id": plan["id"]})
    _put({"kind": "issues", "target": "o/app", "plan_id": ""})
    assert "o/app" not in _gh().issue_repo_settings
    assert _gh().repo_settings["o/app"]["plan"] == plan["id"]


def test_put_plan_id_refuses_an_unknown_plan():
    r = _put({"kind": "tickets", "target": "sc", "plan_id": "p_gone"})
    assert r.status_code == 404 and r.json()["error"] == "unknown_plan"
    assert _put({"kind": "x", "target": "sc", "plan_id": ""}).status_code == 400
    assert _put({"kind": "tickets", "target": "sc"}).status_code == 400


# --- from_flight + for-flight ---------------------------------------------- #
def _arm(title, subject):
    return F.arm_pending(title, "builtin:ticket@pr", subject, by="intake")


@pytest.mark.parametrize(
    "subject, kind, target",
    [
        ({"kind": "ticket", "source": "sc", "id": "12"}, "tickets", "sc"),
        ({"kind": "pr", "source": "o/app", "id": "7"}, "prs", "o/app"),
        ({"kind": "issue", "source": "o/lib", "id": "3"}, "issues", "o/lib"),
    ],
)
def test_from_flight_maps_the_subject_to_its_source(subject, kind, target):
    f = _arm("t-" + kind, subject)
    got = client.get("/api/flights/intake-default/for-flight/" + f["id"])
    assert got.status_code == 200
    assert (got.json()["kind"], got.json()["target"]) == (kind, target)
    d = _put({"from_flight": f["id"]}).json()
    assert d["kind"] == kind and d["target"] == target and d["own"] is True
    assert d["previous_plan_id"] == ""
    plan = F.get_plan(d["plan_id"])
    assert [s["kind"] for s in plan["steps"]] == [s["kind"] for s in f["steps"]]
    assert all(
        set(s)
        <= {
            "id",
            "kind",
            "title",
            "config",
            "on_fail",
            "halt",
            "group",
            "when",
            "pos",
            "parallel",
        }
        for s in plan["steps"]
    )
    assert _get(kind, target).json()["plan_id"] == d["plan_id"]


def test_for_flight_404s_without_an_intake_source():
    hand = F.ground(raw_steps=STEPS)
    r = client.get("/api/flights/intake-default/for-flight/" + hand["id"])
    assert r.status_code == 404 and r.json()["error"] == "no_source"
    assert _put({"from_flight": hand["id"]}).status_code == 404
    gone = _arm("t-gone", {"kind": "ticket", "source": "removed", "id": "1"})
    assert (
        client.get("/api/flights/intake-default/for-flight/" + gone["id"]).status_code
        == 404
    )
    assert (
        client.get("/api/flights/intake-default/for-flight/f_nope").status_code == 404
    )


# --- The editor chain ------------------------------------------------------ #
def test_normalizer_keeps_edits_default_and_drops_a_malformed_one():
    base = {"id": "f1", "steps": STEPS, "state": "grounded"}
    f = F._normalize_flight(
        "f1", dict(base, edits_default={"kind": "PRS", "target": "o/app"})
    )
    assert f["edits_default"] == {"kind": "prs", "target": "o/app"}
    for bad in (
        {"kind": "boards", "target": "x"},
        {"kind": "prs", "target": ""},
        "prs",
        None,
    ):
        assert "edits_default" not in F._normalize_flight(
            "f1", dict(base, edits_default=bad)
        )


def test_grounded_start_stores_edits_default_and_attach_is_refused():
    r = client.post(
        "/api/flights/start",
        json={
            "grounded": True,
            "plan_id": "builtin:ticket@pr",
            "edits_default": {"kind": "tickets", "target": "sc"},
        },
    )
    assert r.status_code == 202, r.text
    flight = r.json()["flight"]
    assert flight["edits_default"] == {"kind": "tickets", "target": "sc"}
    assert F.get(flight["id"])["edits_default"] == {"kind": "tickets", "target": "sc"}
    a = client.post("/api/flights/%s/attach" % flight["id"], json={"title": "sess"})
    assert a.status_code == 409 and a.json()["error"] == "edits_default"
    # The free-run "@default" path refuses too, before anything starts.
    a = client.post("/api/flights/%s/attach" % flight["id"], json={"title": "@default"})
    assert a.status_code == 409
    with pytest.raises(F.FlightError) as err:
        F.attach(flight["id"], "sess")
    assert err.value.error == "edits_default" and err.value.status == 409
    assert F.get(flight["id"])["state"] == "grounded"
    # Its grounded editing works as for any chain.
    added = client.post(
        "/api/flights/%s/steps" % flight["id"],
        json={"step": {"kind": "you", "config": {"text": "Look"}}},
    )
    assert added.status_code == 200, added.text
    # Its source is the one it edits; Save as default writes its steps.
    got = client.get("/api/flights/intake-default/for-flight/" + flight["id"])
    assert got.json()["target"] == "sc"
    d = _put({"from_flight": flight["id"]}).json()
    assert d["own"] is True and d["previous_plan_id"] == ""
    steps = F.get_plan(d["plan_id"])["steps"]
    assert steps[-1]["kind"] == "you"
    # The group the editor put round the default it was seeded from is not
    # saved (the next grounding adds one back); another stop's group is.
    assert all("group" not in s for s in steps)


def test_plan_steps_keeps_a_group_it_was_not_seeded_from():
    from backend.web.core import flight_intake_default as D

    g = {"id": "g_1", "name": "Review", "plan_id": "p_other"}
    steps = D.plan_steps([STEPS[0], dict(STEPS[1], group=g)], "builtin:ticket@pr")
    assert steps[1]["group"] == g
    whole = D.plan_steps([dict(s, group=g) for s in STEPS])
    assert all("group" not in s for s in whole)


def test_grounded_start_refuses_a_malformed_edits_default():
    r = client.post(
        "/api/flights/start",
        json={"grounded": True, "steps": STEPS, "edits_default": {"kind": "x"}},
    )
    assert r.status_code == 400 and r.json()["error"] == "bad_edits_default"
