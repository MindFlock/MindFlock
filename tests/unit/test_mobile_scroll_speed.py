"""The phone's own scroll speed (``/m`` ⋯ → Scroll speed).

Owner: "I should be able to edit scroll speed on mobile independent of scroll
speed on PC. Maybe add the option to the 3 dots thing."

The phone's touch scrollback sends SGR wheel ticks down the PTY, and tmux
scrolls each tick by the desktop's wheel speed (a global binding). So the phone
keeps its own setting, ``ui.mobile_scroll_speed`` (lines per row dragged,
synced, default 3 = how it felt before), and divides the desktop's lines per
tick back out (``touchScrollTicks``), carrying the remainder between moves.

The phone's functions run as written under node.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.config import settings as store
from backend.web import server
from backend.web.core import settings_sync, terminal

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "backend" / "web" / "static"

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")


def _js() -> str:
    return (STATIC / "mobile.js").read_text()


def _whole(js: str, name: str) -> str:
    """``function name(…) { … }`` as written (two-space indent)."""
    start = js.index("  function " + name + "(")
    end = js.index("\n  }\n", start) + len("\n  }\n")
    return js[start:end]


def _var(js: str, name: str) -> str:
    line = next(
        ln for ln in js.splitlines() if ln.lstrip().startswith("var " + name + " = ")
    )
    return line.strip() + "\n"


def _run(script: str):
    res = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30
    )
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout.strip().splitlines()[-1])


def _math(js: str) -> str:
    return (
        _var(js, "touchSpeed")
        + _whole(js, "tmuxLinesPerTick")
        + _whole(js, "touchScrollTicks")
        + _whole(js, "touchSpeedHint")
    )


# --------------------------------------------------------------------------- #
# the setting
# --------------------------------------------------------------------------- #
def test_default_is_unset_and_omitted():
    ui = store.UiSettings()
    assert ui.mobile_scroll_speed is None
    assert "mobile_scroll_speed" not in ui.to_dict()
    assert store.MOBILE_SCROLL_SPEED_DEFAULT == 3


@pytest.mark.parametrize(
    "raw, want",
    [
        (4, 4),
        ("6", 6),
        (2.4, 2),
        (0, 1),  # clamp low
        (-5, 1),
        (99, 8),  # clamp high
        (float("inf"), 8),
        ("nan", None),
        ("fast", None),
        (None, None),
        (True, None),
    ],
)
def test_clamped_to_whole_lines(raw, want):
    got = store.UiSettings.from_dict({"mobile_scroll_speed": raw}).mobile_scroll_speed
    assert got == want
    if want is not None:
        assert isinstance(got, int)


def test_round_trips_beside_the_desktop_speed():
    ui = store.UiSettings(scroll_speed=2, mobile_scroll_speed=5)
    assert ui.to_dict() == {"scroll_speed": 2, "mobile_scroll_speed": 5}
    assert store.UiSettings.from_dict(ui.to_dict()) == ui


def test_it_syncs_with_its_own_label():
    assert "mobile_scroll_speed" in settings_sync.SYNCED["ui"]
    assert settings_sync.LABELS["ui.mobile_scroll_speed"] == "Phone scroll speed"
    assert settings_sync.LABELS["ui.scroll_speed"] == "Terminal scroll speed (desktop)"


# --------------------------------------------------------------------------- #
# the route
# --------------------------------------------------------------------------- #
@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.delenv("CS_WEB_MODE", raising=False)
    monkeypatch.setenv("MINDFLOCK_AUTH", "0")
    monkeypatch.setattr(terminal, "SCROLL_SPEED_PATH", tmp_path / "scroll-speed")
    return TestClient(server.app)


def test_get_says_the_default_and_the_desktop_lines_per_tick(client):
    j = client.get("/api/mobile-scroll-speed").json()
    assert j["speed"] == 3 and j["default"] == 3
    assert (j["min"], j["max"]) == (1, 8)
    assert j["desktop"] == 1 and j["lines_per_tick"] == 1


def test_post_saves_the_synced_setting_and_leaves_the_desktop_alone(client):
    r = client.post("/api/mobile-scroll-speed", json={"speed": 12})
    assert r.status_code == 200
    assert r.json()["speed"] == 8  # clamped
    store.invalidate()
    ui = store.load_settings().ui
    assert ui.mobile_scroll_speed == 8
    assert ui.scroll_speed is None
    assert terminal.load_scroll_speed() == 1
    assert client.get("/api/mobile-scroll-speed").json()["speed"] == 8
    assert (
        client.post("/api/mobile-scroll-speed", json={"speed": "x"}).status_code == 400
    )


def test_lines_per_tick_follows_the_desktop_speed(client, monkeypatch):
    monkeypatch.setattr(server, "apply_scroll_speed", lambda speed: None)
    client.post("/api/scroll-speed", json={"speed": 2})
    j = client.get("/api/mobile-scroll-speed").json()
    assert (j["desktop"], j["lines_per_tick"], j["speed"]) == (2, 2, 3)


@pytest.mark.parametrize(
    "desktop, lines",
    [(1 / 3, 1), (2 / 3, 1), (1, 1), (4 / 3, 1), (5 / 3, 2), (2, 2), (3, 3)],
)
def test_scroll_lines_per_tick_is_the_tmux_binding(desktop, lines):
    assert terminal.scroll_lines_per_tick(desktop) == lines


# --------------------------------------------------------------------------- #
# the phone's math
# --------------------------------------------------------------------------- #
@needs_node
def test_lines_per_tick_matches_the_server():
    js = _math(_js())
    speeds = [1 / 3, 0.6667, 1, 1.3333, 1.6667, 2, 2.3333, 2.6667, 3]
    got = _run(
        js
        + "console.log(JSON.stringify(%s.map(tmuxLinesPerTick)));" % json.dumps(speeds)
    )
    assert got == [terminal.scroll_lines_per_tick(s) for s in speeds]
    assert _run(
        js
        + 'console.log(JSON.stringify([tmuxLinesPerTick(undefined), tmuxLinesPerTick("x"), tmuxLinesPerTick(0)]));'
    ) == [1, 1, 1]


@needs_node
@pytest.mark.parametrize("desktop", [1 / 3, 1, 2, 3])
@pytest.mark.parametrize("speed", [1, 3, 8])
def test_lines_scrolled_do_not_move_with_the_desktop(desktop, speed):
    """A drag of 12 rows in 60 small moves scrolls speed × 12 lines whatever
    the desktop speed: ticks × lines-per-tick, the carry keeping slow moves."""
    got = _run(_math(_js()) + """
var perTick = tmuxLinesPerTick(%r), carry = 0, ticks = 0;
for (var i = 0; i < 60; i++) {
  var s = touchScrollTicks(0.2, %d, perTick, carry);
  ticks += s.ticks; carry = s.carry;
}
console.log(JSON.stringify({ lines: ticks * perTick, carry: carry, perTick: perTick }));
""" % (desktop, speed))
    want = speed * 12
    # At most one tick (perTick lines) still owed in the carry.
    assert want - got["perTick"] < got["lines"] <= want
    assert got["lines"] + got["carry"] * got["perTick"] == pytest.approx(want)


@needs_node
def test_remainders_carry_between_moves():
    got = _run(_math(_js()) + """
var a = touchScrollTicks(0.25, 1, 1, 0);       // a quarter row: nothing yet
var b = touchScrollTicks(0.25, 1, 1, a.carry);
var c = touchScrollTicks(0.5, 1, 1, b.carry);  // the fourth quarter sends one
var d = touchScrollTicks(1, 3, 2, 0);          // 1.5 ticks: one now, half owed
var e = touchScrollTicks(1, 3, 2, d.carry);    // 1.5 + 0.5: two
console.log(JSON.stringify([a.ticks, b.ticks, c.ticks, c.carry, d.ticks, d.carry, e.ticks, e.carry]));
""")
    assert got == [0, 0, 1, 0, 1, 0.5, 2, 0]


@needs_node
def test_the_default_feels_like_before():
    """Before: 3 ticks per row dragged at the desktop default of 1 line a tick."""
    got = _run(_math(_js()) + """
var r = [];
[1, 2, 5, 10].forEach(function (cells) {
  r.push(touchScrollTicks(cells, touchSpeed.speed, tmuxLinesPerTick(1), 0).ticks);
});
console.log(JSON.stringify({ ticks: r, speed: touchSpeed.speed, def: touchSpeed.def }));
""")
    assert got == {"ticks": [3, 6, 15, 30], "speed": 3, "def": 3}


@needs_node
def test_the_hint_names_the_speed():
    got = _run(
        _math(_js())
        + "console.log(JSON.stringify([touchSpeedHint(3, 3), touchSpeedHint(1, 3), touchSpeedHint(6, 3)]));"
    )
    assert got == [
        "3 lines per row dragged (default)",
        "1 line per row dragged",
        "6 lines per row dragged",
    ]


def test_the_touch_handler_uses_the_phone_speed():
    js = _js()
    move = js[js.index('termHost.addEventListener("touchmove"') :]
    move = move[: move.index("}, { capture: true, passive: false });")]
    assert (
        "touchScrollTicks(Math.abs(dy) / cellH, touchSpeed.speed, touchSpeed.perTick, touchCarry)"
        in move
    )
    assert "TOUCH_SCROLL_MULT" not in js  # the fixed 3x is gone
    assert 'fetch("/api/mobile-scroll-speed")' in js


# --------------------------------------------------------------------------- #
# the ⋯ sheet
# --------------------------------------------------------------------------- #
_SHEET = r"""
function El(tag) { this.tag = tag; this.children = []; this.textContent = ""; this.className = "";
  this.listeners = {}; var cls = {};
  this.classList = { toggle: function (c, on) { cls[c] = !!on; } };
}
El.prototype.appendChild = function (c) { this.children.push(c); return c; };
El.prototype.addEventListener = function (ev, fn) { this.listeners[ev] = fn; };
El.prototype.querySelector = function (sel) {
  var want = sel.replace(".", "");
  for (var i = 0; i < this.children.length; i++)
    if (this.children[i].className === want) return this.children[i];
  return null;
};
Object.defineProperty(El.prototype, "innerHTML", { set: function () { this.children = []; } });
var document = { createElement: function (t) { return new El(t); } };
var actListEl = new El("div"), actTitleEl = new El("div"), actStateEl = new El("div"), actErrEl = new El("div");
var actFor = "", actBusy = false, actOpenedAt = 0, ACT_SETTLE_MS = -1;
function actSettled() { return true; }
function actError(m) { actErrEl.textContent = m || ""; }
function instByTitle() { return null; }
function nameOf(t) { return t; }
function folderNote(t) { var e = new El("div"); e.textContent = t; return e; }
function closeActSheet() {} function openRecentSheet() {}
var posted = [], answer = { ok: true, status: 200 };
function fetch(url, opts) {
  if (opts && opts.method === "POST") posted.push(JSON.parse(opts.body));
  var body = opts && opts.method === "POST" ? JSON.parse(opts.body) : {};
  return Promise.resolve({ ok: answer.ok, status: answer.status, json: function () {
    return Promise.resolve({ speed: body.speed, "default": 3, min: 1, max: 8, desktop: 2, lines_per_tick: 2 });
  } });
}
function rows() { return actListEl.children.map(function (c) {
  if (c.className === "act-speed") return { picker: c.children[1].children.map(function (b) {
    return b.textContent + (/ on$/.test(b.className) ? "*" : ""); }) };
  return c.children.map(function (x) { return x.textContent; }).join(" | ");
}); }
function settle() { return new Promise(function (r) { setTimeout(r, 20); }); }
"""


def _sheet(js: str) -> str:
    return (
        _SHEET
        + _math(js)
        + _var(js, "TOUCH_SPEED_STEPS")
        + _var(js, "actSpeedOpen")
        + "".join(
            _whole(js, n)
            for n in (
                "applyTouchSpeed",
                "actRow",
                "renderActSheet",
                "refreshSpeedHint",
                "touchSpeedPicker",
                "setTouchSpeed",
            )
        )
    )


@needs_node
def test_the_more_sheet_has_a_scroll_speed_row_that_picks_and_saves():
    got = _run(_sheet(_js()) + """
var out = {};
renderActSheet();
out.closed = rows();
var speedRow = actListEl.children[actListEl.children.length - 1];
speedRow.listeners.click();
out.open = rows();
var picker = actListEl.children[actListEl.children.length - 1];
picker.children[1].children[4].listeners.click();   // "6"
out.now = touchSpeed.speed;
settle().then(function () {
  out.posted = posted.slice();
  out.after = rows();
  out.perTick = touchSpeed.perTick;
  answer.ok = false; answer.status = 500;
  picker = actListEl.children[actListEl.children.length - 1];
  picker.children[1].children[0].listeners.click();  // "1", but the save fails
  return settle();
}).then(function () {
  out.failed = { speed: touchSpeed.speed, err: actErrEl.textContent };
  console.log(JSON.stringify(out));
});
""")
    assert got["closed"][-1] == "Scroll speed | 3 lines per row dragged (default)"
    assert got["open"][-1] == {"picker": ["1", "2", "3*", "4", "6", "8"]}
    assert got["now"] == 6  # the next drag uses it straight away
    assert got["posted"] == [{"speed": 6}]
    assert got["after"][-2] == "Scroll speed | 6 lines per row dragged"
    assert got["after"][-1] == {"picker": ["1", "2", "3", "4", "6*", "8"]}
    assert got["perTick"] == 2  # the answer's desktop lines per tick
    assert got["failed"] == {"speed": 6, "err": "Couldn't save the scroll speed"}


def test_the_more_button_opens_that_sheet():
    js = _js()
    assert (
        'getElementById("more-btn").addEventListener("click", function () { openActSheet(current); });'
        in js
    )
    assert "loadTouchSpeed().then(refreshSpeedHint)" in _whole(js, "openActSheet")
    css = (STATIC / "mobile.css").read_text()
    for sel in (
        ".act-speed ",
        ".act-speed-steps ",
        ".act-speed-step {",
        ".act-speed-step.on ",
    ):
        assert sel in css
