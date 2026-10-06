"""The ship and ticket tools (backend.mcp.ship) against the in-memory API.

``ship_session`` is driven through a small simulation of the server's
fire-and-forget commit and push (:class:`Ship`): the commit finishes a couple
of polls after ``POST /commit``, the push lands (or fails on the shell) a
couple of polls after ``POST /push-branch``. Policy, the mid-turn guard, the
commit-message chain, each step's failure mode, resume/timeout, and the PR
body built from a worker's report are covered; then ``set_autopilot``,
``list_tickets`` and ``spawn_ticket_session``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from backend import client
from backend.mcp import ship as ship_mod
from backend.mcp.policy import Flock, Policy
from backend.mcp.protocol import ToolError
from tests.unit._mcp_fakes import FakeCtx, make_box, row


def _tree(**w1):
    return [
        row("root"),
        row("orch", parent="root"),
        row("w1", parent="orch", spawned=True, **w1),
        row("w1a", parent="w1", spawned=True),
        row("w2", parent="orch", spawned=True),
        row("other"),
        row("dev::far", tmux_name="mindflock_far"),
    ]


class Ship:
    """The server's ship routes for one session, simulated."""

    def __init__(self, api, clock, title="w1"):
        self.api = api
        self.clock = clock
        self.title = title
        self.dirty = True
        self.head = "sha1"
        self.upstream = ""
        self.beyond = 1
        self.branch = "mindflock/" + title
        self.base = "main"
        self.committing = False
        self.rc: Optional[int] = None
        self.at: Optional[float] = None
        self.tail = "$ ls\n"
        #: what POST /commit leads to: an exit status, "nothing" (the tree
        #: comes out clean, HEAD unchanged) or None (never finishes)
        self.commit_outcome: Any = 0
        #: what POST /push-branch leads to: "ok", "fail" or None (never)
        self.push_outcome: Optional[str] = "ok"
        self.polls_to_finish = 2
        #: False: the shell has not started the typed one-liner yet, so the
        #: lock is not taken and the PREVIOUS attempt's marker is on disk.
        self.shell_starts_at_once = True
        self._commit_polls: Optional[int] = None
        self._push_polls: Optional[int] = None
        self.stage = "agent"
        self.pr_url: Optional[str] = None
        self.merge_state: Optional[dict] = None
        self.make_pr: Dict[str, Any] = {"ok": True, "url": "https://gh.test/pr/7"}
        self.merge_pr: Dict[str, Any] = {"ok": True}
        self.pending_message = ""
        self.suggested: Any = "Add the thing\n\nBecause."
        self.posts: List[tuple] = []
        r = api.routes
        r[("GET", "ship-status")] = self.status
        r[("POST", "commit")] = self.commit
        r[("GET", "commit-message")] = lambda t, p, q: {"message": self.pending_message}
        r[("POST", "commit-message/suggest")] = self.suggest
        r[("POST", "push-branch")] = self.push
        r[("GET", "stage")] = lambda t, p, q: {
            "title": t,
            "stage": self.stage,
            "pr_url": self.pr_url,
            "merge_state": self.merge_state,
        }
        r[("POST", "make-pr")] = self.mkpr
        r[("POST", "merge-pr")] = self.merge

    # -- routes -------------------------------------------------------------- #
    def _advance(self):
        if self._commit_polls is not None:
            self._commit_polls += 1
            if self._commit_polls >= self.polls_to_finish:
                self._commit_polls = None
                self.committing = False
                out = self.commit_outcome
                if out == "nothing":
                    self.dirty = False
                elif out == 0:
                    self.rc, self.at = 0, self.clock.now
                    self.head = "sha2"
                    self.dirty = False
                    self.beyond += 1
                else:
                    self.rc, self.at = out, self.clock.now
                    self.tail += "black....Failed\n- hook id: black\nreformatted x.py\n"
        if self._push_polls is not None:
            self._push_polls += 1
            if self._push_polls >= self.polls_to_finish:
                self._push_polls = None
                if self.push_outcome == "ok":
                    self.upstream = self.head
                    self.tail += "To origin\n * [new branch] HEAD -> x\n"
                elif self.push_outcome == "fail":
                    self.tail += (
                        " ! [rejected]        HEAD -> x (fetch first)\n"
                        "error: failed to push some refs\n"
                    )

    def status(self, title, payload, q):
        self._advance()
        doc = {
            "title": title,
            "now": self.clock.now,
            "branch": self.branch,
            "base": self.base,
            "head_sha": self.head,
            "upstream_sha": self.upstream,
            "pushed": bool(self.head) and self.head == self.upstream,
            "dirty": self.dirty,
            "beyond_base": self.beyond,
            "committing": self.committing,
            "commit_rc": self.rc,
            "commit_at": self.at,
            "has_origin": True,
            "failed_step": "black" if self.rc not in (None, 0) else None,
            "failed_hook": "black" if self.rc not in (None, 0) else None,
        }
        if int(q.get("tail") or 0):
            doc["shell_tail"] = self.tail
        return doc

    def commit(self, title, payload, q):
        self.posts.append(("commit", payload))
        if self.shell_starts_at_once:
            # The one-liner takes the lock and drops the old marker first.
            self.committing = True
            self.rc = None
            self.at = None
        self.tail += "$ git add -A; git commit -F .mindflock_commit_msg\n"
        if self.commit_outcome is not None:
            self._commit_polls = 0
        return {"ok": True}

    def suggest(self, title, payload, q):
        if isinstance(self.suggested, Exception):
            raise self.suggested
        return {"message": self.suggested}

    def push(self, title, payload, q):
        self.posts.append(("push", payload))
        self.tail += "$ " + ship_mod.PUSH_COMMAND + "\n"
        if self.push_outcome is not None:
            self._push_polls = 0
        return {"ok": True}

    def mkpr(self, title, payload, q):
        self.posts.append(("make-pr", payload))
        if self.make_pr.get("ok"):
            self.stage, self.pr_url = "pr", self.make_pr.get("url")
        return self.make_pr

    def merge(self, title, payload, q):
        self.posts.append(("merge-pr", payload))
        return self.merge_pr


def _setup(me="orch", scope=None, **w1):
    box, api, clock = make_box(_tree(**w1), me=me, scope=scope)
    box.ship_poll_s = 1.0
    sim = Ship(api, clock)
    return box, api, clock, sim


def _ctx(clock):
    return FakeCtx(clock)


def _steps(out):
    return [(s["step"], s["state"]) for s in out["steps"]]


# --------------------------------------------------------------------------- #
# Policy
# --------------------------------------------------------------------------- #
class TestShipPolicy:
    @pytest.mark.parametrize("target", ["other", "root", "w2x"])
    def test_non_descendants_are_refused(self, target):
        box, api, clock, _ = _setup()
        if target == "w2x":
            api.rows.append(row("w2x", parent="other", spawned=True))
        with pytest.raises(ToolError) as err:
            box.ship_session({"title": target, "depth": "commit"}, _ctx(clock))
        assert "you or one of your descendants" in err.value.message

    def test_remote_is_refused(self):
        box, _, clock, _ = _setup()
        with pytest.raises(ToolError) as err:
            box.ship_session({"title": "dev::far", "depth": "push"}, _ctx(clock))
        assert "another device" in err.value.message

    def test_readonly_is_refused(self):
        box, _, clock, _ = _setup(scope="readonly")
        with pytest.raises(ToolError) as err:
            box.set_autopilot({"title": "w1", "depth": "pr"}, _ctx(clock))
        assert "readonly" in err.value.message

    def test_grandchild_and_self_are_allowed(self):
        box, api, clock, sim = _setup()
        sim.dirty = False
        out = box.ship_session({"title": "w1a", "depth": "commit"}, _ctx(clock))
        assert out["ok"] is True
        out = box.ship_session({"depth": "commit"}, _ctx(clock))
        assert out["title"] == "orch" and out["ok"] is True

    def test_merge_needs_confirm(self):
        box, _, clock, _ = _setup()
        with pytest.raises(ToolError) as err:
            box.ship_session({"title": "w1", "depth": "merge"}, _ctx(clock))
        assert "confirm_merge=true" in err.value.message
        with pytest.raises(ToolError):
            box.set_autopilot({"title": "w1", "depth": "merge"}, _ctx(clock))

    def test_merging_yourself_needs_scope_all(self):
        box, _, clock, _ = _setup()
        with pytest.raises(ToolError) as err:
            box.ship_session({"depth": "merge", "confirm_merge": True}, _ctx(clock))
        assert "scope all" in err.value.message

    def test_policy_unit_rules(self):
        flock = Flock(_tree(), "orch")
        p = Policy("children", identity_managed=True)
        assert p.require_ship(flock, "orch", "x")["title"] == "orch"
        assert p.require_ship(flock, "w1", "x", merge=True, confirm_merge=True)
        with pytest.raises(ToolError):
            p.require_ship(flock, "orch", "x", merge=True, confirm_merge=True)
        p_all = Policy("all", identity_managed=True)
        assert p_all.require_ship(flock, "other", "x")
        assert p_all.require_ship(flock, "orch", "x", merge=True, confirm_merge=True)
        with pytest.raises(ToolError):
            p_all.require_ship(flock, "dev::far", "x")

    def test_external_client_ships_only_what_it_spawned(self):
        box, api, clock, sim = _setup(me=None)
        with pytest.raises(ToolError) as err:
            box.ship_session({"title": "w1", "depth": "commit"}, _ctx(clock))
        assert "spawned" in err.value.message
        box.policy.spawned_by_me.add("w1")
        sim.dirty = False
        assert box.ship_session({"title": "w1", "depth": "commit"}, _ctx(clock))["ok"]

    def test_external_client_needs_a_title(self):
        box, _, clock, _ = _setup(me=None)
        with pytest.raises(ToolError) as err:
            box.ship_session({"depth": "commit"}, _ctx(clock))
        assert "needs title" in err.value.message


# --------------------------------------------------------------------------- #
# The mid-turn guard
# --------------------------------------------------------------------------- #
class TestShippableGuard:
    @pytest.mark.parametrize(
        "kw,needle",
        [
            ({"activity": "working"}, "mid-turn"),
            ({"activity": "clarify"}, "dialog"),
            ({"activity": "limit"}, "usage limit"),
            ({"status": "loading"}, "still starting"),
            ({"status": "paused"}, "paused"),
        ],
    )
    def test_refused_with_the_autopilot_hint(self, kw, needle):
        box, api, clock, sim = _setup(**kw)
        with pytest.raises(ToolError) as err:
            box.ship_session({"title": "w1", "depth": "pr"}, _ctx(clock))
        assert needle in err.value.message
        assert ("POST", "/api/instances/w1/commit", {"message": "x"}) not in api.calls
        if needle != "still starting" and needle != "paused":
            assert "set_autopilot" in err.value.message
        assert sim.posts == []

    def test_an_armed_autopilot_is_not_raced(self):
        box, api, clock, sim = _setup(
            autopilot={"depth": "pr", "state": "running", "note": "waiting for idle"}
        )
        with pytest.raises(ToolError) as err:
            box.ship_session({"title": "w1", "depth": "push"}, _ctx(clock))
        assert "autopilot armed" in err.value.message
        assert 'depth="off"' in err.value.message
        assert sim.posts == []
        # A finished or halted run no longer drives anything.
        api.row("w1")["autopilot"] = {"depth": "pr", "state": "halted"}
        sim.dirty = False
        assert box.ship_session({"title": "w1", "depth": "commit"}, _ctx(clock))["ok"]

    def test_self_is_never_refused_for_being_mid_turn(self):
        box, api, clock, _ = _setup()
        api.row("orch")["activity"] = "working"
        sim = Ship(api, clock, title="orch")
        sim.dirty = False
        out = box.ship_session({"depth": "commit"}, _ctx(clock))
        assert out["ok"] is True


# --------------------------------------------------------------------------- #
# Commit
# --------------------------------------------------------------------------- #
class TestCommit:
    def test_commit_with_the_suggested_message(self):
        box, api, clock, sim = _setup()
        out = box.ship_session({"title": "w1", "depth": "commit"}, _ctx(clock))
        assert out["ok"] is True and out["depth"] == "commit"
        (step,) = out["steps"]
        assert step == {
            "step": "commit",
            "state": "done",
            "sha": "sha2",
            "message": "Add the thing",
        }
        assert sim.posts == [("commit", {"message": "Add the thing\n\nBecause."})]
        assert "warnings" not in out

    def test_message_chain(self):
        # explicit > a blocked attempt's > suggested > report headline > default
        box, api, clock, sim = _setup()
        sim.pending_message = "Pending subject"
        box.ship_session(
            {"title": "w1", "depth": "commit", "message": "Mine"}, _ctx(clock)
        )
        assert sim.posts[-1][1]["message"] == "Mine"
        sim.dirty, sim.head = True, "sha3"
        box.ship_session({"title": "w1", "depth": "commit"}, _ctx(clock))
        assert sim.posts[-1][1]["message"] == "Pending subject"

    def test_suggest_failure_falls_back_to_the_report_headline(self):
        box, api, clock, sim = _setup(
            last_report={"status": "done", "summary": "Fixed the login loop\nmore"}
        )
        sim.suggested = client.ApiError(502, "no CLI could write it")
        out = box.ship_session({"title": "w1", "depth": "commit"}, _ctx(clock))
        assert sim.posts[-1][1]["message"] == "Fixed the login loop"
        assert any("no CLI could write it" in w for w in out["warnings"])

    def test_generic_message_as_a_last_resort(self):
        box, api, clock, sim = _setup()
        sim.suggested = client.ApiError(502, "nope")
        out = box.ship_session({"title": "w1", "depth": "commit"}, _ctx(clock))
        assert sim.posts[-1][1]["message"] == "Work from MindFlock session w1"
        assert any("generic" in w for w in out["warnings"])

    def test_hook_failure_is_an_error_with_the_output(self):
        box, api, clock, sim = _setup()
        sim.commit_outcome = 1
        with pytest.raises(ToolError) as err:
            box.ship_session({"title": "w1", "depth": "pr"}, _ctx(clock))
        e = err.value
        assert "blocked by a pre-commit hook (black)" in e.message
        assert e.data["failed_hook"] == "black"
        assert "reformatted x.py" in e.data["output_tail"]
        assert e.data["steps"] == [{"step": "commit", "state": "failed"}]
        assert [p[0] for p in sim.posts] == ["commit"]  # never pushed

    def test_an_old_failure_marker_is_not_this_commit(self):
        """The marker predates the POST: the commit still running must not
        read as that old failure."""
        box, api, clock, sim = _setup()
        sim.rc, sim.at = 1, clock.now - 100
        sim.shell_starts_at_once = False
        sim.polls_to_finish = 3
        out = box.ship_session({"title": "w1", "depth": "commit"}, _ctx(clock))
        assert out["steps"][0]["state"] == "done"
        assert out["steps"][0]["sha"] == "sha2"

    def test_tree_coming_out_clean_is_skipped(self):
        box, api, clock, sim = _setup()
        sim.commit_outcome = "nothing"
        out = box.ship_session({"title": "w1", "depth": "commit"}, _ctx(clock))
        assert out["steps"][0]["state"] == "skipped"

    def test_clean_tree_skips_the_commit(self):
        box, api, clock, sim = _setup()
        sim.dirty = False
        out = box.ship_session({"title": "w1", "depth": "commit"}, _ctx(clock))
        assert _steps(out) == [("commit", "skipped")]
        assert sim.posts == []

    def test_commit_route_error(self):
        box, api, clock, sim = _setup()
        api.errors[("POST", "/api/instances/w1/commit")] = client.ApiError(
            409, "workspace not ready"
        )
        with pytest.raises(ToolError) as err:
            box.ship_session({"title": "w1", "depth": "commit"}, _ctx(clock))
        assert "workspace not ready" in err.value.message

    def test_a_running_commit_is_waited_for_first(self):
        box, api, clock, sim = _setup()
        sim.committing, sim._commit_polls = True, 0
        out = box.ship_session({"title": "w1", "depth": "commit"}, _ctx(clock))
        # Someone else's commit took the changes: nothing left to commit.
        assert _steps(out) == [("commit", "skipped")]
        assert sim.posts == []

    def test_wait_false_returns_after_starting(self):
        box, api, clock, sim = _setup()
        out = box.ship_session(
            {"title": "w1", "depth": "pr", "wait": False}, _ctx(clock)
        )
        assert out["ok"] is True
        assert out["steps"][0]["state"] == "started"
        assert "call ship_session again" in out["hint"]

    def test_timeout_resumes(self):
        box, api, clock, sim = _setup()
        sim.commit_outcome = None  # never finishes
        out = box.ship_session(
            {"title": "w1", "depth": "pr", "timeout_s": 5}, _ctx(clock)
        )
        assert out["timed_out"] is True
        assert out["steps"][-1] == {"step": "commit", "state": "running"}
        assert "call ship_session again" in out["hint"]


# --------------------------------------------------------------------------- #
# Push
# --------------------------------------------------------------------------- #
class TestPush:
    def test_commit_then_push(self):
        box, api, clock, sim = _setup()
        out = box.ship_session({"title": "w1", "depth": "push"}, _ctx(clock))
        assert _steps(out) == [("commit", "done"), ("push", "done")]
        assert out["steps"][1]["sha"] == "sha2"
        assert out["branch"] == "mindflock/w1"
        assert [p[0] for p in sim.posts] == ["commit", "push"]

    def test_already_pushed_is_skipped(self):
        box, api, clock, sim = _setup()
        sim.dirty, sim.upstream = False, "sha1"
        out = box.ship_session({"title": "w1", "depth": "push"}, _ctx(clock))
        assert _steps(out) == [("commit", "skipped"), ("push", "skipped")]

    def test_nothing_to_push(self):
        box, api, clock, sim = _setup()
        sim.dirty, sim.beyond = False, 0
        with pytest.raises(ToolError) as err:
            box.ship_session({"title": "w1", "depth": "push"}, _ctx(clock))
        assert "no commits beyond main" in err.value.message

    def test_push_failure_carries_the_shell_output(self):
        box, api, clock, sim = _setup()
        sim.dirty = False
        sim.push_outcome = "fail"
        with pytest.raises(ToolError) as err:
            box.ship_session({"title": "w1", "depth": "pr"}, _ctx(clock))
        assert "push failed" in err.value.message
        assert "[rejected]" in err.value.data["output_tail"]
        assert "make-pr" not in [p[0] for p in sim.posts]

    def test_an_old_push_failure_on_screen_is_not_news(self):
        box, api, clock, sim = _setup()
        sim.dirty = False
        sim.tail = "$ " + ship_mod.PUSH_COMMAND + "\nerror: failed to push some refs\n"
        sim.polls_to_finish = 3
        out = box.ship_session({"title": "w1", "depth": "push"}, _ctx(clock))
        assert out["steps"][-1]["state"] == "done"

    @pytest.mark.parametrize(
        "msg,needle",
        [
            ("checks haven't passed for this commit", "set_autopilot"),
            ("red zone breached: config/x (config/)", "Zoned files need a human"),
            ("no origin remote — add one with: git remote add", "no origin"),
        ],
    )
    def test_push_route_refusals(self, msg, needle):
        box, api, clock, sim = _setup()
        sim.dirty = False
        api.errors[("POST", "/api/instances/w1/push-branch")] = client.ApiError(
            409, msg
        )
        with pytest.raises(ToolError) as err:
            box.ship_session({"title": "w1", "depth": "push"}, _ctx(clock))
        assert needle in err.value.message
        assert err.value.data["steps"] == [
            {
                "step": "commit",
                "state": "skipped",
                "reason": "nothing to commit (working tree clean)",
            }
        ]

    def test_push_failure_pure(self):
        before = "$ " + ship_mod.PUSH_COMMAND + "\nfatal: old\n"
        assert ship_mod.push_failure(before, before) is None
        now = before + "$ " + ship_mod.PUSH_COMMAND + "\n"
        assert ship_mod.push_failure(before, now) is None
        now += "fatal: Authentication failed\n"
        assert "Authentication failed" in ship_mod.push_failure(before, now)
        assert ship_mod.push_failure("", "no push here") is None


# --------------------------------------------------------------------------- #
# PR and merge
# --------------------------------------------------------------------------- #
class TestPrAndMerge:
    def _pushed(self, **w1):
        box, api, clock, sim = _setup(**w1)
        sim.dirty, sim.upstream = False, "sha1"
        return box, api, clock, sim

    def test_pr_url_and_the_worker_report_as_body(self):
        box, api, clock, sim = self._pushed(
            last_report={"status": "done", "summary": "Added X.\nTests: 12 pass."}
        )
        out = box.ship_session({"title": "w1", "depth": "pr"}, _ctx(clock))
        assert out["ok"] is True and out["pr_url"] == "https://gh.test/pr/7"
        assert out["steps"][-1] == {
            "step": "pr",
            "state": "done",
            "url": "https://gh.test/pr/7",
        }
        _, payload = sim.posts[-1]
        assert payload["body"].startswith("Added X.\nTests: 12 pass.")
        assert "worker `w1` (status: done)" in payload["body"]
        assert "title" not in payload and "base" not in payload

    def test_no_report_means_the_commit_derived_body(self):
        box, api, clock, sim = self._pushed()
        box.ship_session({"title": "w1", "depth": "pr"}, _ctx(clock))
        assert sim.posts[-1] == ("make-pr", {})

    def test_explicit_pr_fields(self):
        box, api, clock, sim = self._pushed(
            last_report={"status": "done", "summary": "ignored"}
        )
        box.ship_session(
            {
                "title": "w1",
                "depth": "pr",
                "pr_title": "T",
                "pr_body": "B",
                "base": "develop",
            },
            _ctx(clock),
        )
        assert sim.posts[-1] == (
            "make-pr",
            {"base": "develop", "title": "T", "body": "B"},
        )

    def test_an_open_pr_is_skipped(self):
        box, api, clock, sim = self._pushed()
        sim.stage, sim.pr_url = "pr", "https://gh.test/pr/1"
        out = box.ship_session({"title": "w1", "depth": "pr"}, _ctx(clock))
        assert out["steps"][-1]["state"] == "skipped"
        assert out["pr_url"] == "https://gh.test/pr/1"
        assert "make-pr" not in [p[0] for p in sim.posts]

    def test_browser_handoff_is_not_ok_and_stops(self):
        box, api, clock, sim = self._pushed()
        sim.make_pr = {
            "ok": False,
            "compare_url": "https://gh.test/compare/x",
            "message": "MindFlock could not open the pull request",
        }
        out = box.ship_session(
            {"title": "w1", "depth": "merge", "confirm_merge": True}, _ctx(clock)
        )
        assert out["ok"] is False
        assert out["steps"][-1]["state"] == "handoff"
        assert out["steps"][-1]["url"] == "https://gh.test/compare/x"
        assert "merge-pr" not in [p[0] for p in sim.posts]

    def test_make_pr_error(self):
        box, api, clock, sim = self._pushed()
        api.errors[("POST", "/api/instances/w1/make-pr")] = client.ApiError(
            400, "nothing to PR: no commits between main and x"
        )
        with pytest.raises(ToolError) as err:
            box.ship_session({"title": "w1", "depth": "pr"}, _ctx(clock))
        assert "nothing to PR" in err.value.message

    def test_merge(self):
        box, api, clock, sim = self._pushed()
        sim.merge_state = {"can_merge": True, "checks": "ok", "blockers": []}
        out = box.ship_session(
            {"title": "w1", "depth": "merge", "confirm_merge": True}, _ctx(clock)
        )
        assert out["ok"] is True
        assert _steps(out)[-2:] == [("pr", "done"), ("merge", "done")]

    @pytest.mark.parametrize(
        "ms,needle",
        [
            (
                {"can_merge": False, "checks": "ok", "blockers": ["needs review"]},
                "needs review",
            ),
            ({"can_merge": True, "checks": "failed", "blockers": []}, "CI failed"),
            ({"can_merge": True, "checks": "pending", "blockers": []}, "still running"),
        ],
    )
    def test_merge_gates(self, ms, needle):
        box, api, clock, sim = self._pushed()
        sim.stage, sim.pr_url, sim.merge_state = "pr", "https://gh.test/pr/1", ms
        with pytest.raises(ToolError) as err:
            box.ship_session(
                {"title": "w1", "depth": "merge", "confirm_merge": True},
                _ctx(clock),
            )
        assert needle in err.value.message
        assert err.value.data["pr_url"] == "https://gh.test/pr/1"
        assert "merge-pr" not in [p[0] for p in sim.posts]

    def test_merge_without_a_pr(self):
        box, api, clock, sim = self._pushed()
        sim.make_pr = {"ok": True, "url": "u"}
        api.routes[("GET", "stage")] = lambda t, p, q: {"stage": "pushed"}
        with pytest.raises(ToolError) as err:
            box.ship_session(
                {"title": "w1", "depth": "merge", "confirm_merge": True},
                _ctx(clock),
            )
        assert "no open PR" in err.value.message

    def test_merge_handoff(self):
        box, api, clock, sim = self._pushed()
        sim.stage, sim.pr_url = "pr", "https://gh.test/pr/1"
        sim.merge_pr = {"ok": False, "pr_url": "https://gh.test/pr/1", "message": "m"}
        out = box.ship_session(
            {"title": "w1", "depth": "merge", "confirm_merge": True}, _ctx(clock)
        )
        assert out["ok"] is False and out["steps"][-1]["state"] == "handoff"

    def test_old_server_without_ship_status(self):
        box, api, clock, sim = _setup()
        api.route_missing.add("ship-status")
        del api.routes[("GET", "ship-status")]
        with pytest.raises(ToolError) as err:
            box.ship_session({"title": "w1", "depth": "commit"}, _ctx(clock))
        assert "upgrade" in err.value.message


# --------------------------------------------------------------------------- #
# set_autopilot
# --------------------------------------------------------------------------- #
class TestSetAutopilot:
    def test_arm(self):
        box, api, clock = make_box(_tree())
        seen = []

        def _arm(title, payload, q):
            seen.append((title, payload))
            return {
                "ok": True,
                "autopilot": {"depth": payload["depth"], "state": "running"},
            }

        api.routes[("POST", "fast-track")] = _arm
        out = box.set_autopilot(
            {"title": "w1", "depth": "pr", "message": "M", "base": "dev"},
            FakeCtx(clock),
        )
        assert seen == [
            ("w1", {"depth": "pr", "by": "agent:orch", "message": "M", "base": "dev"})
        ]
        assert out["autopilot"] == {"depth": "pr", "state": "running"}
        assert "get_session" in out["hint"]

    def test_an_agent_never_raises_a_lane_its_user_set(self):
        """set_autopilot on a session whose USER chose "commit": depth pr would
        push and open a PR the user did not ask for. Stopping is fine."""
        box, api, clock = make_box(
            _tree(lane={"target": "commit", "ask_first": False, "by": "user"})
        )
        seen = []
        api.routes[("POST", "fast-track")] = lambda t, p, q: seen.append(p) or {}
        with pytest.raises(ToolError) as err:
            box.set_autopilot({"title": "w1", "depth": "pr"}, FakeCtx(clock))
        assert "stop at commit" in str(err.value)
        box.set_autopilot({"title": "w1", "depth": "commit"}, FakeCtx(clock))
        assert [p["depth"] for p in seen] == ["commit"]

    def test_ask_first_is_the_users_to_lift(self):
        box, api, clock = make_box(
            _tree(lane={"target": "pr", "ask_first": True, "by": "user"}), me="w1"
        )
        api.routes[("POST", "fast-track")] = lambda t, p, q: {}
        api.routes[("DELETE", "fast-track")] = lambda t, p, q: {"stopped": True}
        with pytest.raises(ToolError) as err:
            box.set_autopilot({"depth": "pr"}, FakeCtx(clock))
        assert "ask them before it ships" in str(err.value)
        # Turning its own autopilot OFF never ships anything: allowed.
        box.set_autopilot({"depth": "off"}, FakeCtx(clock))

    def test_a_lane_an_agent_set_it_may_change(self):
        box, api, clock = make_box(
            _tree(lane={"target": "commit", "ask_first": False, "by": "agent:orch"})
        )
        api.routes[("POST", "fast-track")] = lambda t, p, q: {}
        box.set_autopilot({"title": "w1", "depth": "pr"}, FakeCtx(clock))

    def test_a_team_run_member_ships_only_with_its_group(self):
        box, api, clock = make_box(
            _tree(run={"id": "r_x", "name": "g", "role": "task"}), me="w1"
        )
        api.routes[("POST", "fast-track")] = lambda t, p, q: {}
        with pytest.raises(ToolError) as err:
            box.set_autopilot({"depth": "pr"}, FakeCtx(clock))
        assert "part of group" in str(err.value)
        with pytest.raises(ToolError):
            box.ship_session({"depth": "pr", "wait": False}, FakeCtx(clock))

    def test_ship_session_never_bypasses_ask_first(self):
        box, api, clock = make_box(
            _tree(
                lane={"target": "pr", "ask_first": True, "by": "user"},
                autopilot={"depth": "commit", "state": "done"},
            )
        )
        with pytest.raises(ToolError) as err:
            box.ship_session({"title": "w1", "depth": "pr"}, FakeCtx(clock))
        assert "ask them before it ships" in str(err.value)

    def test_arming_works_while_the_worker_is_mid_turn(self):
        box, api, clock = make_box(_tree())
        api.row("w1")["activity"] = "working"
        api.routes[("POST", "fast-track")] = {
            "ok": True,
            "autopilot": {"depth": "push"},
        }
        out = box.set_autopilot({"title": "w1", "depth": "push"}, FakeCtx(clock))
        assert out["autopilot"]["depth"] == "push"

    def test_off_disarms(self):
        box, api, clock = make_box(_tree())
        api.routes[("DELETE", "fast-track")] = {"ok": True, "stopped": True}
        out = box.set_autopilot({"title": "w1", "depth": "off"}, FakeCtx(clock))
        assert out == {"title": "w1", "autopilot": None, "stopped": True}

    def test_merge_with_confirm(self):
        box, api, clock = make_box(_tree())
        api.routes[("POST", "fast-track")] = lambda t, p, q: {
            "ok": True,
            "autopilot": {"depth": p["depth"]},
        }
        out = box.set_autopilot(
            {"title": "w1", "depth": "merge", "confirm_merge": True}, FakeCtx(clock)
        )
        assert out["autopilot"]["depth"] == "merge"

    def test_server_refusal(self):
        box, api, clock = make_box(_tree())
        api.errors[("POST", "/api/instances/w1/fast-track")] = client.ApiError(
            409, "workspace not ready"
        )
        with pytest.raises(ToolError) as err:
            box.set_autopilot({"title": "w1", "depth": "pr"}, FakeCtx(clock))
        assert "workspace not ready" in err.value.message

    def test_unmanaged_refused(self):
        box, api, clock = make_box(_tree())
        with pytest.raises(ToolError):
            box.set_autopilot({"title": "other", "depth": "pr"}, FakeCtx(clock))


# --------------------------------------------------------------------------- #
# Tickets
# --------------------------------------------------------------------------- #
def _tickets():
    return {
        "sources": ["sc", "jira"],
        "tickets": [
            {
                "source": "sc",
                "id": "23588",
                "slug": "sc-23588",
                "name": "Fix the login loop",
                "url": "https://app.shortcut.com/x/story/23588",
                "session": "sc-23588",
                "has_session": False,
                "eligible": True,
                "reasons": [],
                "assignee": "me",
                "bucket": "In Progress",
                "branch": "feature/sc-23588/fix",
            },
            {
                "source": "jira",
                "id": "PROJ-7",
                "slug": "proj-7",
                "name": "Billing export",
                "url": "https://jira/PROJ-7",
                "session": "proj-7",
                "has_session": True,
                "eligible": False,
                "reasons": ["has a session"],
                "assignee": "me",
                "bucket": "Todo",
            },
            {
                "source": "jira2",
                "id": "PROJ-7",
                "slug": "proj-7",
                "name": "Billing export (mirror)",
                "url": "https://jira2/PROJ-7",
                "session": "proj-7",
                "has_session": False,
                "eligible": True,
                "reasons": [],
                "assignee": "me",
                "bucket": "Todo",
            },
        ],
        "errors": [{"source": "linear", "error": "bad token"}],
    }


class TestListTickets:
    def test_compact_rows_and_filters(self):
        box, api, clock = make_box(_tree())
        api.top_routes[("GET", "/api/tickets")] = _tickets()
        out = box.list_tickets({}, FakeCtx(clock))
        assert len(out["tickets"]) == 3 and out["more"] == 0
        t = out["tickets"][0]
        assert t["slug"] == "sc-23588" and t["state"] == "In Progress"
        assert "branch" not in t and "bucket" not in t
        assert out["errors"] == [{"source": "linear", "error": "bad token"}]
        out = box.list_tickets({"query": "billing"}, FakeCtx(clock))
        assert [t["source"] for t in out["tickets"]] == ["jira", "jira2"]
        out = box.list_tickets({"source": "jira"}, FakeCtx(clock))
        assert len(out["tickets"]) == 1
        out = box.list_tickets({"startable_only": True, "limit": 1}, FakeCtx(clock))
        assert len(out["tickets"]) == 1 and out["more"] == 1

    def test_unconfigured_is_an_error(self):
        box, api, clock = make_box(_tree())
        api.errors[("GET", "/api/tickets")] = client.ApiError(502, "no sources")
        with pytest.raises(ToolError) as err:
            box.list_tickets({}, FakeCtx(clock))
        assert "no sources" in err.value.message


class TestSpawnTicket:
    def _box(self, me="orch", scope=None, start=None):
        box, api, clock = make_box(_tree(), me=me, scope=scope)
        box.ready_poll_s = 1.0
        api.top_routes[("GET", "/api/tickets")] = _tickets()
        posted: list = []

        def _start(payload, q):
            posted.append(payload)
            if start is not None:
                return start(payload)
            api.rows.append(
                row(
                    "sc-23588",
                    status="running",
                    parent=payload.get("parent", ""),
                    spawned=True,
                    provisioned=True,
                    branch="feature/sc-23588/fix",
                )
            )
            return {
                "started": True,
                "title": "sc-23588",
                "branch": "feature/sc-23588/fix",
                "program": "claude",
                "parent": payload.get("parent", ""),
                "spawned": True,
                "report_back": True,
            }

        api.top_routes[("POST", "/api/tickets/start")] = _start
        return box, api, clock, posted

    def test_start_by_slug(self):
        box, api, clock, posted = self._box()
        out = box.spawn_ticket_session(
            {"ticket": "SC-23588", "note": "API only", "agent": "codex"},
            FakeCtx(clock),
        )
        assert posted == [
            {
                "source": "sc",
                "id": "23588",
                "spawned": True,
                "report_back": True,
                "depth": "off",
                "parent": "orch",
                "agent": "codex",
                "note": "API only",
            }
        ]
        assert out["title"] == "sc-23588" and out["ready"] is True
        assert out["ticket"]["name"] == "Fix the login loop"
        assert out["branch"] == "feature/sc-23588/fix"
        assert out["report_back"] is True and out["autopilot"] == "off"
        assert "not forked from your HEAD" in out["warnings"][0]
        assert "sc-23588" in box.policy.spawned_by_me
        assert "sc-23588" in box.dispatched

    def test_autopilot_and_report_back_pass_through(self):
        box, api, clock, posted = self._box()
        box.spawn_ticket_session(
            {"ticket": "23588", "autopilot": "pr", "report_back": False},
            FakeCtx(clock),
        )
        assert posted[0]["depth"] == "pr" and posted[0]["report_back"] is False

    def test_autopilot_merge_needs_confirm(self):
        box, api, clock, posted = self._box()
        with pytest.raises(ToolError) as err:
            box.spawn_ticket_session(
                {"ticket": "sc-23588", "autopilot": "merge"}, FakeCtx(clock)
            )
        assert "confirm_merge" in err.value.message
        assert posted == []

    def test_ambiguous_ticket_needs_a_source(self):
        box, api, clock, posted = self._box()
        with pytest.raises(ToolError) as err:
            box.spawn_ticket_session({"ticket": "PROJ-7"}, FakeCtx(clock))
        assert "jira, jira2" in err.value.message
        api.top_routes[("POST", "/api/tickets/start")] = lambda p, q: (
            posted.append(p) or {"started": True, "title": "proj-7"}
        )
        box.ready_poll_s = 1.0
        api.rows.append(row("proj-7"))
        box.spawn_ticket_session(
            {"ticket": "PROJ-7", "source": "jira2"}, FakeCtx(clock)
        )
        assert posted[-1]["source"] == "jira2" and posted[-1]["id"] == "PROJ-7"

    def test_unlisted_ticket(self):
        box, api, clock, posted = self._box()
        with pytest.raises(ToolError) as err:
            box.spawn_ticket_session({"ticket": "sc-99"}, FakeCtx(clock))
        assert "pass source" in err.value.message
        with pytest.raises(ToolError):
            box.spawn_ticket_session(
                {"ticket": "https://x/story/99", "source": "sc"}, FakeCtx(clock)
            )
        api.top_routes[("POST", "/api/tickets/start")] = lambda p, q: (
            posted.append(p) or {"started": True, "title": "sc-99", "report_back": True}
        )
        api.rows.append(row("sc-99"))
        box.spawn_ticket_session({"ticket": "sc-99", "source": "sc"}, FakeCtx(clock))
        assert posted[-1]["id"] == "99"

    def test_listing_down_still_starts_with_a_source(self):
        box, api, clock, posted = self._box()
        api.errors[("GET", "/api/tickets")] = client.ApiError(502, "down")
        box.spawn_ticket_session({"ticket": "23588", "source": "sc"}, FakeCtx(clock))
        assert posted[0]["id"] == "23588"

    def test_already_exists(self):
        def _409(payload):
            raise client.ApiError(409, "session sc-23588 already exists — close it")

        box, api, clock, posted = self._box(start=_409)
        with pytest.raises(ToolError) as err:
            box.spawn_ticket_session({"ticket": "sc-23588"}, FakeCtx(clock))
        assert "already exists" in err.value.message
        assert "set_parent" in err.value.message

    def test_spawn_limit_refusal(self):
        def _limit(payload):
            raise client.ApiError(
                409,
                "session orch already has 8 live children (limit MINDFLOCK_MAX_CHILDREN=8)",
            )

        box, api, clock, posted = self._box(start=_limit)
        with pytest.raises(ToolError) as err:
            box.spawn_ticket_session({"ticket": "sc-23588"}, FakeCtx(clock))
        assert "MINDFLOCK_MAX_CHILDREN" in err.value.message

    def test_vanishing_session_reports_the_server_reason(self):
        def _start(payload):
            return {"started": True, "title": "sc-23588", "report_back": True}

        box, api, clock, posted = self._box(start=_start)
        api.create_failures["sc-23588"] = "clone failed: auth"
        with pytest.raises(ToolError) as err:
            box.spawn_ticket_session({"ticket": "sc-23588"}, FakeCtx(clock))
        assert "clone failed: auth" in err.value.message
        assert "sc-23588" not in box.policy.spawned_by_me

    def test_still_provisioning_is_not_an_error(self):
        def _start(payload):
            api_ref[0].rows.append(
                row("sc-23588", status="loading", pending=True, parent="")
            )
            return {"started": True, "title": "sc-23588", "report_back": True}

        api_ref: list = []
        box, api, clock, posted = self._box(start=_start)
        api_ref.append(api)
        out = box.spawn_ticket_session({"ticket": "sc-23588"}, FakeCtx(clock))
        assert out["ready"] is False and "provisioning" in out["hint"]
        assert out["status"] == "loading"

    def test_older_server_without_lineage_is_warned(self):
        def _start(payload):
            api_ref[0].rows.append(row("sc-23588"))
            return {"started": True, "title": "sc-23588"}

        api_ref: list = []
        box, api, clock, posted = self._box(start=_start)
        api_ref.append(api)
        out = box.spawn_ticket_session({"ticket": "sc-23588"}, FakeCtx(clock))
        assert any("not your child" in w for w in out["warnings"])

    def test_readonly_refused(self):
        box, api, clock, posted = self._box(scope="readonly")
        with pytest.raises(ToolError):
            box.spawn_ticket_session({"ticket": "sc-23588"}, FakeCtx(clock))

    def test_external_client_spawns_without_a_parent(self):
        box, api, clock, posted = self._box(me=None)
        box.spawn_ticket_session({"ticket": "sc-23588"}, FakeCtx(clock))
        assert "parent" not in posted[0] and posted[0]["spawned"] is True


# --------------------------------------------------------------------------- #
# Compact rows
# --------------------------------------------------------------------------- #
def test_compact_rows_carry_autopilot_and_pr_url_only_when_set():
    box, api, clock = make_box(_tree())
    api.row("w1")["autopilot"] = {"depth": "pr", "state": "running"}
    api.row("w1")["pr_url"] = "https://gh.test/pr/1"
    api.row("w2")["autopilot"] = None
    out = box.list_sessions({"filter": "children"}, FakeCtx(clock))
    by = {r["title"]: r for r in out["sessions"]}
    assert by["w1"]["autopilot"] == {"depth": "pr", "state": "running"}
    assert by["w1"]["pr_url"] == "https://gh.test/pr/1"
    assert "autopilot" not in by["w2"] and "pr_url" not in by["w2"]
