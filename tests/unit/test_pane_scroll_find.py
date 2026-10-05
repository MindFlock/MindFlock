"""Scroll-mode find (core/pane_scroll_find.py): the pure stitching/locating
logic, plus index-then-jump runs against a fake mouse-grabbing TUI on a
private tmux server (tests/fixtures/fake_scroll_tui.py)."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

from backend.web.core import pane_find as pf
from backend.web.core import pane_scroll_find as sf

TUI = Path(__file__).resolve().parents[1] / "fixtures" / "fake_scroll_tui.py"


# --------------------------------------------------------------------------- #
# Pure logic
# --------------------------------------------------------------------------- #


def _content(n):
    return [f"c{i}" for i in range(n)]


def test_measure_counts_only_rows_that_moved():
    c = _content(30)
    a = ["HEAD"] + c[10:20] + ["FOOT"]
    b = ["HEAD"] + c[7:17] + ["FOOT"]  # scrolled up 3: content moved down
    assert sf.measure(a, b) == (3, 7)
    assert sf.measure(b, a) == (-3, 7)
    assert sf.measure(a, a) == (0, 0)


def test_stood_still_tolerates_a_ticking_row():
    a = [f"r{i}" for i in range(10)]
    assert sf.stood_still(a, a)
    assert sf.stood_still(a, a[:-1] + ["clock 12:01"])
    assert not sf.stood_still(a, a[3:] + ["x", "y", "z"])


def test_moved_region_skips_chrome_and_widens_at_the_entering_edge():
    c = _content(30)
    a = ["HEAD"] + c[10:20] + ["FOOT"]
    b = ["HEAD"] + c[7:17] + ["FOOT"]
    assert sf.moved_region(a, b, 3) == (1, 10)
    assert sf.moved_region(b, a, -3) == (1, 10)


def test_build_index_stitches_a_sweep_and_drops_an_overlay():
    doc = [f"line {i}" for i in range(40)]

    def screen(top, overlay=False):
        rows = doc[top : top + 10]
        if overlay:
            rows[5] = "JUMP TO BOTTOM"
        return ["pinned"] + rows + ["> input"]

    # Walking up by 4 lines a screen, a hint floating on row 6 throughout.
    tops = [30, 26, 22, 18]
    screens = [screen(t, overlay=True) for t in tops]
    ds = [4, 4, 4]
    lines = sf.build_index(screens, ds, 1, 10)
    assert "JUMP TO BOTTOM" not in lines
    assert lines == doc[18:40]


def test_build_index_leaves_out_a_lazily_blank_bottom_view():
    doc = [f"line {i}" for i in range(20)]
    lazy = ["", "", "", ""] + doc[14:20]  # only the last lines drawn
    full = doc[9:19]  # one notch up, everything drawn
    full2 = doc[8:18]
    lines = sf.build_index([lazy, full, full2], [-5 + 6, 1], 0, 9)
    assert "" not in lines[:-1]
    assert lines == doc[8:20]


def test_locate_votes_by_distinctive_lines():
    doc = [f"line {i}" for i in range(50)] + ["same"] * 3
    lookup = sf._unique(doc)
    screen = ["pinned"] + doc[20:30] + ["> input"]
    assert sf.locate(lookup, screen, 1, 10) == 20
    assert sf.locate(lookup, ["nothing"] * 12, 1, 10) is None


def test_pick_start_prefers_the_newest_at_or_above_the_view():
    hits = [(3, 0), (40, 1), (40, 9), (90, 2)]
    assert sf.pick_start(hits, 60) == 2
    assert sf.pick_start(hits, 1000) == 3
    assert sf.pick_start(hits, 1) == 0


def test_burst_ticks_never_resends_a_burst_seen_to_overshoot():
    # Accelerating wheel: 4 notches moved 6 lines, 12 moved 36. Scaling up
    # from 4 alone asks for 12 to cover 18 lines, which overshoots both ways
    # and bounces forever; the overshooting burst bounds it instead.
    seen = {4: 6, 8: 20, 12: 36, 16: 72}
    assert 4 <= sf.burst_ticks(seen, 18, 1.0) < 8
    # Far moves still scale from the biggest burst that fell short.
    assert sf.burst_ticks(seen, 100, 1.0) == 22
    # No rates yet: a third of the distance in notches.
    assert sf.burst_ticks({}, 30, 1.0) == 10
    # Only an overshoot known: stay under it.
    assert 1 <= sf.burst_ticks({8: 20}, 10, 1.0) < 8


def test_bottom_chrome_matches_pinned_rows_only():
    normal = ["a", "b", "c", "────", "> ", "────", "status"]
    tall = ["x"] * 10 + ["", "", "────", "> ", "────", "status"]
    assert sf.bottom_chrome(normal, tall) == 4


def test_find_row_hits_smart_case():
    scr = ["a Needle b needle", "NEEDLE"]
    assert sf.find_row_hits(scr, "needle", range(2)) == [(0, 2), (0, 11), (1, 0)]
    assert sf.find_row_hits(scr, "Needle", range(2)) == [(0, 2)]


# --------------------------------------------------------------------------- #
# Real tmux + fake TUI
# --------------------------------------------------------------------------- #

needs_tmux = pytest.mark.skipif(
    shutil.which("tmux") is None, reason="tmux not installed"
)

N_LINES = 400
LINES = []
for _i in range(N_LINES):
    if _i == 40:
        LINES.append(f"line {_i}: needle and needle")
    elif _i % 17 == 5 or _i == 3:
        LINES.append(f"line {_i}: the needle is here")
    elif _i % 10 == 0:
        LINES.append(f"## section {_i}")
    else:
        LINES.append(f"line {_i}: plain text")
HIT_ORDER = sorted([(i, 0) for i, t in enumerate(LINES) if "needle" in t] + [(40, 1)])


@pytest.fixture
def tui(monkeypatch):
    sock = "mf-sfind-" + uuid.uuid4().hex[:8]
    tmux = ["tmux", "-L", sock]
    env = {k: v for k, v in os.environ.items() if k != "TMUX"}
    monkeypatch.setattr(pf, "TMUX", tmux)
    monkeypatch.setattr(sf, "_states", {})
    monkeypatch.setattr(sf, "_keys_ok", {})
    clients = []

    def start(
        lpt=1, accel=False, swallow=False, lazy=0, max_rows=0, keys=False, name="t"
    ):
        cmd = (
            f"{sys.executable} {TUI} {N_LINES} {lpt} {int(accel)} {int(swallow)}"
            f" {lazy} {max_rows} {int(keys)}"
        )
        subprocess.run(
            [
                *tmux,
                "-f",
                "/dev/null",
                "new-session",
                "-d",
                "-s",
                name,
                "-x",
                "80",
                "-y",
                "24",
                cmd,
            ],
            check=True,
            env=env,
        )
        subprocess.run(
            [*tmux, "set-option", "-t", name, "window-size", "latest"], check=True
        )
        for _ in range(100):
            out = subprocess.run(
                [*tmux, "display-message", "-p", "-t", name, "#{mouse_any_flag}"],
                capture_output=True,
                text=True,
            )
            if out.stdout.strip() == "1":
                break
            time.sleep(0.05)
        time.sleep(0.2)
        return name

    yield start, tmux
    subprocess.run([*tmux, "kill-server"], capture_output=True)
    tmpdir = os.environ.get("TMUX_TMPDIR") or "/tmp"
    Path(tmpdir, f"tmux-{os.getuid()}", sock).unlink(missing_ok=True)


def _screen(tmux, name):
    return subprocess.run(
        [*tmux, "capture-pane", "-p", "-t", name], capture_output=True, text=True
    ).stdout.split("\n")


def _found(tmux, name, res):
    assert res["status"] == "found", res
    text = _screen(tmux, name)[res["row"]]
    m = re.match(r"line (\d+):", text)
    assert m, f"hit landed on chrome: {text!r}"
    assert text[res["col"] : res["col"] + 6] == "needle", text
    return (int(m.group(1)), 1 if res["col"] > text.index("needle") else 0)


APPS = {
    # Claude Code-like: pages with keys, lazy at the ends.
    "keys": dict(keys=True, lazy=30),
    # Wheel only, accelerating bursts, swallowed reversals, lazy bottom and a
    # height cap below the scrollback: a multi-screen tall sweep.
    "wheel": dict(lpt=1, accel=True, swallow=True, lazy=30, max_rows=150),
    # opencode-like: 3 lines a notch, no keys.
    "wheel3": dict(lpt=3),
}


@needs_tmux
@pytest.mark.parametrize("app", sorted(APPS))
def test_index_is_the_whole_scrollback_and_leaves_the_view_alone(tui, app):
    start, tmux = tui
    name = start(**APPS[app])
    before = _screen(tmux, name)
    res = sf.find(name, "needle", "prepare")
    assert res["status"] == "ready", res
    lines = sf._states[name].index.lines
    # Every content line, in order, and no chrome, pinned row or hint.
    assert [line for line in lines if line.startswith(("line", "##"))] == LINES
    assert not any(
        "pinned" in line or "jump needle" in line or "input box" in line
        for line in lines
    )
    assert _screen(tmux, name) == before
    out = subprocess.run(
        [*tmux, "display-message", "-p", "-t", name, "#{window_height}"],
        capture_output=True,
        text=True,
    )
    assert out.stdout.strip() == "24"


@needs_tmux
@pytest.mark.parametrize("app", sorted(APPS))
def test_counts_first_then_walks_every_hit_and_wraps(tui, app):
    start, tmux = tui
    name = start(**APPS[app])
    sf.find(name, "needle", "prepare")
    first = sf.find(name, "needle", "search")
    assert first["total"] == len(HIT_ORDER)
    assert first["index"] == len(HIT_ORDER)  # the newest, from the bottom
    seen = [_found(tmux, name, first)]
    for _ in range(len(HIT_ORDER) - 1):
        seen.append(_found(tmux, name, sf.find(name, "needle", "older")))
    assert seen == HIT_ORDER[::-1]
    wrapped = sf.find(name, "needle", "older")
    assert wrapped["index"] == len(HIT_ORDER)
    assert _found(tmux, name, wrapped) == HIT_ORDER[-1]
    assert _found(tmux, name, sf.find(name, "needle", "newer")) == HIT_ORDER[0]


@needs_tmux
def test_hits_land_inside_the_region(tui):
    start, tmux = tui
    name = start(**APPS["keys"])
    sf.find(name, "needle", "prepare")
    for op in ("search", "older", "newer", "newer"):
        res = sf.find(name, "needle", op)
        lo, hi = res["region"]
        assert lo <= res["row"] <= hi, res


@needs_tmux
def test_unchanged_pane_reuses_the_index(tui):
    start, tmux = tui
    name = start(**APPS["keys"])
    sf.find(name, "needle", "prepare")
    sf.find(name, "needle", "close")
    again = sf.find(name, "needle", "prepare")
    assert again.get("cached") is True


@needs_tmux
def test_no_hits_reports_none_without_moving(tui):
    start, tmux = tui
    name = start(**APPS["keys"])
    sf.find(name, "haystack", "prepare")
    before = _screen(tmux, name)
    res = sf.find(name, "haystack", "search")
    assert res["status"] == "none" and res["total"] == 0
    assert _screen(tmux, name) == before


@needs_tmux
@pytest.mark.parametrize("app", ["keys", "wheel3"])
def test_close_returns_to_the_bottom(tui, app):
    start, tmux = tui
    name = start(**APPS[app])
    sf.find(name, "needle", "prepare")
    sf.find(name, "needle", "search")
    sf.find(name, "needle", "newer")  # the very top
    assert sf.find(name, "needle", "close")["status"] == "closed"
    assert f"line {N_LINES - 1}:" in "\n".join(_screen(tmux, name))


@needs_tmux
def test_cancel_stops_a_running_index(tui):
    start, tmux = tui
    name = start(**APPS["wheel"])
    got = {}
    t = threading.Thread(target=lambda: got.update(sf.find(name, "needle", "prepare")))
    t0 = time.monotonic()
    t.start()
    time.sleep(0.3)
    sf.find(name, "needle", "cancel")
    t.join(10)
    assert got.get("status") == "cancelled"
    assert time.monotonic() - t0 < 5
    # The tall window never outlives the step that made it.
    out = subprocess.run(
        [*tmux, "display-message", "-p", "-t", name, "#{window_height}"],
        capture_output=True,
        text=True,
    )
    assert out.stdout.strip() == "24"


@needs_tmux
def test_refuses_a_pane_whose_app_has_no_mouse(tui):
    _, tmux = tui
    subprocess.run(
        [
            *tmux,
            "-f",
            "/dev/null",
            "new-session",
            "-d",
            "-s",
            "plain",
            "-x",
            "80",
            "-y",
            "20",
            "sleep 60",
        ],
        check=True,
    )
    res = sf.find("plain", "needle", "search")
    assert res["status"] == "error"
    assert sf.find("nope", "needle", "search") is None


@needs_tmux
def test_observe_folds_new_output_into_the_index(tui):
    start, tmux = tui
    name = start(**APPS["keys"])
    sf.find(name, "needle", "prepare")
    before = len(sf._states[name].index.lines)
    subprocess.run([*tmux, "send-keys", "-t", name, "-l", "+++"], check=True)
    time.sleep(0.3)
    sf.observe(name)
    lines = sf._states[name].index.lines
    assert len(lines) == before + 3
    assert lines[-1] == f"line {N_LINES + 2}: fresh needle"
    # Still current, so a reopened find reuses it — and counts the new hits.
    assert sf.find(name, "needle", "prepare").get("cached") is True
    assert sf.find(name, "needle", "search")["total"] == len(HIT_ORDER) + 3


@needs_tmux
def test_observe_leaves_a_scrolled_up_view_alone(tui):
    start, tmux = tui
    name = start(**APPS["keys"])
    sf.find(name, "needle", "prepare")
    sf.find(name, "needle", "search")
    sf.find(name, "needle", "newer")  # far up
    lines = list(sf._states[name].index.lines)
    sf.observe(name)
    assert sf._states[name].index.lines == lines


@needs_tmux
def test_background_index_is_announced_and_leaves_everything_as_it_was(tui):
    start, tmux = tui
    name = start(**APPS["keys"])
    before = _screen(tmux, name)
    said = []
    assert sf.background_index(name, said.append) is True
    assert said == [True, False]
    assert not sf.needs_index(name)
    assert _screen(tmux, name) == before
    assert sf.find(name, "needle", "prepare").get("cached") is True


@needs_tmux
def test_human_input_cancels_a_background_index(tui):
    start, tmux = tui
    name = start(**APPS["wheel"])  # slow enough to catch mid-sweep
    said = []
    t = threading.Thread(
        target=lambda: said.append(sf.background_index(name, lambda on: None))
    )
    t.start()
    time.sleep(0.5)
    sf.cancel_background(name)
    t.join(10)
    assert said == [False]
    assert sf.needs_index(name)
    out = subprocess.run(
        [*tmux, "display-message", "-p", "-t", name, "#{window_height}"],
        capture_output=True,
        text=True,
    )
    assert out.stdout.strip() == "24"
    assert f"line {N_LINES - 1}:" in "\n".join(
        _screen(tmux, name)
    )  # back at the bottom
