"""API-contract tests for the web UI backend.

These exercise the REST surface and static serving WITHOUT spawning real tmux
sessions (which would mutate ~/.mindflock and require a git repo). The full
create/terminal path is covered by the manual integration run documented in
backend.web/README.md.
"""

from fastapi.testclient import TestClient

from backend import session
from backend.session.storage import Loading
from backend.web import server
from backend.web.server import app

client = TestClient(app)


class _FakeInst:
    """A session stand-in that records the options it was built from, so a
    create test can assert the derived title/branch without building a real
    worktree or starting tmux."""

    def __init__(self, opts):
        self.opts = opts
        self.Title = getattr(opts, "title", "") or ""
        self.Branch = getattr(opts, "new_branch", "") or ""
        self.Program = getattr(opts, "program", "") or ""
        self.Path = getattr(opts, "path", "") or ""
        self.Status = Loading
        self.Prompt = getattr(opts, "prompt", "") or ""
        self.ExtraEnv = {}

    def SetStatus(self, status):  # noqa: N802
        self.Status = status

    def Started(self):  # noqa: N802
        return False

    def Start(self, *a, **k):  # noqa: N802
        return None

    def GetWorktreePath(self):  # noqa: N802
        return ""


def _stub_create(monkeypatch, captured=None, git_enabled=False):
    """Neutralize create_instance's side effects so only its routing/branching
    logic runs: no real worktree (fake NewInstance + _prepare_plain_repo), no
    background Start (the _register_task coro is closed), no disk writes."""

    def _fake_new_instance(opts):
        inst = _FakeInst(opts)
        if captured is not None:
            captured.append(opts)
        return inst

    def _close(coro):
        coro.close()  # never scheduled -> also silences "never awaited"
        return None

    monkeypatch.setattr(session, "NewInstance", _fake_new_instance)
    monkeypatch.setattr(
        server, "_prepare_plain_repo", lambda repo, init: ("/tmp/mf-fake", git_enabled)
    )
    monkeypatch.setattr(
        server,
        "_instance_json",
        lambda inst, **kw: {"title": inst.Title, "branch": inst.Branch},
    )
    monkeypatch.setattr(server, "_register_task", _close)
    monkeypatch.setattr(server, "_mark_onboarded", lambda: None)
    monkeypatch.setattr(server._ports, "env_for", lambda title: {})


def test_index_served():
    r = client.get("/")
    assert r.status_code == 200
    assert "MindFlock" in r.text
    assert "/app.js" in r.text


def test_static_assets_resolve():
    for path in ("/style.css", "/app.js", "/vendor/xterm.js", "/vendor/xterm.css"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert len(r.content) > 0


def test_list_instances_is_json_list():
    r = client.get("/api/instances")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_config_returns_default_program():
    r = client.get("/api/config")
    assert r.status_code == 200
    assert "default_program" in r.json()


def test_create_requires_folder():
    # A blank title is legal (quick-launch names the session "untitled"), but
    # creating without a folder must fail cleanly before any side effects.
    r = client.post("/api/instances", json={"title": "  ", "program": "bash"})
    assert r.status_code == 400
    assert "a folder is required" in r.json()["error"]


def test_delete_missing_instance_404():
    r = client.delete("/api/instances/does-not-exist-xyz")
    assert r.status_code == 404
    assert "instance not found" in r.json()["error"]


def test_action_endpoints_404_on_missing():
    for method, path in (
        ("get", "/api/instances/nope/diff"),
        ("post", "/api/instances/nope/pause"),
        ("post", "/api/instances/nope/resume"),
    ):
        r = getattr(client, method)(path)
        assert r.status_code == 404, path
        assert "instance not found" in r.json()["error"]


def test_instance_json_shape_keys():
    # Contract for the list payload (empty or not, each item has these keys).
    r = client.get("/api/instances")
    for item in r.json():
        assert {
            "title",
            "branch",
            "program",
            "path",
            "status",
            "started",
            "tmux_name",
        } <= set(item)


def test_create_duplicate_title_409():
    # A title already in the registry is refused before any side effects.
    title = "dup-refine-check"
    server.ENGINE.instances[title] = object()  # 409 check only tests membership
    try:
        r = client.post("/api/instances", json={"title": title, "program": "bash"})
        assert r.status_code == 409
        assert "already exists" in r.json()["error"]
    finally:
        server.ENGINE.instances.pop(title, None)


def test_create_untitled_autonumbers(monkeypatch):
    # Two blank-title quick launches: "untitled", then "untitled-2".
    _stub_create(monkeypatch)
    for t in ("untitled", "untitled-2"):
        server.ENGINE.instances.pop(t, None)  # clean slate for the numbering
    try:
        r1 = client.post("/api/instances", json={"title": "", "program": "bash"})
        assert r1.status_code == 202
        assert r1.json()["title"] == "untitled"
        r2 = client.post("/api/instances", json={"title": "", "program": "bash"})
        assert r2.status_code == 202
        assert r2.json()["title"] == "untitled-2"
    finally:
        for t in ("untitled", "untitled-2"):
            server.ENGINE.instances.pop(t, None)
            server._EVENT_SNAPSHOT.pop(t, None)


def test_create_provisioned_branch_name_as_title(monkeypatch):
    # A provisioned Name that is itself a branch path is used verbatim as the
    # branch, with the title set to its last segment.
    captured = []
    _stub_create(monkeypatch, captured=captured, git_enabled=True)
    monkeypatch.setattr(server, "git_available", lambda: True)
    branch = "feature/sc-99987/refine-split-check"
    seg = "refine-split-check"
    server.ENGINE.instances.pop(seg, None)
    try:
        r = client.post(
            "/api/instances",
            json={
                "title": branch,
                "program": "bash",
                "provisioned": True,
                "repo_path": "/tmp/mf-fake",
                "workspace_strategy": "worktree",
            },
        )
        assert r.status_code == 202
        assert r.json()["title"] == seg
        assert len(captured) == 1
        assert captured[0].title == seg
        assert captured[0].new_branch == branch
    finally:
        server.ENGINE.instances.pop(seg, None)
        server._EVENT_SNAPSHOT.pop(seg, None)
