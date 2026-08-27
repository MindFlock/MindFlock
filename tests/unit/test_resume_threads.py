"""Per-window resume-thread markers: each window resumes ITS OWN conversation.

Several sessions can share one working directory (in-place sessions / window
copies). The CLIs' bulk resume flags (``claude --continue``, ``codex resume
--last``) pick the directory's NEWEST conversation, so after a restart every
sibling resumed the same thread. A per-window thread marker + resume-by-id
launch command fixes that; these tests pin the mechanism.
"""

import json

import pytest

from backend import providers
from backend.providers import thread_markers
from backend.providers.claude import claude_launch_command
from backend.providers.base import LaunchContext


@pytest.fixture(autouse=True)
def _isolated_markers(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_THREAD_MARKER_DIR", str(tmp_path / "threads"))


def test_marker_roundtrip_and_claims():
    thread_markers.record("mindflock_a", "aaaa-1111")
    thread_markers.record("mindflock_b", "bbbb-2222")
    assert thread_markers.read("mindflock_a") == "aaaa-1111"
    # claimed() excludes the asking session, so discovery can't bind a window
    # to a sibling's conversation.
    assert thread_markers.claimed(exclude_session="mindflock_a") == {"bbbb-2222"}
    thread_markers.clear("mindflock_a")
    assert thread_markers.read("mindflock_a") == ""


def test_garbled_marker_never_reaches_a_shell_command():
    # A thread id is spliced into a tmux launch command; anything that isn't a
    # plain uuid-ish token must be dropped on read.
    thread_markers.record("mindflock_bad", "evil; rm -rf /")
    assert thread_markers.read("mindflock_bad") == ""
    assert "evil; rm -rf /" not in thread_markers.claimed()


def test_record_ignores_empty_session_or_id():
    # Nothing is written (and no marker dir created) when either arg is empty.
    thread_markers.record("", "some-id")
    thread_markers.record("mindflock_x", "")
    assert thread_markers.read("mindflock_x") == ""


def test_record_never_raises_when_marker_dir_unwritable(tmp_path, monkeypatch):
    # Point the marker dir at a path whose parent is a FILE, so mkdir() fails —
    # record must swallow it (markers are enrichment only) and read stays "".
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file, not a dir")
    monkeypatch.setenv("MINDFLOCK_THREAD_MARKER_DIR", str(blocker / "markers"))
    thread_markers.record("mindflock_y", "abcd-1234")  # must not raise
    assert thread_markers.read("mindflock_y") == ""


def test_clear_never_raises_when_path_errors(monkeypatch):
    # If resolving the marker path itself blows up, clear() still swallows it.
    def _boom(name):
        raise RuntimeError("path exploded")

    monkeypatch.setattr(thread_markers, "_path", _boom)
    thread_markers.clear("whatever")  # must not raise


def test_claimed_skips_unreadable_marker_file(tmp_path, monkeypatch):
    # A directory named "*.thread" is yielded by the glob but read_text raises —
    # claimed() must skip it and still collect the real siblings' ids.
    mdir = tmp_path / "threads"
    monkeypatch.setenv("MINDFLOCK_THREAD_MARKER_DIR", str(mdir))
    thread_markers.record("mindflock_ok", "good-id-1")
    (mdir / "broken.thread").mkdir()  # read_text() will raise on this entry
    assert thread_markers.claimed() == {"good-id-1"}


def test_claimed_never_raises_when_dir_scan_errors(monkeypatch):
    # If globbing the marker dir blows up, claimed() degrades to an empty set.
    class _Dir:
        def glob(self, pat):
            raise RuntimeError("scan exploded")

    monkeypatch.setattr(thread_markers, "marker_dir", lambda: _Dir())
    assert thread_markers.claimed() == set()


def test_codex_resumes_own_thread_and_falls_back_fresh():
    thread_markers.record("mindflock_w1", "aaaa-1111")
    cx = providers.resolve("codex")
    # With a recorded id: resume THAT session; a vanished id falls back FRESH
    # (never `resume --last`, which would steal a sibling's newest thread).
    assert (
        cx.build_launch_command(
            LaunchContext(program="codex", resume=True, session_name="mindflock_w1")
        )
        == "codex resume aaaa-1111 || codex"
    )
    # Without one: the pre-existing bulk-resume behaviour is unchanged.
    assert (
        cx.build_launch_command(
            LaunchContext(program="codex", resume=True, session_name="mindflock_w2")
        )
        == "codex resume --last || codex"
    )


def test_antigravity_resumes_own_conversation():
    thread_markers.record("mindflock_w1", "cafe-beef")
    ag = providers.resolve("antigravity")
    assert (
        ag.build_launch_command(
            LaunchContext(program="agy", resume=True, session_name="mindflock_w1")
        )
        == "agy --conversation cafe-beef || agy"
    )


def test_claude_resume_targets_thread_id():
    # Claude resumes retry once (transient network failures exit non-zero
    # exactly like "nothing to continue"), then start plain — never re-seeded.
    plain = (
        "{ echo '[mindflock] resume failed twice; starting a fresh session"
        " WITHOUT re-sending the task prompt'; claude; }"
    )
    assert claude_launch_command("claude", resume=True, thread_id="cccc-3333") == (
        "claude --resume cccc-3333 || "
        "{ sleep 3; claude --resume cccc-3333; } || " + plain
    )
    # No recorded thread -> --continue, with the same retry-then-plain chain.
    assert claude_launch_command("claude", resume=True) == (
        "claude --continue || { sleep 3; claude --continue; } || " + plain
    )


def test_claude_hook_persists_session_id(tmp_path, monkeypatch):
    # The activity-hook command must also write the hook payload's session_id
    # as this window's thread marker. Execute the embedded python the same way
    # Claude Code would (JSON payload on stdin, session name via env).
    import shlex
    import subprocess

    from backend.providers.activity_markers import hook_command as _hook_command

    cmd = _hook_command("working", str(tmp_path / "activity"))
    # Extract the `python3 -c <code>` payload from the hook command line.
    parts = shlex.split(cmd.split(" || ")[0])
    assert parts[:2] == ["python3", "-c"]
    payload = json.dumps(
        {"session_id": "dead-beef-0001", "hook_event_name": "PreToolUse"}
    )
    subprocess.run(
        ["python3", "-c", parts[2]],
        input=payload,
        text=True,
        timeout=10,
        check=True,
        env={
            "MINDFLOCK_SESSION_NAME": "mindflock_hooked",
            "PATH": "/usr/bin:/bin",
            "MINDFLOCK_THREAD_MARKER_DIR": str(tmp_path / "threads_own_env"),
        },
    )
    # The hook resolves the thread dir at FIRE time, from ITS OWN environment —
    # never from the env of whoever installed it. Baking the installer's path
    # was a live incident: a sandboxed Verify run (HOME redirected into a
    # scratchpad) re-pinned a SHARED repo's hooks file with its sandbox path,
    # and every cohabiting session's markers silently vanished into /tmp —
    # chips frozen on the last pre-poison reading.
    assert (tmp_path / "threads_own_env" / "mindflock_hooked.thread").read_text() == (
        "dead-beef-0001"
    )
    # And nothing leaked into this process's own (autouse-fixture) dir.
    assert not thread_markers.read("mindflock_hooked")


def test_codex_discovery_binds_by_cwd_and_launch_time(tmp_path, monkeypatch):
    # Build a fake $CODEX_HOME with two rollouts in the same cwd: an old one
    # (before this window's launch) and a fresh one. Discovery must pick the
    # fresh, unclaimed one.
    from backend.providers import codex_usage_api

    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    day = tmp_path / "codex" / "sessions" / "2026" / "07" / "07"
    day.mkdir(parents=True)
    cwd = str(tmp_path / "repo")

    def rollout(name, sid, ts_iso):
        (day / name).write_text(
            json.dumps(
                {
                    "type": "session_meta",
                    "payload": {"id": sid, "cwd": cwd, "timestamp": ts_iso},
                }
            )
            + "\n",
            encoding="utf-8",
        )

    rollout("rollout-old.jsonl", "old-session-id", "2026-07-07T10:00:00.000Z")
    rollout("rollout-new.jsonl", "new-session-id", "2026-07-07T12:00:00.000Z")
    import datetime as dt

    launch = dt.datetime(2026, 7, 7, 11, 0, tzinfo=dt.timezone.utc).timestamp()
    assert codex_usage_api.find_thread_id(cwd, launch) == "new-session-id"
    # A sibling already claimed the fresh one -> nothing to bind (never steal).
    assert codex_usage_api.find_thread_id(cwd, launch, exclude={"new-session-id"}) == ""
    # Unrelated cwd -> nothing.
    assert codex_usage_api.find_thread_id(str(tmp_path / "other"), launch) == ""
