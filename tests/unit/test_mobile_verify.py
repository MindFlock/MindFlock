"""The phone page (/m) answers a Verify the way the desktop does.

Verify is the session's own agent (docs/web-ui.md "Verify"): it walks its own
steps and, when some need a person, the row's `verify` waits in `needs_you`
until a person presses Confirm verified or Not verified… (with a note).
static/mobile.js is hand-written (no build step), so — as
test_mobile_needs_you does — these slice the real functions out of it and run
them under node, here over a few-line fake DOM: the buttons a `needs_you`
verify draws, the body each one posts to /api/test-plans/{plan}/confirm, and
the ⋯ sheet's Verify item (sidebar/verifyMenu.ts canStartVerify's rule)."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "backend" / "web" / "static"
JS = STATIC / "mobile.js"
CSS = STATIC / "mobile.css"

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")


def _js() -> str:
    return JS.read_text()


def _between(src: str, start: str, end: str) -> str:
    i = src.index(start)
    return src[i : src.index(end, i)]


# A DOM just big enough for mobile.js's builders: elements with children,
# classes, attributes, listeners and querySelector(".class").
FAKE_DOM = r"""
var localStorage = { getItem: function () { return null; }, setItem: function () {} };
var focused = null;
function El(tag) {
  this.tagName = tag; this.children = []; this.attrs = {}; this.listeners = {};
  this.className = ""; this.textContent = ""; this.value = ""; this.disabled = false;
  this.title = ""; this.type = ""; this.placeholder = ""; this.parentNode = null;
}
El.prototype.appendChild = function (c) { this.children.push(c); c.parentNode = this; return c; };
Object.defineProperty(El.prototype, "innerHTML", {
  get: function () { return ""; }, set: function () { this.children = []; },
});
El.prototype.setAttribute = function (k, v) { this.attrs[k] = String(v); };
El.prototype.getAttribute = function (k) { return k in this.attrs ? this.attrs[k] : null; };
El.prototype.addEventListener = function (t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); };
El.prototype.fire = function (t, ev) {
  ev = ev || {};
  ev.preventDefault = ev.preventDefault || function () {};
  ev.stopPropagation = ev.stopPropagation || function () {};
  (this.listeners[t] || []).forEach(function (f) { f(ev); });
};
El.prototype.click = function () { if (!this.disabled) this.fire("click"); };
El.prototype.focus = function () { focused = this; };
El.prototype.hasClass = function (c) { return (" " + this.className + " ").indexOf(" " + c + " ") >= 0; };
El.prototype.all = function (pred, out) {
  out = out || [];
  this.children.forEach(function (c) { if (pred(c)) out.push(c); c.all(pred, out); });
  return out;
};
El.prototype.querySelectorAll = function (sel) {
  var cls = sel.replace(/^\./, "");
  return this.all(function (c) { return c.hasClass(cls); });
};
El.prototype.querySelector = function (sel) { return this.querySelectorAll(sel)[0] || null; };
El.prototype.classList = null;
var document = { createElement: function (t) { return new El(t); }, activeElement: null };
function buttons(el) {
  return el.all(function (c) { return c.tagName === "button"; });
}
function labels(el) {
  return el.all(function (c) { return c.tagName === "button" || c.tagName === "input"; })
    .map(function (c) { return c.tagName === "input" ? "<input>" : c.textContent; });
}
function press(el, label) {
  var b = buttons(el).filter(function (c) { return c.textContent === label; })[0];
  if (!b) throw new Error("no button " + label + " in " + JSON.stringify(labels(el)));
  b.click();
}
var posts = [], flashed = [], nextStatus = 200, nextBody = { ok: true, plan: "x" };
function fetch(url, opts) {
  posts.push({ url: url, method: opts.method, body: JSON.parse(opts.body) });
  var status = nextStatus, body = nextBody;
  return Promise.resolve({ ok: status < 400, status: status,
                           json: function () { return Promise.resolve(body); } });
}
function flashStatus(m) { flashed.push(m); }
function poll() {}
function settle() { return new Promise(function (r) { setTimeout(r, 0); }); }
"""


def _run(body: str) -> dict:
    js = _js()
    src = (
        _between(js, "  var SESSION_STATES = {", "  function poll() {")
        + _between(
            js,
            "  function apiError(msg, status, body) {",
            "  // The red-zone gate's 409",
        )
        + _between(
            js,
            "  // --- a verify waiting on you (components/behaviors/VerifyConfirm.tsx)",
            "  // The current session's card: under the flight card",
        )
        + _between(
            js, "  function verifyKey(inst) {", "  function updateVerifyCard() {"
        )
    )
    script = (
        FAKE_DOM
        + src
        + "\n(async function () {\n"
        + body
        + "\n})().catch(function (e) {"
    )
    script += " console.error(e && e.stack || e); process.exit(1); });\n"
    res = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30
    )
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


def _verify(state: str, **extra) -> dict:
    v = {
        "state": state,
        "plan": "feat/login",
        "steps": {"total": 4, "agent_done": 2, "human": 2},
        "human_steps": [
            {"id": "h1", "text": "Open /login on a phone", "expect": "The form fits"},
            {"id": "h2", "text": "Sign in with SSO", "expect": ""},
        ],
        "failed_steps": [],
        "detail": "",
        "at": 1731200000,
    }
    v.update(extra)
    return v


# --------------------------------------------------------------------------- #
# Confirm verified / Not verified… on a needs_you verify
# --------------------------------------------------------------------------- #
@needs_node
def test_needs_you_draws_the_steps_and_both_answers_and_confirm_posts_true():
    inst = {"title": "login", "activity": "idle", "verify": _verify("needs_you")}
    out = _run("var inst = %s;\n" % json.dumps(inst) + """
var box = verifyConfirmEl(inst);
var steps = box.all(function (c) { return c.hasClass("vc-step"); }).map(function (li) {
  return li.children.map(function (s) { return s.textContent; });
});
var before = labels(box);
press(box, "Confirm verified");
var during = labels(box);
await settle(); await settle();
console.log(JSON.stringify({ steps: steps, before: before, during: during, after: labels(box),
                             posts: posts, flashed: flashed, card: verifyKey(inst) !== "" }));
""")
    assert out["steps"] == [
        ["Open /login on a phone", "Expect: The form fits"],
        ["Sign in with SSO"],
    ]
    assert out["before"] == ["Confirm verified", "Not verified…"]
    # Busy while the request is out: the pressed button says "…".
    assert out["during"] == ["…", "Not verified…"]
    # A slash in the plan id is encoded, as verifyActions.ts planPath does.
    assert out["posts"] == [
        {
            "url": "/api/test-plans/feat%2Flogin/confirm",
            "method": "POST",
            "body": {"verified": True},
        }
    ]
    assert out["flashed"] == ["Confirmed verified"]
    assert out["card"] is True


@needs_node
def test_not_verified_opens_an_inline_note_that_goes_with_the_answer():
    inst = {"title": "login", "activity": "idle", "verify": _verify("needs_you")}
    out = _run("var inst = %s;\n" % json.dumps(inst) + """
var box = verifyConfirmEl(inst);
press(box, "Not verified…");
var asking = labels(box);
var input = box.querySelector(".vc-note");
var wasFocused = focused === input;
input.value = "  The SSO button is grey  ";
input.fire("input");
press(box, "Not verified");
await settle(); await settle();
// Cancel closes the note without posting; an empty note posts no `note`.
var box2 = verifyConfirmEl({ title: "b", verify: %s });
press(box2, "Not verified…");
press(box2, "Cancel");
var cancelled = labels(box2);
press(box2, "Not verified…");
box2.querySelector(".vc-note").fire("keydown", { key: "Enter" });
await settle(); await settle();
console.log(JSON.stringify({ asking: asking, focused: wasFocused, placeholder: input.placeholder,
                             cancelled: cancelled, posts: posts, flashed: flashed }));
""" % json.dumps(_verify("needs_you", plan="other")))
    assert out["asking"] == ["<input>", "Not verified", "Cancel"]
    assert out["focused"] is True
    assert out["placeholder"] == "What did you see? The agent gets this"
    assert out["cancelled"] == ["Confirm verified", "Not verified…"]
    assert out["posts"] == [
        {
            "url": "/api/test-plans/feat%2Flogin/confirm",
            "method": "POST",
            "body": {"verified": False, "note": "The SSO button is grey"},
        },
        {
            "url": "/api/test-plans/other/confirm",
            "method": "POST",
            "body": {"verified": False},
        },
    ]
    assert out["flashed"] == ["Marked not verified — the agent gets your note"] * 2


@needs_node
def test_a_409_says_it_stopped_waiting_where_it_was_pressed():
    inst = {"title": "login", "activity": "idle", "verify": _verify("needs_you")}
    out = _run("var inst = %s;\n" % json.dumps(inst) + """
nextStatus = 409; nextBody = { error: "this check isn't waiting for a person (passed)" };
var box = verifyConfirmEl(inst);
press(box, "Confirm verified");
await settle(); await settle();
var msg = box.querySelector(".vc-msg").textContent;
nextStatus = 500; nextBody = { error: "boom" };
press(box, "Not verified…");
press(box, "Not verified");
await settle(); await settle();
console.log(JSON.stringify({ msg: msg, msg2: box.querySelector(".vc-msg").textContent,
                             flashed: flashed, after: labels(box) }));
""")
    assert out["msg"].startswith(
        "Couldn't confirm verified — it isn't waiting for you any more"
    )
    assert out["flashed"][0] == out["msg"]
    assert out["msg2"] == "Couldn't mark it not verified: boom"
    # The buttons are back (not stuck busy) so it can be tried again.
    assert "…" not in out["after"]


@needs_node
def test_only_needs_you_draws_the_answers():
    cases = {
        "none": None,
        "verifying": _verify("verifying"),
        "passed": _verify("passed"),
        "failed": _verify("failed"),
        "blocked": _verify("blocked"),
        "noplan": _verify("needs_you", plan=""),
        "needs_you": _verify("needs_you"),
    }
    out = _run("var cases = %s;\n" % json.dumps(cases) + """
var res = {};
for (var k in cases) res[k] = !!verifyConfirmEl({ title: "t", activity: "idle", verify: cases[k] });
console.log(JSON.stringify(res));
""")
    assert out == {k: k == "needs_you" for k in cases}


@needs_node
def test_pills_wear_the_phones_own_verify_tones_and_the_hint_says_confirm_here():
    rows = {
        "verifying": {"activity": "working", "verify": _verify("verifying")},
        "passed": {"activity": "idle", "verify": _verify("passed")},
        "needs_you": {"activity": "idle", "verify": _verify("needs_you")},
        "one": {
            "activity": "idle",
            "verify": _verify(
                "needs_you", human_steps=[{"id": "h", "text": "Look", "expect": ""}]
            ),
        },
    }
    out = _run("var rows = %s;\n" % json.dumps(rows) + """
var res = {};
for (var k in rows) { var s = sessionState(rows[k]); res[k] = [s.label, s.tone, s.inBell, s.hint]; }
console.log(JSON.stringify(res));
""")
    assert out["verifying"][:3] == ["Verifying", "verifying", False]
    assert out["passed"][:3] == ["Verified", "verified", False]
    assert out["needs_you"][:3] == ["Confirm verified", "attention", True]
    hint = out["needs_you"][3]
    assert "desktop" not in hint
    assert hint.startswith("The agent checked what it can. Do these steps, ")
    assert "press Confirm verified (or Not verified…)" in hint
    assert "Open /login on a phone — The form fits" in hint
    assert "Do this step," in out["one"][3]


def test_verify_tones_are_styled():
    css = CSS.read_text()
    for tone in ("verifying", "verified"):
        assert re.search(r"--tone-%s:\s*#[0-9a-f]{6};" % tone, css), tone
        assert ".dot.tone-%s " % tone in css, tone
        assert re.search(
            r"\n\.tone-%s\s+\{ --tone: var\(--tone-%s\); \}" % (tone, tone), css
        ), tone
    # The desktop's violet; Verified calm — no Working glow.
    assert "--tone-verifying: #8b6cf0;" in css
    dot = css.split(".dot.tone-verified ")[1].split("\n")[0]
    assert "box-shadow" not in dot


# --------------------------------------------------------------------------- #
# Where the answers are drawn
# --------------------------------------------------------------------------- #
def test_card_and_sessions_sheet_draw_the_answers():
    js = _js()
    # The current session's card, refreshed with the flight card.
    assert "updateFlightCard();\n        updateVerifyCard();" in js
    assert "updateFlightCard();\n    updateVerifyCard();" in js
    card = _between(js, "  function updateVerifyCard() {", "\n  }\n")
    assert "card.appendChild(verifyConfirmEl(inst));" in card
    # The Sessions sheet (the phone's Needs-you list): under the row.
    rows = _between(js, "  function renderRows() {", "\n  }\n")
    assert "verifyConfirmEl(inst)" in rows
    # A change in the steps redraws them.
    sig = _between(js, "        var sig = instances.map(", '.join("|");')
    assert "verifyKey(i)" in sig


def test_never_window_prompt_and_no_desktop_detour():
    js = _js()
    block = _between(
        js,
        "  // --- a verify waiting on you (components/behaviors/VerifyConfirm.tsx)",
        "  function updateVerifyCard() {",
    )
    assert "prompt(" not in block and "confirm(" not in block and "alert(" not in block
    assert "on the desktop" not in _between(
        js, "  function verifyNeedsYou(v) {", "\n  }\n"
    )


# --------------------------------------------------------------------------- #
# The ⋯ sheet's Verify
# --------------------------------------------------------------------------- #
@needs_node
def test_verify_menu_item_follows_the_desktops_rule_and_posts_verify():
    js = _js()
    src = (
        _between(js, "  var SESSION_STATES = {", "  function poll() {")
        + _between(
            js, "  // --- one session's actions ---", "  // Resume / Restart / Wake"
        )
        + _between(js, "  function startVerify(title) {", '  // "Memory is low"')
    )
    rows = {
        "fresh": {"title": "fresh", "activity": "idle"},
        "passed": {"title": "passed", "activity": "idle", "verify": _verify("passed")},
        "failed": {"title": "failed", "activity": "idle", "verify": _verify("failed")},
        "blocked": {
            "title": "blocked",
            "activity": "idle",
            "verify": _verify("blocked"),
        },
        "verifying": {
            "title": "verifying",
            "activity": "working",
            "verify": _verify("verifying"),
        },
        "needs_you": {
            "title": "needs_you",
            "activity": "idle",
            "verify": _verify("needs_you"),
        },
        "paused": {"title": "paused", "status": "paused", "activity": "offline"},
        "remote": {"title": "mini::remote", "device": "mini", "activity": "idle"},
        "pending": {"title": "pending", "pending": True},
        "missing": {"title": "missing", "workspace_missing": True},
        "peer": {"title": "peer", "peer_share": {"id": "p"}, "activity": "idle"},
    }
    script = (
        FAKE_DOM
        + """
var instances = %s;
var actListEl = new El("div"), actTitleEl = new El("div"), actStateEl = new El("div");
var actErrEl = new El("div"); actErrEl.classList = { toggle: function () {} };
var actSheet = new El("div"), sessionsSheet = new El("div");
var current = "fresh", view = "agent", selected = [], tabs = [], postedFor = [], closed = 0;
function nameOf(t) { return t; }
function isPeerRow() { return false; }
function peerWho() { return ""; } function peerModeWords() { return ""; }
function folderNote(m) { var e = new El("div"); e.textContent = m; return e; }
function openSheet() {} function closeSheet() { closed++; }
function sessionCall() {} function closeSession() {} function askDelete() {} function openRecentSheet() {}
function select(t) { selected.push(t); current = t; }
function switchTab(t) { tabs.push(t); view = t; }
function postFor(title, action, body) { postedFor.push([title, action, body]); return Promise.resolve({ started: true }); }
""" % json.dumps(list(rows.values()))
        + src
        + """
function items() {
  return actListEl.all(function (c) { return c.hasClass("act-label"); }).map(function (c) { return c.textContent; });
}
var res = {}, can = {};
instances.forEach(function (i) { actFor = i.title; renderActSheet(); res[i.title] = items(); can[i.title] = canStartVerify(i); });
(async function () {
  actFor = "failed"; current = "fresh"; view = "shell"; actOpenedAt = 0; renderActSheet();
  var b = actListEl.all(function (c) { return c.tagName === "button"; })
    .filter(function (c) { return c.children[0] && c.children[0].textContent === "Verify again"; })[0];
  b.click();
  await settle();
  console.log(JSON.stringify({ items: res, can: can, posted: postedFor, selected: selected, tabs: tabs, flashed: flashed }));
})();
"""
    )
    res = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30
    )
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout)
    shown = {
        k: [x for x in v if x.startswith("Verify")] for k, v in out["items"].items()
    }
    assert shown == {
        "fresh": ["Verify"],
        "passed": ["Verify again"],
        "failed": ["Verify again"],
        "blocked": ["Verify again"],
        "verifying": [],
        "needs_you": [],
        "paused": [],
        "mini::remote": [],
        "pending": [],
        "missing": [],
        "peer": [],
    }
    assert out["can"] == {
        "fresh": True,
        "passed": True,
        "failed": True,
        "blocked": True,
        "verifying": False,
        "needs_you": False,
        "paused": False,
        "mini::remote": False,
        "pending": False,
        "missing": False,
        "peer": False,
    }
    # Verify sits before Close, as the desktop puts it ahead of the end of
    # the menu.
    fresh = out["items"]["fresh"]
    assert fresh.index("Verify") < fresh.index("Close")
    # The press posts POST /api/instances/{title}/verify with {} and opens the
    # session on its Agent tab.
    assert out["posted"] == [["failed", "verify", {}]]
    assert out["selected"] == ["failed"] and out["tabs"] == ["agent"]
    assert out["flashed"][0].startswith("Verifying failed — its agent writes its steps")
