"""The phone page's voice loop, driven in a real (headless) Chrome.

``tests/unit/data/voice_browser/voice_browser.js`` static-serves
``backend/web/static``, mocks every ``/api`` route the page calls, stubs the
browser's speech recognition / synthesis / wake lock / vibration, and walks
the conversation: dictate → countdown → ``/send`` → the "what it is doing"
sentence → the Say: line → the answer; dialogs by voice (yes / always only
when said / a refusal containing "always" / a number on a permission prompt
/ other-only numbering / approval off); switching; the poll fallback; a
repeated Say: line across two turns; the sheet; and the ask sheet (a session
asking: opens by itself, options → /answer, prose → /send, in_dialog, Later,
the stepper, the mic on both kinds, notifications only while hidden, the
title count, the push's deep link); and the fast start (the stream opened
at the tap, no quiet uploaded, the first chunk on the first word, the warm
microphone reused and given back after its grace, the still ring's
three steps, one tick per phrase); the strip's one word (never the words
heard or the line said); the accurate final typed over the phrase; the
wake word's immediate start and its measured latency; and the server's
voice sentence by sentence (the first sound after sentence 1's fetch, one
ahead, back to back, cancel drops the rest). Every scenario prints one
``PASS``/``FAIL`` line; this test runs the script and fails on any ``FAIL``.

Skipped — never failed — where the harness cannot run: no ``node``, no
``playwright-core`` under ``frontend/node_modules`` (``npm ci`` there), or no
Chrome at ``/usr/bin/google-chrome`` (``MF_CHROME`` points elsewhere).
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HARNESS = ROOT / "tests" / "unit" / "data" / "voice_browser" / "voice_browser.js"
PLAYWRIGHT = ROOT / "frontend" / "node_modules" / "playwright-core" / "package.json"


def _chrome() -> str:
    return os.environ.get("MF_CHROME") or "/usr/bin/google-chrome"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
@pytest.mark.skipif(
    not PLAYWRIGHT.exists(), reason="frontend/node_modules has no playwright-core"
)
@pytest.mark.skipif(
    not Path(_chrome()).exists(), reason="no Chrome binary (set MF_CHROME)"
)
def test_voice_loop_in_headless_chrome():
    env = dict(
        os.environ, MF_ROOT=str(ROOT), MF_PORT=str(_free_port()), MF_CHROME=_chrome()
    )
    res = subprocess.run(
        ["node", str(HARNESS)],
        capture_output=True,
        text=True,
        timeout=420,
        env=env,
        cwd=str(ROOT),
    )
    lines = res.stdout.splitlines()
    passed = [ln for ln in lines if ln.startswith("PASS ")]
    failed = [ln for ln in lines if ln.startswith("FAIL ")]
    assert res.returncode == 0, "harness exit %d\n%s\n%s" % (
        res.returncode,
        res.stdout[-3000:],
        res.stderr[-2000:],
    )
    assert not failed, "\n".join(failed)
    assert len(passed) >= 200, "only %d checks ran:\n%s" % (
        len(passed),
        res.stdout[-2000:],
    )
