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
    assert note.rstrip().endswith("the route asks for a fresh review.")
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


def watching(session="s", **config):
    """A reviews stop watching an open PR with nothing on it, from NOW."""
    f = fly({"kind": "reviews", "config": config}, session=session)
    a = F.next_action(f, {"session": sess()}, NOW)
    F.apply(f, a, NOW)
    o = {
        "session": sess(pr_url="https://pr/1"),
        "reviews": {"ids": [], "reviewer": False},
    }
    a = go(f, o, NOW)
    assert a["action"] == "wait"
    return f, o


def detail(f):
    return f["step_states"][f["steps"][0]["id"]]["detail"]


def test_a_short_quiet_still_waits_for_the_required_reviewer():
    f, o = watching(require_reviewer="coderabbit", quiet_minutes=5)
    assert detail(f)["pr_since"] == NOW and detail(f)["seeded"] is True
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


def test_settles_once_the_reviewer_posted_and_the_pr_went_quiet():
    f, o = watching(require_reviewer="coderabbit", quiet_minutes=5)
    posted = dict(o, reviews={"ids": [], "reviewer": True})
    a = go(f, posted, NOW + 3 * 60)
    assert a["action"] == "wait" and a["note"].startswith("coderabbit reviewed")
    assert detail(f)["reviewer_seen"] is True
    assert detail(f)["quiet_since"] == NOW + 3 * 60
    # The quiet clock starts at its review, not at the stop's start.
    after = dict(o, reviews={"ids": []})  # the driver stops asking once seen
    a = go(f, after, NOW + 6 * 60)
    assert a["action"] == "wait" and a["note"].startswith("quiet for")
    a = F.next_action(f, after, NOW + 8 * 60)
    assert a["action"] == "done" and a["note"] == "no new review comments for 5 min"


def test_the_reviewers_comments_go_to_the_agent_first():
    f, o = watching(require_reviewer="coderabbit", quiet_minutes=5)
    a = F.next_action(f, dict(o, reviews={"ids": ["7"], "reviewer": True}), NOW + 60)
    assert a["action"] == "run" and a["phase"] == "send" and a["comments"] == ["7"]


def test_rounds_exhausted_needs_a_human():
    f, o = watching(require_reviewer="coderabbit", max_rounds=2)
    F.apply(f, {"action": "wait", "note": "", "detail": {"review_rounds": 2}}, NOW)
    a = F.next_action(f, dict(o, reviews={"ids": ["8"], "reviewer": True}), NOW + 60)
    assert a["action"] == "fail"
    assert a["reason"] == "comments still open after 2 rounds — it needs a human review"


# --------------------------------------------------------------------------- #
# reviews: the driver asks GitHub whether the reviewer posted
# --------------------------------------------------------------------------- #
def _pr():
    return PullRequest(
        1, "feat", "abc", "main", "T", "https://pr/1", "", dt.datetime.now(), repo="o/r"
    )


def _comment(i, author):
    return PRComment(id=i, kind="review", author=author, body="b", url="u")


@pytest.fixture
def github(monkeypatch):
    """Fake PR lookups: ``state["comments"]`` is the actionable list,
    ``state["posted"]`` what ``_reviewer_posted`` answers (and counts)."""
    state = {"comments": [], "posted": False, "asked": 0}

    async def _pr_of(sess, flight):
        return _pr(), "o/r"

    async def _fetch(pr, cfg):
        return list(state["comments"])

    async def _posted(pr, who):
        state["asked"] += 1
        if isinstance(state["posted"], Exception):
            raise state["posted"]
        return state["posted"]

    monkeypatch.setattr(D, "_pr_of", _pr_of)
    monkeypatch.setattr(_prc, "fetch_actionable_comments", _fetch)
    monkeypatch.setattr(D, "_reviewer_posted", _posted)
    return state


def _obs(f, now=NOW):
    return asyncio.run(D._reviews_obs(f, f["steps"][0], sess(pr_url="u"), now))


def test_the_driver_reports_whether_the_reviewer_posted(github):
    f = fly({"kind": "reviews", "config": {"require_reviewer": "CodeRabbit"}})
    assert _obs(f) == {"ids": [], "reviewer": False} and github["asked"] == 1
    # Within the minute: the cache answers.
    github["posted"] = True
    assert _obs(f, NOW + 30) == {"ids": [], "reviewer": False}
    assert _obs(f, NOW + 61) == {"ids": [], "reviewer": True}
    # An inline comment of its own answers without a second request.
    github["comments"] = [_comment(5, "coderabbitai[bot]")]
    asked = github["asked"]
    assert _obs(f, NOW + 200) == {"ids": ["5"], "reviewer": True}
    assert github["asked"] == asked


def test_the_driver_stops_asking_once_seen_and_never_asks_unrequired(github):
    f = fly({"kind": "reviews", "config": {"require_reviewer": "coderabbit"}})
    f["step_states"][f["steps"][0]["id"]]["detail"]["reviewer_seen"] = True
    assert _obs(f) == {"ids": []} and github["asked"] == 0
    g = fly({"kind": "reviews", "config": {}}, session="t")
    assert _obs(g) == {"ids": []} and github["asked"] == 0


def test_a_failed_ask_is_unknown_not_never(github):
    github["posted"] = RuntimeError("502")
    f = fly({"kind": "reviews", "config": {"require_reviewer": "coderabbit"}})
    assert _obs(f) == {"ids": [], "reviewer": None}


def test_reviewer_matching_is_a_case_insensitive_substring():
    assert D._is_reviewer("coderabbitai[bot]", "CodeRabbit")
    assert D._is_reviewer("CodeRabbitAI", "coderabbit")
    assert not D._is_reviewer("alice", "coderabbit")
    assert not D._is_reviewer("alice", "")


class _Resp:
    def __init__(self, data, status=200):
        self.status, self._data = status, data

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


@pytest.mark.parametrize(
    "reviews,comments,want",
    [
        ([{"user": {"login": "coderabbitai[bot]"}, "state": "COMMENTED"}], [], True),
        (
            [{"user": {"login": "alice"}}],
            [{"user": {"login": "coderabbitai[bot]"}}],
            True,
        ),
        ([{"user": {"login": "alice"}}], [{"user": None}], False),
    ],
)
def test_reviewer_posted_reads_reviews_and_conversation_comments(
    monkeypatch, reviews, comments, want
):
    import aiohttp

    from backend.ticket_ingestion import github_auth

    seen: list = []
    pages = {"reviews": (reviews,), "comments": (comments,)}
    monkeypatch.setattr(aiohttp, "ClientSession", lambda **kw: _Http(pages, seen))

    async def _token(cfg=None):
        return "t"

    monkeypatch.setattr(github_auth, "resolve_token", _token)
    assert asyncio.run(D._reviewer_posted(_pr(), "coderabbit")) is want
    assert seen[0] == "https://api.github.com/repos/o/r/pulls/1/reviews"
    if not want:
        assert seen[1] == "https://api.github.com/repos/o/r/issues/1/comments"
