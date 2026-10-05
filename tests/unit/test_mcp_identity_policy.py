"""MCP identity resolution (backend.mcp.identity) and scope policy
(backend.mcp.policy): who this server is, and what it may steer."""

from __future__ import annotations

import subprocess

import pytest

from backend.mcp.identity import Identity, is_local_row
from backend.mcp.policy import Flock, Policy, parse_scope
from backend.mcp.protocol import ToolError
from tests.unit._mcp_fakes import row


def _tmux(name, rc=0, calls=None):
    def run(cmd, **kw):
        if calls is not None:
            calls.append(cmd)
        return subprocess.CompletedProcess(cmd, rc, stdout=name + "\n", stderr="")

    return run


ROWS = [
    row("alpha"),
    row("beta"),
    row("a.b", tmux_name="mindflock_a_b"),
    row("a_b", tmux_name="mindflock_a_b"),
    row("x_sh", tmux_name="mindflock_x_sh"),
    row("dev::alpha", tmux_name="mindflock_alpha"),
    row("queued", tmux_name="", pending=True),
]


class TestIdentity:
    def test_env_title_wins_without_tmux(self):
        ident = Identity(
            {"MINDFLOCK_SESSION_TITLE": "beta", "TMUX_PANE": "%1", "TMUX": "x"},
            run=_tmux("bogus"),
        )
        assert ident.resolve(ROWS) == "beta"
        assert ident.tmux_session_name() == "bogus"  # only looked up on demand

    def test_env_title_not_live_is_unresolved_with_reason(self):
        ident = Identity({"MINDFLOCK_SESSION_TITLE": "gone"})
        assert ident.resolve(ROWS) is None
        assert "not a live session" in ident.reason

    def test_tmux_exact_match(self):
        calls = []
        ident = Identity(
            {"TMUX": "/tmp/t,1,0", "TMUX_PANE": "%7"},
            run=_tmux("mindflock_alpha", calls=calls),
        )
        assert ident.resolve(ROWS) == "alpha"
        assert calls == [
            ["tmux", "display-message", "-p", "-t", "%7", "#{session_name}"]
        ]

    def test_no_pane_means_no_tmux_lookup(self):
        # A bare display-message answers for the focused window, i.e. some
        # OTHER session the human is looking at — never ask without -t.
        calls = []
        ident = Identity(
            {"TMUX": "/tmp/t,1,0", "MINDFLOCK_MCP_MANAGED": "1"},
            run=_tmux("mindflock_alpha", calls=calls),
        )
        assert ident.resolve(ROWS) is None
        assert calls == []

    def test_remote_row_with_same_tmux_name_is_ignored(self):
        # dev::alpha carries the same tmux_name but lives on another device.
        ident = Identity({"TMUX_PANE": "%1", "TMUX": "x"}, run=_tmux("mindflock_alpha"))
        assert ident.resolve(ROWS) == "alpha"

    def test_shell_pane_suffix_is_stripped(self):
        ident = Identity(
            {"TMUX_PANE": "%1", "TMUX": "x"}, run=_tmux("mindflock_beta_sh")
        )
        assert ident.resolve(ROWS) == "beta"

    def test_exact_match_beats_suffix_strip(self):
        # A title that really ends in _sh must not be read as x's shell pane.
        rows = ROWS + [row("x", tmux_name="mindflock_x")]
        ident = Identity({"TMUX_PANE": "%1", "TMUX": "x"}, run=_tmux("mindflock_x_sh"))
        assert ident.resolve(rows) == "x_sh"

    def test_ambiguous_tmux_name_is_unresolved(self):
        ident = Identity({"TMUX_PANE": "%1", "TMUX": "x"}, run=_tmux("mindflock_a_b"))
        assert ident.resolve(ROWS) is None
        assert "more than one" in ident.reason

    def test_unknown_tmux_session(self):
        ident = Identity({"TMUX_PANE": "%1", "TMUX": "x"}, run=_tmux("my-own-shell"))
        assert ident.resolve(ROWS) is None
        assert "not a MindFlock session" in ident.reason

    def test_tmux_failure_is_unresolved(self):
        ident = Identity({"TMUX_PANE": "%1", "TMUX": "x"}, run=_tmux("", rc=1))
        assert ident.resolve(ROWS) is None

    def test_tmux_missing_binary(self):
        def boom(cmd, **kw):
            raise FileNotFoundError("tmux")

        ident = Identity({"TMUX_PANE": "%1", "TMUX": "x"}, run=boom)
        assert ident.resolve(ROWS) is None

    def test_outside_tmux_is_external(self):
        ident = Identity({}, run=_tmux("never"))
        assert ident.resolve(ROWS) is None
        assert "not running inside" in ident.reason

    def test_tmux_is_looked_up_once_and_title_cached(self):
        calls = []
        ident = Identity(
            {"TMUX_PANE": "%1", "TMUX": "x"}, run=_tmux("mindflock_alpha", calls=calls)
        )
        ident.resolve(ROWS)
        ident.resolve(ROWS)
        assert len(calls) == 1

    def test_reresolves_when_cached_title_vanishes(self):
        env = {"MINDFLOCK_SESSION_TITLE": "beta"}
        ident = Identity(env)
        assert ident.resolve(ROWS) == "beta"
        assert ident.resolve([r for r in ROWS if r["title"] != "beta"]) is None
        assert ident.resolve(ROWS) == "beta"

    def test_managed_marker(self):
        assert Identity({"MINDFLOCK_MCP_MANAGED": "1"}).managed is True
        assert Identity({"MINDFLOCK_MCP_MANAGED": "0"}).managed is False
        assert Identity({}).managed is False

    def test_is_local_row(self):
        assert is_local_row(row("a"))
        assert not is_local_row(row("dev::a"))
        assert not is_local_row(row("p", pending=True))
        assert not is_local_row({"title": ""})


# --------------------------------------------------------------------------- #
# Policy
# --------------------------------------------------------------------------- #
TREE = [
    row("root"),
    row("orch", parent="root"),
    row("w1", parent="orch", spawned=True),
    row("w1a", parent="w1", spawned=True),
    row("w2", parent="orch", spawned=True),
    row("other"),
    row("orphan", spawned=True),
    row("stale", parent="closed-long-ago", spawned=True),
    row("dev::far"),
]


class TestFlock:
    def test_lineage_helpers(self):
        f = Flock(TREE, "orch")
        assert f.parent_of("w1a") == "w1"
        assert sorted(f.children_of("orch")) == ["w1", "w2"]
        assert sorted(f.descendants_of("orch")) == ["w1", "w1a", "w2"]
        assert f.ancestors_of("w1a") == ["w1", "orch", "root"]
        assert f.siblings_of("w1") == ["w2"]
        assert f.siblings_of("root") == []

    def test_dangling_parent_reads_as_none(self):
        assert Flock(TREE).parent_of("stale") == ""

    def test_cycle_is_bounded(self):
        f = Flock([row("a", parent="b"), row("b", parent="a")])
        assert set(f.ancestors_of("a")) == {"a", "b"}  # terminates

    def test_self_title_must_be_live_local(self):
        assert Flock(TREE, "nobody").self_title is None
        assert Flock(TREE, "dev::far").self_title is None


class TestScope:
    def test_parse_scope(self):
        assert parse_scope(None) == "children"
        assert parse_scope(" ALL ") == "all"
        assert parse_scope("bogus") is None

    def test_invalid_scope_fails_closed(self):
        p = Policy("bogus", identity_managed=False)
        assert p.scope("orch") == "readonly"
        assert "unknown scope" in p.scope_note("orch")

    def test_managed_marker_without_identity_is_readonly(self):
        p = Policy("all", identity_managed=True)
        assert p.scope(None) == "readonly"
        assert p.scope("orch") == "all"

    def test_external_without_marker_keeps_configured_scope(self):
        assert Policy(None, identity_managed=False).scope(None) == "children"


class TestManaged:
    def test_children_scope_is_transitive_descendants(self):
        p = Policy("children", False)
        f = Flock(TREE, "orch")
        assert (
            p.is_managed(f, "w1") and p.is_managed(f, "w1a") and p.is_managed(f, "w2")
        )
        assert not p.is_managed(f, "root")
        assert not p.is_managed(f, "other")
        assert not p.is_managed(f, "orch")  # never itself
        assert not p.is_managed(f, "dev::far")

    def test_all_scope_manages_any_local(self):
        p = Policy("all", False)
        f = Flock(TREE, "orch")
        assert p.is_managed(f, "other") and p.is_managed(f, "root")
        assert not p.is_managed(f, "orch")
        assert not p.is_managed(f, "dev::far")

    def test_readonly_manages_nothing(self):
        p = Policy("readonly", False)
        assert not p.is_managed(Flock(TREE, "orch"), "w1")

    def test_external_children_scope_manages_its_spawn_and_descendants(self):
        p = Policy("children", False)
        p.spawned_by_me.add("w1")
        f = Flock(TREE, None)
        assert p.is_managed(f, "w1") and p.is_managed(f, "w1a")
        assert not p.is_managed(f, "w2")

    def test_require_managed_messages(self):
        p = Policy("children", False)
        f = Flock(TREE, "orch")
        with pytest.raises(ToolError, match="own session"):
            p.require_managed(f, "orch", "kill_session")
        with pytest.raises(ToolError, match="descendants"):
            p.require_managed(f, "other", "kill_session")
        with pytest.raises(ToolError, match="another device"):
            p.require_managed(f, "dev::far", "kill_session")
        with pytest.raises(ToolError, match="no session named"):
            p.require_managed(f, "ghost", "kill_session")
        assert p.require_managed(f, "w1a", "kill_session")["title"] == "w1a"

    def test_pending_rows_are_refused(self):
        p = Policy("all", False)
        f = Flock(TREE + [row("p", pending=True)], "orch")
        with pytest.raises(ToolError, match="pending"):
            p.require_message(f, "p")


class TestSetParent:
    def test_adopt_orphaned_spawned(self):
        p = Policy("children", False)
        assert p.require_set_parent(Flock(TREE, "orch"), "orphan", "orch")

    def test_cannot_adopt_user_session(self):
        p = Policy("children", False)
        with pytest.raises(ToolError, match="created by the user"):
            p.require_set_parent(Flock(TREE, "orch"), "other", "orch")

    def test_cannot_steal_a_parented_spawned_session(self):
        p = Policy("children", False)
        with pytest.raises(ToolError, match="already has a parent"):
            p.require_set_parent(Flock(TREE, "w1"), "w2", "w1")

    def test_all_scope_adopts_anything(self):
        p = Policy("all", False)
        assert p.require_set_parent(Flock(TREE, "orch"), "other", "orch")

    def test_cycle_refused(self):
        p = Policy("all", False)
        with pytest.raises(ToolError, match="cycle"):
            p.require_set_parent(Flock(TREE, "root"), "orch", "w1a")

    def test_self_refused(self):
        p = Policy("all", False)
        with pytest.raises(ToolError, match="own session"):
            p.require_set_parent(Flock(TREE, "orch"), "orch", "")

    def test_reparent_within_tree_and_detach(self):
        p = Policy("children", False)
        f = Flock(TREE, "orch")
        assert p.require_set_parent(f, "w2", "w1")
        assert p.require_set_parent(f, "w1a", "")

    def test_new_parent_outside_tree_refused(self):
        p = Policy("children", False)
        with pytest.raises(ToolError, match="must be you or one of your descendants"):
            p.require_set_parent(Flock(TREE, "orch"), "w2", "other")

    def test_detach_unmanaged_orphan_refused(self):
        p = Policy("children", False)
        with pytest.raises(ToolError, match="no parent to detach"):
            p.require_set_parent(Flock(TREE, "orch"), "orphan", "")

    def test_readonly_refused(self):
        p = Policy("readonly", False)
        with pytest.raises(ToolError, match="readonly"):
            p.require_set_parent(Flock(TREE, "orch"), "w1", "orch")


class TestIdentityCrossCheck:
    def test_env_title_in_another_live_sessions_pane_fails_closed(self):
        """A stale baked title (launcher written for 'alpha', session reopened
        as something else) must not take over the live 'alpha'."""
        ident = Identity(
            {"MINDFLOCK_SESSION_TITLE": "alpha", "TMUX_PANE": "%1", "TMUX": "x"},
            run=_tmux("mindflock_beta"),
        )
        assert ident.resolve(ROWS) is None
        assert "terminal of session 'beta'" in ident.reason

    def test_env_title_in_its_own_or_shell_pane_resolves(self):
        for pane in ("mindflock_alpha", "mindflock_alpha_sh", "unrelated"):
            ident = Identity(
                {"MINDFLOCK_SESSION_TITLE": "alpha", "TMUX_PANE": "%1", "TMUX": "x"},
                run=_tmux(pane),
            )
            assert ident.resolve(ROWS) == "alpha", pane
