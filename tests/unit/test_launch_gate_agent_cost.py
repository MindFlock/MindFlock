"""The launch gate sizes one more session by its AGENT (2026-11-12).

On 2026-11-12, with no session opened or closed, ``capacity.session_mb``
went 637 → 1,780 → 1,220 → 709 MB and ``room`` 17 → 5 → 7 → 9 in three
minutes: one session ran a test suite in its terminal, and the gate's cost
was the 75th percentile of each session's WHOLE process tree. That run was
counted twice: once in what the machine has available (right) and again as
the cost of every future session (wrong). The cost is now ``agent_mb +
mcp_mb`` (the agent CLI and the MCP servers it started); what else runs in
the panes counts once, through available memory and the pressure readings.

Pinned here, on ``core.memory_watch``'s fake ``/proc`` and the gate's seams:

* the 2026-11-12 shape: four sessions of ~520-700 MB of agent, one of them
  also running a ~4 GB test run: ``session_mb`` does not move when the run
  starts or stops, ``room`` moves only by what the run takes from available
  memory; the old whole-tree rule's numbers are pinned alongside;
* a machine reading distressed (PSI, swap-in, OOM kills, Windows' commit
  charge) holds an automatic start and asks a person, whatever the room
  by arithmetic;
* the warm-up protections stand: a warming agent counts at the learned
  agent peak (700 MB with too few readings), an unmeasured one at all of it;
* a sleeping, closed or remote session, or one whose agent has exited, is
  not a sample.
"""

from __future__ import annotations

import sys
import time
from types import SimpleNamespace

import pytest

from backend import memory_pressure as mp
from backend.web.core import launch_gate as gate
from backend.web.core import memory_watch as mw
from tests.unit.test_memory_watch import fake  # noqa: F401 — the fake /proc fixture

GiB = 1024**3
MiB = 1024**2
TOTAL = 26 * GiB  # the owner's laptop
#: The low line on 26 GB at the default 8 %: max(2 GiB, 0.08 × 26 GiB).
RESERVE = int(0.08 * TOTAL)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    gate._reset_for_tests()
    monkeypatch.setattr(gate, "AUTO_DRAIN", False)
    monkeypatch.setattr(gate, "_PROBE_SECONDS", 0.0)
    for k in ("MINDFLOCK_AGENT_WARMUP_MB", "MINDFLOCK_AUTO_LAUNCH_SPACING"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MINDFLOCK_MAX_LIVE_AGENTS", "0")
    monkeypatch.setattr(mp, "_share_settings", lambda: (8, 4))
    gate.set_probe(lambda: [])
    yield
    gate.set_probe(None)
    gate._reset_for_tests()


def _ok_memory(monkeypatch, available: int) -> dict:
    """A healthy reading on 26 GB (no swap-in, no stall): the room is pure
    arithmetic. Returns the dict the gate reads, to move ``available``."""
    snap = {
        "level": "ok",
        "available": int(available),
        "total": TOTAL,
        "swap_total": 8 * GiB,
        "swap_free": 8 * GiB,
        "swap_in_mb_s": 0.0,
        "psi": {"some_avg10": 0.0, "some_avg60": 0.0},
    }
    monkeypatch.setattr(mp, "snapshot", lambda fresh=False: dict(snap))
    return snap


# --------------------------------------------------------------------------- #
# the 2026-11-12 shape, on the sampler's fake /proc
# --------------------------------------------------------------------------- #
#: (title, agent CLI MB): with its 25 MB MindFlock MCP server the agents read
#: 520 / 600 / 650 / 700 MB.
SESSIONS = [("w1", 495), ("w2", 575), ("w3", 625), ("w4", 675)]
TEST_RUN_MB = 4000


def _session(fp, base: int, cli_mb: int) -> int:
    """A launcher ``sh -c`` pane → claude → the MindFlock MCP server, and the
    Bash tool's idle zsh. Returns the pane pid."""
    fp.add(base, 1, "sh", ["/usr/bin/sh", "-c", "claude"], 2, 2)
    fp.add(base + 1, base, "claude", ["claude", "--mcp-config=x"], cli_mb, cli_mb)
    fp.add(base + 2, base + 1, "python", ["/v/bin/python", "-m", "backend.mcp"], 25, 25)
    fp.add(base + 3, base + 1, "zsh", ["/usr/bin/zsh", "-c", "true"], 3, 3)
    return base


def _test_run(fp, base: int, on: bool) -> None:
    """``pytest -n 8`` under the Bash tool's zsh of the session at ``base``."""
    pid = base + 4
    if on:
        fp.add(
            pid,
            base + 3,
            "python",
            ["/v/bin/python", "-m", "pytest"],
            TEST_RUN_MB,
            TEST_RUN_MB,
        )
    else:
        fp.remove(pid)


def _wire_sampler(monkeypatch, fp):
    """Four sessions on the fake /proc; the gate reads the sampler through
    its default seams (as in the server). Returns ``tick()``."""
    monkeypatch.setitem(sys.modules, "backend.web.server", SimpleNamespace())
    gate.set_sizes_source(None)
    gate.set_agent_sizes_source(None)
    panes = {}
    for i, (title, cli) in enumerate(SESSIONS):
        panes[gate._tmux_name(title)] = [_session(fp, 100 * (i + 1), cli)]
    gate.set_probe(lambda: [(n, time.time() - 3600) for n in panes])
    pairs = [(t, gate._tmux_name(t)) for t, _ in SESSIONS]
    return lambda: mw.sample(pairs, panes)


def _old_rule(snap: dict) -> dict:
    """What the gate said before 2026-11-12: the same learned cost and budget
    over each session's WHOLE tree (``memory_watch.session_mb``)."""
    mb, peak = gate.session_sizes()
    return gate.budget(snap, gate.learned_cost(mb, peak), [], live=4, cap=0)


def test_a_test_run_in_one_session_leaves_the_cost_alone(fake, monkeypatch):
    tick = _wire_sampler(monkeypatch, fake)
    idle_avail = 14 * GiB
    snap = _ok_memory(monkeypatch, idle_avail)
    w3 = 300  # the third session's pane

    seen = []
    for running in (False, True, False):
        if running:
            _test_run(fake, w3, True)
            snap["available"] = idle_avail - TEST_RUN_MB * MiB
        elif seen:
            _test_run(fake, w3, False)
            snap["available"] = idle_avail
        tick()
        cap = gate.capacity()
        old = _old_rule(snap)
        seen.append((cap["session_mb"], cap["room"], old["session_mb"], old["room"]))

    # The sampler still shows the run, in the session that runs it.
    rows = mw.snapshot()["sessions"]
    assert {r["title"] for r in rows} == {"w1", "w2", "w3", "w4"}

    # Agent cost: p75 of 520/600/650/700 = 662.5 → 662 MB, start to finish.
    # Room: (14 GiB − 2.08 GiB reserve) / 662 MB = 18; while the run takes
    # 4,000 MB of available, (10,336 − 2,130) / 662 = 12; back to 18.
    assert [s[:2] for s in seen] == [(662, 18), (662, 12), (662, 18)]
    # The old whole-tree rule on the same machine: p75 of 525/605/655/705 =
    # 668 MB, then 525/605/705/4,655 = 1,692 MB while the run is on: room
    # 18 → 4 → 18 (the run counted twice: 4,000 MB out of available AND
    # every future session costed at 1.7 GB).
    assert [s[2:] for s in seen] == [(668, 18), (1692, 4), (668, 18)]
    # The room moves only by what the run takes: 4,000 MB / 662 MB = 6.
    assert seen[0][1] - seen[1][1] == int(TEST_RUN_MB // 662)


def test_the_agent_figure_is_the_cli_and_its_mcp_servers(fake, monkeypatch):
    tick = _wire_sampler(monkeypatch, fake)
    _test_run(fake, 300, True)
    tick()
    mb, peak = gate.agent_sizes()
    assert mb == {"w1": 520, "w2": 600, "w3": 650, "w4": 700}
    assert peak == mb
    whole, whole_peak = gate.session_sizes()
    # The memory shield still sizes a session by what sleeping it frees.
    assert whole["w3"] == 650 + 5 + TEST_RUN_MB and whole_peak["w3"] == whole["w3"]
    row = mw.row("w3")
    assert (row["agent_mb"] + row["mcp_mb"], row["other_mb"]) == (650, 5 + TEST_RUN_MB)


def test_the_agent_peak_ignores_the_test_run_and_resets_per_incarnation(
    fake, monkeypatch
):
    tick = _wire_sampler(monkeypatch, fake)
    tick()
    fake.add(301, 300, "claude", ["claude"], 900, 900)  # the agent grows
    _test_run(fake, 300, True)
    tick()
    fake.add(301, 300, "claude", ["claude"], 625, 625)  # and settles back
    _test_run(fake, 300, False)
    tick()
    assert mw.session_agent_mb()["w3"] == 650
    assert mw.session_agent_peak_mb()["w3"] == 925  # its own peak, not 4.9 GB
    assert mw.session_peak_mb()["w3"] == 925 + 5 + TEST_RUN_MB


# --------------------------------------------------------------------------- #
# distress stops starts on its own, whatever the arithmetic
# --------------------------------------------------------------------------- #
def _machine(monkeypatch, *, some60=0.0, swapin=None, oom=0, host=None):
    """The REAL ``memory_pressure.snapshot`` over faked kernel readings:
    26 GB with 20 GB available (room by arithmetic ≈ 30 agents at 600 MB)."""
    from backend import host_memory

    monkeypatch.setenv("MINDFLOCK_MEMORY_GUARD", "1")
    monkeypatch.setattr(mp, "SUSTAIN_SECONDS", 0.0)
    mp._reset_for_tests()
    monkeypatch.setattr(
        mp,
        "_read_meminfo",
        lambda: {
            "total": TOTAL,
            "available": 20 * GiB,
            "swap_total": 8 * GiB,
            "swap_free": 8 * GiB,
        },
    )
    monkeypatch.setattr(
        mp,
        "_read_psi",
        lambda: {
            "some_avg10": some60,
            "some_avg60": some60,
            "full_avg10": 0.0,
            "full_avg60": 0.0,
        },
    )
    monkeypatch.setattr(mp, "_read_vmstat", lambda: None)
    monkeypatch.setattr(mp, "_swapin_rate", lambda vm, mono: swapin)
    monkeypatch.setattr(mp, "_oom_recent", lambda vm, mono: oom)
    monkeypatch.setattr(host_memory, "reading", lambda: host)
    agents = {"a": 600, "b": 600, "c": 600}
    gate.set_agent_sizes_source(lambda: (dict(agents), dict(agents)))
    gate.set_sizes_source(lambda: (dict(agents), dict(agents)))


_WINDOWS_COMMIT_FULL = {
    "total": 32 * GiB,
    "available": 12 * GiB,
    "committed": 98 * GiB,
    "commit_limit": 100 * GiB,
    "pages_in_median": 0,
    "at": 0,
}


@pytest.mark.parametrize(
    "signals, level",
    [
        ({"some60": 55.0}, "critical"),  # PSI: stalled 55 % of the last minute
        ({"some60": 25.0}, "low"),
        ({"swapin": 3000.0}, "critical"),  # pages/s swapped back in
        ({"swapin": 300.0}, "low"),
        ({"oom": 1}, "low"),  # an OOM kill alone
        ({"oom": 1, "some60": 25.0}, "critical"),  # with company
        ({"host": _WINDOWS_COMMIT_FULL}, "critical"),  # Windows' commit charge
    ],
)
def test_a_distressed_machine_holds_starts_whatever_the_room(
    monkeypatch, signals, level
):
    _machine(monkeypatch, **signals)
    a = gate.assess(fresh=True)
    assert a["level"] == level
    b = a["budget"]
    # By arithmetic there is room for many more agents...
    assert b["session_mb"] == 600 and b["free_mb"] // b["session_mb"] >= 15
    # ... and none is given: the level alone stops automatic starts...
    assert b["room"] == 0
    v = gate.decide("auto", fresh=True)
    assert (v["decision"], v["why"]) == ("wait", "memory")
    rec = gate.gate_auto("w-%s" % level, lambda: None, kind="spawn")
    assert rec is not None and rec["why"] == "memory"
    # ... and a person is asked (refused on critical) before one more starts.
    p = gate.decide("person", fresh=True)
    assert p["decision"] == ("refuse" if level == "critical" else "confirm")
    assert gate.person_refusal(p)["memory_confirm"] is True


def test_the_same_machine_without_distress_has_the_room(monkeypatch):
    _machine(monkeypatch)
    a = gate.assess(fresh=True)
    assert a["level"] == "ok"
    assert a["budget"]["room"] == int((20 * GiB - RESERVE) // (600 * MiB))
    assert gate.decide("auto", fresh=True)["decision"] == "go"


# --------------------------------------------------------------------------- #
# the warm-up protections stand
# --------------------------------------------------------------------------- #
def test_a_warming_agent_still_counts_at_the_learned_agent_peak(monkeypatch):
    _ok_memory(monkeypatch, 10 * GiB)
    now = time.time()
    gate.set_probe(lambda: [(gate._tmux_name("new"), now - 10)])
    gate.set_agent_sizes_source(
        lambda: (
            {"a": 600, "b": 600, "c": 600, "new": 150},
            {"a": 900, "b": 900, "c": 900},
        )
    )
    a = gate.assess(now=now)
    assert a["cost"]["session_mb"] == 600  # "new" is not a steady sample
    assert a["to_come"] == (900 - 150) * MiB


def test_too_few_agent_readings_keep_the_700_mb_default(monkeypatch):
    _ok_memory(monkeypatch, 10 * GiB)
    now = time.time()
    gate.set_probe(lambda: [(gate._tmux_name("new"), now - 10)])
    gate.set_agent_sizes_source(lambda: ({"a": 300, "b": 300}, {"a": 320}))
    a = gate.assess(now=now)
    assert a["cost"] == {
        "session_mb": 700,
        "peak_mb": 700,
        "samples": 0,
        "source": "default",
    }
    # Not measured yet: all of the default still to come.
    assert a["to_come"] == 700 * MiB


def test_small_agents_never_read_under_the_floor(monkeypatch):
    _ok_memory(monkeypatch, 10 * GiB)
    gate.set_agent_sizes_source(lambda: ({"a": 90, "b": 110, "c": 120}, {}))
    assert gate.assess()["cost"]["session_mb"] == gate.COST_FLOOR_MB


# --------------------------------------------------------------------------- #
# asleep, closed, remote, exited: not samples
# --------------------------------------------------------------------------- #
def test_sleeping_closed_and_exited_sessions_read_nothing(fake, monkeypatch):
    tick = _wire_sampler(monkeypatch, fake)
    # w1's agent exited: only its launcher shell is left in the pane.
    fake.remove(103)
    fake.remove(102)
    fake.remove(101)
    # w2 was put to sleep / closed: its tmux session (and panes) are gone.
    for pid in (200, 201, 202, 203):
        fake.remove(pid)
    tick()
    mb, peak = gate.agent_sizes()
    assert mb == {"w3": 650, "w4": 700} and set(peak) == {"w3", "w4"}


def test_the_rows_fallback_reads_the_agent_and_skips_asleep_and_remote(monkeypatch):
    from backend.web.core import events

    monkeypatch.setitem(sys.modules, "backend.web.server", SimpleNamespace())
    monkeypatch.setattr(mw, "session_agent_mb", lambda: {})
    monkeypatch.setattr(mw, "session_agent_peak_mb", lambda: {})
    rows = [
        {
            "title": "a",
            "memory": {"mb": 4700, "agent_mb": 600, "mcp_mb": 50, "other_mb": 4050},
        },
        {"title": "b", "memory": {"mb": 700, "agent_mb": "640", "mcp_mb": 20}},
        {"title": "x", "memory": {"mb": 10, "agent_mb": 0, "other_mb": 10}},
        {"title": "z", "sleeping": True, "memory": {"mb": 900, "agent_mb": 900}},
        {"title": "r", "device": "mac", "memory": {"mb": 900, "agent_mb": 900}},
        {"title": "old", "memory": {"mb": 900}},
    ]
    monkeypatch.setattr(events, "sessions_snapshot", lambda: rows)
    mb, peak = gate._default_agent_sizes()
    assert mb == {"a": 650, "b": 660} and peak == {}
