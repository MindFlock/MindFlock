"""The Sky's sentence box (``flight_describe``) reads the SAME cookbook the
MCP's describe_flight hands an agent, and returns the read-back sentence and
the lint beside the steps. No model is ever run."""

from __future__ import annotations

from backend.web.core import commit_message as cm
from backend.web.core import flight_cookbook as C
from backend.web.core import flight_describe as D


def test_the_prompt_carries_the_cookbook():
    p = D.build_prompt("ticket to merge after my OK", "plan")
    assert C.prompt_text() in p
    assert "route" in p and "Never invent a kind, a URL or a command" in p
    # the request stays quoted data, after everything else
    assert p.rstrip().endswith('"ticket to merge after my OK"')


def test_the_prompt_stays_a_one_shot_size():
    assert len(D.build_prompt("x", "plan")) < 24000


def test_describe_returns_the_sentence_and_the_warnings(monkeypatch, tmp_path):
    monkeypatch.setattr(
        cm, "pick_argv", lambda *a, **kw: ["/usr/bin/claude", "-p", "x"]
    )
    monkeypatch.setattr(cm, "neutral_cwd", lambda: str(tmp_path))
    monkeypatch.setattr(
        cm,
        "_run",
        lambda *a: '<flight>[{"kind": "agent", "config": {"prompt": "fix it"}},'
        '{"kind": "join"}, {"kind": "ship", "config": {"lane": "pr"}}]</flight>',
    )
    out = D.describe("fix it, join, PR", "plan", program="claude")
    assert out["sentence"] == "Agent: fix it → Join (waits for the workers) → Open PR"
    assert any("Join with no Split" in w for w in out["warnings"])


def test_a_clean_route_has_no_warnings(monkeypatch, tmp_path):
    monkeypatch.setattr(cm, "pick_argv", lambda *a, **kw: ["claude"])
    monkeypatch.setattr(cm, "neutral_cwd", lambda: str(tmp_path))
    monkeypatch.setattr(
        cm,
        "_run",
        lambda *a: '<flight>[{"kind": "test", "on_fail": "agent"},'
        '{"kind": "ship", "config": {"lane": "pr"}}]</flight>',
    )
    out = D.describe("test then PR", "plan", program="claude")
    assert "warnings" not in out
    assert out["sentence"] == "Test (agent fixes failures ×2) → Open PR"
