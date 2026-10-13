"""Background jobs a Claude Code session left running past its turn.

Live incident (sitecheck-bot7, 2026-10-13): the agent backgrounded
``until ! pgrep -f "git commit -q -F" >/dev/null; do sleep 5; done`` to wait for
its own commit. ``pgrep -f`` matches the loop's OWN shell, so the loop never
ends; ``claude agents --json`` reported the session ``busy`` because a job was
alive, and MindFlock showed it "working" for as long as nobody looked, even
though the Stop hook had written ``idle``.

Pinned here: the job classifier (running / waiting / stuck, and the
self-matching pgrep proof) against REAL child processes, the provider's
live-vs-marker arbitration, the stop route's pid check, and the autopilot and
MCP holds a waiting job imposes.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import types

import pytest
from fastapi.testclient import TestClient

from backend.providers import claude as claude_mod
from backend.providers import claude_background as bg
from backend.providers.claude import ClaudeProvider

linux_only = pytest.mark.skipif(
    not os.path.isdir("/proc/self"), reason="background-job scan reads /proc"
)

INCIDENT = (
    'until ! pgrep -f "git commit -q -F" >/dev/null; do sleep 5; done; '
    "cd /repo && git log --oneline -1"
)


# --- command parsing ---------------------------------------------------------
def test_command_of_unwraps_claude_codes_shell_preamble():
    raw = (
        "/usr/bin/zsh -c source /home/u/.claude/shell-snapshots/s.sh 2>/dev/null "
        "|| true && setopt NO_EXTENDED_GLOB && eval 'echo '\\''hi'\\'' && sleep 1' "
        "< /dev/null && pwd -P >| /tmp/claude-de2f-cwd"
    )
    assert bg.command_of(raw) == "echo 'hi' && sleep 1"
    assert bg.command_of("python3 -m http.server") == "python3 -m http.server"
    assert (
        bg.command_of("zsh -c eval 'echo '\"'\"'a b'\"'\"'' < /dev/null")
        == "echo 'a b'"
    )


@pytest.mark.parametrize(
    "command,pattern",
    [
        (INCIDENT, "git commit -q -F"),
        ("while pgrep -af pytest >/dev/null; do sleep 2; done", "pytest"),
        ("while pgrep -u me -f 'npm run build'; do sleep 1; done", "npm run build"),
        ("until ! pgrep --full vite; do sleep 1; done", "vite"),
        # Flags before the pattern, bundled or with a value.
        ("until ! pgrep -fx 'npm run build'; do sleep 1; done", "npm run build"),
        ("while pgrep -u me -f -n vite; do sleep 1; done", "vite"),
        # -x without -f still compares names.
        ("while pgrep -x node; do sleep 1; done", None),
        # Without -f pgrep matches process NAMES: the loop's shell can't match.
        ("while pgrep pytest; do sleep 2; done", None),
        # Not a wait-until-gone loop.
        ("pgrep -f pytest && echo running", None),
        ("sleep 600", None),
    ],
)
def test_pgrep_wait_pattern(command, pattern):
    assert bg._pgrep_wait_pattern(command) == pattern


def test_waits_on_itself_when_only_its_own_tree_matches():
    own = {10, 11}
    cmdlines = {
        10: "zsh -c " + INCIDENT,
        11: "sleep 5",
        20: "node /usr/bin/claude",
        21: "pgrep -f git commit -q -F",  # some loop's transient probe
    }
    assert bg.waits_on_itself(INCIDENT, own, cmdlines) is True


def test_a_real_match_outside_the_job_means_it_is_really_waiting():
    cmdlines = {10: "zsh -c " + INCIDENT, 30: "git commit -q -F /tmp/msg"}
    assert bg.waits_on_itself(INCIDENT, {10}, cmdlines) is False


def test_two_copies_of_the_same_loop_wait_on_each_other():
    cmdlines = {10: "zsh -c " + INCIDENT, 40: "bash -c " + INCIDENT}
    assert bg.waits_on_itself(INCIDENT, {10}, cmdlines) is True


def test_non_loop_command_is_never_proven_stuck():
    assert bg.waits_on_itself("sleep 600", {10}, {10: "sleep 600"}) is False


def test_a_name_only_pgrep_loop_is_never_proven_stuck():
    """``pgrep P`` (no -f) matches process names: the loop's own shell can
    never satisfy it, so there is nothing to prove."""
    cmd = "while pgrep pytest; do sleep 2; done"
    assert bg.waits_on_itself(cmd, {10}, {10: "zsh -c " + cmd}) is False


def test_regex_metacharacters_match_like_pgrep_does():
    cmd = "while pgrep -f 'pytest.*-k slow'; do sleep 2; done"
    assert bg._pgrep_wait_pattern(cmd) == "pytest.*-k slow"
    outside = {10: "zsh -c " + cmd, 30: "python -m pytest tests -k slow"}
    assert bg.waits_on_itself(cmd, {10}, outside) is False


def test_an_invalid_pattern_is_never_proven_stuck():
    """``foo[`` is not a valid regex. Real pgrep exits 2 on it, so such a loop
    ends at once and can't be the quiet job under inspection: it is never
    called self-waiting, and nothing raises out of the activity probe."""
    cmd = "until ! pgrep -f 'foo['; do sleep 1; done"
    assert bg._pgrep_wait_pattern(cmd) is None
    assert bg.waits_on_itself(cmd, {10}, {10: "zsh -c " + cmd, 30: "sleep 9"}) is False


# --- real processes ----------------------------------------------------------
@pytest.fixture
def fresh_samples(monkeypatch):
    monkeypatch.setattr(bg, "_samples", {})
    monkeypatch.setattr(bg, "_last", {})


def _spawn_job(tmp_path, command: str, task_id: str = "bjob00001"):
    """A child of THIS process shaped like a Claude Code background job: its
    stdout is ``…/tasks/<id>.output`` (pytest's is not)."""
    tasks = tmp_path / "tasks"
    tasks.mkdir(exist_ok=True)
    out = open(tasks / (task_id + ".output"), "w")
    proc = subprocess.Popen(
        ["sh", "-c", command], stdout=out, stderr=out, start_new_session=True
    )
    out.close()
    return proc


def _wait_for_job(pid, deadline_s=5.0, **kw):
    end = time.time() + deadline_s
    while time.time() < end:
        jobs = bg.jobs_under(os.getpid(), **kw) or []
        mine = [j for j in jobs if j["pid"] == pid]
        if mine:
            return mine[0]
        time.sleep(0.05)
    raise AssertionError("job %d never showed up" % pid)


@linux_only
def test_self_waiting_pgrep_loop_is_stuck(tmp_path, fresh_samples, monkeypatch):
    pattern = "mf-self-wait-%d" % os.getpid()
    proc = _spawn_job(
        tmp_path, 'until ! pgrep -f "%s" >/dev/null; do sleep 0.2; done' % pattern
    )
    try:
        monkeypatch.setattr(bg, "ACTIVE_GRACE_S", 0.0)
        job = _wait_for_job(proc.pid)
        assert job["id"] == "bjob00001"
        assert job["state"] == "stuck"
        assert "waits for itself" in job["reason"]
        assert pattern in job["command"]
    finally:
        proc.kill()
        proc.wait()


@linux_only
def test_quiet_job_is_waiting_then_stuck(tmp_path, fresh_samples, monkeypatch):
    proc = _spawn_job(tmp_path, "sleep 30")
    try:
        monkeypatch.setattr(bg, "ACTIVE_GRACE_S", 0.0)
        assert _wait_for_job(proc.pid)["state"] == "waiting"
        monkeypatch.setattr(bg, "STUCK_AFTER_S", 0.0)
        job = _wait_for_job(proc.pid)
        assert job["state"] == "stuck"
        assert job["reason"].startswith("no CPU or output for")
    finally:
        proc.kill()
        proc.wait()


@linux_only
def test_busy_job_is_running(tmp_path, fresh_samples):
    proc = _spawn_job(tmp_path, "while :; do :; done")
    try:
        time.sleep(0.5)  # burn some CPU before the first sighting
        assert _wait_for_job(proc.pid)["state"] == "running"
    finally:
        proc.kill()
        proc.wait()


@linux_only
def test_children_sharing_the_output_file_are_not_separate_jobs(
    tmp_path, fresh_samples
):
    proc = _spawn_job(tmp_path, "sleep 30 & sleep 30 & wait")
    try:
        _wait_for_job(proc.pid)
        time.sleep(0.2)
        jobs = [j for j in bg.jobs_under(os.getpid()) if j["id"] == "bjob00001"]
        assert [j["pid"] for j in jobs] == [proc.pid]
    finally:
        os.killpg(proc.pid, 9)
        proc.wait()


def test_no_pane_pid_means_unknown():
    assert bg.jobs_under(None) is None
    assert bg.jobs_under("not-a-pid") is None


@linux_only
def test_stop_kills_only_a_current_job(tmp_path, fresh_samples, monkeypatch):
    proc = _spawn_job(tmp_path, "sleep 30 & wait")
    monkeypatch.setattr(bg, "_pane_pid", lambda name: str(os.getpid()))
    try:
        _wait_for_job(proc.pid)
        # Not a job root (this test process): refused, nothing signalled.
        assert bg.stop("mindflock_x", os.getpid()) is False
        assert bg.stop("mindflock_x", proc.pid) is True
        assert proc.wait(timeout=5) is not None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


@linux_only
def test_a_three_level_tree_sharing_the_output_is_one_job(tmp_path, fresh_samples):
    proc = _spawn_job(tmp_path, "sh -c 'sh -c \"sleep 30\" & wait' & wait")
    try:
        _wait_for_job(proc.pid)
        time.sleep(0.3)
        jobs = [j for j in bg.jobs_under(os.getpid()) if j["id"] == "bjob00001"]
        assert [j["pid"] for j in jobs] == [proc.pid]
    finally:
        os.killpg(proc.pid, 9)
        proc.wait()


def test_no_proc_means_unknown(monkeypatch, fresh_samples):
    """Off Linux (no ``/proc``) the scan is None, never an exception, and a
    stale scan is not left behind for the row builder."""
    monkeypatch.setattr(bg.os.path, "isdir", lambda p: False)
    monkeypatch.setattr(bg, "_pane_pid", lambda name: "1")
    assert bg.jobs_under(1) is None
    bg._last["mindflock_s"] = (time.time(), [_job("waiting")])
    assert bg.scan("mindflock_s") is None
    assert bg.recent("mindflock_s") is None


# --- classification thresholds (a fake process table) ------------------------
NOW = 1_000_000.0
PID, START = 4242, 500


@pytest.fixture
def fake_job(tmp_path, monkeypatch, fresh_samples):
    """One job root (pid 4242) under pane 100, with a PREVIOUS sample seeded
    ``quiet`` seconds before NOW and no CPU since: its quiet time is exact."""
    out = tmp_path / "tasks" / "bfake0001.output"
    out.parent.mkdir()
    out.write_text("")
    os.utime(out, (0, 0))
    table = {
        100: {"ppid": 1, "cpu": 0, "start": 1},
        PID: {"ppid": 100, "cpu": 10, "start": START},
    }
    monkeypatch.setattr(bg, "_process_table", lambda: table)
    monkeypatch.setattr(bg, "_hz", lambda: 100)
    monkeypatch.setattr(bg, "_cmdline", lambda pid: "sh -c sleep 600")
    monkeypatch.setattr(
        bg, "_stdout_target", lambda pid: str(out) if pid == PID else "/dev/pts/1"
    )

    def classify(quiet, cpu_delta=0.0):
        bg._samples[(PID, START)] = {"ts": NOW - 10, "cpu": 0.1, "active": NOW - quiet}
        table[PID]["cpu"] = int(round((0.1 + cpu_delta) * 100))
        jobs = bg.jobs_under(100, now=NOW)
        assert [j["pid"] for j in jobs] == [PID]
        return jobs[0]

    classify.out = out
    return classify


@linux_only
def test_running_until_the_grace_runs_out(fake_job):
    assert fake_job(bg.ACTIVE_GRACE_S - 1)["state"] == "running"
    job = fake_job(bg.ACTIVE_GRACE_S)
    assert job["state"] == "waiting"
    assert job["quiet_s"] == bg.ACTIVE_GRACE_S


@linux_only
def test_waiting_until_stuck_after(fake_job):
    assert fake_job(bg.STUCK_AFTER_S - 1)["state"] == "waiting"
    job = fake_job(bg.STUCK_AFTER_S)
    assert job["state"] == "stuck"
    assert job["reason"] == "no CPU or output for 15m"


@linux_only
def test_cpu_since_the_last_sample_is_running(fake_job):
    # 1s of CPU over the 10s since the last sample: 10% of a core.
    assert fake_job(bg.STUCK_AFTER_S, cpu_delta=1.0)["state"] == "running"


@linux_only
def test_output_growth_alone_is_running(fake_job):
    """No CPU since the last sample, but the job wrote output 5s ago."""
    os.utime(fake_job.out, (NOW - 5, NOW - 5))
    job = fake_job(bg.STUCK_AFTER_S)
    assert job["state"] == "running"
    assert job["quiet_s"] == 5.0


# --- the result cache --------------------------------------------------------
def test_recent_serves_a_scan_only_within_the_ttl(monkeypatch, fresh_samples):
    clock = {"t": NOW}
    monkeypatch.setattr(bg.time, "time", lambda: clock["t"])
    monkeypatch.setattr(bg, "_pane_pid", lambda name: "100")
    monkeypatch.setattr(bg, "jobs_under", lambda pane, now=None: [_job("waiting")])
    assert bg.scan("mindflock_s")[0]["state"] == "waiting"
    clock["t"] += bg._RESULT_TTL_S
    assert bg.recent("mindflock_s")[0]["state"] == "waiting"
    clock["t"] += 0.5
    assert bg.recent("mindflock_s") is None


def test_an_unknowable_scan_drops_the_previous_one(monkeypatch, fresh_samples):
    monkeypatch.setattr(bg, "_pane_pid", lambda name: None)
    bg._last["mindflock_s"] = (time.time(), [_job("stuck")])
    assert bg.recent("mindflock_s") is not None
    assert bg.scan("mindflock_s") is None
    assert bg.recent("mindflock_s") is None


# --- stop() signalling (no real processes) -----------------------------------
@pytest.fixture
def signals(monkeypatch, fresh_samples):
    """stop() against a job root pid 4242 with every OS call recorded."""
    sent = []
    state = {"alive": False, "pgid": PID, "pgrp": 1}
    monkeypatch.setattr(bg, "_pane_pid", lambda name: "100")
    monkeypatch.setattr(bg, "jobs_under", lambda pane, now=None: [{"pid": PID}])
    monkeypatch.setattr(bg.os, "kill", lambda pid, sig: sent.append(("kill", pid, sig)))
    monkeypatch.setattr(bg.os, "killpg", lambda g, sig: sent.append(("killpg", g, sig)))
    monkeypatch.setattr(bg.os, "getpgrp", lambda: state["pgrp"])

    def getpgid(pid):
        if isinstance(state["pgid"], Exception):
            raise state["pgid"]
        return state["pgid"]

    monkeypatch.setattr(bg.os, "getpgid", getpgid)
    monkeypatch.setattr(bg, "_read_stat", lambda pid: {} if state["alive"] else None)
    bg._last["mindflock_s"] = (time.time(), [_job("stuck")])
    return sent, state


def test_stop_signals_the_group_the_job_leads(signals):
    sent, _ = signals
    assert bg.stop("mindflock_s", PID) is True
    assert sent == [("killpg", PID, bg.signal.SIGTERM)]
    assert bg.recent("mindflock_s") is None  # forgotten


@pytest.mark.parametrize("pgid,pgrp", [(77, 1), (PID, PID)])
def test_stop_never_signals_a_group_it_does_not_own(signals, pgid, pgrp):
    """Not the group leader, or the group is the server's own: the pid alone."""
    sent, state = signals
    state.update(pgid=pgid, pgrp=pgrp)
    assert bg.stop("mindflock_s", PID) is True
    assert sent == [("kill", PID, bg.signal.SIGTERM)]


def test_stop_escalates_to_sigkill_after_the_grace(signals):
    sent, state = signals
    state["alive"] = True
    assert bg.stop("mindflock_s", PID, grace_s=0.05) is True
    assert sent == [
        ("killpg", PID, bg.signal.SIGTERM),
        ("killpg", PID, bg.signal.SIGKILL),
    ]


def test_stop_refuses_a_job_that_exited_before_the_signal(signals):
    sent, state = signals
    state["pgid"] = ProcessLookupError()
    assert bg.stop("mindflock_s", PID) is False
    assert sent == []
    assert bg.recent("mindflock_s") is not None  # nothing changed: not forgotten


# --- provider arbitration ----------------------------------------------------
@pytest.fixture
def marker(tmp_path, monkeypatch):
    d = tmp_path / "activity-markers"
    d.mkdir()
    monkeypatch.setenv("MINDFLOCK_ACTIVITY_MARKER_DIR", str(d))

    def write(name, state):
        (d / (name + ".json")).write_text(
            json.dumps({"state": state, "ts": time.time()})
        )

    return write


def _job(state):
    return {
        "id": "b1",
        "pid": 1,
        "command": "x",
        "state": state,
        "reason": "",
        "started": 0.0,
        "quiet_s": 0.0,
    }


@pytest.mark.parametrize(
    "jobs,expected",
    [
        ([_job("waiting")], "idle"),
        ([_job("stuck")], "idle"),
        ([_job("stuck"), _job("running")], "working"),
        ([_job("waiting"), _job("stuck")], "idle"),
        ([], "working"),  # busy with no job: a real turn (or a sub-agent)
        (None, "working"),  # unknowable (no /proc): trust the CLI as before
    ],
)
def test_turn_ended_but_busy_reads_from_the_jobs(marker, monkeypatch, jobs, expected):
    marker("mindflock_s", "idle")
    monkeypatch.setattr(claude_mod, "_live_agent_state", lambda s: "working")
    monkeypatch.setattr(bg, "scan", lambda s: jobs)
    assert ClaudeProvider().activity_state("mindflock_s") == expected


def test_mid_turn_never_scans_and_forgets_the_last_scan(marker, monkeypatch):
    marker("mindflock_s", "working")
    monkeypatch.setattr(claude_mod, "_live_agent_state", lambda s: "working")
    monkeypatch.setattr(bg, "scan", lambda s: pytest.fail("scanned mid-turn"))
    monkeypatch.setattr(bg, "_last", {"mindflock_s": (time.time(), [_job("stuck")])})
    p = ClaudeProvider()
    assert p.activity_state("mindflock_s") == "working"
    assert p.background_jobs("mindflock_s") is None


def test_no_live_signal_forgets_the_last_scan(marker, monkeypatch):
    """Falling back to the hook marker: a scan from an earlier poll must not
    leak into the row as if it described now."""
    marker("mindflock_s", "idle")
    monkeypatch.setattr(claude_mod, "_live_agent_state", lambda s: None)
    monkeypatch.setattr(bg, "scan", lambda s: pytest.fail("scanned without live"))
    monkeypatch.setattr(bg, "_last", {"mindflock_s": (time.time(), [_job("stuck")])})
    p = ClaudeProvider()
    assert p.activity_state("mindflock_s") == "idle"
    assert p.background_jobs("mindflock_s") is None


@pytest.mark.parametrize("live", ["clarify", "limit", "idle"])
def test_only_a_busy_live_reading_scans(marker, monkeypatch, live):
    marker("mindflock_s", "idle")
    monkeypatch.setattr(claude_mod, "_live_agent_state", lambda s: live)
    monkeypatch.setattr(bg, "scan", lambda s: pytest.fail("scanned on " + live))
    assert ClaudeProvider().activity_state("mindflock_s") == live


def test_other_providers_have_no_background_jobs():
    from backend import providers
    from backend.providers.base import BaseProvider

    assert BaseProvider().background_jobs("x") is None
    # A TOML-defined CLI inherits the default.
    assert providers.resolve("aider").background_jobs("mindflock_x") is None


def test_background_jobs_serves_the_recent_scan_only(monkeypatch):
    monkeypatch.setattr(
        bg,
        "_last",
        {
            "a": (time.time(), [_job("waiting")]),
            "b": (time.time() - 3600, [_job("waiting")]),
        },
    )
    p = ClaudeProvider()
    assert p.background_jobs("a")[0]["state"] == "waiting"
    assert p.background_jobs("b") is None


# --- consumers ---------------------------------------------------------------
def test_autopilot_holds_for_a_waiting_job():
    from backend.web.core import autopilot

    rec = {"depth": "commit", "state": "running", "idle_since": 0.0}
    snap = {
        "stage": "agent",
        "activity": "idle",
        "background_waiting": True,
        "dirty": True,
        "now": 10_000.0,
    }
    action, detail = autopilot.next_action(rec, snap)
    assert action == "wait"
    assert "background job" in detail["reason"]
    snap["background_waiting"] = False
    assert autopilot.next_action(rec, snap)[0] == "commit"


@pytest.mark.parametrize("depth", ["commit", "push", "pr", "merge"])
def test_autopilot_holds_at_every_depth(depth):
    from backend.web.core import autopilot

    rec = {"depth": depth, "state": "running", "idle_since": 0.0}
    snap = {
        "stage": "agent",
        "activity": "idle",
        "background_waiting": True,
        "dirty": True,
        "now": 10_000.0,
    }
    assert autopilot.next_action(rec, snap) == (
        "wait",
        {"reason": "a background job the agent started is still waiting"},
    )


def test_autopilot_reports_a_pending_queue_before_a_waiting_job():
    from backend.web.core import autopilot

    rec = {"depth": "commit", "state": "running", "idle_since": 0.0}
    snap = {
        "stage": "agent",
        "activity": "idle",
        "background_waiting": True,
        "queue_pending": True,
        "dirty": True,
        "now": 10_000.0,
    }
    assert autopilot.next_action(rec, snap) == (
        "wait",
        {"reason": "prompt queue still has work"},
    )


def test_autopilot_without_the_key_behaves_as_before():
    from backend.web.core import autopilot

    snap = {"stage": "agent", "activity": "idle", "dirty": True, "now": 10_000.0}
    rec = {"depth": "commit", "state": "running", "idle_since": 0.0}
    assert autopilot.next_action(rec, snap)[0] == "commit"
    # Idle too briefly: still the dwell's own wait, not the background one.
    rec["idle_since"] = 9_999.0
    action, detail = autopilot.next_action(rec, snap)
    assert action == "wait" and "background" not in detail.get("reason", "")


@pytest.fixture
def snapshot_harness(monkeypatch, tmp_path):
    """``_autopilot_snapshot`` with every probe stubbed but the background one."""
    from backend.web import server

    monkeypatch.setenv("MINDFLOCK_AUTOPILOT_FILE", str(tmp_path / "ap.json"))
    monkeypatch.setattr(server.tmux, "to_mindflock_tmux_name", lambda t: t)
    monkeypatch.setattr(server, "_refresh_limit_state", lambda inst, title, name: 0.0)
    monkeypatch.setattr(server._prompt_queue, "get_state", lambda t: {"items": []})
    monkeypatch.setattr(server, "_is_dirty", lambda wt: True)
    monkeypatch.setattr(server, "_has_origin", lambda wt: True)
    monkeypatch.setattr(server, "_session_base_branch", lambda inst: "main")
    monkeypatch.setattr(server, "_commits_beyond_base", lambda wt, base: 0)
    monkeypatch.setattr(server, "_current_branch", lambda wt: "feature/x")
    monkeypatch.setattr(server._wt_setup, "check_summary", lambda wt: None)
    inst = types.SimpleNamespace(Title="t", Program="claude")

    def snap(activity, jobs):
        monkeypatch.setattr(server, "_agent_activity", lambda i, t: activity)
        if isinstance(jobs, Exception):

            def boom(i, t):
                raise jobs

            monkeypatch.setattr(server, "_background_jobs", boom)
        else:
            monkeypatch.setattr(server, "_background_jobs", lambda i, t: jobs)
        return server._autopilot_snapshot(inst, "t", "/wt", {"stage": "agent"})

    return snap


@pytest.mark.parametrize(
    "activity,jobs,expected",
    [
        ("idle", [_job("waiting")], True),
        ("idle", [_job("stuck"), _job("waiting")], True),
        ("working", [_job("waiting")], False),  # the agent gate already holds
        ("idle", [_job("stuck")], False),  # a stuck job never holds autopilot
        ("idle", None, False),
        ("idle", RuntimeError("probe"), False),  # detection never breaks a pass
    ],
)
def test_autopilot_snapshot_background_waiting(
    snapshot_harness, activity, jobs, expected
):
    snap = snapshot_harness(activity, jobs)
    assert snap["background_waiting"] is expected
    assert snap["stage"] == "agent"


def test_mcp_background_waiting_helper():
    from backend.mcp import tools

    assert tools.background_waiting([_job("waiting")]) is True
    assert tools.background_waiting([_job("stuck"), _job("running")]) is False
    assert tools.background_waiting(None) is False
    for junk in ("x", [None], ["waiting"], [{"state": "stuck"}], {"state": "waiting"}):
        assert tools.background_waiting(junk) is False
    assert "background" in tools._COMPACT_OPTIONAL
    assert "background" in tools._D_LIST


def test_mcp_list_sessions_carries_background_only_when_set():
    from tests.unit._mcp_fakes import FakeCtx, make_box, row

    rows = [
        row("orch"),
        row("w1", parent="orch", background=[_job("waiting")]),
        row("w2", parent="orch", background=None),
    ]
    box, api, clock = make_box(rows)
    out = box.list_sessions({"filter": "all"}, FakeCtx(clock))
    by = {r["title"]: r for r in out["sessions"]}
    assert by["w1"]["background"][0]["state"] == "waiting"
    assert "background" not in by["w2"]


def test_mcp_wait_does_not_call_a_session_with_a_waiting_job_idle():
    from tests.unit._mcp_fakes import FakeCtx, make_box, row

    rows = [
        row("orch"),
        row("w1", parent="orch", activity_since=1.0, background=[_job("waiting")]),
    ]
    box, api, clock = make_box(rows)
    out = box.wait_for_session({"title": "w1", "timeout_s": 6}, FakeCtx(clock))
    assert out["timed_out"] is True and out["still_running"] == ["w1"]
    # Only a stuck job left: nothing will wake it, so it is done.
    api.rows[1]["background"] = [_job("stuck")]
    out = box.wait_for_session(
        {"title": "w1", "timeout_s": 60, "settle_s": 1}, FakeCtx(clock)
    )
    assert out["sessions"]["w1"]["reason"] == "idle"


def test_stop_route_refuses_a_pid_that_is_not_a_job(monkeypatch):
    from backend.session.storage import Status as _Status
    from backend.web import server

    class _Inst:
        Status = _Status.Running
        Program = "claude"

    monkeypatch.setattr(server.ENGINE, "instances", {"bg-1": _Inst()})
    calls = []

    def fake_stop(name, pid):
        calls.append((name, pid))
        return pid == 4242

    monkeypatch.setattr(bg, "stop", fake_stop)
    client = TestClient(server.app)
    assert client.post("/api/instances/nope/background/1/stop").status_code == 404
    r = client.post("/api/instances/bg-1/background/7/stop")
    assert r.status_code == 409
    r = client.post("/api/instances/bg-1/background/4242/stop")
    assert r.status_code == 200
    assert r.json() == {"title": "bg-1", "pid": 4242, "stopped": True}
    assert [p for _, p in calls] == [7, 4242]
    assert all(n.endswith("bg-1") for n, _ in calls)

    assert client.post("/api/instances/bg-1/background/abc/stop").status_code == 422
    assert [p for _, p in calls] == [7, 4242]  # never reached stop()


def test_stop_route_is_registered():
    from backend.web import server

    paths = server.app.openapi()["paths"]
    assert "post" in paths["/api/instances/{title}/background/{pid}/stop"]


class _Provider:
    def __init__(self, jobs):
        self.jobs = jobs
        self.asked = []

    def background_jobs(self, name):
        self.asked.append(name)
        if isinstance(self.jobs, Exception):
            raise self.jobs
        return self.jobs


@pytest.mark.parametrize(
    "jobs,expected",
    [
        ([_job("waiting")], [_job("waiting")]),
        ([], None),
        (None, None),
        (RuntimeError("boom"), None),
    ],
)
def test_server_background_jobs_reads_the_provider(monkeypatch, jobs, expected):
    from backend.web import server

    prov = _Provider(jobs)
    monkeypatch.setattr(server.providers, "resolve", lambda program: prov)
    inst = types.SimpleNamespace(Title="bg-1", Program="claude")
    assert server._background_jobs(inst, "bg-1") == expected
    assert prov.asked == [server.tmux.to_mindflock_tmux_name("bg-1")]


class _SnapInst:
    Program = "claude"
    Path = ""
    Branch = ""
    BaseBranch = ""
    InPlace = False

    def __init__(self, title):
        from backend.session.storage import Status as _Status

        self.Title = title
        self.Status = _Status.Running

    def Started(self):  # noqa: N802
        return True

    def GetWorktreePath(self):  # noqa: N802
        return ""


def test_snapshot_rows_carry_background(monkeypatch):
    from backend.web import server

    inst = _SnapInst("bg-snap")
    monkeypatch.setattr(server.ENGINE, "instances", {inst.Title: inst})
    monkeypatch.setattr(server, "_agent_activity", lambda i, t: "idle")
    monkeypatch.setattr(server, "_background_jobs", lambda i, t: [_job("waiting")])
    server._ACTIVITY_CACHE.pop(inst.Title, None)
    mine = [d for d in server._build_instances_snapshot() if d["title"] == inst.Title]
    assert mine and mine[0]["background"] == [_job("waiting")]


def test_the_cheap_snapshot_carries_background_over():
    from backend.web import server

    inst = _SnapInst("bg-cheap")
    assert "background" in server._SNAPSHOT_PROBE_KEYS
    assert server._session_snapshot_cheap(inst, {})["background"] is None
    prev = {"background": [_job("stuck")]}
    assert server._session_snapshot_cheap(inst, {}, prev)["background"] == [
        _job("stuck")
    ]


def test_the_bundle_ships_the_chip_and_the_stop_call():
    """The committed build carries the row chip and its stop route."""
    from pathlib import Path

    js = (Path(__file__).resolve().parents[2] / "backend/web/static/app.js").read_text(
        encoding="utf-8"
    )
    assert "bgchip" in js
    assert '"/background/"' in js and '"/stop"' in js


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
