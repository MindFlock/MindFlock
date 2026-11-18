"""The To-do tools (``add_todo``, ``list_todos``, ``update_todo``) over the
fake ``/api/todo`` store in :mod:`tests.unit._mcp_fakes`."""

from __future__ import annotations

import pytest

from backend import client
from backend.mcp import playbooks
from backend.mcp.protocol import SchemaError, ToolError, validate
from backend.mcp.tools import INSTRUCTIONS, build_tools
from backend.mcp import todos

from tests.unit._mcp_fakes import FakeCtx, make_box, row

NAMES = ("add_todo", "list_todos", "update_todo")


def _tree():
    return [
        row("orch"),
        row("w1", parent="orch", spawned=True),
        row("w1a", parent="w1", spawned=True),
        row("other"),
    ]


def _box(me="w1", scope=None, managed_marker=False):
    box, api, clock = make_box(
        _tree(), me=me, scope=scope, managed_marker=managed_marker
    )
    return box, api, FakeCtx(clock)


def _seed(api, *items):
    for i, it in enumerate(items):
        api.todos.append(dict({"id": "s%d" % i, "done": False}, **it))


def _posts(api):
    return [
        p for (m, path, p) in api.calls if m == "POST" and path == "/api/todo/items"
    ]


# --------------------------------------------------------------------------- #
# Registry, text budgets, peer mode
# --------------------------------------------------------------------------- #
class TestRegistry:
    def test_served_with_annotations(self):
        box, _, _ = _box()
        by = {t.name: t for t in build_tools(box)}
        for n in NAMES:
            assert n in by and n in playbooks.TOOL_NAMES
        assert by["list_todos"].annotations["readOnlyHint"] is True
        for n in ("add_todo", "update_todo"):
            ann = by[n].annotations
            assert ann["readOnlyHint"] is False
            assert ann["destructiveHint"] is False
            assert ann["openWorldHint"] is False

    def test_text_budgets_and_closed_schemas(self):
        box, _, _ = _box()
        by = {t.name: t for t in build_tools(box)}
        assert len(INSTRUCTIONS) <= 2000
        for n in NAMES:
            assert len(by[n].description) <= 1500, n
            assert by[n].input_schema["additionalProperties"] is False
            assert n in INSTRUCTIONS

    def test_add_description_says_when(self):
        d = todos.D_ADD
        for phrase in (
            "manual test",
            "login",
            "decision",
            "follow-up after merge",
            "One item per action",
            "NOT enough",
            '"manual_test"',
            '"needs_you"',
            "copy-paste",
        ):
            assert phrase in d, phrase
        assert "even if said in chat" in INSTRUCTIONS

    def test_never_offered_to_a_peer(self):
        from backend.mcp.peer_tools import (
            PEER_INSTRUCTIONS,
            PEER_TOOL_NAMES,
            PeerClient,
            build_peer_tools,
        )

        assert not set(NAMES) & set(PEER_TOOL_NAMES)
        served = {t.name for t in build_peer_tools(PeerClient(env={}))}
        assert not set(NAMES) & served
        assert "todo" not in PEER_INSTRUCTIONS

    def test_schema_caps_items_at_twenty(self):
        box, _, _ = _box()
        schema = {t.name: t for t in build_tools(box)}["add_todo"].input_schema
        ok = {"items": [{"text": "x%d" % i} for i in range(20)]}
        validate(ok, schema)
        with pytest.raises(SchemaError):
            validate({"items": [{"text": "x"}] * 21}, schema)
        with pytest.raises(SchemaError):
            validate({"kind": "chore", "text": "x"}, schema)
        with pytest.raises(SchemaError):
            validate({"items": [{"details": "no text"}]}, schema)


# --------------------------------------------------------------------------- #
# add_todo
# --------------------------------------------------------------------------- #
class TestAdd:
    def test_one_item_is_stamped_with_session_and_source(self):
        box, api, ctx = _box()
        out = box.add_todo(
            {
                "text": "Log in to staging as admin",
                "details": "open https://staging.example/billing",
                "kind": "manual_test",
                "priority": "high",
                "due": "2026-11-20T09:00",
                "link": "https://staging.example",
            },
            ctx,
        )
        (body,) = _posts(api)
        assert body == {
            "text": "Log in to staging as admin",
            "details": "open https://staging.example/billing",
            "kind": "manual_test",
            "priority": "high",
            "due": "2026-11-20T09:00",
            "link": "https://staging.example",
            "session": "w1",
            "source": "agent",
        }
        assert out["added"] == [{"id": api.todos[0]["id"], "text": body["text"]}]
        assert out["updated"] == []
        assert out["session"] == "w1"
        assert out["counts"]["needs_you"] == 1

    def test_defaults_are_sent_as_absent(self):
        box, api, ctx = _box()
        box.add_todo(
            {"text": "  Click   Deploy ", "kind": "task", "priority": "normal"}, ctx
        )
        (body,) = _posts(api)
        assert body == {"text": "Click Deploy", "session": "w1", "source": "agent"}

    def test_several_in_one_call(self):
        box, api, ctx = _box()
        out = box.add_todo(
            {
                "items": [
                    {"text": "Step one", "kind": "manual_test"},
                    {"text": "Step two", "kind": "needs_you", "priority": "low"},
                ]
            },
            ctx,
        )
        (body,) = _posts(api)
        assert [i["text"] for i in body["items"]] == ["Step one", "Step two"]
        assert all(
            i["session"] == "w1" and i["source"] == "agent" for i in body["items"]
        )
        assert body["items"][1]["priority"] == "low"
        assert [a["text"] for a in out["added"]] == ["Step one", "Step two"]
        assert len({a["id"] for a in out["added"]}) == 2

    def test_top_level_and_items_together(self):
        box, api, ctx = _box()
        out = box.add_todo({"text": "First", "items": [{"text": "Second"}]}, ctx)
        assert [a["text"] for a in out["added"]] == ["First", "Second"]

    def test_external_client_sends_no_session(self):
        box, api, ctx = _box(me=None)
        out = box.add_todo({"text": "Approve the release"}, ctx)
        (body,) = _posts(api)
        assert body == {"text": "Approve the release", "source": "agent"}
        assert "session" not in out

    @pytest.mark.parametrize("scope", ["readonly", "children", "all"])
    def test_allowed_in_every_scope(self, scope):
        box, api, ctx = _box(scope=scope)
        box.add_todo({"text": "Check the email"}, ctx)
        assert len(api.todos) == 1

    def test_allowed_when_identity_fails_closed_to_readonly(self):
        # MindFlock attached the server but it cannot tell which session it
        # is: readonly, no session stamp — and still allowed to add.
        box, api, ctx = _box(me=None, managed_marker=True)
        assert box.policy.scope(None) == "readonly"
        box.add_todo({"text": "Check the email"}, ctx)
        assert _posts(api) == [{"text": "Check the email", "source": "agent"}]

    def test_updated_in_place_is_reported(self):
        box, api, ctx = _box()
        _seed(api, {"text": "old", "key": "k1"})
        api.todos[0]["key"] = "k1"

        # The tools never send key; a server-side merge still shows up.
        def merge(payload, q):
            return {
                "added": [],
                "updated": [dict(api.todos[0], text=payload["text"])],
                "counts": {"open": 1, "needs_you": 0, "high": 0},
            }

        api.top_routes[("POST", "/api/todo/items")] = merge
        out = box.add_todo({"text": "new"}, ctx)
        assert out["added"] == [] and out["updated"] == [{"id": "s0", "text": "new"}]

    def test_bad_input_is_refused_before_any_call(self):
        box, api, ctx = _box()
        for args in (
            {},
            {"text": "   "},
            {"text": "x", "kind": "chore"},
            {"text": "x", "priority": "urgent"},
            {"text": "x", "due": "tomorrow"},
            {"text": "x" * (todos.MAX_TEXT + 1)},
            {"items": [{"text": "x"}] * (todos.MAX_ADD + 1)},
            {"items": [{"details": "no text"}]},
        ):
            with pytest.raises(ToolError):
                box.add_todo(args, ctx)
        assert _posts(api) == []

    def test_api_error_is_a_tool_error(self):
        box, api, ctx = _box()
        api.errors[("POST", "/api/todo/items")] = client.ApiError(
            400, "text is required"
        )
        with pytest.raises(ToolError, match="add_todo failed: text is required"):
            box.add_todo({"text": "x"}, ctx)

    def test_server_without_the_extension(self):
        box, api, ctx = _box()
        api.todo_route = False
        with pytest.raises(ToolError, match="no To-do list"):
            box.add_todo({"text": "x"}, ctx)


# --------------------------------------------------------------------------- #
# list_todos
# --------------------------------------------------------------------------- #
class TestList:
    def _seeded(self, me="w1"):
        box, api, ctx = _box(me=me)
        _seed(
            api,
            {"text": "mine open", "session": "w1", "kind": "manual_test"},
            {"text": "mine done", "session": "w1", "done": True},
            {"text": "child's", "session": "w1a", "priority": "high"},
            {"text": "loose"},
            {
                "text": "long",
                "session": "w1",
                "details": "d" * 1000,
                "due": "2026-12-01",
            },
        )
        return box, api, ctx

    def test_mine_by_default_inside_a_session(self):
        box, _, ctx = self._seeded()
        out = box.list_todos({}, ctx)
        assert out["mine"] is True
        assert [r["text"] for r in out["todos"]] == ["mine open", "long"]
        first = out["todos"][0]
        assert first == {
            "id": "s0",
            "text": "mine open",
            "done": False,
            "kind": "manual_test",
            "priority": "normal",
            "session": "w1",
        }
        assert out["counts"]["open"] == 4

    def test_all_and_done(self):
        box, _, ctx = self._seeded()
        out = box.list_todos({"mine": False, "include_done": True}, ctx)
        assert [r["text"] for r in out["todos"]] == [
            "mine open",
            "mine done",
            "child's",
            "loose",
            "long",
        ]
        assert out["todos"][2]["priority"] == "high"
        assert "session" not in out["todos"][3]

    def test_external_client_sees_everyone(self):
        box, _, ctx = self._seeded(me=None)
        out = box.list_todos({}, ctx)
        assert out["mine"] is False
        assert len(out["todos"]) == 4
        noted = box.list_todos({"mine": True}, ctx)
        assert noted["mine"] is False and "note" in noted

    def test_details_preview_and_full_item(self):
        box, _, ctx = self._seeded()
        long = box.list_todos({}, ctx)["todos"][1]
        assert long["has_more_details"] is True
        assert len(long["details"]) <= todos.DETAILS_PREVIEW + 1
        assert long["due"] == "2026-12-01"
        full = box.list_todos({"id": "s4"}, ctx)["todo"]
        assert full["details"] == "d" * 1000 and full["has_more_details"] is False
        with pytest.raises(ToolError, match="no to-do"):
            box.list_todos({"id": "nope"}, ctx)

    def test_limit(self):
        box, _, ctx = self._seeded()
        out = box.list_todos({"mine": False, "limit": 2}, ctx)
        assert len(out["todos"]) == 2 and out["more"] == 2

    def test_readonly_scope_reads(self):
        box, api, ctx = _box(scope="readonly")
        _seed(api, {"text": "x", "session": "w1"})
        assert box.list_todos({}, ctx)["todos"][0]["text"] == "x"

    def test_api_error_is_a_tool_error(self):
        box, api, ctx = _box()
        api.errors[("GET", "/api/todo")] = client.ApiError(500, "boom")
        with pytest.raises(ToolError, match="reading the To-do list failed: boom"):
            box.list_todos({}, ctx)


# --------------------------------------------------------------------------- #
# update_todo
# --------------------------------------------------------------------------- #
class TestUpdate:
    def _seeded(self, me="w1", scope=None):
        box, api, ctx = _box(me=me, scope=scope)
        _seed(
            api,
            {"text": "mine", "session": "w1"},
            {"text": "child's", "session": "w1a"},
            {"text": "loose"},
            {"text": "parent's", "session": "orch"},
            {"text": "stranger's", "session": "other"},
        )
        return box, api, ctx

    def test_own_child_and_loose_items(self):
        box, api, ctx = self._seeded()
        for tid in ("s0", "s1", "s2"):
            out = box.update_todo({"id": tid, "done": True}, ctx)
            assert out["todo"]["done"] is True
        patches = [(p, b) for (m, p, b) in api.calls if m == "PATCH"]
        assert patches[0] == ("/api/todo/items/s0", {"done": True})

    def test_others_are_refused(self):
        box, api, ctx = self._seeded()
        for tid in ("s3", "s4"):
            with pytest.raises(ToolError, match="belongs to session"):
                box.update_todo({"id": tid, "done": True}, ctx)
        assert not [c for c in api.calls if c[0] == "PATCH"]

    def test_scope_all_changes_any(self):
        box, api, ctx = self._seeded(scope="all")
        out = box.update_todo(
            {"id": "s4", "text": "  re  worded ", "priority": "high"}, ctx
        )
        assert out["todo"]["text"] == "re worded"
        assert out["todo"]["priority"] == "high"

    def test_readonly_may_check_off_its_own(self):
        box, api, ctx = self._seeded(scope="readonly")
        assert box.update_todo({"id": "s0", "done": True}, ctx)["todo"]["done"]
        with pytest.raises(ToolError):
            box.update_todo({"id": "s4", "done": True}, ctx)

    def test_external_client(self):
        box, api, ctx = self._seeded(me=None)
        assert box.update_todo({"id": "s2", "done": True}, ctx)["todo"]["done"]
        with pytest.raises(ToolError, match="a session you spawned"):
            box.update_todo({"id": "s0", "done": True}, ctx)
        box.policy.spawned_by_me.add("w1")
        box.update_todo({"id": "s1", "done": True}, ctx)  # w1's descendant

    def test_priority_normal_clears(self):
        box, api, ctx = self._seeded()
        api.todos[0]["priority"] = "high"
        out = box.update_todo({"id": "s0", "priority": "normal"}, ctx)
        assert out["todo"]["priority"] == "normal"
        assert "priority" not in api.todos[0]

    def test_bad_input(self):
        box, api, ctx = self._seeded()
        with pytest.raises(ToolError, match="nothing to change"):
            box.update_todo({"id": "s0"}, ctx)
        with pytest.raises(ToolError, match="no to-do"):
            box.update_todo({"id": "zz", "done": True}, ctx)
        with pytest.raises(ToolError):
            box.update_todo({"id": "s0", "priority": "urgent"}, ctx)

    def test_api_error_is_a_tool_error(self):
        box, api, ctx = self._seeded()
        api.errors[("PATCH", "/api/todo/items/s0")] = client.ApiError(
            400, "priority must be high or low"
        )
        with pytest.raises(ToolError, match=r"update_todo\(s0\) failed: priority"):
            box.update_todo({"id": "s0", "priority": "high"}, ctx)
