"""``backend.web.core.loop_watchdog`` — a stalled event loop writes down why.

Pins: no dump while the heartbeat is fresh; one dump (header + every
thread's stack) when it goes stale, naming the blocking frame; no repeat
inside REPEAT_SECONDS, a repeat after it; one "answered again" line on
recovery; and the real thing end to end: a loop blocked by a sync call is
caught by the real watchdog thread.
"""

from __future__ import annotations

import asyncio
import io
import time

from backend.web.core import loop_watchdog as wd


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _dog(clock, out):
    return wd.Watchdog(
        lambda: out,
        stall_s=10.0,
        repeat_s=60.0,
        clock=clock,
        wall=lambda: 0.0,
        context=lambda: "memory low, 1.0 of 20.0 GB available",
    )


def test_fresh_heartbeat_writes_nothing():
    clock, out = _Clock(), io.StringIO()
    dog = _dog(clock, out)
    wd.beat(clock.t)
    clock.t += 9.0
    assert dog.check() is None
    assert out.getvalue() == ""


def test_a_stall_dumps_once_then_repeats_then_recovers():
    clock, out = _Clock(), io.StringIO()
    dog = _dog(clock, out)
    wd.beat(clock.t)
    clock.t += 12.0
    assert dog.check() == "stalled"
    text = out.getvalue()
    assert "event loop stalled for 12 s" in text
    assert "memory low, 1.0 of 20.0 GB available" in text
    # every thread's stack, this test's frame among them
    assert "test_a_stall_dumps_once_then_repeats_then_recovers" in text
    clock.t += 30.0
    assert dog.check() is None  # still stalled, inside REPEAT_SECONDS
    clock.t += 31.0
    assert dog.check() == "stalled"
    assert "STILL stalled for 73 s" in out.getvalue()
    assert dog.dumps == 2
    wd.beat(clock.t)  # the loop runs again
    assert dog.check() == "recovered"
    assert "answered again after 73 s" in out.getvalue()
    assert dog.check() is None


def test_no_output_file_is_no_crash():
    clock = _Clock()
    dog = wd.Watchdog(lambda: None, stall_s=1.0, clock=clock, context=lambda: "")
    wd.beat(clock.t)
    clock.t += 5
    assert dog.check() == "stalled" and dog.dumps == 0


def test_a_blocked_loop_is_caught_by_the_real_thread(monkeypatch, tmp_path):
    """The heartbeat coroutine on a real loop, the watchdog thread checking
    every 50 ms, and a sync ``time.sleep`` on the loop thread: the dump
    names the blocking frame."""
    path = tmp_path / "stacks.log"
    fh = open(path, "a", buffering=1)
    monkeypatch.setattr(wd, "_FH", fh)
    monkeypatch.setattr(wd, "BEAT_SECONDS", 0.02)
    monkeypatch.setattr(wd, "CHECK_SECONDS", 0.05)
    monkeypatch.setenv("MINDFLOCK_LOOP_STALL_S", "0.3")
    monkeypatch.setenv("MINDFLOCK_LOOP_WATCHDOG", "1")

    def blocking_handler_on_the_loop():
        time.sleep(0.9)

    async def main():
        hb = asyncio.create_task(wd.heartbeat_loop())
        await asyncio.sleep(0.1)
        assert wd.start()
        await asyncio.sleep(0.1)
        blocking_handler_on_the_loop()
        await asyncio.sleep(0.3)  # beats again: the recovery line
        hb.cancel()

    try:
        asyncio.run(main())
    finally:
        wd.stop()
        fh.close()
    text = path.read_text()
    assert "event loop stalled" in text
    assert "blocking_handler_on_the_loop" in text
    assert "answered again" in text


def test_off_switch(monkeypatch):
    monkeypatch.setenv("MINDFLOCK_LOOP_WATCHDOG", "0")
    assert wd.start() is False
    assert wd._THREAD is None
