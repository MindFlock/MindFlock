"""Other people's keystrokes (:mod:`backend.web.core.guest_terminal`): the
read-only attach for output and ``send-keys -l`` into the exact pane for
input — shared by guest links' terminals and peers' shared sessions. No
tmux is run: argv is captured."""

from __future__ import annotations

import asyncio

import pytest

from backend.web.core import guest_terminal as gt

T = "=mf_api-fix:"


def _literal(calls):
    """What the ``send-keys -l`` calls type, in order."""
    return "".join(c[-1] for c in calls if "-l" in c)


# --------------------------------------------------------------------------- #
# the attach
# --------------------------------------------------------------------------- #
def test_the_attach_is_read_only_on_the_exact_session(monkeypatch):
    import ptyprocess

    seen = {}

    def spawn(argv, dimensions, env):
        seen.update(argv=argv, dimensions=dimensions, term=env["TERM"])
        return "proc"

    monkeypatch.setattr(ptyprocess.PtyProcess, "spawn", spawn)
    assert gt.spawn_readonly_attach("mf_api-fix", 30, 100) == "proc"
    assert seen == {
        "argv": ["tmux", "attach-session", "-r", "-t", "=mf_api-fix"],
        "dimensions": (30, 100),
        "term": "xterm-256color",
    }


# --------------------------------------------------------------------------- #
# keystrokes
# --------------------------------------------------------------------------- #
def test_text_goes_literally_into_the_exact_pane():
    assert gt.send_keys_argv("mf_api-fix", b"ls -la\r") == [
        ["tmux", "send-keys", "-t", T, "-l", "--", "ls -la\r"]
    ]


def test_newlines_and_control_bytes_stay_literal():
    # Ctrl+B is tmux's default prefix: typed literally it reaches the
    # program, never tmux's key table.
    data = b"\x02c\x03line one\nline two\r\x1b[A\t-x"
    calls = gt.send_keys_argv("mf_api-fix", data)
    assert calls == [["tmux", "send-keys", "-t", T, "-l", "--", data.decode()]]


def test_nul_goes_as_the_ctrl_space_key():
    assert gt.send_keys_argv("mf_api-fix", b"a\x00\x00b") == [
        ["tmux", "send-keys", "-t", T, "-l", "--", "a"],
        ["tmux", "send-keys", "-t", T, "C-Space"],
        ["tmux", "send-keys", "-t", T, "C-Space"],
        ["tmux", "send-keys", "-t", T, "-l", "--", "b"],
    ]
    assert gt.send_keys_argv("mf_api-fix", b"\x00") == [
        ["tmux", "send-keys", "-t", T, "C-Space"]
    ]
    assert gt.send_keys_argv("mf_api-fix", b"") == []


def test_invalid_utf8_is_replaced_not_raised():
    assert gt.send_keys_argv("mf_api-fix", b"a\xffb")[0][-1] == "a�b"


def test_a_long_paste_is_chunked_under_tmuxs_command_cap():
    # tmux refuses one command over ~16 KiB ("command too long"): a long
    # paste is several calls, cut between characters, nothing lost.
    text = ("é" * 5000) + ("x" * 9000) + "\n" + ("日本" * 3000)
    calls = gt.send_keys_argv("mf_api-fix", text.encode())
    assert len(calls) > 2
    assert all(c[:6] == ["tmux", "send-keys", "-t", T, "-l", "--"] for c in calls)
    assert all(len(c[-1].encode()) <= gt.MAX_KEYS_BYTES for c in calls)
    assert _literal(calls) == text


def test_send_keys_runs_the_calls_in_order():
    ran = []
    gt.send_keys("mf_api-fix", b"hi\x00there", run=lambda a: ran.append(a) or "")
    assert ran == gt.send_keys_argv("mf_api-fix", b"hi\x00there")


# --------------------------------------------------------------------------- #
# mouse reports: never typed into the prompt
# --------------------------------------------------------------------------- #
WHEEL_UP = b"\x1b[<64;10;5M"
WHEEL_DOWN = b"\x1b[<65;10;5M"
CLICK = b"\x1b[<0;3;4M\x1b[<0;3;4m"


class Tmux:
    """A fake ``run``: answers the pane's mouse flags, records the rest."""

    def __init__(self, flags="0 0 0"):
        self.flags = flags
        self.calls = []

    def __call__(self, argv):
        if argv[1] == "display-message":
            return self.flags + "\n"
        self.calls.append(argv)
        return ""


@pytest.fixture(autouse=True)
def _lines(monkeypatch):
    monkeypatch.setattr(gt, "_scroll_lines", lambda: 2)


def test_the_wheel_scrolls_copy_mode_when_nobody_has_the_mouse():
    tmux = Tmux("0 0 0")
    gt.send_keys("mf_api-fix", b"ab" + WHEEL_UP + WHEEL_UP + b"cd", run=tmux)
    assert tmux.calls == [
        ["tmux", "send-keys", "-t", T, "-l", "--", "ab"],
        ["tmux", "copy-mode", "-e", "-t", T],
        ["tmux", "send-keys", "-t", T, "-X", "-N", "2", "scroll-up"],
        ["tmux", "send-keys", "-t", T, "-X", "-N", "2", "scroll-up"],
        ["tmux", "send-keys", "-t", T, "-l", "--", "cd"],
    ]


def test_wheel_down_outside_copy_mode_and_clicks_do_nothing():
    tmux = Tmux("0 0 0")
    gt.send_keys("mf_api-fix", WHEEL_DOWN + CLICK, run=tmux)
    assert tmux.calls == []
    tmux = Tmux("0 0 1")  # already scrolled back: down scrolls
    gt.send_keys("mf_api-fix", WHEEL_DOWN, run=tmux)
    assert tmux.calls == [
        ["tmux", "send-keys", "-t", T, "-X", "-N", "2", "scroll-down"]
    ]


def test_a_program_with_the_mouse_gets_the_reports():
    tmux = Tmux("1 1 0")
    gt.send_keys("mf_api-fix", WHEEL_UP + CLICK, run=tmux)
    want = WHEEL_UP.decode() * 2 + CLICK.decode()  # a notch per scroll line
    assert tmux.calls == [["tmux", "send-keys", "-t", T, "-l", "--", want]]
    # … but only as SGR, the encoding it can parse straight from here.
    tmux = Tmux("1 0 0")
    gt.send_keys("mf_api-fix", WHEEL_UP, run=tmux)
    assert tmux.calls == []


def test_mouse_reports_for_a_gone_pane_are_dropped_keys_still_go():
    tmux = Tmux("")
    gt.send_keys("mf_api-fix", WHEEL_UP + b"x" + b"\x1b[M #!", run=tmux)
    assert tmux.calls == [["tmux", "send-keys", "-t", T, "-l", "--", "x"]]


# --------------------------------------------------------------------------- #
# the typist
# --------------------------------------------------------------------------- #
def test_the_typist_delivers_in_order_and_coalesces():
    async def main():
        got = []
        typist = gt.Typist("mf_api-fix", lambda name, data: got.append((name, data)))
        for d in (b"a", b"b", b"", b"c"):
            typist.type(d)
        typist.start()
        typist.close()
        await asyncio.wait_for(typist._task, 3)
        assert got == [("mf_api-fix", b"abc")]

    asyncio.run(main())


def test_a_failing_send_does_not_stop_the_typist():
    async def main():
        got = []

        def send(name, data):
            if data == b"boom":
                raise OSError("tmux gone")
            got.append(data)

        typist = gt.Typist("mf_api-fix", send).start()
        typist.type(b"boom")
        await asyncio.sleep(0.1)
        typist.type(b"ok")
        typist.close()
        await asyncio.wait_for(typist._task, 3)
        assert got == [b"ok"]

    asyncio.run(main())
