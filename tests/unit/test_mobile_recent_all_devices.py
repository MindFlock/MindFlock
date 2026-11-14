"""The phone's Recently closed sheet lists every connected device's closed
sessions (`/m`, ``mobile.js`` ``openRecentSheet``).

Owner: "on mobile open recent should show recent sessions for all associated
devices." The sheet reads this server's ``GET /api/recently-closed`` plus each
connected device's through ``/api/devices/<device>/fwd/…`` (the relay's
allow-list carries the list and the one-id reopen — ``test_remote_devices``),
merges them newest first, names the device in each other device's row, and
reopens a row where it was closed. A device that fails or is slow costs its own
rows and one quiet line, never the sheet; a single-device phone sees the sheet
it always did.

The sheet's functions run as written under node against a small DOM/fetch stub.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

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


# A DOM just big enough for the sheet: elements keep their children and text;
# fetch answers from ANSWERS (a value, an HTTP status, or "hang").
_HARNESS = r"""
function El(tag) { this.tag = tag; this.children = []; this.textContent = ""; this.className = "";
  this.disabled = false; this.listeners = {};
  var cls = {};
  this.classList = { toggle: function (c, on) { cls[c] = !!on; }, contains: function (c) { return !!cls[c]; } };
}
El.prototype.appendChild = function (c) { this.children.push(c); return c; };
El.prototype.addEventListener = function (ev, fn) { this.listeners[ev] = fn; };
Object.defineProperty(El.prototype, "innerHTML", { set: function () { this.children = []; } });
var document = { createElement: function (t) { return new El(t); } };
var recentListEl = new El("div"), recentErrEl = new El("div");
var sessionsSheet = {}, recentSheet = {};
function openSheet() {} function closeSheet() {}
var flashed = [], polls = 0, fetched = [];
function flashStatus(m) { flashed.push(m); } function poll() { polls++; }
var pendingNew = "", pendingNewUntil = 0, PENDING_NEW_MS = 1000;
var newDevices = [], selfHost = "";
function renderDevices() {}
function fetch(url, opts) {
  fetched.push([(opts && opts.method) || "GET", url]);
  var a = ANSWERS[url];
  if (typeof a === "function") a = a();
  if (a && a.delay) {
    var v = a.value;
    return new Promise(function (r) { setTimeout(r, a.delay); }).then(function () {
      return { ok: true, status: 200, json: function () { return Promise.resolve(v); } };
    });
  }
  if (a === "hang") return new Promise(function () {});
  if (typeof a === "number") return Promise.resolve({ ok: false, status: a, json: function () { return Promise.resolve({}); } });
  if (a === undefined) return Promise.reject(new Error("no route " + url));
  return Promise.resolve({ ok: true, status: 200, json: function () { return Promise.resolve(a); } });
}
function texts(el) { return el.children.map(function (c) {
  if (c.className === "recent-row") {
    var t = c.children[0];
    return { name: t.children[0].textContent, sub: t.children[1].textContent,
             btn: c.children[1] && c.children[1].textContent };
  }
  return c.textContent;
}); }
function settle() { return new Promise(function (r) { setTimeout(r, 200); }); }
"""


def _sheet(js: str) -> str:
    return (
        _HARNESS
        + _var(js, "recentSeq")
        + "var RECENT_DEVICE_MS = 30;\n"
        + "".join(
            _whole(js, n)
            for n in (
                "devicePath",
                "deviceTitle",
                "deviceLabel",
                "loadDevices",
                "folderNote",
                "closedMs",
                "agoWord",
                "recentError",
                "recentFetch",
                "openRecentSheet",
                "renderRecent",
                "recentRow",
                "reopenClosed",
            )
        )
    )


def _run(script: str):
    res = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30
    )
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout.strip().splitlines()[-1])


_NOW = "var H = 3600 * 1000, now = Date.now();\n"


@needs_node
def test_every_connected_device_is_merged_newest_first_and_labelled():
    script = _sheet(_js()) + _NOW + r"""
var ANSWERS = {
  "/api/devices": { self: { host: "tower" }, devices: [
    { device: "mac-mini", host: "Mac-mini", connected: true },
    { device: "laptop", host: "laptop", connected: false } ] },
  "/api/recently-closed": [
    { id: "a-1", title: "a", branch: "b-a", closed_at: (now - 3 * H) / 1000 },
    { id: "c-1", title: "c", branch: "b-c", closed_at: now - 1 * H } ],
  "/api/devices/mac-mini/fwd/api/recently-closed": [
    { id: "m-1", title: "m", branch: "b-m", closed_at: new Date(now - 2 * H).toISOString() } ],
};
openRecentSheet();
settle().then(function () {
  console.log(JSON.stringify({ rows: texts(recentListEl), fetched: fetched,
                               err: recentErrEl.textContent }));
});
"""
    out = _run(script)
    assert out["rows"] == [
        {"name": "c", "sub": "b-c  ·  1h ago", "btn": "Reopen"},
        {"name": "m", "sub": "b-m  ·  Mac-mini  ·  2h ago", "btn": "Reopen"},
        {"name": "a", "sub": "b-a  ·  3h ago", "btn": "Reopen"},
    ]
    # A device that isn't connected is never asked.
    assert ["GET", "/api/devices/laptop/fwd/api/recently-closed"] not in out["fetched"]
    assert out["err"] == ""


@needs_node
def test_a_failing_or_slow_device_costs_only_its_rows_and_a_quiet_line():
    script = _sheet(_js()) + _NOW + r"""
var ANSWERS = {
  "/api/devices": { self: { host: "tower" }, devices: [
    { device: "mac-mini", host: "Mac-mini", connected: true },
    { device: "pi", host: "pi", connected: true } ] },
  "/api/recently-closed": [ { id: "a-1", title: "a", branch: "", closed_at: now - H } ],
  "/api/devices/mac-mini/fwd/api/recently-closed": 502,
  "/api/devices/pi/fwd/api/recently-closed": "hang",
};
openRecentSheet();
settle().then(function () {
  console.log(JSON.stringify({ rows: texts(recentListEl), err: recentErrEl.textContent }));
});
"""
    out = _run(script)
    assert out["rows"] == [
        {"name": "a", "sub": "1h ago", "btn": "Reopen"},
        "couldn't reach Mac-mini, pi",
    ]
    assert out["err"] == ""


@needs_node
def test_a_single_device_phone_sees_todays_sheet():
    script = _sheet(_js()) + _NOW + r"""
var ANSWERS = {
  "/api/devices": { self: { host: "tower" }, devices: [] },
  "/api/recently-closed": [ { id: "a-1", title: "a", branch: "main", closed_at: now - H } ],
};
var out = {};
openRecentSheet();
settle().then(function () {
  out.rows = texts(recentListEl);
  out.fetched = fetched.slice();
  // This server failing: the sheet's own error, as before.
  ANSWERS["/api/recently-closed"] = 500;
  openRecentSheet();
  return settle();
}).then(function () {
  out.failRows = texts(recentListEl);
  out.err = recentErrEl.textContent;
  // No device list at all (an older server): still this server's sheet.
  delete ANSWERS["/api/devices"];
  ANSWERS["/api/recently-closed"] = [];
  openRecentSheet();
  return settle();
}).then(function () {
  out.emptyRows = texts(recentListEl);
  console.log(JSON.stringify(out));
});
"""
    out = _run(script)
    # No device name in the sub line when there is only this device.
    assert out["rows"] == [{"name": "a", "sub": "main  ·  1h ago", "btn": "Reopen"}]
    assert out["fetched"] == [["GET", "/api/devices"], ["GET", "/api/recently-closed"]]
    assert out["failRows"] == []
    assert out["err"] == "couldn't read the list (500)"
    assert out["emptyRows"] == ["Nothing closed recently."]


@needs_node
def test_a_stale_answer_never_paints_the_reopened_sheet():
    script = _sheet(_js()) + _NOW + r"""
var ANSWERS = {
  "/api/devices": { self: { host: "tower" }, devices: [
    { device: "mac-mini", host: "Mac-mini", connected: true } ] },
  "/api/devices/mac-mini/fwd/api/recently-closed": [],
};
var calls = 0;
// The first opening's list lands AFTER the second opening's.
ANSWERS["/api/recently-closed"] = function () {
  calls += 1;
  return calls === 1
    ? { delay: 80, value: [ { id: "a-1", title: "old", closed_at: now - H } ] }
    : [ { id: "b-1", title: "new", closed_at: now - H } ];
};
openRecentSheet();                       // first opening: its list is on the way
new Promise(function (r) { setTimeout(r, 20); }).then(function () {
  openRecentSheet();                     // reopened before it lands
  return settle();
}).then(function () {
  console.log(JSON.stringify({ rows: texts(recentListEl) }));
});
"""
    out = _run(script)
    assert [r["name"] for r in out["rows"]] == ["new"]


@needs_node
def test_reopen_goes_to_the_device_it_was_closed_on_and_claims_its_title():
    script = _sheet(_js()) + _NOW + r"""
var ANSWERS = {
  "/api/devices": { self: { host: "tower" }, devices: [
    { device: "mac-mini", host: "Mac-mini", connected: true } ] },
  "/api/recently-closed": [ { id: "here-1", title: "here", closed_at: now - 2 * H } ],
  "/api/devices/mac-mini/fwd/api/recently-closed": [
    { id: "fix login-17", title: "fix login", closed_at: now - H } ],
  "/api/devices/mac-mini/fwd/api/recently-closed/fix%20login-17/reopen": { title: "fix login" },
  "/api/recently-closed/here-1/reopen": { title: "here" },
};
var out = {};
openRecentSheet();
settle().then(function () {
  recentListEl.children[0].children[1].listeners.click();   // the Mac mini's row
  return settle();
}).then(function () {
  out.remote = { post: fetched[fetched.length - 1], pending: pendingNew, flash: flashed[0] };
  recentListEl.children[1].children[1].listeners.click();   // this device's row
  return settle();
}).then(function () {
  out.local = { post: fetched[fetched.length - 1], pending: pendingNew };
  console.log(JSON.stringify(out));
});
"""
    out = _run(script)
    assert out["remote"]["post"] == [
        "POST",
        "/api/devices/mac-mini/fwd/api/recently-closed/fix%20login-17/reopen",
    ]
    # Selected once GET /api/instances lists it, namespaced like a remote create.
    assert out["remote"]["pending"] == "mac-mini::fix login"
    assert out["remote"]["flash"] == "Reopened fix login on Mac-mini"
    assert out["local"] == {
        "post": ["POST", "/api/recently-closed/here-1/reopen"],
        "pending": "here",
    }


def test_the_sheet_only_forwards_routes_the_relay_allows():
    """Every path the sheet sends through devicePath must pass the gateway, or
    another device's rows turn into "couldn't reach"."""
    from backend.web.core import remote

    assert remote._fwd_target("GET", "/api/recently-closed") is not None
    assert remote._fwd_target("POST", "/api/recently-closed/x-1/reopen") is not None
    js = _js()
    assert 'fetch(devicePath(device, "/api/recently-closed"))' in js
    reopen = _whole(js, "reopenClosed")
    assert (
        'devicePath(device, "/api/recently-closed/" + encodeURIComponent(ent.id)'
        ' + "/reopen")' in reopen
    )
    assert "pendingNew = deviceTitle(device," in reopen
    # Forgetting a closed session is never offered for another device's row.
    assert "/forget" not in _whole(js, "recentRow")


def test_the_recent_sheet_is_still_es5_shaped():
    js = _js()
    section = js.split("  // --- recently closed ---")[1].split(
        '  document.getElementById("more-btn")'
    )[0]
    for banned in ("=>", "const ", "let ", "async ", "await ", "..."):
        assert banned not in section, banned
