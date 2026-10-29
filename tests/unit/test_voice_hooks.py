"""The voice branches of the generated activity hook (``activity_markers.
hook_command``), run for real: ``python3 -c <script>`` under a fake
environment, fed Claude Code's Stop / UserPromptSubmit payloads on stdin.

``MINDFLOCK_SESSION_NAME`` short-circuits the tmux lookup, so no pane and no
tmux server are needed; every directory the hook can write to points into
``tmp_path``. The existing guarantees ride along: the marker still carries
``state`` + ``ts``, the thread marker is still recorded, nothing is printed
unless a one-shot ``.next`` request is armed (and then once), and the script
never fails (``|| true`` aside).
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
# UserPromptSubmit: the instruction, once, from the one-shot `.next`
# --------------------------------------------------------------------------- #
def _arm(dirs, session="mindflock_voice", age=0.0):
    dirs["v"].mkdir(parents=True, exist_ok=True)
    p = dirs["v"] / (session + ".next")
    p.write_text("%d\n" % int(time.time() - age))
    return p


_PROMPT = {"hook_event_name": "UserPromptSubmit", "session_id": "conv-2"}


def test_prompt_submit_prints_nothing_without_a_request(dirs):
    r = _fire(am.hook_command("working"), _PROMPT)
    assert r.returncode == 0 and r.stdout == b""
    assert _marker(dirs)["state"] == "working"
    assert thread_markers.read("mindflock_voice") == "conv-2"


def test_a_young_request_prints_the_instruction_once_and_is_consumed(dirs):
    cmd = am.hook_command("working")  # the UserPromptSubmit entry
    nxt = _arm(dirs)
    r = _fire(cmd, _PROMPT)
    assert r.returncode == 0
    assert r.stdout.decode("utf-8").strip() == am.VOICE_INSTRUCTION
    assert not nxt.exists()
    assert _marker(dirs)["state"] == "working"
    assert thread_markers.read("mindflock_voice") == "conv-2"
    # One-shot: the next prompt (typed, not spoken) gets nothing.
    assert _fire(cmd, _PROMPT).stdout == b""


def test_a_stale_request_prints_nothing_and_is_still_consumed(dirs):
    cmd = am.hook_command("working")
    nxt = _arm(dirs, age=am.VOICE_NEXT_MAX_AGE_S + 5)
    r = _fire(cmd, _PROMPT)
    assert r.returncode == 0 and r.stdout == b""
    assert not nxt.exists()


@pytest.mark.parametrize("body", ["", "garbage\n", "1e400\n"])
def test_an_unreadable_request_never_fails_the_hook(dirs, body):
    dirs["v"].mkdir(parents=True)
    nxt = dirs["v"] / "mindflock_voice.next"
    nxt.write_text(body)
    r = _fire(am.hook_command("working"), _PROMPT)
    assert r.returncode == 0 and r.stdout == b""
    assert not nxt.exists()
    assert _marker(dirs)["state"] == "working"


def test_the_sticky_flag_alone_no_longer_prints(dirs):
    dirs["v"].mkdir(parents=True)
    on = dirs["v"] / "mindflock_voice.on"
    on.write_text("1\n")
    assert _fire(am.hook_command("working"), _PROMPT).stdout == b""
    assert on.exists()  # the conversation marker is never consumed


def test_the_request_is_per_session_and_read_from_the_firing_env(dirs, tmp_path):
    cmd = am.hook_command("working")
    other = _arm(dirs, session="mindflock_other")
    assert _fire(cmd, _PROMPT).stdout == b""  # another session's request
    assert other.exists()
    assert (
        _fire(cmd, _PROMPT, session="mindflock_other").stdout.strip()
        == am.VOICE_INSTRUCTION.encode()
    )
    # The flag dir is resolved when the hook FIRES, from its own environment.
    alt = tmp_path / "alt-voice"
    alt.mkdir()
    (alt / "mindflock_voice.next").write_text("%d\n" % int(time.time()))
    assert (
        _fire(cmd, _PROMPT, MINDFLOCK_VOICE_FLAG_DIR=str(alt)).stdout.strip()
        == am.VOICE_INSTRUCTION.encode()
    )
    assert str(dirs["v"]) not in cmd and str(alt) not in cmd


def test_the_request_is_what_core_voice_arms(dirs):
    from backend.web.core import voice

    cmd = am.hook_command("working")
    assert voice.next_path("mindflock_voice") == dirs["v"] / "mindflock_voice.next"
    voice.arm_next("mindflock_voice")
    assert _fire(cmd, _PROMPT).stdout.strip() == am.VOICE_INSTRUCTION.encode()
    voice.arm_next("mindflock_voice")
    voice.disarm_next("mindflock_voice")
    assert _fire(cmd, _PROMPT).stdout == b""
    voice.disarm_next("mindflock_voice")  # idempotent
    # The sticky flag is still core.voice's, and forget drops both.
    assert voice.set_on("mindflock_voice", True) is True
    assert voice.flag_path("mindflock_voice") == dirs["v"] / "mindflock_voice.on"
    voice.arm_next("mindflock_voice")
    voice.forget("mindflock_voice")
    assert voice.is_on("mindflock_voice") is False
    assert not voice.next_path("mindflock_voice").exists()
    voice.forget("mindflock_voice")  # idempotent


def test_stop_files_the_reply_with_the_sticky_flag_off(dirs):
    r = _fire(
        am.hook_command("idle"),
        {"hook_event_name": "Stop", "last_assistant_message": "Say: Done."},
    )
    assert r.returncode == 0 and r.stdout == b""
    assert not (dirs["v"] / "mindflock_voice.on").exists()
    assert _reply(dirs)["text"] == "Say: Done."


def test_stop_never_prints_even_with_a_request(dirs):
    nxt = _arm(dirs)
    r = _fire(
        am.hook_command("idle"),
        {"hook_event_name": "Stop", "last_assistant_message": "m"},
    )
    assert r.stdout == b""
    assert nxt.exists()  # only a prompt consumes it


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
    nxt = _arm(dirs)
    r = _fire(cmd, {"hook_event_name": "UserPromptSubmit", "session_id": "codex-id"})
    assert r.returncode == 0 and r.stdout == b""
    assert thread_markers.read("mindflock_voice") == ""  # still no thread marker
    assert nxt.exists()  # Codex gets the sentence typed, never this file
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


# --------------------------------------------------------------------------- #
# The standing say-all marker: every prompt, every session
# --------------------------------------------------------------------------- #
def test_the_say_all_marker_prints_for_every_prompt_and_is_never_consumed(dirs):
    from backend.web.core import voice

    cmd = am.hook_command("working")
    assert voice.say_all() is False
    assert voice.set_say_all(True) is True
    assert voice.say_all_path() == dirs["v"] / am.VOICE_SAY_ALL_FILE
    # Printed once per prompt, prompt after prompt, for any session.
    assert _fire(cmd, _PROMPT).stdout.strip() == am.VOICE_INSTRUCTION.encode()
    assert _fire(cmd, _PROMPT).stdout.strip() == am.VOICE_INSTRUCTION.encode()
    assert (
        _fire(cmd, _PROMPT, session="mindflock_other").stdout.strip()
        == am.VOICE_INSTRUCTION.encode()
    )
    assert voice.say_all() is True
    # A one-shot .next beside it is still consumed, and the line prints once.
    nxt = _arm(dirs)
    out = _fire(cmd, _PROMPT).stdout
    assert out.count(am.VOICE_INSTRUCTION.encode()) == 1
    assert not nxt.exists()
    assert voice.set_say_all(False) is False
    assert voice.set_say_all(False) is False  # idempotent
    assert _fire(cmd, _PROMPT).stdout == b""


# --------------------------------------------------------------------------- #
# Flock mode: the sticky <session>.flock request, after the voice line
# --------------------------------------------------------------------------- #
def _flock(dirs, text, session="mindflock_voice"):
    p = dirs["v"] / (session + am.FLOCK_FLAG_SUFFIX)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def test_the_flock_file_prints_its_content_on_every_prompt(dirs):
    cmd = am.hook_command("working")
    p = _flock(dirs, "  Use MindFlock workers for this.\n")
    for _ in range(2):  # sticky: never consumed
        r = _fire(cmd, _PROMPT)
        assert r.returncode == 0
        assert r.stdout.decode("utf-8") == "Use MindFlock workers for this.\n"
    assert p.exists()
    # Per session: another window prints nothing.
    assert _fire(cmd, _PROMPT, session="mindflock_other").stdout == b""


def test_the_flock_request_follows_the_voice_line(dirs):
    cmd = am.hook_command("working")
    _flock(dirs, "Use MindFlock workers for this.")
    _arm(dirs)
    out = _fire(cmd, _PROMPT).stdout.decode("utf-8")
    assert out == am.VOICE_INSTRUCTION + "\nUse MindFlock workers for this.\n"


@pytest.mark.parametrize("body", ["", "  \n\n"])
def test_an_absent_or_blank_flock_file_prints_nothing(dirs, body):
    cmd = am.hook_command("working")
    assert _fire(cmd, _PROMPT).stdout == b""
    _flock(dirs, body)
    r = _fire(cmd, _PROMPT)
    assert r.returncode == 0 and r.stdout == b""


def test_the_flock_file_is_what_core_flock_mode_writes(dirs):
    from backend.web.core import flock_mode

    cmd = am.hook_command("working")
    assert flock_mode.set_on("mindflock_voice", True, "Spawn workers.") is True
    assert _fire(cmd, _PROMPT).stdout == b"Spawn workers.\n"
    assert flock_mode.set_on("mindflock_voice", False) is False
    assert _fire(cmd, _PROMPT).stdout == b""


def test_codex_never_prints_the_flock_request(dirs):
    _flock(dirs, "Use MindFlock workers for this.")
    cmd = am.hook_command("working", record_thread=False)
    assert _fire(cmd, _PROMPT).stdout == b""
