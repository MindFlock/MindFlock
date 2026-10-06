"""Split modes, driven against REAL git checkouts.

Two things a split could not do before:

* **A lead that works directly in its folder (in place), or sits on its
  trunk**, could not split at all. Now it PLANS, and "separate worktrees"
  (the default) gives the pieces a NEW lead — a plain worktree on a fresh
  branch cut from the original's last commit — while the original session,
  its checkout, branch, index and uncommitted files are never touched.
* **"In this folder (no merge)"**: the pieces run as extra agents in the
  lead's own folder, each fenced to its paths PER SESSION (the guard file
  carries a fence per tmux session; the hook overlays its own), and
  MindFlock commits each done piece's paths itself — one commit per piece,
  nothing else in it — with plumbing that never touches the tree. A piece
  that commits by itself is detected (kept when it is only its own files,
  escalated when it mixes), a change no piece owns is said, a trunk folder
  is refused until you start a branch there, and a restart reads the truth
  back from the branch's history (the piece trailer).

The engine is faked at the seam the driver uses (``session_create.
create_result``) exactly as the split driver tests do; every checkout,
commit, guard file and hook run is real.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from backend.config import red_zones as rz
from backend.providers import activity_markers as am
from backend.session.storage import Status
from backend.session.tmux import tmux as tmux_mod
from backend.web import server
from backend.web.core import autopilot as ap
from backend.web.core import events as events_mod
from backend.web.core import git_merge as gm
from backend.web.core import mailbox as mailbox_mod
from backend.web.core import team_run_driver as drv
from backend.web.core import team_runs as tr


def _git(path, *args, check=True):
    return subprocess.run(
        ["git", "-C", str(path), *args], check=check, capture_output=True, text=True
    )


class _Inst:
    def __init__(
        self, title, path, branch, repo, base_branch="main", parent="", in_place=False
    ):
        self.Title = title
        self.Program = "claude"
        self.Branch = branch
        self.Path = repo
        self.InPlace = in_place
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
    """A repository whose MAIN CHECKOUT is the user's own folder (an in-place
    session works there), plus the engine seam."""
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "t@example.test"),
        ("config", "user.name", "t"),
    ):
        _git(repo, *args)
    os.makedirs(repo / "auth")
    for rel, text in (
        ("auth/tokens.py", "T = 1\n"),
        ("auth/session.py", "S = 1\n"),
        ("README.md", "readme\n"),
    ):
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
        if payload.get("in_place"):
            folder = payload["repo_path"]
            branch = gm.current_branch(folder)
            inst = _Inst(
                title,
                folder,
                branch,
                folder,
                parent=payload.get("parent") or "",
                in_place=True,
            )
        else:
            branch = "mf/" + title
            path = str(tmp_path / "wt" / title)
            _git(
                payload["repo_path"],
                "worktree",
                "add",
                "-q",
                "-b",
                branch,
                path,
                payload.get("base_ref") or "main",
            )
            inst = _Inst(
                title,
                path,
                branch,
                payload["repo_path"],
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


def _write(folder, rel, text):
    p = os.path.join(folder, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write(text)


def _idle(env, title, report=None, since=None):
    now = time.time()
    row = {
        "title": title,
        "activity": "idle",
        "activity_since": since if since is not None else now - 120,
        "repo": "repo",
    }
    if report:
        row["last_report"] = {"status": "done", "summary": report, "ts": now}
        env.reports[title] = {
            "text": "%s\n\nDetails:\nTests: pytest — 2 passed" % report
        }
    env.rows[title] = row


def _working(env, title):
    env.rows[title] = {
        "title": title,
        "activity": "working",
        "activity_since": time.time(),
        "repo": "repo",
    }


PLAN = [
    {"title": "tokens", "prompt": "Rotate the tokens.", "paths": ["auth/tokens*"]},
    {"title": "sessions", "prompt": "Session store.", "paths": ["auth/session*"]},
]


def _split_of(env, lead):
    payload = {
        "name": "Auth cleanup",
        "items": [{"kind": "task", "text": "Clean up auth: tokens and sessions"}],
        "policy": {"lane": "pr", "release": "ask"},
        "split": True,
        "lead": lead,
    }
    run, _w = asyncio.run(drv.create_run(payload))
    drv.propose_plan(run["id"], {"pieces": PLAN, "why": "two seams", "from": lead})
    return run["id"]


def _snapshot(folder):
    """What a person would see of their own checkout."""
    return {
        "branch": gm.current_branch(folder),
        "head": gm.rev_parse(folder, "HEAD"),
        "status": _git(folder, "status", "--porcelain=v1", "-uall").stdout,
        "staged": _git(folder, "diff", "--cached").stdout,
        "unstaged": _git(folder, "diff").stdout,
    }


def _in_place(env, title="mine", branch=""):
    """An in-place session in the repository's own checkout."""
    if branch:
        _git(env.repo, "switch", "-q", "-c", branch)
    env.instances[title] = _Inst(
        title, env.repo, gm.current_branch(env.repo), env.repo, in_place=True
    )
    return title


# --------------------------------------------------------------------------- #
# Decision 1 — an in-place / trunk lead gets a lead of its own
# --------------------------------------------------------------------------- #
class TestOwnLeadForSeparateWorktrees:
    def test_split_on_an_in_place_session_is_no_longer_refused(self, env):
        lead = _in_place(env)
        rec = ap.arm("mine", "pr", source="session")
        assert rec is not None
        rid = _split_of(env, lead)
        r = tr.load(rid)
        assert r["state"] == "plan_ready" and r["lead"]["title"] == "mine"
        assert r["lead"]["in_place"] is True and r["lead"]["trunk"] is True
        # It only plans: its own fast-track is left alone (it is never the
        # group's lead under "separate worktrees").
        assert ap.get("mine") is not None
        assert env.created == []

    def test_separate_worktrees_start_a_new_lead_and_never_touch_the_original(
        self, env
    ):
        lead = _in_place(env)
        rid = _split_of(env, lead)
        # Uncommitted tracked work in the original: not in the split, said so.
        _write(env.repo, "README.md", "my own edit\n")
        before = _snapshot(env.repo)
        with pytest.raises(drv.RunError) as err:
            asyncio.run(drv.approve(rid, "worktrees"))
        assert err.value.status == 409 and err.value.extra["code"] == "origin_dirty"
        assert "they would not be in the split" in err.value.message
        assert "commit them first" in err.value.message
        assert env.created == [] and _snapshot(env.repo) == before
        # Committed — but an untracked scratch file and a staged-nothing
        # index stay exactly as they are.
        _git(env.repo, "commit", "-qam", "my own work")
        _write(env.repo, "scratch.txt", "mine, untracked\n")
        before = _snapshot(env.repo)
        head = before["head"]
        dto = asyncio.run(drv.approve(rid, "worktrees"))
        assert dto["mode"] == "worktrees"
        new = dto["lead"]["title"]
        assert new == "mine-split"
        (p,) = env.created
        assert p["title"] == "mine-split" and p["base_ref"] == head
        assert p["repo_path"] == env.repo and "parent" not in p
        assert dto["origin"]["title"] == "mine" and dto["origin"]["head"] == head
        new_wt = _wt(env, new)
        assert gm.rev_parse(new_wt, "HEAD") == head
        assert gm.current_branch(new_wt) == "mf/mine-split"
        # The pieces fork from the NEW lead.
        r = _step(rid)
        for piece in env.created[1:]:
            assert piece["parent"] == new and piece["base_ref"] == head
            assert piece["base_branch"] == "mf/mine-split"
        r = _step(rid)
        t1, t2 = [t["title"] for t in r["tasks"]]
        assert t1 == "mine-tokens" and t2 == "mine-sessions"
        for title, rel, text in (
            (t1, "auth/tokens.py", "T = 2\n"),
            (t2, "auth/session.py", "S = 2\n"),
        ):
            _write(_wt(env, title), rel, text)
            _git(_wt(env, title), "commit", "-qam", "piece " + title)
            _idle(env, title, "did " + title)
        _idle(env, new)
        for _ in range(6):
            r = _step(rid)
        assert [t["state"] for t in r["tasks"]] == ["integrated", "integrated"]
        assert r["state"] == "release_ready"
        log = _git(new_wt, "log", "--format=%s", "main..HEAD").stdout
        assert "piece mine-tokens" in log and "piece mine-sessions" in log
        # The release ships the NEW lead's branch — the original is untouched:
        # its branch, HEAD, index, worktree and the untracked file.
        assert r["release"]["branch"] == "mf/mine-split"
        drv.release(rid)
        assert ap.get("mine-split")["lane"] == "pr"
        assert _snapshot(env.repo) == before
        assert os.path.exists(os.path.join(env.repo, "scratch.txt"))
        assert gm.rev_parse(env.repo, "main") == head
        listed = _git(env.repo, "worktree", "list", "--porcelain").stdout
        assert "branch refs/heads/main" in listed  # still on its own branch

    def test_a_trunk_worktree_lead_gets_its_own_lead_too(self, env):
        path = str(env.tmp / "wt" / "onmain")
        _git(env.repo, "worktree", "add", "-q", "-b", "trunkish", path, "main")
        env.instances["onmain"] = _Inst(
            "onmain", path, "trunkish", env.repo, base_branch="trunkish"
        )
        rid = _split_of(env, "onmain")
        assert tr.load(rid)["lead"]["trunk"] is True
        before = _snapshot(path)
        dto = asyncio.run(drv.approve(rid, ""))  # the default IS worktrees
        assert dto["mode"] == "worktrees" and dto["lead"]["title"] == "onmain-split"
        assert dto["origin"] == {
            "title": "onmain",
            "branch": "trunkish",
            "head": before["head"],
            "in_place": False,
            "trunk": True,
        }
        (p,) = env.created
        assert p["base_branch"] == "trunkish"
        assert _snapshot(path) == before

    def test_the_sync_approve_still_refuses_an_in_place_lead(self, env):
        """Approving straight through ``approve_plan`` (no lead of its own
        started) never merges into an in-place lead."""
        lead = _in_place(env, branch="feat")
        rid = _split_of(env, lead)
        with pytest.raises(drv.RunError) as err:
            drv.approve_plan(rid, "worktrees")
        assert err.value.extra["code"] == "needs_lead"

    def test_a_restart_mid_run_keeps_the_new_lead(self, env):
        lead = _in_place(env)
        rid = _split_of(env, lead)
        asyncio.run(drv.approve(rid, "worktrees"))
        _step(rid)
        n = len(env.created)
        r = _step(rid, boot=True)
        assert len(env.created) == n  # nothing re-created
        assert r["lead"]["title"] == "mine-split" and r["origin"]["title"] == "mine"
        assert [t["state"] for t in r["tasks"]] == ["working", "working"]


# --------------------------------------------------------------------------- #
# Decision 2 — in this folder (no merge)
# --------------------------------------------------------------------------- #
def _sf_running(env, branch="feat"):
    lead = _in_place(env, branch=branch)
    rid = _split_of(env, lead)
    dto = asyncio.run(drv.approve(rid, "same_folder"))
    assert dto["mode"] == "same_folder"
    r = _step(rid)  # starts both pieces
    r = _step(rid)  # working + fenced
    return rid, lead, r


def _trailer_commits(folder, base):
    out = []
    for c in gm.commits_since(folder, base) or []:
        out.append((c["subject"], sorted(c["files"]), c["body"]))
    return out


class TestSameFolder:
    def test_trunk_is_refused_until_you_start_a_branch_there(self, env):
        lead = _in_place(env)  # on main
        rid = _split_of(env, lead)
        with pytest.raises(drv.RunError) as err:
            asyncio.run(drv.approve(rid, "same_folder"))
        assert err.value.status == 409 and err.value.extra["code"] == "trunk"
        assert "Start a branch here first" in err.value.message
        assert gm.current_branch(env.repo) == "main" and env.created == []
        dto = drv.lead_branch(rid)
        branch = dto["lead"]["branch"]
        assert branch.endswith("mine-split") and gm.current_branch(env.repo) == branch
        assert dto["lead"]["trunk"] is False
        with pytest.raises(drv.RunError) as err:
            drv.lead_branch(rid)  # already on its own branch
        assert err.value.status == 409
        dto = asyncio.run(drv.approve(rid, "same_folder"))
        assert dto["state"] == "running" and dto["lead"]["branch"] == branch

    def test_pieces_run_in_the_lead_folder_and_are_fenced_per_session(self, env):
        rid, lead, r = _sf_running(env)
        assert [t["state"] for t in r["tasks"]] == ["working", "working"]
        pieces = env.created
        assert len(pieces) == 2
        for p in pieces:
            assert p["in_place"] is True and p["repo_path"] == env.repo
            assert p["parent"] == lead and p["spawned"] is True
            assert "base_ref" not in p
            assert "Don't run git add, commit" in p["prompt"]
            assert "Other pieces work in this same folder" in p["prompt"]
        # No autopilot drives a same-folder piece (it would commit them all).
        assert all(ap.get(t["title"]) is None for t in r["tasks"])
        # Their own lane is off: the group ships the lead once.
        assert ap.get(lead) is None
        # One fence PER SESSION in the shared folder — never the folder's
        # green zones (which would fence everyone to the union).
        assert env.fenced == []
        fences = rz.session_fences(env.repo)
        keys = {tmux_mod.to_mindflock_tmux_name(t["title"]) for t in r["tasks"]}
        assert set(fences) == keys
        guard = json.load(open(rz.guard_path(os.path.realpath(env.repo))))
        assert set(guard["sessions"]) == keys
        assert guard["green_rules"] == []  # the lead and you are not fenced
        assert all(t["fenced"] for t in r["tasks"])

    def test_one_commit_per_piece_with_only_its_paths_then_release(self, env):
        rid, lead, r = _sf_running(env)
        base = r["sf"]["base"]
        t1, t2 = [t["title"] for t in r["tasks"]]
        # Both pieces edit at once; a new file too; the lead's folder also
        # has a person's untracked scratch file from before.
        _write(env.repo, "auth/tokens.py", "T = 2\n")
        _write(env.repo, "auth/tokens_new.py", "N = 1\n")
        _write(env.repo, "auth/session.py", "S = 2\n")
        _idle(env, t1, "Rotated the tokens.")
        _working(env, t2)
        r = _step(rid)
        assert [t["state"] for t in r["tasks"]] == ["integrating", "working"]
        r = _step(rid)  # the commit
        r = _step(rid)  # verified by its trailer
        assert r["tasks"][0]["state"] == "integrated"
        commits = _trailer_commits(env.repo, base)
        assert len(commits) == 1
        subject, files, body = commits[0]
        assert subject == "Rotated the tokens."
        assert files == ["auth/tokens.py", "auth/tokens_new.py"]
        assert "MindFlock-Piece: %s/t1" % rid in body
        # The other piece's work is still in the tree, uncommitted, untouched.
        assert gm.changed_paths(env.repo) == ["auth/session.py"]
        assert open(os.path.join(env.repo, "auth/session.py")).read() == "S = 2\n"
        # Its fence is gone; the other one's stays.
        r = _step(rid)
        assert set(rz.session_fences(env.repo)) == {tmux_mod.to_mindflock_tmux_name(t2)}
        _idle(env, t2, "Put sessions behind a store.")
        _idle(env, lead)
        for _ in range(5):
            r = _step(rid)
        assert [t["state"] for t in r["tasks"]] == ["integrated", "integrated"]
        commits = _trailer_commits(env.repo, base)
        assert [c[1] for c in commits] == [
            ["auth/tokens.py", "auth/tokens_new.py"],
            ["auth/session.py"],
        ]
        assert r["state"] == "release_ready"
        rel = r["release"]
        assert rel["branch"] == "feat" and rel["commits"] == 2
        assert gm.changed_paths(env.repo) == []
        drv.release(rid)
        assert ap.get(lead)["lane"] == "pr"
        assert rz.session_fences(env.repo) == {}

    def test_a_piece_that_commits_by_itself_is_kept_or_escalated(self, env):
        rid, lead, r = _sf_running(env)
        t1, t2 = [t["title"] for t in r["tasks"]]
        # t1 commits its own files itself (against its brief): kept as its own.
        _write(env.repo, "auth/tokens.py", "T = 3\n")
        _git(env.repo, "commit", "-qm", "tokens by hand", "--", "auth/tokens.py")
        r = _step(rid)
        own = r["tasks"][0]
        assert len(own["self_commits"]) == 1 and own["state"] == "working"
        assert any("committed by itself" in e["text"] for e in r["events"])
        _idle(env, t1, "Rotated the tokens.")
        for _ in range(3):
            r = _step(rid)
        assert r["tasks"][0]["state"] == "integrated"
        assert r["tasks"][0]["commits"] == ["tokens by hand"]
        # A commit that mixes t2's files with a file no piece owns can't be
        # split per piece: t2 needs you, and it is said once.
        _write(env.repo, "auth/session.py", "S = 3\n")
        _write(env.repo, "README.md", "edited\n")
        _git(env.repo, "commit", "-qam", "everything at once")
        r = _step(rid)
        t = r["tasks"][1]
        assert t["state"] == "needs_you" and t["reason"] == "blocked"
        assert "can't be split per piece" in t["detail"]
        assert "README.md" in t["detail"]
        assert _step(rid)["tasks"][1]["state"] == "needs_you"
        assert len(r["sf"]["seen_foreign"]) == 2

    def test_a_change_no_piece_owns_is_said_and_blocks_the_release(self, env):
        rid, lead, r = _sf_running(env)
        _write(env.repo, "README.md", "who did this\n")
        r = _step(rid)
        assert r["sf"]["stray"] == ["README.md"]
        ask = drv._run_ask(r)
        assert ask[1] == "stray" and "README.md" in ask[2]
        dto = tr.run_dto(r)
        assert dto["stray"]["paths"] == ["README.md"]
        # Nothing of it lands in a piece's commit.
        t1, t2 = [t["title"] for t in r["tasks"]]
        _write(env.repo, "auth/tokens.py", "T = 4\n")
        _write(env.repo, "auth/session.py", "S = 4\n")
        _idle(env, t1, "tokens")
        _idle(env, t2, "sessions")
        _idle(env, lead)
        for _ in range(6):
            r = _step(rid)
        files = [c[1] for c in _trailer_commits(env.repo, r["sf"]["base"])]
        assert files == [["auth/tokens.py"], ["auth/session.py"]]
        assert gm.changed_paths(env.repo) == ["README.md"]
        assert r["state"] == "release_ready"
        with pytest.raises(drv.RunError) as err:
            drv.release(rid)
        assert "uncommitted changes" in err.value.message

    def test_done_with_nothing_under_its_paths_is_said_not_committed(self, env):
        rid, _lead, r = _sf_running(env)
        _idle(env, r["tasks"][0]["title"], "done, I think")
        r = _step(rid)
        t = r["tasks"][0]
        assert t["state"] == "needs_you" and "nothing under its paths" in t["detail"]
        assert gm.commits_since(env.repo, r["sf"]["base"]) == []

    def test_a_restart_mid_run_reads_the_commit_back_from_history(self, env):
        rid, lead, r = _sf_running(env)
        t1 = r["tasks"][0]["title"]
        _write(env.repo, "auth/tokens.py", "T = 5\n")
        _idle(env, t1, "tokens")
        r = _step(rid)
        assert r["tasks"][0]["state"] == "integrating"
        # The server commits, then "dies" before it re-read the index or
        # recorded anything: simulate by committing through the very same
        # plumbing and putting the index back the way it was.
        drv._sf_commit(rid, "t1", time.time())
        _git(
            env.repo,
            "update-index",
            "--cacheinfo",
            "100644,%s,auth/tokens.py"
            % (_git(env.repo, "rev-parse", "HEAD~1:auth/tokens.py").stdout.strip()),
        )
        assert gm.tracked_dirty(env.repo) is True  # a stale index entry
        n = len(gm.commits_since(env.repo, r["sf"]["base"]))
        r = _step(rid, boot=True)
        assert r["tasks"][0]["state"] == "integrated"
        assert len(gm.commits_since(env.repo, r["sf"]["base"])) == n == 1
        assert gm.tracked_dirty(env.repo) is False  # healed, nothing lost
        # The fences survived the restart (they live in the zone store).
        assert tmux_mod.to_mindflock_tmux_name(r["tasks"][1]["title"]) in (
            rz.session_fences(env.repo)
        )

    def test_cancel_drops_every_fence_it_set(self, env):
        rid, _lead, _r = _sf_running(env)
        assert rz.session_fences(env.repo)
        drv.cancel(rid)
        assert rz.session_fences(env.repo) == {}


# --------------------------------------------------------------------------- #
# Per-session fencing in a shared folder — the REAL hook command
# --------------------------------------------------------------------------- #
@pytest.fixture
def shared(tmp_path):
    repo = tmp_path / "shared"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "feat")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    for rel in ("a/x.py", "b/y.py", "c/z.py"):
        _write(str(repo), rel, "1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    root = os.path.realpath(str(repo))
    rz.set_session_fence(str(repo), "mindflock_p1", ["a/**"], owner="run:r_t")
    rz.set_session_fence(str(repo), "mindflock_p2", ["b/**"], owner="run:r_t")
    rz.sync_guard(root, lroot=str(repo))
    base_env = {
        **os.environ,
        "MINDFLOCK_ACTIVITY_MARKER_DIR": str(tmp_path / "markers"),
        "MINDFLOCK_THREAD_MARKER_DIR": str(tmp_path / "threads"),
    }
    base_env.pop("TMUX_PANE", None)
    return str(repo), base_env


def _hook(payload, ev, env, session):
    cmd = am.hook_command("working", tool_hook=ev)
    cp = subprocess.run(
        ["sh", "-c", cmd],
        input=json.dumps(payload).encode(),
        capture_output=True,
        env=dict(env, MINDFLOCK_SESSION_NAME=session),
    )
    out = cp.stdout.decode().strip()
    return json.loads(out) if out else None


def _write_payload(repo, rel, tuid="tu"):
    return {
        "session_id": "sid",
        "cwd": repo,
        "tool_name": "Write",
        "tool_input": {"file_path": os.path.join(repo, rel)},
        "tool_use_id": tuid,
    }


def _bash_payload(repo, cmd, tuid):
    return {
        "session_id": "sid",
        "cwd": repo,
        "tool_name": "Bash",
        "tool_input": {"command": cmd},
        "tool_use_id": tuid,
    }


def _denied(out):
    return bool(out) and (
        out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"
    )


class TestPerSessionFence:
    def test_each_session_is_fenced_to_its_own_paths(self, shared):
        repo, env = shared
        p1, p2, lead = "mindflock_p1", "mindflock_p2", "mindflock_lead"
        assert not _denied(_hook(_write_payload(repo, "a/x.py"), "pre", env, p1))
        assert _denied(_hook(_write_payload(repo, "b/y.py"), "pre", env, p1))
        assert _denied(_hook(_write_payload(repo, "c/z.py"), "pre", env, p1))
        assert not _denied(_hook(_write_payload(repo, "b/y.py"), "pre", env, p2))
        assert _denied(_hook(_write_payload(repo, "a/x.py"), "pre", env, p2))
        # A session with no fence of its own (the lead, a person's window)
        # sees the folder unfenced.
        for rel in ("a/x.py", "b/y.py", "c/z.py"):
            assert not _denied(_hook(_write_payload(repo, rel), "pre", env, lead))

    def test_a_fenced_piece_may_not_commit_or_stage(self, shared):
        repo, env = shared
        for cmd in (
            "git commit -am wip",
            "git add a/x.py",
            "cd a && git stash",
            "sh -c 'git -C .. reset --hard'",
        ):
            out = _hook(_bash_payload(repo, cmd, "tb"), "pre", env, "mindflock_p1")
            assert _denied(out), cmd
            reason = out["hookSpecificOutput"]["permissionDecisionReason"]
            assert "MindFlock commits exactly your paths" in reason
        for cmd in ("git status", "git diff a/x.py", "pytest -q"):
            out = _hook(_bash_payload(repo, cmd, "tc"), "pre", env, "mindflock_p1")
            assert not _denied(out), cmd
        # The lead is never refused git.
        out = _hook(_bash_payload(repo, "git commit -am x", "td"), "pre", env, "x")
        assert not _denied(out)

    def test_the_backstop_never_blames_a_piece_for_a_sibling_s_paths(self, shared):
        repo, env = shared
        p1 = "mindflock_p1"
        pre = _bash_payload(repo, "python3 -c 'print(1)'", "tu-s1")
        assert _hook(pre, "pre", env, p1) is None
        _write(repo, "b/y.py", "sibling\n")  # p2, working at the same time
        assert _hook(pre, "post", env, p1) is None
        pre = _bash_payload(repo, "python3 -c 'print(2)'", "tu-s2")
        _hook(pre, "pre", env, p1)
        _write(repo, "c/z.py", "nobody's\n")  # outside every piece
        out = _hook(pre, "post", env, p1)
        assert out and out.get("decision") == "block" and "c/z.py" in out["reason"]

    def test_dropping_a_fence_frees_that_session_only(self, shared):
        repo, env = shared
        rz.drop_session_fence(repo, "mindflock_p1")
        rz.sync_guard(os.path.realpath(repo), lroot=repo)
        assert not _denied(
            _hook(_write_payload(repo, "c/z.py"), "pre", env, "mindflock_p1")
        )
        assert _denied(
            _hook(_write_payload(repo, "c/z.py"), "pre", env, "mindflock_p2")
        )
        assert rz.drop_session_fence(repo, owner="run:r_t") == 1
        rz.sync_guard(os.path.realpath(repo), lroot=repo)
        guard = json.load(open(rz.guard_path(os.path.realpath(repo))))
        assert "sessions" not in guard
