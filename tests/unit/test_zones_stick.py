"""Zones stick: a per-session fence is the worker's task contract.

The owner's complaint: "agents often block off areas to then just unblock
them later". The audit found every change was an orchestrator widening a
fence after the worker asked, and ~60% of the denies that made it ask were
the Bash parser misreading a command. So: every fence change is logged and
said WHY (an ending never reads as an unblock), the deny names the fence and
points at report_result(blocked), and the parser no longer invents targets.
The MCP side (reason required, inheritance, whoami) is
``test_mcp_order_tools``."""

from __future__ import annotations

import json
import os

import pytest

from backend.config import red_zones as rz
from backend.providers import _tool_hook_src as th
from tests.unit.test_tool_hook import _GFILES, _bash_pre, _feed, _mk_green, _pre_write


def _feed_of(key):
    try:
        with open(rz.feed_path(key)) as f:
            return [json.loads(x) for x in f if x.strip()]
    except OSError:
        return []


# --------------------------------------------------------------------------- #
# The fence log: every change says what it did, who did it and why
# --------------------------------------------------------------------------- #
class TestFenceChange:
    @pytest.mark.parametrize(
        "old, new, want",
        [
            (None, {"only": ["a/**"]}, "set"),
            ({"only": ["a/**"]}, {"only": ["a/**"]}, "same"),
            ({"only": ["a/**"]}, {"only": ["a/b/**"]}, "narrowed"),
            ({"only": ["a/**"]}, {"only": ["a/**", "c.py"]}, "widened"),
            ({"only": ["a/**"]}, {"only": ["c/**"]}, "replaced"),
            ({"only": ["a/**"]}, None, "lifted"),
            ({"only": ["a/**"], "keep_out": ["a/x"]}, {"only": ["a/**"]}, "widened"),
            ({"only": ["a/**"]}, {"only": ["a/**"], "keep_out": ["a/x"]}, "narrowed"),
            ({"keep_out": ["z"]}, {"only": ["a/**"], "keep_out": ["z"]}, "narrowed"),
            # a filled-in glob never "covers" the glob it came from
            ({"only": ["src/ax.py"]}, {"only": ["src/a*.py"]}, "widened"),
            ({"only": ["src/a*.py"]}, {"only": ["src/ax.py"]}, "narrowed"),
        ],
    )
    def test_classifies_requests(self, old, new, want):
        assert rz.fence_change(old, new) == want

    def test_reads_stored_fences_too(self):
        stored = {"green": [{"pattern": "a/**"}], "red": [{"pattern": "a/k"}]}
        assert (
            rz.fence_change(stored, {"only": ["a/**"], "keep_out": ["a/k"]}) == "same"
        )
        assert rz.fence_change(stored, {"only": ["a/**"]}) == "widened"


class TestFenceLog:
    def test_set_widen_lift_are_logged_with_who_and_why(self, tmp_path):
        wt = str(tmp_path)
        rz.set_session_fence(wt, "mindflock_w1", ["a/**"], owner="orch:o", by="o")
        rz.set_session_fence(
            wt, "mindflock_w1", ["a/**"], owner="orch:o", by="o"
        )  # same: not logged
        rz.set_session_fence(
            wt,
            "mindflock_w1",
            ["a/**", "b.py"],
            owner="orch:o",
            by="o",
            reason="follow-up needs b",
        )
        assert (
            rz.drop_session_fence(
                wt, "mindflock_w1", cause="lifted", by="o", reason="task changed"
            )
            == 1
        )
        log = rz.fence_log(wt, "mindflock_w1")
        assert [r["event"] for r in log] == ["set", "widened", "lifted"]
        assert log[1]["only"] == ["a/**", "b.py"]
        assert log[1]["reason"] == "follow-up needs b" and log[1]["by"] == "o"
        assert log[2]["by"] == "o" and log[2]["reason"] == "task changed"
        assert log[2]["only"] == ["a/**", "b.py"]  # what was in force

    def test_an_ending_never_reads_as_a_lift(self, tmp_path):
        wt = str(tmp_path)
        rz.set_session_fence(wt, "mindflock_w1", ["a/**"], owner="orch:o", by="o")
        rz.set_session_fence(wt, "mindflock_p1", ["b/**"], owner="run:r1")
        rz.set_session_fence(wt, "mindflock_p2", ["c/**"], owner="run:r1")
        rz.drop_session_fence(wt, "mindflock_w1", cause="ended")
        rz.drop_session_fence(wt, "mindflock_p1")  # a run's piece, no cause given
        rz.drop_session_fence(wt, owner="run:r1", cause="done")
        ev = {r["session"]: r["event"] for r in rz.fence_log(wt) if r["event"] != "set"}
        assert ev == {
            "mindflock_w1": "ended",
            "mindflock_p1": "ended",
            "mindflock_p2": "done",
        }

    def test_unknown_cause_is_removed_not_lifted(self, tmp_path):
        wt = str(tmp_path)
        rz.set_session_fence(wt, "mindflock_w1", ["a/**"], owner="orch:o")
        rz.drop_session_fence(wt, "mindflock_w1")
        assert rz.fence_log(wt)[-1]["event"] == "removed"

    def test_the_log_is_bounded(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rz, "FENCE_LOG_MAX", 3)
        wt = str(tmp_path)
        for i in range(5):
            rz.set_session_fence(wt, "mindflock_w%d" % i, ["a/**"], owner="orch:o")
        log = rz.fence_log(wt, limit=0)
        assert [r["session"] for r in log] == [
            "mindflock_w2",
            "mindflock_w3",
            "mindflock_w4",
        ]

    def test_changes_mid_task_reach_the_sessions_feed_not_the_first_set(self, tmp_path):
        wt = str(tmp_path)
        rz.set_session_fence(wt, "mindflock_w1", ["a/**"], owner="orch:o", by="o")
        assert _feed_of("mindflock_w1") == []  # the set is in front of its task
        rz.set_session_fence(
            wt, "mindflock_w1", ["a/**", "b/**"], owner="orch:o", by="o", reason="why"
        )
        rz.drop_session_fence(wt, "mindflock_w1", cause="ended")
        recs = _feed_of("mindflock_w1")
        assert [r["fence"]["event"] for r in recs] == ["widened", "ended"]
        assert all(r["ev"] == "mindflock" and r["kind"] == "fence" for r in recs)
        assert recs[0]["fence"]["by"] == "o" and recs[0]["fence"]["reason"] == "why"

    def test_guard_names_who_set_the_fence(self, tmp_path):
        wt = str(tmp_path)
        rz.set_session_fence(wt, "mindflock_w1", ["a/**"], owner="orch:o", by="o")
        rz.set_session_fence(wt, "mindflock_p1", ["b/**"], owner="run:r1")
        g = rz._guard_sessions(wt)
        assert g["mindflock_w1"]["by"] == "o"
        assert g["mindflock_p1"]["by"] == "your team run's plan"


# --------------------------------------------------------------------------- #
# The deny names the fence and points at report_result(blocked)
# --------------------------------------------------------------------------- #
def test_fence_deny_template_matches_the_hook():
    assert rz._FENCE_DENY_TMPL == th._MF_FENCE_TMPL


def test_fence_deny_reason_wording():
    r = rz.fence_deny_reason("x/y.py", ["a/**", "b.py"], by="orch")
    assert r.startswith(
        "MindFlock fence: x/y.py is outside your fence (only: `a/**`, `b.py`), "
        "set by orch."
    )
    assert 'call report_result with status "blocked"' in r
    words = r.lower().split()
    assert "user" not in words and "ask" not in words  # never "ask to widen"
    kept = rz.fence_deny_reason("a/k.py", ["a/**"], kept="a/k.py")
    assert "is in your fence's keep-out (`a/k.py`), set by your orchestrator" in kept


def _fenced(tmp_path, red=()):
    """A repo whose session ``mindflock_t1`` (the hook's) is fenced by orch."""
    repo, env = _mk_green(tmp_path, _GFILES, [])
    root = os.path.realpath(repo)
    rz.set_session_fence(
        root,
        "mindflock_t1",
        ["src/green/**"],
        red=list(red),
        owner="orch:orch",
        by="orch",
        no_commit=False,
    )
    assert rz.sync_guard(root, rz.repo_identity(root)[0]) in ("written", "same")
    return repo, env


def test_e2e_fence_deny_names_the_fence_and_is_no_request(tmp_path):
    repo, env = _fenced(tmp_path)
    out = _pre_write(
        env, repo, "Write", {"file_path": os.path.join(repo, "src/other/b.py")}, "t1"
    )
    assert out["permissionDecision"] == "deny"
    want = rz.fence_deny_reason("src/other/b.py", ["src/green/**"], by="orch")
    assert out["permissionDecisionReason"] == want
    rec = [r for r in _feed(env) if r.get("deny")][-1]["deny"]
    # not a "scope request" with [Allow this file]: the worker reports blocked
    assert rec["fence"] is True and rec["request"] is False and rec["kind"] == "green"
    assert (
        _pre_write(
            env,
            repo,
            "Write",
            {"file_path": os.path.join(repo, "src/green/a.py")},
            "t2",
        )
        is None
    )


def test_e2e_fence_keep_out_deny(tmp_path):
    repo, env = _fenced(tmp_path, red=["src/green/a.py"])
    out = _pre_write(
        env, repo, "Edit", {"file_path": os.path.join(repo, "src/green/a.py")}, "t1"
    )
    assert out["permissionDecision"] == "deny"
    assert out["permissionDecisionReason"].startswith(
        "MindFlock fence: src/green/a.py is in your fence's keep-out (`src/green/a.py`), "
        "set by orch."
    )
    rec = [r for r in _feed(env) if r.get("deny")][-1]["deny"]
    assert rec["fence"] is True and rec["kind"] == "red"


def test_e2e_folder_zones_keep_their_own_wording(tmp_path):
    """A user's green zone in an unfenced session still says "the user"."""
    repo, env = _mk_green(tmp_path, _GFILES, ["src/green"])
    out = _pre_write(
        env, repo, "Write", {"file_path": os.path.join(repo, "src/other/b.py")}, "t1"
    )
    assert out["permissionDecisionReason"].startswith("MindFlock scope: ")
    rec = [r for r in _feed(env) if r.get("deny")][-1]["deny"]
    assert rec["request"] is True and "fence" not in rec


# --------------------------------------------------------------------------- #
# The parser stops inventing targets (the audit's ~60% bogus denies)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "cmd, want",
    [
        # an fd number is no argument
        ("git checkout -- f.json 2>/dev/null", {"f.json", "/dev/null"}),
        ("git checkout -- f.json 2>&1 | head -1", {"f.json"}),
        ("echo x >&2", set()),
        ("echo x 1>&2", set()),
        ("cmd 2>> log.txt", {"log.txt"}),
        ("echo 2 > f", {"f"}),  # a real argument "2", spaced: still just f
        ("echo x >&out.txt", {"out.txt"}),  # `>&file` writes the file
        # a mode / owner is no file
        ("chmod +x bin/x", {"bin/x"}),
        ("chmod 755 bin/x", {"bin/x"}),
        ("chmod -R u+x bin", {"bin"}),
        ("chown me:me f", {"f"}),
        ("chmod --reference=a b", {"b"}),
        # into a directory: the file it lands as, not the whole directory
        ("cp /elsewhere/.python-version .", {".python-version"}),
        ("cp a sub", {"sub/a"}),
        ("cp a b sub/", {"sub/a", "sub/b"}),
        ("cp -r /x/dir .", {"dir"}),
        ("mv a sub", {"a", "sub/a"}),
        ("ln -s /x/target .", {"target"}),
        ("cp a b", {"b"}),
        ("mv a b", {"a", "b"}),
    ],
)
def test_bash_targets_no_phantoms(tmp_path, cmd, want):
    (tmp_path / "sub").mkdir()
    root = str(tmp_path)
    w, _r, ok = th._bash_targets(cmd, root)
    assert ok is True
    rel = {p if p.startswith("/dev/") else os.path.relpath(p, root) for p in w}
    assert rel == want, (cmd, rel)


def test_bash_into_dir_still_denies_a_zoned_landing(tmp_path):
    """`cp x .` lands `./x`: inside the fence it passes, outside it is still
    refused — the fix makes the target exact, not looser."""
    repo, env = _fenced(tmp_path)
    ok = _bash_pre(env, repo, "cp /etc/hostname src/green/", "t1")
    assert ok is None
    bad = _bash_pre(env, repo, "cp /etc/hostname .", "t2")
    assert bad["permissionDecision"] == "deny"
    assert bad["permissionDecisionReason"].startswith(
        "MindFlock fence: hostname is outside"
    )
    phantom = _bash_pre(env, repo, "git checkout -- src/green/a.py 2>/dev/null", "t3")
    assert phantom is None
