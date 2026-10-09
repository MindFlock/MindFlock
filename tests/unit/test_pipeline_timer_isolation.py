"""A pipeline reconcile one test schedules never fires inside the next.

settings_hooks debounces the reconcile by 5 s on a timer thread, which
outlives the test that armed it; tests/conftest.py cancels it after every
test. The two tests below run in file order (``-p no:randomly``): the first
arms a short timer and leaves it, the second watches the bus past its
deadline. In the reverse order (random) they pass trivially."""

import threading

from backend.web.core import events, settings_hooks


def test_a_test_that_leaves_a_reconcile_pending(monkeypatch):
    monkeypatch.setattr(settings_hooks, "PIPELINE_DEBOUNCE", 0.3)
    settings_hooks._schedule_pipeline()


def test_the_next_test_never_sees_it_fire():
    seen = []
    unsubscribe = events.BUS.subscribe(seen.append)
    try:
        threading.Event().wait(0.6)
    finally:
        unsubscribe()
    assert settings_hooks.PIPELINE_EVENT not in [e["event"] for e in seen]
