"""Property-based tests for the launch gate's queue and in-flight list
(backend/web/core/launch_gate.py).

For any interleaving of enqueue, a background drain step, cancel, Start
anyway, a launch returning (started, failed or raising) and memory going low
and back:

* no title is ever both waiting (``_QUEUE``) and starting (``_INFLIGHT``);
* each queued launch runs at most once, and one that was replaced or
  cancelled never runs;
* the public positions are contiguous from 1, in FIFO order;
* the gate agrees with a plain model of the queue after every step.

Launches admitted by the drainer run in its ``launch-gate-start`` threads and
hold on an Event until the sequence lets them return, so every step is
deterministic (no sleeps). A title is not queued again while its own launch
is still starting: that is the callers' contract (a session has one launch
at a time), and the gate does not refuse it.
"""

from __future__ import annotations

import contextlib
import threading
from typing import Dict, List, Optional

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from backend import memory_pressure
from backend.web.core import launch_gate as gate

GiB = 1024**3
TITLES = ["a", "b", "c", "d"]
WAIT = 30  # a safety net for a thread that never shows up, not a pace


class _Launch:
    """One queued launch: counts its runs; in a drainer thread it holds until
    released, in the caller's thread (Start anyway) it returns at once."""

    def __init__(self, title: str, outcome: str):
        self.title = title
        self.outcome = outcome  # "ok" | "error" | "raise"
        self.runs = 0
        self.overlap: List[str] = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.thread: Optional[threading.Thread] = None

    @property
    def error(self) -> Optional[str]:
        return None if self.outcome == "ok" else "boom"

    def __call__(self) -> Optional[str]:
        self.runs += 1
        self.thread = threading.current_thread()
        with gate._LOCK:
            waiting = {it["title"] for it in gate._QUEUE}
            flying = [it["title"] for it in gate._INFLIGHT]
        if self.title in waiting or flying.count(self.title) != 1:
            self.overlap.append(
                "%s: waiting=%s flying=%s" % (self.title, waiting, flying)
            )
        self.entered.set()
        if self.thread.name == "launch-gate-start":
            assert self.release.wait(WAIT), "never released"
        if self.outcome == "raise":
            raise RuntimeError("boom")
        return self.error


@contextlib.contextmanager
def _gate(level: Dict[str, str]):
    """A clean gate per example: memory from ``level``, no stagger, no cap."""
    with pytest.MonkeyPatch.context() as mp:
        gate._reset_for_tests()
        mp.setattr(gate, "AUTO_DRAIN", False)
        mp.setattr(gate, "START_NOW_WAIT_SECONDS", 0)
        mp.setattr(gate, "on_change", None)
        mp.setenv("MINDFLOCK_AUTO_LAUNCH_SPACING", "0")
        mp.setenv("MINDFLOCK_MAX_LIVE_AGENTS", "0")
        mp.setenv("MINDFLOCK_AGENT_WARMUP_MB", "1")
        mp.setenv("MINDFLOCK_LAUNCH_QUEUE_MAX", "0")
        mp.delenv("MINDFLOCK_AUTO_SPACING_FAST", raising=False)

        def snapshot(fresh: bool = False) -> dict:
            low = level["now"] == "low"
            return {
                "level": level["now"],
                "available": (3 if low else 1000) * GiB,
                "total": 2000 * GiB,
                "available_pct": 0.15 if low else 50.0,
                "psi": None,
            }

        mp.setattr(memory_pressure, "snapshot", snapshot)
        mp.setattr(
            memory_pressure,
            "thresholds",
            lambda: {
                "low": (0.0, 2 * GiB, 20.0, 5.0),
                "critical": (0.0, 1 * GiB, 40.0, 15.0),
            },
        )
        gate.set_probe(lambda: [])
        try:
            yield
        finally:
            gate.set_probe(None)
            gate._reset_for_tests()


_OPS = st.one_of(
    st.tuples(
        st.just("enqueue"),
        st.sampled_from(TITLES),
        st.sampled_from(["ok", "ok", "error", "raise"]),
    ),
    st.tuples(st.just("drain"), st.none(), st.none()),
    st.tuples(st.just("cancel"), st.sampled_from(TITLES), st.none()),
    st.tuples(st.just("start_now"), st.sampled_from(TITLES), st.none()),
    st.tuples(st.just("finish"), st.sampled_from(TITLES), st.none()),
    st.tuples(st.just("memory"), st.sampled_from(["ok", "low"]), st.none()),
)


def _check(queue: List[str], flying: List[str], every: List[_Launch]) -> None:
    with gate._LOCK:
        waiting = [it["title"] for it in gate._QUEUE]
        starting = [it["title"] for it in gate._INFLIGHT]
    assert waiting == queue and starting == flying
    assert not set(waiting) & set(starting)
    assert len(set(waiting)) == len(waiting) and len(set(starting)) == len(starting)
    pub = gate.items()
    assert [p["title"] for p in pub] == queue
    assert [p["position"] for p in pub] == list(range(1, len(queue) + 1))
    assert len({p["id"] for p in pub}) == len(pub)
    assert all(p["why"] in gate.WHYS for p in pub)
    for i, t in enumerate(queue, 1):
        assert gate.waiting(t)["position"] == i
    for t in flying:
        assert gate.waiting(t) is None
    assert gate.status()["starting"] == flying
    for launch in every:
        assert launch.runs <= 1 and launch.overlap == []


@given(ops=st.lists(_OPS, max_size=40))
@settings(max_examples=60)
def test_queue_and_in_flight_never_overlap_and_each_launch_runs_once(ops):
    level = {"now": "ok"}
    with _gate(level):
        queue: List[str] = []  # the model: titles waiting, FIFO
        queued: Dict[str, _Launch] = {}  # the launch each waiting title holds
        flying: Dict[str, _Launch] = {}  # admitted, not yet returned (in order)
        every: List[_Launch] = []
        never: List[_Launch] = []  # replaced or cancelled: must never run
        ran: List[_Launch] = []  # admitted or started anyway: exactly once

        def settle(title: str) -> None:
            launch = flying.pop(title)
            launch.release.set()
            launch.thread.join(WAIT)
            assert not launch.thread.is_alive()
            out = gate.last_outcome(title)
            assert out["started"] is (launch.error is None)
            assert out["error"] == (launch.error or "")

        try:
            for op, arg, outcome in ops:
                if op == "enqueue":
                    if arg in flying:
                        continue  # one launch per session at a time
                    launch = _Launch(arg, outcome)
                    every.append(launch)
                    rec = gate.enqueue(arg, launch, kind="spawn")
                    if arg in queued:
                        never.append(queued[arg])  # replaced, place kept
                    else:
                        queue.append(arg)
                    queued[arg] = launch
                    assert rec["position"] == queue.index(arg) + 1
                elif op == "drain":
                    got = gate.drain_once(wait=False)
                    if not queue or level["now"] != "ok":
                        assert got is None
                    else:
                        head = queue.pop(0)
                        assert got == head
                        launch = queued.pop(head)
                        assert launch.entered.wait(WAIT)
                        flying[head] = launch
                        ran.append(launch)
                elif op == "cancel":
                    assert gate.cancel(arg) is (arg in queue)
                    if arg in queue:
                        queue.remove(arg)
                        never.append(queued.pop(arg))
                        assert gate.last_outcome(arg)["error"] == "cancelled"
                elif op == "start_now":
                    got = gate.start_now(arg)
                    if arg in queue:
                        queue.remove(arg)
                        launch = queued.pop(arg)
                        ran.append(launch)
                        assert got == (True, launch.error)
                        assert launch.thread is threading.current_thread()
                    elif arg in flying:
                        assert got == (True, "still starting")
                    else:
                        assert got == (False, None)
                elif op == "finish":
                    if arg in flying:
                        settle(arg)
                else:
                    level["now"] = arg
                _check(queue, list(flying), every)
            for title in list(flying):
                settle(title)
            _check(queue, [], every)
            assert all(launch.runs == 0 for launch in never)
            assert all(launch.runs == 1 for launch in ran)
            assert all(launch.runs == 0 for launch in queued.values())
        finally:
            for launch in every:
                launch.release.set()
            for launch in every:
                if (
                    launch.thread is not None
                    and launch.thread is not threading.current_thread()
                ):
                    launch.thread.join(WAIT)
