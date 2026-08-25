"""Repo suggestions and by-name search for the folder picker: ranking, bounds,
routes.

Two flavors of coverage, for the reason ``test_git_ops`` splits the same way:

* ranking/bounds tests that replace the authoritative git probe with a ``.git``
  stat, so a test that builds seventy candidate folders stays hermetic and fast
  (the ranking is the subject; whether git agrees is not);
* a handful of tests against *real* throwaway repos in ``tmp_path`` — a plain
  folder, a bare ``git init`` with no HEAD, and a repo with one commit — because
  the ``is_git`` / ``has_commits`` distinction is exactly what the dialog uses to
  say whether the session gets worktrees and diffs.

The by-name search (:class:`TestSearchRepos`) is the exception to the first
flavor: its subject IS the walk — how deep it reaches, which trees it refuses to
enter, what it does when a budget runs out — so it builds real directory trees
and lets the real walk walk them, stubbing only the git probe at the end of it.

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


def _folder(parent, *parts) -> str:
    """A plain (non-git) directory — the other half of what a search returns.

    The sweep only ever offers repos, so ``TestSuggestRepos`` had no use for
    this; the name search returns plain folders too (the folder field accepts
    either, and ``is_git`` is what tells them apart in the dialog), and the
    directories a walk must step THROUGH are plain by definition.
    """
    d = parent.joinpath(*parts)
    d.mkdir(parents=True, exist_ok=True)
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


class TestSearchRepos:
    """Looking a folder up by NAME: what it reaches, what it refuses to walk.

    Every test here lays out a real directory tree under the throwaway ``home``
    and lets the real walk walk it. That is deliberate: the depth, the trees it
    declines to enter and the budgets that stop it are the entire subject, and a
    monkeypatched ``os.walk`` would leave nothing under test but the sort. Only
    the authoritative git probe is stubbed (``marker_git``), for the same reason
    the ranking tests above stub it — it is a subprocess per surviving match and
    whether git agrees is not what these are asking.
    """

    def test_a_repo_three_levels_down_is_found_and_a_fourth_is_not(
        self, home, marker_git
    ):
        # The whole reason this function exists: the depth-1 sweep behind
        # /api/repos/suggest cannot see either of these. Depth 3 is where the
        # search stops paying for itself (the directory count multiplies with
        # every level), so the fourth level stays a Browse click.
        reachable = _marker_repo(home / "nest1" / "nest2", "needle-near")
        _marker_repo(home / "nest1" / "nest2" / "nest3", "needle-far")

        got = repo_picker.search_repos("needle", str(home))

        assert _paths(got["matches"]) == [reachable]
        assert got["truncated"] is False  # nothing was skipped for want of budget

    def test_a_repos_own_source_tree_is_never_walked_into(self, home, marker_git):
        # A repo's subdirectories are not separate repos, and the tree hanging
        # off one holds node_modules/vendor/target — tens of thousands of
        # directories that would eat the whole budget to produce matches nobody
        # asked for. Skipping it is the correct ANSWER, not a shortcut.
        outside = _marker_repo(home / "code", "needle-outside")
        _marker_repo(home / "code", "wrapper")
        _folder(home / "code" / "wrapper", "needle-src")
        _folder(home / "code" / "wrapper" / "needle-src", "needle-deeper")

        got = repo_picker.search_repos("needle", str(home))

        assert _paths(got["matches"]) == [outside]

    def test_a_checkout_nested_in_a_repo_IS_found(self, home, marker_git):
        # The other half of the rule above, and the case it originally got
        # wrong. An umbrella directory that happens to be a git repo — a work
        # folder with a committed README and a pile of sibling checkouts under
        # it — is not a source tree, and the repos under it are exactly what
        # somebody searching by name is looking for. On a real machine this cost
        # the search every repo the user worked in daily, because their whole
        # ~/Work was itself a repo; the shallower suggestion tier never reached
        # that deep either, so the folder was unreachable by every route but
        # Browse.
        #
        # So a repo is descended ONE level and only nested REPOS are kept: a
        # src/ under a repo is dropped right there (the test above), a checkout
        # is not.
        _marker_repo(home / "code", "umbrella")
        nested = _marker_repo(home / "code" / "umbrella", "needle-checkout")
        # ...and its own source tree is still off limits, one level further in.
        _folder(home / "code" / "umbrella" / "needle-checkout", "needle-inner-src")

        got = repo_picker.search_repos("needle", str(home))

        assert _paths(got["matches"]) == [nested]

    def test_dotted_and_noise_directories_are_never_walked(self, home, marker_git):
        wanted = _marker_repo(home / "code", "widget")
        _marker_repo(home / "code" / "node_modules", "widget-vendored")
        _marker_repo(home / "code" / ".cache", "widget-cached")
        _folder(home / "code" / "venv", "widget-in-venv")

        got = repo_picker.search_repos("widget", str(home))

        assert _paths(got["matches"]) == [wanted]
        # The rule is about the NAME, not only about what hangs off it: a user
        # searching for "venv" is not asking for the virtualenv the filter drops
        # everywhere else.
        assert repo_picker.search_repos("venv", str(home))["matches"] == []

    def test_a_one_character_query_walks_nothing(self, home, marker_git):
        # One letter matches a large fraction of any real home directory, so the
        # "results" would be a random sample of the machine bought with the whole
        # scan budget — and the user is almost certainly still typing. Not an
        # error, though: the dialog asks on every keystroke.
        _marker_repo(home / "code", "api")

        assert repo_picker.search_repos("a", str(home)) == {
            "matches": [],
            "truncated": False,
        }
        assert repo_picker.search_repos("  x  ", str(home))["matches"] == []
        assert _names(repo_picker.search_repos("ap", str(home))["matches"]) == ["api"]

    def test_ranking_puts_the_folder_the_user_named_first(self, home, marker_git):
        # Someone typing into a folder field is naming a folder, so the basename
        # is what is being searched and the path is the "it's under acme/
        # somewhere" fallback: exact, then prefix, then substring, then path.
        # One folder per tier, so the order below IS the ranking.
        _marker_repo(home / "code", "api")  # exact
        _marker_repo(home / "code", "api-gateway")  # basename prefix
        _folder(home / "code", "rapid")  # basename substring (r-API-d)
        _folder(home / "code" / "rapid", "tools")  # only its PATH matches

        got = repo_picker.search_repos("api", str(home))

        assert _names(got["matches"]) == ["api", "api-gateway", "rapid", "tools"]

    def test_two_folders_of_one_name_rank_shallowest_first(self, home, marker_git):
        # Equal rank, so the tie-break decides: the parent of two identically
        # named folders is the one the user more likely means.
        top = _marker_repo(home, "api")
        deep = _marker_repo(home / "code" / "acme", "api")

        got = repo_picker.search_repos("api", str(home))

        assert _paths(got["matches"]) == [top, deep]

    def test_the_home_directory_itself_is_never_the_answer(self, home, marker_git):
        # $HOME is named after the user, not after their project — matching it
        # would put "your whole account" at the top of a list of repos. The
        # genuine hit is here so an empty list cannot pass this by accident.
        hit = _folder(home / "code", "homework")

        assert _paths(repo_picker.search_repos("home", str(home))["matches"]) == [hit]

    def test_an_omitted_home_falls_back_to_the_users_own(self, home, marker_git):
        # What the route relies on when it passes expanduser("~") — and what the
        # CLI would get by leaving the argument off entirely.
        repo = _marker_repo(home / "code", "defaulted")

        assert _paths(repo_picker.search_repos("defaulted")["matches"]) == [repo]

    def test_matches_carry_the_suggestion_shape_and_flag_plain_folders(self, home):
        """Real git, no stub: ``is_git`` is what the dialog renders the two as."""
        repo = _real_repo(home / "code", "genuine-thing", commit=True)
        plain = _folder(home / "code", "genuine-notes")

        got = repo_picker.search_repos("genuine", str(home))

        # Same rank and same path length, so alphabetical breaks the tie.
        assert got["matches"] == [
            {
                "path": plain,
                "name": "genuine-notes",
                "is_git": False,
                "source": "search",
            },
            {
                "path": repo,
                "name": "genuine-thing",
                "is_git": True,
                "source": "search",
            },
        ]

    def test_the_scan_cap_ends_the_walk_and_says_so(
        self, home, marker_git, monkeypatch
    ):
        # A count bounds WORK, and this is the bound that trips on a home
        # directory with a junk-drawer in it. Breadth-first is what makes the
        # partial answer worth returning: ~/code's own children are all examined
        # before anything descends into the bulk, so the early hit survives the
        # truncation instead of being lost inside whichever subtree was entered
        # first.
        monkeypatch.setattr(repo_picker, "_MAX_SEARCH_SCANNED", 6)
        early = _marker_repo(home / "code", "aaa-hit")
        for n in range(8):
            _folder(home / "code" / "zz-bulk", "noise-%d" % n)

        got = repo_picker.search_repos("hit", str(home))

        assert got["truncated"] is True  # "there may be more", not "it isn't there"
        assert _paths(got["matches"]) == [early]

    def test_loose_files_cannot_spend_a_budget_meant_for_directories(
        self, home, marker_git, monkeypatch
    ):
        # The junk-drawer case, and the one the cap exists to survive rather than
        # to be defeated by. The budget was once charged per NAME a listing
        # returned, files included, so a Downloads folder of loose files spent the
        # whole allowance before the walk descended anywhere — and since a tripped
        # budget ends the walk, the repo already sitting in the queue two cheap
        # levels away came back as "nothing found", under a line of copy telling
        # the user to type more of a name that would have changed nothing.
        monkeypatch.setattr(repo_picker, "_MAX_SEARCH_SCANNED", 12)
        drawer = home / "Downloads"
        drawer.mkdir()
        for n in range(40):
            (drawer / ("junk-%02d.zip" % n)).write_text("x")
        wanted = _marker_repo(home / "code" / "acme", "api")

        got = repo_picker.search_repos("api", str(home))

        assert _paths(got["matches"]) == [wanted]
        # Forty files, a cap of twelve, and nothing was skipped: only the handful
        # of real directories was ever charged for.
        assert got["truncated"] is False

    def test_the_deadline_is_read_once_per_directory_not_only_before_a_descent(
        self, home, marker_git
    ):
        # A count bounds WORK, not TIME — which is why the deadline is here at
        # all. It used to be read only just before a child was queued, so every
        # directory that skipped earlier (a repo, a node at the depth limit, one
        # that cannot be read) returned to the top of the walk without the clock
        # ever being consulted: a ~/code holding five hundred sibling clones
        # drained its whole queue past an expired deadline at two stats apiece,
        # which on the cold mount this guards is a minute of a dialog that looks
        # hung — and ``truncated`` reported nothing about it.
        for n in range(20):
            _marker_repo(home / "code", "hit-%02d" % n)

        class _Clock:
            """A clock a second later every time it is read.

            The deadline is set from the first reading (0.0 + 1.5s), survives the
            second (1.0) and has expired by the third (2.0), so the walk gets
            exactly one directory in. A fake beats a sleep: the real 1.5s would
            put a second and a half into every run of the suite to assert a
            branch that is pure arithmetic.
            """

            def __init__(self) -> None:
                self.now = 0.0

            def monotonic(self) -> float:
                reading = self.now
                self.now += 1.0
                return reading

        examined: list = []
        real_has_git = repo_picker._has_git_dir

        def counting_has_git(path: str) -> bool:
            examined.append(path)
            return real_has_git(path)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(repo_picker, "time", _Clock())
            mp.setattr(repo_picker, "_has_git_dir", counting_has_git)
            got = repo_picker.search_repos("hit", str(home))

        assert got["truncated"] is True  # and it says so, rather than "not here"
        # One directory examined — home itself — and then the walk stopped. The
        # twenty repos it had queued were never stat'd.
        assert examined == [str(home)]
        assert got["matches"] == []

    def test_more_matches_than_asked_for_are_flagged_too(self, home, marker_git):
        # Same flag as a tripped budget, because it is the same news to the user
        # ("this list is not everything") with the same fix (type more).
        for n in range(5):
            _marker_repo(home / "code", "many-%d" % n)

        got = repo_picker.search_repos("many", str(home), limit=2)

        assert _names(got["matches"]) == ["many-0", "many-1"]
        assert got["truncated"] is True
        # A caller asking for nothing gets nothing, without a walk.
        assert repo_picker.search_repos("many", str(home), limit=0) == {
            "matches": [],
            "truncated": False,
        }

    def test_an_unreadable_directory_costs_its_own_subtree_and_nothing_more(
        self, home, marker_git, monkeypatch
    ):
        # Module contract: nothing in here raises. A dead mount or a directory
        # the user cannot read must not turn a folder lookup into an error toast
        # in front of someone who is merely trying to start a session.
        _folder(home / "code", "walled")
        reachable = _marker_repo(home / "work", "walled-off-elsewhere")
        real_scandir = os.scandir

        def deny(path, *a, **kw):
            if str(path) == str(home / "code" / "walled"):
                raise PermissionError(13, "Permission denied")
            return real_scandir(path, *a, **kw)

        # scandir, not listdir: the walk lists with scandir so it can tell a
        # directory from a file without a stat per name (see
        # ``_walkable_child_names``), and this is the call that dies on a folder
        # the user cannot read.
        monkeypatch.setattr(repo_picker.os, "scandir", deny)

        got = repo_picker.search_repos("walled", str(home))

        # The unreadable folder is still a match — it is only its CONTENTS that
        # could not be read.
        assert _paths(got["matches"]) == [str(home / "code" / "walled"), reachable]


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

    def test_search_finds_the_repo_the_sweep_is_too_shallow_to_see(
        self, client, home, marker_git
    ):
        # ~/code/acme/services/api: three levels below a scan root, so
        # /api/repos/suggest cannot offer it and /api/repos/check only answers
        # about it once the user has typed the whole path.
        deep = _marker_repo(home / "code" / "acme" / "services", "api")

        body = client.get("/api/repos/search", params={"q": "api"}).json()

        assert body["home"] == str(home)  # the picker shortens paths against it
        assert _paths(body["matches"]) == [deep]
        assert body["truncated"] is False
        assert set(body["matches"][0]) == {"path", "name", "is_git", "source"}
        assert body["matches"][0]["source"] == "search"

    def test_search_answers_200_while_the_user_is_still_typing(
        self, client, home, marker_git
    ):
        # One character is under the floor. A 4xx per keystroke would light the
        # folder field up red at someone who is mid-word, exactly as it would on
        # /api/repos/check — so this answers an empty list instead.
        _marker_repo(home / "code", "a-repo")

        r = client.get("/api/repos/search", params={"q": "a"})

        assert r.status_code == 200
        assert r.json() == {"matches": [], "truncated": False, "home": str(home)}

    def test_search_that_matches_nothing_says_so_without_erroring(
        self, client, home, marker_git
    ):
        # "Your folder isn't in the places I looked" is an answer the dialog
        # renders as a line of prose pointing at Browse…, not a failure.
        _marker_repo(home / "code", "something-else")

        r = client.get("/api/repos/search", params={"q": "nowhere-near-this"})

        assert r.status_code == 200
        assert r.json()["matches"] == []
        assert r.json()["truncated"] is False

    def test_search_clamps_a_silly_limit_instead_of_refusing_it(
        self, client, home, marker_git
    ):
        # The list is a convenience; the only harm a daft limit does is to a scan
        # budget the caller does not pay for.
        for n in range(3):
            _marker_repo(home / "code", "clamped-%d" % n)

        low = client.get("/api/repos/search", params={"q": "clamped", "limit": 0})
        high = client.get("/api/repos/search", params={"q": "clamped", "limit": 999})

        assert len(low.json()["matches"]) == 1
        assert low.json()["truncated"] is True  # two more were found
        assert len(high.json()["matches"]) == 3

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
