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
            "label": "Wait for me",
            "actor": "you",
            "summary": "Wait for a person to press Done.",
            "fields": [{"name": "text"}, {"name": "items"}],
            "default_title": "Wait for me",
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
        # The read-back sentence, and the cookbook for the kinds served.
        assert out["sentence"].startswith("Agent: fix the bug → Open PR")
        agent = next(k for k in out["stops"] if k.startswith("agent "))
        assert "use: the coding work itself" in agent and "not: " in agent
        # A kind the cookbook doesn't know yet: the server's own line.
        assert not any(k.startswith("loop ") and "use:" not in k for k in out["stops"])
        assert any(k.startswith("land") and "[irreversible]" in k for k in out["stops"])
        assert any(k.startswith("you (Wait for me)") for k in out["stops"])
        assert out["rules"] and "recipes" in out
        assert "kinds" not in out
        assert out["on_fail"] == ["halt", "agent", "skip", "you"]

    def test_want_step_and_kinds_off(self):
        box, api, clock = _box()
        api.top_routes[("POST", "/api/flights/describe")] = {"steps": [_step("s1")]}
        out = box.describe_flight(
            {"text": "x", "want": "step", "kinds": False}, _ctx(clock)
        )
        assert api.calls[0][2] == {"text": "x", "want": "step"}
        assert "stops" not in out and "recipes" not in out
        assert ("GET", "/api/flights/kinds", None) not in api.calls

    def test_no_model_is_a_clear_answer_with_the_kinds(self):
        box, api, clock = _box()
        api.errors[("POST", "/api/flights/describe")] = client.ApiError(
            409, "no_model", {"error": "no_model"}
        )
        out = box.describe_flight({"text": "x", "kinds": False}, _ctx(clock))
        assert out["error"] == "no_model" and out["steps"] == []
        assert "write the steps yourself" in out["hint"]
        assert out["stops"] and out["rules"]  # brought along even with kinds=false

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
        with pytest.raises(ToolError, match="no routes; upgrade"):
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
            {
                "error": "bad_step",
                "detail": "until is a time like 18:00",
                "field": "steps[0].config.until",
            },
        )
        with pytest.raises(ToolError) as err:
            box.start_flight({"steps": [{"kind": "agent"}]}, _ctx(clock))
        msg = str(err.value)
        assert "until is a time like 18:00 (field steps[0].config.until)" in msg
        assert "judge is" in msg  # the server's refusal, taught

    def test_an_unknown_kind_is_refused_with_what_it_meant(self):
        box, api, clock = _box()
        with pytest.raises(ToolError) as err:
            box.start_flight({"steps": [{"kind": "approve"}]}, _ctx(clock))
        assert "no stop kind 'approve' — use you" in str(err.value)
        assert not [c for c in api.calls if c[0] == "POST"]


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
        with pytest.raises(ToolError, match="has no route"):
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
    @pytest.mark.parametrize("op", ["pause", "resume", "cancel", "run"])
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
        with pytest.raises(ToolError, match="cancel the route instead"):
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
            "sentence": "Agent",
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


# --------------------------------------------------------------------------- #
# The cookbook, the dry run and the teaching refusals (docs/vision/mcp-routes.md)
# --------------------------------------------------------------------------- #
def _real_box(**kw):
    from backend.web.core import flights as F

    box, api, clock = _box(**kw)
    api.top_routes[("GET", "/api/flights/kinds")] = F.kinds_dto()
    return box, api, clock


class TestCookbookAndDryRun:
    def test_describe_flight_with_every_real_kind_stays_under_its_budget(self):
        from backend.web.core import flight_cookbook as C

        box, api, clock = _real_box()
        steps = [
            _step(
                "s%d" % (i + 1), r["kind"], r.get("title"), config=r.get("config", {})
            )
            for i, r in enumerate(C.RECIPES[1]["steps"])
        ]
        api.top_routes[("POST", "/api/flights/describe")] = {"steps": steps}
        out = box.describe_flight({"text": "ticket to merge after my OK"}, _ctx(clock))
        size = len(json.dumps(out, ensure_ascii=False))
        assert size <= mcp_flights.DESCRIBE_BUDGET + 2500, size
        assert len(out["stops"]) == len(C.STOPS)
        assert len(out["recipes"]) == len(C.RECIPES)
        assert all("use:" in line for line in out["stops"])
        assert out["sentence"].endswith("Merge when green → Ticket: move to Done")
        view = mcp_flights.cookbook_view(api.top_routes[("GET", "/api/flights/kinds")])
        assert len(json.dumps(view, ensure_ascii=False)) <= mcp_flights.DESCRIBE_BUDGET

    def test_a_kind_newer_than_the_cookbook_shows_the_servers_line(self):
        dto = {"kinds": [{"kind": "teleport", "label": "Teleport", "summary": "Go."}]}
        view = mcp_flights.cookbook_view(dto)
        assert view["stops"] == ["teleport · Teleport · Go. · fields: none"]
        assert view["recipes"] == []  # no recipe uses kinds this server lacks

    def test_the_budget_shrinks_lines_rather_than_dropping_kinds(self):
        from backend.web.core import flights as F

        view = mcp_flights.cookbook_view(F.kinds_dto(), budget=6000)
        assert len(view["stops"]) == len(F.STEP_KINDS)
        assert len(json.dumps(view, ensure_ascii=False)) < 9000

    def test_dry_run_reads_back_and_starts_nothing(self):
        box, api, clock = _real_box()
        steps = [
            {"kind": "test", "on_fail": "agent"},
            {"kind": "ship", "config": {"lane": "pr"}},
            {"kind": "you", "config": {"text": "approve"}},
            {"kind": "ship", "config": {"lane": "merge"}},
        ]
        out = box.start_flight({"steps": steps, "dry_run": True}, _ctx(clock))
        assert out["dry_run"] is True and out["session"] == "orch"
        assert out["sentence"] == (
            "Test (agent fixes failures ×2) → Open PR → waits for you: approve → "
            "Merge when green"
        )
        assert out["needs_confirm_merge"] == ["ship"]
        assert "read the sentence back" in out["next"]
        assert not [c for c in api.calls if c[0] == "POST"]

    def test_dry_run_says_a_leading_agent_stop_is_not_sent(self):
        """On an existing session the read-back listed "Agent: next pass" as
        if it would be sent; the route then skipped it and reviewed an empty
        branch. The dry run now says what happens and how to send it."""
        box, api, clock = _real_box()
        steps = [
            {"kind": "agent", "config": {"prompt": "next pass"}},
            {"kind": "review"},
        ]
        out = box.start_flight({"steps": steps, "dry_run": True}, _ctx(clock))
        assert out["first_stop"].startswith("not sent: on orch the work so far")
        assert "at='s1'" in out["first_stop"]
        for extra in ({"at": "s1"}, {"start_on": "run"}):
            out = box.start_flight(
                dict({"steps": steps, "dry_run": True}, **extra), _ctx(clock)
            )
            assert "first_stop" not in out
        out = box.start_flight({"steps": steps[1:], "dry_run": True}, _ctx(clock))
        assert "first_stop" not in out

    def test_dry_run_shows_the_problems(self):
        box, api, clock = _real_box()
        out = box.start_flight(
            {"steps": [{"kind": "join", "config": {"of": "spawned"}}], "dry_run": True},
            _ctx(clock),
        )
        assert out["problems"] and "add a split stop" in out["problems"][0]
        assert out["next"] == "fix the problems first"

    @pytest.mark.parametrize(
        "steps, fix",
        [
            (
                [{"kind": "agent"}, {"kind": "join", "config": {"of": "spawned"}}],
                "add a split stop before it",
            ),
            (
                [
                    {"kind": "ship", "config": {"lane": "merge"}},
                    {"kind": "ship", "config": {"lane": "pr"}},
                ],
                "goes backwards",
            ),
            (
                [{"kind": "agent"}, {"kind": "loop", "config": {"back_to": "s9"}}],
                "stops are numbered",
            ),
            ([{"kind": "land"}, {"kind": "agent"}], "move it to the end"),
        ],
    )
    def test_a_route_that_cannot_work_is_refused_with_the_fix(self, steps, fix):
        box, api, clock = _real_box()
        with pytest.raises(ToolError) as err:
            box.start_flight({"steps": steps, "confirm_merge": True}, _ctx(clock))
        assert fix in str(err.value) and "dry_run=true" in str(err.value)
        assert not [c for c in api.calls if c[0] == "POST"]

    def test_warnings_ride_along_with_a_started_route(self):
        box, api, clock = _real_box()
        captured = []
        _start_echo(api, captured)
        out = box.start_flight(
            {"steps": [{"kind": "agent"}, {"kind": "you"}]}, _ctx(clock)
        )
        assert captured and out["sentence"]
        assert any("config.text" in w for w in out["warnings"])

    @pytest.mark.parametrize("start_on", ["prompt", "run"])
    def test_start_on_builds_a_ready_route(self, start_on):
        box, api, clock = _real_box()
        captured = []

        def start(payload, q):
            captured.append(payload)
            f = _flight(session=payload.get("title"), cursor=0, state="ready")
            f["start_on"] = payload.get("start_on")
            return {"flight": f, "absorbed": []}

        api.top_routes[("POST", "/api/flights/start")] = start
        out = box.start_flight(
            {"steps": [{"kind": "agent"}], "start_on": start_on}, _ctx(clock)
        )
        assert captured[0]["start_on"] == start_on
        assert out["state"] == "ready" and out["note"].startswith("ready:")

    def test_start_on_now_sends_nothing_extra(self):
        box, api, clock = _real_box()
        captured = []
        _start_echo(api, captured)
        box.start_flight({"steps": [{"kind": "agent"}], "start_on": "now"}, _ctx(clock))
        assert "start_on" not in captured[0]

    def test_edit_refuses_a_new_problem_but_not_an_old_one(self):
        box, api, clock = _real_box(flights=[_flight(n=3)])
        with pytest.raises(ToolError, match="add a split stop before it"):
            box.edit_flight(
                {"id": "f_1", "add": [{"kind": "join", "config": {"of": "spawned"}}]},
                _ctx(clock),
            )
        hit = []
        _ok(api, "POST", "/api/flights/f_1/steps", hit)
        box.edit_flight({"id": "f_1", "add": [{"kind": "notify"}]}, _ctx(clock))
        assert hit

    def test_save_dry_run_and_refusal(self):
        box, api, clock = _real_box()
        trig = {"kind": "schedule", "schedule": {"at": "01:00", "days": "weekdays"}}
        out = box.save_flight_plan(
            {
                "name": "Nightly",
                "steps": [{"kind": "agent"}, {"kind": "test", "on_fail": "agent"}],
                "trigger": trig,
                "dry_run": True,
            },
            _ctx(clock),
        )
        assert out["dry_run"] and out["trigger"] == trig
        assert out["sentence"] == "Agent → Test (agent fixes failures ×2)"
        with pytest.raises(ToolError, match="goes backwards"):
            box.save_flight_plan(
                {
                    "name": "x",
                    "steps": [
                        {"kind": "ship", "config": {"lane": "pr"}},
                        {"kind": "ship", "config": {"lane": "commit"}},
                    ],
                },
                _ctx(clock),
            )
        assert not [c for c in api.calls if c[0] == "POST"]

    def test_a_pr_saved_route_does_not_warn_about_its_pr(self):
        box, api, clock = _real_box()
        out = box.save_flight_plan(
            {
                "name": "Bots",
                "steps": [
                    {"kind": "checks"},
                    {"kind": "ship", "config": {"lane": "merge"}},
                ],
                "trigger": {"kind": "pr"},
                "dry_run": True,
            },
            _ctx(clock),
        )
        assert "warnings" not in out and "problems" not in out


class TestSavedRoutesAndSelfMerge:
    PLANS = [
        {
            "id": "builtin:ticket",
            "name": "Ticket to merge",
            "trigger": {"kind": "ticket"},
            "steps": [_step("s1"), _step("s2", "ship", config={"lane": "merge"})],
        },
        {
            "id": "p_9",
            "name": "Nightly chores",
            "trigger": {
                "kind": "schedule",
                "schedule": {"at": "02:00", "days": "daily"},
            },
            "steps": [
                _step("s1", config={"prompt": "bump"}),
                _step("s2", "ship", config={"lane": "pr"}),
            ],
        },
    ]

    def test_describe_lists_the_saved_routes(self):
        box, api, clock = _real_box()
        api.top_routes[("GET", "/api/flights/plans")] = {"plans": self.PLANS}
        api.top_routes[("POST", "/api/flights/describe")] = {"steps": [_step("s1")]}
        out = box.describe_flight({"text": "like last time"}, _ctx(clock))
        assert out["saved"][1].startswith(
            "p_9 · Nightly chores · starts: schedule 02:00 daily"
        )
        assert 'ship(lane="pr")' in out["saved"][1]

    def test_plan_id_takes_a_name(self):
        assert mcp_flights.find_plan(self.PLANS, "nightly chores")["id"] == "p_9"
        assert mcp_flights.find_plan(self.PLANS, "Night")["id"] == "p_9"
        assert mcp_flights.find_plan(self.PLANS, "p_9")["id"] == "p_9"
        assert mcp_flights.find_plan(self.PLANS, "nope") is None
        box, api, clock = _real_box()
        api.top_routes[("GET", "/api/flights/plans")] = {"plans": self.PLANS}
        captured = []
        _start_echo(api, captured)
        out = box.start_flight(
            {"plan_id": "Nightly chores", "steps": [{"kind": "notify"}]}, _ctx(clock)
        )
        assert captured[0]["plan_id"] == "p_9"
        assert "Notify you" in out["sentence"] or captured

    def test_an_unknown_saved_route_lists_the_shelf(self):
        box, api, clock = _real_box()
        api.top_routes[("GET", "/api/flights/plans")] = {"plans": self.PLANS}
        with pytest.raises(
            ToolError, match=r"no saved route 'Weekly' .*Nightly chores \(p_9\)"
        ):
            box.start_flight({"plan_id": "Weekly"}, _ctx(clock))

    def test_dry_run_says_up_front_when_you_may_not_merge_yourself(self):
        box, api, clock = _real_box()
        out = box.start_flight(
            {"steps": [{"kind": "ship", "config": {"lane": "merge"}}], "dry_run": True},
            _ctx(clock),
        )
        assert (
            "scope all" in out["merge_blocked"]
            and "grounded=true" in out["merge_blocked"]
        )
        assert "grounded=true" in out["next"]

    def test_the_self_merge_refusal_says_do_it_grounded_without_asking(self):
        box, api, clock = _real_box()
        with pytest.raises(ToolError, match="no need to ask again"):
            box.start_flight(
                {
                    "steps": [{"kind": "ship", "config": {"lane": "merge"}}],
                    "confirm_merge": True,
                },
                _ctx(clock),
            )
