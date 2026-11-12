"""Stability sweep: a scheduled slot is spent by ONE process. The run record
(``template_runs.json``, ``flights/schedule_runs.json``) is shared by every
server on one home — on the owner's machine 8765 and 9000 are the same
device, so both are the automation device and both tick. Load → plan → save
under the in-process lock alone let both read the slot as due, and both
fired it (two sessions / two flights for one schedule slot)."""

import json
import os
import subprocess
import sys
import time

import pytest

from backend.web.core import template_schedule as ts

_OTHER = """
import json, sys
from backend.web.core import template_schedule as ts
now, rows, path = json.loads(sys.argv[1])
print("ready", flush=True)
sys.stdin.readline()
fire, _ = ts.claim_slots(now, rows, True, path)
print(json.dumps([t["name"] for t, _slot in fire]), flush=True)
"""


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_TEMPLATE_RUNS_FILE", str(tmp_path / "tpl_runs.json"))
    yield


def test_two_processes_never_both_fire_one_slot(tmp_path, monkeypatch):
    path = str(tmp_path / "schedule_runs.json")
    rows = [{"name": "p_1", "schedule": {"at": "09:00", "days": "daily"}}]
    now = time.time()
    # Armed long ago, today's slot not yet spent: it is due now.
    sig = ts.signature(ts.schedule_or_none(rows[0]["schedule"]))
    ts.save_runs({"p_1": {"sig": sig, "armed_at": now - 3 * 86400}}, path)
    assert ts.plan_tick(now, rows, ts.load_runs(path), True)[0], "slot is due"
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    other = subprocess.Popen(
        [sys.executable, "-c", _OTHER, json.dumps([now, rows, path])],
        cwd=root,
        env=dict(os.environ, PYTHONPATH=root),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert other.stdout.readline().strip() == "ready"
        real = ts._save_runs

        def save_after_the_other_ticks(runs, p=None):
            # This process has read the record and planned; the other ticks
            # now (or waits for the lock), then this save lands.
            other.stdin.write("go\n")
            other.stdin.flush()
            time.sleep(1.0)
            real(runs, p)

        monkeypatch.setattr(ts, "_save_runs", save_after_the_other_ticks)
        mine, _ = ts.claim_slots(now, rows, True, path)
        monkeypatch.setattr(ts, "_save_runs", real)
        theirs = json.loads(other.stdout.readline())
        other.wait(timeout=30)
    finally:
        if other.poll() is None:
            other.kill()
    fired = [t["name"] for t, _slot in mine] + theirs
    assert fired == ["p_1"], "the slot fired %d times" % len(fired)
