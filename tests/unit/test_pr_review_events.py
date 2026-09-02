"""``session.pr_review_changed`` — "your PR was approved", once, when it is true.

The event exists because the fact has no other home. ``mergeable_state`` says
``blocked`` both for a missing review and for a failing required check, and says
nothing once the approval lands; the stage ladder only knows "there is a PR".
So the verdict is read from the PR's own review list, and the same two rules the
sibling ``session.pr_state_changed`` follows apply here: seed silently, and never
let a failed lookup masquerade as a change.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from backend.session.storage import Status
from backend.web import server
from backend.web.addons import notify
from backend.web.core import agent_state
from backend.web.core import events as events_mod
from backend.web.core import github_pr


# --------------------------------------------------------------------------- #
# helpers (same shapes as test_pr_state_events.py)
# --------------------------------------------------------------------------- #
def _git(*args: str, cwd: str) -> str:
    cp = subprocess.run(
        ["git", "-C", cwd, "-c", "user.email=t@t", "-c", "user.name=t", *args],
        capture_output=True,
        text=True,
    )
    assert cp.returncode == 0, cp.stderr + cp.stdout
    return cp.stdout.strip()


def _repo_with_feature_commit(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    exclude = path / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text(".mindflock_*\n")
    (path / "a.txt").write_text("one\n")
    _git("add", "-A", cwd=str(path))
    _git("commit", "-q", "-m", "init", cwd=str(path))
    _git("checkout", "-q", "-b", "feat", cwd=str(path))
    (path / "b.txt").write_text("two\n")
    _git("add", "-A", cwd=str(path))
    _git("commit", "-q", "-m", "work", cwd=str(path))
    return path


class _FakeInst:
    Program = "bash"
    Path = ""
    InPlace = False

    def __init__(self, wt: str, *, branch: str = "feat", title: str = "t"):
        self.Title = title
        self.Branch = branch
        self.BaseBranch = "main"
        self.Status = Status.Running
        self._wt = wt

    def Started(self):  # noqa: N802
        return True

    def GetWorktreePath(self):  # noqa: N802
        return self._wt


@pytest.fixture
def bus_events():
    seen: list = []
    unsubscribe = events_mod.BUS.subscribe(seen.append)
    yield seen
    unsubscribe()


@pytest.fixture(autouse=True)
def _clear_caches(monkeypatch):
    caches = (
        server._BASE_BRANCH_CACHE,
        server._DIFF_STAT_CACHE,
        server._PR_CACHE,
        server._REVIEW_CACHE,
        server._ORIGIN_SHA_CACHE,
        server._PROBE_CACHE,
        agent_state._LAST_BRANCH,
        agent_state._LAST_PR_STATE,
        agent_state._LAST_PR_REVIEW,
    )
    for cache in caches:
        cache.clear()
    monkeypatch.setattr(server, "_failed_precommit_step", lambda title: "hook")
    yield
    for cache in caches:
        cache.clear()


def _reviews(envelopes):
    return [
        (e["old"], e["new"])
        for e in envelopes
        if e["event"] == "session.pr_review_changed"
    ]


def _poll(inst):
    server._PROBE_CACHE.clear()
    server._REVIEW_CACHE.clear()
    return server._session_stage(inst)


# --------------------------------------------------------------------------- #
# The verdict itself: the latest review per reviewer, nothing else
# --------------------------------------------------------------------------- #
def _decide(monkeypatch, reviews):
    """Run pr_review_state against a canned review list."""
    monkeypatch.setattr(
        github_pr, "repo_ref", lambda wt: github_pr.RemoteRef("o", "r", "o/r")
    )

    async def _token():
        return "t"

    async def _request(method, path, **kw):
        assert path.endswith("/pulls/7/reviews")
        return 200, reviews

    monkeypatch.setattr(github_pr, "api_token", _token)
    monkeypatch.setattr(github_pr, "_request", _request)
    return github_pr.pr_review_state_sync("/wt", 7)


def _review(login, state):
    return {"user": {"login": login}, "state": state}


def test_an_approval_is_an_approval(monkeypatch):
    assert _decide(monkeypatch, [_review("ann", "APPROVED")]) == "approved"


def test_a_comment_is_not_a_decision(monkeypatch):
    # The most common review of all, and the one that must stay silent.
    assert _decide(monkeypatch, [_review("ann", "COMMENTED")]) == ""
    assert _decide(monkeypatch, [_review("ann", "PENDING")]) == ""


def test_changes_requested_outranks_someone_elses_approval(monkeypatch):
    verdict = _decide(
        monkeypatch,
        [_review("ann", "APPROVED"), _review("bo", "CHANGES_REQUESTED")],
    )
    assert verdict == "changes_requested"


def test_only_a_reviewers_latest_review_counts(monkeypatch):
    # Ann asked for changes and then approved the fix: that is an approval.
    verdict = _decide(
        monkeypatch,
        [_review("ann", "CHANGES_REQUESTED"), _review("ann", "APPROVED")],
    )
    assert verdict == "approved"


def test_a_dismissed_approval_stops_counting(monkeypatch):
    verdict = _decide(
        monkeypatch, [_review("ann", "APPROVED"), _review("ann", "DISMISSED")]
    )
    assert verdict == ""


def test_a_failed_lookup_is_never_an_approval(monkeypatch):
    monkeypatch.setattr(
        github_pr, "repo_ref", lambda wt: github_pr.RemoteRef("o", "r", "o/r")
    )

    async def _token():
        return "t"

    async def _boom(method, path, **kw):
        return 403, {"message": "rate limited"}

    monkeypatch.setattr(github_pr, "api_token", _token)
    monkeypatch.setattr(github_pr, "_request", _boom)
    assert github_pr.pr_review_state_sync("/wt", 7) == ""


# --------------------------------------------------------------------------- #
# The event: once per real transition
# --------------------------------------------------------------------------- #
def test_approval_emits_once(tmp_path, monkeypatch, bus_events):
    wt = _repo_with_feature_commit(tmp_path / "r")
    inst = _FakeInst(str(wt))
    verdict = {"value": ""}
    monkeypatch.setattr(
        server, "_pr_info", lambda *a, **k: {"url": "u", "state": "OPEN", "number": 7}
    )
    monkeypatch.setattr(server, "_pr_review_state", lambda *a, **k: verdict["value"])

    _poll(inst)  # no verdict yet — nothing to say
    assert _reviews(bus_events) == []

    verdict["value"] = "approved"
    _poll(inst)  # first sighting SEEDS: a restart must not re-announce it
    assert _reviews(bus_events) == []

    verdict["value"] = "changes_requested"
    _poll(inst)
    assert _reviews(bus_events) == [("approved", "changes_requested")]

    verdict["value"] = "approved"
    _poll(inst)
    assert _reviews(bus_events) == [
        ("approved", "changes_requested"),
        ("changes_requested", "approved"),
    ]

    # Polling again on the same approved PR must not re-announce it.
    _poll(inst)
    assert len(_reviews(bus_events)) == 2


def test_failed_lookup_does_not_withdraw_an_approval(tmp_path, monkeypatch, bus_events):
    wt = _repo_with_feature_commit(tmp_path / "r")
    inst = _FakeInst(str(wt))
    verdict = {"value": "changes_requested"}
    monkeypatch.setattr(
        server, "_pr_info", lambda *a, **k: {"url": "u", "state": "OPEN", "number": 7}
    )
    monkeypatch.setattr(server, "_pr_review_state", lambda *a, **k: verdict["value"])

    _poll(inst)  # seed
    verdict["value"] = ""  # rate limit / token expired / no GitHub remote
    _poll(inst)
    assert _reviews(bus_events) == []
    # ...and the remembered verdict is untouched, so the real approval still fires.
    verdict["value"] = "approved"
    _poll(inst)
    assert _reviews(bus_events) == [("changes_requested", "approved")]


def test_a_closed_pr_is_not_asked_about(tmp_path, monkeypatch, bus_events):
    """An approval on a merged PR is history — and the lookup costs a GitHub
    call per session per poll, so it must not run."""
    wt = _repo_with_feature_commit(tmp_path / "r")
    inst = _FakeInst(str(wt))
    asked: list = []
    monkeypatch.setattr(
        server, "_pr_info", lambda *a, **k: {"url": "u", "state": "MERGED", "number": 7}
    )
    monkeypatch.setattr(
        server,
        "_pr_review_state",
        lambda *a, **k: (asked.append(1), "approved")[1],
    )
    _poll(inst)
    _poll(inst)
    assert asked == []
    assert _reviews(bus_events) == []


def test_branch_switch_reseeds_instead_of_emitting(tmp_path, monkeypatch, bus_events):
    wt = _repo_with_feature_commit(tmp_path / "r")
    inst = _FakeInst(str(wt))
    monkeypatch.setattr(
        server, "_pr_info", lambda *a, **k: {"url": "u", "state": "OPEN", "number": 7}
    )
    monkeypatch.setattr(server, "_pr_review_state", lambda *a, **k: "changes_requested")
    _poll(inst)  # seed on "feat"

    # A different branch is a different pull request, not this one changing its
    # mind — even though the verdict differs.
    _git("checkout", "-q", "-b", "feat2", cwd=str(wt))
    monkeypatch.setattr(server, "_pr_review_state", lambda *a, **k: "approved")
    _poll(inst)
    assert _reviews(bus_events) == []
    assert agent_state._LAST_PR_REVIEW[inst.Title] == ("feat2", "approved")


# --------------------------------------------------------------------------- #
# The PR number the lookup needs, from whichever rung answered
# --------------------------------------------------------------------------- #
def test_pr_number_comes_from_the_field_or_the_url():
    assert server._pr_number({"number": 42}) == 42
    # The `gh` rung returns no number — only the URL carries it.
    assert server._pr_number({"url": "https://github.com/o/r/pull/42"}) == 42
    assert server._pr_number({"url": "https://github.com/o/r/pull/42/files"}) == 42
    assert server._pr_number({"url": "https://example.com/nope"}) is None
    assert server._pr_number(None) is None


# --------------------------------------------------------------------------- #
# The rule both channels apply
# --------------------------------------------------------------------------- #
def test_notify_rule_fires_on_approval_only():
    rule = next(r for r in notify.NOTIFY_RULES if r["id"] == "pr_approved")
    assert rule["event"] == "session.pr_review_changed"
    assert rule["default_enabled"] is True

    assert notify._matches(
        rule,
        {
            "event": "session.pr_review_changed",
            "old": "changes_requested",
            "new": "approved",
        },
    )
    # Changes requested has its own (opt-in) rule; this one must stay quiet.
    assert not notify._matches(
        rule,
        {"event": "session.pr_review_changed", "old": "", "new": "changes_requested"},
    )
    # And a PR merely opening is not an approval.
    assert not notify._matches(
        rule, {"event": "session.pr_state_changed", "old": "", "new": "OPEN"}
    )

    other = next(r for r in notify.NOTIFY_RULES if r["id"] == "pr_changes_requested")
    assert other["default_enabled"] is False
    assert notify._matches(
        other,
        {
            "event": "session.pr_review_changed",
            "old": "approved",
            "new": "changes_requested",
        },
    )


# --------------------------------------------------------------------------- #
# ...and the ways the lookup can fail to be one
# --------------------------------------------------------------------------- #
def _no_calls(monkeypatch):
    """Arm ``_request`` as a tripwire: any HTTP at all fails the test."""
    calls: list = []

    async def _request(method, path, **kw):
        calls.append((method, path, kw))
        return 200, []

    monkeypatch.setattr(github_pr, "_request", _request)
    return calls


def test_a_repo_with_no_github_remote_is_not_asked(monkeypatch):
    """Half the sessions on a machine are on a repo GitHub has never heard of.
    Answering "" is right; spending a request to find that out is not."""
    calls = _no_calls(monkeypatch)
    monkeypatch.setattr(github_pr, "repo_ref", lambda wt: None)
    assert github_pr.pr_review_state_sync("/wt", 7) == ""
    assert calls == []


def test_no_token_means_no_request(monkeypatch):
    calls = _no_calls(monkeypatch)
    monkeypatch.setattr(
        github_pr, "repo_ref", lambda wt: github_pr.RemoteRef("o", "r", "o/r")
    )

    async def _token():
        return ""

    monkeypatch.setattr(github_pr, "api_token", _token)
    assert github_pr.pr_review_state_sync("/wt", 7) == ""
    assert calls == []


def test_no_pr_number_means_no_request(monkeypatch):
    calls = _no_calls(monkeypatch)
    monkeypatch.setattr(
        github_pr, "repo_ref", lambda wt: github_pr.RemoteRef("o", "r", "o/r")
    )
    for number in (None, 0):
        assert github_pr.pr_review_state_sync("/wt", number) == ""
    assert calls == []


@pytest.mark.parametrize(
    "status,body",
    [
        (403, {"message": "API rate limit exceeded"}),
        (401, {"message": "Bad credentials"}),
        (404, {"message": "Not Found"}),
        (200, {"message": "not a list"}),
        (200, None),
        (200, "reviews"),
        (200, [None, 7, "APPROVED"]),
    ],
)
def test_a_body_that_is_not_a_review_list_is_never_a_verdict(monkeypatch, status, body):
    """Every one of these has to be indistinguishable from silence — the caller
    announces an approval to a human, and none of them is one."""
    monkeypatch.setattr(
        github_pr, "repo_ref", lambda wt: github_pr.RemoteRef("o", "r", "o/r")
    )

    async def _token():
        return "t"

    async def _request(method, path, **kw):
        return status, body

    monkeypatch.setattr(github_pr, "api_token", _token)
    monkeypatch.setattr(github_pr, "_request", _request)
    assert github_pr.pr_review_state_sync("/wt", 7) == ""


def test_a_review_with_no_reviewer_is_skipped_not_bucketed_together(monkeypatch):
    """The verdict is keyed by login, so an item with no usable one has to be
    dropped: bucketing them all under "" would let a ghost's CHANGES_REQUESTED
    overwrite another ghost's DISMISSED — and outrank a real approval."""
    verdict = _decide(
        monkeypatch,
        [
            {"user": None, "state": "CHANGES_REQUESTED"},
            {"user": {"login": ""}, "state": "CHANGES_REQUESTED"},
            {"user": {}, "state": "CHANGES_REQUESTED"},
            _review("ann", "APPROVED"),
        ],
    )
    assert verdict == "approved"


def test_the_review_list_is_asked_for_a_page_at_a_time(monkeypatch):
    """One page of 100 is requested and no ``Link`` header is followed, so a PR
    with more than 100 reviews is a known truncation.

    Pinned deliberately: "the latest review per reviewer" is decided from what
    came back, so a silent page limit is the one way this can INVERT a verdict
    (an old CHANGES_REQUESTED surviving on page one while the approval that
    replaced it sits on page two). Changing the request to follow pages is
    fine — changing the page size without deciding about pagination is not.
    """
    seen: list = []
    monkeypatch.setattr(
        github_pr, "repo_ref", lambda wt: github_pr.RemoteRef("o", "r", "o/r")
    )

    async def _token():
        return "t"

    async def _request(method, path, **kw):
        seen.append((path, kw.get("params")))
        return 200, [_review("ann", "APPROVED")]

    monkeypatch.setattr(github_pr, "api_token", _token)
    monkeypatch.setattr(github_pr, "_request", _request)
    assert github_pr.pr_review_state_sync("/wt", 7) == "approved"
    assert len(seen) == 1  # one page, and no Link-header follow-up
    assert seen[0][0].endswith("/pulls/7/reviews")
    assert seen[0][1] == {"per_page": "100"}


def test_the_sync_wrapper_answers_from_inside_a_running_loop(monkeypatch):
    """The stage probe is synchronous and normally runs on a worker thread, but
    a caller on the event-loop thread must get an answer rather than a
    ``RuntimeError`` that would take the whole poll down with it."""
    monkeypatch.setattr(
        github_pr, "repo_ref", lambda wt: github_pr.RemoteRef("o", "r", "o/r")
    )

    async def _token():
        return "t"

    async def _request(method, path, **kw):
        return 200, [_review("ann", "APPROVED")]

    monkeypatch.setattr(github_pr, "api_token", _token)
    monkeypatch.setattr(github_pr, "_request", _request)

    async def _from_the_loop():
        return github_pr.pr_review_state_sync("/wt", 7)

    assert asyncio.run(_from_the_loop()) == "approved"


def test_the_sync_wrapper_swallows_anything_the_lookup_throws(monkeypatch):
    monkeypatch.setattr(
        github_pr, "repo_ref", lambda wt: github_pr.RemoteRef("o", "r", "o/r")
    )

    async def _token():
        raise TimeoutError("the token helper hung")

    monkeypatch.setattr(github_pr, "api_token", _token)
    assert github_pr.pr_review_state_sync("/wt", 7) == ""


# --------------------------------------------------------------------------- #
# The memo in front of it
# --------------------------------------------------------------------------- #
def test_the_verdict_is_asked_for_once_every_two_minutes(monkeypatch):
    """Nothing ACTS on this — it exists to be told to a human once — so it is
    memoized far longer than the merge state. One call per open PR per two
    minutes is what a flock of sessions can afford."""
    asked: list = []
    monkeypatch.setattr(
        _github_pr_of_server(),
        "pr_review_state_sync",
        lambda wt, number: (asked.append(number), "approved")[1],
    )
    info = {"url": "u", "state": "OPEN", "number": 7}
    assert server._pr_review_state("/wt", "feat", info) == "approved"
    assert server._pr_review_state("/wt", "feat", info) == "approved"
    assert asked == [7]
    # ...and `force` is the escape hatch the refresh paths use.
    assert server._pr_review_state("/wt", "feat", info, force=True) == "approved"
    assert asked == [7, 7]


def test_a_pr_with_no_resolvable_number_is_not_asked_about(monkeypatch):
    asked: list = []
    monkeypatch.setattr(
        _github_pr_of_server(),
        "pr_review_state_sync",
        lambda wt, number: (asked.append(number), "approved")[1],
    )
    assert server._pr_review_state("/wt", "feat", {"url": "https://x/y"}) == ""
    assert server._pr_review_state("/wt", "feat", None) == ""
    assert server._pr_review_state("/wt", "", {"number": 7}) == ""
    assert asked == []


def test_the_memo_is_keyed_by_branch_alone(monkeypatch):
    """Two worktrees on identically-named branches SHARE one entry — the same
    key ``_MERGE_STATE_CACHE`` has always used. Pinned rather than fixed because
    it is now what decides whether a "PR approved" push goes out, so a future
    change to the key should be a deliberate one and not a silent widening.
    """
    asked: list = []
    monkeypatch.setattr(
        _github_pr_of_server(),
        "pr_review_state_sync",
        lambda wt, number: (asked.append(wt), "approved")[1],
    )
    info = {"url": "u", "state": "OPEN", "number": 7}
    server._pr_review_state("/wt/one", "shared", info)
    server._pr_review_state("/wt/two", "shared", info)
    assert asked == ["/wt/one"]


def _github_pr_of_server():
    """The module object ``server`` reaches the lookup through (it imports it
    under a private alias)."""
    return server._github_pr


# --------------------------------------------------------------------------- #
# The tracker's own edges
# --------------------------------------------------------------------------- #
def test_an_unnamed_session_or_branch_is_not_even_remembered(bus_events):
    """A session with no title (or a worktree with no branch) has nothing to key
    the memory on, so seeding one would let the NEXT session with that key
    inherit a verdict that was never about it."""
    agent_state._track_pr_review("", "feat", "approved")
    agent_state._track_pr_review("win", "", "approved")
    assert agent_state._LAST_PR_REVIEW == {}
    assert _reviews(bus_events) == []


def test_two_pollers_on_one_session_announce_the_approval_once(bus_events):
    """Every session is polled on its own thread and the flock refresh can
    overlap a manual one, so the same transition really does arrive twice at
    once. It is a notification: exactly one has to survive."""
    import threading

    agent_state._track_pr_review("win", "feat", "changes_requested")  # seed
    barrier = threading.Barrier(8)

    def _poll():
        barrier.wait()
        agent_state._track_pr_review("win", "feat", "approved")

    threads = [threading.Thread(target=_poll) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert _reviews(bus_events) == [("changes_requested", "approved")]


def test_a_broken_bus_never_breaks_the_stage_probe(monkeypatch, bus_events):
    """The stage probe is the session list. An event is decoration on top of it
    and must never be able to take it down."""

    def _boom(*a, **kw):
        raise RuntimeError("the bus is on fire")

    monkeypatch.setattr(events_mod.BUS, "emit", _boom)
    agent_state._track_pr_review("win", "feat", "approved")  # seed
    agent_state._track_pr_review("win", "feat", "changes_requested")  # would emit
    # The memory still moved, so the next poll does not re-attempt the same
    # announcement for ever.
    assert agent_state._LAST_PR_REVIEW["win"] == ("feat", "changes_requested")


# --------------------------------------------------------------------------- #
# The rules, as a set
# --------------------------------------------------------------------------- #
def test_an_empty_verdict_matches_no_rule_at_all():
    """ "" is "nobody has decided OR we could not ask". The tracker already
    refuses to emit it, and the rules are the second line: neither switch may
    ever deliver a push for one."""
    envelope = {"event": "session.pr_review_changed", "old": "approved", "new": ""}
    assert [r["id"] for r in notify.NOTIFY_RULES if notify._matches(r, envelope)] == []


def test_a_transition_only_ever_lights_its_own_rule():
    """The two review rules share an event and differ only in ``new``, so one
    transition must never satisfy both — a user opted into just one of them."""
    for new in ("approved", "changes_requested"):
        matched = [
            r["id"]
            for r in notify.NOTIFY_RULES
            if notify._matches(
                r, {"event": "session.pr_review_changed", "old": "", "new": new}
            )
        ]
        assert matched == [
            "pr_approved" if new == "approved" else "pr_changes_requested"
        ]


def test_every_rule_id_is_unique():
    """Preferences are stored by rule id, so a duplicate would silently shadow a
    user's saved switch — with the enabled state of whichever rule the lookup
    happened to find first."""
    ids = [r["id"] for r in notify.NOTIFY_RULES]
    assert len(ids) == len(set(ids))


def test_the_pr_number_is_only_ever_a_real_one():
    """A number the lookup would put straight into a URL path."""
    assert server._pr_number({"number": 0}) is None
    assert server._pr_number({"number": -1}) is None
    assert server._pr_number({"number": "42"}) is None  # not an int: not trusted
    assert server._pr_number({"number": "42", "url": "https://g/o/r/pull/9"}) == 9
    assert server._pr_number({}) is None
    assert server._pr_number("https://g/o/r/pull/9") is None
