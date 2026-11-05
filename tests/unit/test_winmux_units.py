"""The session host's pure parts: send-keys names, the format language,
argv / chain parsing and the screen model (no daemon, no child)."""

from __future__ import annotations

import pytest

pytest.importorskip("pyte")

from backend.session.winmux import formats, keys, proto  # noqa: E402
from backend.session.winmux.screen import Term  # noqa: E402

# -- keys ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,want",
    [
        ("Enter", b"\r"),
        ("Escape", b"\x1b"),
        ("C-c", b"\x03"),
        ("C-u", b"\x15"),
        ("C-Space", b"\x00"),
        ("BSpace", b"\x7f"),
        ("Tab", b"\t"),
        ("BTab", b"\x1b[Z"),
        ("Up", b"\x1b[A"),
        ("Left", b"\x1b[D"),
        ("Home", b"\x1b[1~"),
        ("End", b"\x1b[4~"),
        ("PageUp", b"\x1b[5~"),
        ("PgDn", b"\x1b[6~"),
        ("NPage", b"\x1b[6~"),
        ("PPage", b"\x1b[5~"),
        ("IC", b"\x1b[2~"),
        ("DC", b"\x1b[3~"),
        ("F1", b"\x1bOP"),
        ("F5", b"\x1b[15~"),
        ("F12", b"\x1b[24~"),
        ("Space", b" "),
        ("M-x", b"\x1bx"),
        ("M-Enter", b"\x1b\r"),
        ("C-Up", b"\x1b[1;5A"),
        ("S-F5", b"\x1b[15;2~"),
        ("^C", b"\x03"),
        ("y", b"y"),
    ],
)
def test_key_names(name, want):
    assert keys.key_bytes(name) == want


def test_app_cursor_mode_arrows():
    assert keys.key_bytes("Up", app_cursor=True) == b"\x1bOA"
    assert keys.key_bytes("C-Up", app_cursor=True) == b"\x1b[1;5A"


def test_unknown_key_is_none_so_it_is_sent_literally():
    assert keys.key_bytes("hello") is None
    assert keys.key_bytes("") is None


# -- formats -----------------------------------------------------------------------


def _look(d):
    return lambda k: d.get(k)


def test_format_variables_and_conditionals():
    v = _look({"a": "1", "b": "0", "name": "sess", "n": "42"})
    assert formats.expand("#{name}:#{n}", v) == "sess:42"
    assert formats.expand("#{missing}", v) == ""
    assert formats.expand("#{?a,yes,no}", v) == "yes"
    assert formats.expand("#{?b,yes,no}", v) == "no"
    assert formats.expand("#{||:#{a},#{b}}", v) == "1"
    assert formats.expand("#{||:#{b},#{b}}", v) == "0"
    assert formats.expand("#{&&:#{a},#{b}}", v) == "0"
    assert formats.expand("#{==:#{name},sess}", v) == "1"
    assert formats.expand("#{!=:#{name},sess}", v) == "0"
    assert formats.expand("#{?#{==:#{n},42},match,nope}", v) == "match"
    assert formats.expand("#{=2:name}", v) == "se"
    assert formats.expand("#{=-2:name}", v) == "ss"
    assert formats.expand("#{n:name}", v) == "4"
    assert formats.expand("#{s/e/E/:name}", v) == "sEss"
    assert formats.expand("## #, #S", _look({"session_name": "x"})) == "# , x"
    assert formats.expand("#{l:#{name}}", v) == "#{name}"
    assert formats.expand("#{<:#{n},100}", v) == "1"


def test_format_time_is_ctime():
    assert formats.ctime(0).endswith(("1970", "1969"))


# -- argv -------------------------------------------------------------------------


def test_split_chain_semicolons_like_tmux():
    assert proto.split_chain(["a", "-l", "x;", "b"]) == [["a", "-l", "x"], ["b"]]
    assert proto.split_chain(["a", ";", "b", "c"]) == [["a"], ["b", "c"]]
    assert proto.split_chain(["send-keys", "-l", "x\\;"]) == [["send-keys", "-l", "x;"]]
    assert proto.split_chain(["a", ";", ";", "b"]) == [["a"], ["b"]]


def test_top_flags_and_namespaces():
    flags, rest = proto.parse_top(["-S", "/tmp/sock", "ls"])
    assert rest == ["ls"] and proto.namespace_of(flags) == "/tmp/sock"
    flags, _ = proto.parse_top(["-S", "winmux", "ls"])
    assert proto.namespace_of(flags) == "default"
    flags, _ = proto.parse_top(["-Lother", "ls"])
    assert proto.namespace_of(flags) == "other"
    flags, rest = proto.parse_top(["-V"])
    assert "V" in flags and rest == []


def test_args_getopt():
    a = proto.Args(["-dP", "-sname", "-c", "/x", "--", "-cmd"], "c:dPs:")
    assert a.has("d") and a.has("P") and a.get("s") == "name" and a.get("c") == "/x"
    assert a.pos == ["-cmd"]
    a = proto.Args(["-t=name", "key", "-l"], "lt:")
    assert a.get("t") == "=name" and a.pos == ["key", "-l"] and not a.has("l")
    with pytest.raises(proto.CmdError):
        proto.Args(["-Z"], "t:")
    with pytest.raises(proto.CmdError):
        proto.Args(["-t"], "t:")
    assert proto.canonical("ls") == "list-sessions"
    assert proto.canonical("display") == "display-message"
    assert proto.canonical("capture-p") == "capture-pane"
    assert proto.canonical("nope") is None


# -- screen -------------------------------------------------------------------------


def test_capture_trims_and_pads_like_tmux():
    t = Term(10, 3)
    t.feed("ab  \r\ncd")
    assert t.capture() == "ab\ncd\n\n"


def test_wrapped_line_joins_with_J():
    t = Term(5, 4)
    t.feed("abcdefgh\r\nxy")
    assert t.capture() == "abcde\nfgh\nxy\n\n"
    assert t.capture(join=True) == "abcdefgh\nxy\n\n"


def test_explicit_newline_clears_wrap_flag():
    t = Term(5, 4)
    t.feed("abcdefgh\x1b[1;1H\x1b[2B\r\nxy")  # a later explicit LF on other rows
    assert t.capture(join=True).startswith("abcdefgh\n")


def test_history_and_ranges():
    t = Term(20, 5, history=100)
    t.feed("\r\n".join("line%d" % i for i in range(1, 21)))
    assert t.history_size == 15
    assert t.capture().split("\n")[:5] == [
        "line16",
        "line17",
        "line18",
        "line19",
        "line20",
    ]
    full = t.capture(start="-")
    assert full.startswith("line1\nline2\n") and full.count("\n") == 20
    assert t.capture(start="-3", end="-1") == "line13\nline14\nline15\n"
    t.set_history_limit(4)
    assert t.history_size == 4


def test_history_limit_zero_keeps_nothing():
    t = Term(20, 2, history=0)
    t.feed("a\r\nb\r\nc")
    assert t.history_size == 0


def test_colours_with_e():
    t = Term(20, 2)
    t.feed("\x1b[31mred\x1b[0m plain \x1b[1;38;5;196mx\x1b[38;2;1;2;3my")
    out = t.capture(codes=True).split("\n")[0]
    assert out.startswith("\x1b[31mred\x1b[0m plain ")
    assert "\x1b[1;38;5;196mx" in out
    assert "38;2;1;2;3my" in out
    assert out.endswith("\x1b[0m")


def test_colon_sgr_and_private_sequences_do_not_corrupt_the_screen():
    t = Term(20, 2)
    client, _ = t.feed("\x1b[>4;1m\x1b[>1u\x1b[4:3mu\x1b[24m\x1b[38:2::9:9:9mz")
    line = t.screen.buffer[0]
    assert line[0].data == "u" and line[0].underscore and not line[0].bold
    assert line[1].data == "z" and line[1].fg == "090909"
    assert "\x1b[>" not in client  # kitty / modifyOtherKeys: never shown to clients


def test_queries_are_answered_and_kept_from_clients():
    t = Term(20, 5)
    client, reply = t.feed("ab\x1b[c\x1b[>c\x1b[6n\x1b[5n\x1b[>q\x1b]11;?\x07cd")
    assert reply == (
        "\x1b[?1;2c"
        + "\x1b[>84;0;0c"
        + "\x1b[1;3R"
        + "\x1b[0n"
        + "\x1bP>|tmux 3.4\x1b\\"
    )
    assert client == "abcd"


def test_split_sequences_across_reads():
    t = Term(20, 2)
    c1, _ = t.feed("x\x1b[3")
    c2, _ = t.feed("1my\x1b]0;tit")
    c3, _ = t.feed("le\x07z")
    assert (c1, c2, c3) == ("x", "\x1b[31my", "\x1b]0;title\x07z")
    assert t.title == "title"
    assert t.screen.buffer[0][1].fg == "red"


def test_dcs_body_is_not_drawn():
    t = Term(20, 2)
    client, _ = t.feed("a\x1bP+q544e\x1b\\b\x1bPq#0;2;0;0;0~\x1b\\c")
    assert t.capture().split("\n")[0] == "abc"
    assert "+q" not in client and "#0;2" in client


def test_alternate_screen_keeps_main_and_history():
    t = Term(10, 3)
    t.feed("main1\r\nmain2")
    t.feed("\x1b[?1049h")
    assert t.alternate_on
    t.feed("\x1b[Halt")
    assert t.capture().split("\n")[0] == "alt"
    assert t.capture(alternate=True).startswith("main1\nmain2\n")
    t.feed("\x1b[?1049l")
    assert not t.alternate_on
    assert t.capture().startswith("main1\nmain2")
    assert t.history_size == 0
    with pytest.raises(ValueError):
        t.capture(alternate=True)


def test_scroll_on_clear_and_clear_history():
    t = Term(10, 3)
    t.feed("one\r\ntwo\x1b[2J")
    assert t.history_size == 2 and t.capture() == "\n\n\n"
    t.feed("\x1b[3J")
    assert t.history_size == 0


def test_resize_pushes_to_and_pulls_from_history():
    t = Term(10, 4, history=50)
    t.feed("a\r\nb\r\nc\r\nd")
    t.resize(2, 10)
    assert t.history_size == 2 and t.capture() == "c\nd\n"
    t.resize(4, 10)
    assert t.history_size == 0 and t.capture() == "a\nb\nc\nd\n"


def test_mouse_and_paste_modes_tracked():
    t = Term(10, 2)
    t.feed("\x1b[?1000;1006h\x1b[?2004h")
    assert t.mouse_any and t.mode(1006) and t.bracketed_paste
    t.feed("\x1b[?1000l")
    assert not t.mouse_any


def test_repaint_reproduces_the_screen():
    t = Term(12, 3)
    t.feed("\x1b[32mgreen\x1b[0m\r\nx\x1b[?2004h\x1b[?1h")
    paint = t.repaint()
    other = Term(12, 3)
    other.feed(paint)
    assert other.capture(codes=True) == t.capture(codes=True)
    assert (other.screen.cursor.x, other.screen.cursor.y) == (1, 1)
    assert other.bracketed_paste and other.app_cursor
