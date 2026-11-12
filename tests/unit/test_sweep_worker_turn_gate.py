"""Stability sweep: a held worker's turn start that the launch gate let go
and that then FAILED takes its admission back (``launch_gate.unnote_start``),
as every other gated start does. Without it the failed start read as an
agent warming up for WARMUP_SECONDS and held the next automatic start a
whole stagger."""

from types import SimpleNamespace

from backend.web.core import launch_gate as gate
from backend.web.core import worker_order_driver as W


def test_a_turn_start_that_fails_takes_its_admission_back(monkeypatch):
    monkeypatch.setattr(W, "_agent_live", lambda srv, title: False)
    monkeypatch.setattr(W, "start_refusal", lambda title, inst: None)
    monkeypatch.setattr(W, "start_agent", lambda title, inst=None: "workspace missing")
    monkeypatch.setattr(gate, "decide", lambda kind: {"decision": "go"})
    note = W.start_on_turn(SimpleNamespace(), "w1", object(), parent="orch")
    assert note == "its agent did not start: workspace missing"
    assert gate._ADMITTED == {} and gate._LAST_AUTO["at"] == 0.0


def test_a_turn_start_that_works_keeps_its_admission(monkeypatch):
    monkeypatch.setattr(W, "_agent_live", lambda srv, title: False)
    monkeypatch.setattr(W, "start_refusal", lambda title, inst: None)
    monkeypatch.setattr(W, "start_agent", lambda title, inst=None: None)
    monkeypatch.setattr(gate, "decide", lambda kind: {"decision": "go"})
    assert W.start_on_turn(SimpleNamespace(), "w1", object(), parent="orch") == ""
    assert gate._ADMITTED and gate._LAST_AUTO["at"] > 0
