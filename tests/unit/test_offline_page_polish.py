"""electron/offline.html, the desktop app's "can't reach the server" page:
the logs shortcut reads as the Mac form on a Mac, and Restart WSL's armed
question stands down by itself and says how the restart went.

The page's inline script is run under node with a tiny fake DOM (no Electron,
no display), so these check what the button actually says over time."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_HTML = (_ROOT / "electron" / "offline.html").read_text(encoding="utf-8")

# A fake DOM just big enough for the page's script: elements by id (created on
# demand), querySelectorAll by class, click handlers, and a manual clock.
_HARNESS = r"""
const vm = require('node:vm')
const fs = require('node:fs')
const cfg = JSON.parse(process.argv[process.argv.length - 1])
const html = fs.readFileSync(cfg.path, 'utf8')
const script = /<script>([\s\S]*?)<\/script>/.exec(html)[1]
const els = {}
function el(id) {
  if (!els[id]) els[id] = { id, textContent: '', hidden: false, disabled: false,
    scrollTop: 0, clientHeight: 0, scrollHeight: 0, handlers: {},
    addEventListener(ev, fn) { this.handlers[ev] = fn } }
  return els[id]
}
el('restart-wsl').textContent = 'Restart WSL'
const logkeys = []
const m = html.match(/class="logkey"/g) || []
for (let i = 0; i < m.length; i++) logkeys.push({ textContent: 'Ctrl+Shift+L' })
let now = 0
const timers = []
function setTimeout(fn, ms) { const t = { at: now + ms, fn }; timers.push(t); return t }
function clearTimeout(t) { const i = timers.indexOf(t); if (i >= 0) timers.splice(i, 1) }
function advance(ms) {
  const until = now + ms
  for (;;) {
    timers.sort((a, b) => a.at - b.at)
    const t = timers[0]
    if (!t || t.at > until) break
    timers.shift(); now = t.at; t.fn()
  }
  now = until
}
const restartResult = cfg.restart
const window = {
  mfshell: { platform: cfg.platform },
  mfdiag: {
    get: () => new Promise(() => {}),
    installState: () => new Promise(() => {}),
    restartWsl: () => restartResult === 'throw' ? Promise.reject(new Error('x')) : Promise.resolve(restartResult),
  },
}
const ctx = {
  window, navigator: { platform: cfg.navPlatform || '' }, setTimeout, clearTimeout,
  setInterval: () => 0, Promise, Date: { now: () => now },
  document: {
    getElementById: el,
    querySelectorAll: (sel) => sel === '.logkey' ? logkeys : [],
  },
}
vm.runInNewContext(script, ctx)
const btn = el('restart-wsl')
const out = { keys: logkeys.map((k) => k.textContent) }
const click = () => btn.handlers.click()
const flush = () => new Promise((r) => setImmediate(r))
;(async () => {
  for (const step of cfg.steps) {
    if (step === 'click') click()
    else if (step === 'flush') await flush()
    else if (typeof step === 'number') advance(step)
    else if (step.startsWith('note:')) out[step.slice(5)] = { text: btn.textContent, disabled: btn.disabled }
  }
  process.stdout.write(JSON.stringify(out))
})()
"""


def _run(
    platform: str = "win32",
    restart: object = {"ok": True},
    steps: list | None = None,
    nav: str = "",
) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    cfg = {
        "path": str(_ROOT / "electron" / "offline.html"),
        "platform": platform,
        "navPlatform": nav,
        "restart": restart,
        "steps": steps or [],
    }
    r = subprocess.run(
        [node, "-e", _HARNESS, json.dumps(cfg)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_every_logs_shortcut_is_marked_for_the_platform_swap():
    keys = re.findall(r"<code[^>]*>(?:Ctrl|Cmd)\+Shift\+L</code>", _HTML)
    assert len(keys) >= 3
    assert all('class="logkey"' in k for k in keys)


def test_a_mac_reads_cmd_and_windows_reads_ctrl():
    assert set(_run(platform="darwin")["keys"]) == {"Cmd+Shift+L"}
    assert set(_run(platform="win32")["keys"]) == {"Ctrl+Shift+L"}
    # No shell bridge (a plain browser): navigator decides.
    assert set(_run(platform="", nav="MacIntel")["keys"]) == {"Cmd+Shift+L"}


def test_the_armed_restart_question_stands_down_after_a_few_seconds():
    out = _run(steps=["click", "note:armed", 4900, "note:still", 200, "note:disarmed"])
    assert out["armed"]["text"].startswith("Restart WSL now?")
    assert out["still"]["text"].startswith("Restart WSL now?")
    assert out["disarmed"]["text"] == "Restart WSL"
    assert out["disarmed"]["disabled"] is False


def test_a_click_after_it_stood_down_asks_again_rather_than_restarting():
    out = _run(steps=["click", 6000, "click", "note:asked"])
    assert out["asked"]["text"].startswith("Restart WSL now?")


def test_a_restart_says_restarted_then_reads_restart_wsl_again():
    out = _run(
        restart={"ok": True},
        steps=[
            "click",
            "click",
            "note:busy",
            "flush",
            "note:result",
            3000,
            "note:after",
        ],
    )
    assert out["busy"] == {"text": "Restarting WSL…", "disabled": True}
    assert out["result"]["text"] == "Restarted"
    assert out["after"] == {"text": "Restart WSL", "disabled": False}


@pytest.mark.parametrize("restart", [{"ok": False}, "throw", None])
def test_a_restart_that_failed_says_so(restart):
    out = _run(
        restart=restart,
        steps=["click", "click", "flush", "note:result", 3000, "note:after"],
    )
    assert out["result"]["text"] == "Didn’t work"
    assert out["after"]["text"] == "Restart WSL"


def test_the_disarm_timer_does_not_fire_into_a_running_restart():
    # Confirmed at 1 s; the 5 s disarm must not relabel the button mid-restart.
    out = _run(restart={"ok": True}, steps=["click", 1000, "click", 4500, "note:mid"])
    assert out["mid"]["text"] == "Restarting WSL…"
