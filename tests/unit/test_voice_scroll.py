"""Voice scroll: the grammar (``voice_router.scroll_of`` / ``command``), the
route that carries it out (``POST /api/voice/route`` → ``scroll``, "more"
repeating the same client's last scroll) and ``voice_actions.scroll`` on a
REAL tmux pane — a private server (``-L``), killed by name, never the
owner's."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid

import pytest

from backend.web.core import voice_actions as va
from backend.web.core import voice_router as vr
from tests.unit.test_voice_router import ROWS, env, route  # noqa: F401 — fixture

# --- the grammar -------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,dir_,amount,count",
    [
        ("scroll up", "up", "page", 1),
        ("Scroll down.", "down", "page", 1),
        ("scroll it up", "up", "page", 1),
        ("scroll the output down", "down", "page", 1),
        ("scroll the screen up", "up", "page", 1),
        ("scroll up a bit", "up", "half", 1),
        ("scroll down a little", "down", "half", 1),
        ("scroll up a little bit", "up", "half", 1),
        ("scroll up half a page", "up", "half", 1),
        ("scroll up 3 pages", "up", "page", 3),
        ("scroll down two lines", "down", "line", 2),
        ("scroll up one line", "up", "line", 1),
        ("scroll up a line", "up", "line", 1),
        ("scroll down 10 lines", "down", "line", 10),
        ("scroll up a few lines", "up", "line", 3),
        ("scroll down a couple of pages", "down", "page", 2),
        ("scroll up twice", "up", "page", 2),
        ("page up", "up", "page", 1),
        ("Page down.", "down", "page", 1),
        ("half page up", "up", "half", 1),
        ("page up twice", "up", "page", 2),
        ("page down three times", "down", "page", 3),
        ("scroll back up", "up", "page", 1),
        ("scroll back down", "down", "page", 1),
        ("scroll to the top", "up", "top", 1),
        ("scroll back to the top", "up", "top", 1),
        ("scroll to the bottom", "down", "bottom", 1),
        ("go to the top", "up", "top", 1),
        ("go to the bottom", "down", "bottom", 1),
        ("jump to the top", "up", "top", 1),
        ("jump to the bottom", "down", "bottom", 1),
        ("back to the bottom", "down", "bottom", 1),
        ("go back to the bottom", "down", "bottom", 1),
        ("scroll all the way up", "up", "top", 1),
        ("scroll all the way down", "down", "bottom", 1),
        ("jump to the end", "down", "bottom", 1),
        ("show me the latest", "down", "bottom", 1),
        ("show the latest", "down", "bottom", 1),
        ("go to the top of the output", "up", "top", 1),
        # fillers around it
        ("ok scroll up", "up", "page", 1),
        ("now scroll down please", "down", "page", 1),
        ("Mindflock, scroll up.", "up", "page", 1),
        ("can you scroll up a bit", "up", "half", 1),
        ("scroll up now", "up", "page", 1),
        ("scroll scroll up", "up", "page", 1),  # a doubled word
    ],
)
def test_scroll_phrases(text, dir_, amount, count):
    assert vr.command(text) == {
        "kind": "scroll",
        "dir": dir_,
        "amount": amount,
        "count": count,
    }


@pytest.mark.parametrize(
    "text",
    [
        "scroll down to the footer and fix the margin",
        "scroll up the list in the sidebar component",
        "make the page scroll up smoothly",
        "the scroll down button is broken",
        "page up is not handled in the editor",
        "go to the top of the file and add an import",
        "jump to the end of the function",
        "scroll",
        "show me the latest test results",
    ],
)
def test_scroll_lookalikes_are_words(text):
    got = vr.command(text)
    assert got is None or got["kind"] != "scroll", got


@pytest.mark.parametrize(
    "text",
    [
        "more",
        "More.",
        "keep going",
        "keep scrolling",
        "scroll more",
        "further",
        "again",
        "a bit more",
        "a little more",
        "some more",
        "once more",
        "more please",
    ],
)
def test_more_phrases(text):
    assert vr.command(text) == {"kind": "scroll_more"}


@pytest.mark.parametrize(
    "text",
    [
        "more tests please",
        "keep going with the refactor",
        "add more logging",
        "do it again",
    ],
)
def test_more_lookalikes_are_not_more(text):
    got = vr.command(text)
    assert got is None or got["kind"] != "scroll_more"


def test_answer_without_memory_makes_more_a_prompt():
    assert vr.answer("keep going", "docs", ROWS) == {"kind": "prompt"}


def test_answer_names_the_scrolled_session():
    got = vr.answer("scroll up", "docs", ROWS)
    assert got == {
        "kind": "scroll",
        "dir": "up",
        "amount": "page",
        "count": 1,
        "title": "docs",
    }
    assert (
        vr.answer("scroll up", "docs", ROWS, title="login-fix")["title"] == "login-fix"
    )
    assert "say" not in got


# --- the route -----------------------------------------------------------------------


@pytest.fixture
def scrolls(env, monkeypatch):  # noqa: F811 — the route fixture
    done = []

    def fake(title, direction, amount, count):
        done.append((title, direction, amount, count))
        return title != "gone"

    monkeypatch.setattr("backend.web.routes.voice._scroll_pane", fake, raising=True)
    return done


def test_route_scrolls_the_session_and_says_nothing(scrolls):
    got = route("scroll up a bit", "docs")
    assert got == {
        "kind": "scroll",
        "dir": "up",
        "amount": "half",
        "count": 1,
        "title": "docs",
        "ok": True,
    }
    assert scrolls == [("docs", "up", "half", 1)]


def test_route_scrolls_the_title_the_client_names(scrolls):
    got = route("page down", "docs", title="login-fix")
    assert got["title"] == "login-fix" and got["ok"] is True
    assert scrolls == [("login-fix", "down", "page", 1)]


def test_route_scroll_reads_no_rows(scrolls, env):  # noqa: F811
    route("scroll up", "docs")
    assert env.row_reads == 0


def test_route_scroll_failure_is_ok_false(scrolls):
    got = route("scroll up", "gone")
    assert got["kind"] == "scroll" and got["ok"] is False


def test_route_scroll_with_no_session(scrolls):
    got = route("scroll up", "")
    assert got == {
        "kind": "scroll",
        "dir": "up",
        "amount": "page",
        "count": 1,
        "title": "",
        "ok": False,
    }
    assert scrolls == []


def test_more_repeats_the_last_scroll(scrolls):
    route("scroll down three lines", "docs")
    got = route("more", "docs")
    assert got == {
        "kind": "scroll",
        "dir": "down",
        "amount": "line",
        "count": 3,
        "title": "docs",
        "ok": True,
    }
    assert route("keep going", "docs")["kind"] == "scroll"
    assert len(scrolls) == 3


def test_more_without_a_scroll_is_words(scrolls):
    assert route("keep going", "docs") == {"kind": "prompt"}
    assert scrolls == []


def test_more_is_per_session(scrolls):
    route("scroll up", "docs")
    assert route("more", "login-fix") == {"kind": "prompt"}


def test_more_is_per_client(scrolls):
    route("scroll up", "docs")
    from tests.unit.test_voice_router import client

    r = client.post(
        "/api/voice/route",
        json={"text": "more", "session": "docs", "client": "the-phone"},
    )
    assert r.json() == {"kind": "prompt"}


def test_more_forgets_after_the_window(scrolls, monkeypatch):
    route("scroll up", "docs")
    t0 = time.monotonic()
    monkeypatch.setattr(va, "_now", lambda: t0 + va.SCROLL_AGAIN_S + 1)
    assert route("more", "docs") == {"kind": "prompt"}


def test_a_failed_scroll_is_not_remembered(scrolls):
    route("scroll up", "gone")
    assert route("more", "gone") == {"kind": "prompt"}


def test_heard_routes_a_scroll_whisper_missed(scrolls):
    """Moonshine heard the control words; whisper wrote something else."""
    from tests.unit.test_voice_router import client

    r = client.post(
        "/api/voice/route",
        json={"text": "school up", "heard": "scroll up", "session": "docs"},
    )
    assert r.json()["kind"] == "scroll"
    assert scrolls == [("docs", "up", "page", 1)]


def test_heard_that_is_words_leaves_the_text(scrolls):
    from tests.unit.test_voice_router import client

    r = client.post(
        "/api/voice/route",
        json={"text": "scroll up", "heard": "fix the scroll bug", "session": "docs"},
    )
    assert r.json()["kind"] == "scroll"
    r = client.post(
        "/api/voice/route",
        json={
            "text": "fix the login bug",
            "heard": "fix the logging bug",
            "session": "docs",
        },
    )
    assert r.json() == {"kind": "prompt"}


# --- voice_actions.scroll on a real tmux pane ------------------------------------------

needs_tmux = pytest.mark.skipif(
    shutil.which("tmux") is None or os.name == "nt", reason="needs tmux"
)


@pytest.fixture
def pane(monkeypatch):
    """A private tmux server with one session full of history; yields its
    ``=name:`` target and a ``tmux`` runner. Killed by its own ``-L``."""
    from backend.session import mux

    sock = "mf-voice-scroll-%s" % uuid.uuid4().hex[:8]
    base = ["tmux", "-L", sock]
    monkeypatch.setattr(mux, "_GUARDED", mux.OrderedDict())

    def tmux(*args):
        return subprocess.run(
            base + list(args), capture_output=True, text=True, timeout=10
        )

    made = tmux(
        "-f",
        "/dev/null",
        "new-session",
        "-d",
        "-s",
        "vs",
        "-x",
        "80",
        "-y",
        "20",
        "cat",
    )
    assert made.returncode == 0, made.stderr
    try:
        tmux("set-option", "-g", "history-limit", "2000")
        for i in range(120):
            tmux("send-keys", "-t", "=vs:", "-l", "line %d" % i)
            tmux("send-keys", "-t", "=vs:", "Enter")
        monkeypatch.setattr(va, "TMUX", base)
        deadline = time.time() + 5
        while time.time() < deadline:
            if "line 119" in tmux("capture-pane", "-p", "-t", "=vs:").stdout:
                break
            time.sleep(0.05)
        yield "=vs:", tmux
    finally:
        subprocess.run(base + ["kill-server"], capture_output=True, timeout=10)


def _view(tmux):
    out = tmux(
        "display-message", "-p", "-t", "=vs:", "#{pane_in_mode} #{scroll_position}"
    ).stdout.split()
    return out[0] == "1", int(out[1]) if len(out) > 1 and out[1] else 0


@needs_tmux
def test_real_scroll_up_enters_copy_mode_and_pages(pane):
    target, tmux = pane
    assert va.scroll(target, "up", "page") is True
    in_mode, pos = _view(tmux)
    assert in_mode and pos > 10
    assert va.scroll(target, "up", "line", 3) is True
    assert _view(tmux)[1] == pos + 3
    assert va.scroll(target, "up", "half") is True
    assert _view(tmux)[1] > pos + 3


@needs_tmux
def test_real_scroll_top_and_bottom(pane):
    target, tmux = pane
    assert va.scroll(target, "up", "top") is True
    in_mode, pos = _view(tmux)
    hist = int(tmux("display-message", "-p", "-t", target, "#{history_size}").stdout)
    assert in_mode and pos == hist
    assert va.scroll(target, "down", "bottom") is True
    assert _view(tmux)[0] is False


@needs_tmux
def test_real_scroll_down_past_the_bottom_leaves_copy_mode(pane):
    target, tmux = pane
    va.scroll(target, "up", "line", 2)
    assert _view(tmux) == (True, 2)
    assert va.scroll(target, "down", "page") is True
    assert _view(tmux)[0] is False


@needs_tmux
def test_real_scroll_down_at_the_live_screen_is_a_no_op(pane):
    target, tmux = pane
    assert va.scroll(target, "down", "page") is True
    assert _view(tmux)[0] is False


@needs_tmux
def test_real_scroll_of_a_missing_pane_is_false(pane):
    assert va.scroll("=nope:", "up", "page") is False


@needs_tmux
def test_real_typing_after_a_voice_scroll_reaches_the_app(pane):
    """The safety rule: a scroll leaves the pane in copy-mode, and the next
    dictation (any send-keys through mux.run) leaves it first — the words
    reach the app instead of driving copy-mode."""
    from backend.session import mux

    target, tmux = pane
    va.scroll(target, "up", "page")
    assert _view(tmux)[0] is True
    assert "vs" in mux._GUARDED  # the scroll armed the guard for its session
    done = mux.run(
        [*va.TMUX, "send-keys", "-t", target, "-l", "--", "qhello"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert done.returncode == 0, done.stderr
    assert _view(tmux)[0] is False
    mux.run([*va.TMUX, "send-keys", "-t", target, "Enter"], timeout=10)
    deadline = time.time() + 5
    while time.time() < deadline:
        if "qhello" in tmux("capture-pane", "-p", "-t", target).stdout:
            break
        time.sleep(0.05)
    assert "qhello" in tmux("capture-pane", "-p", "-t", target).stdout


@needs_tmux
def test_real_scroll_of_a_mouse_app_sends_wheel_reports(pane):
    """An app that grabbed the mouse scrolls itself: it gets wheel reports
    (SGR), and tmux never enters copy-mode."""
    target, tmux = pane
    tmux(
        "respawn-pane", "-k", "-t", target, "printf '\\033[?1000h\\033[?1006h'; cat -v"
    )
    deadline = time.time() + 5
    while time.time() < deadline:
        flags = tmux("display-message", "-p", "-t", target, "#{mouse_any_flag}").stdout
        if flags.strip() == "1":
            break
        time.sleep(0.05)
    assert va.scroll(target, "up", "line") is True
    assert _view(tmux)[0] is False
    deadline = time.time() + 5
    while time.time() < deadline:
        if "[<64;" in tmux("capture-pane", "-p", "-t", target).stdout:
            break
        time.sleep(0.05)
    assert "[<64;40;" in tmux("capture-pane", "-p", "-t", target).stdout
