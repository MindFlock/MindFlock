"""A Reviews stop's per-round additions (docs/flights.md "Reviews settle"):
``each_round`` — the route's own instructions added to every round's prompt —
and ``reply_declined`` — a declined comment also gets a reply on the PR.
Both off, every round's prompt is the pipeline's, byte for byte."""

import asyncio
import datetime as dt

import pytest

from backend.ticket_ingestion import pr_runner as P
from backend.ticket_ingestion.models import PRComment, PullRequest
from backend.web.core import flight_driver as D
from backend.web.core import flights as F

NOW = 1_800_000_000.0
RULES = (
    "Re-run your code review on this round's changes before the push.\n"
    "Reply on the PR to each comment you decline, with the reason."
)


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_FLIGHTS_DIR", str(tmp_path / "flights"))
    monkeypatch.setenv("MINDFLOCK_AUTOPILOT_FILE", str(tmp_path / "autopilot.json"))
    monkeypatch.setattr(D, "_REVIEWS", {})


def _pr():
    return PullRequest(
        1, "feat", "abc", "main", "T", "https://pr/1", "", dt.datetime.now(), repo="o/r"
    )


def _comment(i):
    return PRComment(
        id=i, kind="review", author="rev", body="fix %d" % i, url="u%d" % i
    )


def _reviews(**config):
    return F.normalize_step({"kind": "reviews", "config": config})["config"]


# --------------------------------------------------------------------------- #
# the fields
# --------------------------------------------------------------------------- #
def test_the_kinds_dto_serves_both_fields():
    k = {x["kind"]: x for x in F.kinds_dto()["kinds"]}["reviews"]
    names = {x["name"]: x for x in k["fields"]}
    each = names["each_round"]
    assert each["type"] == "textarea" and each["default"] == ""
    assert each["label"] == "Also, every round"
    reply = names["reply_declined"]
    assert reply["type"] == "bool" and reply["default"] is False


def test_defaults_trimming_and_the_cap():
    c = _reviews()
    assert c["each_round"] == "" and c["reply_declined"] is False
    assert _reviews(each_round="  " + RULES + "\n\n")["each_round"] == RULES
    assert _reviews(each_round=None)["each_round"] == ""
    long = "x" * (F.MAX_EACH_ROUND + 500)
    assert len(_reviews(each_round=long)["each_round"]) == F.MAX_EACH_ROUND == 8000
    assert _reviews(reply_declined="yes")["reply_declined"] is True
    # A stored plan read leniently keeps it too.
    lenient = F.normalize_step(
        {"kind": "reviews", "config": {"each_round": " hi "}}, strict=False
    )
    assert lenient["config"]["each_round"] == "hi"


# --------------------------------------------------------------------------- #
# the prompt
# --------------------------------------------------------------------------- #
def test_the_prompt_is_unchanged_without_them():
    pr, cs = _pr(), [_comment(1)]
    plain = P.build_consolidated_pr_prompt(pr, cs, "/ws")
    assert P.build_consolidated_pr_prompt(pr, cs, "/ws", each_round="  ") == plain
    assert "## Also, every round" not in plain
    assert plain.endswith(
        "3. Finish with a concise per-comment summary: what you changed, or why "
        "you skipped it."
    )
    assert "reply to it on the PR" not in plain


def test_each_round_sits_between_the_comments_and_when_done():
    out = P.build_consolidated_pr_prompt(_pr(), [_comment(1)], "/ws", each_round=RULES)
    head = out.index("## Also, every round")
    assert out.index("fix 1") < head < out.index("## When done")
    assert "## Also, every round\n\n" + RULES + "\n\n## When done" in out


def test_reply_declined_asks_for_a_reply_on_the_pr():
    out = P.build_consolidated_pr_prompt(
        _pr(), [_comment(1)], "/ws", reply_declined=True
    )
    assert (
        "3. For each comment you decline (no change made), reply to it on the PR" in out
    )
    assert out.endswith(
        "4. Finish with a concise per-comment summary: what you changed, or why "
        "you skipped it."
    )


# --------------------------------------------------------------------------- #
# the driver delivers it every round
# --------------------------------------------------------------------------- #
def _send(monkeypatch, config):
    plan = F.save_plan(
        {"name": "P", "steps": [{"kind": "reviews", "config": config}]}, now=NOW
    )
    f = F.take_off(plan, session="s", now=NOW)
    step = f["steps"][0]
    pr, comments = _pr(), {"1": _comment(1), "2": _comment(2)}
    D._REVIEWS[f["id"]] = {"pr": pr, "comments": comments}
    sent: list = []
    monkeypatch.setattr(D, "_session_obs", lambda *a: {"folder": "/ws"})
    monkeypatch.setattr(D, "_deliver", lambda title, text: sent.append((title, text)))
    action = {"action": "run", "phase": "send", "comments": ["1", "2"]}
    out = asyncio.run(D._send_reviews(f, step, action, NOW))
    assert out["detail"]["review_rounds"] == 1
    assert [t for t, _ in sent] == ["s"]
    return sent[0][1], pr, [comments["1"], comments["2"]]


def test_send_reviews_delivers_each_round(monkeypatch):
    text, pr, cs = _send(monkeypatch, {"each_round": RULES, "reply_declined": True})
    assert text == P.build_consolidated_pr_prompt(
        pr, cs, "/ws", each_round=RULES, reply_declined=True
    )
    assert "## Also, every round\n\n" + RULES in text


def test_send_reviews_without_them_is_the_old_prompt(monkeypatch):
    text, pr, cs = _send(monkeypatch, {})
    assert text == P.build_consolidated_pr_prompt(pr, cs, "/ws")
