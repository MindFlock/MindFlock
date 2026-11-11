"""Flow polish (fp-flight-api): what the MCP flight tools tell an agent is
true, and steering a route from them is complete.

* ``start_flight(dry_run=true)`` asks the server's own check
  (``POST /api/flights/check``, the real route here over a tmp store): the
  stops as the server stores them, where it starts and what it skips, the
  session's current route, every refusal with what to change — and ``ok``
  means the real start is accepted.
* Every refusal carries the server's field and detail and, for a closed
  list, the allowed values.
* ``edit_flight`` edits a stop's options / when / halt / on_fail, checks the
  whole request before writing, says what was applied when a write fails,
  and takes ``title``; ``save_flight_plan`` replaces a saved route by ``id``.
* ``describe_flight`` lists each kind's options from the kind definitions.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend import client
from backend.mcp.protocol import ToolError
from backend.web import server
from backend.web.core import flight_driver as D
from backend.web.core import flights as F
from backend.web.routes import flights as routes
from tests.unit._mcp_fakes import FakeCtx, make_box, row

web = TestClient(server.app)


def _inst():
    return SimpleNamespace(
        Path="/repo",
        Branch="b",
        GetWorktreePath=lambda: "/repo",
        GetGitWorktree=lambda: SimpleNamespace(GetRepoPath=lambda: "/repo"),
    )


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_FLIGHTS_DIR", str(tmp_path / "flights"))
    monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setenv("MINDFLOCK_TEST_PLANS_FILE", str(tmp_path / "test_plans.json"))
    monkeypatch.setenv("MINDFLOCK_AUTOPILOT_FILE", str(tmp_path / "autopilot.json"))
    monkeypatch.setenv("MINDFLOCK_AUTH", "0")
    monkeypatch.delenv("CS_WEB_MODE", raising=False)
    monkeypatch.setattr(server.ENGINE, "instances", {"orch": _inst(), "w1": _inst()})
    monkeypatch.setattr(D, "_session_busy", lambda title: "")
    monkeypatch.setattr(D, "_fasttrack_to_absorb", lambda title: None)
    monkeypatch.delattr(D, "preflight", raising=False)
    yield


def _box(**kw):
    """A box whose flights routes are the REAL server's (tmp store)."""
    rows = [
        row("root"),
        row("orch", parent="root"),
        row("w1", parent="orch", spawned=True),
    ]
    box, api, clock = make_box(rows, me="orch", scope=kw.get("scope", "children"))
    api.top_routes[("GET", "/api/flights/kinds")] = F.kinds_dto()
    api.top_routes[("POST", "/api/flights/check")] = lambda p, q: routes.check_start(p)

    def start(p, q):
        r = web.post("/api/flights/start", json=p)
        if r.status_code >= 400:
            body = r.json()
            raise client.ApiError(r.status_code, body.get("error", ""), body)
        return r.json()

    api.top_routes[("POST", "/api/flights/start")] = start
    api.top_routes[("GET", "/api/flights/plans")] = lambda p, q: web.get(
        "/api/flights/plans"
    ).json()
    api.top_routes[("GET", "/api/flights")] = lambda p, q: web.get(
        "/api/flights"
    ).json()
    return box, api, clock


def _ctx(clock):
    return FakeCtx(clock)


# --------------------------------------------------------------------------- #
# The dry run tells the truth
# --------------------------------------------------------------------------- #
class TestDryRun:
    def test_lists_the_normalised_stops_where_it_starts_and_why_it_skips(self):
        box, api, clock = _box()
        steps = [
            {"kind": "agent"},
            {"kind": "checks", "config": {"timeout_minutes": 20}},
        ]
        out = box.start_flight({"steps": steps, "dry_run": True}, _ctx(clock))
        assert out["ok"] is True and "problems" not in out
        assert out["lines"][0].startswith('skipped · s1 agent "Agent works"')
        assert "it has no prompt to send" in out["lines"][0]
        assert out["lines"][1].startswith('▶ s2 checks "CI checks"')
        assert out["start"] == 's2 "CI checks"'
        assert "at='s1'" in out["first_stop"]
        assert F.list_flights() == []
        # The real start, same arguments: accepted, at the stop it said.
        real = box.start_flight({"steps": steps}, _ctx(clock))
        assert real["cursor"].endswith("s2")

    def test_an_option_the_server_drops_is_a_problem_not_an_echo(self):
        box, api, clock = _box()
        out = box.start_flight(
            {
                "steps": [{"kind": "checks", "config": {"timeout": 120}}],
                "dry_run": True,
            },
            _ctx(clock),
        )
        assert out["ok"] is False
        assert any(
            "unknown option 'timeout' on a CI checks stop — it has: timeout_minutes"
            in p
            for p in out["problems"]
        )
        # Never echoed as if accepted: the server refuses the plan outright,
        # so there are no "as it will fly" lines to show for it.
        assert not any("timeout=120" in line for line in out.get("lines") or [])

    def test_a_strict_refusal_lists_the_allowed_values(self):
        box, api, clock = _box()
        out = box.start_flight(
            {
                "steps": [
                    {"kind": "agent"},
                    {"kind": "verify", "config": {"who": "robot"}},
                ],
                "dry_run": True,
                "at": "s1",
            },
            _ctx(clock),
        )
        assert out["ok"] is False
        (p,) = out["problems"]
        assert p.startswith("steps[1].config.who: who is agent, you or both")
        assert "who is one of: both (Agent, then me) | agent (Agent only" in p
        assert "| you (Agent, then my sign-off)" in p
        # …and the real start refuses it the same way, with the values.
        with pytest.raises(ToolError) as err:
            box.start_flight(
                {
                    "steps": [
                        {"kind": "agent"},
                        {"kind": "verify", "config": {"who": "robot"}},
                    ]
                },
                _ctx(clock),
            )
        assert "(field steps[1].config.who)" in str(err.value)
        assert "who is one of: both" in str(err.value)

    def test_says_the_session_is_already_on_a_route(self):
        box, api, clock = _box()
        box.start_flight(
            {"steps": [{"kind": "you", "config": {"text": "ok"}}]}, _ctx(clock)
        )
        out = box.start_flight(
            {"steps": [{"kind": "notify"}], "dry_run": True}, _ctx(clock)
        )
        assert out["ok"] is False and out["in_flight"]["state"]
        assert any("already on a route" in p for p in out["problems"])

    def test_a_preflight_problem_names_the_environments(self, monkeypatch):
        def preflight(plan, *, repo, session=None, attach=False, start_at=None):
            return [
                {
                    "field": "steps[1].config.where",
                    "detail": "no environment 'stage' in this repo",
                    "session": session,
                }
            ]

        monkeypatch.setattr(D, "preflight", preflight, raising=False)
        box, api, clock = _box()
        out = box.start_flight(
            {
                "steps": [
                    {"kind": "agent"},
                    {"kind": "verify", "config": {"where": "stage"}},
                ],
                "dry_run": True,
            },
            _ctx(clock),
        )
        assert out["ok"] is False
        (p,) = out["problems"]
        assert "no environment 'stage'" in p
        assert "where is branch or one of the repo's environments: live" in p
        assert p.endswith("[session orch]")
        assert "live" in out["environments"]

    def test_an_older_server_without_the_check_says_so(self):
        box, api, clock = _box()
        del api.top_routes[("POST", "/api/flights/check")]
        out = box.start_flight(
            {"steps": [{"kind": "notify"}], "dry_run": True}, _ctx(clock)
        )
        assert "only the cookbook's lint ran" in out["checked"]
        assert out["ok"] is True


# --------------------------------------------------------------------------- #
# Refusals an agent can act on
# --------------------------------------------------------------------------- #
def test_a_bad_kind_lists_the_stop_kinds():
    box, api, clock = _box()
    msg = str(
        box._flight_error(
            client.ApiError(
                400,
                "bad_step",
                {
                    "error": "bad_step",
                    "detail": "unknown step kind",
                    "field": "steps[0].kind",
                },
            ),
            "start_flight",
        )
    )
    assert "stop kinds: agent, split, join, ship, verify" in msg


def test_a_lane_refusal_lists_the_lanes():
    box, api, clock = _box()
    msg = str(
        box._flight_error(
            client.ApiError(
                400,
                "bad_step",
                {
                    "error": "bad_step",
                    "detail": "bad lane",
                    "field": "step.config.lane",
                },
            ),
            "edit_flight",
            [{"kind": "ship"}],
        )
    )
    assert "lane is one of: commit | push | pr | merge" in msg


def test_done_on_an_open_verify_says_what_works():
    box, api, clock = _box()
    msg = str(
        box._flight_error(
            client.ApiError(409, "answer_behaviors", {"error": "answer_behaviors"}),
            "control_flight(done)",
        )
    )
    assert "Confirm verified" in msg and "skip" in msg


# --------------------------------------------------------------------------- #
# edit_flight
# --------------------------------------------------------------------------- #
def _ready_route(box, clock):
    return box.start_flight(
        {
            "steps": [
                {"kind": "agent", "config": {"prompt": "go"}},
                {"kind": "checks"},
                {"kind": "notify", "config": {"text": "hi"}},
            ],
            "start_on": "run",
        },
        _ctx(clock),
    )["id"]


def _wire_flight_routes(api):
    def fwd(method):
        def call(p, q, path):
            r = (
                web.request(method, path, json=p)
                if p is not None
                else web.request(method, path)
            )
            if r.status_code >= 400:
                body = r.json()
                raise client.ApiError(r.status_code, body.get("error", ""), body)
            return r.json() if r.content else {}

        return call

    orig = api.request

    def request(method, path, payload=None, **kw):
        if path.startswith("/api/flights/f_"):
            api.calls.append((method, path, payload))
            return fwd(method)(payload, {}, path)
        return orig(method, path, payload, **kw)

    api.request = request


class TestEdit:
    def test_edits_a_stops_options_when_and_halt_by_title(self):
        box, api, clock = _box()
        fid = _ready_route(box, clock)
        _wire_flight_routes(api)
        out = box.edit_flight(
            {
                "title": "orch",
                "edit": [
                    {
                        "step": "s2",
                        "config": {"timeout_minutes": 15},
                        "on_fail": "agent",
                        "when": "weekday",
                        "halt": True,
                    }
                ],
            },
            _ctx(clock),
        )
        assert out["changed"] == ["edited s2 (config, on_fail, when, halt)"]
        s2 = F.get(fid)["steps"][1]
        assert s2["config"] == {"timeout_minutes": 15}
        assert s2["on_fail"] == "agent" and s2["when"] == "weekday" and s2["halt"]

    def test_config_is_merged_into_the_stops_own(self):
        box, api, clock = _box()
        fid = _ready_route(box, clock)
        _wire_flight_routes(api)
        box.edit_flight(
            {"id": fid, "edit": [{"step": "s1", "config": {"fresh": True}}]},
            _ctx(clock),
        )
        assert F.get(fid)["steps"][0]["config"]["prompt"] == "go"
        assert F.get(fid)["steps"][0]["config"]["fresh"] is True

    def test_nothing_is_written_when_any_part_is_bad(self):
        box, api, clock = _box()
        fid = _ready_route(box, clock)
        _wire_flight_routes(api)
        with pytest.raises(ToolError) as err:
            box.edit_flight(
                {
                    "id": fid,
                    "remove": ["s3"],
                    "add": [{"kind": "ship", "config": {"lane": "moon"}}],
                    "edit": [{"step": "s9", "title": "x"}],
                },
                _ctx(clock),
            )
        msg = str(err.value)
        assert msg.startswith("edit_flight: nothing was changed")
        assert "edit s9: no such stop (stops: s1, s2, s3)" in msg
        assert "lane is one of: commit | push | pr | merge" in msg
        assert len(F.get(fid)["steps"]) == 3
        assert not [c for c in api.calls if c[0] in ("DELETE", "PATCH")]

    def test_an_unknown_option_is_refused_before_writing(self):
        box, api, clock = _box()
        fid = _ready_route(box, clock)
        _wire_flight_routes(api)
        with pytest.raises(ToolError, match="unknown option 'timeout' on a CI checks"):
            box.edit_flight(
                {"id": fid, "edit": [{"step": "s2", "config": {"timeout": 5}}]},
                _ctx(clock),
            )

    def test_a_write_that_fails_says_what_was_applied(self):
        box, api, clock = _box()
        fid = _ready_route(box, clock)
        _wire_flight_routes(api)
        orig = api.request

        def request(method, path, payload=None, **kw):
            if method == "PATCH" and path.endswith("/steps/s2"):
                raise client.ApiError(
                    409, "in_progress", {"error": "in_progress", "detail": "started"}
                )
            return orig(method, path, payload, **kw)

        api.request = request
        with pytest.raises(ToolError) as err:
            box.edit_flight(
                {
                    "id": fid,
                    "remove": ["s3"],
                    "edit": [{"step": "s2", "title": "CI"}],
                    "rename": "Again",
                },
                _ctx(clock),
            )
        msg = str(err.value)
        assert "Applied: removed s3" in msg
        assert "Not applied: edit s2 (title), rename" in msg
        assert [s["id"] for s in F.get(fid)["steps"]] == ["s1", "s2"]

    def test_a_stop_already_started_can_only_change_its_halt(self):
        box, api, clock = _box()
        fid = box.start_flight(
            {
                "steps": [
                    {"kind": "you", "config": {"text": "ok"}},
                    {"kind": "notify"},
                ],
                "at": "s1",
            },
            _ctx(clock),
        )["id"]
        _wire_flight_routes(api)
        with pytest.raises(ToolError, match="only a stop still ahead can change"):
            box.edit_flight(
                {"id": fid, "edit": [{"step": "s1", "title": "x"}]}, _ctx(clock)
            )
        out = box.edit_flight(
            {"id": fid, "edit": [{"step": "s1", "halt": True}]}, _ctx(clock)
        )
        assert out["changed"] == ["edited s1 (halt)"]


# --------------------------------------------------------------------------- #
# save_flight_plan replaces by id
# --------------------------------------------------------------------------- #
def test_save_flight_plan_replaces_by_id_or_name_and_keeps_the_trigger():
    box, api, clock = _box()
    api.top_routes[("POST", "/api/flights/plans")] = lambda p, q: web.post(
        "/api/flights/plans", json=p
    ).json()
    trig = {"kind": "schedule", "schedule": {"at": "02:00", "days": "daily"}}
    made = box.save_flight_plan(
        {"name": "Nightly", "steps": [{"kind": "agent"}], "trigger": trig}, _ctx(clock)
    )
    again = box.save_flight_plan(
        {
            "id": "nightly",
            "name": "Nightly",
            "steps": [{"kind": "agent"}, {"kind": "notify"}],
        },
        _ctx(clock),
    )
    assert again["plan_id"] == made["plan_id"] and again["replaced"] == made["plan_id"]
    plan = F.get_plan(made["plan_id"])
    assert len(plan["steps"]) == 2 and plan["trigger"]["kind"] == "schedule"
    assert len([p for p in F.list_plans() if p["name"] == "Nightly"]) == 1
    with pytest.raises(ToolError, match="no saved route 'Weekly' to replace"):
        box.save_flight_plan(
            {"id": "Weekly", "name": "W", "steps": [{"kind": "agent"}]}, _ctx(clock)
        )


def test_save_dry_run_runs_the_strict_check():
    box, api, clock = _box()
    out = box.save_flight_plan(
        {"name": "x", "steps": [{"kind": "command"}], "dry_run": True}, _ctx(clock)
    )
    assert out["ok"] is False
    assert any("a command step needs a command" in p for p in out["problems"])


# --------------------------------------------------------------------------- #
# describe_flight documents every option from the kind definitions
# --------------------------------------------------------------------------- #
def test_describe_lists_every_kinds_options_with_defaults_and_values():
    box, api, clock = _box()
    api.top_routes[("POST", "/api/flights/describe")] = {"steps": []}
    out = box.describe_flight({"text": "anything"}, _ctx(clock))
    by = {line.split(" ", 1)[0]: line for line in out["stops"]}
    assert set(by) == set(F.STEP_KINDS)
    for line in out["stops"]:
        assert " · options: " in line
    v = by["verify"]
    assert "who=both (both = Agent, then me | agent = Agent only" in v
    assert "| you = Agent, then my sign-off" in v
    assert "must give evidence" in v and "your sign-off" in v
    assert "ready=now (now = Right away" in v and "where=branch" in v
    assert by["ship"].startswith(
        "ship · options: lane=push (commit | push | pr | merge); ask_first=false"
    )
