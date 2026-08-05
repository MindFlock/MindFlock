"""Repo suggestions for the folder picker: the ranking, the bounds, the routes.

Two flavors of coverage, for the reason ``test_git_ops`` splits the same way:

* ranking/bounds tests that replace the authoritative git probe with a ``.git``
  stat, so a test that builds seventy candidate folders stays hermetic and fast
  (the ranking is the subject; whether git agrees is not);
* a handful of tests against *real* throwaway repos in ``tmp_path`` — a plain
  folder, a bare ``git init`` with no HEAD, and a repo with one commit — because
  the ``is_git`` / ``has_commits`` distinction is exactly what the dialog uses to
  say whether the session gets worktrees and diffs.

``HOME`` is monkeypatched throughout: the scan roots hang off it, and a test that
swept the developer's real home directory would be both slow and unrepeatable.
The route tests point ``MINDFLOCK_SETTINGS_FILE`` at ``tmp_path`` as well, so the
onboarded/last-repo writes never touch the user's own ``settings.json``.
"""

from __future__ import annotations

import json
import os
import subprocess

import pytest
from fastapi.testclient import TestClient

from backend.config import settings as S
from backend.web import server
from backend.web.core import repo_picker


# --------------------------------------------------------------------------- #
# helpers                                                                      #
# --------------------------------------------------------------------------- #
@pytest.fixture()
def home(tmp_path, monkeypatch):
    """A throwaway HOME the scan roots hang off."""
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    return h


@pytest.fixture()
def marker_git(monkeypatch):
    """Count any folder carrying a ``.git`` marker as a repo — no subprocesses.

    The real probe shells out to git once per surviving candidate, which is fine
    for a dialog and far too slow for a test that lays out seventy of them.
    """
    monkeypatch.setattr(
        repo_picker,
        "_is_git_repo",
        lambda p: os.path.exists(os.path.join(p, ".git")),
    )


def _marker_repo(parent, name: str) -> str:
    """A directory that looks like a repo to ``marker_git``."""
    d = parent / name
    (d / ".git").mkdir(parents=True)
    return str(d)


def _git(cwd, *args) -> None:
    """Run git in ``cwd`` (real, local, throwaway — never the user's repo)."""
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def _real_repo(parent, name: str, *, commit: bool = False) -> str:
    d = parent / name
    d.mkdir(parents=True)
    _git(d, "init", "-q")
    if commit:
        _git(d, "commit", "-q", "--allow-empty", "-m", "first")
    return str(d)


def _paths(suggestions) -> list:
    return [s["path"] for s in suggestions]


def _names(suggestions) -> list:
    return [s["name"] for s in suggestions]


class TestSuggestRepos:
    def test_recent_paths_keep_the_order_the_caller_gave_them(self, home, marker_git):
        # Recency is the caller's call (settings, then live, then closed
        # sessions) — this module must not re-sort it.
        first = _marker_repo(home, "gamma")
        second = _marker_repo(home, "alpha")
        third = _marker_repo(home, "beta")

        got = repo_picker.suggest_repos(recent_paths=[first, second, third])

        assert _paths(got)[:3] == [first, second, third]
        assert [s["source"] for s in got[:3]] == ["recent", "recent", "recent"]

    def test_a_recent_folder_that_no_longer_exists_is_dropped(self, home, marker_git):
        alive = _marker_repo(home, "alive")

        got = repo_picker.suggest_repos(
            recent_paths=[str(home / "moved-away"), "", alive]
        )

        assert _paths(got) == [alive]

    def test_every_suggestion_carries_the_full_wire_shape(self, home, marker_git):
        repo = _marker_repo(home, "shapely")

        (got,) = repo_picker.suggest_repos(recent_paths=[repo])

        assert got == {
            "path": repo,
            "name": "shapely",
            "is_git": True,
            "source": "recent",
        }

    def test_a_recent_folder_that_is_not_a_repo_is_still_offered(
        self, home, marker_git
    ):
        # A plain folder runs a session in-place with git features off, so it is
        # a legitimate answer — flagged, not hidden.
        plain = home / "notes"
        plain.mkdir()

        (got,) = repo_picker.suggest_repos(recent_paths=[str(plain)])

        assert got["is_git"] is False

    def test_the_launch_directory_is_offered_when_it_is_a_repo(self, home, marker_git):
        launched_from = _marker_repo(home, "launched")

        got = repo_picker.suggest_repos(cwd=launched_from)

        assert got[0]["path"] == launched_from
        assert got[0]["source"] == "cwd"

    def test_a_launch_directory_that_is_not_a_repo_is_left_out(self, home, marker_git):
        plain = home / "somewhere"
        plain.mkdir()

        assert repo_picker.suggest_repos(cwd=str(plain)) == []

    def test_the_same_repo_seen_twice_keeps_its_best_rank(self, home, marker_git):
        repo = _marker_repo(home / "code", "shared")

        got = repo_picker.suggest_repos(recent_paths=[repo], cwd=repo)

        assert _paths(got) == [repo]
        assert got[0]["source"] == "recent"  # not re-listed as cwd or nearby

    def test_a_symlink_and_its_target_are_one_suggestion(self, home, marker_git):
        target = _marker_repo(home / "code", "real")
        link = home / "shortcut"
        link.symlink_to(target)

        got = repo_picker.suggest_repos(recent_paths=[str(link)])

        # Resolved, so the nearby sweep can't offer the same repo a second time
        # under its other name.
        assert _paths(got) == [os.path.realpath(target)]

    def test_only_direct_children_of_a_scan_root_are_swept(self, home, marker_git):
        near = _marker_repo(home / "code", "near")
        _marker_repo(home / "code" / "nested", "deep")

        got = repo_picker.suggest_repos()

        assert _paths(got) == [near]

    def test_a_scan_root_that_is_itself_a_repo_is_offered(self, home, marker_git):
        root_repo = _marker_repo(home, "projects")

        got = repo_picker.suggest_repos()

        assert root_repo in _paths(got)

    def test_dotted_and_noise_folders_are_skipped(self, home, marker_git):
        wanted = _marker_repo(home / "code", "wanted")
        _marker_repo(home / "code", ".hidden")
        _marker_repo(home / "code", "node_modules")
        _marker_repo(home / "code", "__pycache__")
        _marker_repo(home / "code", "venv")

        got = repo_picker.suggest_repos()

        assert _paths(got) == [wanted]

    def test_nearby_is_alphabetical_across_every_root(self, home, marker_git):
        _marker_repo(home / "work", "zeta")
        _marker_repo(home / "code", "Alpha")
        _marker_repo(home / "dev", "middle")

        got = repo_picker.suggest_repos()

        assert _names(got) == ["Alpha", "middle", "zeta"]
        assert {s["source"] for s in got} == {"nearby"}

    def test_the_limit_caps_the_list(self, home, marker_git):
        for n in range(6):
            _marker_repo(home / "code", "repo-%d" % n)

        assert len(repo_picker.suggest_repos(limit=4)) == 4
        assert repo_picker.suggest_repos(limit=0) == []

    def test_the_per_root_cap_stops_a_huge_directory(self, home, marker_git):
        # 70 repos in one root, examined in sorted order: the cap must cut the
        # tail rather than stat every folder in someone's home directory. The
        # dotted siblings are what makes the root realistic — they sort ahead of
        # every letter, so the budget has to be spent on names that can actually
        # be offered rather than on names the filter drops.
        for n in range(70):
            _marker_repo(home / "code", "repo-%02d" % n)
        for n in range(70):
            (home / "code" / (".junk-%02d" % n)).mkdir()

        got = repo_picker.suggest_repos(limit=500)

        assert len(got) == repo_picker._MAX_PER_ROOT
        assert "repo-59" in _names(got)
        assert "repo-60" not in _names(got)

    def test_dot_entries_do_not_spend_the_per_root_budget(self, home, marker_git):
        # A lived-in home directory is mostly dotted entries (~/.cache, ~/.ssh,
        # ~/.zshrc), and "." sorts ahead of every letter and digit. Slicing the
        # raw listing to the budget therefore handed the whole allowance to names
        # the very next line discarded, and the nearby tier returned NOTHING on
        # any home directory with sixty-odd dotfiles — that is every machine that
        # has been used, and nearby is the only tier a first-run user has.
        for n in range(repo_picker._MAX_PER_ROOT + 5):
            (home / (".cache-%02d" % n)).mkdir()
        alpha = _marker_repo(home, "alpha")
        beta = _marker_repo(home, "beta")

        got = repo_picker.suggest_repos()

        assert _paths(got) == [alpha, beta]

    def test_a_repeated_recent_path_costs_one_git_probe(self, home, monkeypatch):
        # server.py's gather appends last_repo_path, then one path per live
        # session, then one per recently-closed entry — for someone who keeps
        # every session in one checkout that is the same folder forty times over.
        # Probing git as an argument to _add ran the rev-parse subprocess before
        # the dedup check saw it, so opening the New Session dialog spawned ~41
        # processes to produce one suggestion.
        first = _marker_repo(home, "one")
        second = _marker_repo(home, "two")
        probed: list = []

        def counting_probe(path):
            probed.append(path)
            return os.path.exists(os.path.join(path, ".git"))

        monkeypatch.setattr(repo_picker, "_is_git_repo", counting_probe)

        got = repo_picker.suggest_repos(
            recent_paths=[first] * 10 + [second] * 10, limit=2
        )

        assert _paths(got) == [first, second]
        assert probed == [first, second]

    def test_the_overall_cap_stops_the_whole_sweep(self, home, marker_git, monkeypatch):
        monkeypatch.setattr(repo_picker, "_MAX_SCANNED", 3)
        for n in range(3):
            _marker_repo(home / "code", "code-%d" % n)
        _marker_repo(home / "work", "later")

        got = repo_picker.suggest_repos(limit=500)

        # The budget was spent under ~/code, so ~/work is never reached.
        assert "later" not in _names(got)

    def test_an_unreadable_directory_degrades_instead_of_raising(
        self, home, marker_git, monkeypatch
    ):
        (home / "code").mkdir()
        reachable = _marker_repo(home / "work", "reachable")
        real_listdir = os.listdir

        def deny(path, *a, **kw):
            if str(path) == str(home / "code"):
                raise PermissionError(13, "Permission denied")
            return real_listdir(path, *a, **kw)

        monkeypatch.setattr(repo_picker.os, "listdir", deny)

        got = repo_picker.suggest_repos()

        assert _paths(got) == [reachable]

    def test_a_real_repo_is_recognised_without_the_stubbed_probe(self, home):
        """The wiring to the shared git probe, exercised against real git."""
        repo = _real_repo(home / "code", "genuine", commit=True)
        (home / "code" / "plain").mkdir()

        got = repo_picker.suggest_repos(cwd=repo)

        assert got[0] == {
            "path": repo,
            "name": "genuine",
            "is_git": True,
            "source": "cwd",
        }
        assert "plain" not in _names(got)


class TestCheckRepo:
    def test_a_path_that_does_not_exist_is_a_normal_answer(self, home):
        got = repo_picker.check_repo(str(home / "not-yet"))

        assert got == {
            "path": str(home / "not-yet"),
            "exists": False,
            "is_dir": False,
            "is_git": False,
            "has_commits": False,
        }

    def test_a_blank_path_answers_about_nothing(self, home, monkeypatch):
        # realpath("") is the process cwd, which nobody asked about.
        monkeypatch.chdir(str(home))

        assert repo_picker.check_repo("   ")["path"] == ""
        assert repo_picker.check_repo("")["exists"] is False

    def test_a_tilde_is_expanded_but_an_env_var_is_left_alone(
        self, home, marker_git, monkeypatch
    ):
        # Expanduser only, because that is all the folder actually gets created
        # with: /api/browse and _prepare_plain_repo never call expandvars. When
        # this step expanded $VAR and they did not, the dialog reported "git repo,
        # has commits" about one directory and Create then made a literal "$VAR"
        # folder tree somewhere else entirely — a git-less session in a directory
        # the user never named.
        repo = _marker_repo(home, "typed")
        monkeypatch.setenv("MY_CODE_DIR", str(home))
        monkeypatch.chdir(str(home))

        assert repo_picker.check_repo("~/typed")["path"] == repo

        got = repo_picker.check_repo("$MY_CODE_DIR/typed")

        assert got["path"].endswith("$MY_CODE_DIR/typed")
        assert (got["exists"], got["is_git"]) == (False, False)

    def test_a_plain_folder_exists_but_is_not_a_repo(self, home):
        plain = home / "notes"
        plain.mkdir()

        got = repo_picker.check_repo(str(plain))

        assert (got["exists"], got["is_dir"], got["is_git"]) == (True, True, False)
        assert got["has_commits"] is False

    def test_a_file_is_not_a_directory(self, home):
        f = home / "README.md"
        f.write_text("hi\n")

        got = repo_picker.check_repo(str(f))

        assert got["exists"] is True
        assert got["is_dir"] is False

    def test_a_fresh_git_init_has_no_commits_yet(self, home):
        # This is the case that can't have a worktree forked off it — the dialog
        # needs to say so before the session is created, not after it fails.
        repo = _real_repo(home, "empty-repo")

        got = repo_picker.check_repo(repo)

        assert got["is_git"] is True
        assert got["has_commits"] is False

    def test_a_repo_with_a_commit_reports_commits(self, home):
        repo = _real_repo(home, "used-repo", commit=True)

        got = repo_picker.check_repo(repo)

        assert (got["is_git"], got["has_commits"]) == (True, True)


class TestLastRepoPathSetting:
    def test_it_survives_a_save_and_a_fresh_load(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(tmp_path / "settings.json"))
        S.invalidate()

        S.update_settings(general={"last_repo_path": "/home/me/code/foo"})
        S.invalidate()  # force a re-read from disk, not the parse cache

        assert S.load_settings().general.last_repo_path == "/home/me/code/foo"
        stored = json.loads((tmp_path / "settings.json").read_text())
        assert stored["general"]["last_repo_path"] == "/home/me/code/foo"
        S.invalidate()

    def test_an_unset_path_stays_out_of_the_document(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(tmp_path / "settings.json"))
        S.invalidate()

        S.update_settings(general={"onboarded": True})

        stored = json.loads((tmp_path / "settings.json").read_text())
        assert "last_repo_path" not in stored["general"]
        S.invalidate()

    def test_creating_a_session_remembers_the_folder_it_chose(
        self, tmp_path, home, monkeypatch
    ):
        """The writer side: without this the suggestion list never learns."""
        monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(tmp_path / "settings.json"))
        S.invalidate()
        chosen = str(home / "chosen-repo")

        # Same neutering as test_webui's create tests: no real worktree, no
        # background Start, no tmux — only the route's own bookkeeping runs.
        class _FakeInst:
            def __init__(self, opts):
                self.Title = opts.title
                self.Branch = ""
                self.Prompt = getattr(opts, "prompt", "") or ""
                self.ExtraEnv = {}

            def SetStatus(self, status):  # noqa: N802
                return None

            def Started(self):  # noqa: N802
                return False

            def Start(self, *a, **k):  # noqa: N802
                return None

            def GetWorktreePath(self):  # noqa: N802
                return ""

        monkeypatch.setattr(server.session, "NewInstance", _FakeInst)
        monkeypatch.setattr(
            server, "_prepare_plain_repo", lambda repo, init: (chosen, True)
        )
        monkeypatch.setattr(server, "_instance_json", lambda inst, **kw: {})
        monkeypatch.setattr(server, "_register_task", lambda coro: coro.close())
        monkeypatch.setattr(server._ports, "env_for", lambda title: {})
        monkeypatch.setattr(server.ENGINE, "instances", {})

        r = TestClient(server.app).post(
            "/api/instances",
            json={"title": "remembers", "program": "bash", "repo_path": chosen},
        )

        assert r.status_code == 202
        assert S.load_settings().general.last_repo_path == chosen
        S.invalidate()


class TestRepoPickerApi:
    @pytest.fixture()
    def client(self, tmp_path, home, monkeypatch):
        monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(tmp_path / "settings.json"))
        S.invalidate()
        # The registry is a process singleton that may already hold the
        # developer's own sessions; empty it so the ranking under test is the
        # only thing feeding the list.
        monkeypatch.setattr(server.ENGINE, "instances", {})
        monkeypatch.setattr(server, "_load_recently_closed", lambda: [])
        yield TestClient(server.app)
        S.invalidate()

    def test_suggest_ranks_settings_then_closed_then_nearby(
        self, client, home, marker_git, monkeypatch
    ):
        last_used = _marker_repo(home / "code", "last-used")
        closed_repo = _marker_repo(home / "code", "closed-repo")
        _marker_repo(home / "code", "just-lying-around")
        S.update_settings(general={"last_repo_path": last_used})
        monkeypatch.setattr(
            server,
            "_load_recently_closed",
            lambda: [
                {"title": "x", "folder": "/gone/wt", "data": {"path": closed_repo}}
            ],
        )

        body = client.get("/api/repos/suggest").json()

        assert body["home"] == str(home)
        assert _paths(body["suggestions"])[:2] == [last_used, closed_repo]
        assert [s["source"] for s in body["suggestions"][:2]] == ["recent", "recent"]
        assert "just-lying-around" in _names(body["suggestions"])
        assert set(body["suggestions"][0]) == {"path", "name", "is_git", "source"}

    def test_suggest_offers_the_repo_a_live_session_uses(
        self, client, home, marker_git, monkeypatch
    ):
        repo = _marker_repo(home / "code", "in-use")
        inst = type("_Inst", (), {"Path": repo, "UpdatedAt": None})()
        monkeypatch.setattr(server.ENGINE, "instances", {"live": inst})

        body = client.get("/api/repos/suggest").json()

        assert body["suggestions"][0]["path"] == repo
        assert body["suggestions"][0]["source"] == "recent"

    def test_check_reports_a_repo_the_user_typed(self, client, home):
        repo = _real_repo(home, "typed-repo", commit=True)

        body = client.get("/api/repos/check", params={"path": repo}).json()

        assert body == {
            "path": repo,
            "exists": True,
            "is_dir": True,
            "is_git": True,
            "has_commits": True,
        }

    def test_check_answers_200_while_the_user_is_still_typing(self, client, home):
        r = client.get("/api/repos/check", params={"path": str(home / "co")})

        assert r.status_code == 200
        assert r.json()["exists"] is False

    def test_check_rejects_only_a_blank_path(self, client):
        assert client.get("/api/repos/check", params={"path": "  "}).status_code == 400
        assert client.get("/api/repos/check").status_code == 400
