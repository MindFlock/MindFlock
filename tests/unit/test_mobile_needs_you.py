"""The phone page (/m) mirrors THE one Needs-you list.

frontend/src/lib/needsYou.ts is the source of truth (the desktop's bell, the
Sky's Needs you strip and every badge read it); static/mobile.js can't import
TS, so it copies the rule. These tests keep the two in step: the same kinds in
the same order, and the phone's rail and "what needs me" reading it."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
JS = ROOT / "backend" / "web" / "static" / "mobile.js"
TS = ROOT / "frontend" / "src" / "lib" / "needsYou.ts"


def _ts_kinds() -> list[str]:
    ts = TS.read_text()
    body = ts.split("export const NEEDS_YOU_KINDS = [")[1].split("] as const;")[0]
    return re.findall(r'"(\w+)"', body)


def _js_kinds() -> list[str]:
    js = JS.read_text()
    body = js.split("  var NEEDS_YOU_KINDS = [")[1].split("];")[0]
    return re.findall(r'"(\w+)"', body)


def test_same_kinds_same_order():
    ts, js = _ts_kinds(), _js_kinds()
    assert ts, "lib/needsYou.ts lost NEEDS_YOU_KINDS"
    assert js == ts
    for kind in ("asking", "wait_for_me", "halted"):
        assert kind in js


def test_mobile_points_at_the_source_of_truth():
    js = JS.read_text()
    block = js[
        js.index("  // --- Needs you: THE one list") : js.index(
            "  var NEEDS_YOU_KINDS = ["
        )
    ]
    assert "frontend/src/lib/needsYou.ts" in block
    assert "tests/unit/test_mobile_needs_you.py" in block


def test_every_kind_the_phone_returns_is_on_the_list():
    js = JS.read_text()
    fn = js[
        js.index("  function needsYouKind(inst) {") : js.index(
            "  function haltedWords(inst) {"
        )
    ]
    returned = set(re.findall(r'return "(\w+)"', fn)) | set(
        re.findall(r'\? "(\w+)" : "(\w+)"', fn)[0]
    )
    returned.discard("")
    assert returned and returned <= set(_js_kinds())
    # The rail's order and "what needs me" both read it.
    assert "return k ? NEEDS_YOU_RANK[k] : 9;" in js
    # The ranks rise in NEEDS_YOU_KINDS order (ties allowed) and name every kind.
    table = js.split("  var NEEDS_YOU_RANK = {")[1].split("};")[0]
    rank = {k: float(v) for k, v in re.findall(r"(\w+): ([\d.]+)", table)}
    kinds = _js_kinds()
    assert set(rank) == set(kinds)
    assert [rank[k] for k in kinds] == sorted(rank[k] for k in kinds)
    needs = js[
        js.index("  function voiceNeedsMe() {") : js.index("  function voiceStatus() {")
    ]
    assert "needsYouKind(i)" in needs


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_asking_wait_for_me_halted_in_the_desktops_order():
    js = JS.read_text()
    src = js[js.index("  var SESSION_STATES = {") : js.index("  function poll() {")]

    def flight(state, title, actor="robot"):
        return {
            "id": "f_" + title,
            "plan_name": "Ticket to PR",
            "cursor": 1,
            "total": 3,
            "state": state,
            "step": {
                "id": "s2",
                "kind": "checks",
                "title": title,
                "state": "pending",
                "actor": actor,
            },
        }

    rows = [
        {
            "title": "work",
            "activity": "working",
            "flight": flight("running", "Agent works"),
        },
        {
            "title": "halt",
            "activity": "idle",
            "stage": "pushed",
            "flight": flight("halted", "CI checks"),
        },
        {
            "title": "you",
            "activity": "idle",
            "flight": flight("waiting_you", "Try it on staging", "you"),
        },
        {
            "title": "ask",
            "activity": "idle",
            "asking": {"kind": "question", "text": "Redis?", "ts": 1},
        },
        {"title": "ready", "activity": "idle", "stage": "pushed"},
        {"title": "done", "activity": "idle"},
    ]
    script = (
        "var localStorage = { getItem: function () { return null; }, setItem: function () {} };\n"
        + src
        + "\nvar rows = "
        + json.dumps(rows)
        + ";\n"
        "var list = rows.filter(function (i) { return !!needsYouKind(i); })"
        ".sort(function (a, b) { return attnRank(a) - attnRank(b); })"
        ".map(function (i) { return [i.title, needsYouKind(i)]; });\n"
        "console.log(JSON.stringify({ list: list, halted: haltedWords(rows[1]) }));\n"
    )
    res = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30
    )
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout)
    # The same rows, in the same order, as lib/needsYou.ts needsYouRows over
    # the same sessions (frontend/src/__tests__/r21-needsyou.test.ts).
    assert out["list"] == [
        ["ask", "asking"],
        ["you", "wait_for_me"],
        ["halt", "halted"],
        ["ready", "ready"],
    ]
    assert out["halted"] == "Halted at CI checks"


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_a_verify_checker_needs_you_only_for_its_verify():
    """A checker (``hidden_by: "verify"``) is off the rail, the Sky and the
    ask pop-up: only its verify waits on you (confirm / failed / blocked) —
    never its own agent's states (offline, stuck, a dialog, a question),
    which were "needs you" about a window nowhere to be seen. The desktop's
    rule: components/sidebar/ordering.ts ``attentionItems`` with
    dialogs/verifySession.ts ``isVerifySession`` (``hidden_by`` decides; a
    row without it falls back to the ``verify-`` title)."""
    js = JS.read_text()
    src = js[js.index("  var SESSION_STATES = {") : js.index("  function poll() {")]
    ask = {"kind": "question", "text": "Which port?", "ts": 1}
    rows = [
        {"title": "verify-p1", "hidden_by": "verify", "activity": "offline"},
        {
            "title": "verify-p2",
            "hidden_by": "verify",
            "activity": "idle",
            "asking": ask,
        },
        {
            "title": "verify-p3",
            "hidden_by": "verify",
            "activity": "idle",
            "verify": {"state": "failed"},
        },
        {"title": "verify-p4", "activity": "offline"},
        {"title": "verify-mine", "hidden_by": "", "activity": "offline"},
        {"title": "work", "activity": "offline"},
    ]
    script = (
        "var localStorage = { getItem: function () { return null; }, setItem: function () {} };\n"
        + src
        + "\nvar instances = "
        + json.dumps(rows)
        + ";\nvar said = [];\n"
        "function nameOf(t) { return t; }\n"
        "function endSentence(s) { return s + '.'; }\n"
        "function voiceStateLine(n) { return n; }\n"
        "function speakLine(l) { said.push(l); return Promise.resolve(false); }\n"
        "function voiceIdle() {}\n"
        + js[
            js.index("  function voiceNeedsMe(") : js.index(
                "\n  }\n", js.index("  function voiceNeedsMe(")
            )
            + 5
        ]
        + "voiceNeedsMe();\n"
        "console.log(JSON.stringify({ kinds: instances.map(function (i) { return [i.title, needsYouKind(i)]; }),"
        " said: said[0] }));\n"
    )
    res = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30
    )
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout)
    assert out["kinds"] == [
        ["verify-p1", ""],
        ["verify-p2", ""],
        ["verify-p3", "broken"],
        ["verify-p4", ""],
        ["verify-mine", "broken"],
        ["work", "broken"],
    ]
    # "What needs me" reads the same list: the checker's verify, never its agent.
    assert "verify-p3" in out["said"]
    assert "verify-p1" not in out["said"] and "verify-p2" not in out["said"]


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_a_squash_merged_pr_is_not_ready_for_a_pr():
    """``pr_merged`` on the row (the branch is exactly the commit its merged
    PR was at): the stage still reads "pushed", and nothing is ready for a PR
    — the desktop's rule (sidebar/ordering.ts ``attentionItems``)."""
    js = JS.read_text()
    src = js[js.index("  var SESSION_STATES = {") : js.index("  function poll() {")]
    rows = [
        {"title": "ready", "activity": "idle", "stage": "pushed"},
        {"title": "merged", "activity": "idle", "stage": "pushed", "pr_merged": True},
        {"title": "again", "activity": "idle", "stage": "pushed", "pr_merged": False},
    ]
    script = (
        "var localStorage = { getItem: function () { return null; }, setItem: function () {} };\n"
        + src
        + "\nvar rows = "
        + json.dumps(rows)
        + ";\n"
        "console.log(JSON.stringify(rows.map(function (i) { return [i.title, needsYouKind(i)]; })));\n"
    )
    out = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=60
    )
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == [
        ["ready", "ready"],
        ["merged", ""],
        ["again", "ready"],
    ]
