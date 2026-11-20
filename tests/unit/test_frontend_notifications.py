"""Browser notifications: the served notify addon (/addons/notify.js).

The addon is hand-written and served as-is, so it is checked here. The bell
itself (markup, event curation, styling) is React and is pinned in vitest:
frontend/src/__tests__/notifyWiring.test.ts.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from backend.web import server

client = TestClient(server.app)


def test_the_browser_channel_dedupes_per_RULE_like_the_server_does():
    """Same bug, same fix, in the channel where it also swapped a POPUP.

    The key is reused as the Notification ``tag``, so an event-keyed window did
    not merely swallow the second rule's push — the browser replaced the first
    rule's still-visible popup with it.
    """
    js = client.get("/addons/notify.js").text
    assert 'const key = env.session + "|" + rule.id;' in js
    assert "tag: key" in js or "tag:key" in js


def test_the_two_channels_collapse_a_flap_over_the_same_window():
    """A mirrored constant with a comment saying so is a constant that drifts.

    Both channels dedupe the same (session, rule) pair; if only one of them
    widened, a flapping transition would push twice on the phone and once in
    the browser (or the reverse), which reads as a bug in whichever the user
    happens to be looking at.
    """
    from backend.web.addons import notify as notify_addon

    js = client.get("/addons/notify.js").text
    at = js.index("const DEDUPE_MS")
    ms = int(js[at : js.index("\n", at)].split("=")[1].strip().rstrip(";"))
    assert ms == notify_addon._DEDUPE_SECONDS * 1000


# --- UI sweep round 3: rules that failed to load, focus, and the click -------

_NODE_HARNESS = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
const shown = [];
const opened = [];
let focused = false;
let fetches = 0;
let failFirst = true;
globalThis.window = globalThis;
globalThis.localStorage = { getItem: (k) => (k === "mindflock.notify.enabled" ? "1" : null), setItem() {} };
globalThis.document = { hasFocus: () => focused, dispatchEvent() {} };
globalThis.isSecureContext = true;
globalThis.Notification = class {
  constructor(title, opts) { this.title = title; this.opts = opts; shown.push(this); }
  close() {}
};
Notification.permission = "granted";
globalThis.fetch = async () => {
  fetches += 1;
  if (failFirst) { failFirst = false; throw new Error("booting"); }
  return { ok: true, json: async () => ({ rules: [
    { id: "needs_input", event: "session.activity_changed", new: "clarify", title: "{session} needs you", body: "b" },
  ] }) };
};
globalThis.mindflock = { openSession: (t) => opened.push(t) };
let handler = null;
(0, eval)(src);
const env = (s) => ({ event: "session.activity_changed", new: "clarify", session: s });
const tick = () => new Promise((r) => setTimeout(r, 0));
(async () => {
  window.mindflockAddons.notify.init({
    descriptor: { api_base: "/api/notify" },
    events: { subscribe: (_n, fn) => { handler = fn; } },
  });
  await tick(); await tick();
  const out = { afterBoot: { fetches, shown: shown.length } };
  // The first fetch failed: the next event fetches again and is shown.
  handler(env("alpha"));
  await tick(); await tick();
  out.afterEvent = { fetches, shown: shown.length };
  // Looking at the app: no OS popup.
  focused = true;
  handler(env("beta"));
  await tick();
  out.whileFocused = shown.length;
  focused = false;
  // The click opens the session it is about.
  shown[0].onclick();
  out.opened = opened;
  console.log(JSON.stringify(out));
  process.exit(0);
})();
"""


def test_failed_rules_are_fetched_again_and_a_focused_page_gets_no_popup(tmp_path):
    """start() used to keep [] from a failed GET /api/notify/config (a server
    still booting) and never ask again while the toggle read On; and a popup
    showed beside the in-app toast, its click only focusing the tab."""
    import json
    import shutil
    import subprocess

    import pytest

    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    js = tmp_path / "notify.js"
    js.write_text(client.get("/addons/notify.js").text)
    harness = tmp_path / "harness.js"
    harness.write_text(_NODE_HARNESS)
    run = subprocess.run(
        [node, str(harness), str(js)], capture_output=True, text=True, timeout=30
    )
    assert run.returncode == 0, run.stderr
    out = json.loads(run.stdout.strip().splitlines()[-1])
    assert out["afterBoot"] == {"fetches": 1, "shown": 0}
    assert out["afterEvent"] == {"fetches": 2, "shown": 1}
    assert out["whileFocused"] == 1
    assert out["opened"] == ["alpha"]


def test_rules_retry_on_a_timer_and_a_refresh_never_wipes_them():
    js = client.get("/addons/notify.js").text
    assert "const RULES_RETRY_MS = 30000;" in js
    assert "setTimeout(" in js
    # A failed fetch is null, not [] — refreshRules keeps what it had.
    assert "return null;" in js
    at = js.index("async function refreshRules")
    assert "if (got)" in js[at : at + 300]


def test_the_click_opens_the_session_through_the_apps_bridge():
    js = client.get("/addons/notify.js").text
    assert "window.mindflock.openSession" in js
    assert "document.hasFocus()" in js
