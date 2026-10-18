"""The phone (/m) sees the plan loop: the rail's zone chip on each session row
(redZoneChip's rules, ported) and, for a plan waiting for Go, a card with the
Map's two buttons over the Map's own routes."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[2] / "backend" / "web" / "static"


def _js() -> str:
    return (STATIC / "mobile.js").read_text()


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_zone_chip_and_rank_follow_the_rail():
    js = _js()
    src = js[js.index("  var SESSION_STATES = {") : js.index("  function poll() {")]

    def row(title="", **rz):
        base = {"zones": 1, "breaches": 0, "last_block_ts": None, "guard": "guarded"}
        return {"title": title, "activity": "idle", "redzone": dict(base, **rz)}

    rows = {
        "plan": row(zones=0, guard="none", plan_pending=3),
        "breach": row(breaches=2, plan_pending=3),
        "off": row(guard="off", plan_pending=3),
        "shield": row(zones=2),
        "green": row(mode="green"),
        "blocked": row("b", last_block_ts=50),
        "none": {"activity": "idle"},
    }
    script = (
        "var STORE = {};\n"
        "var localStorage = { getItem: function (k) { return STORE[k] || null; },"
        " setItem: function (k, v) { STORE[k] = v; } };\n"
        + src
        + "\nvar rows = "
        + json.dumps(rows)
        + ";\nvar out = {};\nfor (var k in rows) { var c = rzChip(rows[k]);"
        " out[k] = [c ? c.label : null, c ? c.cls : null, attnRank(rows[k])]; }\n"
        "markRzSeen(rows.blocked); var c2 = rzChip(rows.blocked); out.seen = c2 && c2.label;\n"
        "console.log(JSON.stringify(out));\n"
    )
    res = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30
    )
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout)
    assert out["plan"] == ["plan · 3", "rz-warn rz-plan", 1]
    assert out["breach"][0] == "⛔2"
    assert out["off"][0] == "!"
    assert out["shield"][0] == "🛡" and out["shield"][2] == 9
    assert out["green"][0] == "✓"
    assert out["blocked"][0] == "⛔"
    # Opening the session on the phone counts as having looked.
    assert out["seen"] == "🛡"
    assert out["none"] == [None, None, 9]


def test_plan_card_uses_the_maps_routes():
    js = _js()
    card = js[
        js.index("  function updatePlanCard() {") : js.index(
            "  // --- touch scrollback"
        )
    ]
    assert '"/code-map/live?since=' in card
    assert (
        'postFor(title, "code-map/go", { zone_ids: zoneIds, scope_to_plan: scopeToPlan })'
        in card
    )
    assert '"Go — only the planned files"' in card
    assert "Plan ready — review & Go" in card
    # Built without HTML strings: the plan's paths and intents are the agent's.
    assert "innerHTML = " not in card.replace('innerHTML = ""', "")
    # Painted every poll and on every switch of session.
    poll = js.split("  function poll() {")[1].split("\n  }\n")[0]
    assert "updatePlanCard();" in poll
    sel = js.split("  function select(title) {")[1].split("\n  }\n")[0]
    assert "updatePlanCard();" in sel
