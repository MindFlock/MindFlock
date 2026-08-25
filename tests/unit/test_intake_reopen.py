"""Reopening the workspace an intake item already has on this machine.

The gap this closes: a ticket/PR/issue that has been worked once shows its
history in chips ("already ingested", "a feature branch for it already exists on
the remote") and offers exactly one action — start it *again*. But ending a
session keeps its worktree, and a restart loses the session without touching the
directory, so the work is usually still right there. These cover the two halves:

* :mod:`backend.web.core.reopen` — *is* there a workspace, and which one. A
  recently-closed session outranks a bare directory (it restores the branch,
  program and prompt); a directory that has been deleted must never be offered.
* ``POST /api/intake/reopen`` — the row identifies the item, the SERVER
  re-resolves the workspace (a panel open for an hour must not be able to name a
  directory that has since gone), and a closed session is restored rather than
  approximated.

The worktree lookup runs against real git, like ``test_worktree_reclaim``: "what
does git think is checked out where" is not a question a mock can answer.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from backend.web import server
from backend.web.core import reopen

client = TestClient(server.app)


def _closed(title, folder, *, branch="feature/sc-1/x", entry_id="e1"):
    return {"id": entry_id, "title": title, "branch": branch, "folder": str(folder)}


def _repo_dir(tmp_path, name):
    """A directory that looks like a checkout (the probe insists on ``.git``:
    an emptied workspace must not be offered as reopenable)."""
    d = tmp_path / name
    (d / ".git").mkdir(parents=True)
    return d


@pytest.fixture
def no_closed(monkeypatch):
    """The recently-closed store, empty by default and settable per test."""
    entries: list = []
    monkeypatch.setattr(server, "_load_recently_closed", lambda: entries)
    return entries


# --------------------------------------------------------------------------- #
# find_workspace: which workspace, if any
# --------------------------------------------------------------------------- #
class TestFindWorkspace:
    def test_a_closed_session_is_the_best_answer(self, tmp_path, no_closed):
        folder = _repo_dir(tmp_path, "wt")
        no_closed.append(_closed("sc-1", folder))
        found = reopen.find_workspace(title="sc-1", branch="feature/sc-1/x")
        assert found == {
            "kind": "closed",
            "path": str(folder),
            "entry_id": "e1",
            "branch": "feature/sc-1/x",
            "closed_at": "",
        }

    def test_matched_on_the_branch_when_the_title_differs(self, tmp_path, no_closed):
        # The pipeline titles its own sessions; the panel's title is the slug.
        # Either identifies the same work, so either match counts.
        folder = _repo_dir(tmp_path, "wt")
        no_closed.append(_closed("some-other-title", folder))
        found = reopen.find_workspace(title="sc-1", branch="feature/sc-1/x")
        assert found is not None and found["kind"] == "closed"

    def test_a_deleted_folder_is_not_offered(self, tmp_path, no_closed):
        no_closed.append(_closed("sc-1", tmp_path / "gone"))
        assert reopen.find_workspace(title="sc-1", branch="feature/sc-1/x") is None

    def test_a_folder_that_moved_on_is_not_offered(
        self, tmp_path, no_closed, monkeypatch
    ):
        """The entry records the branch as of the close; the directory may have
        been switched since (a session run in place on a real checkout is the
        case that matters). Claiming that as this ticket's workspace would be a
        lie the button then acts on."""
        folder = _repo_dir(tmp_path, "checkout")
        no_closed.append(_closed("sc-1", folder))
        monkeypatch.setattr(reopen, "_head_branch", lambda path: "main")
        monkeypatch.setattr(reopen, "_worktree_index", lambda repo_url, cache: {})
        assert reopen.find_workspace(title="sc-1", branch="feature/sc-1/x") is None

    def test_an_unreadable_head_does_not_disqualify(
        self, tmp_path, no_closed, monkeypatch
    ):
        # Not evidence of anything — and Recent… would reopen it regardless.
        folder = _repo_dir(tmp_path, "wt")
        no_closed.append(_closed("sc-1", folder))
        monkeypatch.setattr(reopen, "_head_branch", lambda path: "")
        found = reopen.find_workspace(title="sc-1", branch="feature/sc-1/x")
        assert found is not None and found["kind"] == "closed"

    def test_a_folder_stripped_of_its_git_dir_is_not_offered(self, tmp_path, no_closed):
        empty = tmp_path / "husk"
        empty.mkdir()
        no_closed.append(_closed("sc-1", empty))
        assert reopen.find_workspace(title="sc-1", branch="feature/sc-1/x") is None

    def test_an_adopted_workspace_path_is_used_verbatim(self, tmp_path, no_closed):
        # PR review provisions `pr-<slug>` itself, so its path is known without
        # asking git anything.
        folder = _repo_dir(tmp_path, "pr-app-42")
        found = reopen.find_workspace(
            title="pr-app-42", branch="fix/x", workspace_path=str(folder)
        )
        assert found == {"kind": "clone", "path": str(folder), "branch": "fix/x"}

    def test_clone_strategy_derives_the_directory(
        self, tmp_path, no_closed, monkeypatch
    ):
        folder = _repo_dir(tmp_path, "feature-sc-1-x")
        monkeypatch.setattr(
            reopen,
            "_provision_settings",
            lambda repo_url, cache: SimpleNamespace(workspace_dir=tmp_path),
        )
        found = reopen.find_workspace(
            title="sc-1", branch="feature/sc-1/x", strategy="clone"
        )
        assert found == {
            "kind": "clone",
            "path": str(folder),
            "branch": "feature/sc-1/x",
        }

    def test_clone_strategy_without_the_directory_finds_nothing(
        self, tmp_path, no_closed, monkeypatch
    ):
        monkeypatch.setattr(
            reopen,
            "_provision_settings",
            lambda repo_url, cache: SimpleNamespace(workspace_dir=tmp_path),
        )
        assert (
            reopen.find_workspace(
                title="sc-1", branch="feature/sc-1/never-ran", strategy="clone"
            )
            is None
        )

    def test_no_branch_and_no_closed_session_finds_nothing(self, no_closed):
        assert reopen.find_workspace(title="sc-1") is None

    def test_worktree_strategy_asks_the_base_clone(
        self, tmp_path, no_closed, monkeypatch
    ):
        wt = _repo_dir(tmp_path, "held")
        monkeypatch.setattr(
            reopen,
            "_worktree_index",
            lambda repo_url, cache: {"feature/sc-1/x": str(wt)},
        )
        found = reopen.find_workspace(title="sc-1", branch="feature/sc-1/x")
        assert found == {
            "kind": "worktree",
            "path": str(wt),
            "branch": "feature/sc-1/x",
        }

    def test_a_branch_no_worktree_holds_finds_nothing(self, no_closed, monkeypatch):
        monkeypatch.setattr(reopen, "_worktree_index", lambda repo_url, cache: {})
        assert reopen.find_workspace(title="sc-1", branch="feature/sc-1/x") is None


# --------------------------------------------------------------------------- #
# The worktree index (real git)
# --------------------------------------------------------------------------- #
def _git(repo, *args):
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


class TestWorktreeIndex:
    @pytest.fixture
    def base(self, tmp_path):
        base = tmp_path / "base"
        subprocess.run(
            ["git", "init", "-b", "main", str(base)],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        _git(base, "config", "user.email", "t@t.t")
        _git(base, "config", "user.name", "t")
        (base / "seed.txt").write_text("seed\n")
        _git(base, "add", ".")
        _git(base, "commit", "-m", "seed")
        return base

    def test_indexes_every_branch_by_its_worktree(self, base, tmp_path):
        wt = tmp_path / "wt"
        _git(base, "worktree", "add", "-b", "feature/sc-9/x", str(wt), "main")
        index = reopen._list_worktrees(str(base))
        assert index["feature/sc-9/x"] == str(wt)
        # The base clone's own checkout is in there too, which is harmless: an
        # intake item's feature branch is never what the base clone has out.
        assert index["main"] == str(base)

    def test_a_removed_worktree_drops_out(self, base, tmp_path):
        wt = tmp_path / "wt"
        _git(base, "worktree", "add", "-b", "feature/sc-9/x", str(wt), "main")
        _git(base, "worktree", "remove", str(wt))
        assert "feature/sc-9/x" not in reopen._list_worktrees(str(base))

    def test_a_directory_that_is_not_a_repo_answers_empty(self, tmp_path):
        assert reopen._list_worktrees(str(tmp_path)) == {}


# --------------------------------------------------------------------------- #
# annotate: what the panels stamp onto their rows
# --------------------------------------------------------------------------- #
class TestAnnotate:
    def test_rows_with_a_live_session_are_left_alone(self, tmp_path, no_closed):
        folder = _repo_dir(tmp_path, "wt")
        no_closed.append(_closed("sc-1", folder))
        rows = [{"session": "sc-1", "has_session": True}]
        reopen.annotate(rows, lambda r: {"title": r["session"]})
        # It is on screen: offering to reopen it would be noise.
        assert "workspace" not in rows[0]

    def test_rows_without_a_workspace_stay_unannotated(self, no_closed):
        rows = [{"session": "sc-1"}]
        reopen.annotate(rows, lambda r: {"title": r["session"]})
        assert "workspace" not in rows[0]

    def test_a_failing_probe_never_breaks_the_listing(self, no_closed):
        def _boom(row):
            raise RuntimeError("provider config is a mess")

        rows = [{"session": "sc-1"}]
        reopen.annotate(rows, _boom)  # must not raise
        assert rows == [{"session": "sc-1"}]

    def test_one_probe_serves_every_row(self, tmp_path, no_closed, monkeypatch):
        """The cache is shared across the rows of one response — a panel with
        forty tickets on one repo must not run forty ``git worktree list``."""
        calls: list = []

        def _index(repo_url, cache):
            key = ("worktrees", repo_url)
            if key not in cache:
                calls.append(repo_url)
                cache[key] = {}
            return cache[key]

        monkeypatch.setattr(reopen, "_worktree_index", _index)
        rows = [
            {"session": "sc-%d" % n, "branch": "feature/sc-%d/x" % n} for n in range(5)
        ]
        reopen.annotate(
            rows,
            lambda r: {
                "title": r["session"],
                "branch": r["branch"],
                "repo_url": "o/app",
            },
        )
        assert calls == ["o/app"]


# --------------------------------------------------------------------------- #
# POST /api/intake/reopen
# --------------------------------------------------------------------------- #
@pytest.fixture
def cached_ticket():
    """One ticket row in the panel cache, as the tickets listing leaves it."""
    server._ASSIGNED_TICKETS_CACHE["v"] = (
        time.monotonic() + 60,
        {
            "tickets": [
                {
                    "source": "shortcut",
                    "id": "21214",
                    "session": "sc-21214",
                    "branch": "feature/sc-21214/gate-replies",
                    "repo_url": "git@github.com:o/app.git",
                    "strategy": "worktree",
                }
            ]
        },
    )
    yield
    server._ASSIGNED_TICKETS_CACHE.pop("v", None)


class TestReopenRoute:
    def test_an_unknown_kind_is_rejected(self):
        r = client.post("/api/intake/reopen", json={"kind": "nonsense"})
        assert r.status_code == 400

    def test_an_item_the_panel_is_not_showing_is_rejected(self):
        r = client.post(
            "/api/intake/reopen",
            json={"kind": "tickets", "source": "shortcut", "id": "999"},
        )
        assert r.status_code == 409

    def test_a_bad_pr_target_is_rejected(self):
        r = client.post("/api/intake/reopen", json={"kind": "prs", "repo": "nope"})
        assert r.status_code == 400

    def test_an_open_session_is_not_reopened(self, cached_ticket, monkeypatch):
        monkeypatch.setitem(server.ENGINE.instances, "sc-21214", object())
        r = client.post(
            "/api/intake/reopen",
            json={"kind": "tickets", "source": "shortcut", "id": "21214"},
        )
        assert r.status_code == 409
        assert r.json()["title"] == "sc-21214"

    def test_a_vanished_workspace_answers_410(self, cached_ticket, monkeypatch):
        monkeypatch.setattr(reopen, "find_workspace", lambda **kw: None)
        r = client.post(
            "/api/intake/reopen",
            json={"kind": "tickets", "source": "shortcut", "id": "21214"},
        )
        assert r.status_code == 410
        assert "Begin work" in r.json()["error"]

    def test_the_workspace_is_resolved_from_the_row_not_the_payload(
        self, cached_ticket, monkeypatch
    ):
        """The client sends an identity, never a path: the server looks the
        workspace up itself, from the row's own branch/repo/strategy."""
        seen: dict = {}

        def _find(**kw):
            seen.update(kw)
            return None

        monkeypatch.setattr(reopen, "find_workspace", _find)
        client.post(
            "/api/intake/reopen",
            json={
                "kind": "tickets",
                "source": "shortcut",
                "id": "21214",
                "path": "/etc",  # ignored — not part of the contract
            },
        )
        assert seen == {
            "title": "sc-21214",
            "branch": "feature/sc-21214/gate-replies",
            "repo_url": "git@github.com:o/app.git",
            "strategy": "worktree",
        }

    def test_a_closed_session_is_restored_through_the_undo_store(
        self, cached_ticket, monkeypatch
    ):
        """Not re-created from scratch: the stashed InstanceData carries the
        branch, program and prompt, so the reopen must go through it."""
        called: list = []

        async def _reopen_entry(entry_id):
            called.append(entry_id)
            from starlette.responses import JSONResponse

            return JSONResponse({"title": "sc-21214"})

        monkeypatch.setattr(
            reopen,
            "find_workspace",
            lambda **kw: {"kind": "closed", "entry_id": "e42", "path": "/w"},
        )
        monkeypatch.setattr(server, "_reopen_closed_entry", _reopen_entry)
        r = client.post(
            "/api/intake/reopen",
            json={"kind": "tickets", "source": "shortcut", "id": "21214"},
        )
        assert r.status_code == 200
        assert called == ["e42"]

    def test_an_orphan_worktree_opens_in_place(
        self, cached_ticket, monkeypatch, tmp_path
    ):
        """No closed entry to restore (a restart ate the session), so a fresh
        session is put ON the directory — in place, because the directory is not
        this session's to delete when it ends."""
        folder = _repo_dir(tmp_path, "held")
        monkeypatch.setattr(
            reopen,
            "find_workspace",
            lambda **kw: {"kind": "worktree", "path": str(folder), "branch": "b"},
        )
        opts: list = []

        class _Inst:
            Title = "sc-21214"

            def __init__(self, o):
                opts.append(o)
                self.ExtraEnv: dict = {}

            def SetStatus(self, status):  # noqa: N802
                self.Status = status

        monkeypatch.setattr(server.session, "NewInstance", lambda o: _Inst(o))
        monkeypatch.setattr(
            server, "_instance_json", lambda inst: {"title": inst.Title}
        )
        monkeypatch.setattr(server, "_register_task", lambda coro: coro.close())
        try:
            r = client.post(
                "/api/intake/reopen",
                json={"kind": "tickets", "source": "shortcut", "id": "21214"},
            )
            assert r.status_code == 202
            assert r.json()["title"] == "sc-21214"
            assert opts[0].path == str(folder)
            assert opts[0].in_place is True
            # Not provisioned: provisioning would create a SECOND workspace,
            # which is the exact thing this button exists to avoid.
            assert opts[0].provisioned is False
            assert opts[0].title == "sc-21214"
        finally:
            server.ENGINE.instances.pop("sc-21214", None)


# --------------------------------------------------------------------------- #
# The frontend wiring (source + shipped bundle)
# --------------------------------------------------------------------------- #
_INTAKE_SRC = (
    Path(__file__).resolve().parents[2] / "frontend" / "src" / "components" / "intake"
)


class TestFrontendWiring:
    def test_every_tab_offers_the_reopen(self):
        """All three panels, not just tickets: the same thing happens to a PR
        review and to an issue run, and the row component is shared precisely so
        they can't drift."""
        for name in ("TicketsTab.tsx", "PullRequestsTab.tsx", "IssuesTab.tsx"):
            src = (_INTAKE_SRC / name).read_text(encoding="utf-8")
            assert "reopenIntakeItem" in src, name
            assert "workspace={" in src, name
            assert "onReopen={" in src, name

    def test_the_row_only_offers_it_when_there_is_something_to_reopen(self):
        kit = (_INTAKE_SRC / "kit.tsx").read_text(encoding="utf-8")
        # A live session already IS the work on screen, and a row with no
        # workspace has nothing to point at.
        assert "!!workspace && !!onReopen && !hasSession" in kit

    def test_the_bundle_ships_the_button_and_its_endpoint(self):
        js = client.get("/app.js").text
        assert "Reopen window" in js
        assert "/api/intake/reopen" in js
        # The start button demotes when a workspace exists, so the row has one
        # primary action rather than two competing ones.
        assert "ik-start-again" in js

    def test_the_stylesheet_ships_the_demoted_start(self):
        css = client.get("/style.css").text
        assert ".ik-start-again" in css
