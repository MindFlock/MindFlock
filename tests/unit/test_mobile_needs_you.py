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
