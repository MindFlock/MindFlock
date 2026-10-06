"""A split, driven end to end against REAL git worktrees.

The engine is faked at the seam the driver uses (``session_create.
create_result`` registers an instance) but every workspace is a real ``git
worktree`` of a temp repository: the lead's, and each piece's, cut from the
lead's commit with ``base_ref`` exactly as a live server does. So the merge
queue really merges, a conflict really conflicts (and is really aborted),
ancestry is really checked, and the check really runs.

Covers: the lead created with its brief; plan proposals validated (422 with
problems, the lead-only rule); approval refusing a dirty lead, then starting
every piece as a fenced worker of the lead; members committing → merged back
one at a time; a conflict handed to the lead, ``report_integrated`` with a
wrong sha (verified false) and the right one; the check on the merged
branch; the release card (title, body, diff); the release arming the lead's
autopilot with the PR title/body; done. Plus: a restart mid-integration
(reconcile verifies ancestry, re-merges what was not merged, says nothing);
the run-level announcements once each; a one-for-all batch starting its lines
as the lead's workers; and the Outbox's plan / release / merge rows.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from backend.session.storage import Status
from backend.web import server
from backend.web.core import autopilot as ap
from backend.web.core import events as events_mod
from backend.web.core import git_merge as gm
from backend.web.core import mailbox as mailbox_mod
from backend.web.core import outbox as outbox_mod
from backend.web.core import team_run_driver as drv
from backend.web.core import team_runs as tr


def _git(path, *args, check=True):
    return subprocess.run(
        ["git", "-C", str(path), *args], check=check, capture_output=True, text=True
    )


class _WtInst:
    def __init__(self, title, path, branch, repo, base_branch="main", parent=""):
        self.Title = title
        self.Program = "claude"
        self.Branch = branch
        self.Path = repo
        self.InPlace = False
        self.Parent = parent
        self.Spawned = bool(parent)
        self.BaseBranch = base_branch
        self.Status = Status.Running
        self.CreatedAt = datetime.fromtimestamp(time.time(), timezone.utc)
        self._wt = path

    def Started(self):  # noqa: N802
        return True

    def GetWorktreePath(self):  # noqa: N802
        return self._wt


@pytest.fixture
def env(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "t@example.test"),
        ("config", "user.name", "t"),
    ):
        _git(repo, *args)
    os.makedirs(repo / "auth")
    for rel, text in (("auth/tokens.py", "T = 1\n"), ("auth/session.py", "S = 1\n")):
        (repo / rel).write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")

    instances: dict = {}
    monkeypatch.setattr(server.ENGINE, "instances", instances)
    monkeypatch.setattr(server.ENGINE, "save", lambda **kw: None)
    rows: dict = {}
    monkeypatch.setattr(
        events_mod, "sessions_snapshot", lambda: [dict(r) for r in rows.values()]
    )
    monkeypatch.setattr(server, "_pending_rows", lambda: [])
    monkeypatch.setattr(server, "_CREATE_FAILURES", {})
    monkeypatch.setattr(server._agent_state, "worked_at", lambda t: None)
    monkeypatch.setattr(server, "_session_limited_until", lambda t: 0.0)
    monkeypatch.setattr(server, "_in_boot_quiet", lambda: False)
    monkeypatch.setattr(server, "_mcp_unattachable", lambda program: None)
    monkeypatch.setattr(server, "_configured_pr_base", lambda: "")
    monkeypatch.setattr(drv, "_DELETED", {})
    monkeypatch.setattr(drv, "_TURN_ENDED", {})
    monkeypatch.setattr(drv, "_UNSUB", None)
    created: list = []
    messages: list = []
    fenced: list = []
    reports: dict = {}

    async def _create(payload):
        title = payload["title"]
        if title in instances:
            return 409, {"error": "instance %s already exists" % title}
        created.append(payload)
        branch = "mf/" + title
        path = str(tmp_path / "wt" / title)
        _git(
            repo,
            "worktree",
            "add",
            "-q",
            "-b",
            branch,
            path,
            payload.get("base_ref") or "main",
        )
        inst = _WtInst(
            title,
            path,
            branch,
            str(repo),
            base_branch=payload.get("base_branch") or "main",
            parent=payload.get("parent") or "",
        )
        instances[title] = inst
        return 202, {
            "title": title,
            "branch": branch,
            "created_at": inst.CreatedAt.timestamp(),
        }

    async def _message(run, text):
        messages.append((run["lead"]["title"], text))
        return True

    async def _fence_route(title, payload):
        fenced.append((title, payload["pattern"], payload["kind"]))
        return SimpleNamespace(status_code=200, body=b"{}")

    def _last_result(recipient, sender, since=None):
        return reports.get(sender)

    monkeypatch.setattr(server._session_create, "create_result", _create)
    monkeypatch.setattr(drv, "_message_lead", _message)
    monkeypatch.setattr(server, "instance_red_zones_add", _fence_route)
    monkeypatch.setattr(mailbox_mod, "last_result", _last_result)
    emitted: list = []
    unsub = events_mod.BUS.subscribe(emitted.append)
    drv.subscribe()
    yield SimpleNamespace(
        repo=str(repo),
        tmp=tmp_path,
        instances=instances,
        rows=rows,
        created=created,
        messages=messages,
        fenced=fenced,
        reports=reports,
        emitted=emitted,
    )
    unsub()
    if drv._UNSUB:
        drv._UNSUB()


def _step(rid, boot=False):
    asyncio.run(drv.step_run(rid, boot=boot))
    return tr.load(rid)


def _wt(env, title):
    return env.instances[title].GetWorktreePath()


def _commit(env, title, rel, text, msg):
    wt = _wt(env, title)
    with open(os.path.join(wt, rel), "w") as f:
        f.write(text)
    _git(wt, "add", rel)
    _git(wt, "commit", "-q", "-m", msg)
    return gm.rev_parse(wt, "HEAD")


def _done(env, title, summary="did it", tests="pytest — 3 passed"):
    """The worker committed, reported done, and went idle."""
    now = time.time()
    env.rows[title] = {
        "title": title,
        "activity": "idle",
        "activity_since": now - 60,
        "stage": "committed",
        "repo": "repo",
        "branch": "mf/" + title,
        "last_report": {"status": "done", "summary": summary, "ts": now},
    }
    env.reports[title] = {"text": "%s\n\nDetails:\nTests: %s" % (summary, tests)}


def _lead_idle(env, title, since=None):
    env.rows[title] = {
        "title": title,
        "activity": "idle",
        "activity_since": since if since is not None else time.time() - 600,
        "repo": "repo",
    }


def _split(env, **kw):
    payload = {
        "name": "Auth cleanup",
        "items": [{"kind": "task", "text": "Clean up auth: tokens and sessions"}],
        "policy": {"lane": "pr", "release": "ask"},
        "repo_path": env.repo,
        "split": True,
    }
    payload.update(kw)
    run, _warnings = asyncio.run(drv.create_run(payload))
    return run


PLAN = [
    {"title": "tokens", "prompt": "Rotate the tokens.", "paths": ["auth/tokens*"]},
    {"title": "sessions", "prompt": "Session store.", "paths": ["auth/session*"]},
]


def _approved(env):
    run = _split(env)
    lead = run["lead"]["title"]
    drv.propose_plan(run["id"], {"pieces": PLAN, "why": "two seams", "from": lead})
    drv.approve_plan(run["id"])
    return run["id"], lead


def _events(env, name):
    return [e for e in env.emitted if e["event"] == name]


class TestTheSplitEndToEnd:
    def test_create_makes_the_lead_with_its_brief(self, env):
        run = _split(env)
        assert run["state"] == "planning" and run["split"] is True
        lead = run["lead"]["title"]
        assert lead == "clean-up-auth-tokens-lead"
        (payload,) = env.created
        assert payload["title"] == lead and "parent" not in payload
        assert payload["prompt"].startswith("Clean up auth: tokens and sessions\n\n---")
        assert (
            "mcp__mindflock__propose_run_plan(run_id=%s" % run["id"]
            in payload["prompt"]
        )
        assert run["policy"]["grouping"] == "together"

    def test_a_cli_without_the_tools_cannot_lead_a_split(self, env, monkeypatch):
        monkeypatch.setattr(server, "_mcp_unattachable", lambda p: "not supported")
        with pytest.raises(drv.RunError) as err:
            _split(env)
        assert "this CLI doesn't get the MindFlock tools" in err.value.message
        assert tr.list_runs() == [] and env.created == []

    def test_a_lead_that_cannot_be_created_leaves_no_group(self, env, monkeypatch):
        async def _fail(payload):
            return 409, {"error": "branch taken"}

        monkeypatch.setattr(server._session_create, "create_result", _fail)
        with pytest.raises(drv.RunError) as err:
            _split(env)
        assert "lead could not be started: branch taken" in err.value.message
        assert err.value.status == 409 and tr.list_runs() == []

    def test_the_plan_is_validated_and_only_the_lead_proposes(self, env):
        run = _split(env)
        lead = run["lead"]["title"]
        overlap = [
            {"title": "a", "prompt": "x", "paths": ["auth/**"]},
            {"title": "b", "prompt": "y", "paths": ["auth/session.py"]},
        ]
        with pytest.raises(drv.RunError) as err:
            drv.propose_plan(run["id"], {"pieces": overlap, "from": lead})
        assert err.value.status == 422
        assert err.value.extra["problems"] == [
            {"piece": "b", "error": "overlaps a on auth/session.py"}
        ]
        assert "overlaps a on auth/session.py" in err.value.message
        with pytest.raises(drv.RunError) as err:
            drv.propose_plan(run["id"], {"pieces": PLAN, "from": "someone-else"})
        assert err.value.status == 409 and "only the group's lead" in err.value.message
        out = drv.propose_plan(run["id"], {"pieces": PLAN, "why": "two", "from": lead})
        assert out["problems"] == [] and len(out["plan"]["pieces"]) == 2
        stored = tr.load(run["id"])
        assert stored["state"] == "plan_ready"
        assert stored["plan"]["by"] == "agent:" + lead and stored["plan"]["round"] == 1

    def test_approval_refuses_a_dirty_lead_then_starts_fenced_workers(self, env):
        run = _split(env)
        lead = run["lead"]["title"]
        drv.propose_plan(run["id"], {"pieces": PLAN, "from": lead})
        with open(os.path.join(_wt(env, lead), "auth/tokens.py"), "w") as f:
            f.write("groundwork\n")
        with pytest.raises(drv.RunError) as err:
            drv.approve_plan(run["id"])
        assert (
            err.value.status == 409
            and "commit its groundwork first" in err.value.message
        )
        _git(_wt(env, lead), "commit", "-qam", "groundwork")
        head = gm.rev_parse(_wt(env, lead), "HEAD")
        dto = drv.approve_plan(run["id"])
        assert dto["state"] == "running" and dto["concurrency"] == 2
        assert [t["title"] for t in dto["tasks"]] == [
            "clean-up-auth-tokens-tokens",
            "clean-up-auth-tokens-sessions",
        ]
        r = _step(run["id"])
        assert [t["state"] for t in r["tasks"]] == ["starting", "starting"]
        for p in env.created[1:]:
            assert p["parent"] == lead and p["spawned"] is True
            assert p["base_ref"] == head and p["base_branch"] == "mf/" + lead
            assert "Change only files under:" in p["prompt"]
        assert [t["base_sha"] for t in r["tasks"]] == [head, head]
        # Pieces arm at commit, never ask first, whatever the group's lane.
        rec = ap.get(r["tasks"][0]["title"])
        assert rec["lane"] == "commit" and rec["ask_first"] is False
        r = _step(run["id"])
        assert [t["state"] for t in r["tasks"]] == ["working", "working"]
        assert sorted(env.fenced) == sorted(
            [
                ("clean-up-auth-tokens-tokens", "auth/tokens*", "green"),
                ("clean-up-auth-tokens-sessions", "auth/session*", "green"),
            ]
        )
        assert all(t["fenced"] for t in r["tasks"])

    def test_merge_back_with_a_conflict_handed_to_the_lead_then_release(self, env):
        rid, lead = _approved(env)
        _step(rid)
        r = _step(rid)
        t1, t2 = [t["title"] for t in r["tasks"]]
        _lead_idle(env, lead)
        # Both pieces touch auth/session.py: the second one to merge conflicts.
        _commit(env, t1, "auth/tokens.py", "T = 2\n", "auth: rotate refresh tokens")
        _commit(env, t1, "auth/session.py", "S = 'tokens'\n", "auth: token sessions")
        p2 = _commit(env, t2, "auth/session.py", "S = 'store'\n", "auth: session store")
        _done(env, t1, "Rotated the refresh tokens.")
        _done(env, t2, "Put sessions behind a store.")
        r = _step(rid)
        assert [t["state"] for t in r["tasks"]] == ["integrating", "integrating"]
        r = _step(rid)  # merges t1
        r = _step(rid)  # t1 verified merged back; t2 conflicts → handed to the lead
        a, b = r["tasks"]
        assert a["state"] == "integrated" and a["conflict_fixed"] is False
        assert a["commits"] == ["auth: rotate refresh tokens", "auth: token sessions"]
        assert a["tests"] == "pytest — 3 passed"
        assert b["state"] == "integrating" and b["reason"] == "conflict"
        assert b["conflict"]["files"] == ["auth/session.py"]
        assert "is resolving conflicts in auth/session.py" in b["detail"]
        ((to, text),) = env.messages
        assert to == lead and "`auth/session.py`" in text and 'task_id="t2"' in text
        wt = _wt(env, lead)
        assert not gm.merge_in_progress(wt) and gm.tracked_dirty(wt) is False
        # Nothing else merges while the conflict is the lead's.
        assert _step(rid)["tasks"][1]["state"] == "integrating"
        # The lead resolves it by hand...
        _git(wt, "merge", "--no-ff", "mf/" + t2, check=False)
        with open(os.path.join(wt, "auth/session.py"), "w") as f:
            f.write("S = 'tokens+store'\n")
        _git(wt, "commit", "-qam", "Merge sessions: keep both")
        resolved = gm.rev_parse(wt, "HEAD")
        wrong = gm.rev_parse(wt, "HEAD~1")
        assert drv.report_integrated(rid, "t2", wrong, lead) == {
            "ok": True,
            "verified": False,
        }
        assert tr.load(rid)["tasks"][1]["state"] == "integrating"
        with pytest.raises(drv.RunError) as err:
            drv.report_integrated(rid, "t2", resolved, "someone-else")
        assert err.value.status == 409
        assert drv.report_integrated(rid, "t2", resolved, lead) == {
            "ok": True,
            "verified": True,
        }
        r = tr.load(rid)
        assert r["tasks"][1]["state"] == "integrated"
        assert r["tasks"][1]["conflict_fixed"] is True
        assert r["tasks"][1]["head_sha"] == p2
        # The check on the merged branch (a real one, from .mindflock.toml).
        with open(os.path.join(wt, ".mindflock.toml"), "w") as f:
            f.write('[workspace]\ncheck_command = "echo 7 passed"\n')
        r = _step(rid)
        assert r["state"] == "checking" and r["check"]["state"] == "running"
        assert r["check"]["command"] == "echo 7 passed"
        for _ in range(100):
            if (server._wt_setup.check_status(wt) or {}).get("state") == "ok":
                break
            time.sleep(0.05)
        r = _step(rid)
        assert r["state"] == "release_ready"
        assert r["check"]["state"] == "ok" and r["check"]["tests"] == 7
        rel = r["release"]
        assert rel["state"] == "ready" and rel["base"] == "main"
        assert rel["branch"] == "mf/" + lead
        assert rel["title"] == ("Auth cleanup: rotate refresh tokens, session store")
        assert rel["files"] == 2 and rel["conflict_fixes"] == 1
        assert "## tokens" in rel["body"] and "## sessions" in rel["body"]
        assert "Rotated the refresh tokens." in rel["body"]
        assert "- sessions: resolved by %s (auth/session.py)" % lead in rel["body"]
        assert "`echo 7 passed` passed (7 tests)" in rel["body"]
        # Announced once: the plan, then the release.
        _step(rid)
        asks = [e["new"] for e in _events(env, "run.needs_you")]
        assert asks == ["plan", "release"] or asks == ["release"]
        assert asks.count("release") == 1
        # Your click: the lead's lane, with the PR title and body.
        summary = drv.release(rid, merge_when_green=False)
        assert summary["state"] == "releasing"
        rec = ap.get(lead)
        assert rec["lane"] == "pr" and rec["depth"] == "pr"
        assert rec["pr_title"] == rel["title"] and rec["pr_body"] == rel["body"]
        with pytest.raises(drv.RunError):
            drv.release(rid)
        ap.update(lead, state="done", url="https://example.test/pull/7")
        r = _step(rid)
        assert r["state"] == "done"
        assert r["release"]["state"] == "done"
        assert r["release"]["pr_url"] == "https://example.test/pull/7"
        assert "One PR: https://example.test/pull/7" in r["summary"]["text_md"]
        assert len(_events(env, "run.finished")) == 1
        # Pieces are never announced as shipped on their own.
        assert _events(env, "run.task_shipped") == []

    def test_merge_when_green_arms_merge(self, env):
        rid, lead = _approved(env)
        head = gm.rev_parse(_wt(env, lead), "HEAD")
        with tr.edit(rid) as r:
            for t in r["tasks"]:
                t["state"] = "integrated"
            r["state"] = "release_ready"
            r["release"] = tr._normalize_release(
                {"state": "ready", "title": "T", "body": "B", "head_sha": head}
            )
        drv.release(rid, merge_when_green=True)
        assert ap.get(lead)["lane"] == "merge"

    def test_restart_mid_integration_verifies_and_re_merges_silently(self, env):
        rid, lead = _approved(env)
        _step(rid)
        r = _step(rid)
        t1, t2 = [t["title"] for t in r["tasks"]]
        _lead_idle(env, lead)
        _commit(env, t1, "auth/tokens.py", "T = 2\n", "auth: tokens")
        _commit(env, t2, "auth/session.py", "S = 2\n", "auth: sessions")
        _done(env, t1)
        _done(env, t2)
        _step(rid)
        # The server "dies" right after merging t1 by hand-equivalent: t1 is
        # in the lead's history, t2 is not, and the record still says
        # integrating for both.
        gm.merge_into(_wt(env, lead), "mf/" + t1)
        before = len(env.emitted)
        r = _step(rid, boot=True)
        assert r["tasks"][0]["state"] == "integrated"
        assert [e for e in env.emitted[before:] if e["event"].startswith("run.")] == []
        # t2 is merged by the boot pass's merge effect or the next pass —
        # once, not twice.
        for _ in range(3):
            r = _step(rid)
        assert [t["state"] for t in r["tasks"]] == ["integrated", "integrated"]
        merges = _git(_wt(env, lead), "log", "--merges", "--format=%s").stdout
        assert merges.count(t2) == 1

    def test_a_retry_on_a_conflict_puts_it_back_in_the_merge_queue(self, env):
        rid, _lead = _approved(env)
        with tr.edit(rid) as r:
            t = r["tasks"][0]
            t.update(state="needs_you", reason="conflict", detail="x")
            t["conflict"] = {"files": ["a"], "attempts": 2, "at": 1.0}
        task = drv.retry(rid, "t1")
        assert task["state"] == "integrating" and task["conflict"] is None

    def test_plan_reject_goes_back_to_planning_and_tells_the_lead(self, env):
        run = _split(env)
        lead = run["lead"]["title"]
        drv.propose_plan(run["id"], {"pieces": PLAN, "from": lead})
        dto = asyncio.run(drv.reject_plan(run["id"], "fewer pieces"))
        assert dto["state"] == "planning" and dto["plan"]["note"] == "fewer pieces"
        ((to, text),) = env.messages
        assert to == lead and "fewer pieces. Propose again" in text
        with pytest.raises(drv.RunError):
            drv.approve_plan(run["id"])

    def test_split_from_an_existing_session_briefs_it_through_the_queue(self, env):
        from backend.web.core import prompt_queue as pq

        lead_path = str(env.tmp / "wt" / "mine")
        _git(env.repo, "worktree", "add", "-q", "-b", "me/mine", lead_path, "main")
        env.instances["mine"] = _WtInst("mine", lead_path, "me/mine", env.repo)
        run = _split(env, repo_path="", lead="mine")
        assert run["lead"]["title"] == "mine" and run["lead"]["adopted"] is True
        assert run["repo_root"] == env.repo
        assert env.created == []
        (item,) = pq.snapshot()["mine"]["items"]
        assert "Split this task into parallel pieces" in item["text"]
        # A session sitting on its own base branch PLANS the split now (the
        # plan card then offers separate worktrees under a new lead, or its
        # folder once it is on a branch) — its brief says to commit nothing
        # on the trunk, and its own lane is left alone.
        on_base = str(env.tmp / "wt" / "trunk")
        _git(env.repo, "worktree", "add", "-q", "-b", "trunkish", on_base, "main")
        env.instances["trunk"] = _WtInst(
            "trunk", on_base, "trunkish", env.repo, base_branch="trunkish"
        )
        trunk_run = _split(env, repo_path="", lead="trunk")
        assert trunk_run["lead"]["trunk"] is True
        assert trunk_run["lead"]["in_place"] is False
        (item,) = pq.snapshot()["trunk"]["items"]
        assert "You are on trunkish, the trunk: commit nothing here" in item["text"]
        # Never one already leading, or an unknown one.
        for title, status, words in (
            ("mine", 409, "already in group"),
            ("nope", 404, "instance not found"),
        ):
            with pytest.raises(drv.RunError) as err:
                _split(env, repo_path="", lead=title)
            assert err.value.status == status and words in err.value.message


class TestOneForAll:
    def test_a_batch_starts_its_lines_as_workers_of_the_lead(self, env):
        payload = {
            "name": "Q4",
            "items": [
                {"kind": "task", "text": "first line"},
                {"kind": "task", "text": "second line"},
                {"kind": "task", "text": "third line"},
            ],
            "policy": {"lane": "pr", "grouping": "together"},
            "repo_path": env.repo,
            "concurrency": 3,
        }
        run, _w = asyncio.run(drv.create_run(payload))
        assert run["state"] == "running" and run["lead"]["title"] == "q4-lead"
        assert "integration session" in env.created[0]["prompt"]
        r = _step(run["id"])
        assert [t["state"] for t in r["tasks"]] == ["starting"] * 3
        lead_head = gm.rev_parse(_wt(env, "q4-lead"), "HEAD")
        for p in env.created[1:]:
            assert p["parent"] == "q4-lead" and p["base_ref"] == lead_head
            assert "one branch for a single PR" in p["prompt"]
        dto = tr.run_dto(tr.load(run["id"]))
        assert all(tr.task_lane(r, t) == "commit" for t in r["tasks"])
        assert dto["lead"]["title"] == "q4-lead"

    def test_one_for_all_is_capped_by_the_children_limit(self, env, monkeypatch):
        monkeypatch.setenv("MINDFLOCK_MAX_CHILDREN", "2")
        with pytest.raises(drv.RunError) as err:
            asyncio.run(
                drv.create_run(
                    {
                        "items": [
                            {"kind": "task", "text": "l%d" % i} for i in range(3)
                        ],
                        "policy": {"grouping": "together"},
                        "repo_path": env.repo,
                    }
                )
            )
        assert "at most 2 lines" in err.value.message


class TestOutboxRows:
    def _run(self, state, **kw):
        base = {
            "id": "r_ob1",
            "name": "Auth",
            "state": state,
            "split": True,
            "policy": {"lane": "pr", "grouping": "together", "release": "ask"},
            "lead": {"title": "auth-lead", "branch": "mf/auth-lead"},
            "plan": {
                "state": "proposed",
                "pieces": [{"title": "a", "prompt": "x", "paths": ["a/**"]}],
            },
            "tasks": [],
        }
        base.update(kw)
        return tr._normalize(base)

    def _build(self, runs, rows=(), autopilot=None):
        return outbox_mod.build(
            rows, runs, autopilot or {}, now=time.time(), today_start=0.0
        )

    def test_a_plan_waits_on_you_with_its_pieces(self):
        out = self._build([self._run("plan_ready")])
        (item,) = out["groups"]["waiting"]
        assert item["kind"] == "plan" and item["title"] == "auth-lead"
        assert item["actions"] == ["approve", "open"]
        assert item["preview"] == {"pieces": [{"title": "a", "paths": ["a/**"]}]}

    def test_the_release_waits_on_you(self):
        run = self._run(
            "release_ready",
            release={
                "state": "ready",
                "title": "Auth: x",
                "branch": "mf/auth-lead",
                "files": 3,
            },
            tasks=[
                {"id": "t1", "kind": "piece", "title": "auth-a", "state": "integrated"}
            ],
        )
        (item,) = self._build([run])["groups"]["waiting"]
        assert item["kind"] == "release" and item["actions"] == ["release", "open"]
        assert item["preview"]["pr_title"] == "Auth: x"
        assert "1 merged into mf/auth-lead" in item["reason"]

    def test_a_failing_check_waits_on_you(self):
        run = self._run("checking", check={"state": "failed", "summary": "E boom"})
        (item,) = self._build([run])["groups"]["waiting"]
        assert item["kind"] == "check_failed"
        assert item["actions"] == ["open", "retry_check"]

    def test_merging_members_show_as_shipping_and_pieces_never_ship_alone(self):
        run = self._run(
            "running",
            tasks=[
                {
                    "id": "t1",
                    "kind": "piece",
                    "title": "auth-a",
                    "state": "integrating",
                },
                {
                    "id": "t2",
                    "kind": "piece",
                    "title": "auth-b",
                    "state": "integrated",
                    "finished_at": time.time(),
                },
            ],
        )
        rows = [{"title": "auth-b", "repo": "r", "branch": "mf/auth-b"}]
        aprec = {"auth-b": {"state": "done", "step": "commit", "updated": time.time()}}
        out = self._build([run], rows=rows, autopilot=aprec)
        (ship,) = out["groups"]["shipping"]
        assert (
            ship["step"] == "integrate"
            and ship["note"] == "merging back into auth-lead"
        )
        assert out["groups"]["shipped"] == []

    def test_a_conflict_offers_retry(self):
        assert outbox_mod.WAITING_ACTIONS["conflict"] == ["retry", "open", "skip"]
