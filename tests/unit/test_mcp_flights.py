"""The MCP flight tools (backend.mcp.flights) against an in-memory MindFlock API.

Each tool's request shape on /api/flights*, the scope rule (you or a
descendant, like ship_session / set_autopilot; a grounded flight needs only
write scope), the confirm rule for stops that can't be undone (read from the
kinds' ``risk``, never a hard-coded kind list), the compactness of
flight_status, and the kinds list describe_flight brings along.
"""

from __future__ import annotations

import json

import pytest

from backend import client
from backend.mcp import flights as mcp_flights
from backend.mcp.api import Api
from backend.mcp.protocol import ToolError
from backend.mcp.tools import build_tools
from tests.unit._mcp_fakes import FakeCtx, make_box, row

KINDS = {
    "kinds": [
        {
            "kind": "agent",
            "label": "Agent",
            "actor": "robot",
            "summary": "Type a prompt into the session and wait for its turn.",
            "fields": [{"name": "prompt"}, {"name": "fresh"}, {"name": "join"}],
            "default_title": "Agent",
            "risk": [],
        },
        {
            "kind": "ship",
            "label": "Ship",
            "actor": "robot",
            "summary": "Commit, push, open a PR or merge.",
            "fields": [{"name": "lane"}, {"name": "ask_first"}, {"name": "message"}],
            "default_title": "Push",
            "risk": ["remote"],
        },
        {
            "kind": "land",
            "label": "Land",
            "actor": "robot",
            "summary": "Merge the branch into its base.",
            "fields": [{"name": "how"}],
            "default_title": "Land",
            "risk": ["irreversible"],
        },
        {
            "kind": "you",
            "label": "Perch",
            "actor": "you",
            "summary": "Wait for a person to press Done.",
            "fields": [{"name": "text"}, {"name": "items"}],
            "default_title": "Perch",
        },
        {
            "kind": "loop",
            "label": "Loop",
            "actor": "robot",
            "summary": "Send the flight back to an earlier stop N times.",
            "fields": [
                {"name": "back_to"},
                {"name": "times"},
                {"name": "every_minutes"},
                {"name": "wait_limits"},
                {"name": "fresh"},
                {"name": "until"},
            ],
            "default_title": "Loop ×3",
            "risk": [],
        },
    ],
    "on_fail": ["halt", "agent", "skip", "you"],
}


def _tree():
    return [
        row("root"),
        row("orch", parent="root"),
        row("w1", parent="orch", spawned=True),
        row("w1a", parent="w1", spawned=True),
        row("other"),
        row("member", run={"id": "r_1", "name": "Q3", "role": "member"}),
    ]


def _step(sid, kind="agent", title=None, **kw):
    return dict({"id": sid, "kind": kind, "title": title or kind, "config": {}}, **kw)


def _flight(fid="f_1", session="orch", state="running", cursor=1, n=3, **kw):
    steps = [_step("s%d" % (i + 1), title="Stop %d" % (i + 1)) for i in range(n)]
    states = {}
    for i, s in enumerate(steps):
        states[s["id"]] = {
            "state": "done" if i < cursor else ("running" if i == cursor else "pending")
        }
    out = {
        "id": fid,
        "plan_id": "adhoc",
        "plan_name": "Flight on %s" % session,
        "session": session,
        "state": state,
        "cursor": cursor,
        "steps": steps,
        "step_states": states,
        "reason": "",
        "history": [{"at": 9_000.0, "step": "s1", "event": "done", "note": ""}],
        "updated": 9_500.0,
        "by": "user",
    }
    out.update(kw)
    return out


def _box(me="orch", scope="children", flights=(), rows=None):
    box, api, clock = make_box(rows or _tree(), me=me, scope=scope)
    api.top_routes[("GET", "/api/flights/kinds")] = KINDS
    api.top_routes[("GET", "/api/flights")] = {"flights": list(flights)}
    for f in flights:
        api.top_routes[("GET", "/api/flights/" + f["id"])] = {"flight": f}
    return box, api, clock


def _start_echo(api, captured):
    def start(payload, q):
        captured.append(payload)
        if payload.get("grounded"):
            f = _flight(session="", state="grounded", cursor=0)
        else:
            f = _flight(session=payload.get("title"), cursor=0)
        return {"flight": f, "absorbed": []}

    api.top_routes[("POST", "/api/flights/start")] = start


def _ctx(clock):
    return FakeCtx(clock)


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #
class TestRegistry:
    NAMES = (
        "describe_flight",
        "start_flight",
        "flight_status",
        "control_flight",
        "edit_flight",
        "save_flight_plan",
    )

    def test_six_tools_are_served_and_known_to_playbooks(self):
        from backend.mcp import playbooks

        box, _, _ = _box()
        names = [t.name for t in build_tools(box)]
        assert len(names) == 34
        for n in self.NAMES:
            assert n in names and n in playbooks.TOOL_NAMES

    def test_they_are_never_offered_to_a_peer(self):
        from backend.mcp.peer_tools import PEER_TOOL_NAMES

        assert len(PEER_TOOL_NAMES) == 10
        assert not set(self.NAMES) & set(PEER_TOOL_NAMES)

    def test_descriptions_fit_and_schemas_are_closed(self):
        box, _, _ = _box()
        by = {t.name: t for t in build_tools(box)}
        for n in self.NAMES:
            assert len(by[n].description) <= 1500, n
            assert by[n].input_schema["additionalProperties"] is False

    def test_the_kind_list_is_never_hard_coded(self):
        """A kind is whatever GET /api/flights/kinds serves (round 16's loop
        included): the step schema leaves ``kind`` an open string."""
        kind = mcp_flights.S_START["properties"]["steps"]["items"]["properties"]["kind"]
        assert "enum" not in kind


# --------------------------------------------------------------------------- #
# describe_flight
# --------------------------------------------------------------------------- #
class TestDescribe:
    def test_request_shape_steps_lines_and_kinds(self):
        box, api, clock = _box()
        api.top_routes[("POST", "/api/flights/describe")] = {
            "steps": [
                _step("s1", "agent", "Fix it", config={"prompt": "fix the bug"}),
                _step("s2", "ship", "Open PR", config={"lane": "pr"}, on_fail="agent"),
                _step(
                    "s3", "loop", "Loop ×3", config={"times": 3}, when="diff lines > 0"
                ),
            ],
            "model": "claude-haiku",
        }
        out = box.describe_flight({"text": "fix, PR, loop"}, _ctx(clock))
        assert (
            "POST",
            "/api/flights/describe",
            {"text": "fix, PR, loop", "want": "plan"},
        ) in api.calls
        assert [s["kind"] for s in out["steps"]] == ["agent", "ship", "loop"]
        assert out["model"] == "claude-haiku"
        assert out["lines"][0] == 's1 agent "Fix it" · prompt=fix the bug'
        assert "on_fail agent" in out["lines"][1]
        assert "when diff lines > 0" in out["lines"][2]
        # The kinds list: kind · label · summary · config field names.
        loop = next(k for k in out["kinds"] if k.startswith("loop "))
        assert loop.startswith("loop · Loop · Send the flight back")
        assert (
            "fields: back_to, times, every_minutes, wait_limits, fresh, until" in loop
        )
        assert any("[irreversible]" in k for k in out["kinds"])
        assert any(
            k.startswith("you ") and "waits on a person" in k for k in out["kinds"]
        )
        assert out["on_fail"] == ["halt", "agent", "skip", "you"]

    def test_want_step_and_kinds_off(self):
        box, api, clock = _box()
        api.top_routes[("POST", "/api/flights/describe")] = {"steps": [_step("s1")]}
        out = box.describe_flight(
            {"text": "x", "want": "step", "kinds": False}, _ctx(clock)
        )
        assert api.calls[0][2] == {"text": "x", "want": "step"}
        assert "kinds" not in out
        assert ("GET", "/api/flights/kinds", None) not in api.calls

    def test_no_model_is_a_clear_answer_with_the_kinds(self):
        box, api, clock = _box()
        api.errors[("POST", "/api/flights/describe")] = client.ApiError(
            409, "no_model", {"error": "no_model"}
        )
        out = box.describe_flight({"text": "x", "kinds": False}, _ctx(clock))
        assert out["error"] == "no_model" and out["steps"] == []
        assert "write the steps yourself" in out["hint"]
        assert out["kinds"]  # brought along even with kinds=false

    def test_could_not_describe_carries_the_detail(self):
        box, api, clock = _box()
        api.errors[("POST", "/api/flights/describe")] = client.ApiError(
            422,
            "could_not_describe",
            {"error": "could_not_describe", "detail": "no <flight> block"},
        )
        out = box.describe_flight({"text": "x"}, _ctx(clock))
        assert out["error"] == "could_not_describe"
        assert "no <flight> block" in out["hint"]

    def test_an_old_server_says_upgrade(self):
        box, api, clock = _box()
        api.errors[("POST", "/api/flights/describe")] = client.ApiError(
            404, "{'detail': 'Not Found'}"
        )
        with pytest.raises(ToolError, match="no flights; upgrade"):
            box.describe_flight({"text": "x"}, _ctx(clock))


# --------------------------------------------------------------------------- #
# start_flight
# --------------------------------------------------------------------------- #
class TestStart:
    def test_defaults_to_the_caller_and_records_who(self):
        box, api, clock = _box()
        got = []
        _start_echo(api, got)
        steps = [
            {"kind": "agent", "config": {"prompt": "go"}},
            {"kind": "ship", "config": {"lane": "pr"}},
        ]
        out = box.start_flight({"steps": steps}, _ctx(clock))
        assert got == [{"by": "agent:orch", "steps": steps, "title": "orch"}]
        assert out["id"] == "f_1" and out["session"] == "orch"
        assert out["state"] == "running"
        assert out["steps"][0].startswith('s1 running agent "Stop 1"')

    def test_on_a_descendant_with_plan_at_and_name(self):
        box, api, clock = _box()
        got = []
        _start_echo(api, got)
        api.top_routes[("GET", "/api/flights/plans")] = {
            "plans": [{"id": "p_1", "name": "Mine", "steps": [_step("s1")]}]
        }
        renamed = []
        api.top_routes[("PATCH", "/api/flights/f_1")] = lambda p, q: (
            renamed.append(p)
            or {"flight": _flight(session="w1a", plan_name=p["plan_name"])}
        )
        out = box.start_flight(
            {"plan_id": "p_1", "title": "w1a", "at": "s1", "name": "Nightly"},
            _ctx(clock),
        )
        assert got == [
            {"by": "agent:orch", "plan_id": "p_1", "title": "w1a", "start_at": "s1"}
        ]
        assert renamed == [{"plan_name": "Nightly"}]
        assert out["name"] == "Nightly"

    @pytest.mark.parametrize("target", ["other", "root"])
    def test_refuses_a_session_that_is_not_you_or_yours(self, target):
        box, api, clock = _box()
        _start_echo(api, [])
        with pytest.raises(ToolError, match="you or one of your descendants"):
            box.start_flight(
                {"steps": [{"kind": "agent"}], "title": target}, _ctx(clock)
            )
        assert ("POST", "/api/flights/start") not in [(m, p) for m, p, _ in api.calls]

    def test_refuses_a_team_run_member(self):
        box, api, clock = _box(scope="all")
        _start_echo(api, [])
        with pytest.raises(ToolError, match="team run"):
            box.start_flight(
                {"steps": [{"kind": "agent"}], "title": "member"}, _ctx(clock)
            )

    def test_readonly_scope_refuses(self):
        box, api, clock = _box(scope="readonly")
        with pytest.raises(ToolError, match="readonly"):
            box.start_flight({"steps": [{"kind": "agent"}]}, _ctx(clock))

    def test_needs_steps_or_a_plan(self):
        box, _, clock = _box()
        with pytest.raises(ToolError, match="needs steps"):
            box.start_flight({}, _ctx(clock))

    def test_needs_a_title_outside_a_session(self):
        box, _, clock = _box(me=None, scope="all")
        with pytest.raises(ToolError, match="needs title"):
            box.start_flight({"steps": [{"kind": "agent"}]}, _ctx(clock))

    def test_grounded_puts_stops_on_the_board(self):
        box, api, clock = _box()
        got = []
        _start_echo(api, got)
        out = box.start_flight(
            {
                "steps": [{"kind": "agent"}],
                "grounded": True,
                "name": "Later",
                "title": "other",
            },
            _ctx(clock),
        )
        assert got == [
            {
                "by": "agent:orch",
                "steps": [{"kind": "agent"}],
                "grounded": True,
                "name": "Later",
            }
        ]
        assert out["state"] == "grounded" and out["session"] is None
        assert "attach" in out["note"]

    @pytest.mark.parametrize(
        "step",
        [
            {"kind": "ship", "config": {"lane": "merge"}},
            {"kind": "land", "title": "Land it"},
        ],
    )
    def test_a_stop_that_cannot_be_undone_needs_confirm(self, step):
        box, api, clock = _box()
        got = []
        _start_echo(api, got)
        with pytest.raises(ToolError, match="confirm_merge"):
            box.start_flight({"steps": [step], "title": "w1"}, _ctx(clock))
        box.start_flight(
            {"steps": [step], "title": "w1", "confirm_merge": True}, _ctx(clock)
        )
        assert len(got) == 1

    def test_merging_yourself_under_children_scope_points_at_grounded(self):
        box, api, clock = _box()
        _start_echo(api, [])
        with pytest.raises(ToolError, match="grounded=true"):
            box.start_flight(
                {
                    "steps": [{"kind": "ship", "config": {"lane": "merge"}}],
                    "confirm_merge": True,
                },
                _ctx(clock),
            )

    def test_a_grounded_merge_still_needs_confirm(self):
        box, api, clock = _box()
        _start_echo(api, [])
        with pytest.raises(ToolError, match="can't be undone"):
            box.start_flight(
                {"steps": [{"kind": "land"}], "grounded": True}, _ctx(clock)
            )

    def test_in_flight_says_what_to_do(self):
        box, api, clock = _box()
        api.errors[("POST", "/api/flights/start")] = client.ApiError(
            409, "in_flight", {"error": "in_flight", "detail": "orch has a flight"}
        )
        with pytest.raises(ToolError) as err:
            box.start_flight({"steps": [{"kind": "agent"}]}, _ctx(clock))
        assert "in_flight — orch has a flight" in str(err.value)
        assert "flight_status" in str(err.value)

    def test_a_bad_step_names_its_field(self):
        box, api, clock = _box()
        api.errors[("POST", "/api/flights/start")] = client.ApiError(
            400,
            "bad_step",
            {"error": "bad_step", "detail": "unknown kind", "field": "steps[0].kind"},
        )
        with pytest.raises(ToolError, match=r"unknown kind \(field steps\[0\]\.kind\)"):
            box.start_flight({"steps": [{"kind": "nope"}]}, _ctx(clock))


# --------------------------------------------------------------------------- #
# flight_status
# --------------------------------------------------------------------------- #
class TestStatus:
    def test_by_id(self):
        box, api, clock = _box(flights=[_flight()])
        out = box.flight_status({"id": "f_1"}, _ctx(clock))
        assert ("GET", "/api/flights/f_1", None) in api.calls
        assert out["cursor"] == "2/3 s2"
        assert out["steps"][1].startswith('s2 running agent "Stop 2"')
        assert out["history"] == ["-16m s1 done"]

    def test_by_title_prefers_the_live_flight(self):
        old = _flight("f_old", session="w1", state="done", updated=9_900.0)
        live = _flight(
            "f_live", session="w1", state="halted", updated=9_000.0, reason="checks red"
        )
        box, api, clock = _box(flights=[old, live, _flight("f_x", session="other")])
        out = box.flight_status({"title": "w1"}, _ctx(clock))
        assert out["id"] == "f_live" and out["reason"] == "checks red"

    def test_defaults_to_your_own_flight(self):
        box, _, clock = _box(flights=[_flight("f_me", session="orch")])
        assert box.flight_status({}, _ctx(clock))["id"] == "f_me"

    def test_no_flight(self):
        box, _, clock = _box()
        with pytest.raises(ToolError, match="has no flight"):
            box.flight_status({"title": "w1"}, _ctx(clock))

    def test_receipts_notes_and_when(self):
        f = _flight(n=2, cursor=1)
        f["steps"][0].update(kind="ship", title="Open PR", when="diff touches src/**")
        f["step_states"]["s1"] = {
            "state": "done",
            "note": "pushed",
            "detail": {"receipt": "PR #291"},
        }
        box, _, clock = _box(flights=[f])
        line = box.flight_status({"id": "f_1"}, _ctx(clock))["steps"][0]
        assert (
            line
            == 's1 done ship "Open PR" · when diff touches src/** · pushed · ✓ PR #291'
        )

    def test_a_big_flight_stays_compact(self):
        n = 40
        f = _flight(n=n, cursor=30, reason="x" * 500)
        for i, s in enumerate(f["steps"]):
            s["title"] = "A long stop title number %d " % i + "y" * 80
            f["step_states"][s["id"]]["note"] = "n" * 300
            f["step_states"][s["id"]]["detail"] = {"receipt": "r" * 100}
        f["history"] = [
            {
                "at": 9_000.0 + i,
                "step": "s%d" % (i % n + 1),
                "event": "done",
                "note": "h" * 200,
            }
            for i in range(200)
        ]
        box, _, clock = _box(flights=[f])
        out = box.flight_status({"id": "f_1"}, _ctx(clock))
        size = len(json.dumps(out, ensure_ascii=False))
        assert size <= mcp_flights.STATUS_BUDGET, size
        assert len(out["history"]) <= 10
        assert out["steps"][0].startswith("s1–s29: 29 done")
        assert any(line.startswith("s31 running") for line in out["steps"])
        assert len(out["reason"]) <= 200

    def test_forty_long_stops_ahead_fold_too(self):
        f = _flight(n=40, cursor=0)
        for s in f["steps"]:
            s["title"] = "t" * 120
            f["step_states"][s["id"]]["note"] = "n" * 300
        box, _, clock = _box(flights=[f])
        out = box.flight_status({"id": "f_1"}, _ctx(clock))
        assert len(json.dumps(out, ensure_ascii=False)) <= mcp_flights.STATUS_BUDGET
        assert out["steps"][0].startswith("s1 running")
        assert out["steps"][-1] == "s7–s40: 34 pending"

    def test_a_small_flight_is_left_whole(self):
        box, _, clock = _box(flights=[_flight(n=5, cursor=4)])
        out = box.flight_status({"id": "f_1"}, _ctx(clock))
        assert len(out["steps"]) == 5


# --------------------------------------------------------------------------- #
# control_flight
# --------------------------------------------------------------------------- #
def _ok(api, method, path, sink):
    def route(payload, q):
        sink.append((path, payload))
        return {"flight": _flight()}

    api.top_routes[(method, path)] = route


class TestControl:
    @pytest.mark.parametrize("op", ["pause", "resume", "cancel"])
    def test_flight_ops(self, op):
        box, api, clock = _box(flights=[_flight()])
        hit = []
        _ok(api, "POST", "/api/flights/f_1/" + op, hit)
        out = box.control_flight({"id": "f_1", "op": op}, _ctx(clock))
        assert hit == [("/api/flights/f_1/" + op, {})]
        assert out["id"] == "f_1"

    @pytest.mark.parametrize("op", ["skip", "retry", "done"])
    def test_step_ops_default_to_the_current_stop(self, op):
        box, api, clock = _box(flights=[_flight(cursor=1)])
        hit = []
        _ok(api, "POST", "/api/flights/f_1/steps/s2/" + op, hit)
        box.control_flight({"id": "f_1", "op": op}, _ctx(clock))
        assert hit == [("/api/flights/f_1/steps/s2/" + op, {})]

    def test_done_on_a_named_perch_with_a_note(self):
        box, api, clock = _box(flights=[_flight()])
        hit = []
        _ok(api, "POST", "/api/flights/f_1/steps/s3/done", hit)
        box.control_flight(
            {"id": "f_1", "op": "done", "step": "s3", "note": "looks good"}, _ctx(clock)
        )
        assert hit == [("/api/flights/f_1/steps/s3/done", {"note": "looks good"})]

    def test_jump_needs_a_step(self):
        box, api, clock = _box(flights=[_flight()])
        with pytest.raises(ToolError, match="needs step"):
            box.control_flight({"id": "f_1", "op": "jump"}, _ctx(clock))
        hit = []
        _ok(api, "POST", "/api/flights/f_1/jump", hit)
        box.control_flight({"id": "f_1", "op": "jump", "step": "s1"}, _ctx(clock))
        assert hit == [("/api/flights/f_1/jump", {"step": "s1"})]

    def test_by_title(self):
        box, api, clock = _box(flights=[_flight("f_9", session="w1")])
        hit = []
        _ok(api, "POST", "/api/flights/f_9/pause", hit)
        box.control_flight({"title": "w1", "op": "pause"}, _ctx(clock))
        assert hit

    def test_refuses_another_sessions_flight(self):
        box, api, clock = _box(flights=[_flight(session="other")])
        with pytest.raises(ToolError, match="you or one of your descendants"):
            box.control_flight({"id": "f_1", "op": "cancel"}, _ctx(clock))
        assert not [c for c in api.calls if c[0] == "POST"]

    def test_a_grounded_flight_needs_only_write_scope(self):
        box, api, clock = _box(
            flights=[_flight(session="", state="grounded", cursor=0)]
        )
        hit = []
        _ok(api, "POST", "/api/flights/f_1/cancel", hit)
        box.control_flight({"id": "f_1", "op": "cancel"}, _ctx(clock))
        assert hit

    def test_a_grounded_refusal_explains(self):
        box, api, clock = _box(
            flights=[_flight(session="", state="grounded", cursor=0)]
        )
        api.errors[("POST", "/api/flights/f_1/pause")] = client.ApiError(
            409, "grounded", {"error": "grounded"}
        )
        with pytest.raises(ToolError, match="until your user attaches"):
            box.control_flight({"id": "f_1", "op": "pause"}, _ctx(clock))


# --------------------------------------------------------------------------- #
# edit_flight
# --------------------------------------------------------------------------- #
class TestEdit:
    def test_remove_add_and_rename(self):
        box, api, clock = _box(flights=[_flight(n=4)])
        hit = []
        _ok(api, "DELETE", "/api/flights/f_1/steps/s4", hit)
        _ok(api, "POST", "/api/flights/f_1/steps", hit)
        _ok(api, "PATCH", "/api/flights/f_1", hit)
        add = [{"kind": "loop", "config": {"times": 3}}]
        out = box.edit_flight(
            {"id": "f_1", "remove": ["s4"], "add": add, "at": 2, "rename": "Again"},
            _ctx(clock),
        )
        assert hit == [
            ("/api/flights/f_1/steps/s4", None),
            ("/api/flights/f_1/steps", {"steps": add, "at": 2}),
            ("/api/flights/f_1", {"plan_name": "Again"}),
        ]
        assert out["changed"] == ["removed s4", "added 1", "renamed"]

    def test_needs_something_to_do(self):
        box, _, clock = _box(flights=[_flight()])
        with pytest.raises(ToolError, match="needs add, remove or rename"):
            box.edit_flight({"id": "f_1"}, _ctx(clock))

    def test_refuses_another_sessions_flight(self):
        box, _, clock = _box(flights=[_flight(session="other")])
        with pytest.raises(ToolError, match="you or one of your descendants"):
            box.edit_flight({"id": "f_1", "rename": "x"}, _ctx(clock))

    def test_adding_a_merge_needs_confirm(self):
        box, api, clock = _box(flights=[_flight(session="w1")])
        with pytest.raises(ToolError, match="confirm_merge"):
            box.edit_flight(
                {"id": "f_1", "add": [{"kind": "ship", "config": {"lane": "merge"}}]},
                _ctx(clock),
            )

    def test_the_last_stop_stays(self):
        box, api, clock = _box(flights=[_flight(n=1, cursor=0)])
        api.errors[("DELETE", "/api/flights/f_1/steps/s1")] = client.ApiError(
            409, "last_step", {"error": "last_step"}
        )
        with pytest.raises(ToolError, match="cancel the flight instead"):
            box.edit_flight({"id": "f_1", "remove": ["s1"]}, _ctx(clock))


# --------------------------------------------------------------------------- #
# save_flight_plan
# --------------------------------------------------------------------------- #
class TestSavePlan:
    def test_request_shape(self):
        box, api, clock = _box()
        got = []

        def save(payload, q):
            got.append(payload)
            return {
                "plan": {
                    "id": "p_7",
                    "name": payload["name"],
                    "steps": [_step("s1", title="Work")],
                    "trigger": payload.get("trigger"),
                }
            }

        api.top_routes[("POST", "/api/flights/plans")] = save
        trig = {"kind": "schedule", "schedule": {"at": "09:00", "days": "weekdays"}}
        out = box.save_flight_plan(
            {
                "name": "Morning",
                "steps": [{"kind": "agent"}],
                "trigger": trig,
                "description": "d",
            },
            _ctx(clock),
        )
        assert got == [
            {
                "name": "Morning",
                "steps": [{"kind": "agent"}],
                "description": "d",
                "trigger": trig,
            }
        ]
        assert out == {
            "plan_id": "p_7",
            "name": "Morning",
            "steps": ['s1 agent "Work"'],
            "trigger": trig,
        }

    def test_readonly_refuses(self):
        box, _, clock = _box(scope="readonly")
        with pytest.raises(ToolError, match="readonly"):
            box.save_flight_plan(
                {"name": "x", "steps": [{"kind": "agent"}]}, _ctx(clock)
            )


# --------------------------------------------------------------------------- #
# The PATCH the rename and step edits need
# --------------------------------------------------------------------------- #
def test_api_patch_sends_json_with_the_patch_method(monkeypatch):
    sent = []

    def fake(url, data, timeout, method=None, **kw):
        sent.append((url, json.loads(data), method))
        return {"flight": {}}

    monkeypatch.setattr(client, "_request", fake)
    api = Api(host="127.0.0.1", port=8765, env={})
    api._base = "http://127.0.0.1:8765"
    api.patch("/api/flights/f_1", {"plan_name": "n"})
    assert sent == [
        ("http://127.0.0.1:8765/api/flights/f_1", {"plan_name": "n"}, "PATCH")
    ]
