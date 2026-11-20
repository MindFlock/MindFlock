"""The phone page (/m) when the server is slow, down or answers an error.

- A failed /api/instances poll (a network error, a 401, a 5xx, any body that
  is not a list) keeps the last session list: it used to become [] and close
  the open terminal to show "No sessions yet".
- Until the first answer the header says "Loading sessions…"; two failed polls
  in a row put "Couldn't reach MindFlock. Is it still running?" where the
  status line is, and the next good poll clears it.
- An action whose fetch got no answer says the same words, not the browser's
  "Load failed" / "Failed to fetch".
- Push / PR / Merge / Commit errors stay up (setStatus) instead of flashing for
  2.5 s; the action bar is off while one request runs; a failed commit keeps
  its message in the box.

The functions run as written under node against small stubs.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "backend" / "web" / "static"
UNREACHABLE = "Couldn't reach MindFlock. Is it still running?"

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")


def _js() -> str:
    return (STATIC / "mobile.js").read_text()


def _whole(js: str, name: str) -> str:
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


_STUBS = r"""
var statusEl = { textContent: "" };
var statuses = [], flashed = [];
function setStatus(m) { statuses.push(m); statusEl.textContent = m; }
function flashStatus(m) { flashed.push(m); statusEl.textContent = m; }
var rendered = 0;
function renderPicker() { rendered++; }
function voiceDialogRows() {} function rzChip() { return null; } function attnRank() { return 0; }
function sessionState() { return { label: "" }; } function verifyKey() { return ""; }
function claimPending() {} function pollApprovals() {} function attachWhenReady() {}
function updateDot() {} function updatePlanCard() {} function updateFlightCard() {}
function updateVerifyCard() {} function voiceOnPoll() {} function askOnPoll() {}
var bar = 0;
function updateActions() { bar++; }
var ANSWER;
var fetched = [];
function fetch(url, opts) {
  fetched.push(url);
  var a = typeof ANSWER === "function" ? ANSWER() : ANSWER;
  if (a === "down") return Promise.reject(new TypeError("Load failed"));
  if (a === "hang") return new Promise(function () {});
  if (typeof a === "number") return Promise.resolve({ ok: false, status: a, json: function () { return Promise.resolve({ error: "boom " + a }); } });
  return Promise.resolve({ ok: true, status: 200, json: function () { return Promise.resolve(a); } });
}
function settle() { return new Promise(function (r) { setTimeout(r, 20); }); }
"""


def _poll_script(js: str) -> str:
    return (
        _STUBS
        + "var instances = [];\n"
        + _var(js, "listLoaded")
        + _var(js, "pollFails")
        + _var(js, "UNREACHABLE")
        + "".join(_whole(js, n) for n in ("pollFailed", "pollAnswered", "poll"))
    )


@needs_node
def test_a_failed_poll_keeps_the_list_and_two_say_unreachable():
    script = _poll_script(_js()) + r"""
var out = {};
ANSWER = [{ title: "a" }, { title: "b" }];
out.before = listLoaded;
poll().then(function () {
  out.first = { n: instances.length, loaded: listLoaded, rendered: rendered };
  ANSWER = 500;
  return poll();
}).then(function () {
  out.after500 = { n: instances.length, fails: pollFails, statuses: statuses.slice() };
  ANSWER = { error: "unauthorized" };
  return poll();
}).then(function () {
  out.afterBody = { n: instances.length, fails: pollFails, status: statusEl.textContent };
  ANSWER = "down";
  return poll();
}).then(function () {
  out.afterDown = { n: instances.length, fails: pollFails, status: statusEl.textContent };
  ANSWER = [{ title: "a" }];
  return poll();
}).then(function () {
  out.back = { n: instances.length, fails: pollFails, status: statusEl.textContent };
  console.log(JSON.stringify(out));
});
"""
    out = _run(script)
    assert out["before"] is False
    assert out["first"] == {"n": 2, "loaded": True, "rendered": 1}
    # One failure: the list stays, nothing said yet.
    assert out["after500"] == {"n": 2, "fails": 1, "statuses": []}
    # A non-list body is a failure too; the second in a row says so.
    assert out["afterBody"] == {"n": 2, "fails": 2, "status": UNREACHABLE}
    assert out["afterDown"] == {"n": 2, "fails": 3, "status": UNREACHABLE}
    # The next good poll clears the line and takes the new list.
    assert out["back"] == {"n": 1, "fails": 0, "status": ""}


@needs_node
def test_a_good_poll_leaves_someone_elses_status_line_alone():
    script = _poll_script(_js()) + r"""
ANSWER = 503;
poll().then(poll).then(function () {
  setStatus("push failed: rejected");
  ANSWER = [];
  return poll();
}).then(function () {
  console.log(JSON.stringify({ status: statusEl.textContent, n: instances.length }));
});
"""
    out = _run(script)
    assert out == {"status": "push failed: rejected", "n": 0}


def test_header_says_loading_until_the_first_answer():
    html = (STATIC / "mobile.html").read_text()
    assert '<span id="picker-title">Loading sessions…</span>' in html
    dot = _whole(_js(), "updateDot")
    assert 'listLoaded ? "no sessions" : "Loading sessions…"' in dot


def test_the_socket_says_unreachable_while_the_poll_does():
    js = _js()
    assert 'setStatus(pollFails >= 2 ? UNREACHABLE : "reconnecting…");' in js
    assert 'setStatus(pollFails >= 2 ? UNREACHABLE : "connection error");' in js


def _actions_script(js: str) -> str:
    return (
        _STUBS
        + "var current = 's1', gitBusy = false, commitText = { value: '' };\n"
        + "var PR_REMEDY = 'remedy';\n"
        + _var(js, "UNREACHABLE")
        + "function poll() {}\n"
        + "function redZoneBreaches() { return null; }\n"
        + "function nameOf(t) { return t; }\n"
        + "function askSheet() {}\n"
        + "function switchTab() {}\n"
        + "function closeCommitSheet() {}\n"
        + "function postAction(a, b) { return postFor(current, a, b); }\n"
        + "".join(
            _whole(js, n)
            for n in (
                "postFor",
                "unreachableError",
                "errText",
                "apiError",
                "gitInFlight",
                "doPush",
                "doMakePr",
                "submitCommit",
                "doCommit",
            )
        )
    )


@needs_node
def test_a_network_failure_says_unreachable_not_the_browser_words():
    script = _actions_script(_js()) + r"""
ANSWER = "down";
var out = {};
postFor("s1", "push-branch", {}).catch(function (e) {
  out.msg = e.message; out.status = e.status;
  ANSWER = 409;
  return postFor("s1", "push-branch", {});
}).catch(function (e) {
  out.server = e.message;
  out.typeErr = errText(new TypeError("Failed to fetch"), "x");
  out.other = errText(new Error("nope"), "x");
  out.none = errText(null, "fallback");
  console.log(JSON.stringify(out));
});
"""
    out = _run(script)
    assert out == {
        "msg": UNREACHABLE,
        "status": 0,
        "server": "boom 409",
        "typeErr": UNREACHABLE,
        "other": "nope",
        "none": "fallback",
    }


@needs_node
def test_push_is_one_request_at_a_time_and_its_error_stays_up():
    script = _actions_script(_js()) + r"""
ANSWER = function () {
  return "hang";
};
doPush(false, "s1");
var busyDuring = gitBusy;
doPush(false, "s1");          // the second tap while the first runs
doMakePr("s1");
var during = fetched.length;
ANSWER = 500;
gitBusy = false;               // let the next one through; it fails
doPush(false, "s1");
settle().then(function () {
  console.log(JSON.stringify({
    busyDuring: busyDuring, during: during, busyAfter: gitBusy,
    statuses: statuses, flashed: flashed,
  }));
});
"""
    out = _run(script)
    assert out["busyDuring"] is True
    assert out["during"] == 1
    assert out["busyAfter"] is False
    # Progress flashes; the failure stays until the next action.
    assert "pushing…" in out["flashed"]
    assert out["statuses"][-1] == "push failed: boom 500"


@needs_node
def test_a_failed_commit_keeps_its_message():
    script = _actions_script(_js()) + r"""
var out = {};
commitText.value = "fix the thing";
ANSWER = 500;
submitCommit();
settle().then(function () {
  out.failed = { box: commitText.value, status: statusEl.textContent };
  ANSWER = { ok: true };
  submitCommit();
  return settle();
}).then(function () {
  out.ok = { box: commitText.value, flashed: flashed[flashed.length - 1] };
  console.log(JSON.stringify(out));
});
"""
    out = _run(script)
    assert out["failed"] == {
        "box": "fix the thing",
        "status": "commit failed: boom 500",
    }
    assert out["ok"] == {"box": "", "flashed": "commit started — watch the shell"}


def test_the_action_bar_is_off_while_a_request_runs():
    js = _js()
    assert "btn.disabled = !inst || gitBusy;" in _whole(js, "updateActions")
    assert "if (b.disabled || gitBusy || !current) return;" in js
