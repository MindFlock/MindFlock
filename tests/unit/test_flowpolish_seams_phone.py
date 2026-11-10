"""Flow polish seams: the phone (/m) lists a route paused at its halt point
as needing the person — "Paused at <stop> — Carry on", under Wait for me —
the way lib/needsYou.ts does (FLIGHT_PAUSED); a route a person paused by
hand stays calm and off the list."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
JS = ROOT / "backend" / "web" / "static" / "mobile.js"
TS = ROOT / "frontend" / "src" / "lib" / "needsYou.ts"


def _flight(state, title, reason=""):
    return {
        "id": "f_" + title,
        "plan_name": "Ticket to PR",
        "cursor": 1,
        "total": 3,
        "state": state,
        "reason": reason,
        "step": {
            "id": "s2",
            "kind": "ship",
            "title": title,
            "state": "pending",
            "actor": "robot",
        },
    }


def test_the_words_match_the_desktop():
    ts = TS.read_text()
    assert 'return "Paused at " + (t || "its halt point") + " — Carry on";' in ts
    js = JS.read_text()
    assert 'return "Paused at " + (at || "its halt point") + " — Carry on";' in js


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_a_halt_point_needs_you_a_hand_pause_does_not():
    js = JS.read_text()
    src = js[js.index("  var SESSION_STATES = {") : js.index("  function poll() {")]
    rows = [
        {
            "title": "point",
            "activity": "idle",
            "flight": _flight("paused", "Push", "halt point"),
        },
        {
            "title": "hand",
            "activity": "idle",
            "flight": _flight("paused", "Merge", "paused"),
        },
        {"title": "halt", "activity": "idle", "flight": _flight("halted", "CI checks")},
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
        "console.log(JSON.stringify({ list: list, words: pausedAtWords(rows[0]),"
        " hint: sessionState(rows[0]).hint, calm: sessionState(rows[1]).inBell }));\n"
    )
    res = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30
    )
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout)
    assert out["list"] == [["point", "wait_for_me"], ["halt", "halted"]]
    assert out["words"] == "Paused at Push — Carry on"
    assert "reached its halt point at Push" in out["hint"]
    assert out["calm"] is False
