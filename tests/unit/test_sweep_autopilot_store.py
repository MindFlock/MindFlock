"""Stability sweep: ``autopilot.json`` is shared by every process on one
config dir — both servers (8765 and 9000 on the owner's machine) and the
ticket pipeline, which arms from its own OS process. A read-modify-write
under the in-process lock alone let one process's whole-file rewrite (the
lease refresh, every pass, for every armed session) drop what another had
just written: an arm vanished, and "take this to a PR" did nothing."""

import os
import subprocess
import sys
import time

import pytest

from backend.web.core import autopilot as ap

_OTHER = """
import os, sys
from backend.web.core import autopilot as ap
print("ready", flush=True)
sys.stdin.readline()
ap.arm("other", "push", source="intake")
print("armed", flush=True)
"""


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_AUTOPILOT_FILE", str(tmp_path / "ap.json"))
    yield


def test_another_process_write_is_not_lost_under_a_lease_refresh(monkeypatch):
    ap.arm("mine", "pr")
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    other = subprocess.Popen(
        [sys.executable, "-c", _OTHER],
        cwd=root,
        env=dict(os.environ, PYTHONPATH=root),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert other.stdout.readline().strip() == "ready"
        real = ap._save

        def save_after_the_other_writes(data):
            # This process has READ the store; the other process writes now
            # (or waits for the lock), then this write lands.
            other.stdin.write("go\n")
            other.stdin.flush()
            time.sleep(1.0)
            real(data)

        monkeypatch.setattr(ap, "_save", save_after_the_other_writes)
        rec, _took = ap.claim("mine", "boot-1")
        assert rec is not None
        monkeypatch.setattr(ap, "_save", real)
        assert other.stdout.readline().strip() == "armed"
        other.wait(timeout=30)
    finally:
        if other.poll() is None:
            other.kill()
    assert ap.get("mine")["owner"] == "boot-1"
    assert ap.get("other") is not None, "the other process's arm was overwritten"
