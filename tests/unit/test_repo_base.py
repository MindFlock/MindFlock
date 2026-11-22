"""Which branch a repository's work goes into (``backend/repo_base.py``).

Each step of the resolution order, on REAL temp git repos with a local bare
"origin" (the conftest guard lets only those be asked): the per-repository
setting → the flat setting only where the repository has it → the remote's
default branch (``origin/HEAD``, else ``ls-remote --symref``) → main/master →
a plain sentence. Plus the callers that used to hand git a flat "staging" the
repository did not have: Make PR (the route's Open PR stop), the ship dialog's
default, and the base a provisioned workspace is cut from.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from backend import repo_base
from backend.config import settings as S
from backend.session import provisioned
from backend.web import server

client = TestClient(server.app)


@pytest.fixture(autouse=True)
def _isolate_store(isolate_settings_store, monkeypatch):
    monkeypatch.delenv("MINDFLOCK_BASE_BRANCH", raising=False)
    monkeypatch.delenv("MINDFLOCK_REPO_URL", raising=False)


def _git(*args: str, cwd: Path | str | None = None) -> str:
    cp = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        check=True,
        capture_output=True,
        text=True,
    )
    return cp.stdout.strip()


def _remote(tmp_path: Path, *branches: str, head: str = "main") -> Path:
    """A bare "origin" whose HEAD names ``head`` and which has ``branches``
    (the first is the one HEAD points at when it exists)."""
    seed = tmp_path / "seed"
    _git("init", "-q", "-b", branches[0] if branches else "seed", str(seed))
    (seed / "a.txt").write_text("one\n")
    _git("add", "-A", cwd=seed)
    _git("commit", "-q", "-m", "init", cwd=seed)
    for b in branches[1:]:
        _git("branch", b, cwd=seed)
    bare = tmp_path / "origin.git"
    _git("clone", "-q", "--bare", str(seed), str(bare))
    if not branches:
        _git("branch", "-D", "seed", cwd=bare)
    _git("symbolic-ref", "HEAD", "refs/heads/" + head, cwd=bare)
    return bare


def _checkout(tmp_path: Path, bare: Path, *, origin_head: bool = True) -> Path:
    """A clone of ``bare``; without ``origin_head`` its origin/HEAD is unset
    (a ``git remote add`` + fetch checkout, the case that asks the remote).

    git >= 2.48 makes ``fetch`` create origin/HEAD itself
    (``remote.<name>.followRemoteHEAD`` defaults to "create"), so the unset
    case is made explicitly rather than relied on from the git version."""
    wt = tmp_path / "wt"
    if origin_head:
        _git("clone", "-q", str(bare), str(wt))
    else:
        _git("init", "-q", "-b", "work", str(wt))
        _git("remote", "add", "origin", str(bare), cwd=wt)
        _git("fetch", "-q", "origin", cwd=wt)
        _git("remote", "set-head", "origin", "--delete", cwd=wt)
    return wt


def _flat(**repo):
    S.update_settings(repository=repo)


# --------------------------------------------------------------------------- #
# The resolution order, step by step                                           #
# --------------------------------------------------------------------------- #
def test_step_a_the_per_repo_setting_is_used_as_written(tmp_path):
    bare = _remote(tmp_path, "main")
    wt = _checkout(tmp_path, bare)
    got = repo_base.resolve(wt.as_posix(), "pr", per_repo="release", flat="staging")
    assert (got.branch, got.source, got.note) == ("release", "repo", "")
    assert got.why == "release · your setting for this repository"


def test_step_b_the_flat_setting_when_the_repo_has_it(tmp_path):
    bare = _remote(tmp_path, "main", "staging")
    wt = _checkout(tmp_path, bare)
    got = repo_base.resolve(str(wt), "pr", per_repo="", flat="staging")
    assert (got.branch, got.source, got.note) == ("staging", "setting", "")
    assert got.why == "staging · your setting"


def test_step_b_asks_the_remote_when_the_checkout_has_not_fetched_it(tmp_path):
    bare = _remote(tmp_path, "main")
    wt = _checkout(tmp_path, bare)
    # The branch appears on the remote after the clone: no local ref yet.
    seed = tmp_path / "seed"
    _git("branch", "staging", cwd=seed)
    _git("push", "-q", str(bare), "staging", cwd=seed)
    assert repo_base.branch_exists(str(wt), "staging", network=False) is None
    got = repo_base.resolve(str(wt), "pr", per_repo="", flat="staging")
    assert (got.branch, got.source) == ("staging", "setting")


def test_flat_branch_missing_on_this_repo_falls_to_its_default(tmp_path):
    # THE 2026-11-19 case: a flat "staging" (right for another repository)
    # on a repository that only has main.
    bare = _remote(tmp_path, "main")
    wt = _checkout(tmp_path, bare)
    got = repo_base.resolve(str(wt), "pr", per_repo="", flat="staging")
    assert (got.branch, got.source, got.skipped) == ("main", "default", "staging")
    assert got.note == "Using main: this repository has no staging branch"
    assert got.why == "main · the repository's default"
    assert got.error == ""


def test_step_c_origin_head_unset_asks_the_remote(tmp_path):
    bare = _remote(tmp_path, "trunk", head="trunk")
    wt = _checkout(tmp_path, bare, origin_head=False)
    assert repo_base._origin_head(str(wt)) == ""
    got = repo_base.resolve(str(wt), "pr", per_repo="", flat="")
    assert (got.branch, got.source) == ("trunk", "default")


def test_step_c_the_remote_answer_is_cached(tmp_path, monkeypatch):
    bare = _remote(tmp_path, "trunk", head="trunk")
    wt = _checkout(tmp_path, bare, origin_head=False)
    calls = []
    real = repo_base.gitutil.git

    def _count(args, *a, **kw):
        if args and args[0] == "ls-remote":
            calls.append(args)
        return real(args, *a, **kw)

    monkeypatch.setattr(repo_base.gitutil, "git", _count)
    for _ in range(3):
        assert repo_base.default_branch(str(wt)) == "trunk"
    assert len(calls) == 1


def test_step_d_main_or_master_when_the_remote_names_no_default(tmp_path):
    # HEAD points at a branch that does not exist: no default to read.
    bare = _remote(tmp_path, "master", head="nowhere")
    wt = _checkout(tmp_path, bare, origin_head=False)
    got = repo_base.resolve(str(wt), "pr", per_repo="", flat="staging")
    assert (got.branch, got.source, got.skipped) == ("master", "guess", "staging")
    assert got.note == "Using master: this repository has no staging branch"


def test_nothing_resolves_is_a_plain_sentence_with_the_remedy(tmp_path):
    bare = _remote(tmp_path, "trunk", head="nowhere")
    wt = _checkout(tmp_path, bare, origin_head=False)
    got = repo_base.resolve(str(wt), "pr", per_repo="", flat="staging")
    assert got.branch == ""
    assert got.error == (
        "MindFlock could not tell which branch this repository's work goes into: "
        "it has no staging branch, its remote names no default branch, and it "
        "has no main or master. Set a base branch for this repository in "
        "Settings → Workspace → Per-repo branches."
    )
    assert "ambiguous" not in got.error and "origin/" not in got.error


def test_an_unreachable_remote_keeps_the_setting(tmp_path):
    # "Missing" is only said on evidence: offline is not a reason to skip.
    wt = tmp_path / "wt"
    _git("init", "-q", "-b", "main", str(wt))
    _git("remote", "add", "origin", str(tmp_path / "gone.git"), cwd=wt)
    assert repo_base.branch_exists(str(wt), "staging") is None
    got = repo_base.resolve(str(wt), "pr", per_repo="", flat="staging")
    assert (got.branch, got.source) == ("staging", "setting")


def test_a_repo_with_no_origin_is_judged_by_its_own_branches(tmp_path):
    wt = tmp_path / "wt"
    _git("init", "-q", "-b", "main", str(wt))
    _git("commit", "-q", "--allow-empty", "-m", "x", cwd=wt)
    got = repo_base.resolve(str(wt), "base", per_repo="", flat="staging")
    assert (got.branch, got.source, got.skipped) == ("main", "guess", "staging")


def test_settings_feed_the_resolver_per_kind(tmp_path):
    bare = _remote(tmp_path, "main", "develop")
    wt = _checkout(tmp_path, bare)
    _flat(base_branch="develop", pr_base_branch="staging")
    assert repo_base.resolve(str(wt), "base").branch == "develop"
    pr = repo_base.resolve(str(wt), "pr")
    assert (pr.branch, pr.skipped) == ("main", "staging")


# --------------------------------------------------------------------------- #
# Make PR / the Open PR stop                                                   #
# --------------------------------------------------------------------------- #
class _Inst:
    Program = "bash"
    InPlace = False

    def __init__(self, title: str, wt: str, base: str = ""):
        self.Title = title
        self.Branch = "feat/x"
        self.BaseBranch = base
        self.Path = wt
        self._wt = wt

    def GetWorktreePath(self):  # noqa: N802
        return self._wt

    def Started(self):  # noqa: N802
        return True


@pytest.fixture
def pr_route(monkeypatch):
    """Register a session and capture the ``gh pr create`` argv make-pr runs."""
    made: list = []
    argv: list = []

    def _register(title: str, wt: Path, base: str = ""):
        inst = _Inst(title, str(wt), base)
        monkeypatch.setitem(server.ENGINE.instances, title, inst)
        made.append(title)
        return inst

    def _fake_run(args, **kw):
        argv.append(list(args))
        return SimpleNamespace(
            returncode=0, stdout=b"https://github.com/o/r/pull/5\n", stderr=b""
        )

    async def _no_gate(*_a, **_k):
        return None

    monkeypatch.setattr(server, "git_available", lambda: True)
    monkeypatch.setattr(server, "gh_available", lambda: True)
    monkeypatch.setattr(server, "_run_capped", _fake_run)
    monkeypatch.setattr(server, "_current_branch", lambda w: "feat/x")
    monkeypatch.setattr(server, "_red_zone_gate", _no_gate)
    monkeypatch.setattr(server._peer_guard, "refusal", lambda *a, **k: None)
    monkeypatch.setattr(server, "_forget_probes", lambda t: None)
    yield SimpleNamespace(register=_register, argv=argv)
    for t in made:
        server.ENGINE.instances.pop(t, None)
        server._BASE_BRANCH_CACHE.pop(str(t), None)


def _gh_base(argv: list) -> str:
    gh = [a for a in argv if a[:3] == ["gh", "pr", "create"]]
    assert gh, argv
    return gh[-1][gh[-1].index("--base") + 1]


def test_make_pr_skips_a_flat_base_this_repo_lacks(tmp_path, pr_route):
    bare = _remote(tmp_path, "main")
    wt = _checkout(tmp_path, bare)
    _flat(base_branch="staging", pr_base_branch="staging")
    # A session provisioned under the old fallback recorded "staging" too.
    pr_route.register("rb-skip", wt, base="staging")
    r = client.post("/api/instances/rb-skip/make-pr", json={})
    assert r.status_code == 200, r.text
    assert _gh_base(pr_route.argv) == "main"
    assert r.json()["base_note"] == "Using main: this repository has no staging branch"


def test_make_pr_keeps_a_flat_base_the_repo_has(tmp_path, pr_route):
    bare = _remote(tmp_path, "main", "staging")
    wt = _checkout(tmp_path, bare)
    _flat(pr_base_branch="staging")
    pr_route.register("rb-keep", wt, base="main")
    r = client.post("/api/instances/rb-keep/make-pr", json={})
    assert r.status_code == 200, r.text
    assert _gh_base(pr_route.argv) == "staging"
    assert "base_note" not in r.json()


def test_make_pr_per_repo_setting_wins(tmp_path, pr_route):
    bare = _remote(tmp_path, "main", "staging", "release")
    wt = _checkout(tmp_path, bare)
    _git("remote", "set-url", "origin", "git@github.com:acme/beacon.git", cwd=wt)
    _flat(
        pr_base_branch="staging",
        repo_branches={"acme/beacon": {"pr_base_branch": "release"}},
    )
    pr_route.register("rb-repo", wt, base="main")
    r = client.post("/api/instances/rb-repo/make-pr", json={})
    assert r.status_code == 200, r.text
    assert _gh_base(pr_route.argv) == "release"


def test_make_pr_nothing_resolves_is_a_plain_409(tmp_path, pr_route):
    bare = _remote(tmp_path, "trunk", head="nowhere")
    wt = _checkout(tmp_path, bare, origin_head=False)
    _flat(pr_base_branch="staging")
    pr_route.register("rb-none", wt, base="")
    # The worktree itself is on "work" (no main/master anywhere).
    r = client.post("/api/instances/rb-none/make-pr", json={})
    assert r.status_code == 409
    err = r.json()["error"]
    assert err.startswith("MindFlock could not tell which branch")
    assert "Settings → Workspace → Per-repo branches" in err
    assert not [a for a in pr_route.argv if a[:2] == ["gh", "pr"]]


def test_the_ship_dialog_default_says_why(tmp_path, pr_route):
    bare = _remote(tmp_path, "main")
    wt = _checkout(tmp_path, bare)
    _flat(pr_base_branch="staging")
    pr_route.register("rb-dialog", wt, base="staging")
    body = client.get("/api/instances/rb-dialog/branches").json()
    assert body["default"] == "main"
    assert body["note"] == "Using main: this repository has no staging branch"
    assert body["why"] == "main · the repository's default"
    assert body["error"] == ""


def test_the_open_pr_stop_receipt_carries_the_note():
    from backend.web.core import flights

    rec = {
        "state": "done",
        "url": "https://github.com/o/r/pull/291",
        "base_note": "Using main: this repository has no staging branch",
    }
    out = flights._judge_ship_lane({"config": {"lane": "pr"}}, rec, "pr")
    assert out["action"] == "done"
    assert out["detail"]["receipt"] == (
        "PR #291 · Using main: this repository has no staging branch"
    )


# --------------------------------------------------------------------------- #
# The base a provisioned workspace is cut from                                 #
# --------------------------------------------------------------------------- #
def _prov(tmp_path, bare, base, source):
    return provisioned.ProvisionSettings(
        repo_url=str(bare),
        workspace_dir=tmp_path / "ws",
        base_branch=base,
        base_source=source,
    )


def test_settle_skips_a_flat_base_the_repo_lacks(tmp_path):
    bare = _remote(tmp_path, "main")
    s = provisioned.settle_base_branch(_prov(tmp_path, bare, "staging", "setting"))
    assert (s.base_branch, s.base_note) == (
        "main",
        "Using main: this repository has no staging branch",
    )


def test_settle_keeps_a_flat_base_the_repo_has(tmp_path):
    bare = _remote(tmp_path, "main", "staging")
    s = provisioned.settle_base_branch(_prov(tmp_path, bare, "staging", "setting"))
    assert (s.base_branch, s.base_note) == ("staging", "")


def test_settle_unconfigured_uses_the_repo_default(tmp_path):
    bare = _remote(tmp_path, "trunk", head="trunk")
    s = provisioned.settle_base_branch(_prov(tmp_path, bare, "main", ""))
    assert s.base_branch == "trunk"


def test_settle_leaves_a_per_repo_base_alone(tmp_path):
    bare = _remote(tmp_path, "main")
    s = provisioned.settle_base_branch(_prov(tmp_path, bare, "release", "repo"))
    assert s.base_branch == "release"


def test_load_provision_settings_marks_where_the_base_came_from(tmp_path):
    _flat(url="git@github.com:acme/site.git", workspace_dir=str(tmp_path / "ws"))
    s = provisioned.load_provision_settings(config_path=tmp_path / "none.toml")
    assert (s.base_branch, s.base_source) == ("main", "")
    _flat(base_branch="staging")
    s = provisioned.load_provision_settings(config_path=tmp_path / "none.toml")
    assert (s.base_branch, s.base_source) == ("staging", "setting")
    _flat(repo_branches={"acme/site": {"base_branch": "develop"}})
    s = provisioned.load_provision_settings(config_path=tmp_path / "none.toml")
    assert (s.base_branch, s.base_source) == ("develop", "repo")


# --------------------------------------------------------------------------- #
# Settings → Per-repo branches, and the PR monitor's base filter               #
# --------------------------------------------------------------------------- #
def test_settings_list_says_what_each_known_repo_uses_and_why(tmp_path):
    from backend.web.core import repo_bases

    bare = _remote(tmp_path, "main")
    wt = _checkout(tmp_path, bare)
    _flat(base_branch="staging", pr_base_branch="staging")
    engine = SimpleNamespace(instances={"s": _Inst("s", str(wt))})
    rows = repo_bases.list_bases(engine)["repos"]
    assert len(rows) == 1
    row = rows[0]
    assert row["base"]["why"] == "main · the repository's default"
    assert row["base"]["note"] == "Using main: this repository has no staging branch"
    assert row["pr"]["branch"] == "main"


def test_settings_list_shows_a_per_repo_row_without_a_checkout():
    from backend.web.core import repo_bases

    _flat(repo_branches={"acme/beacon": {"base_branch": "trunk"}})
    rows = repo_bases.list_bases(SimpleNamespace(instances={}))["repos"]
    assert rows == [
        {
            "repo": "acme/beacon",
            "base": {
                "branch": "trunk",
                "source": "repo",
                "why": "trunk · your setting for this repository",
                "note": "",
                "error": "",
            },
            "pr": {
                "branch": "trunk",
                "source": "repo",
                "why": "trunk · your setting for this repository",
                "note": "",
                "error": "",
            },
        }
    ]


def test_the_route_answers_the_list(monkeypatch):
    # server.ENGINE is the real state file: never enumerate its sessions here.
    monkeypatch.setattr(server.ENGINE, "instances", {})
    r = client.get("/api/repos/bases")
    assert r.status_code == 200
    assert isinstance(r.json()["repos"], list)


def _gh(**kw):
    from backend.ticket_ingestion.config import GithubConfig

    base = dict(
        base_branch="staging",
        min_age_minutes=15,
        poll_interval_seconds=60,
        enabled=True,
        skip_authors=[],
    )
    base.update(kw)
    return GithubConfig(**base)


def test_pr_monitor_drops_a_screen_wide_base_the_repo_lacks(monkeypatch):
    import asyncio

    from backend.ticket_ingestion import pr_monitor

    monkeypatch.setattr(pr_monitor, "_BASE_SEEN", {})
    asked: list = []

    async def _exists(self, repo, branch):
        asked.append((repo, branch))
        return repo != "acme/app"

    monkeypatch.setattr(pr_monitor.PRMonitor, "_branch_exists", _exists)
    mon = pr_monitor.PRMonitor(
        _gh(repo_settings={"acme/own": {"base_branch": "develop"}})
    )
    assert asyncio.run(mon.base_filter("acme/app")) == ""
    assert pr_monitor.flat_base_missing("acme/app", "staging")
    assert asyncio.run(mon.base_filter("acme/site")) == "staging"
    # A repo's own override is used as written, never asked about.
    assert asyncio.run(mon.base_filter("acme/own")) == "develop"
    # Cached: a second pass asks GitHub nothing new.
    asyncio.run(mon.base_filter("acme/app"))
    assert asked == [("acme/app", "staging"), ("acme/site", "staging")]
