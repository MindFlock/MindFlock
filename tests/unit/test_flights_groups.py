"""Flights round 11: groups — a stop with sub-steps (docs/flights.md "Groups").
The normaliser, the pure group operations and their routes, against a tmp
store (never ``~/.mindflock``)."""

import re
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.web import server
from backend.web.core import flights as F

NOW = 1_800_000_000.0
client = TestClient(server.app)


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_FLIGHTS_DIR", str(tmp_path / "flights"))
    monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(tmp_path / "settings.json"))
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
    yield


G = {"id": "g_3fa2c1", "name": "Ticket to merge", "plan_id": "builtin:ticket"}


def fly(*steps, session="s"):
    plan = F.save_plan({"name": "P", "steps": list(steps)}, now=NOW)
    return F.take_off(plan, session=session, now=NOW)


def gid(step):
    return (step.get("group") or {}).get("id")


# --------------------------------------------------------------------------- #
# The normaliser carries a group through unchanged
# --------------------------------------------------------------------------- #
def test_normalize_step_keeps_a_group_and_drops_a_broken_one():
    s = F.normalize_step({"kind": "notify", "group": dict(G)})
    assert s["group"] == G
    assert "group" not in F.normalize_step({"kind": "notify"})
    for bad in ({"id": "", "name": "x"}, {"id": "g_1", "name": " "}, "g_1", None):
        assert "group" not in F.normalize_step({"kind": "notify", "group": bad})
    long = F.normalize_step(
        {"kind": "notify", "group": {"id": "g" * 60, "name": "n" * 200, "plan_id": 7}}
    )["group"]
    assert (len(long["id"]), len(long["name"]), long["plan_id"]) == (40, 80, "7")
    assert F.normalize_step({"kind": "notify", "group": {"id": "g", "name": "N"}})[
        "group"
    ] == {"id": "g", "name": "N", "plan_id": ""}


def test_plans_flights_and_adhoc_carry_the_group():
    plan = F.normalize_plan(
        {"name": "P", "steps": [{"kind": "notify", "group": G}, {"kind": "agent"}]}
    )
    assert plan["steps"][0]["group"] == G and "group" not in plan["steps"][1]
    saved = F.save_plan(plan, now=NOW)
    assert F.get_plan(saved["id"])["steps"][0]["group"] == G
    f = F.take_off(saved, session="s", now=NOW)
    assert F.get(f["id"])["steps"][0]["group"] == G
    ad = F.adhoc_plan([{"kind": "notify", "group": G}])
    assert ad["steps"][0]["group"] == G


# --------------------------------------------------------------------------- #
# Adding a saved stop / several steps
# --------------------------------------------------------------------------- #
def test_op_add_plan_adds_one_fresh_group():
    f = fly({"kind": "agent"}, {"kind": "notify"})
    plan = F.save_plan(
        {"name": "Ship it", "steps": [{"kind": "ship"}, {"kind": "wait"}]}, now=NOW
    )
    F.op_add_plan(f, plan, now=NOW)
    new = f["steps"][2:]
    assert [s["id"] for s in new] == ["s3", "s4"]
    assert re.match(r"^g_[0-9a-f]{6}$", gid(new[0]))
    assert new[0]["group"] == new[1]["group"]
    assert new[0]["group"]["name"] == "Ship it"
    assert new[0]["group"]["plan_id"] == plan["id"]
    assert all(f["step_states"][s["id"]]["state"] == "pending" for s in new)
    # Twice: a second fresh group, never the first one's id.
    F.op_add_plan(f, plan, at=2, now=NOW)
    assert gid(f["steps"][2]) != gid(f["steps"][4])
    assert [h["event"] for h in f["history"][-2:]] == ["added", "added"]


def test_op_add_steps_grouped_or_as_given():
    f = fly({"kind": "agent"}, {"kind": "notify"})
    F.op_add_steps(
        f, [{"kind": "notify"}, {"kind": "wait"}], group={"name": "Pair"}, now=NOW
    )
    assert gid(f["steps"][2]) == gid(f["steps"][3]) and f["steps"][2]["group"][
        "name"
    ] == ("Pair")
    F.op_add_steps(f, [{"kind": "notify", "group": G}], now=NOW)
    assert f["steps"][-1]["group"] == G
    with pytest.raises(F.FlightError) as err:
        F.op_add_steps(f, [{"kind": "notify"}], group={"name": ""}, now=NOW)
    assert (err.value.status, err.value.error) == (400, "bad_group")
    with pytest.raises(F.PlanError) as perr:
        F.op_add_steps(f, [{"kind": "notify"}, {"kind": "moon"}], now=NOW)
    assert perr.value.field == "steps[1].kind"
    with pytest.raises(F.FlightError) as err:
        F.op_add_steps(f, [{"kind": "notify"}] * F.MAX_STEPS, now=NOW)
    assert err.value.error == "too_many_steps"


# --------------------------------------------------------------------------- #
# Rename, take out, group, ungroup
# --------------------------------------------------------------------------- #
def _grouped():
    """s1 current (agent), s2..s4 one group ahead, s5 alone."""
    f = fly({"kind": "agent"}, {"kind": "notify"})
    F.op_remove_step(f, "s2", now=NOW)
    F.op_add_steps(
        f,
        [{"kind": "notify"}, {"kind": "wait"}, {"kind": "notify"}],
        group={"name": "Three"},
        now=NOW,
    )
    F.op_add_step(f, {"kind": "notify"}, now=NOW)
    assert [s["id"] for s in f["steps"]] == ["s1", "s3", "s4", "s5", "s6"]
    return f


def test_patch_group_renames_takes_out_and_makes_one():
    f = _grouped()
    g = gid(f["steps"][1])
    F.op_edit_step(f, "s4", {"group": {"name": "Renamed"}}, now=NOW)
    assert {s["group"]["name"] for s in f["steps"][1:4]} == {"Renamed"}
    assert {gid(s) for s in f["steps"][1:4]} == {g}
    # Rename is only a label: it works from any step of the group, even
    # once the flight is on it.
    F.op_edit_step(f, "s5", {"group": None}, now=NOW)
    assert "group" not in f["steps"][3] and gid(f["steps"][2]) == g
    F.op_edit_step(f, "s6", {"group": {"name": "Solo"}}, now=NOW)
    assert f["steps"][4]["group"]["name"] == "Solo" and gid(f["steps"][4]) != g
    # Taking the current step out of a group is refused; so is a bad name.
    f["steps"][0]["group"] = {"id": "g_x", "name": "X", "plan_id": ""}
    with pytest.raises(F.FlightError) as err:
        F.op_edit_step(f, "s1", {"group": None}, now=NOW)
    assert err.value.error == "in_progress"
    F.op_edit_step(f, "s1", {"group": {"name": "Y"}}, now=NOW)
    assert f["steps"][0]["group"]["name"] == "Y"
    with pytest.raises(F.FlightError) as err:
        F.op_edit_step(f, "s3", {"group": {"name": ""}}, now=NOW)
    assert err.value.error == "bad_group"
    with pytest.raises(F.PlanError) as perr:
        F.op_edit_step(f, "s3", {"group": "x"}, now=NOW)
    assert perr.value.field == "step.group"
    # group + title in one patch.
    F.op_edit_step(f, "s3", {"group": {"name": "Z"}, "title": "Ping"}, now=NOW)
    assert (f["steps"][1]["title"], f["steps"][1]["group"]["name"]) == ("Ping", "Z")


def test_op_group_and_ungroup():
    f = _grouped()
    with pytest.raises(F.FlightError) as err:
        F.op_group(f, ["s3", "s6"], "Gap", now=NOW)
    assert (err.value.status, err.value.error) == (400, "bad_group")
    with pytest.raises(F.FlightError) as err:
        F.op_group(f, ["s1", "s3"], "Current", now=NOW)
    assert (err.value.status, err.value.error) == (409, "in_progress")
    with pytest.raises(F.FlightError) as err:
        F.op_group(f, [], "None", now=NOW)
    assert err.value.error == "bad_group"
    with pytest.raises(F.FlightError) as err:
        F.op_group(f, ["s9"], "Who", now=NOW)
    assert err.value.error == "unknown_step"
    g = F.op_group(f, ["s6", "s5"], "Tail", now=NOW)
    assert g["name"] == "Tail" and g["plan_id"] == "" and g["id"].startswith("g_")
    assert [gid(s) for s in f["steps"][3:]] == [g["id"], g["id"]]
    assert f["history"][-1]["event"] == "edited"
    F.op_ungroup(f, g["id"], now=NOW)
    assert not any(s.get("group") for s in f["steps"][3:])
    with pytest.raises(F.FlightError) as err:
        F.op_ungroup(f, g["id"], now=NOW)
    assert (err.value.status, err.value.error) == (404, "unknown_group")


def test_move_leaves_groups_alone():
    f = _grouped()
    F.op_move_step(f, "s6", 2, now=NOW)
    assert [s["id"] for s in f["steps"]] == ["s1", "s3", "s6", "s4", "s5"]
    assert "group" not in f["steps"][2]
    assert gid(f["steps"][1]) == gid(f["steps"][3])


def test_remove_a_whole_group_that_is_flying():
    f = _grouped()
    # Fly into the group: s1 done, cursor on s4 (its 2nd sub-step).
    f["step_states"]["s1"]["state"] = "done"
    f["step_states"]["s3"]["state"] = "done"
    f["cursor"] = 2
    f["step_states"]["s4"]["state"] = "running"
    flying = f["steps"][2]["title"]
    F.op_remove_group(f, "s5", now=NOW)
    assert [s["id"] for s in f["steps"]] == ["s1", "s6"]
    assert (f["state"], f["reason"], f["cursor"]) == ("paused", "stop removed", 1)
    assert set(f["step_states"]) == {"s1", "s6"}
    notes = [h["note"] for h in f["history"] if h["event"] == "removed"]
    # Last first: s5 (ahead), s4 (flying), s3 (flown).
    assert notes[-3:] == ["Notify me", "removed %s while it flew" % flying, "Notify me"]
    # A step with no group: just that step.
    F.op_remove_group(f, "s6", now=NOW)
    assert [s["id"] for s in f["steps"]] == ["s1"]
    # The last one out cancels the flight.
    F.op_remove_group(f, "s1", now=NOW)
    assert (f["state"], f["reason"]) == ("cancelled", F.LAST_STOP_REMOVED)


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
def _flight():
    r = client.post(
        "/api/flights/start",
        json={"title": "sess", "steps": [{"kind": "agent"}, {"kind": "you"}]},
    )
    assert r.status_code == 202
    return r.json()["flight"]


def test_step_routes_with_groups():
    f = _flight()
    base = "/api/flights/%s" % f["id"]
    plan = client.post(
        "/api/flights/plans",
        json={"name": "Two", "steps": [{"kind": "notify"}, {"kind": "wait"}]},
    ).json()["plan"]
    r = client.post(base + "/steps", json={"plan_id": plan["id"]})
    assert r.status_code == 200
    steps = r.json()["flight"]["steps"]
    assert [s["group"]["plan_id"] for s in steps[2:]] == [plan["id"]] * 2
    assert steps[2]["group"]["name"] == "Two"
    r = client.post(base + "/steps", json={"plan_id": "p_nope"})
    assert (r.status_code, r.json()["error"]) == (404, "unknown_plan")
    r = client.post(
        base + "/steps",
        json={
            "steps": [{"kind": "notify"}, {"kind": "notify"}],
            "group": {"name": "N"},
        },
    )
    steps = r.json()["flight"]["steps"]
    assert steps[-1]["group"]["name"] == "N" and gid(steps[-1]) == gid(steps[-2])
    r = client.post(base + "/steps", json={"steps": [{"kind": "moon"}]})
    assert (r.status_code, r.json()["error"], r.json()["field"]) == (
        400,
        "bad_step",
        "steps[0].kind",
    )
    r = client.post(base + "/steps", json={"steps": [{"kind": "notify"}], "group": 1})
    assert (r.status_code, r.json()["error"]) == (400, "bad_group")
    # Rename through PATCH; group two lone steps; ungroup; remove a group.
    sid = steps[-1]["id"]
    r = client.patch(base + "/steps/" + sid, json={"group": {"name": "M"}})
    assert r.json()["flight"]["steps"][-1]["group"]["name"] == "M"
    r = client.post(base + "/steps", json={"step": {"kind": "notify"}})
    r = client.post(base + "/steps", json={"step": {"kind": "notify"}})
    last = [s["id"] for s in r.json()["flight"]["steps"][-2:]]
    r = client.post(base + "/group", json={"steps": last, "name": "Pair"})
    assert r.status_code == 200
    g = r.json()["group"]
    assert [gid(s) for s in r.json()["flight"]["steps"][-2:]] == [g["id"]] * 2
    r = client.post(
        base + "/group", json={"steps": [steps[2]["id"], last[0]], "name": "x"}
    )
    assert (r.status_code, r.json()["error"]) == (400, "bad_group")
    r = client.post(base + "/group", json={"steps": ["s1"], "name": "x"})
    assert (r.status_code, r.json()["error"]) == (409, "in_progress")
    r = client.post(base + "/ungroup", json={"group": g["id"]})
    assert r.status_code == 200
    assert not any(gid(s) == g["id"] for s in r.json()["flight"]["steps"])
    r = client.post(base + "/ungroup", json={"group": "g_nope"})
    assert (r.status_code, r.json()["error"]) == (404, "unknown_group")
    n = len(client.get(base).json()["flight"]["steps"])
    r = client.delete(base + "/steps/" + steps[2]["id"] + "?group=1")
    assert r.status_code == 200
    left = r.json()["flight"]["steps"]
    assert len(left) == n - 2 and not any(
        s.get("group", {}).get("plan_id") for s in left
    )
    assert client.post("/api/flights/f_nope/group", json={}).status_code == 404
