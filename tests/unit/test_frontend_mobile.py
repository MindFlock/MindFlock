"""Mobile touch scrolling + compose box: structural contract checks.

On a phone there are no wheel events, and the agent TUI is an alt-screen app
whose history lives on the tmux side — xterm's local scrollback is empty, so a
touch drag had nothing to scroll and the terminal appeared frozen. The fix
translates a vertical one-finger drag into the same SGR mouse-wheel ticks the
desktop wheel path sends down the PTY (natural direction — content follows the
finger), in BOTH heads: attachWheelScroll in app.js (desktop grid on touch
screens) and mobile.js (the /m view). The /m view also gains a native compose
box (type/paste/autocorrect like any phone text field, Send delivers the line
plus Enter) alongside the raw soft-key bar. Same style as
test_frontend_wave4.py: the live UI is verified with screenshots/CDP; these
pin the JS wiring so it can't silently regress.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from backend.web import server

client = TestClient(server.app)


def test_app_js_touch_scroll_wiring():
    js = client.get("/app.js").text
    # Shared tick sender used by both the wheel and touch paths.
    assert "function sendWheel(" in js
    # All three touch phases are handled on the terminal host.
    for ev in ('"touchstart"', '"touchmove"', '"touchend"'):
        assert ev in js, ev
    # touchmove must be non-passive to be able to preventDefault page panning.
    assert "{ capture: true, passive: false }" in js
    # The browser's own gesture handling is disabled on PTY terminals so the
    # drag reaches our handler instead of panning/zooming the page.
    assert 'host.style.touchAction = "none"' in js


def test_app_js_touch_scroll_semantics():
    js = client.get("/app.js").text
    # Natural scrolling: finger moving down reveals earlier output (wheel up).
    assert "sendWheel(t.clientX, t.clientY, dy > 0" in js
    # Only single-finger drags scroll — pinch and multi-touch are left alone.
    assert "ev.touches.length !== 1" in js
    # A drag only becomes a scroll once clearly vertical, so taps still focus
    # the terminal and horizontal gestures pass through.
    assert "Math.abs(dy) <= Math.abs(dx)" in js
    # Without a live PTY the handler steps aside for xterm's own scrolling.
    assert "ws.readyState !== WebSocket.OPEN) return false" in js


# --------------------------------------------------------------------------- #
# /m mobile view — touch scrollback
# --------------------------------------------------------------------------- #


def test_mobile_js_touch_scroll_wiring():
    js = client.get("/mobile.js").text
    assert "function sendWheelTicks(" in js
    for ev in ('"touchstart"', '"touchmove"', '"touchend"'):
        assert ev in js, ev
    # Same SGR wheel encoding as the desktop path (CSI < 64/65 ; col ; row M).
    assert '"\\x1b[<" + btn + ";" + col + ";" + row + "M"' in js
    # Natural scrolling: finger moving down reveals earlier output (wheel up).
    assert "sendWheelTicks(t.clientX, t.clientY, dy > 0" in js
    # Single-finger vertical drags only; taps and pinches pass through.
    assert "ev.touches.length !== 1" in js
    assert "Math.abs(dy) <= Math.abs(dx)" in js
    # touchmove must be non-passive to be able to preventDefault page panning.
    assert "{ capture: true, passive: false }" in js


def test_mobile_css_term_touch_action():
    css = client.get("/mobile.css").text
    # The browser must not claim vertical drags for page panning.
    assert "touch-action: none" in css


# --------------------------------------------------------------------------- #
# /m mobile view — compose box
# --------------------------------------------------------------------------- #


def test_mobile_html_has_composer_markup():
    html = client.get("/m").text
    assert 'id="composer"' in html
    assert 'id="compose"' in html
    assert 'id="send"' in html
    # The raw soft-key bar stays alongside the composer.
    assert 'id="keys"' in html
    assert 'id="ctrl"' in html
    # Text box ABOVE the key bar (explicit user preference). Both stay
    # visible while typing because applyViewport clamps + polls the app
    # height (see test_mobile_keyboard_viewport_handling) — a keys-above-
    # composer swap was tried and reverted; don't fix visibility by
    # reordering again.
    assert html.index('id="composer"') < html.index('id="keys"')


def test_mobile_css_keys_is_bottommost_bar():
    css = client.get("/mobile.css").text
    # The home-bar safe-area inset belongs to the key bar (the bottommost
    # bar), not the composer.
    composer_rule = css.split("#composer {")[1].split("}")[0]
    keys_rule = css.split("#keys {")[1].split("}")[0]
    assert "safe-area-inset-bottom" in keys_rule
    assert "safe-area-inset-bottom" not in composer_rule


def test_touch_scroll_speed_multiplier():
    # 1:1 finger-to-line felt "far too slow" (user report) — both heads emit
    # 3 ticks per cell dragged, with the per-event cap raised to match.
    for path in ("/app.js", "/mobile.js"):
        js = client.get(path).text
        assert "TOUCH_SCROLL_MULT = 3" in js, path
        assert "Math.min(24, lines)" in js, path


def test_mobile_js_composer_wiring():
    js = client.get("/mobile.js").text
    assert "function sendCompose(" in js
    # The draft is typed, then Enter follows as a SEPARATE keystroke a beat
    # later — a rapid burst reads as a paste to the TUI, and a \r inside a
    # paste becomes a newline in its input box instead of a submit.
    assert "send(text);" in js
    assert 'setTimeout(function () { send("\\r"); }, 150)' in js
    # Enter sends, Shift+Enter keeps editing (multiline drafts).
    assert '"Enter" && !ev.shiftKey' in js
    # Tapping Send must not blur the field (keyboard would dismiss).
    assert 'sendBtn.addEventListener("pointerdown"' in js
    # The draft area grows with pasted content, capped so the terminal survives.
    assert "autosizeCompose" in js


def test_mobile_js_softkeys_compose_aware():
    js = client.get("/mobile.js").text
    # Soft keys must keep working mid-draft: some engines blur the compose box
    # on a button tap despite preventDefault (dismissing the keyboard), so
    # focus is explicitly restored after handling.
    assert "document.activeElement === composeEl" in js
    assert js.count("composeEl.focus()") >= 3  # send, ctrl, other keys
    # ⏎ under an open draft sends it — same as the Send button.
    assert 'composing && key === "enter"' in js


def test_mobile_image_paste():
    # Pasting an image (or any file) can't go down the PTY as keystrokes, and
    # the agent CLI runs on the server so it can't see the phone's clipboard —
    # the bytes are uploaded to /api/paste-image and the returned PATH is what
    # gets pasted (into the compose draft while composing, straight down the
    # PTY otherwise). Capture phase so a file paste wins over xterm's handler.
    js = client.get("/mobile.js").text
    assert "/api/paste-image" in js
    assert 'document.addEventListener("paste"' in js
    assert "uploadPastedFile" in js
    assert "insertIntoCompose" in js


def test_mobile_file_drop():
    # iPadOS/Android split-screen drag-drop uploads through the same endpoint;
    # dragover must preventDefault or the browser navigates to the file.
    js = client.get("/mobile.js").text
    assert 'document.addEventListener("drop"' in js
    assert 'document.addEventListener("dragover"' in js
    assert "dataTransfer" in js


def test_mobile_keyboard_viewport_handling():
    # Focusing the compose box must not leave a blank band between the key
    # bar and the browser chrome: the keyboard should RESIZE the viewport,
    # not pan over the page.
    html = client.get("/m").text
    assert "interactive-widget=resizes-content" in html
    js = client.get("/mobile.js").text
    # If the browser panned toward the input anyway, snap the scroll back and
    # translate #app down by the visual-viewport offset — without that the key
    # bar sits exactly offsetTop px under the keyboard (user report: "can't
    # see the buttons below like esc or control"). A v1 transform compensation
    # was reverted ("disappears until I start writing") because it applied a
    # single mid-keyboard-animation offset from an event and nothing corrected
    # it; the transform is only safe because the settle loop ALSO polls
    # viewportTop() and re-applies until the offset stops moving.
    assert "window.scrollTo(0, 0)" in js
    assert "app.style.transform" in js
    assert "viewportTop()" in js and "appliedTop" in js
    # A single visualViewport reading is stale mid-keyboard-animation, which
    # buried the bottommost bar behind the keyboard. Clamp to innerHeight
    # (authoritative under interactive-widget=resizes-content) and poll until
    # the height stops moving.
    assert "Math.min(vv.height, window.innerHeight)" in js
    assert "setInterval" in js and "appliedH" in js
    # iOS Safari (page mode) reports vv.height WITHOUT subtracting the compact
    # address-bar strip it keeps above the keyboard (?debug=1 overlay measured
    # vvH 458 vs ~400 actually visible, 2026-07-09) — a fixed allowance is
    # reserved while the keyboard is up, folded into visibleHeight() so the
    # settle loop compares like with like. Standalone mode has no strip.
    assert "iosChromeAllowance" in js
    assert "IOS_KB_CHROME_PX" in js


def test_mobile_css_composer_rules():
    css = client.get("/mobile.css").text
    for sel in ("#composer", "#compose", "#send"):
        assert sel in css, sel
    # 16px floor: below that iOS Safari zooms the whole page on focus.
    assert "font-size: 16px" in css


# --------------------------------------------------------------------------- #
# /m mobile view — git workflow action bar (Commit / Push / PR / Merge)
#
# Mirrors the desktop pane header's guided next-step (lib/stage.ts nextStep):
# each button hits the same /api/instances/<title>/{commit,push-branch,make-pr,
# merge-pr} endpoint app.js uses, and the step recommended for the current
# stage is highlighted. As with the rest of this file the live UI is verified
# with screenshots/CDP; these pin the JS/HTML/CSS wiring so it can't regress.
# --------------------------------------------------------------------------- #
def test_mobile_html_has_git_action_bar():
    html = client.get("/m").text
    assert 'id="actions"' in html
    # All four guided steps, each carrying the action its handler dispatches on.
    for act in ("commit", "push", "pr", "merge"):
        assert 'data-act="%s"' % act in html, act
    # The commit-message bottom sheet (commit needs a message before firing).
    assert 'id="commit-sheet"' in html
    assert 'id="commit-text"' in html
    assert 'id="commit-ok"' in html
    assert 'id="commit-cancel"' in html


def test_mobile_css_action_bar_rules():
    css = client.get("/mobile.css").text
    for sel in ("#actions", ".act", ".sheet", "#commit-text"):
        assert sel in css, sel
    # The recommended step is highlighted with the accent (the "is-next" class).
    assert ".act.is-next" in css
    # 16px floor on the commit box too: below that iOS Safari zooms on focus.
    commit_rule = css.split("#commit-text {")[1].split("}")[0]
    assert "font-size: 16px" in commit_rule


def test_mobile_js_git_actions_wiring():
    js = client.get("/mobile.js").text
    # All the handlers exist and each fires the same endpoint app.js uses.
    for fn in (
        "function nextAct(",
        "function updateActions(",
        "function postAction(",
        "function doPush(",
        "function doMakePr(",
        "function doMerge(",
        "function submitCommit(",
        "function openCommitSheet(",
    ):
        assert fn in js, fn
    for ep in ('"push-branch"', '"make-pr"', '"merge-pr"', '"commit"'):
        assert ep in js, ep
    # A poll tick re-derives the highlighted step (the stage can change under us).
    assert "updateActions();" in js


def test_mobile_js_nextact_stage_mapping():
    # nextAct() mirrors lib/stage.ts nextStep: each stage maps to the button to
    # highlight, and busy/blocked states map to no recommendation (null).
    js = client.get("/mobile.js").text
    assert 'inst.status === "loading" || inst.status === "paused") return null' in js
    assert "if (inst.workspace_missing) return null;" in js
    assert 'case "agent": return "commit";' in js
    assert 'case "interrupt": return "commit";' in js  # re-commit after ✗
    # committed only offers Push when there IS an origin to push to.
    assert 'case "committed": return inst.has_origin === false ? null : "push";' in js
    assert 'case "pushed": return "pr";' in js
    assert 'case "pr": return "merge";' in js
    assert "default: return null;" in js  # precommit / provisioning


def test_mobile_js_updateactions_gates_on_instance():
    # No selected instance -> every button disabled; only the recommended action
    # gets the is-next highlight.
    js = client.get("/mobile.js").text
    assert 'btn.classList.toggle("is-next", act === next);' in js
    assert "btn.disabled = !inst;" in js


def test_mobile_js_dopush_soft_gate_override():
    # O3 soft gate: if the server rejects with the "checks haven't passed"
    # message, confirm then retry with force — exactly the desktop pushSession
    # flow. Any other error just flashes status.
    js = client.get("/mobile.js").text
    assert 'err.message === "checks haven\'t passed for this commit"' in js
    assert "doPush(true);" in js
    assert 'flashStatus("push failed: " + err.message)' in js


def test_mobile_js_submit_commit_behavior():
    js = client.get("/mobile.js").text
    # No-op on an empty/whitespace message (trim -> falsy), keeping focus.
    assert "var msg = commitText.value.trim();" in js
    assert "if (!msg) { commitText.focus(); return; }" in js
    # Switch to the shell tab (watch the hooks run) and clear the draft.
    assert 'switchTab("shell");' in js
    assert 'commitText.value = "";' in js
    # POST {message} to the commit endpoint.
    assert 'postAction("commit", { message: msg })' in js


def test_mobile_js_postaction_contract():
    # postAction resolves the parsed JSON on success and rejects with the
    # server's {error} message on a non-ok response (same contract app.js speaks).
    js = client.get("/mobile.js").text
    assert (
        'if (!r.ok) throw new Error((j && j.error) || "request failed (" + r.status + ")");'
        in js
    )
    assert "return j;" in js


# --------------------------------------------------------------------------- #
# /m mobile view — the Diff tab
#
# Reading the diff is what turns the phone from "unblock the agent" into
# "approve the work", so it sits next to Agent/Shell rather than behind a menu.
# Unified only (a split view needs width a phone hasn't got), same endpoint and
# same two baselines as the desktop Diff tab.
# --------------------------------------------------------------------------- #
def test_mobile_html_has_the_diff_tab():
    html = client.get("/m").text
    assert 'data-tab="diff"' in html
    for el in (
        'id="diff-wrap"',
        'id="diff-head"',
        'id="diff-body"',
        'id="diff-base"',
        'id="diff-refresh"',
        'id="diff-stat"',
    ):
        assert el in html, el
    # Hidden until the tab is chosen — the terminal owns that space by default.
    assert 'id="diff-wrap" class="hidden"' in html


def test_mobile_css_diff_rules():
    css = client.get("/mobile.css").text
    for sel in ("#diff-wrap", "#diff-head", "#diff-body", ".diff-btn"):
        assert sel in css, sel
    # Diff lines are not wrapped (a wrapped diff line loses its shape), so the
    # panel scrolls in both directions instead.
    body = css.split("#diff-body {")[1].split("}")[0]
    assert "overflow: auto" in body
    assert "white-space: pre" in css.split("#diff-body .dl {")[1].split("}")[0]
    # The colors carry the same meaning as the desktop diff.
    for rule in ("#diff-body .add", "#diff-body .del", "#diff-body .hunk"):
        assert rule in css, rule
    # The filename stays visible while its hunks scroll past.
    assert "position: sticky" in css.split("#diff-body .file {")[1].split("}")[0]


def test_mobile_js_diff_fetches_the_same_endpoint_as_the_desktop():
    js = client.get("/mobile.js").text
    assert (
        '"/api/instances/" + encodeURIComponent(title) + "/diff?base=" + diffBase' in js
    )
    # Both baselines the desktop offers, persisted under the same key.
    assert 'localStorage.getItem("mf_diffbase") === "head" ? "head" : "fork"' in js
    assert 'localStorage.setItem("mf_diffbase", diffBase)' in js


def test_mobile_js_diff_is_built_without_html():
    """Diff text is repo content — it reaches the DOM as text, never markup."""
    js = client.get("/mobile.js").text
    diff_fns = js.split("function diffNode(")[1].split("function loadDiff(")[0]
    assert "d.textContent = text;" in js
    assert "innerHTML = " not in diff_fns.replace('innerHTML = ""', "")


def test_mobile_js_diff_classifies_lines_like_the_desktop():
    js = client.get("/mobile.js").text
    # Same classification as lib/diff.ts parseUnifiedDiff.
    assert 'var cls = line.indexOf("@@") === 0 ? "hunk"' in js
    assert 'c === "+" ? "add"' in js
    assert 'c === "-" ? "del" : "";' in js
    # The filename is lifted out of the "diff --git" line into a header.
    assert 'if (line.indexOf("diff --git ") === 0)' in js
    assert "DIFF_NOISE.test(line)" in js
    # A phone can't usefully render an unbounded diff.
    assert "shown >= DIFF_MAX_LINES" in js


def test_mobile_js_diff_load_is_race_safe():
    js = client.get("/mobile.js").text
    # A slow load must not overwrite the result of a newer one (switching
    # sessions or baselines mid-flight).
    assert "var seq = ++diffSeq;" in js
    assert "if (seq !== diffSeq) return;" in js


def test_mobile_js_diff_tab_keeps_the_pty_attached():
    js = client.get("/mobile.js").text
    # The Diff panel is a *view*, not another PTY: `tab` (which tmux session the
    # terminal holds) only changes for agent/shell, so coming back from Diff
    # doesn't tear down and rebuild the websocket.
    assert 'var view = "agent";' in js
    assert "if (isDiff) { loadDiff(); return; }" in js
    assert "if (next === tab) { fitSoon(); return; }" in js
    # The compose box and key bar go with the terminal; the git action bar stays.
    assert (
        'document.getElementById("composer").classList.toggle("hidden", isDiff);' in js
    )
    assert 'document.getElementById("keys").classList.toggle("hidden", isDiff);' in js
    # Switching sessions while the Diff tab is up re-reads the diff.
    assert 'if (view === "diff") loadDiff();' in js


def test_mobile_js_status_toast_follows_the_visible_panel():
    """Push/merge feedback has to be readable from the Diff tab — that's where
    the decision to push is made."""
    js = client.get("/mobile.js").text
    assert 'var host = view === "diff" ? diffWrap : termWrap;' in js
    assert "if (statusEl.parentNode !== host) host.appendChild(statusEl);" in js
