"""A split led by a TICKET session — the lead's repository is its worktree's.

Ticket sessions (the ingestion pipeline, Intake "Begin work") are created
with ``path="."``: their ``Path`` is the SERVER's cwd, and their worktree is a
provisioned one — a worktree of MindFlock's ``_base_<repo>`` clone, or a clone
of its own. "Split into parallel pieces…" on such a session once took its
``Path`` as the pieces' repository: each piece was asked to fork the lead's
HEAD out of a repository that has never seen that commit, and failed.

Everything here is real git: a "source" checkout, a base clone of it with
the lead's worktree on ``feature/sc-1/x`` (or a clone-strategy lead), and a
DIFFERENT repository as the lead's ``Path`` (the server's cwd is usually a
git repo itself — MindFlock's own). The session create is faked at the seam
the driver uses, but it makes the same repository-sensitive checks the real
route does (``_prepare_plain_repo``, ``base_ref_error``, ``branch_taken_error``)
before cutting a real ``git worktree`` from ``repo_path`` at ``base_ref``.

Covers: approval → workers created in the lead's repository at the lead's
HEAD → merged back into the ticket branch → release (a forge origin arms the
PR; a FOLDER origin pushes only and ends in a hand-off that says where the
branch went, never a PR); a clone-strategy lead; a one-for-all ticket line
matched against the ``_base_`` clone; the rail naming a piece by its repo.
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
from backend.web.core import snapshot as snapshot_mod
from backend.web.core import team_run_driver as drv
from backend.web.core import team_runs as tr

FORGE = "https://github.com/acme/app.git"
LEAD = "sc-1-x"
LEAD_BRANCH = "feature/sc-1/x"


def _git(path, *args, check=True):
    return subprocess.run(
        ["git", "-C", str(path), *args], check=check, capture_output=True, text=True
    )


def _init(path, files):
    path.mkdir(parents=True)
    for args in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "t@example.test"),
        ("config", "user.name", "t"),
    ):
        _git(path, *args)
    for rel, text in files.items():
        os.makedirs(os.path.dirname(path / rel), exist_ok=True)
        (path / rel).write_text(text)
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "base")


class _Inst:
    def __init__(self, title, wt, branch, path, parent="", provisioned=False):
        self.Title = title
        self.Program = "claude"
        self.Branch = branch
        self.Path = path
        self.InPlace = False
        self.Provisioned = provisioned
        self.Parent = parent
        self.Spawned = bool(parent)
        self.BaseBranch = "main"
        self.Status = Status.Running
        self.CreatedAt = datetime.fromtimestamp(time.time(), timezone.utc)
        self._wt = wt

    def Started(self):  # noqa: N802
        return True

    def GetWorktreePath(self):  # noqa: N802
        return self._wt


@pytest.fixture
def env(monkeypatch, tmp_path):
    files = {"auth/tokens.py": "T = 1\n", "auth/session.py": "S = 1\n"}
    source = tmp_path / "src" / "app"
    _init(source, files)
    # The server's cwd — a git repository of its own (MindFlock's checkout),
    # which is what a ticket session's Path ("." at create) resolves to.
    cwd = tmp_path / "server-cwd"
    _init(cwd, {"README.md": "the server\n"})
    ws = tmp_path / "workspaces"
    ws.mkdir()

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
    ledger: list = []
    monkeypatch.setattr(
        server._ticket_start, "record_started", lambda *a, **k: ledger.append(a)
    )
    monkeypatch.setattr(
        server._ticket_start, "record_result", lambda *a, **k: ledger.append(a)
    )
    created: list = []
    refused: list = []

    async def _create(payload):
        """The route's repository-sensitive half, for real: the folder is
        prepared, the fork point must be a commit THERE, the branch must be
        free THERE — then a real worktree is cut from it."""
        title = payload["title"]
        if title in instances:
            return 409, {"error": "instance %s already exists" % title}
        try:
            plain, git_ok = server._prepare_plain_repo(payload["repo_path"], False)
        except ValueError as err:
            refused.append(str(err))
            return 400, {"error": str(err)}
        base_ref = payload.get("base_ref") or ""
        err = server._lineage.base_ref_error(
            plain, base_ref, payload.get("base_branch") or ""
        )
        branch = server._session_branch_name(title)
        if not err:
            err = server._lineage.branch_taken_error(plain, branch)
        if err:
            refused.append(err)
            return 400, {"error": err}
        created.append(dict(payload, repo_path=plain))
        path = str(tmp_path / "wt" / title)
        _git(plain, "worktree", "add", "-q", "-b", branch, path, base_ref or "HEAD")
        inst = _Inst(title, path, branch, plain, parent=payload.get("parent") or "")
        instances[title] = inst
        return 202, {
            "title": title,
            "branch": branch,
            "created_at": inst.CreatedAt.timestamp(),
        }

    async def _message(run, text):
        return True

    async def _fence_route(title, payload):
        return SimpleNamespace(status_code=200, body=b"{}")

    reports: dict = {}
    monkeypatch.setattr(server._session_create, "create_result", _create)
    monkeypatch.setattr(drv, "_message_lead", _message)
    monkeypatch.setattr(server, "instance_red_zones_add", _fence_route)
    monkeypatch.setattr(
        mailbox_mod,
        "last_result",
        lambda recipient, sender, since=None: reports.get(sender),
    )
    drv.subscribe()
    yield SimpleNamespace(
        tmp=tmp_path,
        source=str(source),
        cwd=str(cwd),
        ws=ws,
        instances=instances,
        rows=rows,
        created=created,
        refused=refused,
        reports=reports,
        ledger=ledger,
    )
    if drv._UNSUB:
        drv._UNSUB()


def _provisioned_lead(env, origin=FORGE):
    """A ticket session as the pipeline makes it (worktree strategy): a
    worktree of the ``_base_app`` clone on the ticket branch, its Path the
    server's cwd. ``origin`` is the base clone's (the forge, or — no forge
    remote on the source — the source folder itself)."""
    base = env.ws / "_base_app"
    subprocess.run(
        ["git", "clone", "-q", env.source, str(base)], check=True, capture_output=True
    )
    _git(base, "config", "mindflock.source-repo", env.source)
    if origin != env.source:
        _git(base, "remote", "set-url", "origin", origin)
    wt = env.ws / "sc-1-x_abc123"
    _git(base, "worktree", "add", "-q", "-b", LEAD_BRANCH, str(wt), "main")
    for args in (
        ("config", "user.email", "t@example.test"),
        ("config", "user.name", "t"),
    ):
        _git(wt, *args)
    env.instances[LEAD] = _Inst(LEAD, str(wt), LEAD_BRANCH, env.cwd, provisioned=True)
    return str(base), str(wt)


def _clone_lead(env):
    """A clone-strategy ticket session: its own clone IS its repository."""
    wt = env.ws / "sc-1-x"
    subprocess.run(
        ["git", "clone", "-q", env.source, str(wt)], check=True, capture_output=True
    )
    _git(wt, "remote", "set-url", "origin", FORGE)
    _git(wt, "checkout", "-q", "-b", LEAD_BRANCH)
    for args in (
        ("config", "user.email", "t@example.test"),
        ("config", "user.name", "t"),
    ):
        _git(wt, *args)
    env.instances[LEAD] = _Inst(LEAD, str(wt), LEAD_BRANCH, env.cwd, provisioned=True)
    return str(wt), str(wt)


PLAN = [
    {"title": "tokens", "prompt": "Rotate the tokens.", "paths": ["auth/tokens*"]},
    {"title": "sessions", "prompt": "Session store.", "paths": ["auth/session*"]},
]


def _split_on_lead(env):
    """ "Split into parallel pieces…" on the ticket session, exactly as the
    frontend posts it (no repo_path: the lead names the repository)."""
    run, _warnings = asyncio.run(
        drv.create_run(
            {
                "name": "Ticket split",
                "items": [{"kind": "task", "text": "Clean up auth: tokens, sessions"}],
                "policy": {"lane": "pr", "grouping": "together", "release": "ask"},
                "program": "claude",
                "split": True,
                "lead": LEAD,
            }
        )
    )
    return run


def _step(rid, boot=False):
    asyncio.run(drv.step_run(rid, boot=boot))
    return tr.load(rid)


def _commit(wt, rel, text, msg):
    with open(os.path.join(wt, rel), "w") as f:
        f.write(text)
    _git(wt, "add", rel)
    _git(wt, "commit", "-q", "-m", msg)
    return gm.rev_parse(wt, "HEAD")


def _done(env, title, branch, summary):
    now = time.time()
    env.rows[title] = {
        "title": title,
        "activity": "idle",
        "activity_since": now - 60,
        "stage": "committed",
        "repo": "app",
        "branch": branch,
        "last_report": {"status": "done", "summary": summary, "ts": now},
    }
    env.reports[title] = {"text": "%s\n\nDetails:\nTests: pytest — 1 passed" % summary}


def _run_to_release(env, repo, lead_wt):
    """Plan, approve, both workers commit, merged back, check skipped →
    release_ready. Returns the run id and the lead's HEAD the pieces forked
    from."""
    run = _split_on_lead(env)
    rid = run["id"]
    _commit(lead_wt, "auth/groundwork.py", "G = 1\n", "groundwork")
    head = gm.rev_parse(lead_wt, "HEAD")
    drv.propose_plan(rid, {"pieces": PLAN, "why": "two seams", "from": LEAD})
    dto = drv.approve_plan(rid)
    assert dto["state"] == "running"
    r = _step(rid)
    assert env.refused == []
    assert [t["state"] for t in r["tasks"]] == ["starting", "starting"]
    # The workers are worktrees of the repository HOLDING the lead's worktree,
    # cut at the lead's HEAD — never of its Path (the server's cwd).
    assert [p["repo_path"] for p in env.created] == [repo, repo]
    for p in env.created:
        assert p["base_ref"] == head and p["base_branch"] == LEAD_BRANCH
        assert p["parent"] == LEAD
    titles = [t["title"] for t in r["tasks"]]
    for title in titles:
        mwt = env.instances[title].GetWorktreePath()
        assert gm.repo_of(mwt) == repo
        assert gm.rev_parse(mwt, "HEAD") == head
    assert [t["repo_root"] for t in r["tasks"]] == [repo, repo]
    assert r["repo_root"] == repo
    r = _step(rid)
    assert [t["state"] for t in r["tasks"]] == ["working", "working"]
    env.rows[LEAD] = {
        "title": LEAD,
        "activity": "idle",
        "activity_since": time.time() - 600,
        "repo": "app",
    }
    t1, t2 = titles
    w1 = env.instances[t1].GetWorktreePath()
    w2 = env.instances[t2].GetWorktreePath()
    c1 = _commit(w1, "auth/tokens.py", "T = 2\n", "auth: rotate tokens")
    c2 = _commit(w2, "auth/session.py", "S = 2\n", "auth: session store")
    _done(env, t1, env.instances[t1].Branch, "Rotated the tokens.")
    _done(env, t2, env.instances[t2].Branch, "Put sessions in a store.")
    for _ in range(12):
        r = _step(rid)
        if r["state"] == "release_ready" and r["release"]["state"] == "ready":
            break
    assert [t["state"] for t in r["tasks"]] == ["integrated", "integrated"]
    assert r["state"] == "release_ready", r["state"]
    # Merged back into the TICKET branch, which the lead is still on.
    assert gm.current_branch(lead_wt) == LEAD_BRANCH
    for c in (c1, c2):
        assert gm.is_ancestor(lead_wt, c, "HEAD") is True
    # Pieces are not tickets: the ticket ledger is never touched by them.
    assert env.ledger == []
    return rid, head


class TestATicketLeadSplitsInItsOwnRepository:
    def test_provisioned_lead_with_a_forge_origin_releases_a_pr(self, env):
        repo, wt = _provisioned_lead(env)
        rid, _head = _run_to_release(env, repo, wt)
        rel = tr.load(rid)["release"]
        assert rel["branch"] == LEAD_BRANCH and rel["base"] == "main"
        assert rel["local_origin"] == "" and rel["files"] == 3
        drv.release(rid)
        assert ap.get(LEAD)["lane"] == "pr"
        ap.update(LEAD, state="done", url="https://github.com/acme/app/pull/9")
        r = _step(rid)
        assert r["state"] == "done" and r["release"]["state"] == "done"
        assert r["release"]["pr_url"] == "https://github.com/acme/app/pull/9"

    def test_clone_strategy_lead_uses_its_own_clone(self, env):
        repo, wt = _clone_lead(env)
        assert repo == wt
        _run_to_release(env, repo, wt)

    def test_a_folder_origin_pushes_there_and_never_claims_a_pr(self, env):
        # No forge remote on the source checkout: the workspace's origin is
        # that FOLDER — the deferred provisioned-origin case.
        repo, wt = _provisioned_lead(env, origin=env.source)
        rid, _head = _run_to_release(env, repo, wt)
        r = tr.load(rid)
        assert r["release"]["local_origin"] == env.source
        # The Outbox's release row offers a push, and says why.
        row = outbox_mod._run_ask(r, time.time())
        assert row["kind"] == "release" and row["preview"]["lane"] == "push"
        assert row["preview"]["local_origin"] == env.source
        assert "a folder on this machine" in row["reason"]
        drv.release(rid)
        rec = ap.get(LEAD)
        assert rec["lane"] == "push"  # never "pr": no PR can be opened there
        assert tr.load(rid)["release"]["local_origin"] == env.source
        ap.update(LEAD, state="done")
        r = _step(rid)
        rel = r["release"]
        assert r["state"] == "done"
        assert rel["state"] == "handoff" and rel["pr_url"] == ""
        assert rel["compare_url"] == ""
        assert "to %s — a folder on this machine" % env.source in rel["detail"]
        assert "no PR was opened" in rel["detail"]
        assert "a folder on this machine" in r["summary"]["text_md"]
        assert "One PR" not in r["summary"]["text_md"]
        assert "no PR" in ap.get(LEAD)["note"]
        assert "no PR was opened" in tr.finish_phrase(r, 0)

    def test_old_code_shape_the_lead_path_is_not_the_repository(self, env):
        """The bug, pinned: the lead's Path is a git repo that does not hold
        its commit — taking it would have refused every piece."""
        repo, wt = _provisioned_lead(env)
        head = gm.rev_parse(wt, "HEAD")
        assert server._lineage.base_ref_error(env.cwd, head, LEAD_BRANCH)
        assert server._lineage.base_ref_error(repo, head, LEAD_BRANCH) is None
        inst, got = drv._lead_candidate(LEAD)
        assert got == repo != inst.Path


class TestOneForAllTicketLinesMatchTheBaseClone:
    def test_a_ticket_of_the_same_repo_is_accepted(self, env, monkeypatch):
        repo, _wt = _provisioned_lead(env)

        async def _rows():
            return []

        async def _resolve(ref, rows, source="", tid=""):
            return {
                "title": "sc-9-foo",
                "source": "shortcut",
                "id": "9",
                "name": "Foo",
                "branch": "feature/sc-9/foo",
                "repo_url": "git@github.com:acme/App.git",
            }

        monkeypatch.setattr(drv, "_ticket_rows", _rows)
        monkeypatch.setattr(drv, "resolve_ticket", _resolve)
        monkeypatch.setattr(server._ticket_start, "ledger_holder", lambda t: None)
        tasks, _adopted, _warn = asyncio.run(
            drv._build_tasks(
                [{"kind": "ticket", "source": "shortcut", "id": "9"}],
                "pr",
                repo,
                together=True,
            )
        )
        assert [t["title"] for t in tasks] == ["sc-9-foo"]

        async def _other(ref, rows, source="", tid=""):
            return dict(await _resolve(ref, rows), repo_url=FORGE.replace("app", "web"))

        monkeypatch.setattr(drv, "resolve_ticket", _other)
        with pytest.raises(drv.RunError) as err:
            asyncio.run(
                drv._build_tasks(
                    [{"kind": "ticket", "source": "shortcut", "id": "9"}],
                    "pr",
                    repo,
                    together=True,
                )
            )
        assert "one-for-all needs a single repository" in err.value.message


def test_a_piece_of_a_base_clone_is_named_by_its_repository(env):
    repo, _wt = _provisioned_lead(env)

    class _W:
        def GetRepoPath(self):  # noqa: N802
            return repo

    inst = SimpleNamespace(Provisioned=False, Path=repo, GetGitWorktree=lambda: _W())
    assert snapshot_mod._repo_name(inst) == "app"


def test_repo_of_names_the_repository_holding_a_worktree(tmp_path):
    src = tmp_path / "r"
    _init(src, {"a": "1\n"})
    wt = tmp_path / "w"
    _git(src, "worktree", "add", "-q", "-b", "x", str(wt))
    sub = wt / "deep"
    sub.mkdir()
    assert gm.repo_of(str(wt)) == str(src)
    assert gm.repo_of(str(sub)) == str(src)
    assert gm.repo_of(str(src)) == str(src)
    bare = tmp_path / "b.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    assert gm.repo_of(str(bare)) == str(bare)
    assert gm.repo_of(str(tmp_path / "missing")) == ""
    plain = tmp_path / "plain"
    plain.mkdir()
    assert gm.repo_of(str(plain)) == ""
