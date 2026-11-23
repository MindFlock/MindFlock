"""A route's CodeRabbit loop (a Reviews stop) on a real PR got stuck three
ways (docs/flights.md "Reviews: done when clean", docs/pr-review-rounds.md
"CodeRabbit's review bodies"):

1. CodeRabbit's nitpick / outside-diff-range / duplicate comments live only
   in its review BODY, and the whole body was dropped as an auto-generated
   summary — those items never reached the agent;
2. the agent was handed its own earlier reply and a ticket-link bot's
   notice as "requests";
3. after a round the agent pushed itself (or that changed nothing), the
   stop's push phase waited on a lane with nothing to push ("already at pr
   — waiting for new work") until the lane's 2-hour arm deadline failed it,
   and nothing ended the loop when CodeRabbit's review of the new head came
   back clean.

The CodeRabbit bodies below are synthetic, shaped like the real payloads.
"""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from backend.ticket_ingestion import coderabbit as CR
from backend.ticket_ingestion import pr_comments as PC
from backend.ticket_ingestion import pr_replies, pr_rounds
from backend.ticket_ingestion.config import GithubConfig
from backend.ticket_ingestion.models import PRComment, PullRequest
from backend.web.core import flight_driver as D
from backend.web.core import flights as F

NOW = 1_800_000_000.0
H1 = "a" * 40  # the PR's head when the stop begins
H2 = "b" * 40  # the head after a round's push
BOT = "coderabbitai[bot]"
ICON = (
    '<picture><source media="(prefers-color-scheme: dark)" '
    'srcset="https://example.invalid/i.svg"><img alt="" '
    'src="https://example.invalid/i.svg" width="12"></picture>'
)

# Today's format: a "Nitpick comments: N" block, a file block per file, each
# item with a badge row, a bold title, text, and its own agent prompt.
NEW_BODY = """<!-- coderabbit-ai-generated:v1 -->



<details>
<summary>%(icon)s Nitpick comments: 1</summary><blockquote>
<dl>
<dd>
<br>

<details>
<summary>tests/test_widget.py: 1</summary><blockquote>
<dl>
<dd>
<br>

`40-42`: **%(icon)s Trivial** · **Maintainability** · **Quick win**

**Assert the stored value, not the call count.**

The test counts calls but never checks what was saved.

<!-- ai-agent-prompt:start -->

<details>
<summary>%(icon)s Prompt to fix issue</summary>
<dl>
<dd>

```
In tests/test_widget.py around lines 40-42, assert the saved widget's fields
instead of the mock's call count.
```

</dd>
</dl>
</details>

<!-- ai-agent-prompt:end -->

<!-- cr-comment:v1:0123456789abcdef01234567 -->

</dd>
</dl>
</blockquote></details>

</dd>
</dl>
</blockquote></details>

---

<details>
<summary>%(icon)s Prompt to fix all issues</summary>
<dl>
<dd>

```
Fix every issue above.
```

</dd>
</dl>
</details>

<details>
<summary>%(icon)s Review info</summary>
<dl><dd>Files selected for processing: 1</dd></dl>
</details>

<!-- This is an auto-generated comment by CodeRabbit for review status -->
""" % {"icon": ICON}

# The older format: emoji summaries with "(N)" counts, the outside-diff
# block quoted under a caution, a duplicate block.
OLD_BODY = """**Actionable comments posted: 1**

> [!CAUTION]
> Some comments are outside the diff and can't be posted inline.
>
> <details>
> <summary>⚠️ Outside diff range comments (1)</summary><blockquote>
>
> <details>
> <summary>app/store.py (1)</summary><blockquote>
>
> `88-90`: **Close the file handle on error.**
>
> The handle leaks when the parse raises.
>
> </blockquote></details>
>
> </blockquote></details>

<details>
<summary>♻️ Duplicate comments (2)</summary><blockquote>

<details>
<summary>app/cache.py (1)</summary><blockquote>

`12-14`: **Guard the empty list.**

Still applies: an empty list raises.

</blockquote></details>
<details>
<summary>app/store.py (1)</summary><blockquote>

`88`: **Close the file handle on error!**

Repeated.

</blockquote></details>

</blockquote></details>

<details>
<summary>🧹 Nitpick comments (1)</summary><blockquote>

<details>
<summary>app/cache.py (1)</summary><blockquote>

`30-31`: **Name the magic number.**

`86400` deserves a constant.

</blockquote></details>

</blockquote></details>

<!-- This is an auto-generated comment by CodeRabbit for review status -->
"""

# An inline CodeRabbit thread comment (today's format).
INLINE = """<!-- coderabbit-ai-generated:v1 -->

**%s Minor** · **Functional Correctness** · **Quick win**

**Guard the empty list.**

An empty list raises.
""" % ICON


# --------------------------------------------------------------------------- #
# The body parser (backend/ticket_ingestion/coderabbit.py)
# --------------------------------------------------------------------------- #
def test_a_body_only_nitpick_is_an_item():
    items = CR.body_items(NEW_BODY)
    assert len(items) == 1
    it = items[0]
    assert (it.section, it.path, it.line, it.end_line) == (
        "nitpick",
        "tests/test_widget.py",
        40,
        42,
    )
    assert it.title == "Assert the stored value, not the call count."
    assert it.text.startswith("**Assert the stored value, not the call count.**")
    assert "assert the saved widget's fields" in it.text  # its own prompt
    assert "Fix every issue above" not in it.text  # not the all-issues prompt
    assert "<" not in it.text and "cr-comment" not in it.text


def test_the_older_format_and_its_duplicates():
    items = CR.body_items(OLD_BODY)
    got = [(i.section, i.path, i.line, i.title) for i in items]
    assert got == [
        ("outside diff range", "app/store.py", 88, "Close the file handle on error."),
        ("duplicate", "app/cache.py", 12, "Guard the empty list."),
        # The second duplicate repeats the outside-diff item: one item.
        ("nitpick", "app/cache.py", 30, "Name the magic number."),
    ]
    assert items[0].text.startswith("**Close the file handle on error.**")
    assert "The handle leaks" in items[0].text


def test_a_summary_with_no_item_blocks_has_none():
    assert (
        CR.body_items(
            "**Actionable comments posted: 2**\n<details><summary>Review "
            "info</summary>x</details>"
        )
        == []
    )
    assert CR.body_items("") == []


def test_the_fingerprint_is_the_file_and_title_whatever_the_markup():
    # An inline thread and a body item that say the same thing match.
    dup = next(i for i in CR.body_items(OLD_BODY) if i.section == "duplicate")
    assert CR.fingerprint("app/cache.py", INLINE) == dup.key
    assert CR.fingerprint("app/other.py", INLINE) != dup.key


def _review(i, body, commit, t, who=BOT, state="COMMENTED"):
    return {
        "id": i,
        "user": {"login": who},
        "state": state,
        "body": body,
        "commit_id": commit,
        "html_url": "https://github.com/o/r/pull/7#pullrequestreview-%d" % i,
        "submitted_at": datetime.fromtimestamp(t, timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
    }


def test_only_the_newest_review_with_a_body_on_the_head_counts():
    reviews = [
        _review(1, OLD_BODY, H1, NOW),  # an older commit's review
        _review(2, NEW_BODY, H2, NOW + 10),
        _review(3, "", H2, NOW + 20),  # a thread reply: empty body
        _review(4, "please fix", H2, NOW + 30, who="alice"),
    ]
    assert CR.newest_review(reviews, H2)["id"] == 2
    assert CR.newest_review(reviews, H1)["id"] == 1
    assert CR.newest_review(reviews)["id"] == 2  # head unknown: newest
    assert CR.newest_review(reviews, "c" * 40) is None


# --------------------------------------------------------------------------- #
# fetch_pr_requests: body items, duplicates, own account, bot notices
# --------------------------------------------------------------------------- #
def _pr(author="me", head=H2):
    return PullRequest(
        number=7,
        head_ref="feature/x",
        head_sha=head,
        base_ref="main",
        title="Add parser",
        url="https://github.com/o/r/pull/7",
        author=author,
        created_at=datetime.fromtimestamp(NOW - 9000, timezone.utc),
        repo="o/r",
    )


def _cfg():
    return GithubConfig(
        base_branch="main",
        min_age_minutes=0,
        poll_interval_seconds=60,
        enabled=True,
        skip_authors=[],
    )


def _issue(i, who, body, t=NOW):
    return {
        "id": i,
        "user": {"login": who},
        "body": body,
        "html_url": "https://github.com/o/r/pull/7#issuecomment-%d" % i,
        "created_at": datetime.fromtimestamp(t, timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
    }


def _fetch(monkeypatch, *, inline=(), reviews=(), issue=(), threads=None, **kw):
    seen = {}

    async def _inline(pr, config, *, drop_noise=False, skip=(), info=None):
        seen["skip"] = set(skip)
        if info is not None and threads is not None:
            info.update(threads)
        return list(inline)

    async def _reviews(pr, config):
        return list(reviews)

    async def _issues(pr, config):
        return list(issue)

    monkeypatch.setattr(PC, "fetch_actionable_comments", _inline)
    monkeypatch.setattr(PC, "_fetch_reviews_raw", _reviews)
    monkeypatch.setattr(PC, "_fetch_issue_comments_raw", _issues)
    pr = kw.pop("pr", None) or _pr()
    out = asyncio.run(PC.fetch_pr_requests(pr, _cfg(), **kw))
    return out, seen


def test_a_body_only_nitpick_is_a_request(monkeypatch):
    info: dict = {}
    out, _ = _fetch(
        monkeypatch,
        reviews=[
            _review(1, OLD_BODY, H1, NOW),
            _review(2, NEW_BODY, H2, NOW + 10),
            _review(3, "", H2, NOW + 20),
        ],
        threads={"open_threads": 0, "fingerprints": set()},
        info=info,
    )
    assert len(out) == 1
    c = out[0]
    key = CR.body_items(NEW_BODY)[0].key
    assert pr_rounds.comment_key(c) == "n:" + key
    assert (c.id, c.author, c.path, c.line, c.section) == (
        2,
        BOT,
        "tests/test_widget.py",
        40,
        "nitpick",
    )
    assert c.url.endswith("#pullrequestreview-2")
    # It has a file, but no thread: answered in the conversation comment.
    assert not pr_replies.in_thread(c)
    assert pr_replies.label(c) == "nitpick comment in a review body"
    assert "no thread" in pr_replies.where_to_reply(c)
    assert info == {"open_threads": 0, "coderabbit": True}


def test_no_review_on_the_head_hands_no_body_items(monkeypatch):
    out, _ = _fetch(monkeypatch, reviews=[_review(1, NEW_BODY, H1, NOW)])
    assert out == []


def test_a_duplicate_of_a_thread_is_not_handed_twice(monkeypatch):
    """The "Duplicate comments" item repeats an inline thread (resolved or
    open, already handed or not): the thread is the request, never the
    duplicate as well."""
    thread = CR.fingerprint("app/cache.py", INLINE)
    out, _ = _fetch(
        monkeypatch,
        reviews=[_review(5, OLD_BODY, H2, NOW)],
        threads={"open_threads": 0, "fingerprints": {thread}},
    )
    assert [(c.section, c.path, c.line) for c in out] == [
        ("outside diff range", "app/store.py", 88),
        ("nitpick", "app/cache.py", 30),
    ]


def test_an_item_a_later_review_repeats_keeps_its_key(monkeypatch):
    """Handled = its key was handed: the same item in the next review (on
    the next head) is the same ``n:`` key, so the loop never sends it again."""
    a, _ = _fetch(monkeypatch, reviews=[_review(1, OLD_BODY, H1, NOW)], pr=_pr(head=H1))
    later = OLD_BODY.replace("Guard the empty list.", "Guard the empty list")
    b, _ = _fetch(monkeypatch, reviews=[_review(9, later, H2, NOW + 60)])
    assert [pr_rounds.comment_key(c) for c in a] == [
        pr_rounds.comment_key(c) for c in b
    ]
    assert a[0].id == 1 and b[0].id == 9


SHORTCUT = (
    "This pull request has been linked to [Shortcut Story #123: Sweep lands "
    "all day instead of by morning](https://app.example.invalid/story/123)."
)


def test_own_account_and_bot_notices_are_dropped_coderabbit_kept(monkeypatch):
    out, seen = _fetch(
        monkeypatch,
        reviews=[
            _review(2, NEW_BODY, H2, NOW + 10),
            _review(3, "Consider a guard here.", H2, NOW + 11, who="lint-bot[bot]"),
        ],
        issue=[
            _issue(10, "shortcut-integration[bot]", SHORTCUT),
            _issue(11, "agent-acct", "> " + SHORTCUT + "\n\nNoted, should I add it?"),
            _issue(12, "me", "Why not reuse the helper?"),  # the PR author
            _issue(13, BOT, "Should the retry also cover the None case?"),
            _issue(14, "lint-bot[bot]", "Please fix the import order."),
            _issue(15, "deploy-bot[bot]", "Fix: " + "a long report line. " * 60),
        ],
        skip=["agent-acct"],
    )
    got = sorted(pr_rounds.comment_key(c) for c in out)
    key = CR.body_items(NEW_BODY)[0].key
    assert got == sorted(["b:3", "i:13", "i:14", "i:15", "n:" + key])
    assert "agent-acct" in seen["skip"]  # inline threads skip it too


def test_a_required_reviewer_bot_that_never_reviewed_is_kept(monkeypatch):
    out, _ = _fetch(
        monkeypatch,
        issue=[_issue(20, "review-helper[bot]", "Please split this function.")],
        reviewers=["review-helper"],
    )
    assert out == []  # "review-helper" is not the login...
    out, _ = _fetch(
        monkeypatch,
        issue=[_issue(20, "review-helper[bot]", "Please split this function.")],
        reviewers=["review-helper[bot]"],
    )
    assert [c.id for c in out] == [20]


def test_is_bot_notice():
    assert PC.is_bot_notice("shortcut-integration[bot]", SHORTCUT, set())
    assert not PC.is_bot_notice(BOT, SHORTCUT, set())
    assert not PC.is_bot_notice("alice", SHORTCUT, set())
    assert not PC.is_bot_notice("x[bot]", SHORTCUT, {"x[bot]"})


# --------------------------------------------------------------------------- #
# The driver: the PR author, the agent's own login, round_head
# --------------------------------------------------------------------------- #
def test_pr_of_carries_the_pr_author_and_githubs_head(monkeypatch):
    from backend.web.core import github_pr as GH

    async def _find(wt, branch):
        return {"number": 7, "url": "u", "base": "main", "head": H2, "author": "me"}

    monkeypatch.setattr(GH, "repo_ref", lambda wt: SimpleNamespace(slug="o/r"))
    monkeypatch.setattr(GH, "find_pr", _find)
    monkeypatch.setattr(
        D, "_server", lambda: SimpleNamespace(_git_head_sha=lambda w: H1)
    )
    pr, slug = asyncio.run(
        D._pr_of({"folder": "/w", "branch": "feat"}, {"subject": {"title": "T"}})
    )
    assert slug == "o/r" and pr.author == "me" and pr.head_sha == H2


@pytest.fixture
def _store(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_FLIGHTS_DIR", str(tmp_path / "flights"))
    monkeypatch.setenv("MINDFLOCK_AUTOPILOT_FILE", str(tmp_path / "autopilot.json"))
    monkeypatch.setattr(D, "_REVIEWS", {})


def sess(**kw):
    return dict({"present": True, "status": "running", "activity": "idle"}, **kw)


def fly(config=None, session="s"):
    plan = F.save_plan(
        {"name": "P", "steps": [{"kind": "reviews", "config": config or {}}]},
        now=NOW,
    )
    return F.take_off(plan, session=session, now=NOW)


def test_the_driver_skips_the_agents_own_login(_store, monkeypatch):
    calls = []

    async def _pr_of(s, flight):
        return _pr(), "o/r"

    async def _fetch(pr, cfg, **kw):
        calls.append(kw)
        kw["info"].update(open_threads=0, coderabbit=False)
        return []

    async def _me():
        return "agent-acct"

    monkeypatch.setattr(D, "_pr_of", _pr_of)
    monkeypatch.setattr(PC, "fetch_pr_requests", _fetch)
    monkeypatch.setattr(D, "_own_login", _me)
    f = fly()
    out = asyncio.run(D._reviews_obs(f, f["steps"][0], sess(pr_url="u"), NOW))
    assert calls[0]["skip"] == ["agent-acct"]
    assert out == {"ids": [], "created": {}, "open_threads": 0}


def test_coderabbit_on_the_pr_is_asked_about_the_head_unrequired(_store, monkeypatch):
    asked = []

    async def _pr_of(s, flight):
        return _pr(), "o/r"

    async def _fetch(pr, cfg, **kw):
        kw["info"].update(open_threads=0, coderabbit=True)
        return []

    async def _on_head(pr, who, known, known_since, now):
        asked.append(who)
        return {"head": H2, "since": now, "reviewed": True, "any": True, "skipped": ""}

    monkeypatch.setattr(D, "_pr_of", _pr_of)
    monkeypatch.setattr(PC, "fetch_pr_requests", _fetch)
    monkeypatch.setattr(D, "_reviewer_on_head", _on_head)
    f = fly()
    out = asyncio.run(D._reviews_obs(f, f["steps"][0], sess(pr_url="u"), NOW))
    assert asked == ["coderabbit"]
    assert out["reviewer_name"] == "coderabbit" and out["required"] is False


def test_a_round_records_its_head_and_tells_where_a_body_item_is_answered(
    _store, monkeypatch
):
    sent = []
    monkeypatch.setattr(D, "_deliver", lambda title, text: sent.append(text))
    monkeypatch.setattr(D, "_session_obs", lambda flight, now, agent: sess(folder="/w"))
    item = PRComment(
        id=2,
        kind="review",
        author=BOT,
        body="**Assert the stored value.**\n\nCheck the saved fields.",
        url="https://github.com/o/r/pull/7#pullrequestreview-2",
        path="tests/test_widget.py",
        line=40,
        review_state="COMMENTED",
        item_key="k1",
        section="nitpick",
    )
    f = fly()
    D._REVIEWS[f["id"]] = {
        "comments": {"n:k1": item},
        "pr": _pr(),
        "reviewer": {"head": H2},
    }
    action = {"action": "run", "phase": "send", "comments": ["n:k1"]}
    out = asyncio.run(D._send_reviews(f, f["steps"][0], action, NOW))
    assert out["detail"]["round_head"] == H2
    assert out["detail"]["seen"] == ["n:k1"]
    text = sent[0]
    assert "nitpick comment in a review body by %s" % BOT in text
    assert "`tests/test_widget.py:40`" in text and "no thread to reply in" in text
    assert "issues/7/comments" in text  # the conversation-comment command


# --------------------------------------------------------------------------- #
# The judge: the push phase, the clean finish, waiting for the review
# --------------------------------------------------------------------------- #
def go(f, obs, now):
    a = F.next_action(f, obs, now)
    F.apply(f, a, now)
    return a


def detail(f):
    return f["step_states"][f["steps"][0]["id"]]["detail"]


def rv(head=H1, reviewed=False, any_=True, since=NOW):
    return {
        "head": head,
        "since": since,
        "reviewed": reviewed,
        "any": any_,
        "skipped": "",
    }


def obs(ids=(), reviewer=None, threads=0, required=False, name="coderabbit"):
    r = {"ids": list(ids), "created": {}, "open_threads": threads}
    if name:
        r.update(reviewer=reviewer, reviewer_name=name, required=required)
    return {"session": sess(pr_url="https://pr/1"), "reviews": r}


def watching(session="s", **config):
    f = fly(config, session=session)
    F.apply(f, F.next_action(f, {"session": sess()}, NOW), NOW)
    a = go(f, obs(reviewer=rv(any_=False)), NOW)  # seeds
    assert a["action"] == "wait"
    return f


def test_a_clean_review_of_the_head_ends_the_loop(_store):
    f = watching(quiet_minutes=10)
    a = go(f, obs(reviewer=rv(reviewed=True)), NOW + 1)
    assert a["note"].startswith("coderabbit reviewed aaaaaaa")
    a = go(f, obs(reviewer=rv(reviewed=True)), NOW + 70)
    assert a["action"] == "done"
    assert a["note"] == "coderabbit reviewed aaaaaaa: nothing left to address"
    assert a["detail"]["receipt"] == "no rounds, clean review"


def test_an_open_thread_or_unknown_threads_are_not_clean(_store):
    for k, threads in enumerate((1, None)):
        f = watching("s%d" % k, quiet_minutes=10)
        go(f, obs(reviewer=rv(reviewed=True), threads=threads), NOW + 1)
        a = go(f, obs(reviewer=rv(reviewed=True), threads=threads), NOW + 70)
        assert a["action"] == "wait" and a["note"].startswith("quiet for")
        # The quiet rule still finishes it.
        a = F.next_action(
            f, obs(reviewer=rv(reviewed=True), threads=threads), NOW + 1 + 600
        )
        assert a["action"] == "done" and a["note"].startswith("no new review comments")


def test_a_body_item_on_the_clean_looking_review_goes_to_the_agent(_store):
    f = watching()
    go(f, obs(reviewer=rv(reviewed=True)), NOW + 1)
    a = F.next_action(f, obs(["n:k1"], reviewer=rv(reviewed=True)), NOW + 70)
    assert a["action"] == "run" and a["comments"] == ["n:k1"]
    # Once handed it is seen: the same item from a later review is not new.
    detail(f)["seen"] = ["n:k1"]
    a = F.next_action(f, obs(["n:k1"], reviewer=rv(reviewed=True)), NOW + 80)
    assert a["action"] == "done"


def test_no_review_yet_on_the_head_keeps_waiting(_store):
    f = watching(quiet_minutes=5, reviewer_wait_minutes=10)
    go(f, obs(reviewer=rv(reviewed=True)), NOW + 1)
    # A round was handed on H1; the agent pushed H2, not reviewed yet.
    detail(f).update(round_head=H1, review_rounds=1, quiet_since=NOW + 100)
    pushed = obs(reviewer=rv(head=H2, since=NOW + 100))
    a = go(f, pushed, NOW + 100)
    assert a["note"] == "waiting for coderabbit to review bbbbbbb"
    # Past the quiet window: still waiting for its review of the new head.
    a = go(f, pushed, NOW + 100 + 6 * 60)
    assert a["action"] == "wait"
    assert a["note"].startswith("waiting for coderabbit to review bbbbbbb")
    # Its clean review of H2 ends the loop.
    a = go(f, obs(reviewer=rv(head=H2, reviewed=True)), NOW + 100 + 7 * 60)
    assert a["note"].startswith("coderabbit reviewed bbbbbbb")
    a = go(f, obs(reviewer=rv(head=H2, reviewed=True)), NOW + 100 + 8 * 60)
    assert a["action"] == "done"
    assert a["detail"]["receipt"] == "1 round, clean review"


def test_unrequired_coderabbit_that_never_reviews_falls_back_to_quiet(_store):
    f = watching(quiet_minutes=5, reviewer_wait_minutes=10)
    nothing = obs(reviewer=rv())
    go(f, nothing, NOW + 1)
    assert go(f, nothing, NOW + 9 * 60)["action"] == "wait"
    a = F.next_action(f, nothing, NOW + 10 * 60)
    assert a["action"] == "done" and a["note"] == "no new review comments for 5 min"
    # Required, the same wait fails the stop (unchanged).
    g = watching(
        "t", quiet_minutes=5, reviewer_wait_minutes=10, require_reviewer="coderabbit"
    )
    req = obs(reviewer=rv(), required=True)
    go(g, req, NOW + 1)
    assert F.next_action(g, req, NOW + 10 * 60)["action"] == "fail"


def test_a_round_that_pushed_nothing_is_not_a_clean_review(_store):
    """The agent only replied: the head did not move, so CodeRabbit's old
    review of it is not a review of the round — the quiet rule waits for its
    answers to the replies."""
    f = watching(quiet_minutes=5)
    go(f, obs(reviewer=rv(reviewed=True)), NOW + 1)
    detail(f).update(round_head=H1, review_rounds=1, quiet_since=NOW + 100)
    a = go(f, obs(reviewer=rv(reviewed=True)), NOW + 160)
    assert a["action"] == "wait" and a["note"].startswith("quiet for")
    assert F.next_action(f, obs(reviewer=rv(reviewed=True)), NOW + 401)["action"] == (
        "done"
    )


def test_without_coderabbit_the_quiet_rule_is_unchanged(_store):
    f = fly({"quiet_minutes": 5})
    F.apply(f, F.next_action(f, {"session": sess()}, NOW), NOW)
    plain = obs(name="")
    go(f, plain, NOW)
    assert F.next_action(f, plain, NOW + 4 * 60)["action"] == "wait"
    assert F.next_action(f, plain, NOW + 5 * 60)["action"] == "done"


def _pushing(f):
    F.apply(
        f,
        {"action": "wait", "note": "", "detail": {"phase": "push", "phase_since": NOW}},
        NOW,
    )
    return {
        "source": "flight",
        "item": f["id"],
        "lane": "push",
        "state": "running",
        "commits": 0,
        "note": "already at pr — waiting for new work",
    }


def test_a_round_the_agent_pushed_itself_goes_back_to_watching(_store):
    f = watching()
    rec = _pushing(f)
    o = {
        "session": sess(pr_url="https://pr/1"),
        "autopilot": rec,
        "git": {"stage": "pr", "pr_url": "https://pr/1", "ahead": 3},
    }
    a = go(f, o, NOW + 30)
    assert a["action"] == "wait" and a.get("by_agent") is True
    assert a["note"] == "pushed by the agent — watching for new comments"
    assert detail(f)["phase"] == "watch" and detail(f)["quiet_since"] == NOW + 30


@pytest.mark.parametrize(
    "git,activity",
    [
        ({"stage": "committed"}, "idle"),  # commits not on origin: lane pushes
        ({"stage": "agent"}, "idle"),  # uncommitted edits: lane commits
        ({"stage": "pr"}, "working"),  # the agent's turn may add work
    ],
)
def test_a_round_with_work_left_waits_for_the_lane(_store, git, activity):
    f = watching()
    rec = _pushing(f)
    o = {
        "session": sess(pr_url="https://pr/1", activity=activity),
        "autopilot": rec,
        "git": git,
    }
    a = F.next_action(f, o, NOW + 30)
    assert a["action"] == "wait" and not a.get("by_agent")
    assert a["note"] == "already at pr — waiting for new work"


def test_the_driver_disarms_the_lane_a_round_did_not_need(_store, monkeypatch):
    """Through the driver: a push phase whose lane waits "for new work" on a
    branch the agent already pushed goes back to watching and disarms the
    lane, so it never sits out its 2-hour arm deadline."""
    from backend.web.core import autopilot as AP

    f = fly()
    sid = f["steps"][0]["id"]

    def _push(g):
        g["step_states"][sid].update(state="running", since=NOW)
        g["step_states"][sid]["detail"] = {
            "phase": "push",
            "phase_since": NOW,
            "seeded": True,
            "seen": ["c:1"],
            "review_rounds": 1,
        }

    F.mutate(f["id"], _push)
    AP.arm("s", "push", source="flight", item=f["id"], lane="push")
    row = sess(pr_url="https://pr/1", branch="feat", folder="/repo", pending=False)
    monkeypatch.setattr(D, "_session_obs", lambda flight, now, agent: dict(row))
    monkeypatch.setattr(
        D, "_ship_git_obs", lambda title: {"stage": "pr", "pr_url": "https://pr/1"}
    )
    ns = SimpleNamespace(
        _SERVER_BOOT_ID="boot-1",
        ENGINE=SimpleNamespace(instances={}),
        _pending_rows=lambda: [],
        git_available=lambda: True,
        _peer_guard=SimpleNamespace(refusal=lambda inst, what: None),
        _lane_refusal=lambda title, lane: None,
        _publish_autopilot=lambda title: None,
    )
    monkeypatch.setattr(D, "_server", lambda: ns)
    asyncio.run(D.step_flight(f["id"]))
    d = F.get(f["id"])["step_states"][sid]["detail"]
    assert d["phase"] == "watch"
    assert AP.get("s") is None or AP.get("s").get("state") != "running"
