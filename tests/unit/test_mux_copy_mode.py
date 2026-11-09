"""``mux.leave_copy_mode``: a ``send-keys`` that types into a pane first
leaves copy-mode (a voice scroll leaves the pane there; keys sent then are
copy-mode's commands, not the app's input) — in the sessions
``mux.guard_typing`` named; every other argv reaches tmux unchanged. Pure
argv cases, then the real thing on a private tmux server (``-L``, killed by
name)."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid

import pytest

from backend.session import mux

GUARD = "#{==:#{pane_mode},copy-mode}"


def _guard(target):
    return [
        "if-shell",
        "-F",
        "-t",
        target,
        GUARD,
        "send-keys -X -t '%s' cancel" % target,
        ";",
    ]


def test_literal_text_is_guarded():
    argv = ["tmux", "send-keys", "-t", "=s:", "-l", "--", "hello"]
    assert mux.leave_copy_mode(argv) == ["tmux", *_guard("=s:"), *argv[1:]]


def test_named_keys_and_hex_are_guarded():
    got = mux.leave_copy_mode(["tmux", "send-keys", "-t", "s", "Escape"])
    assert got == ["tmux", *_guard("s"), "send-keys", "-t", "s", "Escape"]
    got = mux.leave_copy_mode(["tmux", "send-keys", "-t", "s", "-H", "3b"])
    assert got[1:8] == _guard("s")


def test_every_typing_command_in_a_chain_is_guarded():
    argv = [
        "tmux",
        "send-keys",
        "-t",
        "s",
        "BSpace",
        ";",
        "send-keys",
        "-t",
        "s",
        "-l",
        "--",
        "x",
    ]
    got = mux.leave_copy_mode(argv)
    assert got == [
        "tmux",
        *_guard("s"),
        "send-keys",
        "-t",
        "s",
        "BSpace",
        ";",
        *_guard("s"),
        "send-keys",
        "-t",
        "s",
        "-l",
        "--",
        "x",
    ]


def test_global_options_stay_in_front():
    argv = ["tmux", "-L", "sock", "-f", "/dev/null", "send-keys", "-t", "s", "Enter"]
    got = mux.leave_copy_mode(argv)
    assert got[:5] == ["tmux", "-L", "sock", "-f", "/dev/null"]
    assert got[5:12] == _guard("s")


def test_glued_target():
    got = mux.leave_copy_mode(["tmux", "send-keys", "-t=s", "Enter"])
    assert got[1:8] == _guard("=s")


def test_no_target_guards_the_current_pane():
    got = mux.leave_copy_mode(["tmux", "send-keys", "Enter"])
    assert got == [
        "tmux",
        "if-shell",
        "-F",
        GUARD,
        "send-keys -X cancel",
        ";",
        "send-keys",
        "Enter",
    ]


@pytest.mark.parametrize(
    "argv",
    [
        ["tmux", "send-keys", "-X", "-t", "s", "cancel"],
        ["tmux", "send-keys", "-t", "s", "-X", "-N", "3", "scroll-up"],
        ["tmux", "send-keys", "-M"],
        ["tmux", "send-keys", "-t", "s"],  # nothing typed
        ["tmux", "has-session", "-t=s"],
        ["tmux", "copy-mode", "-e", "-t", "s"],
        ["tmux", "display-message", "-p", "-t", "s", "#{pane_in_mode}"],
        # a binding's argv is not a send-keys COMMAND
        ["tmux", "bind-key", "-T", "root", "WheelUpPane", "send-keys", "-M"],
        ["tmux", "send-keys", "-t", "it's", "x"],  # unquotable: left alone
    ],
)
def test_other_commands_pass_unchanged(argv):
    assert mux.leave_copy_mode(argv) == argv


@pytest.fixture(autouse=True)
def _fresh_guards(monkeypatch):
    monkeypatch.setattr(mux, "_GUARDED", mux.OrderedDict())


def test_only_named_sessions_are_guarded():
    argv = ["tmux", "send-keys", "-t", "=a:", "x", ";", "send-keys", "-t", "b", "y"]
    got = mux.leave_copy_mode(argv, frozenset({"b"}))
    assert got == [
        "tmux",
        "send-keys",
        "-t",
        "=a:",
        "x",
        ";",
        *_guard("b"),
        "send-keys",
        "-t",
        "b",
        "y",
    ]
    assert mux.leave_copy_mode(["tmux", "send-keys", "Enter"], frozenset({"b"})) == [
        "tmux",
        "send-keys",
        "Enter",
    ]


def test_run_guards_a_session_once_mindflock_scrolled_it(monkeypatch):
    seen = []
    monkeypatch.setattr(mux.subprocess, "run", lambda argv, **kw: seen.append(argv))
    mux.run(["tmux", "send-keys", "-t", "s", "-l", "--", "hi"])
    assert seen[-1] == ["tmux", "send-keys", "-t", "s", "-l", "--", "hi"]  # untouched
    mux.guard_typing("=s:")
    mux.run(["tmux", "send-keys", "-t", "s", "-l", "--", "hi"])
    assert seen[-1][1:8] == _guard("s")
    mux.run(["tmux", "send-keys", "-t", "=s:0.1", "Enter"])
    assert seen[-1][1:8] == _guard("=s:0.1")
    mux.run(["tmux", "send-keys", "-t", "other", "Enter"])
    assert seen[-1] == ["tmux", "send-keys", "-t", "other", "Enter"]


def test_the_guard_list_is_bounded(monkeypatch):
    monkeypatch.setattr(mux, "_GUARDED_MAX", 3)
    for n in "abcd":
        mux.guard_typing(n)
    assert list(mux._GUARDED) == ["b", "c", "d"]


# --- the real thing ---------------------------------------------------------------------

needs_tmux = pytest.mark.skipif(
    shutil.which("tmux") is None or os.name == "nt", reason="needs tmux"
)


@pytest.fixture
def pane():
    sock = "mf-mux-copy-%s" % uuid.uuid4().hex[:8]
    base = ["tmux", "-L", sock]

    def tmux(*args):
        return subprocess.run(
            base + list(args), capture_output=True, text=True, timeout=10
        )

    assert (
        tmux("-f", "/dev/null", "new-session", "-d", "-s", "m", "cat -v").returncode
        == 0
    )
    mux.guard_typing("m")
    try:
        for i in range(60):
            tmux("send-keys", "-t", "=m:", "-l", "row %d" % i)
            tmux("send-keys", "-t", "=m:", "Enter")
        yield base, tmux
    finally:
        subprocess.run(base + ["kill-server"], capture_output=True, timeout=10)


def _in_mode(tmux):
    return (
        tmux("display-message", "-p", "-t", "=m:", "#{pane_in_mode}").stdout.strip()
        == "1"
    )


def _screen_has(tmux, text):
    deadline = time.time() + 5
    while time.time() < deadline:
        if text in tmux("capture-pane", "-p", "-t", "=m:").stdout:
            return True
        time.sleep(0.05)
    return False


@needs_tmux
def test_real_dictation_after_a_wheel_scroll_lands_in_the_app(pane):
    base, tmux = pane
    tmux("copy-mode", "-e", "-t", "=m:")
    tmux("send-keys", "-X", "-t", "=m:", "page-up")
    assert _in_mode(tmux)
    # the voice dictation batch shape (agent_io.dictation_argv): erase + text
    argv = [
        *base,
        "send-keys",
        "-t",
        "=m:",
        "BSpace",
        ";",
        "send-keys",
        "-t",
        "=m:",
        "-l",
        "--",
        "q fix it",
    ]
    assert mux.run(argv, capture_output=True, timeout=10).returncode == 0
    assert not _in_mode(tmux)
    mux.run([*base, "send-keys", "-t", "=m:", "Enter"], timeout=10)
    assert _screen_has(tmux, "q fix it")


@needs_tmux
def test_real_escape_after_a_scroll_reaches_the_app(pane):
    """An interrupt said while the pane is scrolled: the Escape goes to the
    agent (cat -v shows ^[), not to copy-mode's cancel."""
    base, tmux = pane
    tmux("copy-mode", "-e", "-t", "=m:")
    tmux("send-keys", "-X", "-t", "=m:", "page-up")
    mux.run([*base, "send-keys", "-t", "=m:", "Escape"], timeout=10)
    mux.run([*base, "send-keys", "-t", "=m:", "Enter"], timeout=10)
    assert not _in_mode(tmux)
    assert _screen_has(tmux, "^[")


@needs_tmux
def test_real_typing_with_no_copy_mode_is_unchanged(pane):
    base, tmux = pane
    mux.run([*base, "send-keys", "-t", "=m:", "-l", "--", "plain"], timeout=10)
    mux.run([*base, "send-keys", "-t", "=m:", "Enter"], timeout=10)
    assert _screen_has(tmux, "plain")


@needs_tmux
def test_real_unguarded_session_keeps_tmuxs_own_behaviour(pane, monkeypatch):
    """Not scrolled by MindFlock: keys in copy-mode are copy-mode's, as ever."""
    base, tmux = pane
    monkeypatch.setattr(mux, "_GUARDED", mux.OrderedDict())
    tmux("copy-mode", "-t", "=m:")
    mux.run([*base, "send-keys", "-t", "=m:", "-l", "--", "j"], timeout=10)
    assert _in_mode(tmux)


@needs_tmux
def test_real_missing_target_still_fails(pane):
    base, _ = pane
    got = mux.run(
        [*base, "send-keys", "-t", "=nope:", "-l", "--", "x"],
        capture_output=True,
        timeout=10,
    )
    assert got.returncode != 0
