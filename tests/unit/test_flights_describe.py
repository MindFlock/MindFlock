"""Describe a step (``backend.web.core.flight_describe``): the prompt
contract and the parser — no model is ever run."""

import pytest

from backend.web.core import commit_message as cm
from backend.web.core import flight_describe as D
from backend.web.core import flights as F


def test_prompt_lists_every_kind_and_demands_one_flight_block():
    p = D.build_prompt("wait 5 minutes then merge", "step")
    for kind in F.STEP_KINDS:
        assert "- %s [" % kind in p
    assert "lane (select: commit|push|pr|merge" in p
    assert "<flight>" in p and "</flight>" in p
    assert '"wait 5 minutes then merge"' in p  # the request is quoted as data
    assert "several blocks" in p
    assert "whole row of blocks" in D.build_prompt("x", "plan")


@pytest.mark.parametrize(
    "raw",
    [
        '<flight>[{"kind": "wait", "config": {"minutes": 5}}]</flight>',
        'Sure!\n<flight>\n```json\n[{"kind": "wait", "config": {"minutes": 5}}]\n```\n</flight>',
        '<flight>{"kind": "wait", "config": {"minutes": 5}}</flight>',
        '<flight>{"steps": [{"kind": "wait", "config": {"minutes": 5}}]}</flight>',
        'no tags [{"kind": "wait", "config": {"minutes": 5}}] trailing',
    ],
)
def test_parse_answer_shapes(raw):
    steps = D.parse_answer(raw)
    assert steps == [
        {
            "id": "s1",
            "kind": "wait",
            "title": "Wait 5 min",
            "config": {"minutes": 5, "until": ""},
            "on_fail": "halt",
        }
    ]


def test_parse_answer_several_blocks_use_the_plan_validator():
    raw = (
        '<flight>[{"kind": "ship", "title": "To the reviewer", "config": {"lane": "pr"}},'
        '{"kind": "wait", "config": {"minutes": 5}},'
        '{"kind": "checks", "on_fail": "agent"},'
        '{"kind": "ship", "config": {"lane": "merge"}}]</flight>'
    )
    steps = D.parse_answer(raw)
    assert [s["kind"] for s in steps] == ["ship", "wait", "checks", "ship"]
    assert steps[0]["title"] == "To the reviewer" and steps[2]["on_fail"] == "agent"
    assert [s["id"] for s in steps] == ["s1", "s2", "s3", "s4"]


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "I can't help with that",
        "<flight>[]</flight>",
        '<flight>[{"kind": "deploy"}]</flight>',
        '<flight>[{"kind": "ship", "config": {"lane": "prod"}}]</flight>',
        "<flight>{not json</flight>",
    ],
)
def test_bad_answers_are_could_not_describe(raw):
    with pytest.raises(D.DescribeError) as err:
        D.parse_answer(raw)
    assert (err.value.status, err.value.error) == (422, "could_not_describe")


def test_no_model_is_409(monkeypatch):
    def none(*a, **kw):
        raise cm.CommitMessageError("no installed CLI")

    monkeypatch.setattr(cm, "pick_argv", none)
    with pytest.raises(D.DescribeError) as err:
        D.describe("wait", program="x")
    assert (err.value.status, err.value.error) == (409, "no_model")
    assert D.safe_describe("wait")[0] == 409


def test_describe_runs_the_one_shot_and_normalises(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(
        cm,
        "pick_argv",
        lambda prompt, prog, fb, purpose="": seen.update(purpose=purpose)
        or ["/usr/bin/claude", "-p", prompt],
    )
    monkeypatch.setattr(cm, "neutral_cwd", lambda: str(tmp_path))

    def fake_run(argv, cwd, timeout):
        seen.update(cwd=cwd, timeout=timeout)
        return '\x1b[1m<flight>[{"kind": "you", "config": {"text": "Approve"}}]</flight>\x1b[0m'

    monkeypatch.setattr(cm, "_run", fake_run)
    out = D.describe("ask me to approve", "step", program="claude")
    assert out["model"] == "claude"
    assert (
        out["steps"][0]["kind"] == "you"
        and out["steps"][0]["config"]["text"] == "Approve"
    )
    assert seen == {"purpose": "a flight step", "cwd": str(tmp_path), "timeout": 60.0}


def test_a_failed_run_or_empty_text_is_422(monkeypatch, tmp_path):
    monkeypatch.setattr(cm, "pick_argv", lambda *a, **kw: ["claude"])
    monkeypatch.setattr(cm, "neutral_cwd", lambda: str(tmp_path))

    def timeout(*a):
        raise cm.CommitMessageError("claude did not answer within 60s")

    monkeypatch.setattr(cm, "_run", timeout)
    status, body = D.safe_describe("x")
    assert (
        status == 422
        and body["error"] == "could_not_describe"
        and "60s" in body["detail"]
    )
    assert D.safe_describe("   ")[0] == 422
