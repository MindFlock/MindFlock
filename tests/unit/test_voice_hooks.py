"""The voice branches of the generated activity hook (``activity_markers.
hook_command``), run for real: ``python3 -c <script>`` under a fake
environment, fed Claude Code's Stop / UserPromptSubmit payloads on stdin.

``MINDFLOCK_SESSION_NAME`` short-circuits the tmux lookup, so no pane and no
tmux server are needed; every directory the hook can write to points into
``tmp_path``. The existing guarantees ride along: the marker still carries
``state`` + ``ts``, the thread marker is still recorded, nothing is printed
unless voice is on, and the script never fails (``|| true`` aside).
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess

import pytest

from backend.providers import activity_markers as am
from backend.providers import thread_markers


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    m = tmp_path / "markers"
    v = tmp_path / "voice"
    t = tmp_path / "threads"
    monkeypatch.setenv("MINDFLOCK_ACTIVITY_MARKER_DIR", str(m))
    monkeypatch.setenv("MINDFLOCK_VOICE_FLAG_DIR", str(v))
    monkeypatch.setenv("MINDFLOCK_THREAD_MARKER_DIR", str(t))
    return {"m": m, "v": v, "t": t}


def _fire(cmd: str, payload: dict, session: str = "mindflock_voice", **env):
    e = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
    e["MINDFLOCK_SESSION_NAME"] = session
    e.update(env)
    # The command is `python3 -c <code> || true <tag>`: run it exactly as the
    # CLI's shell would.
    return subprocess.run(
        ["sh", "-c", cmd],
        input=json.dumps(payload).encode(),
        env=e,
        capture_output=True,
        timeout=30,
    )


def _reply(dirs, session="mindflock_voice"):
    return json.loads((dirs["m"] / (session + ".reply.json")).read_text())


def _marker(dirs, session="mindflock_voice"):
    return json.loads((dirs["m"] / (session + ".json")).read_text())


# --------------------------------------------------------------------------- #
# Stop: the reply file
# --------------------------------------------------------------------------- #
def test_stop_files_the_last_assistant_message_beside_the_marker(dirs):
    cmd = am.hook_command("idle")  # the Stop entry (claude.py maps Stop -> idle)
    msg = "I fixed it.\n\nSay: The redirect loop is fixed. Should I push?"
    r = _fire(
        cmd,
        {
            "hook_event_name": "Stop",
            "session_id": "conv-1",
            "last_assistant_message": msg,
        },
    )
    assert r.returncode == 0 and r.stdout == b"", r.stderr
    rep = _reply(dirs)
    assert rep["text"] == msg
    assert isinstance(rep["ts"], int) and rep["ts"] > 1_700_000_000
    # The marker still carries state + ts, and the thread id was recorded.
    mk = _marker(dirs)
    assert mk["state"] == "idle" and isinstance(mk["ts"], int)
    assert thread_markers.read("mindflock_voice") == "conv-1"
    # No stray temp file is left behind (tmp + os.replace).
    assert sorted(p.name for p in dirs["m"].iterdir()) == [
        "mindflock_voice.json",
        "mindflock_voice.reply.json",
    ]


def test_stop_caps_the_text_and_tolerates_a_missing_field(dirs):
    cmd = am.hook_command("idle")
    _fire(cmd, {"hook_event_name": "Stop", "last_assistant_message": "x" * 20000})
    assert len(_reply(dirs)["text"]) == am.REPLY_TEXT_MAX
    # Codex-style payload: no last_assistant_message. Best effort: an empty
    # text with a fresh stamp, never a crash, and the marker is still written.
    _fire(cmd, {"hook_event_name": "Stop"})
    assert _reply(dirs)["text"] == ""
    assert _marker(dirs)["state"] == "idle"


def test_stop_replaces_the_previous_reply(dirs):
    cmd = am.hook_command("idle")
    _fire(cmd, {"hook_event_name": "Stop", "last_assistant_message": "first"})
    _fire(cmd, {"hook_event_name": "Stop", "last_assistant_message": "second"})
    assert _reply(dirs)["text"] == "second"


def test_other_events_write_no_reply_file(dirs):
    for state, event in (
        ("working", "UserPromptSubmit"),
        ("working", "PreToolUse"),
        ("idle", "SessionEnd"),
    ):
        _fire(
            am.hook_command(state),
            {"hook_event_name": event, "last_assistant_message": "nope"},
        )
    assert not (dirs["m"] / "mindflock_voice.reply.json").exists()
    assert _marker(dirs)["state"] == "idle"


def test_reply_file_resolves_the_marker_dir_in_the_firing_env(dirs, tmp_path):
    other = tmp_path / "elsewhere"
    cmd = am.hook_command("idle", str(dirs["m"]))  # a passed dir is ignored
    _fire(
        cmd,
        {"hook_event_name": "Stop", "last_assistant_message": "hi"},
        MINDFLOCK_ACTIVITY_MARKER_DIR=str(other),
    )
    assert (other / "mindflock_voice.reply.json").exists()
    assert not (dirs["m"] / "mindflock_voice.reply.json").exists()
    assert str(dirs["m"]) not in cmd


def test_the_reply_file_is_what_core_spoken_reads(dirs):
    from backend.web.core import spoken

    spoken._REPLY_CACHE.clear()
    _fire(
        am.hook_command("idle"),
        {
            "hook_event_name": "Stop",
            "last_assistant_message": "Done.\n\nSay: All set, nothing to do.",
        },
    )
    got = spoken.read_reply("mindflock_voice")
    assert got is not None and got[1].endswith("Say: All set, nothing to do.")
    assert spoken.extract_say(got[1]) == "All set, nothing to do."
    assert am.reply_path("mindflock_voice") == dirs["m"] / "mindflock_voice.reply.json"


# --------------------------------------------------------------------------- #
# UserPromptSubmit: the instruction, only with the flag
# --------------------------------------------------------------------------- #
def test_prompt_submit_prints_the_instruction_only_when_the_flag_exists(dirs):
    cmd = am.hook_command("working")  # the UserPromptSubmit entry
    payload = {
        "hook_event_name": "UserPromptSubmit",
        "session_id": "conv-2",
        "prompt": "fix it",
    }
    r = _fire(cmd, payload)
    assert r.returncode == 0 and r.stdout == b""
    assert _marker(dirs)["state"] == "working"
    dirs["v"].mkdir(parents=True)
    (dirs["v"] / "mindflock_voice.on").write_text("1\n")
    r = _fire(cmd, payload)
    assert r.returncode == 0
    assert r.stdout.decode("utf-8").strip() == am.VOICE_INSTRUCTION
    assert _marker(dirs)["state"] == "working"
    assert thread_markers.read("mindflock_voice") == "conv-2"


def test_the_flag_is_per_session_and_read_from_the_firing_env(dirs, tmp_path):
    cmd = am.hook_command("working")
    payload = {"hook_event_name": "UserPromptSubmit"}
    dirs["v"].mkdir(parents=True)
    (dirs["v"] / "mindflock_other.on").write_text("1\n")
    assert _fire(cmd, payload).stdout == b""  # another session's flag
    assert (
        _fire(cmd, payload, session="mindflock_other").stdout.strip()
        == am.VOICE_INSTRUCTION.encode()
    )
    # The flag dir is resolved when the hook FIRES, from its own environment.
    alt = tmp_path / "alt-voice"
    alt.mkdir()
    (alt / "mindflock_voice.on").write_text("1\n")
    assert (
        _fire(cmd, payload, MINDFLOCK_VOICE_FLAG_DIR=str(alt)).stdout.strip()
        == am.VOICE_INSTRUCTION.encode()
    )
    assert str(dirs["v"]) not in cmd and str(alt) not in cmd


def test_the_flag_is_what_core_voice_writes(dirs):
    from backend.web.core import voice

    cmd = am.hook_command("working")
    payload = {"hook_event_name": "UserPromptSubmit"}
    assert voice.is_on("mindflock_voice") is False
    assert voice.set_on("mindflock_voice", True) is True
    assert voice.flag_path("mindflock_voice") == dirs["v"] / "mindflock_voice.on"
    assert _fire(cmd, payload).stdout.strip() == am.VOICE_INSTRUCTION.encode()
    assert voice.set_on("mindflock_voice", False) is False
    assert _fire(cmd, payload).stdout == b""
    voice.forget("mindflock_voice")  # idempotent


def test_stop_never_prints_even_with_the_flag(dirs):
    dirs["v"].mkdir(parents=True)
    (dirs["v"] / "mindflock_voice.on").write_text("1\n")
    r = _fire(
        am.hook_command("idle"),
        {"hook_event_name": "Stop", "last_assistant_message": "m"},
    )
    assert r.stdout == b""


def test_a_hook_without_a_session_still_no_ops(dirs):
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("TMUX", "TMUX_PANE", "MINDFLOCK_SESSION_NAME")
    }
    r = subprocess.run(
        ["sh", "-c", am.hook_command("idle")],
        input=json.dumps(
            {"hook_event_name": "Stop", "last_assistant_message": "m"}
        ).encode(),
        env=env,
        capture_output=True,
        timeout=30,
    )
    assert r.returncode == 0 and r.stdout == b""
    assert not dirs["m"].exists()


# --------------------------------------------------------------------------- #
# The Codex install (record_thread=False): reply file yes, prompt print no
# --------------------------------------------------------------------------- #
def test_codex_install_files_replies_but_never_prints(dirs):
    cmd = am.hook_command("working", record_thread=False)
    assert "print(" not in cmd and ".reply.json" in cmd
    dirs["v"].mkdir(parents=True)
    (dirs["v"] / "mindflock_voice.on").write_text("1\n")
    r = _fire(cmd, {"hook_event_name": "UserPromptSubmit", "session_id": "codex-id"})
    assert r.returncode == 0 and r.stdout == b""
    assert thread_markers.read("mindflock_voice") == ""  # still no thread marker
    _fire(
        am.hook_command("idle", record_thread=False),
        {"hook_event_name": "Stop", "last_assistant_message": "codex said"},
    )
    assert _reply(dirs)["text"] == "codex said"
    # An explicit prompt_context overrides the record_thread default both ways.
    assert "print(" in am.hook_command(
        "working", record_thread=False, prompt_context=True
    )
    assert "print(" not in am.hook_command(
        "working", record_thread=True, prompt_context=False
    )


# --------------------------------------------------------------------------- #
# Shape: the command stays shell-safe, tagged, and the guard keeps its place
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("tool_hook", [None, "pre", "post", "fail"])
def test_voice_lines_keep_the_command_shell_safe_and_tagged(dirs, tool_hook):
    cmd = am.hook_command("working", tool_hook=tool_hook)
    parts = shlex.split(cmd.split(" || true ")[0])
    assert parts[0] == "python3" and parts[1] == "-c"
    compile(parts[2], "<hook>", "exec")  # the embedded script parses
    assert am._HOOK_TAG in cmd
    assert json.dumps(am.VOICE_INSTRUCTION) in parts[2]  # a literal, quoting-safe
    if tool_hook:
        assert am.TOOL_HOOK_TAG in cmd
        assert cmd.index("_mf_tool_hook") < cmd.index("SystemExit")


def test_merge_installs_voice_aware_hooks(tmp_path, dirs):
    path = tmp_path / "hooks-config.json"
    assert am.merge_activity_hooks(
        path, (("Stop", "idle"), ("UserPromptSubmit", "working")), "mindflock_voice"
    )
    data = json.loads(path.read_text())
    for event in ("Stop", "UserPromptSubmit"):
        cmd = data["hooks"][event][0]["hooks"][0]["command"]
        assert ".reply.json" in cmd and "print(" in cmd
    # Re-installing writes nothing (unchanged body) — a hot-reloading CLI
    # never sees a needless rewrite.
    assert not am.merge_activity_hooks(
        path, (("Stop", "idle"), ("UserPromptSubmit", "working")), "mindflock_other"
    )
    # A Codex-style install (no thread record) carries no print.
    codex = tmp_path / "codex-hooks-config.json"
    am.merge_activity_hooks(
        codex, (("UserPromptSubmit", "working"),), "x", record_thread=False
    )
    assert "print(" not in codex.read_text()
