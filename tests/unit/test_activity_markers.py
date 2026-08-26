"""Hermetic tests for :mod:`backend.providers.activity_markers`.

The per-session ``{state, ts}`` activity-marker machinery moved out of the
Claude provider into this provider-agnostic module (Codex and hook-capable
user CLIs install the same hooks into their own config files). These cover
the shared primitives directly — marker read semantics, the hook commands,
the settings-file merge — plus the backwards-compat re-exports ``claude.py``
keeps for its long-standing call-sites and tests.

Every filesystem write is confined to ``tmp_path`` (MINDFLOCK_ACTIVITY_MARKER_DIR
/ MINDFLOCK_THREAD_MARKER_DIR point there); no tmux / network is touched.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import time

import pytest

from backend.providers import activity_markers as am
from backend.providers import thread_markers
from backend.web.core import agent_state


@pytest.fixture
def marker_dir(tmp_path, monkeypatch):
    d = tmp_path / "activity-markers"
    monkeypatch.setenv("MINDFLOCK_ACTIVITY_MARKER_DIR", str(d))
    return d


@pytest.fixture
def thread_dir(tmp_path, monkeypatch):
    d = tmp_path / "thread-markers"
    monkeypatch.setenv("MINDFLOCK_THREAD_MARKER_DIR", str(d))
    return d


def _write_marker(marker_dir, session_name, state, ts):
    marker_dir.mkdir(parents=True, exist_ok=True)
    (marker_dir / (session_name + ".json")).write_text(
        json.dumps({"state": state, "ts": ts})
    )


# --------------------------------------------------------------------------- #
# marker_dir / marker read
# --------------------------------------------------------------------------- #
def test_marker_dir_honors_env_override(marker_dir):
    assert am.marker_dir() == marker_dir


def test_marker_dir_defaults_under_home(monkeypatch, tmp_path):
    monkeypatch.delenv("MINDFLOCK_ACTIVITY_MARKER_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert am.marker_dir() == tmp_path / ".mindflock-assistant" / ".activity-markers"


def test_read_marker_returns_each_known_state(marker_dir):
    for state in ("working", "idle", "clarify"):
        _write_marker(marker_dir, "s1", state, time.time())
        assert am.read_activity_marker("s1") == state


def test_read_marker_ignores_unknown_state_and_stale(marker_dir):
    _write_marker(marker_dir, "s2", "dancing", time.time())
    assert am.read_activity_marker("s2") is None
    _write_marker(marker_dir, "s3", "working", time.time() - 7 * 3600)
    assert am.read_activity_marker("s3") is None
    assert am.read_activity_marker_age("s3") is None


def test_read_marker_missing_or_garbled_is_none(marker_dir):
    assert am.read_activity_marker("nope") is None
    marker_dir.mkdir(parents=True, exist_ok=True)
    (marker_dir / "bad.json").write_text("{not json")
    assert am.read_activity_marker("bad") is None


def test_read_marker_age_reflects_recent_write(marker_dir):
    _write_marker(marker_dir, "aged", "working", time.time() - 5)
    age = am.read_activity_marker_age("aged")
    assert age is not None and 4 <= age <= 15


# --------------------------------------------------------------------------- #
# hook_command / notification_hook_command
# --------------------------------------------------------------------------- #
def test_hook_command_is_shell_safe_and_tagged(marker_dir):
    cmd = am.hook_command("working", str(marker_dir))
    assert am._HOOK_TAG in cmd
    # shlex must round-trip the command: python3 -c <one quoted arg> || true <tag>
    parts = shlex.split(cmd)
    assert parts[0] == "python3" and parts[1] == "-c"
    assert "||" in parts and "true" in parts


def test_notification_hook_command_is_shell_safe_and_tagged(marker_dir):
    cmd = am.notification_hook_command(str(marker_dir))
    assert am._HOOK_TAG in cmd
    parts = shlex.split(cmd)
    assert parts[0] == "python3" and parts[1] == "-c"
    assert "notification_type" in cmd  # inspects the stdin payload


def test_hook_command_records_state_and_thread_id(marker_dir, thread_dir):
    cmd = am.hook_command("working", str(marker_dir), record_thread=True)
    subprocess.run(
        ["sh", "-c", cmd],
        input=json.dumps({"session_id": "abc-123"}).encode(),
        check=True,
        env={**os.environ, "MINDFLOCK_SESSION_NAME": "sess_a"},
    )
    assert am.read_activity_marker("sess_a") == "working"
    assert thread_markers.read("sess_a") == "abc-123"


def test_hook_command_record_thread_false_skips_thread_marker(marker_dir, thread_dir):
    # Codex records its own resume-thread id from its rollout files; its hooks
    # install with record_thread=False so the payload's differently-shaped
    # session_id never clobbers that.
    cmd = am.hook_command("idle", str(marker_dir), record_thread=False)
    subprocess.run(
        ["sh", "-c", cmd],
        input=json.dumps({"session_id": "abc-123"}).encode(),
        check=True,
        env={**os.environ, "MINDFLOCK_SESSION_NAME": "sess_b"},
    )
    assert am.read_activity_marker("sess_b") == "idle"
    assert thread_markers.read("sess_b") == ""


# --------------------------------------------------------------------------- #
# merge_activity_hooks: the shared settings-file merge
# --------------------------------------------------------------------------- #
_EVENTS = (("Stop", "idle"), ("UserPromptSubmit", "working"))


def _tagged(entries):
    return [
        h["command"]
        for e in entries
        for h in e.get("hooks", [])
        if am._HOOK_TAG in h.get("command", "")
    ]


def test_merge_installs_into_absent_file(tmp_path, marker_dir):
    path = tmp_path / "cli" / "hooks.json"  # parent dir created too
    assert am.merge_activity_hooks(path, _EVENTS, "sess") is True
    data = json.loads(path.read_text())
    for event, state in _EVENTS:
        cmds = _tagged(data["hooks"][event])
        assert len(cmds) == 1
        assert json.dumps(state) in cmds[0]


def test_merge_is_idempotent_and_replaces_only_tagged_entries(tmp_path, marker_dir):
    path = tmp_path / "hooks.json"
    user = {
        "custom_key": {"kept": True},
        "hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": "echo user-hook"}]}]
        },
    }
    path.write_text(json.dumps(user))
    am.merge_activity_hooks(path, _EVENTS, "sess_a")
    am.merge_activity_hooks(path, _EVENTS, "sess_b")  # re-install: replace, not stack
    data = json.loads(path.read_text())
    assert data["custom_key"] == {"kept": True}  # user keys untouched
    stop_cmds = [h["command"] for e in data["hooks"]["Stop"] for h in e["hooks"]]
    assert "echo user-hook" in stop_cmds  # user hook survives
    assert len(_tagged(data["hooks"]["Stop"])) == 1  # ours replaced, not duplicated


def test_merge_routes_notification_event_through_payload_inspection(
    tmp_path, marker_dir
):
    path = tmp_path / "hooks.json"
    am.merge_activity_hooks(
        path,
        (("Stop", "idle"), ("Notification", "clarify")),
        "sess",
        notification_event="Notification",
    )
    data = json.loads(path.read_text())
    (notif_cmd,) = _tagged(data["hooks"]["Notification"])
    assert "notification_type" in notif_cmd  # the stdin-inspecting variant
    (stop_cmd,) = _tagged(data["hooks"]["Stop"])
    assert "notification_type" not in stop_cmd


def test_merge_tolerates_corrupt_settings(tmp_path, marker_dir):
    path = tmp_path / "hooks.json"
    path.write_text("{broken json")
    am.merge_activity_hooks(path, _EVENTS, "sess")
    data = json.loads(path.read_text())  # rewritten as valid JSON with our hooks
    assert _tagged(data["hooks"]["Stop"])


# --------------------------------------------------------------------------- #
# A marker belongs to a tmux INCARNATION
# --------------------------------------------------------------------------- #
# The marker file is keyed by tmux session name, nothing ever deletes it
# (`_ensure_agent_session` clears the exit marker and the thread marker, never
# this one), no provider maps a session-START event to a state, and it only
# expires after six hours. So re-opening a window that had finished cleanly used
# to report the DEAD run's "idle" for the seconds the new CLI took to boot —
# announcing a turn that ended before the session existed.
#
# The fix is a provenance check, not an age check: an idle marker is still
# trusted at any age, as long as it was written by the tmux session that is
# running now.
class _MarkerAgeProvider:
    """A CLI whose hook marker was written ``age`` seconds ago."""

    def __init__(self, age):
        self._age = age

    def activity_state_age(self, name):
        return self._age


@pytest.fixture()
def at_now(monkeypatch):
    """Pin the wall clock ``_marker_is_current`` samples for itself."""

    def _set(t):
        monkeypatch.setattr(agent_state.time, "time", lambda: t)

    return _set


def test_marker_from_this_incarnation_is_current(at_now):
    # created at t=1000, marker written 30s ago, now t=1100 -> written at 1070,
    # comfortably after the session started.
    at_now(1100.0)
    assert agent_state._marker_is_current(_MarkerAgeProvider(30.0), "s", 1000.0)


def test_an_hours_old_marker_on_a_long_lived_session_is_still_current(at_now):
    # "Trust at any age" has to survive: a Stop hook from two hours ago on a CLI
    # that has been up all day is genuinely idle.
    at_now(100000.0)
    assert agent_state._marker_is_current(_MarkerAgeProvider(7200.0), "s", 1000.0)


def test_a_marker_predating_the_tmux_session_is_not_current(at_now):
    # Session created at t=5000; the marker was written 100s before now=5050,
    # i.e. at t=4950 — by the run that used to hold this name.
    at_now(5050.0)
    assert not agent_state._marker_is_current(_MarkerAgeProvider(100.0), "s", 5000.0)


def test_a_live_realtime_signal_always_passes(at_now):
    # Claude's `agents --json` path reports age 0.0 — real-time by construction,
    # so it can never predate anything.
    at_now(5000.0)
    assert agent_state._marker_is_current(_MarkerAgeProvider(0.0), "s", 5000.0)


def test_unknowable_inputs_keep_the_old_behaviour(at_now):
    # No creation stamp or no marker age means there is nothing to compare, and
    # the long-standing behaviour is to trust the CLI's own report.
    at_now(1100.0)
    assert agent_state._marker_is_current(_MarkerAgeProvider(None), "s", 1000.0)
    assert agent_state._marker_is_current(_MarkerAgeProvider(30.0), "s", None)


def test_the_clock_is_sampled_at_comparison_time(at_now):
    # The caller's `now` predates the probes between it and this call (a pane
    # capture, a `claude agents --json` shell-out). Subtracting a freshly
    # measured age from a stale `now` places the write earlier than it happened
    # and would reject the FIRST marker of a genuinely fresh session — the one
    # window the guard was written for. So the clock is read here.
    at_now(5004.5)  # a poll that began at 5003 and spent 1.5s in probes
    # Session created at 5000, its first hook fired at 5001 -> age 3.5s.
    assert agent_state._marker_is_current(_MarkerAgeProvider(3.5), "s", 5000.0)


def test_an_unreadable_age_keeps_the_old_behaviour(at_now, monkeypatch):
    # A provider whose age probe THROWS (a stat on a file that vanished between
    # the read and the stat, a provider that never implemented it) is the same
    # situation as one that answers None: no evidence either way, so the CLI's
    # own report stands. Failing the other way would blank the reading of every
    # provider with a rough edge in a helper that exists to police one bug.
    class _Boom:
        def activity_state_age(self, name):
            raise OSError("marker vanished")

    at_now(5050.0)
    assert agent_state._marker_is_current(_Boom(), "s", 5000.0)


def test_a_marker_written_in_the_same_instant_counts_as_current(at_now):
    # The comparison is >=, not >. A hook that fires as part of session startup
    # writes its marker at (or within a rounding error of) `created`, and
    # rejecting that would blank the first real reading of every session that
    # starts fast — the opposite of what the guard is for.
    at_now(5030.0)
    assert agent_state._marker_is_current(_MarkerAgeProvider(30.0), "s", 5000.0)
    # One hundredth of a second the other way is a previous run, and is not.
    assert not agent_state._marker_is_current(_MarkerAgeProvider(30.01), "s", 5000.0)
