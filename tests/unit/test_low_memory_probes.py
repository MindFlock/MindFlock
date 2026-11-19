"""The instances tick's stage probe while memory is low
(server._session_stage_for_tick): an unwatched session reuses its stage for
up to 20 s; the watched one, and every session at normal memory, is
recomputed each tick exactly as before."""

from __future__ import annotations

import time

import pytest

from backend.web import server


class _Inst:
    def __init__(self, title):
        self.Title = title


@pytest.fixture
def probe(monkeypatch):
    calls = []

    def fresh(inst):
        calls.append(inst.Title)
        return {"stage": "agent", "n": len(calls)}

    monkeypatch.setattr(server, "_session_stage_fresh", fresh)
    monkeypatch.setattr(server, "_STAGE_LOW_MEMO", {})
    monkeypatch.setattr(server._ownership, "terminals", lambda t: 0)
    monkeypatch.setattr(server, "_HUMAN_INPUT_AT", {})
    return calls


def test_normal_memory_recomputes_every_tick(probe, monkeypatch):
    monkeypatch.setattr(server, "_memory_low", lambda: False)
    inst = _Inst("bg")
    for _ in range(3):
        server._session_stage_for_tick(inst)
    assert probe == ["bg"] * 3
    assert server._STAGE_LOW_MEMO == {}  # nothing new kept at normal memory


def test_low_memory_reuses_an_unwatched_sessions_stage(probe, monkeypatch):
    monkeypatch.setattr(server, "_memory_low", lambda: True)
    inst = _Inst("bg")
    first = server._session_stage_for_tick(inst)
    assert server._session_stage_for_tick(inst) == first
    assert server._session_stage_for_tick(inst) == first
    assert probe == ["bg"]
    # Past the TTL it is computed again.
    t, ref, val = server._STAGE_LOW_MEMO["bg"]
    server._STAGE_LOW_MEMO["bg"] = (t - server._STAGE_LOW_TTL - 1, ref, val)
    server._session_stage_for_tick(inst)
    assert probe == ["bg", "bg"]


def test_low_memory_keeps_an_open_terminal_fresh(probe, monkeypatch):
    monkeypatch.setattr(server, "_memory_low", lambda: True)
    monkeypatch.setattr(
        server._ownership, "terminals", lambda t: 1 if t == "open" else 0
    )
    inst = _Inst("open")
    for _ in range(3):
        server._session_stage_for_tick(inst)
    assert probe == ["open"] * 3


def test_low_memory_keeps_a_session_typed_into_fresh(probe, monkeypatch):
    monkeypatch.setattr(server, "_memory_low", lambda: True)
    server._HUMAN_INPUT_AT["typed"] = time.time() - 5
    inst = _Inst("typed")
    for _ in range(3):
        server._session_stage_for_tick(inst)
    assert probe == ["typed"] * 3
    # A minute after the last keystroke it may wait like the rest: the stage
    # the last tick computed is reused.
    server._HUMAN_INPUT_AT["typed"] = time.time() - server._STAGE_HANDS_ON_S - 1
    server._session_stage_for_tick(inst)
    server._session_stage_for_tick(inst)
    assert probe == ["typed"] * 3


def test_a_namesake_never_inherits_the_kept_stage(probe, monkeypatch):
    monkeypatch.setattr(server, "_memory_low", lambda: True)
    server._session_stage_for_tick(_Inst("s"))
    server._session_stage_for_tick(_Inst("s"))  # a recreated session, same title
    assert probe == ["s", "s"]


def test_a_workflow_verb_forgets_the_kept_stage(probe, monkeypatch):
    """Commit / push / PR call _forget_probes: the next tick must show the
    stage they produced, memory low or not."""
    monkeypatch.setattr(server, "_memory_low", lambda: True)
    inst = _Inst("w")
    server._session_stage_for_tick(inst)
    server._forget_probes("w")
    server._session_stage_for_tick(inst)
    assert probe == ["w", "w"]


def test_unknown_presence_reads_as_watched(probe, monkeypatch):
    def boom(t):
        raise RuntimeError("ownership store broken")

    monkeypatch.setattr(server._ownership, "terminals", boom)
    assert server._session_watched("x") is True


def test_the_snapshot_publisher_uses_it():
    import inspect

    src = inspect.getsource(server._session_snapshot)
    assert "_session_stage_for_tick(i)" in src
    assert "_session_stage_fresh(i)" not in src
