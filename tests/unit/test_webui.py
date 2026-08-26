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
from backend.web.core import plain_repo
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


def test_create_can_git_init_the_folder_it_then_works_directly_in(
    monkeypatch, tmp_path
):
    """``init_repo`` and ``in_place`` are combinable — and this is the pair.

    create_instance used to compute ``init_repo = … and not in_place``, on the
    reading that an in-place session works in an existing repo and so cannot
    create one. That silently dropped a tick the user had just made: asking for
    "create a git repo in this folder" AND "work directly in this folder" —
    which is the ordinary way to start a brand-new project, since a worktree
    forked off a repo that was empty a second ago is the awkward case, not the
    obvious one — handed back a git-LESS session in a plain folder.

    The pair that genuinely cannot both be true is ``in_place`` and provisioning
    (which builds a separate worktree or clone), and the line above this one in
    the route has always enforced that: ``in_place = … and not is_provisioned``.

    So the real ``_prepare_plain_repo`` runs here, against a folder under
    ``tmp_path`` that does not exist yet: it is the thing that mkdirs, git-inits
    and makes the initial commit, and short of watching it do that there is no
    way to tell "the tick survived" from "the tick was dropped and the folder
    happened to be a repo already".
    """
    captured = []
    _stub_create(monkeypatch, captured=captured)
    asked = []

    def _spy(repo, init):
        # A spy over the REAL preparation, not a stand-in for it: what was asked
        # for (``init``) is the flag this test exists for, and what it does to
        # the folder is the proof it was honoured.
        asked.append((repo, init))
        return plain_repo._prepare_plain_repo(repo, init)

    monkeypatch.setattr(server, "_prepare_plain_repo", _spy)
    folder = tmp_path / "brand-new-project"
    title = "init-and-work-here"
    server.ENGINE.instances.pop(title, None)
    try:
        r = client.post(
            "/api/instances",
            json={
                "title": title,
                "program": "bash",
                "repo_path": str(folder),
                "in_place": True,
                "init_repo": True,
            },
        )
        assert r.status_code == 202
        # The tick reached the folder preparation instead of being zeroed by the
        # in-place one next to it.
        assert asked == [(str(folder), True)]
        # ...and the folder really is a repo with a HEAD to work against, which
        # is what turns the session's diff/commit/PR features on.
        assert (folder / ".git").is_dir()
        assert server._is_git_repo(str(folder)) is True
        assert server._git_has_commits(str(folder)) is True
        # The session runs IN that folder — no worktree cut from it.
        assert len(captured) == 1
        assert captured[0].in_place is True
        assert captured[0].path == str(folder.resolve())
        assert captured[0].provisioned is False
    finally:
        server.ENGINE.instances.pop(title, None)
        server._EVENT_SNAPSHOT.pop(title, None)


def test_create_drops_in_place_for_a_provisioned_session(monkeypatch):
    """The pair that IS exclusive, and now the only one.

    Provisioning builds a SEPARATE worktree or clone, so there is no "this
    folder" left to work directly in; the route has always answered that by
    computing ``in_place = … and not is_provisioned``. Locked here because the
    New Session dialog's checkboxes were changed to mirror exactly this rule
    (they used to exclude ``init_repo`` and ``in_place`` instead, which the test
    above shows was a rule about nothing), so the UI's claim about which two
    modes conflict is only true for as long as this line is.
    """
    captured = []
    _stub_create(monkeypatch, captured=captured, git_enabled=True)
    monkeypatch.setattr(server, "git_available", lambda: True)
    title = "provisioned-not-in-place"
    server.ENGINE.instances.pop(title, None)
    try:
        r = client.post(
            "/api/instances",
            json={
                "title": title,
                "program": "bash",
                "provisioned": True,
                "repo_path": "/tmp/mf-fake",
                "in_place": True,
            },
        )
        assert r.status_code == 202
        assert len(captured) == 1
        assert captured[0].provisioned is True
        assert captured[0].in_place is False  # the tick the server overrules
    finally:
        server.ENGINE.instances.pop(title, None)
        server._EVENT_SNAPSHOT.pop(title, None)
