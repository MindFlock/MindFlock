"""MCP: ``fence_session``, ``set_order``, and the order/fence arguments of
``spawn_session`` (plus ``wait_for_session`` reading a held worker as not
started). The fake API answers the routes; the server side is
``test_worker_order``."""

from __future__ import annotations

import pytest

from backend.mcp.protocol import ToolError
from tests.unit._mcp_fakes import FakeCtx, make_box, row


def _tree():
    return [
        row("root"),
        row("orch", parent="root"),
        row("w1", parent="orch", spawned=True),
        row("w2", parent="orch", spawned=True),
        row("other"),
    ]


def _ok(title, payload, q):
    return {"ok": True, "title": title, "got": payload}


class TestFence:
    def test_posts_the_fence_with_me_as_setter(self):
        box, api, clock = make_box(_tree())
        api.routes[("POST", "fence")] = _ok
        out = box.fence_session(
            {
                "title": "w1",
                "only": ["src/api/**"],
                "keep_out": ["db/"],
                "reason": "api",
            },
            FakeCtx(clock),
        )
        assert out["ok"] is True
        assert api.calls[-1] == (
            "POST",
            "/api/instances/w1/fence",
            {
                "by": "orch",
                "only": ["src/api/**"],
                "keep_out": ["db/"],
                "reason": "api",
                "change": "set",
            },
        )
        assert out["change"] == "set"
        # No fence on its row yet: nothing to lift, no reason needed.
        box.fence_session({"title": "w1", "clear": True}, FakeCtx(clock))
        assert api.calls[-1][2] == {"by": "orch", "clear": True, "change": "same"}

    def test_only_my_descendants_and_something_to_fence(self):
        box, api, clock = make_box(_tree())
        api.routes[("POST", "fence")] = _ok
        with pytest.raises(ToolError, match="descendants"):
            box.fence_session({"title": "other", "only": ["a"]}, FakeCtx(clock))
        with pytest.raises(ToolError, match="own session"):
            box.fence_session({"title": "orch", "only": ["a"]}, FakeCtx(clock))
        with pytest.raises(ToolError, match="only=|keep_out"):
            box.fence_session({"title": "w1"}, FakeCtx(clock))

    def test_readonly_scope_refuses(self):
        box, api, clock = make_box(_tree(), scope="readonly")
        with pytest.raises(ToolError):
            box.fence_session({"title": "w1", "only": ["a"]}, FakeCtx(clock))


def _fenced_tree(state="running"):
    rows = _tree()
    rows[2]["order"] = {
        "state": state,
        "fence": {
            "only": ["src/api/**"],
            "keep_out": ["src/api/keys.py"],
            "by": "orch",
        },
    }
    return rows


class TestFenceSticks:
    """A started worker's fence is its contract: narrowing is free, widening
    or lifting it needs a reason (the owner's "they block an area, then just
    unblock it")."""

    def test_lifting_a_started_worker_needs_a_reason(self):
        box, api, clock = make_box(_fenced_tree())
        api.routes[("POST", "fence")] = _ok
        with pytest.raises(ToolError, match="reason=") as err:
            box.fence_session({"title": "w1", "clear": True}, FakeCtx(clock))
        assert "lifts" in str(err.value) and "spawn a worker" in str(err.value)
        assert not api.paths("POST")
        out = box.fence_session(
            {"title": "w1", "clear": True, "reason": "the task now spans the db"},
            FakeCtx(clock),
        )
        assert out["change"] == "lifted"
        assert api.calls[-1][2] == {
            "by": "orch",
            "clear": True,
            "reason": "the task now spans the db",
            "change": "lifted",
        }

    @pytest.mark.parametrize(
        "only, keep_out, change",
        [
            (["src/api/**", "db/models.py"], ["src/api/keys.py"], "widened"),
            (["src/api/**"], [], "widened"),  # a keep-out dropped
            (["src/web/**"], ["src/api/keys.py"], "replaced"),
        ],
    )
    def test_widening_a_started_worker_needs_a_reason(self, only, keep_out, change):
        box, api, clock = make_box(_fenced_tree())
        api.routes[("POST", "fence")] = _ok
        args = {"title": "w1", "only": only, "keep_out": keep_out}
        verb = {"widened": "widens", "replaced": "retargets"}[change]
        with pytest.raises(ToolError, match="this change %s it" % verb):
            box.fence_session(dict(args), FakeCtx(clock))
        with pytest.raises(ToolError, match="reason="):
            box.fence_session(dict(args, reason="ok"), FakeCtx(clock))  # too thin
        out = box.fence_session(
            dict(args, reason="follow-up: the db half too"), FakeCtx(clock)
        )
        assert out["change"] == change

    def test_narrowing_needs_no_reason(self):
        box, api, clock = make_box(_fenced_tree())
        api.routes[("POST", "fence")] = _ok
        out = box.fence_session(
            {"title": "w1", "only": ["src/api/v1/**"], "keep_out": ["src/api/keys.py"]},
            FakeCtx(clock),
        )
        assert out["change"] == "narrowed"
        assert "reason" not in api.calls[-1][2]

    def test_a_held_worker_can_be_refenced_freely(self):
        box, api, clock = make_box(_fenced_tree(state="held"))
        api.routes[("POST", "fence")] = _ok
        out = box.fence_session({"title": "w1", "only": ["src/**"]}, FakeCtx(clock))
        assert out["change"] == "widened"

    def test_a_worker_never_fences_itself(self):
        box, api, clock = make_box(_fenced_tree(), me="w1")
        api.routes[("POST", "fence")] = _ok
        with pytest.raises(ToolError, match="own session"):
            box.fence_session(
                {"title": "w1", "clear": True, "reason": "I need the db too"},
                FakeCtx(clock),
            )
        assert not api.paths("POST")


class TestFenceShapesContext:
    def test_whoami_returns_the_fence_and_its_rule(self):
        box, api, clock = make_box(_fenced_tree(), me="w1")
        out = box.whoami({}, FakeCtx(clock))
        f = out["fence"]
        assert f["only"] == ["src/api/**"] and f["keep_out"] == ["src/api/keys.py"]
        assert f["by"] == "orch"
        assert f["rule"].startswith("Work in src/api/**; read outside only")
        assert "never edit outside" in f["rule"]
        assert 'report_result(status="blocked")' in f["rule"]
        assert "Never edit: src/api/keys.py." in f["rule"]

    def test_whoami_unfenced_has_no_fence(self):
        box, api, clock = make_box(_tree(), me="w1")
        assert "fence" not in box.whoami({}, FakeCtx(clock))

    def test_a_fenced_workers_children_inherit_its_fence(self):
        box, api, clock = make_box(_fenced_tree(), me="w1")
        api.created_status = "running"
        box.spawn_session({"prompt": "x", "title": "w1a"}, FakeCtx(clock))
        payload = next(c[2] for c in api.calls if c[:2] == ("POST", "/api/instances"))
        assert payload["fence"] == {
            "only": ["src/api/**"],
            "keep_out": ["src/api/keys.py"],
            "reason": "inside w1's fence",
        }

    def test_a_child_may_narrow_but_never_widen(self):
        box, api, clock = make_box(_fenced_tree(), me="w1")
        api.created_status = "running"
        box.spawn_session(
            {
                "prompt": "x",
                "title": "w1a",
                "only": ["src/api/v1/**"],
                "keep_out": ["x"],
            },
            FakeCtx(clock),
        )
        payload = next(c[2] for c in api.calls if c[:2] == ("POST", "/api/instances"))
        assert payload["fence"]["only"] == ["src/api/v1/**"]
        assert payload["fence"]["keep_out"] == ["x", "src/api/keys.py"]
        with pytest.raises(ToolError, match="outside your own fence"):
            box.spawn_session(
                {"prompt": "x", "title": "w1b", "only": ["db/**"]}, FakeCtx(clock)
            )
        with pytest.raises(ToolError, match="outside your own fence"):
            box.spawn_session(
                {"prompt": "x", "title": "w1c", "in_place": True, "only": ["**"]},
                FakeCtx(clock),
            )


class TestOrder:
    def test_no_arguments_reads(self):
        box, api, clock = make_box(_tree())
        api.routes[("GET", "order")] = {"order": None}
        assert box.set_order({}, FakeCtx(clock)) == {"order": None}
        assert api.calls[-1][:2] == ("GET", "/api/instances/orch/order")

    def test_sets_steps_naming_future_workers(self):
        box, api, clock = make_box(_tree())
        api.routes[("POST", "order")] = _ok
        box.set_order(
            {"steps": [["w1", "w2"], ["w3-not-yet"]], "max_parallel": 2}, FakeCtx(clock)
        )
        assert api.calls[-1] == (
            "POST",
            "/api/instances/orch/order",
            {"steps": [["w1", "w2"], ["w3-not-yet"]], "max_parallel": 2},
        )

    def test_refuses_other_sessions_and_myself(self):
        box, api, clock = make_box(_tree())
        api.routes[("POST", "order")] = _ok
        with pytest.raises(ToolError, match="not one of your workers"):
            box.set_order({"steps": [["w1"], ["other"]]}, FakeCtx(clock))
        with pytest.raises(ToolError, match="yourself"):
            box.set_order({"after": {"w1": ["orch"]}}, FakeCtx(clock))

    def test_needs_an_identity(self):
        box, api, clock = make_box(_tree(), me=None)
        with pytest.raises(ToolError, match="identity"):
            box.set_order({"mode": "serial"}, FakeCtx(clock))


class TestSpawnOrder:
    def test_after_and_fence_go_in_the_create_and_a_hold_is_said(self):
        box, api, clock = make_box(_tree())
        api.created_status = "running"
        original = api._create

        def create(payload):
            out = original(payload)
            out["order"] = {
                "held": True,
                "after": ["w1"],
                "why": {"w1": "asked"},
                "fence": payload.get("fence"),
            }
            return out

        api._create = create
        out = box.spawn_session(
            {
                "prompt": "do the api",
                "title": "w3",
                "after": ["w1"],
                "only": ["src/api/**"],
                "keep_out": ["db/"],
                "fence_reason": "api only",
                "overlap": "parallel",
            },
            FakeCtx(clock),
        )
        payload = next(c[2] for c in api.calls if c[:2] == ("POST", "/api/instances"))
        assert payload["after"] == ["w1"]
        assert payload["fence"] == {
            "only": ["src/api/**"],
            "keep_out": ["db/"],
            "reason": "api only",
        }
        assert payload["overlap"] == "parallel"
        assert out["prompt_delivery"] == "held"
        assert out["order"]["after"] == ["w1"]
        assert any("held until w1 is done" in w for w in out["warnings"])

    def test_an_unordered_spawn_sends_nothing_new(self):
        box, api, clock = make_box(_tree())
        api.created_status = "running"
        box.spawn_session({"prompt": "x", "title": "w3"}, FakeCtx(clock))
        payload = next(c[2] for c in api.calls if c[:2] == ("POST", "/api/instances"))
        assert not {"after", "fence", "overlap"} & set(payload)


class TestWaitThroughAHold:
    def test_a_held_idle_worker_is_not_finished(self):
        rows = _tree()
        rows[2].update(
            activity="idle",
            activity_since=1.0,
            order={"state": "held", "after": ["w2"]},
        )
        box, api, clock = make_box(rows)
        out = box.wait_for_session({"title": "w1", "timeout_s": 6}, FakeCtx(clock))
        assert out["timed_out"] is True and out["still_running"] == ["w1"]
        # Released and idle long enough: it is.
        api.rows[2]["order"] = {"state": "running"}
        out = box.wait_for_session(
            {"title": "w1", "timeout_s": 60, "settle_s": 1}, FakeCtx(clock)
        )
        assert out["sessions"]["w1"]["reason"] == "idle"
