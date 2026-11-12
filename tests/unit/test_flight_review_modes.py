"""Team PR rules as a route (docs/flights.md "Review modes" and "A required
reviewer"): the ``review`` stop's ``mode`` (bugs / security / standards, the
team's rules in ``focus``), the fix-or-rebut hand-off of its findings, and
the ``reviews`` stop's ``require_reviewer`` / ``reviewer_wait_minutes``."""

import asyncio
import datetime as dt
from types import SimpleNamespace

import pytest

from backend.ticket_ingestion import pr_comments as _prc
from backend.ticket_ingestion.models import PRComment, PullRequest
from backend.web.core import flight_driver as D
from backend.web.core import flights as F
from backend.web.core.flight_kinds import review as R

NOW = 1_800_000_000.0
RULES = "R1. Every endpoint logs its outcome.\nR2. Every change ships unit tests."


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_FLIGHTS_DIR", str(tmp_path / "flights"))
    monkeypatch.setenv("MINDFLOCK_AUTOPILOT_FILE", str(tmp_path / "autopilot.json"))
    monkeypatch.setattr(D, "_REVIEWS", {})
    R._FINISHED.clear()
    yield
    R._FINISHED.clear()


def sess(**kw):
    return dict({"present": True, "status": "running", "activity": "idle"}, **kw)


def fly(*steps, session="s"):
    plan = F.save_plan({"name": "P", "steps": list(steps)}, now=NOW)
    return F.take_off(plan, session=session, now=NOW)


def go(f, obs, now):
    a = F.next_action(f, obs, now)
    F.apply(f, a, now)
    return a


# --------------------------------------------------------------------------- #
# review: the mode select
# --------------------------------------------------------------------------- #
def test_the_mode_field_and_its_titles():
    k = {x["kind"]: x for x in F.kinds_dto()["kinds"]}["review"]
    mode = next(x for x in k["fields"] if x["name"] == "mode")
    assert mode["type"] == "select" and mode["default"] == "bugs"
    assert mode["options"] == ["bugs", "security", "standards"]
    titles = {
        m: F.normalize_step({"kind": "review", "config": {"mode": m}})["title"]
        for m in R.MODES
    }
    assert titles == {
        "bugs": "Second opinion",
        "security": "Security review",
        "standards": "Standards review",
    }
    s = F.normalize_step(
        {"kind": "review", "config": {"mode": "Security", "program": "codex"}}
    )
    assert s["config"]["mode"] == "security"
    assert s["title"] == "Security review (codex)"


def test_an_unknown_mode_is_refused_or_read_as_bugs():
    with pytest.raises(F.PlanError) as err:
        F.normalize_step({"kind": "review", "config": {"mode": "style"}}, strict=True)
    assert err.value.field.endswith(".mode")
    s = F.normalize_step({"kind": "review", "config": {"mode": "style"}}, strict=False)
    assert s["config"]["mode"] == "bugs"


def test_the_bugs_brief_is_unchanged():
    text = R.brief("s", "main", "the retry loop")
    assert text == R.brief("s", "main", "the retry loop", "bugs")
    assert text.startswith(
        '[MindFlock route] You are a second-opinion reviewer for the session "s".'
    )
    assert (
        "Look for correctness bugs only: wrong logic, broken edge cases, "
        "crashes, races, lost data, security holes. Ignore style, naming and "
        "refactoring ideas.\n"
    ) in text
    assert text.rstrip().endswith("Also: the retry loop")
    assert "team's rules" not in text and "rebuttal" not in text


@pytest.mark.parametrize("mode", R.MODES)
def test_every_mode_keeps_the_report_format(mode):
    text = R.brief("s", "develop", RULES, mode)
    assert "git diff develop...HEAD" in text and "Change NO files" in text
    assert "exactly `No findings`" in text
    assert "numbered list of at most %d" % R.MAX_FINDINGS in text
    assert "\n1. path/to/file.py:123 — " in text


def test_the_security_brief_has_its_checklist():
    text = R.brief("s", "main", mode="security")
    assert "security reviewer" in text
    for word in (
        "authentication and authorization",
        "secrets and credentials",
        "injection: SQL, shell / command, template",
        "SSRF",
        "path traversal",
        "crypto misuse",
        "IAM, infrastructure-as-code and RBAC",
        "unsafe deserialization",
        "sensitive data (credentials, personal data) in logs",
        "dependency risk",
    ):
        assert word in text, word
    assert "Report only real, exploitable issues" in text and "file:line" in text
    assert "Also:" not in text and "team's rules" not in text
    assert R.REBUTTALS in text


def test_the_security_brief_puts_the_rules_first():
    text = R.brief("s", "main", RULES, "security")
    head = text.index("The team's rules for this review")
    assert head < text.index(RULES) < text.index("authentication and authorization")
    assert "Also:" not in text


def test_the_standards_brief_puts_the_rules_first_and_never_ignores_style():
    text = R.brief("s", "main", RULES, "standards")
    rules = text.index("The team's rules for this review")
    assert rules < text.index(RULES) < text.index("observability")
    assert "ignore style" not in text.lower() and "Also:" not in text
    for word in (
        "code quality",
        "meaningful logging, metrics, and dashboards / alerts",
        "unit tests",
        "tested locally",
        "anything else the rules demand",
    ):
        assert word in text, word
    # No rules given: it still reviews, against sound standards.
    bare = R.brief("s", "main", "", "standards")
    assert "No team rules were given" in bare and "team's rules for" not in bare


def test_a_whole_rules_document_survives_normalize_and_save():
    focus = ("Rule %03d: every handler logs and is unit tested.\n" * 120)[:5000]
    assert len(focus.strip()) > 4900
    s = F.normalize_step(
        {"kind": "review", "config": {"mode": "standards", "focus": focus}},
        strict=True,
    )
    assert s["config"]["focus"] == focus.strip()
    plan = F.save_plan(
        {
            "name": "Rules",
            "steps": [
                {"kind": "agent", "config": {"prompt": "x"}},
                {"kind": "review", "config": {"mode": "standards", "focus": focus}},
            ],
        },
        now=NOW,
    )
    kept = F.get_plan(plan["id"])
    assert kept["steps"][1]["config"]["focus"] == focus.strip()
    assert focus.strip() in R.brief(
        "s", "main", kept["steps"][1]["config"]["focus"], "standards"
    )


def test_run_sends_the_modes_brief(monkeypatch):
    f = fly({"kind": "review", "config": {"mode": "standards", "focus": RULES}})
    calls = []

    async def _spawn(flight, step, prompt, **kw):
        calls.append(prompt)
        return "review-1"

    monkeypatch.setattr(D, "spawn_worker", _spawn)
    monkeypatch.setattr(
        D,
        "_server",
        lambda: SimpleNamespace(
            ENGINE=SimpleNamespace(instances={"s": SimpleNamespace(BaseBranch="main")})
        ),
    )
    out = asyncio.run(R._run(f, f["steps"][0], {"action": "run", "note": ""}, NOW))
    assert out["note"] == "asking review-1 for a standards review"
    assert calls == [R.brief("s", "main", RULES, "standards")]


def _findings(f, text, now=NOW + 10):
    """The review stop's reviewer finished with ``text``."""
    a = F.next_action(f, {"session": sess()}, NOW)
    F.apply(f, dict(a, detail={"children": ["review-1"], "base": "main"}), NOW)
    entry = {
        "title": "review-1",
        "state": "done",
        "report": {"id": "m1", "status": "done", "summary": text, "ts": NOW + 5},
    }
    review = {"entry": entry, "text": text, "report": True, "closed": "deleted"}
    return go(f, {"session": sess(), "review": review}, now)


@pytest.mark.parametrize(
    "mode,noun",
    [
        ("bugs", "second opinion"),
        ("security", "security review"),
        ("standards", "standards review"),
    ],
)
def test_findings_to_the_agent_must_be_fixed_or_rebutted(mode, noun):
    f = fly({"kind": "review", "config": {"mode": mode}, "on_fail": "agent"})
    text = "1. api.py:12 — the handler logs nothing\n2. api.py:40 — no unit test"
    a = _findings(f, text)
    assert a["action"] == "fail" and a["then"] == "agent"
    assert a["reason"].startswith("the %s has 2 findings:" % noun)
    note = D._fix_note(f["steps"][0], a)
    assert "api.py:40 — no unit test" in note
    assert "must be FIXED, or answered with a written rebuttal" in note
    assert "for the PR's description" in note and "commit message" in note
    assert note.rstrip().endswith("the route asks the reviewer to check your fixes.")
    # The manual "send it to the agent" path carries only the stored reason.
    reason = f["step_states"][f["steps"][0]["id"]]["detail"]["reason"]
    assert "written rebuttal" in D._fix_note(f["steps"][0], {"reason": reason})


def test_other_fails_are_not_asked_for_rebuttals():
    step = F.normalize_step({"kind": "review", "config": {"mode": "security"}})
    note = D._fix_note(step, {"reason": "the reviewer did not finish"})
    assert "rebuttal" not in note
    assert note.endswith("Fix it; the route retries the stop once you are done.")
    test_step = F.normalize_step({"kind": "test", "config": {}})
    assert "rebuttal" not in D._fix_note(
        test_step, {"reason": "the second opinion has 2 findings:"}
    )


# --------------------------------------------------------------------------- #
# reviews: a required reviewer
# --------------------------------------------------------------------------- #
def test_the_reviews_fields():
    k = {x["kind"]: x for x in F.kinds_dto()["kinds"]}["reviews"]
    names = {x["name"]: x for x in k["fields"]}
    assert names["require_reviewer"]["type"] == "text"
    assert names["require_reviewer"]["default"] == ""
    assert names["reviewer_wait_minutes"]["default"] == 10
    raw = {
        "kind": "reviews",
        "config": {
            "require_reviewer": " CodeRabbit ",
            "reviewer_wait_minutes": "0",
        },
    }
    # A stored plan is read leniently (clamped); a save or start refuses.
    c = F.normalize_step(raw, strict=False)["config"]
    assert c["require_reviewer"] == "CodeRabbit" and c["reviewer_wait_minutes"] == 1
    with pytest.raises(F.PlanError, match="reviewer_wait_minutes"):
        F.normalize_step(raw)


H1 = "a" * 40  # the PR's head when the stop begins
H2 = "b" * 40  # the head after the stop's own push


def rv(head=H1, reviewed=False, any_=False, skipped="", since=NOW):
    """The driver's ``_reviewer_on_head`` answer."""
    return {
        "head": head,
        "since": since,
        "reviewed": reviewed,
        "any": any_,
        "skipped": skipped,
    }


def watching(session="s", **config):
    """A reviews stop watching an open PR with nothing on it, from NOW."""
    f = fly({"kind": "reviews", "config": config}, session=session)
    a = F.next_action(f, {"session": sess()}, NOW)
    F.apply(f, a, NOW)
    o = {
        "session": sess(pr_url="https://pr/1"),
        "reviews": {"ids": [], "reviewer": rv()},
    }
    a = go(f, o, NOW)
    assert a["action"] == "wait"
    return f, o


def detail(f):
    return f["step_states"][f["steps"][0]["id"]]["detail"]


def with_rv(o, **kw):
    return dict(o, reviews={"ids": [], "reviewer": rv(**kw)})


def test_a_short_quiet_still_waits_for_the_required_reviewer():
    f, o = watching(require_reviewer="coderabbit", quiet_minutes=5)
    assert detail(f)["pr_since"] == NOW and detail(f)["seeded"] is True
    a = go(f, o, NOW + 1)
    assert a["note"] == "waiting for coderabbit to review aaaaaaa"
    assert detail(f)["head"] == H1 and detail(f)["head_since"] == NOW
    # Past quiet_minutes on an empty PR: still waiting for the reviewer.
    a = go(f, o, NOW + 6 * 60)
    assert a["action"] == "wait" and a["note"].startswith("waiting for coderabbit")
    assert "of 10 min" in a["note"]
    # Without one, the same PR settles at 5 min (unchanged).
    g, o2 = watching("t", quiet_minutes=5)
    assert "pr_since" not in detail(g)
    assert F.next_action(g, o2, NOW + 6 * 60)["action"] == "done"


def test_the_required_reviewer_never_coming_fails_the_stop():
    f, o = watching(require_reviewer="coderabbit", reviewer_wait_minutes=10)
    assert go(f, o, NOW + 9 * 60)["action"] == "wait"
    a = F.next_action(f, o, NOW + 10 * 60)
    assert a["action"] == "fail"
    assert a["reason"] == "coderabbit never reviewed this PR — it needs a human review"
    # GitHub could not be asked: it says so instead.
    unknown = dict(o, reviews={"ids": [], "reviewer": None})
    a = F.next_action(f, unknown, NOW + 10 * 60)
    assert a["action"] == "fail" and a["reason"].startswith(
        "could not tell whether coderabbit reviewed"
    )


def test_a_review_of_an_older_commit_does_not_open_the_gate():
    """Bug B: on a PR the reviewer reviewed days ago, the 5-minute quiet ran
    out before it had looked at the commit the route just pushed."""
    f, o = watching(require_reviewer="coderabbit", quiet_minutes=5)
    old = with_rv(o, any_=True)  # it posted, but never on this head
    go(f, old, NOW + 1)
    for t in (5 * 60, 6 * 60, 9 * 60):
        a = go(f, old, NOW + t)
        assert a["action"] == "wait" and a["note"].startswith(
            "waiting for coderabbit to review aaaaaaa"
        )
    a = F.next_action(f, old, NOW + 10 * 60)
    assert a["action"] == "fail"
    assert a["reason"] == (
        "coderabbit never reviewed this PR's latest commit (aaaaaaa) — it needs "
        "a human review"
    )


def test_settles_once_the_reviewer_reviewed_the_head_and_the_pr_went_quiet():
    f, o = watching(require_reviewer="coderabbit", quiet_minutes=5)
    go(f, o, NOW + 1)
    a = go(f, with_rv(o, reviewed=True, any_=True), NOW + 3 * 60)
    assert a["action"] == "wait" and a["note"].startswith("coderabbit reviewed aaaaaaa")
    assert detail(f)["reviewer_head"] == H1
    assert detail(f)["quiet_since"] == NOW + 3 * 60
    # The quiet clock starts at its review, not at the stop's start; a tick
    # GitHub could not answer keeps the gate open.
    for after in (with_rv(o, reviewed=True), dict(o, reviews={"ids": []})):
        a = F.next_action(f, after, NOW + 6 * 60)
        assert a["action"] == "wait" and a["note"].startswith("quiet for")
    a = F.next_action(f, with_rv(o, reviewed=True), NOW + 8 * 60)
    assert a["action"] == "done" and a["note"] == "no new review comments for 5 min"


def test_a_head_reviewed_before_the_stop_began_opens_the_gate_at_once():
    f, o = watching(require_reviewer="coderabbit", quiet_minutes=5)
    a = go(f, with_rv(o, reviewed=True, any_=True), NOW + 1)
    assert a["note"].startswith("coderabbit reviewed aaaaaaa")
    assert detail(f)["head"] == H1 and detail(f)["reviewer_head"] == H1
    assert F.next_action(f, with_rv(o, reviewed=True), NOW + 302)["action"] == "done"


def test_the_stops_own_push_waits_for_a_review_of_the_new_head():
    f, o = watching(require_reviewer="coderabbit", quiet_minutes=5)
    go(f, with_rv(o, reviewed=True, any_=True), NOW + 1)
    # A round was sent, fixed and pushed: back to watching from NOW + 600.
    F.apply(
        f,
        {"action": "wait", "note": "", "detail": {"phase": "watch"}},
        NOW + 600,
    )
    F.apply(
        f, {"action": "wait", "note": "", "detail": {"quiet_since": NOW + 600}}, NOW
    )
    pushed = with_rv(o, head=H2, any_=True, since=NOW + 610)
    a = go(f, pushed, NOW + 610)
    assert a["note"] == "waiting for coderabbit to review bbbbbbb"
    assert detail(f)["head"] == H2 and detail(f)["head_since"] == NOW + 610
    # The quiet window alone never finishes it now.
    a = go(f, pushed, NOW + 600 + 6 * 60)
    assert a["action"] == "wait" and a["note"].startswith(
        "waiting for coderabbit to review bbbbbbb"
    )
    a = go(f, with_rv(o, head=H2, reviewed=True, any_=True), NOW + 600 + 7 * 60)
    assert a["note"].startswith("coderabbit reviewed bbbbbbb")
    assert detail(f)["quiet_since"] == NOW + 600 + 7 * 60
    reviewed = with_rv(o, head=H2, reviewed=True)
    assert F.next_action(f, reviewed, NOW + 600 + 11 * 60)["action"] == "wait"
    assert F.next_action(f, reviewed, NOW + 600 + 12 * 60)["action"] == "done"
    # Out of time on the new head: the old head's review does not count.
    g, o2 = watching("t", require_reviewer="coderabbit", reviewer_wait_minutes=10)
    go(g, with_rv(o2, reviewed=True, any_=True), NOW + 1)
    go(g, with_rv(o2, head=H2, any_=True, since=NOW + 60), NOW + 60)
    a = F.next_action(g, with_rv(o2, head=H2, any_=True), NOW + 60 + 10 * 60)
    assert a["action"] == "fail" and "latest commit (bbbbbbb)" in a["reason"]


def test_a_skipped_review_is_not_a_review_and_says_what_it_posted():
    f, o = watching(require_reviewer="coderabbit", reviewer_wait_minutes=10)
    skip = "Review skipped: Auto reviews are disabled on this repository."
    skipped = with_rv(o, any_=True, skipped=skip)
    go(f, skipped, NOW + 1)
    a = go(f, skipped, NOW + 60)
    assert a["action"] == "wait"
    assert 'it posted "Review skipped: Auto reviews are disabled' in a["note"]
    a = F.next_action(f, skipped, NOW + 10 * 60)
    assert a["action"] == "fail"
    assert a["reason"] == (
        'coderabbit never reviewed this PR — it posted "%s" instead — it needs a '
        "human review" % skip
    )


def test_the_reviewers_comments_go_to_the_agent_first():
    f, o = watching(require_reviewer="coderabbit", quiet_minutes=5)
    obs = dict(o, reviews={"ids": ["7"], "reviewer": rv(reviewed=True)})
    a = F.next_action(f, obs, NOW + 60)
    assert a["action"] == "run" and a["phase"] == "send" and a["comments"] == ["7"]


def test_rounds_exhausted_needs_a_human():
    f, o = watching(require_reviewer="coderabbit", max_rounds=2)
    F.apply(f, {"action": "wait", "note": "", "detail": {"review_rounds": 2}}, NOW)
    obs = dict(o, reviews={"ids": ["8"], "reviewer": rv(reviewed=True)})
    a = F.next_action(f, obs, NOW + 60)
    assert a["action"] == "fail"
    assert a["reason"] == "comments still open after 2 rounds — it needs a human review"


# --------------------------------------------------------------------------- #
# reviews: the driver asks GitHub whether the reviewer reviewed the head
# --------------------------------------------------------------------------- #
def _pr():
    return PullRequest(
        1, "feat", "abc", "main", "T", "https://pr/1", "", dt.datetime.now(), repo="o/r"
    )


def _comment(i, author, created=None):
    return PRComment(
        id=i, kind="review", author=author, body="b", url="u", created_at=created
    )


@pytest.fixture
def github(monkeypatch):
    """Fake PR lookups: ``state["comments"]`` is the actionable list,
    ``state["reviewer"]`` what ``_reviewer_on_head`` answers (its calls in
    ``state["calls"]``), ``state["pr"]`` False = no PR found."""
    state = {"comments": [], "reviewer": rv(), "calls": [], "pr": True}

    async def _pr_of(sess, flight):
        return (_pr(), "o/r") if state["pr"] else (None, "")

    async def _fetch(pr, cfg):
        if isinstance(state["comments"], Exception):
            raise state["comments"]
        return list(state["comments"])

    async def _on_head(pr, who, known, known_since, now):
        state["calls"].append((who, known, known_since, now))
        if isinstance(state["reviewer"], Exception):
            raise state["reviewer"]
        return state["reviewer"]

    monkeypatch.setattr(D, "_pr_of", _pr_of)
    monkeypatch.setattr(_prc, "fetch_actionable_comments", _fetch)
    monkeypatch.setattr(D, "_reviewer_on_head", _on_head)
    return state


def _obs(f, now=NOW):
    return asyncio.run(D._reviews_obs(f, f["steps"][0], sess(pr_url="u"), now))


def test_the_driver_reports_the_reviewer_on_the_head(github):
    f = fly({"kind": "reviews", "config": {"require_reviewer": "CodeRabbit"}})
    assert _obs(f) == {"ids": [], "created": {}, "reviewer": rv()}
    assert github["calls"] == [("CodeRabbit", "", 0.0, NOW)]
    # Within the minute: the cache answers.
    github["reviewer"] = rv(reviewed=True)
    assert _obs(f, NOW + 30)["reviewer"] == rv()
    assert _obs(f, NOW + 61)["reviewer"] == rv(reviewed=True)
    # The head it knows (and since when) goes along; it keeps asking once
    # reviewed — a push moves the head.
    d = f["step_states"][f["steps"][0]["id"]]["detail"]
    d.update(head=H1, head_since=NOW + 5, reviewer_head=H1)
    _obs(f, NOW + 200)
    assert github["calls"][-1] == ("CodeRabbit", H1, NOW + 5, NOW + 200)
    # The quiet clock restarted (the stop's own push): asked afresh at once.
    n = len(github["calls"])
    d["quiet_since"] = NOW + 210
    _obs(f, NOW + 211)
    assert len(github["calls"]) == n + 1


def test_the_driver_never_asks_an_unrequired_reviewer(github):
    g = fly({"kind": "reviews", "config": {}}, session="t")
    assert _obs(g) == {"ids": [], "created": {}} and github["calls"] == []


def test_a_failed_ask_is_unknown_not_never(github):
    github["reviewer"] = RuntimeError("502")
    f = fly({"kind": "reviews", "config": {"require_reviewer": "coderabbit"}})
    assert _obs(f) == {"ids": [], "created": {}, "reviewer": None}


def test_the_driver_says_why_the_comments_could_not_be_read(github):
    github["pr"] = False
    f = fly({"kind": "reviews", "config": {}})
    out = asyncio.run(
        D._reviews_obs(f, f["steps"][0], sess(pr_url="u", branch="feat"), NOW)
    )
    assert out["ids"] is None
    assert out["why"] == "found no pull request for branch feat on GitHub"
    github["pr"], github["comments"] = True, RuntimeError("GraphQL 502")
    g = fly({"kind": "reviews", "config": {}}, session="t")
    out = _obs(g)
    assert out["ids"] is None and out["why"] == "couldn't ask GitHub: GraphQL 502"


def test_the_driver_reports_when_each_comment_was_created(github):
    github["comments"] = [
        _comment(5, "coderabbitai[bot]", "2027-01-15T08:00:00Z"),
        _comment(6, "alice"),
    ]
    f = fly({"kind": "reviews", "config": {}})
    out = _obs(f)
    assert out["ids"] == ["5", "6"]
    want = dt.datetime(2027, 1, 15, 8, tzinfo=dt.timezone.utc).timestamp()
    assert out["created"] == {"5": want, "6": 0.0}


def test_reviewer_matching_is_a_case_insensitive_substring():
    assert D._is_reviewer("coderabbitai[bot]", "CodeRabbit")
    assert D._is_reviewer("CodeRabbitAI", "coderabbit")
    assert not D._is_reviewer("alice", "coderabbit")
    assert not D._is_reviewer("alice", "")


class _Resp:
    def __init__(self, data, status=200, links=None):
        self.status, self._data, self.links = status, data, links or {}

    async def json(self):
        return self._data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Http:
    def __init__(self, pages, seen):
        self.pages, self.seen = pages, seen

    def get(self, url, **kw):
        self.seen.append(url)
        return _Resp(*self.pages[url.rsplit("/", 1)[-1]])

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _iso(t):
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


CR = {"login": "coderabbitai[bot]"}
OLD = "c" * 40


def summary(head):
    """CodeRabbit's summary comment after a review with nothing to flag, as
    seen on a real PR (no review object for that commit, only this edit)."""
    return (
        '<!-- final_review_risk_coverage:{"sourceCommitId":"%s",'
        '"coveredCommitId":"%s","kind":"reviewed"} -->\n'
        "<details>\n<summary>📥 Commits</summary>\n\n"
        "Reviewing files that changed from the base of the PR and between "
        "%s and %s.\n\n</details>\n\n"
        "No actionable comments were generated in the recent review. 🎉\n"
        % (head, head, "d" * 40, head)
    )


def processing(head):
    """Its in-progress notice, which names the commit it is about to read."""
    return (
        "> [!NOTE]\n> Currently processing new changes in this PR. This may "
        "take a few minutes, please wait...\n>\n> Reviewing files that "
        "changed from the base of the PR and between %s and %s.\n" % (OLD, head)
    )


SKIP_BODY = (
    "<!-- This is an auto-generated comment: skip review by coderabbit.ai -->\n"
    "> [!IMPORTANT]\n> ## Review skipped\n>\n"
    "> Auto reviews are disabled on this repository.\n"
)


@pytest.mark.parametrize(
    "reviews,comments,want",
    [
        # A review of the head (inline comments or none) is a review of it.
        ([{"user": CR, "state": "COMMENTED", "commit_id": H1}], [], (True, True, "")),
        # Its review of an older commit: it posted, but not on this head.
        (
            [{"user": CR, "state": "COMMENTED", "commit_id": "c" * 40}],
            [],
            (False, True, ""),
        ),
        # A pending (unsubmitted) review never counts.
        ([{"user": CR, "state": "PENDING", "commit_id": H1}], [], (False, True, "")),
        # A conversation comment after the stop saw the head: a review.
        (
            [],
            [
                {
                    "user": CR,
                    "body": "No actionable comments",
                    "created_at": _iso(NOW + 60),
                }
            ],
            (True, True, ""),
        ),
        # … but not one from before it (about an older head), nor an edit.
        (
            [],
            [
                {
                    "user": CR,
                    "body": "Summary",
                    "created_at": _iso(NOW - 60),
                    "updated_at": _iso(NOW + 60),
                }
            ],
            (False, True, ""),
        ),
        # An acknowledgement is not a review.
        (
            [],
            [
                {
                    "user": CR,
                    "body": "✅ Actions performed\n\nReview triggered.",
                    "created_at": _iso(NOW + 60),
                }
            ],
            (False, True, ""),
        ),
        # A summary comment EDITED to name the head (CodeRabbit with nothing
        # to flag posts no review object): a review of the head.
        (
            [],
            [
                {
                    "user": CR,
                    "body": summary(H1),
                    "created_at": _iso(NOW - 86400),
                    "updated_at": _iso(NOW + 60),
                }
            ],
            (True, True, ""),
        ),
        # … not when it names only an older commit.
        (
            [],
            [
                {
                    "user": CR,
                    "body": summary(OLD),
                    "created_at": _iso(NOW - 86400),
                    "updated_at": _iso(NOW + 60),
                }
            ],
            (False, True, ""),
        ),
        # … and never a "processing" notice, even one naming the head.
        (
            [],
            [
                {
                    "user": CR,
                    "body": processing(H1),
                    "created_at": _iso(NOW - 86400),
                    "updated_at": _iso(NOW + 60),
                }
            ],
            (False, True, ""),
        ),
        # A skip notice is not a review; it is what it posted instead.
        (
            [],
            [{"user": CR, "body": SKIP_BODY, "created_at": _iso(NOW + 60)}],
            (
                False,
                True,
                "Review skipped: Auto reviews are disabled on this repository.",
            ),
        ),
        (
            [],
            [
                {
                    "user": CR,
                    "body": "> [!WARNING]\n> ## Rate limit exceeded\n> wait 5 minutes",
                    "created_at": _iso(NOW + 60),
                }
            ],
            (False, True, "Rate limit exceeded: wait 5 minutes"),
        ),
        # Someone else's review is not the required reviewer's.
        (
            [{"user": {"login": "alice"}, "state": "APPROVED", "commit_id": H1}],
            [{"user": None}],
            (False, False, ""),
        ),
    ],
)
def test_reviewer_on_head_reads_the_head_reviews_and_conversation(
    monkeypatch, reviews, comments, want
):
    import aiohttp

    from backend.ticket_ingestion import github_auth

    seen: list = []
    pages = {
        "1": ({"head": {"sha": H1}},),
        "reviews": (reviews,),
        "comments": (comments,),
    }
    monkeypatch.setattr(aiohttp, "ClientSession", lambda **kw: _Http(pages, seen))

    async def _token(cfg=None):
        return "t"

    monkeypatch.setattr(github_auth, "resolve_token", _token)
    out = asyncio.run(D._reviewer_on_head(_pr(), "coderabbit", "", 0.0, NOW))
    assert (out["reviewed"], out["any"], out["skipped"]) == want
    assert out["head"] == H1 and out["since"] == NOW
    assert seen == [
        "https://api.github.com/repos/o/r/pulls/1",
        "https://api.github.com/repos/o/r/pulls/1/reviews",
        "https://api.github.com/repos/o/r/issues/1/comments",
    ]


def test_reviewer_on_head_keeps_the_known_heads_clock_and_pages(monkeypatch):
    import aiohttp

    from backend.ticket_ingestion import github_auth

    seen: list = []
    nxt = {"next": {"url": "https://api.github.com/x/reviews2"}}
    pages = {
        "1": ({"head": {"sha": H1}},),
        "reviews": ([{"user": CR, "commit_id": "c" * 40}], 200, nxt),
        "reviews2": ([{"user": CR, "state": "COMMENTED", "commit_id": H1}],),
        "comments": ([],),
    }
    monkeypatch.setattr(aiohttp, "ClientSession", lambda **kw: _Http(pages, seen))

    async def _token(cfg=None):
        return ""

    monkeypatch.setattr(github_auth, "resolve_token", _token)
    out = asyncio.run(D._reviewer_on_head(_pr(), "coderabbit", H1, NOW - 99, NOW))
    assert out["since"] == NOW - 99 and out["reviewed"] is True
    assert "https://api.github.com/x/reviews2" in seen
    # A new head: its clock starts now.
    out = asyncio.run(D._reviewer_on_head(_pr(), "coderabbit", H2, NOW - 99, NOW))
    assert out["since"] == NOW
    # GitHub refusing is an error ("could not tell"), never "not reviewed".
    pages["1"] = ({}, 502)
    with pytest.raises(RuntimeError):
        asyncio.run(D._reviewer_on_head(_pr(), "coderabbit", H1, NOW, NOW))
