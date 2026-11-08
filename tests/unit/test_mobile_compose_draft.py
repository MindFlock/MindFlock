"""The phone's Send button never throws a typed draft away.

With no session yet, the draft is what to work on: it opens the new-session
sheet holding the words. With a session whose terminal isn't connected
(reconnecting, paused) the draft stays in the box. Both used to clear the
box while ``send()`` silently dropped the text on a closed socket.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

JS = (
    Path(__file__).resolve().parents[2] / "backend" / "web" / "static" / "mobile.js"
).read_text()


def _fn(name: str) -> str:
    start = JS.index("  function %s(" % name)
    end = JS.index("\n  }\n", start) + len("\n  }\n")
    return JS[start:end]


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_compose_target_routes_the_draft():
    cases = [(False, False), (False, True), (True, True), (True, False)]
    call = "composeTarget(c[0], c[1])"
    script = _fn("composeTarget") + (
        "console.log(JSON.stringify(%s.map(function (c) { return %s; })));"
        % (json.dumps(cases), call)
    )
    out = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30
    )
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == ["new", "new", "send", "hold"]


def test_send_with_no_session_fills_the_new_session_sheet():
    send = _fn("sendCompose")
    new = send[
        send.index('if (target === "new")') : send.index('if (target === "hold")')
    ]
    assert "openNewSheet();" in new
    # After openNewSheet (which empties the sheet), so the words survive.
    assert new.index("openNewSheet();") < new.index("newTextEl.value = text.trim();")


def test_send_while_disconnected_keeps_the_draft():
    send = _fn("sendCompose")
    hold = send[send.index('if (target === "hold")') :]
    hold = hold[: hold.index("return;") + len("return;")]
    assert 'composeEl.value = ""' not in hold
    assert "your message is still here" in hold
    # The voice routes still come first: they own the draft in voice modes.
    assert send.index("voiceTakesSend()") < send.index("composeTarget(")
