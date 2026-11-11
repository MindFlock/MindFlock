"""spawn_session(separate=true): a separate idea forks from the repository's
base branch (``origin/<default>`` when known, else the local default), not
from the caller's HEAD, and is reported as its own branch. Real throwaway
repos; the API is the in-memory fake."""

from __future__ import annotations

import copy
import subprocess

import pytest

from backend import client
from backend.mcp import gitlocal
from backend.mcp.protocol import ToolError
from backend.mcp.tools import build_tools
from tests.unit._mcp_fakes import FakeCtx, make_box, row


def _git(cwd, *args):
    cp = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    )
    return cp.stdout.strip()


def _git_repo(path, branch="main"):
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", branch)
    _git(path, "config", "user.email", "t@example.invalid")
    _git(path, "config", "user.name", "t")
    (path / "README.md").write_text("hi\n")
    _git(path, "add", "README.md")
    _git(path, "commit", "-q", "-m", "init")
    return path


def _commit(path, name):
    (path / name).write_text("x\n")
    _git(path, "add", name)
    _git(path, "commit", "-q", "-m", "add " + name)


def _flock(repo, tmp_path):
    """The orchestrator's worktree (one commit ahead of the base) + its row."""
    orch = tmp_path / "wt-orch"
    _git(repo, "worktree", "add", "-q", "-b", "mindflock/orch", str(orch))
    _commit(orch, "orch.txt")
    rows = [row("orch", folder=str(orch), path=str(repo), branch="mindflock/orch")]
    return orch, rows


@pytest.fixture()
def local_only(tmp_path):
    """No remote: the default branch is the local ``main``."""
    repo = _git_repo(tmp_path / "repo")
    orch, rows = _flock(repo, tmp_path)
    return repo, orch, rows


@pytest.fixture()
def with_origin(tmp_path):
    """A clone of ``upstream`` whose default (origin/HEAD) is ``trunk``; the
    upstream moves on after the clone, so only a fetch sees its new tip."""
    upstream = _git_repo(tmp_path / "upstream", branch="trunk")
    repo = tmp_path / "repo"
    _git(tmp_path, "clone", "-q", str(upstream), str(repo))
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    _commit(upstream, "later.txt")
    orch, rows = _flock(repo, tmp_path)
    return upstream, repo, orch, rows


def _spawn(rows, args):
    # A copy: the fake API adds the created row, which would shift the next
    # spawn's default title.
    box, api, clock = make_box(copy.deepcopy(rows))
    api.created_status = "running"
    out = box.spawn_session(args, FakeCtx(clock))
    posts = [c[2] for c in api.calls if c[:2] == ("POST", "/api/instances")]
    return out, posts


def _note_ok(out, base):
    assert out["separate"] is True and out["base"] == base
    assert base in out["note"] and "do not merge it into yours" in out["note"]
    assert "get_diff" in out["note"] and 'depth="pr"' in out["note"]


class TestSeparateBase:
    def test_origin_default_branch_after_fetch(self, with_origin):
        upstream, repo, orch, rows = with_origin
        (orch / "wip.txt").write_text("wip\n")  # dirty: no warning for separate
        out, posts = _spawn(rows, {"prompt": "idea", "separate": True})
        payload = posts[0]
        assert payload["base_ref"] == "origin/trunk"
        assert payload["base_branch"] == "trunk"
        assert payload["repo_path"] == str(repo)
        # The bounded fetch brought upstream's newer tip in.
        assert _git(repo, "rev-parse", "origin/trunk") == _git(
            upstream, "rev-parse", "HEAD"
        )
        assert out["base_sha"] == _git(upstream, "rev-parse", "HEAD")
        _note_ok(out, "origin/trunk")
        assert "warnings" not in out
        # Still the caller's child, reporting back.
        assert payload["parent"] == "orch" and payload["spawned"] is True
        assert "report_result" in payload["prompt"]

    def test_local_only_default_branch(self, local_only):
        repo, orch, rows = local_only
        out, posts = _spawn(rows, {"prompt": "idea", "separate": True})
        assert posts[0]["base_ref"] == "main"
        assert posts[0]["base_branch"] == "main"
        assert out["base_sha"] == _git(repo, "rev-parse", "main")
        assert out["base_sha"] != _git(orch, "rev-parse", "HEAD")
        _note_ok(out, "main")

    def test_explicit_repo_path(self, local_only, tmp_path):
        repo, _, _ = local_only
        out, posts = _spawn(
            [], {"prompt": "idea", "separate": True, "repo_path": str(repo)}
        )
        assert posts[0]["repo_path"] == str(repo)
        assert posts[0]["base_ref"] == "main" and posts[0]["base_branch"] == "main"
        _note_ok(out, "main")

    def test_in_place_refused(self, local_only):
        _, _, rows = local_only
        box, api, clock = make_box(rows)
        with pytest.raises(ToolError, match="own worktree"):
            box.spawn_session(
                {"prompt": "x", "separate": True, "in_place": True}, FakeCtx(clock)
            )
        assert not [c for c in api.calls if c[:2] == ("POST", "/api/instances")]

    def test_no_default_branch_falls_back_to_head(self, tmp_path):
        repo = _git_repo(tmp_path / "repo", branch="dev")  # no main/master/origin
        orch, rows = _flock(repo, tmp_path)
        out, posts = _spawn(rows, {"prompt": "idea", "separate": True})
        head = _git(orch, "rev-parse", "HEAD")
        assert posts[0]["base_ref"] == head
        assert posts[0]["base_branch"] == "mindflock/orch"
        assert out["base_sha"] == head
        assert "separate" not in out and "note" not in out
        assert any(
            "forked from your HEAD" in w and "base branch could not be found" in w
            for w in out["warnings"]
        )

    def test_fetch_failure_is_silent(self, with_origin, monkeypatch):
        _, _, _, rows = with_origin
        real = gitlocal._RUN

        def no_network(cmd, **kw):
            if "fetch" in cmd:
                raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
            return real(cmd, **kw)

        monkeypatch.setattr(gitlocal, "_RUN", no_network)
        out, posts = _spawn(rows, {"prompt": "idea", "separate": True})
        assert posts[0]["base_ref"] == "origin/trunk"  # the stale ref still
        _note_ok(out, "origin/trunk")
        assert "warnings" not in out

    def test_fetch_is_bounded_and_never_prompts(self, with_origin, monkeypatch):
        _, _, _, rows = with_origin
        seen = []
        real = gitlocal._RUN

        def spy(cmd, **kw):
            if "fetch" in cmd:
                seen.append((cmd, kw))
            return real(cmd, **kw)

        monkeypatch.setattr(gitlocal, "_RUN", spy)
        _spawn(rows, {"prompt": "idea", "separate": True})
        ((cmd, kw),) = seen
        assert cmd[-2:] == ["origin", "trunk"]
        assert kw["timeout"] <= 10
        assert kw["env"]["GIT_TERMINAL_PROMPT"] == "0"

    def test_server_refusing_the_base_names_it(self, local_only):
        _, _, rows = local_only
        box, api, clock = make_box(rows)
        api.created_status = "running"
        original = api._create

        def picky(payload):
            if "base_branch" in payload:
                raise client.ApiError(400, "bad base_branch")
            return original(payload)

        api._create = picky
        out = box.spawn_session({"prompt": "idea", "separate": True}, FakeCtx(clock))
        assert "it still forks from main" in out["warnings"][0]
        _note_ok(out, "main")


class TestUnchanged:
    def test_default_payload_byte_for_byte(self, local_only):
        repo, orch, rows = local_only
        _, plain = _spawn(rows, {"prompt": "p"})
        _, off = _spawn(rows, {"prompt": "p", "separate": False})
        assert off == plain
        assert sorted(plain[0]) == [
            "base_branch",
            "base_ref",
            "parent",
            "program",
            "prompt",
            "repo_path",
            "spawned",
            "title",
        ]
        assert plain[0]["base_branch"] == "mindflock/orch"
        assert plain[0]["base_ref"] == _git(orch, "rev-parse", "HEAD")
        assert plain[0]["repo_path"] == str(repo)

    def test_default_result_has_no_separate_keys(self, local_only):
        _, _, rows = local_only
        out, _ = _spawn(rows, {"prompt": "p"})
        assert not {"separate", "base", "note"} & set(out)

    def test_provisioned_parent_unchanged(self, tmp_path):
        src = _git_repo(tmp_path / "myrepo")
        base = _git_repo(tmp_path / "ws" / "_base_myrepo")
        wt = tmp_path / "ws" / "orch"
        _git(base, "worktree", "add", "-q", "-b", "mindflock/orch", str(wt))
        rows = [
            row(
                "orch",
                folder=str(wt),
                path=str(src),
                provisioned=True,
                workspace_strategy="worktree",
            )
        ]
        out_plain, plain = _spawn(rows, {"prompt": "p"})
        out_sep, sep = _spawn(rows, {"prompt": "p", "separate": True})
        assert sep == plain
        assert "base_ref" not in sep[0] and sep[0]["provisioned"] is True
        assert "separate" not in out_sep
        assert out_sep["warnings"] == out_plain["warnings"]


def test_schema_and_texts():
    box, _, _ = make_box([row("orch")])
    spawn = next(t for t in build_tools(box) if t.name == "spawn_session")
    assert spawn.input_schema["properties"]["separate"]["type"] == "boolean"
    assert spawn.input_schema["properties"]["separate"]["default"] is False
    assert "separate=true" in spawn.description
    whoami = next(t for t in build_tools(box) if t.name == "whoami")
    assert "separate=true" in whoami.description
