"""The memory shield (2026-11-05: the PC had to be rebooted, WSL swapping
under ~10 agents, a 5 GB app and Voice Lab's 4 GB F5 voice).

Pinned here, on fake snapshots and fakes only (nothing real is slept,
stopped or launched):

* the admission budget — the learned per-session cost (75th percentile of
  the sampler's readings, warming agents at the learned peak), the reserve
  and its penalties, the room it gives, and the gate / dialog / capacity
  wording built on it;
* the shed — when (critical for a minute, or PSI full avg10), how much (back
  over the low line), which (largest-and-longest-idle first, never a
  working / asking / viewed / driven / background session), the one
  notice, and the ``agent_memory.shed_idle`` switch;
* Voice Lab yields — no local voice host is woken or called while memory
  reads low (the fallback speaks), and its backend unit is stopped at
  critical through a fake systemctl.
"""

from __future__ import annotations

import subprocess
import time
from types import SimpleNamespace

import pytest

from backend import memory_pressure
from backend.web.core import launch_gate as gate
from backend.web.core import memory_shield as shield

GiB = 1024**3
MiB = 1024**2


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    gate._reset_for_tests()
    shield._reset_for_tests()
    monkeypatch.setattr(gate, "AUTO_DRAIN", False)
    for k in (
        "MINDFLOCK_AGENT_WARMUP_MB",
        "MINDFLOCK_MAX_LIVE_AGENTS",
        "MINDFLOCK_AUTO_LAUNCH_SPACING",
        "MINDFLOCK_SHED_IDLE",
        "MINDFLOCK_VOICELAB_UNIT",
    ):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MINDFLOCK_MAX_LIVE_AGENTS", "0")
    gate.set_probe(lambda: [])
    yield
    gate.set_probe(None)
    gate._reset_for_tests()
    shield._reset_for_tests()


def _snap(level="ok", available=10 * GiB, total=20 * GiB, **kw):
    snap = {
        "level": level,
        "available": available,
        "total": total,
        "available_pct": round(available * 100.0 / total, 1),
        "swap_total": kw.pop("swap_total", 0),
        "swap_free": kw.pop("swap_free", 0),
        "psi": kw.pop("psi", None),
    }
    snap.update(kw)
    return snap


def _memory(monkeypatch, **kw):
    snap = _snap(**kw)
    monkeypatch.setattr(memory_pressure, "snapshot", lambda fresh=False: dict(snap))
    monkeypatch.setattr(
        memory_pressure,
        "thresholds",
        lambda: {
            "low": (0.15, 2 * GiB, 20.0, 5.0),
            "critical": (0.07, 1 * GiB, 40.0, 15.0),
        },
    )
    return snap


def _cost(session=1000, peak=1500, source="measured", samples=5):
    return {
        "session_mb": session,
        "peak_mb": peak,
        "source": source,
        "samples": samples,
    }


# --------------------------------------------------------------------------- #
# the learned cost
# --------------------------------------------------------------------------- #
def test_too_few_readings_keep_the_default_cost():
    c = gate.learned_cost({"a": 900, "b": 1100}, {"a": 1200}, default_mb=700)
    assert c == {"session_mb": 700, "peak_mb": 700, "samples": 0, "source": "default"}


def test_the_cost_is_the_75th_percentile_of_steady_sessions():
    mb = {"a": 400, "b": 600, "c": 800, "d": 1000, "e": 3000}
    c = gate.learned_cost(mb, {}, default_mb=700)
    # sorted 400 600 800 1000 3000: p75 at index 3 = 1000 (one outlier
    # running a test suite does not set the price).
    assert c["session_mb"] == 1000 and c["source"] == "measured"
    assert c["samples"] == 5
    # no peaks measured: a warming agent counts at the default, never under
    # the steady cost.
    assert c["peak_mb"] == 1000


def test_warming_and_empty_sessions_are_not_steady_samples():
    mb = {"a": 900, "b": 900, "c": 900, "new": 100, "asleep": 10}
    c = gate.learned_cost(mb, {}, warming={"new"}, default_mb=700)
    assert c["session_mb"] == 900 and c["samples"] == 3
    c = gate.learned_cost({"a": 900, "b": 900, "new": 900}, {}, warming={"new"})
    assert c["source"] == "default"  # only two steady readings left


def test_the_peak_is_learned_and_capped():
    mb = {"a": 800, "b": 800, "c": 800}
    c = gate.learned_cost(mb, {"a": 1200, "b": 1400, "c": 1600})
    assert c["peak_mb"] == 1500  # p75 of 1200 1400 1600
    # A whole-life peak from an 8-worker test run is not what a CLI starting
    # grows to: at most 2.5x the steady cost.
    c = gate.learned_cost(mb, {"a": 6000, "b": 7000, "c": 8000})
    assert c["peak_mb"] == 2000


def test_the_cost_has_a_floor():
    c = gate.learned_cost({"a": 60, "b": 70, "c": 80}, {})
    assert c["session_mb"] == gate.COST_FLOOR_MB


# --------------------------------------------------------------------------- #
# the budget
# --------------------------------------------------------------------------- #
def test_room_is_available_minus_reserve_over_the_session_cost():
    b = gate.budget(_snap(available=10 * GiB), _cost(), reserve=3 * GiB)
    # (10 - 3) GB / 1000 MB = 7.168 → 7
    assert b["room"] == 7
    assert (b["available_mb"], b["reserve_mb"], b["session_mb"]) == (10240, 3072, 1000)
    assert b["short_mb"] == 0
    assert b["why"].startswith("room for 7 more sessions: 10.0 GB available")
    assert "measured over 5 sessions" in b["why"]


def test_the_reserve_is_the_low_line_never_under_2_gb(monkeypatch):
    _memory(monkeypatch)
    assert gate.reserve_bytes(20 * GiB) == 3 * GiB  # 15% of 20 GB
    assert gate.reserve_bytes(8 * GiB) == 2 * GiB  # 15% = 1.2 GB < the floor


def test_swap_in_use_and_a_rising_stall_cost_room():
    base = dict(available=10 * GiB)
    no = gate.budget(_snap(**base), _cost(), reserve=3 * GiB)
    swap = gate.budget(
        _snap(swap_total=16 * GiB, swap_free=15 * GiB, **base), _cost(), reserve=3 * GiB
    )
    assert swap["penalty_mb"] == 1024 and swap["room"] == no["room"] - 1
    # Swap is capped at 10% of RAM (2 GB here).
    big = gate.budget(
        _snap(swap_total=16 * GiB, swap_free=6 * GiB, **base), _cost(), reserve=3 * GiB
    )
    assert big["penalty_mb"] == 2048
    rising = gate.budget(
        _snap(psi={"some_avg10": 20.0, "some_avg60": 8.0}, **base),
        _cost(),
        reserve=3 * GiB,
    )
    assert rising["penalty_mb"] == 2048  # 20% stalled → 10% of RAM
    falling = gate.budget(
        _snap(psi={"some_avg10": 20.0, "some_avg60": 30.0}, **base),
        _cost(),
        reserve=3 * GiB,
    )
    assert falling["penalty_mb"] == 0


def test_warming_agents_count_at_the_learned_peak():
    # Two agents still starting: one unmeasured (all 1500 MB to come), one at
    # 600 MB already (900 MB to come) → 2.4 GB of the room is spoken for.
    b = gate.budget(_snap(available=10 * GiB), _cost(), [None, 600], reserve=3 * GiB)
    assert b["to_come_mb"] == 2400
    assert b["room"] == int((7 * 1024 - 2400) // 1000)  # 4


def test_no_room_while_low_and_none_when_unmeasured():
    assert gate.budget(_snap("low", 3 * GiB), _cost(), reserve=3 * GiB)["room"] == 0
    assert gate.budget({"level": "unknown"}, _cost())["room"] is None
    # ...though a live cap still bounds an unmeasured machine.
    b = gate.budget({"level": "unknown"}, _cost(), live=3, cap=5)
    assert b["room"] == 2


def test_the_cap_bounds_the_room():
    b = gate.budget(_snap(available=15 * GiB), _cost(), reserve=3 * GiB, live=9, cap=10)
    assert b["room"] == 1 and "automatic starts stop at 10" in b["why"]


def test_one_more_short_says_how_much_it_needs():
    b = gate.budget(_snap(available=3 * GiB + 400 * MiB), _cost(), reserve=3 * GiB)
    assert b["room"] == 0 and b["short_mb"] == 600
    assert b["why"].startswith("one more session needs ~1.0 GB; only 0.4 GB is free")
    hint = gate.capacity_hint(b)
    assert "one needs ~1000 MB, 600 MB more" in hint and "queue" in hint


def test_capacity_hints():
    assert gate.capacity_hint({"room": 2, "session_mb": 900}) == (
        "room for 2 more sessions now (~900 MB each); more will queue"
    )
    assert "not measured" in gate.capacity_hint({"room": None})
    low = gate.capacity_hint({"room": 0, "level": "low", "why": "memory is low: x"})
    assert low.startswith("no room now (memory is low: x)")


# --------------------------------------------------------------------------- #
# the gate on the budget
# --------------------------------------------------------------------------- #
def _sized(mb, peak=None):
    gate.set_sizes_source(lambda: (dict(mb), dict(peak or {})))


def test_the_gate_uses_the_measured_cost(monkeypatch):
    _memory(monkeypatch, available=10 * GiB)
    _sized({"a": 1400, "b": 1400, "c": 1400})
    a = gate.assess()
    assert a["cost"]["session_mb"] == 1400 and a["cost"]["source"] == "measured"
    # (10 - 3) GB / 1400 MB = 5.1
    assert gate.room() == 5
    cap = gate.capacity()
    assert cap["room"] == 5 and cap["session_mb"] == 1400
    assert cap["hint"].startswith("room for 5 more sessions now")


def test_a_warming_agent_counts_from_its_measured_size(monkeypatch):
    _memory(monkeypatch, available=10 * GiB)
    now = time.time()
    gate.set_probe(lambda: [("mindflock_new", now - 10)])
    _sized(
        {"a": 1000, "b": 1000, "c": 1000, "new": 400}, {"a": 1500, "b": 1500, "c": 1500}
    )
    a = gate.assess(now=now)
    # "new" grows from 400 MB to the learned 1500 MB peak: 1100 MB to come.
    assert a["to_come"] == 1100 * MiB
    assert a["budget"]["room"] == int((7 * 1024 - 1100) // 1000)


def test_an_automatic_start_waits_when_one_more_would_not_fit(monkeypatch):
    # 3.5 GB free of 20 reads ok (17.5%), but one 1 GB session would take it
    # past the 3 GB reserve.
    _memory(monkeypatch, available=int(3.5 * GiB))
    _sized({"a": 1000, "b": 1000, "c": 1000})
    v = gate.decide("auto")
    assert v["decision"] == "wait" and v["why"] == "memory"
    assert v["reason"].startswith("one more session needs ~1.0 GB")
    rec = gate.gate_auto("w1", lambda: None, kind="spawn")
    assert rec is not None and rec["reason"].startswith("one more session needs")
    # A person is asked, and the dialog's line says what the room is.
    p = gate.decide("person")
    assert p["decision"] == "confirm"
    body = gate.person_refusal(p)
    assert body["message"].startswith("Memory is tight: 3.5 GB free.")
    assert "No room for another session: one needs ~1.0 GB, 0.5 GB is free" in (
        body["message"]
    )
    assert body["capacity"] == {"room": 0, "session_mb": 1000, "reserve_mb": 3072}


def test_a_person_sees_the_room_when_memory_is_low(monkeypatch):
    _memory(monkeypatch, level="low", available=3 * GiB)
    body = gate.person_refusal(gate.decide("person"))
    assert "No room for another session" in body["message"]


def test_the_gate_status_carries_the_room(monkeypatch):
    _memory(monkeypatch, available=10 * GiB)
    st = gate.status()
    assert st["room"] == 10 and st["session_mb"] == 700  # the default cost


def test_the_default_sizes_read_the_rows_memory_field(monkeypatch):
    import sys

    from backend.web.core import events

    monkeypatch.setitem(sys.modules, "backend.web.server", SimpleNamespace())
    rows = [
        {"title": "a", "memory": {"mb": 900, "peak_mb": 1300}},
        {"title": "b", "memory": {"mb": "1100"}},
        {"title": "z", "sleeping": True, "memory": {"mb": 5}},
        {"title": "r", "device": "mac", "memory": {"mb": 99}},
        {"title": "n"},
    ]
    monkeypatch.setattr(events, "sessions_snapshot", lambda: rows)
    mb, peak = gate._default_sizes()
    assert mb == {"a": 900, "b": 1100} and peak == {"a": 1300}


# --------------------------------------------------------------------------- #
# the shed: when and how much
# --------------------------------------------------------------------------- #
def test_shed_waits_for_critical_to_hold_a_minute():
    st = {"critical_since": None, "last_shed_at": 0.0}
    crit = _snap("critical", 1 * GiB)
    assert shield.shed_due(st, crit, 1000.0) == (False, "")
    assert shield.shed_due(st, crit, 1059.0)[0] is False
    due, why = shield.shed_due(st, crit, 1060.0)
    assert due and why == "memory has been critical for 60s"
    # Recovery resets the clock.
    shield.shed_due(st, _snap("low", 2 * GiB), 1061.0)
    assert st["critical_since"] is None
    assert shield.shed_due(st, crit, 1100.0)[0] is False


def test_a_full_memory_stall_sheds_at_once():
    st = {"critical_since": None, "last_shed_at": 0.0}
    snap = _snap("low", 2 * GiB, psi={"full_avg10": 31.0})
    due, why = shield.shed_due(st, snap, 1000.0)
    assert due and "31%" in why
    assert shield.shed_due(st, _snap(psi={"full_avg10": 24.9}), 1000.0)[0] is False


def test_sheds_are_spaced():
    st = {"critical_since": 0.0, "last_shed_at": 1000.0}
    crit = _snap("critical", 1 * GiB)
    assert (
        shield.shed_due(st, crit, 1000.0 + shield.SHED_COOLDOWN_SECONDS - 1)[0] is False
    )
    assert shield.shed_due(st, crit, 1000.0 + shield.SHED_COOLDOWN_SECONDS)[0] is True


def test_need_is_what_brings_it_back_over_the_low_line():
    # Low line 3 GB (15% of 20); 1.2 GB free → ~1.8 GB to free.
    assert shield.shed_need_mb(_snap("critical", int(1.2 * GiB)), 3 * GiB) == 1843
    assert shield.shed_need_mb(_snap(available=5 * GiB), 3 * GiB) == 0


def _host(available, total=32 * GiB, level="ok"):
    """A WSL host reading as ``memory_pressure.snapshot()["host"]`` carries
    it (the low line of a 32 GB Windows is its 2 GB floor; 6% is 1.92)."""
    return {
        "available": available,
        "total": total,
        "available_pct": round(available * 100.0 / total, 1),
        "level": level,
        "at": 0.0,
    }


def test_need_is_the_bigger_of_the_guest_and_the_host_shortfall():
    # Guest 1.2 GB under a 3 GB line (1843 MB) beats Windows' 0 (at its line).
    snap = _snap("critical", int(1.2 * GiB), host=_host(2 * GiB, level="low"))
    assert shield.shed_need_mb(snap, 3 * GiB) == 1843
    # Both over their lines: nothing to free.
    snap = _snap(available=5 * GiB, host=_host(8 * GiB))
    assert shield.shed_need_mb(snap, 3 * GiB) == 0
    # The guest over its (explicit) line still sizes by Windows' shortfall.
    snap = _snap(available=5 * GiB, host=_host(1 * GiB, level="critical"))
    assert shield.shed_need_mb(snap, 3 * GiB) == round((2 - 1) * 1024)


def test_a_big_windows_sizes_its_shortfall_by_the_six_percent_share():
    # 64 GB Windows: the low line is 6% (3.84 GB), over the 2 GB floor.
    host = _host(2 * GiB, total=64 * GiB, level="low")
    snap = _snap(available=5 * GiB, host=host)
    assert shield.shed_need_mb(snap, 3 * GiB) == round((0.06 * 64 - 2) * 1024)


def test_no_host_or_a_hostless_reading_is_the_guest_need_alone():
    for host in (None, {}, {"available": 0, "total": 0}):
        snap = _snap("critical", int(1.2 * GiB), host=host)
        assert shield.shed_need_mb(snap, 3 * GiB) == 1843


# --------------------------------------------------------------------------- #
# the shed: which
# --------------------------------------------------------------------------- #
def _idle_row(**kw):
    row = {"activity": "idle", "background": [], "driver": "", "viewers": {}}
    row.update(kw)
    return row


def _cand(title, mb, idle_min, **row):
    return {
        "title": title,
        "mb": mb,
        "idle_s": idle_min * 60.0,
        "row": _idle_row(**row),
    }


def test_largest_and_longest_idle_go_first_and_only_as_many_as_needed():
    cands = [
        _cand("small-old", 300, 50),  # 300 x 50
        _cand("big-new", 2000, 3),  # 2000 x 3
        _cand("big-old", 1500, 40),  # 1500 x 40: first
        _cand("mid", 900, 30),  # 900 x 30: second
    ]
    picks = shield.choose_shed(cands, need_mb=2000, cost_mb=800)
    assert [p["title"] for p in picks] == ["big-old", "mid"]
    assert sum(p["mb"] for p in picks) >= 2000
    # Idle time counts up to an hour: a day idle is not 24x the claim.
    picks = shield.choose_shed(
        [_cand("day", 500, 24 * 60), _cand("hour-big", 900, 60)], 1, 800
    )
    assert picks[0]["title"] == "hour-big"


def test_never_working_asking_viewed_driven_or_holding_a_job():
    cands = [
        _cand("working", 3000, 60, activity="working"),
        _cand("asking", 3000, 60, activity="clarify"),
        _cand("dialog", 3000, 60, asking={"kind": "dialog"}),
        _cand("viewed", 3000, 60, viewers={"p1": {"name": "Erik"}}),
        _cand("driven", 3000, 60, driver="p1"),
        _cand("job", 3000, 60, background=[{"cmd": "pytest"}]),
        _cand("asleep", 3000, 60, sleeping={"since": 1}),
        _cand("ok", 400, 10),
    ]
    picks = shield.choose_shed(cands, need_mb=10_000, cost_mb=800)
    assert [p["title"] for p in picks] == ["ok"]


def test_unmeasured_sessions_count_at_the_learned_cost_and_the_limit_holds():
    cands = [_cand("s%d" % i, None, 10 + i) for i in range(10)]
    picks = shield.choose_shed(cands, need_mb=2500, cost_mb=800)
    assert len(picks) == 4 and all(not p["measured"] and p["mb"] == 800 for p in picks)
    assert picks[0]["title"] == "s9"  # longest idle
    assert len(shield.choose_shed(cands, 100_000, 800)) == shield.MAX_SHED_PER_PASS
    assert shield.choose_shed(cands, 0, 800) == []


def test_the_notice_text():
    assert shield.shed_notice_text(["a", "b", "c"]) == (
        "Memory critical — put 3 idle sessions to sleep (Wake brings them back): "
        "a, b, c"
    )
    assert shield.shed_notice_text(["a"]).startswith(
        "Memory critical — put 1 idle session to sleep (Wake brings it back)"
    )


# --------------------------------------------------------------------------- #
# the shed: the pass
# --------------------------------------------------------------------------- #
@pytest.fixture
def fake_pass(monkeypatch):
    """A critical machine with three idle candidates; sleeping and the notice
    are recorded, never real."""
    _memory(monkeypatch, level="critical", available=int(1.5 * GiB))
    cands = [
        dict(_cand("a", 1200, 30), inst=object()),
        dict(_cand("b", 900, 20), inst=object()),
        dict(_cand("c", 600, 10), inst=object()),
    ]
    monkeypatch.setattr(shield, "_candidates", lambda srv, now: (list(cands), {}))
    emitted = []
    from backend.web.core import events

    monkeypatch.setattr(
        events.BUS, "emit", lambda *a, **kw: emitted.append((a, kw)) or {}
    )
    slept = []

    def sleeper(inst, title, idle):
        slept.append(title)
        return None

    return SimpleNamespace(slept=slept, emitted=emitted, sleeper=sleeper)


def test_a_critical_minute_puts_the_needed_idle_agents_to_sleep(fake_pass):
    snap = memory_pressure.snapshot()
    srv = SimpleNamespace()
    t0 = 5000.0
    assert shield.tick(srv, snap, t0, sleeper=fake_pass.sleeper)["shed"] == []
    out = shield.tick(srv, snap, t0 + 61, sleeper=fake_pass.sleeper)
    # 1.5 GB free, the low line at 3 GB: ~1.5 GB to free → a (1200) + b (900).
    assert out["shed"] == ["a", "b"] == fake_pass.slept
    ((args, kw),) = fake_pass.emitted
    assert args == ("system.sessions_slept",)
    assert kw["data"]["detail"] == (
        "Memory critical — put 2 idle sessions to sleep (Wake brings them back): a, b"
    )
    # Spaced: the next pass right after does nothing.
    assert shield.tick(srv, snap, t0 + 70, sleeper=fake_pass.sleeper)["shed"] == []
    assert shield.status()["last_shed"] == ["a", "b"]


def test_a_critical_windows_alone_sheds_its_shortfall(fake_pass, monkeypatch):
    # The VM has 10 GB free (guest ok); Windows has 0.5 GB of 32 (critical).
    _memory(
        monkeypatch,
        level="critical",
        available=10 * GiB,
        guest_level="ok",
        host=_host(int(0.5 * GiB), level="critical"),
    )
    snap = memory_pressure.snapshot()
    srv = SimpleNamespace()
    assert shield.tick(srv, snap, 5000.0, sleeper=fake_pass.sleeper)["shed"] == []
    out = shield.tick(srv, snap, 5061.0, sleeper=fake_pass.sleeper)
    # Windows needs ~1.5 GB back over its 2 GB line → a (1200) + b (900).
    assert out["shed"] == ["a", "b"] == fake_pass.slept
    assert out["why"] == "memory has been critical for 61s"
    assert shield.status()["last_decision"] == (
        "memory has been critical for 61s: put 2 to sleep"
    )


def test_shed_idle_off_sheds_nothing(fake_pass, monkeypatch):
    from backend.providers import agent_limits

    monkeypatch.setattr(agent_limits, "_settings_blob", lambda: {"shed_idle": False})
    assert shield.shed_enabled() is False
    snap = memory_pressure.snapshot()
    shield.tick(SimpleNamespace(), snap, 1000.0, sleeper=fake_pass.sleeper)
    out = shield.tick(SimpleNamespace(), snap, 1061.0, sleeper=fake_pass.sleeper)
    assert out["shed"] == [] and fake_pass.slept == []
    assert "shed_idle is off" not in out["why"]  # the why is the trigger
    assert "shedding is off" in shield.status()["last_decision"]


def test_the_shed_idle_setting_round_trips(tmp_path, monkeypatch):
    from backend.config import settings as S
    from backend.providers import agent_limits

    monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(tmp_path / "s.json"))
    assert agent_limits.clean_config({"shed_idle": "no"}) == {}
    S.update_settings(general={"agent_memory": {"shed_idle": False}})
    assert S.load_settings().general.agent_memory == {"shed_idle": False}
    assert shield.shed_enabled() is False
    monkeypatch.setenv("MINDFLOCK_SHED_IDLE", "0")
    S.update_settings(general={"agent_memory": {"shed_idle": True}})
    assert shield.shed_enabled() is False  # the env var wins


def test_under_pytest_nothing_real_is_slept(fake_pass):
    snap = memory_pressure.snapshot()
    shield.tick(None, snap, 1000.0)
    assert shield.tick(None, snap, 1061.0)["shed"] == []
    assert shield.tick(SimpleNamespace(), snap, 1200.0)["shed"] == []
    assert fake_pass.slept == []


# --------------------------------------------------------------------------- #
# Voice Lab
# --------------------------------------------------------------------------- #
def test_voice_lab_is_held_back_only_while_memory_is_short():
    assert shield.voice_lab_blocked(_snap()) == ""
    assert shield.voice_lab_blocked({"level": "unknown"}) == ""
    assert shield.voice_lab_blocked(_snap("low", int(2.5 * GiB))).startswith(
        "memory is low: 2.5 GB free"
    )
    assert "critically low" in shield.voice_lab_blocked(_snap("critical", GiB))


def test_voice_lab_still_yields_to_a_windows_that_is_only_low(monkeypatch):
    """The local models now load at ``low`` (speech._memory_block holds them
    back only at critical); Voice Lab, which takes GBs, still yields."""
    snap = _snap("low", 10 * GiB, guest_level="ok", host=_host(GiB, level="low"))
    assert shield.voice_lab_blocked(snap) != ""
    monkeypatch.setattr(memory_pressure, "snapshot", lambda fresh=False: snap)
    assert shield.voice_lab_yield() != ""


def test_the_yield_is_logged_once_per_episode(monkeypatch):
    lines = []
    monkeypatch.setattr(shield, "_info", lambda fmt, *a: lines.append(fmt % a))
    snap = {"v": _snap("low", 2 * GiB)}
    monkeypatch.setattr(memory_pressure, "snapshot", lambda fresh=False: snap["v"])
    assert shield.voice_lab_yield() and shield.voice_lab_yield()
    snap["v"] = _snap()
    assert shield.voice_lab_yield() == ""
    assert len(lines) == 2
    assert "is not woken or called while memory is low" in lines[0]
    assert "may be used again" in lines[1]


class _Systemctl:
    def __init__(self, show="LoadState=loaded\nActiveState=active\n", rc=0):
        self.show, self.rc, self.calls = show, rc, []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        out = self.show if "show" in argv else ""
        return subprocess.CompletedProcess(argv, self.rc, out, "")


def test_critical_stops_a_running_voice_lab_backend():
    ctl = _Systemctl()
    assert shield.stop_voice_lab(ctl) == "stopped voicelab-backend.service"
    assert ctl.calls[-1] == ["systemctl", "--user", "stop", "voicelab-backend.service"]


@pytest.mark.parametrize(
    "show",
    [
        "LoadState=loaded\nActiveState=inactive\n",  # idle already
        "LoadState=not-found\nActiveState=inactive\n",  # no Voice Lab here
    ],
)
def test_nothing_to_stop_leaves_it_alone(show):
    ctl = _Systemctl(show)
    assert shield.stop_voice_lab(ctl) == ""
    assert not any("stop" in c for c in ctl.calls)


def test_no_systemd_or_unit_off_is_a_no_op(monkeypatch):
    ctl = _Systemctl(rc=1)  # no user manager
    assert shield.stop_voice_lab(ctl) == ""
    monkeypatch.setenv("MINDFLOCK_VOICELAB_UNIT", "0")
    ctl = _Systemctl()
    assert shield.stop_voice_lab(ctl) == "" and ctl.calls == []
    # Under pytest the real systemctl is never used.
    monkeypatch.delenv("MINDFLOCK_VOICELAB_UNIT")
    assert shield.stop_voice_lab(None) == ""


def test_the_tick_stops_voice_lab_only_at_critical(monkeypatch):
    ctl = _Systemctl()
    shield.tick(SimpleNamespace(), _snap("low", 2 * GiB), 1000.0, runner=ctl)
    assert ctl.calls == []
    out = shield.tick(SimpleNamespace(), _snap("critical", GiB), 1010.0, runner=ctl)
    assert out["voicelab"] == "stopped voicelab-backend.service"


# --------------------------------------------------------------------------- #
# Voice Lab: the speech side (no request reaches a local host while short)
# --------------------------------------------------------------------------- #
@pytest.fixture
def voice(isolate_settings_store, monkeypatch):
    from backend.config import settings as S
    from backend.web.core import speech

    class _Fallback(speech.Backend):
        kind = "tts"

        def __init__(self, id, ok=True):
            self.id, self.label, self.ok, self.spoken = id, id, ok, []

        def ready(self):
            return self.ok, "%s ready" % self.id

        def speak(self, text, voice=""):
            self.spoken.append(text)
            return b"RIFF" + self.id.encode(), "audio/wav"

    host = speech.OpenAITTS()
    piper = _Fallback("local", ok=False)  # piper is held back too while low
    system = _Fallback("system")
    monkeypatch.setattr(speech, "BACKENDS", {"stt": [], "tts": [host, piper, system]})
    S.update_settings(
        voice={"tts_backend": "openai", "tts_url": "http://127.0.0.1:8770/v1"}
    )
    S.invalidate()
    calls = []
    monkeypatch.setattr(
        speech, "_http", lambda *a, **kw: calls.append(a) or (200, "audio/mpeg", b"x")
    )
    level = {"v": "low"}
    monkeypatch.setattr(
        memory_pressure,
        "snapshot",
        lambda fresh=False: _snap(
            level["v"], 2 * GiB if level["v"] != "ok" else 9 * GiB
        ),
    )
    speech.TTS_CHAIN.reset()
    yield SimpleNamespace(
        speech=speech, host=host, piper=piper, system=system, calls=calls, level=level
    )
    speech.TTS_CHAIN.reset()


def test_a_short_machine_never_calls_the_local_voice_host(voice):
    sp = voice.speech
    engine, fell_back, why = sp.TTS_CHAIN.pick(voice.host)
    assert engine is voice.system and fell_back
    assert why.startswith("voice host held back: memory is low")
    audio, _mime, used, fb = sp.TTS_CHAIN.speak(voice.host, "All done.")
    assert used is voice.system and fb and audio == b"RIFFsystem"
    # A warm and a direct call are refused too: nothing reaches the host.
    assert voice.host.warm().startswith("skipped: voice host held back")
    with pytest.raises(sp.SpeechError):
        voice.host.speak("hi")
    assert voice.calls == []
    st = sp.TTS_CHAIN.status(voice.host, probe=False)
    assert st["held"] and st["engine"] is voice.system and st["cold"] is False


def test_with_no_local_voice_the_client_speaks_for_itself(voice):
    voice.system.ok = False
    with pytest.raises(voice.speech.SpeechError) as err:
        voice.speech.TTS_CHAIN.speak(voice.host, "All done.")
    assert "held back" in str(err.value) and not err.value.down
    assert voice.calls == []


def test_once_memory_recovers_the_host_speaks_again(voice):
    voice.level["v"] = "ok"
    voice.speech.TTS_CHAIN.reset()
    engine, fell_back, _why = voice.speech.TTS_CHAIN.pick(voice.host)
    assert engine is voice.host and not fell_back


def test_while_low_piper_says_it_and_only_critical_holds_piper_back(voice, monkeypatch):
    sp = voice.speech
    piper = sp.LocalTTS()
    monkeypatch.setattr(
        sp, "BACKENDS", {"stt": [], "tts": [voice.host, piper, voice.system]}
    )
    monkeypatch.setattr(sp, "_installed", lambda name: True)
    monkeypatch.setattr(memory_pressure, "level", lambda **kw: "low")
    engine, fell_back, why = sp.TTS_CHAIN.pick(voice.host)
    assert engine is piper and fell_back  # not the OS voice
    assert why.startswith("voice host held back: memory is low")
    voice.level["v"] = "critical"
    monkeypatch.setattr(memory_pressure, "level", lambda **kw: "critical")
    sp.TTS_CHAIN.reset()
    engine, fell_back, _why = sp.TTS_CHAIN.pick(voice.host)
    assert engine is voice.system and fell_back
    assert voice.calls == []


def test_a_remote_voice_api_is_never_held():
    from backend.web.core import speech

    assert speech.host_held_back("https://api.openai.com/v1") == ""


# --------------------------------------------------------------------------- #
# the routes
# --------------------------------------------------------------------------- #
def test_the_capacity_route_and_the_memory_status(monkeypatch):
    from fastapi.testclient import TestClient

    from backend.web import server

    _memory(monkeypatch, available=10 * GiB)
    _sized({"a": 1000, "b": 1000, "c": 1000})
    c = TestClient(server.app)
    body = c.get("/api/memory/capacity").json()
    assert body["room"] == 7 and body["session_mb"] == 1000
    assert body["available_mb"] == 10240 and body["reserve_mb"] == 3072
    assert body["why"].startswith("room for 7 more sessions")
    assert body["shield"]["shed_idle"] is True
    mem = c.get("/api/system/memory").json()
    assert mem["capacity"]["room"] == 7
    assert mem["gate"]["room"] == 7


def test_the_capacity_route_names_windows_when_it_binds(monkeypatch):
    from fastapi.testclient import TestClient

    from backend.web import server

    _memory(monkeypatch, available=10 * GiB, host=_host(6 * GiB))
    _sized({"a": 1000, "b": 1000, "c": 1000})
    body = TestClient(server.app).get("/api/memory/capacity").json()
    # VM: 10 - 3 = 7 GB; Windows: 6 - 2 = 4 GB → 4 sessions of 1000 MB.
    assert body["room"] == 4 and body["free_mb"] == 4096
    assert body["available_mb"] == 10240 and body["reserve_mb"] == 3072
    assert body["why"].startswith(
        "room for 4 more sessions: Windows has 6.0 GB available over its own "
        "reserve of 2.0 GB"
    )
    assert body["hint"].startswith("room for 4 more sessions now")


def test_unmeasured_memory_has_no_room_limit():
    from fastapi.testclient import TestClient

    from backend.web import server

    # The suite's memory guard is off (conftest): level unknown.
    body = TestClient(server.app).get("/api/memory/capacity").json()
    assert body["room"] is None and "not measured" in body["hint"]
