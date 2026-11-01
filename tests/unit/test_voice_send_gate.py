"""Voice dictation's erase/replace form on ``/send`` — the server's gate.

Voice cycle 6. The desktop types live dictation through ``POST
/api/instances/{t}/send {"voice": true, "submit": false, "erase": n, "text":
…}`` instead of raw keys down the terminal socket, so every keystroke —
a correction's Backspaces too — passes the gate that reads the SCREEN at send
time (the rows the client sees lag the 4 s state tick by 2.1–2.4 s; a Codex
"y" would approve). One batch = BSpace ×n, then the text, in ONE tmux call.
"""

from __future__ import annotations

import subprocess

import pytest

from backend.web import server
from backend.web.core import agent_io
from backend.web.core import proc as _proc
from backend.web.core import prompt_queue as pq
from backend.web.core import voice_actions
from tests.unit.test_prompt_queue import client, qfile  # noqa: F401
from tests.unit.test_typing_guard import DIALOG, PLAIN, screens  # noqa: F401


@pytest.fixture
def tmux_calls(monkeypatch):
    """Every tmux argv the dictation path runs (none really runs); the
    agent session ``agent_t1`` is live."""
    calls: list = []

    def run(argv, **_kw):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(_proc, "run_capped", run)
    monkeypatch.setattr(server, "_live_session_name", lambda n: "agent_t1")
    voice_actions._reset_for_tests()
    yield calls
    voice_actions._reset_for_tests()


def _send(client, **body):
    return client.post(
        "/api/instances/t1/send", json={"voice": True, "submit": False, **body}
    )


# --------------------------------------------------------------------------- #
# agent_io.dictation_argv — one tmux call, Backspaces first                    #
# --------------------------------------------------------------------------- #
def test_argv_erase_then_type_is_one_tmux_invocation():
    assert agent_io.dictation_argv("s", 3, "bag ") == [
        "tmux",
        "send-keys", "-t", "s", "BSpace", "BSpace", "BSpace",
        ";",
        "send-keys", "-t", "s", "-l", "--", "bag ",
    ]  # fmt: skip


def test_argv_erase_only_and_type_only():
    assert agent_io.dictation_argv("s", 2, "") == [
        "tmux",
        "send-keys",
        "-t",
        "s",
        "BSpace",
        "BSpace",
    ]
    assert agent_io.dictation_argv("s", 0, "-x hi") == [
        "tmux",
        "send-keys",
        "-t",
        "s",
        "-l",
        "--",
        "-x hi",
    ]


@pytest.mark.parametrize(
    "text,body,semis",
    [("fix it;", "fix it", 1), (";", "", 1), ("a;;", "a", 2), ("a;b ", "a;b ", 0)],
)
def test_argv_trailing_semicolons_go_as_hex_keys(text, body, semis):
    """tmux reads an argument ending in ';' as a command separator (measured
    on 3.4: ``send-keys -l -- 'XY;'`` types "XY")."""
    argv = agent_io.dictation_argv("s", 0, text)
    assert not any(a.endswith(";") and a != ";" for a in argv)
    literal = argv[argv.index("--") + 1] if "--" in argv else ""
    assert literal == body
    assert argv.count("3b") == semis


@pytest.mark.parametrize(
    "text,erase,ok",
    [
        ("", 1, True),
        ("ag", 1, True),
        (" ", 0, True),
        ("", 0, False),  # nothing to do
        ("a\rb", 0, False),  # a CR would submit
        ("a\nb", 0, False),
        ("\x1b", 0, False),  # Escape interrupts Claude's turn
        ("\x7f", 0, False),  # a literal DEL is not the erase form
        ("x" * (agent_io.MAX_DICTATION_TEXT + 1), 0, False),
        ("", -1, False),
        ("", agent_io.MAX_DICTATION_ERASE + 1, False),
        ("", True, False),  # a bool is not a count
        ("", "3", False),
        ("", 2.0, False),
    ],
)
def test_parse_dictation(text, erase, ok):
    n, err = agent_io.parse_dictation(text, erase)
    assert (err is None) is ok, err
    if ok:
        assert n == erase


# --------------------------------------------------------------------------- #
# /send erase form — the gate table                                            #
# --------------------------------------------------------------------------- #
GATED = [
    ("clarify", PLAIN),  # the reading says dialog
    ("limit", PLAIN),  # the usage-limit menu
    ("working", DIALOG),  # a sub-agent's prompt under a working row (J1)
    ("idle", DIALOG),  # the dialog is up, the 4 s tick has not seen it
    ("unknown", PLAIN),  # an unreadable activity: don't type blind
]
BATCHES = [
    {"erase": 3, "text": ""},  # a take-back alone
    {"erase": 1, "text": "ag "},  # a correction
    {"erase": 0, "text": "yes "},  # plain typing
]


@pytest.mark.parametrize("activity,screen", GATED)
@pytest.mark.parametrize("batch", BATCHES)
def test_into_a_dialog_is_409_with_nothing_pressed(
    client, screens, tmux_calls, monkeypatch, activity, screen, batch
):
    monkeypatch.setattr(server, "_agent_activity", lambda i, t: activity)
    screens["t1"] = screen
    r = _send(client, **batch)
    assert r.status_code == 409, r.text
    assert r.json()["in_dialog"] is True
    assert tmux_calls == []
    assert client._sent == []
    assert pq.list_queue("t1") == []
    assert voice_actions.answered_at("t1") == 0.0


def test_a_probe_that_raises_reads_as_unknown(client, screens, tmux_calls, monkeypatch):
    def boom(i, t):
        raise RuntimeError("tmux hiccup")

    monkeypatch.setattr(server, "_agent_activity", boom)
    r = _send(client, erase=2, text="")
    assert r.status_code == 409 and r.json()["in_dialog"] is True
    assert tmux_calls == []


@pytest.mark.parametrize("activity", ["working", "idle"])
def test_no_dialog_is_one_tmux_call(client, screens, tmux_calls, monkeypatch, activity):
    monkeypatch.setattr(server, "_agent_activity", lambda i, t: activity)
    r = _send(client, erase=4, text="bag ")
    assert r.status_code == 200, r.text
    assert r.json() == {"sent": True, "submitted": False, "erased": 4}
    assert tmux_calls == [agent_io.dictation_argv("agent_t1", 4, "bag ")]
    assert tmux_calls[0].count("BSpace") == 4
    assert client._sent == []  # not through the paste/submit primitive


def test_erase_only(client, screens, tmux_calls, monkeypatch):
    monkeypatch.setattr(server, "_agent_activity", lambda i, t: "idle")
    r = _send(client, erase=5)
    assert r.json() == {"sent": True, "submitted": False, "erased": 5}
    assert tmux_calls == [["tmux", "send-keys", "-t", "agent_t1"] + ["BSpace"] * 5]


def test_erase_zero_types(client, screens, tmux_calls, monkeypatch):
    monkeypatch.setattr(server, "_agent_activity", lambda i, t: "working")
    r = _send(client, erase=0, text=" ")
    assert r.json() == {"sent": True, "submitted": False, "erased": 0}
    assert tmux_calls == [["tmux", "send-keys", "-t", "agent_t1", "-l", "--", " "]]


def test_the_screen_is_read_at_send_time(client, screens, tmux_calls, monkeypatch):
    """The dialog came up between two batches while the row still says
    working: the second batch is refused."""
    monkeypatch.setattr(server, "_agent_activity", lambda i, t: "working")
    assert _send(client, erase=0, text="please ").status_code == 200
    screens["t1"] = DIALOG
    r = _send(client, erase=0, text="yes ")
    assert r.status_code == 409 and r.json()["in_dialog"] is True
    assert len(tmux_calls) == 1


def test_a_failed_tmux_call_is_502(client, screens, monkeypatch):
    monkeypatch.setattr(server, "_live_session_name", lambda n: "agent_t1")
    monkeypatch.setattr(server, "_agent_activity", lambda i, t: "idle")
    monkeypatch.setattr(
        _proc, "run_capped", lambda argv, **kw: subprocess.CompletedProcess(argv, 1)
    )
    r = _send(client, erase=1, text="x")
    assert r.status_code == 502


def test_not_running_is_409_and_never_boots(client, screens, monkeypatch):
    booted = []
    monkeypatch.setattr(server, "_live_session_name", lambda n: None)
    monkeypatch.setattr(server, "_ensure_agent_session", lambda i, t: booted.append(t))
    monkeypatch.setattr(server, "_agent_activity", lambda i, t: "idle")
    r = _send(client, erase=1, text="x")
    assert r.status_code == 409
    assert "in_dialog" not in r.json()
    assert booted == []


def test_asleep_is_409_and_never_wakes(client, screens, tmux_calls, monkeypatch):
    woke = []
    monkeypatch.setattr(server._sleep, "is_sleeping", lambda t: True)
    monkeypatch.setattr(server._sleep, "ensure_awake", lambda *a, **k: woke.append(a))
    r = _send(client, erase=0, text="hi ")
    assert r.status_code == 409
    assert woke == [] and tmux_calls == []


def test_paused_is_409(client, screens, tmux_calls, monkeypatch):
    monkeypatch.setattr(
        server.ENGINE.instances["t1"], "Status", server.Paused, raising=False
    )
    r = _send(client, erase=0, text="hi ")
    assert r.status_code == 409 and r.json().get("paused") is True
    assert tmux_calls == []


@pytest.mark.parametrize(
    "body",
    [
        {"voice": True, "erase": 1, "text": "x"},  # submitted: not live typing
        {"submit": False, "erase": 1, "text": "x"},  # not voice
        {"voice": "yes", "submit": False, "erase": 1},  # not literally true
        {"voice": True, "submit": False, "enter": True, "erase": 1},
        {"voice": True, "submit": False, "erase": 0, "text": ""},
        {"voice": True, "submit": False, "erase": -2},
        {"voice": True, "submit": False, "erase": None, "text": "x"},
        {"voice": True, "submit": False, "erase": 1, "text": "a\rb"},
    ],
)
def test_malformed_is_400_with_nothing_pressed(
    client, screens, tmux_calls, monkeypatch, body
):
    monkeypatch.setattr(server, "_agent_activity", lambda i, t: "idle")
    r = client.post("/api/instances/t1/send", json=body)
    assert r.status_code == 400, r.text
    assert tmux_calls == [] and client._sent == []


def test_text_only_voice_typing_is_unchanged(client, screens, monkeypatch):
    """Without ``erase``: the c5 form, through the send primitive."""
    monkeypatch.setattr(server, "_agent_activity", lambda i, t: "working")
    r = client.post(
        "/api/instances/t1/send", json={"text": "add ", "submit": False, "voice": True}
    )
    assert r.json() == {"sent": True, "submitted": False}
    assert client._sent == [("agent_t1", "add ", False)]


def test_the_over_enter_is_gated_too(client, screens, tmux_calls, monkeypatch):
    """The desktop's "over" Enter (``{"enter": true, "voice": true}``): a
    dialog on screen is 409 with nothing pressed."""
    monkeypatch.setattr(server, "_agent_activity", lambda i, t: "working")
    screens["t1"] = DIALOG
    r = client.post("/api/instances/t1/send", json={"enter": True, "voice": True})
    assert r.status_code == 409 and r.json()["in_dialog"] is True
    assert client._sent == []


def test_live_tmux_round_trip(tmp_path, monkeypatch):
    """The argv really erases and types, ';' included, on a scratch tmux
    server (TMUX points the client at it; never the owner's server)."""
    import os
    import shutil
    import time

    if not shutil.which("tmux"):
        pytest.skip("no tmux")
    sock_dir = "/tmp/mf-c6g-%d" % os.getpid()
    os.makedirs(sock_dir, exist_ok=True)
    sock = os.path.join(sock_dir, "s")
    out = tmp_path / "out"
    env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_TMPDIR")}
    base = ["tmux", "-S", sock]
    subprocess.run(
        base + ["new-session", "-d", "-s", "g", "-x", "80", "-y", "5",
                "stty raw -echo; cat > %s" % out],
        env=env, check=True,
    )  # fmt: skip
    try:
        time.sleep(0.4)
        for erase, text in [(0, "fix the bu"), (1, "ag;"), (0, " ;"), (2, "é ")]:
            argv = agent_io.dictation_argv("g", erase, text)
            assert subprocess.run(base + argv[1:], env=env).returncode == 0
        time.sleep(0.4)
    finally:
        subprocess.run(base + ["kill-session", "-t", "g"], env=env)
        shutil.rmtree(sock_dir, ignore_errors=True)
    typed = out.read_bytes().decode()
    field = ""
    for ch in typed:
        field = field[:-1] if ch == "\x7f" else field + ch
    assert field == "fix the bag;é "
