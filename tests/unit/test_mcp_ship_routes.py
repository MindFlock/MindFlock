"""Server routes behind the MindFlock MCP's ship and ticket tools.

* ``GET /api/instances/{title}/ship-status`` — the commit/push facts a caller
  of the fire-and-forget ``/commit`` and ``/push-branch`` polls, against a
  real throwaway repo.
* ``POST /api/instances/{title}/make-pr`` — the optional ``title`` / ``body``
  overrides on both PR rungs (gh and REST).
* ``POST /api/tickets/start`` — ``parent`` / ``spawned`` / ``report_back`` /
  ``note`` for an agent starting a ticket as its own worker: validation, the
  spawn limits (before the 202 and again under the claim), the prompt tail,
  the lineage the launched instance carries, and the unchanged shape for the
  Intake panel.

Every test swaps ``ENGINE.instances`` for a private dict (the live engine
reads the real state.json) and never launches anything.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend.session.storage import Status
from backend.web import server
from backend.web.core import agent_state
from backend.web.core import github_pr
from backend.web.core import pending as pending_mod
from backend.web.core import ship_status

client = TestClient(server.app)


class _Inst:
    def __init__(self, title, wt="", parent="", spawned=False, program="claude"):
        self.Title = title
        self.Parent = parent
        self.Spawned = spawned
        self.Program = program
        self.Branch = "feat/x"
        self.BaseBranch = "main"
        self.InPlace = False
        self.Path = wt
        self.Status = Status.Running
        self._wt = wt

    def Started(self):  # noqa: N802
        return True

    def GetWorktreePath(self):  # noqa: N802
        return self._wt


@pytest.fixture
def reg(monkeypatch):
    instances: dict = {}
    monkeypatch.setattr(server.ENGINE, "instances", instances)
    monkeypatch.setattr(server.ENGINE, "save", lambda **kw: None)
    return instances


def _git(cwd, *args):
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@example.test")
    _git(r, "config", "user.name", "t")
    _git(r, "config", "commit.gpgsign", "false")
    (r / "a.txt").write_text("a\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "base")
    _git(r, "checkout", "-q", "-b", "feat/x")
    (r / "b.txt").write_text("b\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "work")
    return r


@pytest.fixture
def quiet_probes(monkeypatch):
    """Stub the tmux-backed probes: no shell pane exists in a test."""
    monkeypatch.setattr(agent_state, "_precommit_lock_active", lambda i, wt: False)
    monkeypatch.setattr(server, "_has_origin", lambda wt, *a: True)
    monkeypatch.setattr(server, "_failed_precommit_step", lambda t: "black")
    monkeypatch.setattr(server, "_failed_precommit_hook", lambda t: "black")
    monkeypatch.setattr(
        server,
        "_capture_shell_pane",
        lambda t, lines=400: "$ git commit\nblack....Failed\n- hook id: black\n\n\n",
    )


# --------------------------------------------------------------------------- #
# ship-status
# --------------------------------------------------------------------------- #
class TestShipStatus:
    def test_unknown_session_is_404(self, reg):
        r = client.get("/api/instances/nope/ship-status")
        assert r.status_code == 404

    def test_no_worktree_is_409(self, reg):
        reg["s"] = _Inst("s", wt="")
        r = client.get("/api/instances/s/ship-status")
        assert r.status_code == 409
        assert "workspace not ready" in r.json()["error"]

    def test_clean_committed_unpushed_branch(self, reg, repo, quiet_probes):
        reg["s"] = _Inst("s", wt=str(repo))
        doc = client.get("/api/instances/s/ship-status").json()
        assert doc["branch"] == "feat/x"
        assert doc["base"] == "main"
        assert doc["head_sha"] == _git(repo, "rev-parse", "HEAD")
        assert doc["upstream_sha"] == "" and doc["pushed"] is False
        assert doc["dirty"] is False
        assert doc["beyond_base"] == 1
        assert doc["committing"] is False
        assert doc["commit_rc"] is None and doc["commit_at"] is None
        assert doc["failed_step"] is None
        assert "shell_tail" not in doc
        assert isinstance(doc["now"], float)

    def test_pushed_when_the_remote_tracking_ref_is_head(self, reg, repo, quiet_probes):
        head = _git(repo, "rev-parse", "HEAD")
        _git(repo, "update-ref", "refs/remotes/origin/feat/x", head)
        reg["s"] = _Inst("s", wt=str(repo))
        doc = client.get("/api/instances/s/ship-status").json()
        assert doc["upstream_sha"] == head and doc["pushed"] is True
        # A newer local commit is no longer pushed.
        (repo / "c.txt").write_text("c\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "more")
        doc = client.get("/api/instances/s/ship-status").json()
        assert doc["pushed"] is False

    def test_dirty_tree(self, reg, repo, quiet_probes):
        (repo / "a.txt").write_text("changed\n")
        reg["s"] = _Inst("s", wt=str(repo))
        assert client.get("/api/instances/s/ship-status").json()["dirty"] is True

    def test_a_failed_commit_marker_carries_the_hook(self, reg, repo, quiet_probes):
        (repo / server._COMMIT_STATUS_FILE).write_text("1\n")
        reg["s"] = _Inst("s", wt=str(repo))
        doc = client.get("/api/instances/s/ship-status?tail=3").json()
        assert doc["commit_rc"] == 1
        assert doc["commit_at"] == pytest.approx(
            os.path.getmtime(repo / server._COMMIT_STATUS_FILE)
        )
        assert doc["failed_step"] == "black" and doc["failed_hook"] == "black"
        # The tail drops trailing blank rows and keeps the LAST lines.
        assert doc["shell_tail"] == "$ git commit\nblack....Failed\n- hook id: black"

    def test_a_successful_marker_reads_no_failure(self, reg, repo, quiet_probes):
        (repo / server._COMMIT_STATUS_FILE).write_text("0\n")
        reg["s"] = _Inst("s", wt=str(repo))
        doc = client.get("/api/instances/s/ship-status").json()
        assert doc["commit_rc"] == 0 and doc["failed_step"] is None

    def test_a_running_commit_reports_no_failure_yet(
        self, reg, repo, quiet_probes, monkeypatch
    ):
        (repo / server._COMMIT_STATUS_FILE).write_text("1\n")
        monkeypatch.setattr(agent_state, "_precommit_lock_active", lambda i, w: True)
        reg["s"] = _Inst("s", wt=str(repo))
        doc = client.get("/api/instances/s/ship-status").json()
        assert doc["committing"] is True and doc["failed_step"] is None

    def test_tail_is_capped(self, reg, repo, quiet_probes, monkeypatch):
        monkeypatch.setattr(
            server,
            "_capture_shell_pane",
            lambda t, lines=400: "\n".join("line %d" % i for i in range(500)),
        )
        reg["s"] = _Inst("s", wt=str(repo))
        doc = client.get("/api/instances/s/ship-status?tail=9999").json()
        rows = doc["shell_tail"].splitlines()
        assert len(rows) == ship_status.MAX_TAIL_LINES
        assert rows[-1] == "line 499"

    def test_tail_text_keeps_the_end_within_the_char_cap(self):
        text = "x" * (ship_status.MAX_TAIL_CHARS + 50) + "\nEND"
        out = ship_status.tail_text(text, 5)
        assert out.endswith("END")
        assert len(out) == ship_status.MAX_TAIL_CHARS


# --------------------------------------------------------------------------- #
# make-pr title / body overrides
# --------------------------------------------------------------------------- #
def _cp(rc, out=""):
    return subprocess.CompletedProcess([], rc, stdout=out.encode(), stderr=b"")


@pytest.fixture
def mkpr(reg, tmp_path, monkeypatch):
    reg["s"] = _Inst("s", wt=str(tmp_path))
    monkeypatch.setattr(server, "git_available", lambda: True)
    monkeypatch.setattr(server, "_configured_pr_base", lambda: "main")
    monkeypatch.setattr(server, "_current_branch", lambda wt: "feat/x")

    async def _no_gate(*a, **k):
        return None

    monkeypatch.setattr(server, "_red_zone_gate", _no_gate)
    return reg


class TestMakePrOverrides:
    def _gh(self, monkeypatch):
        argv: list = []
        monkeypatch.setattr(server, "gh_available", lambda: True)
        monkeypatch.setattr(
            server,
            "_run_capped",
            lambda a, **k: argv.append(list(a)) or _cp(0, "https://gh.test/pr/9"),
        )
        monkeypatch.setattr(
            github_pr, "_fill", lambda wt, b, h: ("Commit subject", "commit body")
        )
        return argv

    def test_gh_without_overrides_still_fills(self, mkpr, monkeypatch):
        argv = self._gh(monkeypatch)
        r = client.post("/api/instances/s/make-pr", json={})
        assert r.json() == {"ok": True, "url": "https://gh.test/pr/9"}
        assert argv[0][-1] == "--fill"
        assert "--body" not in argv[0]

    def test_gh_body_override_keeps_the_commit_title(self, mkpr, monkeypatch):
        argv = self._gh(monkeypatch)
        r = client.post(
            "/api/instances/s/make-pr", json={"body": "  Worker report\n\nTests ok "}
        )
        assert r.status_code == 200, r.text
        a = argv[0]
        assert "--fill" not in a
        assert a[a.index("--title") + 1] == "Commit subject"
        assert a[a.index("--body") + 1] == "Worker report\n\nTests ok"

    def test_gh_title_override_keeps_the_commit_body(self, mkpr, monkeypatch):
        argv = self._gh(monkeypatch)
        client.post("/api/instances/s/make-pr", json={"title": "Better title"})
        a = argv[0]
        assert a[a.index("--title") + 1] == "Better title"
        assert a[a.index("--body") + 1] == "commit body"

    def test_gh_override_with_no_commits_is_the_nothing_to_pr_400(
        self, mkpr, monkeypatch
    ):
        self._gh(monkeypatch)
        monkeypatch.setattr(github_pr, "_fill", lambda wt, b, h: None)
        monkeypatch.setattr(server, "_pr_info", lambda *a, **k: None)
        r = client.post("/api/instances/s/make-pr", json={"body": "x"})
        assert r.status_code == 400
        assert "nothing to PR" in r.json()["error"]

    def test_non_string_overrides_are_400(self, mkpr, monkeypatch):
        self._gh(monkeypatch)
        r = client.post("/api/instances/s/make-pr", json={"body": ["x"]})
        assert r.status_code == 400
        assert "must be strings" in r.json()["error"]

    def test_rest_rung_gets_the_overrides_only_when_given(self, mkpr, monkeypatch):
        monkeypatch.setattr(server, "gh_available", lambda: False)
        calls: list = []

        async def _create(wt, base, head, **kw):
            calls.append(kw)
            return github_pr.PRResult(ok=True, url="https://rest.test/pr/3")

        monkeypatch.setattr(github_pr, "create_pr", _create)
        r = client.post("/api/instances/s/make-pr", json={"body": "report"})
        assert r.json()["url"] == "https://rest.test/pr/3"
        client.post("/api/instances/s/make-pr", json={})
        assert calls == [{"body": "report"}, {}]

    def test_rest_create_pr_overrides_replace_the_fill(self, monkeypatch):
        monkeypatch.setattr(
            github_pr, "repo_ref", lambda wt: SimpleNamespace(slug="o/r")
        )

        async def _token():
            return "tok"

        sent: list = []

        async def _request(method, path, token=None, body=None):
            sent.append(body)
            return 201, {"html_url": "https://x/pr/1", "number": 1}

        monkeypatch.setattr(github_pr, "api_token", _token)
        monkeypatch.setattr(github_pr, "_request", _request)
        monkeypatch.setattr(github_pr, "_fill", lambda wt, b, h: ("subj", "fill"))
        asyncio.run(github_pr.create_pr("/ws", "main", "feat/x", body="mine"))
        asyncio.run(github_pr.create_pr("/ws", "main", "feat/x", title="T"))
        assert (sent[0]["title"], sent[0]["body"]) == ("subj", "mine")
        assert (sent[1]["title"], sent[1]["body"]) == ("T", "fill")


# --------------------------------------------------------------------------- #
# /api/tickets/start lineage
# --------------------------------------------------------------------------- #
class _Stop(Exception):
    pass


def _stub_ticket(monkeypatch, agent="claude"):
    story = SimpleNamespace(
        id="23588", name="Fix it", repo_url="git@x:o/r.git", agent=agent, effort=""
    )
    ts = server._ticket_start

    async def _find(source, tid):
        return story

    async def _none(*a, **k):
        return None

    monkeypatch.setattr(ts, "find_ticket", _find)
    monkeypatch.setattr(ts, "session_title", lambda s: "sc-23588")
    monkeypatch.setattr(ts, "branch_for", lambda s: "feature/sc-23588/fix-it")
    monkeypatch.setattr(ts, "workspace_mode", lambda: "worktree")
    monkeypatch.setattr(ts, "build_prompt", lambda s: "TICKET TEXT")
    monkeypatch.setattr(ts, "record_started", lambda s: None)
    monkeypatch.setattr(ts, "record_result", lambda *a, **k: None)
    monkeypatch.setattr(ts, "agent_for", lambda s: s.agent)
    monkeypatch.setattr(ts, "effort_for", lambda s: "")
    monkeypatch.setattr(ts, "download_attachments", _none)
    monkeypatch.setattr(ts, "move_to_start_state", _none)
    monkeypatch.setattr(server, "_source_intake_depth", lambda src: "")
    monkeypatch.setattr(server, "_cached_session_title", lambda *a, **k: "")
    monkeypatch.setattr(server, "_red_zone_prompt", lambda p, *a, **k: p)
    monkeypatch.setattr(
        server._worktree_reclaim, "reclaim_for_launch", lambda *a, **k: None
    )
    monkeypatch.setattr(server, "_budget_locked", lambda t: False)
    monkeypatch.setattr(
        server,
        "_agent_mcp_caps",
        lambda: {"enabled": True, "providers": ["claude", "codex"]},
    )
    return story


@pytest.fixture
def ticket(reg, monkeypatch):
    """Run a ticket start up to the claim: NewInstance builds a stand-in whose
    Start raises, so nothing is provisioned. Returns (post, seen, tasks)."""
    tasks: list = []
    seen: list = []
    armed: list = []
    # Accepted starts are a process-wide map; a start whose launch a test
    # never runs would otherwise leave its title "already exists" for the next.
    monkeypatch.setattr(pending_mod, "_PENDING", {})
    monkeypatch.setattr(server, "git_available", lambda: True)
    monkeypatch.setattr(server, "_register_task", tasks.append)
    monkeypatch.setattr(
        server,
        "_arm_intake_autopilot",
        lambda *a, **k: armed.append(a) or armed_by.append(k.get("by", "user")),
    )
    armed_by: list = []

    def _new(opts):
        seen.append(opts)
        inst = _Inst(opts.title, parent=opts.parent, spawned=opts.spawned)

        def _start(*_a):
            raise _Stop("not in a test")

        inst.Start = _start
        inst.SetStatus = lambda s: None
        inst.ExtraEnv = {}
        return inst

    monkeypatch.setattr(server.session, "NewInstance", _new)
    monkeypatch.setattr(server, "_seed_event_snapshot", lambda t: None)
    monkeypatch.setattr(server, "_drop_failed_start", lambda t, i: None)
    _stub_ticket(monkeypatch)

    def post(body):
        return client.post("/api/tickets/start", json={"source": "sc", **body})

    yield SimpleNamespace(
        post=post, seen=seen, tasks=tasks, armed=armed, armed_by=armed_by
    )
    for coro in tasks:  # launches a test never ran
        coro.close()


def _run(tasks):
    (coro,) = tasks
    asyncio.run(coro)
    tasks.clear()


class TestTicketStartLineage:
    def test_the_intake_panel_shape_is_unchanged(self, ticket):
        r = ticket.post({"id": "23588"})
        assert r.status_code == 202
        assert r.json() == {"started": True, "title": "sc-23588"}
        _run(ticket.tasks)
        (opts,) = ticket.seen
        assert opts.parent == "" and opts.spawned is False
        assert opts.prompt == "TICKET TEXT"

    def test_parent_spawned_and_footer(self, ticket, reg):
        reg["orch"] = _Inst("orch")
        r = ticket.post(
            {
                "id": "23588",
                "parent": "orch",
                "spawned": True,
                "report_back": True,
                "note": "Only the API half.",
                "depth": "off",
            }
        )
        assert r.status_code == 202, r.text
        body = r.json()
        assert body == {
            "started": True,
            "title": "sc-23588",
            "branch": "feature/sc-23588/fix-it",
            "program": "claude",
            "parent": "orch",
            "spawned": True,
            "report_back": True,
        }
        # depth "off" reaches the autopilot arm as "off" (no source default).
        assert ticket.armed[0][1] == "off"
        # An agent's spawn_ticket_session arms as the AGENT's choice — never
        # read as the user's lane (which an agent may not raise).
        assert ticket.armed_by == ["agent:orch"]
        _run(ticket.tasks)
        (opts,) = ticket.seen
        assert opts.parent == "orch" and opts.spawned is True
        assert opts.prompt.startswith("TICKET TEXT\n\n## Note from session orch")
        assert "Only the API half." in opts.prompt
        assert opts.prompt.rstrip().endswith(
            "the answer will be typed into this terminal."
        )
        assert 'MindFlock worker session "sc-23588", spawned by "orch"' in opts.prompt
        assert "mcp__mindflock__report_result" in opts.prompt

    def test_codex_worker_gets_its_tool_spelling(self, ticket, reg, monkeypatch):
        reg["orch"] = _Inst("orch")
        _stub_ticket(monkeypatch, agent="codex")
        r = ticket.post({"id": "1", "parent": "orch", "report_back": True})
        assert r.json()["program"] == "codex"
        _run(ticket.tasks)
        assert 'report_result tool of the "mindflock"' in ticket.seen[0].prompt

    def test_no_footer_for_a_cli_without_the_tools(self, ticket, reg, monkeypatch):
        reg["orch"] = _Inst("orch")
        _stub_ticket(monkeypatch, agent="aider")
        r = ticket.post({"id": "1", "parent": "orch", "report_back": True})
        body = r.json()
        assert body["report_back"] is False and body["reason"]
        _run(ticket.tasks)
        assert "report_result" not in ticket.seen[0].prompt

    def test_no_footer_without_a_parent(self, ticket):
        r = ticket.post({"id": "1", "spawned": True, "report_back": True})
        assert r.json()["report_back"] is False
        assert r.json()["reason"] == "no parent to report to"

    @pytest.mark.parametrize(
        "extra,msg",
        [
            ({"spawned": "true"}, "spawned must be a boolean"),
            ({"report_back": 1}, "report_back must be a boolean"),
            ({"note": 5}, "note must be a string"),
            ({"note": "x" * 4001}, "note is longer"),
            ({"parent": "ghost"}, "unknown parent session: ghost"),
        ],
    )
    def test_bad_lineage_is_400_before_any_fetch(self, ticket, extra, msg):
        r = ticket.post({"id": "1", **extra})
        assert r.status_code == 400
        assert msg in r.json()["error"]
        assert ticket.tasks == []

    def test_budget_locked_parent_is_409(self, ticket, reg, monkeypatch):
        reg["orch"] = _Inst("orch")
        monkeypatch.setattr(server, "_budget_locked", lambda t: True)
        r = ticket.post({"id": "1", "parent": "orch", "spawned": True})
        assert r.status_code == 409 and r.json()["budget_locked"] is True

    def test_children_limit_is_409_before_the_202(self, ticket, reg, monkeypatch):
        monkeypatch.setenv("MINDFLOCK_MAX_CHILDREN", "1")
        reg["orch"] = _Inst("orch")
        reg["w1"] = _Inst("w1", parent="orch", spawned=True)
        r = ticket.post({"id": "1", "parent": "orch", "spawned": True})
        assert r.status_code == 409
        assert "MINDFLOCK_MAX_CHILDREN=1" in r.json()["error"]
        assert ticket.tasks == []

    def test_total_spawned_limit_applies_without_a_parent(
        self, ticket, reg, monkeypatch
    ):
        monkeypatch.setenv("MINDFLOCK_MAX_SPAWNED", "1")
        reg["w"] = _Inst("w", spawned=True)
        r = ticket.post({"id": "1", "spawned": True})
        assert r.status_code == 409
        assert "MINDFLOCK_MAX_SPAWNED=1" in r.json()["error"]

    def test_the_claim_rechecks_the_limits(self, ticket, reg, monkeypatch):
        """A sibling spawned between the 202 and the launch takes the last
        slot: the launch refuses to claim, records why, and registers
        nothing."""
        monkeypatch.setenv("MINDFLOCK_MAX_CHILDREN", "1")
        reg["orch"] = _Inst("orch")
        r = ticket.post({"id": "1", "parent": "orch", "spawned": True})
        assert r.status_code == 202
        reg["w1"] = _Inst("w1", parent="orch", spawned=True)
        _run(ticket.tasks)
        assert "sc-23588" not in reg
        fail = client.get("/api/create_failures?title=sc-23588").json()
        assert "MINDFLOCK_MAX_CHILDREN=1" in fail["failures"]["sc-23588"]["error"]

    def test_the_claim_refuses_a_parent_that_went_away(self, ticket, reg):
        reg["orch"] = _Inst("orch")
        r = ticket.post({"id": "1", "parent": "orch", "spawned": True})
        assert r.status_code == 202
        del reg["orch"]
        _run(ticket.tasks)
        assert "sc-23588" not in reg
        fail = client.get("/api/create_failures?title=sc-23588").json()
        assert "parent session orch is gone" in (fail["failures"]["sc-23588"]["error"])

    def test_the_spawned_instance_is_registered_under_its_parent(
        self, ticket, reg, monkeypatch
    ):
        reg["orch"] = _Inst("orch")
        seeds: list = []
        monkeypatch.setattr(
            server._thread, "note_seed", lambda t, c, p: seeds.append((t, p))
        )
        registered: list = []
        real_drop = server._note_create_failure

        def _start_ok(*_a):
            registered.append(dict(reg))

        orig_new = server.session.NewInstance

        def _new(opts):
            inst = orig_new(opts)
            inst.Start = _start_ok
            return inst

        monkeypatch.setattr(server.session, "NewInstance", _new)
        monkeypatch.setattr(server, "_note_create_failure", real_drop)
        r = ticket.post({"id": "1", "parent": "orch", "spawned": True})
        assert r.status_code == 202
        _run(ticket.tasks)
        (snap,) = registered
        assert snap["sc-23588"].Parent == "orch"
        assert snap["sc-23588"].Spawned is True
        assert seeds and seeds[0][0] == "sc-23588"

    def test_pending_row_carries_the_lineage(self, ticket, reg, monkeypatch):
        reg["orch"] = _Inst("orch")
        added: list = []
        monkeypatch.setattr(
            server, "_pending_add", lambda t, k, **m: added.append((t, k, m))
        )
        ticket.post({"id": "1", "parent": "orch", "spawned": True})
        title, kind, meta = added[-1]
        assert (title, kind) == ("sc-23588", "tix")
        assert meta["parent"] == "orch" and meta["spawned"] is True
