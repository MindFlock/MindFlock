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
