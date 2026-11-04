"""The server stays responsive under memory pressure.

Pins (2026-11-04: the server stopped answering HTTP under a memory squeeze):

* the always-on ticks run on their OWN bounded pool, so a tick stuck on a
  slow git/tmux/gh child can't hold the threads every request handler's
  ``await asyncio.to_thread`` waits on;
* while memory reads low the background work sheds itself: the probe pool
  narrows, PR lookups serve their cached answer past its TTL instead of
  forking ``gh``, Ctrl+F's pre-indexing skips its pass;
* the memory watch gives back the speech models the server holds (the
  resident whisper models included) once they sit unused.
"""

from __future__ import annotations

import asyncio
import threading
import time

from backend.web import server
from backend.web.core import speech


def test_ticks_run_on_their_own_pool_not_the_handlers_executor():
    names = []

    async def main():
        names.append(await server._in_bg_pool(lambda: threading.current_thread().name))

    asyncio.run(main())
    assert names[0].startswith("mf-tick")


def test_stuck_ticks_cannot_starve_request_handlers():
    """Every tick thread blocked (a hung child under memory pressure): a
    handler's ``asyncio.to_thread`` still answers at once."""
    release = threading.Event()

    async def main():
        stuck = [
            asyncio.ensure_future(server._in_bg_pool(release.wait, 5.0))
            for _ in range(server._BG_POOL_WORKERS + 2)
        ]
        await asyncio.sleep(0.05)
        started = time.monotonic()
        got = await asyncio.wait_for(asyncio.to_thread(lambda: 42), 2.0)
        took = time.monotonic() - started
        release.set()
        await asyncio.gather(*stuck)
        return got, took

    got, took = asyncio.run(main())
    assert got == 42 and took < 1.0


def test_the_probe_pool_narrows_while_memory_is_low(monkeypatch):
    widths = []

    class _Pool:
        def __init__(self, max_workers, thread_name_prefix):
            widths.append(max_workers)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def map(self, fn, items):
            return [fn(i) for i in items]

    class _Inst:
        def __init__(self, t):
            self.Title = t

    monkeypatch.setattr(server.concurrent.futures, "ThreadPoolExecutor", _Pool)
    monkeypatch.setattr(server, "_session_snapshot", lambda i, q: {"title": i.Title})
    monkeypatch.setattr(server._lanes, "fill_duplicates", lambda rows: None)
    monkeypatch.setattr(server.ENGINE, "instances", {t: _Inst(t) for t in "abcdefghij"})
    monkeypatch.setattr(server, "_memory_low", lambda: False)
    server._build_instances_snapshot()
    monkeypatch.setattr(server, "_memory_low", lambda: True)
    server._build_instances_snapshot()
    assert widths == [server._PROBE_POOL_MAX, server._PROBE_POOL_LOW]


def test_pr_lookups_serve_a_stale_answer_instead_of_forking_gh(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "gh_available", lambda: True)
    monkeypatch.setattr(
        server, "_gh_pr_info", lambda wt, b: calls.append(b) or {"url": "u2"}
    )
    monkeypatch.setitem(
        server._PR_CACHE, "br-x", (time.time() - 5, {"url": "u1"}, time.time())
    )
    monkeypatch.setattr(server, "_memory_low", lambda: True)
    assert server._pr_info("/wt", "br-x") == {"url": "u1"} and calls == []
    # a person pressing something still asks
    assert server._pr_info("/wt", "br-x", force=True) == {"url": "u2"}
    monkeypatch.setitem(
        server._PR_CACHE, "br-x", (time.time() - 5, {"url": "u1"}, time.time())
    )
    monkeypatch.setattr(server, "_memory_low", lambda: False)
    assert server._pr_info("/wt", "br-x") == {"url": "u2"}
    assert calls == ["br-x", "br-x"]


def test_merge_and_review_state_keep_their_cache_while_low(monkeypatch):
    asked = []
    monkeypatch.setattr(
        server._github_pr,
        "pr_merge_state_sync",
        lambda wt, b: asked.append("merge") or {"ok": 1},
    )
    monkeypatch.setattr(
        server._github_pr,
        "pr_review_state_sync",
        lambda wt, n: asked.append("review") or "approved",
    )
    monkeypatch.setitem(server._MERGE_STATE_CACHE, "br-y", (time.time() - 1, None))
    monkeypatch.setitem(server._REVIEW_CACHE, "br-y", (time.time() - 1, ""))
    monkeypatch.setattr(server, "_memory_low", lambda: True)
    assert server._pr_merge_state("/wt", "br-y") is None
    assert server._pr_review_state("/wt", "br-y", {"number": 7}) == ""
    assert asked == []


def test_find_indexing_skips_its_pass_while_memory_is_low(monkeypatch):
    touched = []
    monkeypatch.setattr(server, "_memory_low", lambda: True)
    monkeypatch.setattr(
        server._events, "sessions_snapshot", lambda: touched.append(1) or []
    )
    server._find_index_tick()
    assert touched == []


def test_shedding_drops_idle_speech_models_resident_ones_too(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(speech, "_now", lambda: clock[0])
    monkeypatch.setattr(speech, "_CACHE", {})
    monkeypatch.setattr(speech, "_USED", {})
    monkeypatch.setattr(speech, "_WARMED", {"stt": ("loaded", "small", 0.0)})
    monkeypatch.setattr(speech, "_resident", lambda: frozenset({"whisper:small"}))
    speech._CACHE.update({"whisper:small": object(), "piper:amy": object()})
    speech._USED.update({"whisper:small": 700.0, "piper:amy": 990.0})
    assert speech.sweep_idle() == []  # the everyday sweep keeps the resident one
    assert speech.shed_for_memory() == ["whisper:small"]  # piper used 10 s ago
    assert "whisper:small" not in speech._CACHE and "piper:amy" in speech._CACHE
    assert "stt" not in speech._WARMED  # the next warm loads it again


def test_the_memory_watch_sheds_what_the_server_holds(monkeypatch):
    shed = []
    monkeypatch.setattr(speech, "shed_for_memory", lambda: shed.append(1) or ["w"])
    assert server._shed_for_memory() == ["w"] and shed == [1]
