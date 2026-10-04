"""In-place pane find (core/pane_find.py): pure hit logic, plus the tmux
copy-mode stepping driven against a real, private tmux server."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from backend.web.core import pane_find as pf

# --------------------------------------------------------------------------- #
# Real tmux
# --------------------------------------------------------------------------- #

needs_tmux = pytest.mark.skipif(
    shutil.which("tmux") is None, reason="tmux not installed"
)

# Hits on lines 5, 42, 79, 116, 153, 190 — two per line ("Needle", "needle").
_SCRIPT = (
    "for i in $(seq 1 200); do if [ $((i % 37)) = 5 ]; then "
    'echo "line $i the Needle x needle"; else echo "line $i plain"; fi; done; '
    'echo "odd -x #{pane_id} 100% a.b"; sleep 600'
)


@pytest.fixture
def pane(monkeypatch):
    sock = "mf-find-" + uuid.uuid4().hex[:8]
    tmux = ["tmux", "-L", sock]
    env = {k: v for k, v in os.environ.items() if k != "TMUX"}
    subprocess.run(
        [
            *tmux,
            "-f",
            "/dev/null",
            "new-session",
            "-d",
            "-s",
            "t",
            "-x",
            "80",
            "-y",
            "10",
            _SCRIPT,
        ],
        check=True,
        env=env,
    )
    monkeypatch.setattr(pf, "TMUX", tmux)
    for _ in range(50):
        out = subprocess.run(
            [*tmux, "capture-pane", "-p", "-t", "t"], capture_output=True, text=True
        )
        if "odd -x" in out.stdout:
            break
        time.sleep(0.05)
    yield tmux
    subprocess.run([*tmux, "kill-server"], capture_output=True)
    # kill-server leaves the -L socket file behind; don't litter tmux's dir.
    tmpdir = os.environ.get("TMUX_TMPDIR") or "/tmp"
    Path(tmpdir, f"tmux-{os.getuid()}", sock).unlink(missing_ok=True)


def _in_mode(tmux):
    out = subprocess.run(
        [*tmux, "display-message", "-p", "-t", "t", "#{pane_in_mode}"],
        capture_output=True,
        text=True,
    )
    return out.stdout.strip() == "1"


def _at(tmux, res):
    """The DISPLAYED text the result points at (the copy-mode view, which
    plain capture-pane doesn't show), and the line number on that row."""
    pos = subprocess.run(
        [
            *tmux,
            "display-message",
            "-p",
            "-t",
            "t",
            "#{?pane_in_mode,#{scroll_position},0}\t#{pane_height}",
        ],
        capture_output=True,
        text=True,
    ).stdout.split()
    sp, h = int(pos[0] or 0), int(pos[1])
    rows = subprocess.run(
        [*tmux, "capture-pane", "-p", "-t", "t", "-S", str(-sp), "-E", str(h - 1 - sp)],
        capture_output=True,
        text=True,
    ).stdout.split("\n")
    text = rows[res["row"]]
    m = re.match(r"line (\d+)", text)
    return text[res["col"] : res["col"] + res["len"]], int(m.group(1)) if m else None


@needs_tmux
def test_search_lands_on_newest_and_steps_both_ways(pane):
    res = pf.find("t", "needle", "search")
    assert (res["total"], res["index"]) == (12, 12)  # case-insensitive by default
    assert _at(pane, res) == ("needle", 190)
    order = [(res["index"], _at(pane, res))]
    for _ in range(3):
        res = pf.find("t", "needle", "older")
        order.append((res["index"], _at(pane, res)))
    assert order == [
        (12, ("needle", 190)),
        (11, ("Needle", 190)),
        (10, ("needle", 153)),
        (9, ("Needle", 153)),
    ]
    assert pf.find("t", "needle", "newer")["index"] == 10
    for _ in range(2):
        res = pf.find("t", "needle", "newer")
    assert res["index"] == 12
    res = pf.find("t", "needle", "newer")  # wraps to the oldest
    assert res["index"] == 1 and _at(pane, res) == ("Needle", 5)
    res = pf.find("t", "needle", "older")
    assert res["index"] == 12 and _at(pane, res) == ("needle", 190)


@needs_tmux
def test_options(pane):
    from backend.web.core.find_query import Query

    assert pf.find("t", Query("Needle", case=True), "search")["total"] == 6
    assert pf.find("t", Query("need", word=True), "search")["total"] == 0
    assert pf.find("t", Query(r"line 1\d\d the", regex=True), "search")["total"] == 3
    res = pf.find("t", Query("line 42", near="needle", within=0), "search")
    assert res["total"] == 1
    assert _at(pane, res) == ("line 42", 42)
    assert len(res["spans"]) == 2  # the partner is on screen too
    bad = pf.find("t", Query("(", regex=True), "search")
    assert bad["status"] == "error" and "regular expression" in bad["error"]


@needs_tmux
@pytest.mark.parametrize("query", ["-x", "#{pane_id}", "100%", "a.b"])
def test_query_is_literal_text(pane, query):
    res = pf.find("t", query, "search")
    assert (res["total"], res["index"]) == (1, 1)
    assert _at(pane, res)[0] == query


@needs_tmux
def test_no_hits_and_close_return_to_live(pane):
    pf.find("t", "needle", "search")
    pf.find("t", "needle", "older")
    pf.find("t", "needle", "older")
    assert _in_mode(pane)
    assert pf.find("t", "zzz", "search") == {"total": 0, "index": 0}
    assert not _in_mode(pane)
    pf.find("t", "Needle", "search")
    pf.find("t", "needle", "older")
    pf.find("t", "needle", "older")
    assert pf.find("t", "needle", "close") == {"total": 0, "index": 0}
    assert not _in_mode(pane)


@needs_tmux
def test_unreachable_session_is_none(pane):
    assert pf.find("nope", "needle", "search") is None


@needs_tmux
@pytest.mark.parametrize("mode", ["1000", "1002", "1003"])
def test_find_mode_follows_what_the_wheel_scrolls(pane, mode):
    # Nothing grabbed: the wheel scrolls tmux's history.
    assert pf.find_mode("t") == "tmux"
    # A TUI grabbing the mouse (any mode) scrolls itself: tmux's history is
    # redraw debris the reader never sees, so drive the app instead.
    subprocess.run(
        [
            *pane,
            "new-session",
            "-d",
            "-s",
            "tui",
            f"printf '\\033[?{mode}h'; sleep 600",
        ],
        check=True,
    )
    for _ in range(40):
        if pf.find_mode("tui") == "scroll":
            break
        time.sleep(0.05)
    assert pf.find_mode("tui") == "scroll"
    assert pf.find_mode("nope") is None


@needs_tmux
def test_find_mode_alt_screen_without_mouse_is_the_overlay(pane):
    subprocess.run(
        [*pane, "new-session", "-d", "-s", "pager", "printf '\\033[?1049h'; sleep 600"],
        check=True,
    )
    for _ in range(40):
        if pf.find_mode("pager") == "overlay":
            break
        time.sleep(0.05)
    assert pf.find_mode("pager") == "overlay"
