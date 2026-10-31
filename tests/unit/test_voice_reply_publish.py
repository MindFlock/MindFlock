"""A turn's reply is published the moment the Stop hook files it.

``core.state_events.ReplyWatch`` notices ``<marker dir>/<session>.reply.json``
landing (one stat of the directory per poll, debounced per session) and
server.py's ``_reply_watch_loop`` hands the session to ``_republish_reply``,
which refreshes the turn-end fields and emits through ``_emit_state_changes``
— so the settle and boot-quiet windows hold exactly as they do for the 4s
tick. Before this, the reply (and its ``Say:`` line) waited for that tick.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import time

import pytest

from backend.config import settings as S
from backend.providers import activity_markers as am
from backend.session import tmux
from backend.web import server
from backend.web.core import events as events_mod
from backend.web.core import asking, spoken, state_events
from backend.web.core.events import EventBus
from backend.web.core.state_events import ReplyWatch


def _land(directory, name, text="Done.\nSay: All green.", ts=None):
    """File a reply exactly the way the Stop hook does (tmp + os.replace)."""
    os.makedirs(directory, exist_ok=True)
    rp = os.path.join(directory, name + ".reply.json")
    rt = rp + ".%d.tmp" % os.getpid()
    with open(rt, "w") as fh:
        fh.write(json.dumps({"ts": int(ts or time.time()), "text": text}))
    os.replace(rt, rp)


# ---- ReplyWatch ------------------------------------------------------------ #
def test_watch_baselines_then_reports_a_landed_reply_once(tmp_path):
    d = tmp_path / "m"
    _land(d, "old")
    w = ReplyWatch(lambda: d, quiet_s=0.03)
    assert w.poll(now=1.0) == []  # baseline: a reply already there is old news
    _land(d, "s1")
    assert w.poll(now=1.01) == []  # seen, still inside the quiet period
    assert w.poll(now=1.05) == ["s1"]
    assert w.poll(now=1.10) == []  # once per landing
    _land(d, "old")  # a NEW reply for a session that had one counts
    w.poll(now=1.20)
    assert w.poll(now=1.30) == ["old"]


def test_watch_collapses_a_burst_into_one(tmp_path):
    d = tmp_path / "m"
    d.mkdir()
    w = ReplyWatch(lambda: d, quiet_s=0.03, rescan_s=0.0)
    w.poll(now=1.0)
    out = []
    t = 1.0
    for i in range(4):  # a reply rewritten every 10ms
        _land(d, "s1", text="v%d" % i)
        t += 0.01
        out += w.poll(now=t)
    t += 0.05
    out += w.poll(now=t)
    out += w.poll(now=t + 0.1)
    assert out == ["s1"]


def test_watch_does_not_hold_a_reply_that_keeps_changing_forever(tmp_path):
    d = tmp_path / "m"
    d.mkdir()
    w = ReplyWatch(lambda: d, quiet_s=0.03, max_wait_s=0.5, rescan_s=0.0)
    w.poll(now=0.0)
    out = []
    for i in range(1, 30):
        _land(d, "s1", text="v%d" % i)
        out += w.poll(now=i * 0.02)
    assert "s1" in out  # published by the max-wait cap, not starved


def test_watch_survives_a_missing_directory(tmp_path):
    d = tmp_path / "later"
    w = ReplyWatch(lambda: d, quiet_s=0.0)
    assert w.poll(now=1.0) == []
    _land(d, "s1")  # the directory appears WITH a reply: that's the baseline
    assert w.poll(now=2.0) == []
    _land(d, "s2")
    assert w.poll(now=3.0) == ["s2"]


def test_watch_ignores_other_marker_files(tmp_path):
    d = tmp_path / "m"
    d.mkdir()
    w = ReplyWatch(lambda: d, quiet_s=0.0)
    w.poll(now=1.0)
    (d / "s1.json").write_text('{"state": "idle", "ts": 1}')
    (d / "s1.reply.json.123.tmp").write_text("{}")
    assert w.poll(now=2.0) == []


def test_watch_never_names_a_lone_tmp_file(tmp_path):
    """A hook that crashed between write and os.replace leaves only its
    ``<name>.reply.json.<pid>.tmp``: never a reply, however often rescanned."""
    d = tmp_path / "m"
    d.mkdir()
    w = ReplyWatch(lambda: d, quiet_s=0.0, rescan_s=0.0)
    w.poll(now=1.0)
    (d / "s1.reply.json.4242.tmp").write_text('{"ts": 1, "text": "half"}')
    assert [w.poll(now=1.0 + i) for i in range(1, 4)] == [[], [], []]


def _age_dir(d, seconds=10):
    """Stamp ``d``'s mtime ``seconds`` in the past and return it (ns): an
    old mtime is a trusted cache key (no young-mtime rescan)."""
    ns = time.time_ns() - int(seconds * 1e9)
    os.utime(d, ns=(ns, ns))
    return ns


def test_watch_young_dir_mtime_is_not_trusted(tmp_path):
    """Two replaces inside one kernel mtime tick leave the directory key
    unmoved; a young mtime must not be cached, so the next poll rescans."""
    d = tmp_path / "m"
    d.mkdir()
    w = ReplyWatch(lambda: d, quiet_s=0.0, rescan_s=1000.0)
    # Every mtime reads young — deterministic, unlike a real 50ms window.
    w.YOUNG_NS = 10**15
    w.poll(now=1.0)
    _land(d, "s1", text="v1")
    assert w.poll(now=1.1) == ["s1"]
    m = os.stat(d).st_mtime_ns
    _land(d, "s1", text="v2")
    os.utime(d, ns=(m, m))  # the second write landed in the same mtime tick
    assert w.poll(now=1.2) == ["s1"]


def test_watch_without_the_young_guard_waits_for_the_rescan(tmp_path):
    """The control for the test above, and the ``rescan_s`` fallback: a
    trusted directory key that does not move hides a rewrite until the
    periodic rescan, and no longer."""
    d = tmp_path / "m"
    d.mkdir()
    old = _age_dir(d)
    w = ReplyWatch(lambda: d, quiet_s=0.03, rescan_s=1.0)
    w.poll(now=10.0)  # baseline; the old mtime is cached as the key
    _land(d, "s1")
    os.utime(d, ns=(old, old))  # a coarse-mtime mount: the dir never moved
    assert w.poll(now=10.5) == []
    assert w.poll(now=10.9) == []
    assert w.poll(now=11.0) == []  # rescanned: seen, inside the quiet period
    assert w.poll(now=11.1) == ["s1"]
    assert w.poll(now=12.2) == []  # the next rescan finds nothing new


def test_watch_a_replaced_directory_reports_rather_than_rebaselines(tmp_path):
    d = tmp_path / "m"
    d.mkdir()
    _land(d, "s1", text="v1")
    old = _age_dir(d)
    w = ReplyWatch(lambda: d, quiet_s=0.0, rescan_s=1000.0)
    w.poll(now=1.0)
    # rm -rf + recreate, built side by side so the new inode cannot be a
    # reused one; same mtime, so only the inode moves the key.
    fresh = tmp_path / "fresh"
    _land(fresh, "s1", text="v1")
    _land(fresh, "s2")
    ino = os.stat(d).st_ino
    os.rename(d, tmp_path / "gone")
    os.rename(fresh, d)
    os.utime(d, ns=(old, old))
    assert os.stat(d).st_ino != ino
    assert sorted(w.poll(now=2.0)) == ["s1", "s2"]


def test_watch_a_deleted_reply_is_silent_and_a_new_inode_counts(tmp_path):
    d = tmp_path / "m"
    d.mkdir()
    _land(d, "s1")
    w = ReplyWatch(lambda: d, quiet_s=0.0, rescan_s=0.0)
    w.poll(now=1.0)
    os.unlink(d / "s1.reply.json")
    assert w.poll(now=2.0) == []  # gone between polls: nothing, no raise
    _land(d, "s1", text="same")
    assert w.poll(now=3.0) == ["s1"]
    before = os.stat(d / "s1.reply.json")
    _land(d, "s1", text="same")  # identical bytes, os.replace'd in
    p = d / "s1.reply.json"
    os.utime(p, ns=(before.st_mtime_ns, before.st_mtime_ns))
    after = os.stat(p)
    assert (after.st_mtime_ns, after.st_size) == (
        before.st_mtime_ns,
        before.st_size,
    )
    assert after.st_ino != before.st_ino
    assert w.poll(now=4.0) == ["s1"]


def test_watch_a_raising_directory_callable_is_survived(tmp_path):
    d = tmp_path / "m"
    d.mkdir()
    _land(d, "old")
    broken = {"on": True}

    def directory():
        if broken["on"]:
            raise RuntimeError("marker_dir blew up")
        return d

    w = ReplyWatch(directory, quiet_s=0.0, rescan_s=0.0)
    assert w.poll(now=1.0) == []
    broken["on"] = False
    assert w.poll(now=2.0) == []  # first good scan baselines: "old" is old
    _land(d, "s1")
    assert w.poll(now=3.0) == ["s1"]


def test_watch_debounces_each_session_on_its_own_clock(tmp_path):
    """Two sessions land in one poll; one keeps rewriting. The quiet one is
    released after ``quiet_s``, the busy one only by ``max_wait_s``."""
    d = tmp_path / "m"
    d.mkdir()
    w = ReplyWatch(lambda: d, quiet_s=0.03, max_wait_s=0.5, rescan_s=0.0)
    w.poll(now=0.0)
    _land(d, "a", text="a0")
    _land(d, "b")
    released = {}
    t = 0.01
    for name in w.poll(now=t):
        released.setdefault(name, []).append(t)
    i = 0
    while t < 0.8:
        i += 1
        t = round(t + 0.02, 2)
        if "a" not in released:
            _land(d, "a", text="a%d" % i)  # keeps changing until let out
        for name in w.poll(now=t):
            released.setdefault(name, []).append(t)
    assert released["b"] == [0.05]  # 0.04 after it landed: quiet_s, not more
    assert len(released["a"]) == 1  # held, then out exactly once
    assert 0.5 <= released["a"][0] - 0.01 < 0.53  # by the max-wait cap


# ---- the loop: a landing publishes once, within ~100ms ----------------------- #
class _Inst:
    Program = "claude"

    def __init__(self, title):
        self.Title = title
        self.Status = type("St", (), {"name": "Running"})()

    def GetWorktreePath(self):  # noqa: N802
        return "/tmp/wt"


def test_reply_landing_publishes_exactly_once_within_100ms(monkeypatch, tmp_path):
    markers = tmp_path / "markers"
    monkeypatch.setenv("MINDFLOCK_ACTIVITY_MARKER_DIR", str(markers))
    monkeypatch.setattr(server.ENGINE, "instances", {"t": _Inst("t"), "u": _Inst("u")})
    monkeypatch.setattr(server, "_REPLY_WATCH_LOOP_ENABLED", True)
    calls = []
    monkeypatch.setattr(
        server,
        "_republish_reply",
        lambda title: calls.append((title, time.monotonic())),
    )
    name = am.safe_session_name(tmux.to_mindflock_tmux_name("t"))
    _land(markers, am.safe_session_name(tmux.to_mindflock_tmux_name("u")))

    async def run():
        task = asyncio.create_task(server._reply_watch_loop())
        await asyncio.sleep(0.15)  # baseline pass
        landed = time.monotonic()
        _land(markers, name)
        deadline = landed + 3.0
        while not calls and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.3)  # long enough for a duplicate to show up
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return landed

    landed = asyncio.run(run())
    assert [c[0] for c in calls] == ["t"]  # once, and only the session that ended
    lag = calls[0][1] - landed
    # ~100ms by design (≤2 polls of 50ms). The bound is generous on purpose:
    # under full-suite load the event loop can stall for hundreds of ms (see
    # the prompt-queue drain flakes); the deterministic ReplyWatch tests above
    # pin the timing, this is the real-time smoke check.
    print("reply->publish lag ms", round(lag * 1000))
    assert lag < 1.5, lag


def test_reply_watch_loop_is_off_under_pytest_by_default(monkeypatch):
    monkeypatch.setattr(server, "_REPLY_WATCH_LOOP_ENABLED", None)
    assert server._reply_watch_loop_enabled() is False


def test_reply_watch_loop_enabled_overrides(monkeypatch):
    monkeypatch.setattr(server._restart, "_under_pytest", lambda: False)
    monkeypatch.setattr(server, "_REPLY_WATCH_LOOP_ENABLED", None)
    assert server._reply_watch_loop_enabled() is True  # on in a real server
    monkeypatch.setattr(server, "_REPLY_WATCH_LOOP_ENABLED", False)
    assert server._reply_watch_loop_enabled() is False
    monkeypatch.setattr(server._restart, "_under_pytest", lambda: True)
    monkeypatch.setattr(server, "_REPLY_WATCH_LOOP_ENABLED", True)
    assert server._reply_watch_loop_enabled() is True


def test_reply_watch_loop_is_registered_by_the_lifespan():
    assert "_register_task(_reply_watch_loop())" in inspect.getsource(server.lifespan)


def test_reply_watch_loop_returns_at_once_under_pytest(monkeypatch):
    """Off by default here: it must not so much as look at the real marker
    directory (ENGINE is the developer's live state under pytest)."""
    monkeypatch.setattr(server, "_REPLY_WATCH_LOOP_ENABLED", None)

    def _touched():
        raise AssertionError("the real marker dir was read")

    monkeypatch.setattr(am, "marker_dir", _touched)
    assert asyncio.run(asyncio.wait_for(server._reply_watch_loop(), 1.0)) is None


def test_reply_titles_maps_safe_names_back_to_live_titles(monkeypatch):
    titles = ["a b", "ab", "a/b", "a.b", "café", "other", "dev::ab"]
    monkeypatch.setattr(server.ENGINE, "instances", {t: _Inst(t) for t in titles})
    names = {am.safe_session_name(tmux.to_mindflock_tmux_name(t)) for t in titles}
    assert "mindflock_ab" in names and "mindflock_a_b" in names  # collisions
    got = server._reply_titles(
        ["mindflock_ab", "mindflock_a_b", "mindflock_caf_", "mindflock_ghost"]
    )
    # Every title behind a colliding safe name comes back; a name with no
    # live session is dropped; "other" and a device-qualified title don't
    # match a bare local name.
    assert sorted(got) == sorted(["a b", "ab", "a/b", "a.b", "café"])
    assert server._reply_titles([]) == []


def test_reply_watch_loop_survives_a_raising_poll_and_republish(monkeypatch, tmp_path):
    markers = tmp_path / "markers"
    markers.mkdir()
    monkeypatch.setenv("MINDFLOCK_ACTIVITY_MARKER_DIR", str(markers))
    monkeypatch.setattr(server.ENGINE, "instances", {"t": _Inst("t")})
    monkeypatch.setattr(server, "_REPLY_WATCH_LOOP_ENABLED", True)
    monkeypatch.setattr(server, "_REPLY_WATCH_INTERVAL", 0.01)
    polls = {"n": 0}

    class _Flaky(ReplyWatch):
        def poll(self, now=None):
            polls["n"] += 1
            if polls["n"] == 1:
                raise RuntimeError("poll blew up")
            return super().poll(now)

    monkeypatch.setattr(server, "_ReplyWatch", _Flaky)
    calls = []

    def _republish(title):
        calls.append(title)
        if len(calls) == 1:
            raise RuntimeError("republish blew up")

    monkeypatch.setattr(server, "_republish_reply", _republish)
    name = am.safe_session_name(tmux.to_mindflock_tmux_name("t"))

    async def wait_for(cond, timeout=3.0):
        end = time.monotonic() + timeout
        while not cond() and time.monotonic() < end:
            await asyncio.sleep(0.01)

    async def run():
        task = asyncio.create_task(server._reply_watch_loop())
        await wait_for(lambda: polls["n"] >= 3)  # past the raise + baseline
        _land(markers, name, text="first")
        await wait_for(lambda: len(calls) >= 1)
        _land(markers, name, text="second")
        await wait_for(lambda: len(calls) >= 2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run())
    assert calls == ["t", "t"]  # the loop outlived both raises


# ---- _republish_reply: fresh reading, same quiet windows --------------------- #
def _forget_asking(title="t"):
    """core.asking memoizes per title; a sibling test's question must not
    become this test's ``asking`` (the reply files live in per-test dirs)."""
    for d in (asking._STATE, asking._INPUT_AT, asking._DIALOG_MEMO):
        d.pop(title, None)


@pytest.fixture()
def bus(monkeypatch, tmp_path, isolate_settings_store):
    S.update_settings(notifications={"prose_questions": True})
    _forget_asking()
    b = EventBus()
    monkeypatch.setattr(events_mod, "BUS", b)
    server._EVENT_SNAPSHOT.clear()
    state_events._SPOKEN_ANNOUNCED.clear()
    state_events._REPLY_SEEN.clear()
    state_events._REPLY_WORK.clear()
    spoken._REPLY_CACHE.clear()
    monkeypatch.setenv("MINDFLOCK_ACTIVITY_MARKER_DIR", str(tmp_path / "markers"))
    monkeypatch.setenv("MINDFLOCK_VOICE_FLAG_DIR", str(tmp_path / "voice"))
    # Never the live loop against the real ENGINE, whatever a test does.
    monkeypatch.setattr(server, "_REPLY_WATCH_LOOP_ENABLED", False)
    monkeypatch.setattr(server, "_REPLY_REPUBLISHED_AT", {})
    monkeypatch.setattr(server, "_in_boot_quiet", lambda: False)
    monkeypatch.setattr(server._prompt_queue, "peek_next", lambda title: None)
    monkeypatch.setattr(server._agent_state, "claim_work", lambda t: 5.0)
    inst = _Inst("t")
    monkeypatch.setattr(server.ENGINE, "instances", {"t": inst})
    monkeypatch.setattr(server, "_session_stage_cached", lambda i: {"stage": "agent"})
    monkeypatch.setattr(server, "_background_jobs", lambda i, t: None)
    monkeypatch.setattr(server, "_session_last_turn", lambda i: None)
    reading = {"v": "idle"}
    monkeypatch.setattr(server, "_agent_activity", lambda i, t: reading["v"])
    # A Stop-hook idle is authoritative (no 3s settle) unless a test says not.
    monkeypatch.setattr(
        server._agent_state, "reading_is_authoritative", lambda t, v: True
    )
    events_mod.set_sessions_snapshot(
        [{"title": "t", "activity": "working", "stage": "agent", "last_say": None}]
    )
    # The session was working, and the memo still says so.
    server._emit_state_changes("t", "running", "working", "agent")
    server._probe_seed("activity", inst, "working")
    yield {"bus": b, "inst": inst, "reading": reading}
    with server._PROBE_CACHE_LOCK:
        for p in server._REPLY_PROBES:
            server._PROBE_CACHE.pop((p, "t"), None)
    events_mod.set_sessions_snapshot([])
    server._EVENT_SNAPSHOT.clear()
    state_events._SPOKEN_ANNOUNCED.clear()
    state_events._REPLY_SEEN.clear()
    state_events._REPLY_WORK.clear()
    spoken._REPLY_CACHE.clear()
    _forget_asking()


def _land_for(title, text):
    _land(
        str(am.marker_dir()),
        am.safe_session_name(tmux.to_mindflock_tmux_name(title)),
        text,
    )


def _idle(got):
    return [
        e
        for e in got
        if e["event"] == "session.activity_changed" and e.get("new") == "idle"
    ]


def test_republish_reply_announces_the_idle_with_its_say_line(bus):
    got = []
    bus["bus"].subscribe(got.append)
    _land_for("t", "Ran the suite.\nSay: All green, push it?")
    row = server._republish_reply("t")
    assert row["activity"] == "idle"  # the stale "working" memo was dropped
    assert row["last_say"] == "All green, push it?"
    published = [r for r in events_mod.sessions_snapshot() if r["title"] == "t"]
    assert published[0]["activity"] == "idle"
    idle = _idle(got)
    assert len(idle) == 1
    assert idle[0]["data"]["say"] == "All green, push it?"
    # The 4s tick that follows does not announce it again.
    got.clear()
    server._emit_state_changes("t", "running", "idle", "agent")
    assert _idle(got) == []


def test_republish_reply_inside_boot_quiet_emits_nothing(bus, monkeypatch):
    monkeypatch.setattr(server, "_in_boot_quiet", lambda: True)
    got = []
    bus["bus"].subscribe(got.append)
    _land_for("t", "Done.\nSay: Done.")
    row = server._republish_reply("t")
    assert row["activity"] == "idle"  # the row is fresh…
    assert [e for e in got if e["event"].startswith("session.")] == []  # …silently


def test_republish_reply_keeps_the_settle_for_a_pane_reading(bus, monkeypatch):
    monkeypatch.setattr(
        server._agent_state, "reading_is_authoritative", lambda t, v: False
    )
    monkeypatch.setattr(server, "_ACTIVITY_SETTLE_SECONDS", 3.0)
    got = []
    bus["bus"].subscribe(got.append)
    _land_for("t", "Done.\nSay: Done.")
    server._republish_reply("t")
    assert _idle(got) == []  # parked: a misread idle still needs a 2nd sighting


def test_republish_reply_falls_back_to_full_republish_for_an_unpublished_row(
    bus, monkeypatch
):
    events_mod.set_sessions_snapshot([])
    seen = []
    monkeypatch.setattr(server, "_republish_session", lambda t: seen.append(t) or {})
    server._republish_reply("t")
    assert seen == ["t"]


def test_republish_reply_for_a_gone_session_is_none(bus):
    assert server._republish_reply("nope") is None


def _published(title="t"):
    return [r for r in events_mod.sessions_snapshot() if r["title"] == title][0]


def test_republish_reply_patches_only_the_turn_end_fields(bus, monkeypatch):
    git = {
        "diff_stat": {"added": 7, "removed": 3},
        "stage": "committed",
        "branch": "feature/sentinel",
        "autopilot": {"depth": "push"},
    }
    events_mod.set_sessions_snapshot(
        [
            {
                "title": "t",
                "activity": "working",
                "activity_since": 1.0,
                "asking": {"text": "stale?"},
                "background": None,
                "last_turn": None,
                "last_say": None,
                **git,
            }
        ]
    )
    jobs = [{"id": "j1", "state": "running"}]
    monkeypatch.setattr(server, "_background_jobs", lambda i, t: jobs)
    monkeypatch.setattr(server, "_session_last_turn", lambda i: "2026-10-31T12:00")
    monkeypatch.setattr(server._agent_state, "state_since", lambda t: 4242.0)
    _land_for("t", "Fixed it.\nSay: Fixed.")
    server._republish_reply("t")
    row = _published()
    for k, v in git.items():
        assert row[k] == v, k  # git state is the 4s tick's business
    assert row["activity"] == "idle"
    assert row["activity_since"] == 4242.0
    assert row["asking"] is None  # "Fixed." asks nothing
    assert row["background"] == jobs
    assert row["last_turn"] == "2026-10-31T12:00"
    assert row["last_say"] == "Fixed."


def test_republish_reply_drops_exactly_the_reply_probe_memos(bus):
    inst = bus["inst"]
    for p in ("activity", "asking", "last_turn", "last_say", "stage", "diff"):
        server._probe_seed(p, inst, "STALE")
    with server._PROBE_CACHE_LOCK:
        kept = {p: server._PROBE_CACHE[(p, "t")] for p in ("stage", "diff")}
    _land_for("t", "Done.\nSay: Done.")
    server._republish_reply("t")
    with server._PROBE_CACHE_LOCK:
        for p in server._REPLY_PROBES:  # recomputed past the stale memo
            hit = server._PROBE_CACHE.get((p, "t"))
            assert hit is None or hit[2] != "STALE", p
        for p, entry in kept.items():  # untouched, the very same entry
            assert server._PROBE_CACHE.get((p, "t")) is entry, p
        server._PROBE_CACHE.pop(("diff", "t"), None)
        server._PROBE_CACHE.pop(("stage", "t"), None)
    assert set(server._REPLY_PROBES) == {"activity", "asking", "last_turn", "last_say"}


def test_republish_reply_does_not_hasten_turn_ended(bus, monkeypatch):
    """The reply makes the idle instant, not the turn-end announcement: the
    marker lane's dwell still runs on the tick, and that tick's turn_ended
    carries the spoken line once (its own channel's memory)."""
    monkeypatch.setattr(server._agent_state, "work_evidence", lambda t: "marker")
    monkeypatch.setattr(server._agent_state, "worked_at", lambda t: time.time() - 100)
    monkeypatch.setattr(server, "_tmux_client_input_recent", lambda t, w: False)
    monkeypatch.setattr(server, "_autopilot_running", lambda t: False)
    monkeypatch.setattr(server, "_HUMAN_INPUT_AT", {})
    got = []
    bus["bus"].subscribe(got.append)
    _land_for("t", "Shipped.\nSay: Shipped it.")
    server._republish_reply("t")
    assert len(_idle(got)) == 1
    assert [e for e in got if e["event"] == "session.turn_ended"] == []
    # The dwell (12s marker lane) passes; the 4s tick comes round.
    server._EVENT_SNAPSHOT["t"]["activity_at"] -= server._TURN_END_DWELL_MARKER_S + 1
    got.clear()
    server._emit_state_changes("t", "running", "idle", "agent")
    ended = [e for e in got if e["event"] == "session.turn_ended"]
    assert len(ended) == 1
    assert ended[0]["data"]["say"] == "Shipped it."
    assert _idle(got) == []  # the idle was the reply watch's, not repeated
    # Another claimable cycle with no new reply: the turn channel has
    # already spoken this one.
    got.clear()
    server._emit_state_changes("t", "running", "idle", "agent")
    ended = [e for e in got if e["event"] == "session.turn_ended"]
    assert len(ended) == 1 and ended[0]["data"]["say"] is None


def test_republish_reply_with_an_empty_reply_announces_a_silent_idle(bus, monkeypatch):
    """A Codex-style Stop payload with no last_assistant_message files an
    empty reply: the idle still goes out, with nothing to say."""
    monkeypatch.setattr(spoken, "_last_assistant_text", lambda inst, name: None)
    got = []
    bus["bus"].subscribe(got.append)
    _land_for("t", "")
    row = server._republish_reply("t")
    assert row is not None and row["activity"] == "idle"
    assert row["last_say"] is None
    idle = _idle(got)
    assert len(idle) == 1
    assert idle[0]["data"]["say"] is None
    assert idle[0]["data"]["source"] in ("none", "digest")


def _activity_events(got):
    return [
        (e.get("old"), e.get("new"))
        for e in got
        if e["event"] == "session.activity_changed"
    ]


def test_a_tick_that_read_working_before_the_reply_neither_flaps_nor_regresses(
    bus, monkeypatch
):
    """The instances tick builds every row (seconds on a big flock), THEN
    emits and publishes. A reply republished while it was building must win:
    no idle→working→idle flick, and the published row stays idle."""
    for name in ("_check_session_budget", "_prune_session_state"):
        monkeypatch.setattr(server, name, lambda *a, **k: None)
    monkeypatch.setattr(server, "_orphan_dangling_parents", lambda: None)
    monkeypatch.setattr(server._stage_reset, "prune", lambda titles: None)
    monkeypatch.setattr(server._revive, "observe", lambda *a, **k: None)
    monkeypatch.setattr(server._revive, "auto_tick", lambda srv: None)
    got = []
    bus["bus"].subscribe(got.append)

    def build():
        stale = dict(_published(), activity="working", check=None)
        _land_for("t", "Done.\nSay: All set.")
        server._republish_reply("t")  # lands mid-build
        return [stale]

    monkeypatch.setattr(server, "_build_instances_snapshot", build)
    server._instances_tick()
    assert _activity_events(got) == [("working", "idle")]
    assert _idle(got)[0]["data"]["say"] == "All set."
    assert _published()["activity"] == "idle"
    # The next tick reads after the reply: business as usual.
    monkeypatch.setattr(
        server,
        "_build_instances_snapshot",
        lambda: [dict(_published(), check=None)],
    )
    server._instances_tick()
    assert _activity_events(got) == [("working", "idle")]
    assert _published()["activity"] == "idle"


def test_the_events_tick_skips_a_reading_a_reply_overtook(bus, monkeypatch):
    got = []
    bus["bus"].subscribe(got.append)

    def slow_stale_read(inst, title):
        _land_for("t", "Done.\nSay: All set.")
        server._republish_reply("t")  # lands while this read is in flight
        return "working"

    monkeypatch.setattr(server, "_agent_activity_cached", slow_stale_read)
    server._tick_state_changes()
    assert _activity_events(got) == [("working", "idle")]


def test_republish_reply_reads_past_a_stale_memo_stored_after_the_drop(
    bus, monkeypatch
):
    """A reader that began before the hook can store its ``working`` after the
    memo drop; the republish computes the reading itself and donates it."""
    orig = server._agent_activity

    def racing(inst, title):
        server._probe_seed("activity", inst, "working")  # the late store
        return orig(inst, title)

    monkeypatch.setattr(server, "_agent_activity", racing)
    _land_for("t", "Done.\nSay: Done.")
    assert server._republish_reply("t")["activity"] == "idle"
    with server._PROBE_CACHE_LOCK:
        assert server._PROBE_CACHE[("activity", "t")][2] == "idle"
