"""Frontend wave 5 (round-4 findings L5–L9): structural contract checks.

Same style as test_frontend_wave4.py — the live UI is verified with
screenshots/CDP; these pin the markup hooks, JS wiring, and CSS rules so they
can't silently regress.

- L5: Ctrl+K no longer steals readline kill-line from a focused terminal
      (Cmd+K stays global).
- L6: backlog replay detection (mindflock.events.isReplay) + toast subscribers
      skipping replayed envelopes.
- L7: missing-workspace row/pane treatment with a single Clean up action.
- L8: no-origin push guidance (hint button + copyable command + error toasts).
- L9: light-theme stage-chip contrast on focused/active rows.

The last section is newer: it pins the "gh is optional" contract into the
shipped bundle after a contributor with an SSH remote hit a blocking modal
about a CLI she had no reason to install.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from backend.web import server

client = TestClient(server.app)


# --------------------------------------------------------------------------- #
# L5 → keymap era — palette on Ctrl+P / Ctrl+Shift+P; Ctrl+K is the VSCode
# chord prefix for the outward-facing git verbs (push/PR/IDE/duplicate/hide).
# --------------------------------------------------------------------------- #


def test_app_js_keymap_palette_and_ctrl_k_chords():
    js = client.get("/app.js").text
    # One table-driven keymap dispatches every global shortcut (capture-phase,
    # Ctrl == Cmd via the mod alias), and the palette rides it on "p" with
    # shift "any" so both Ctrl+P and VSCode's Ctrl+Shift+P work.
    assert "KEYMAP" in js
    assert "e.ctrlKey || e.metaKey" in js
    assert 'id: "palette"' in js
    assert "togglePalette" in js
    # Ctrl+K is the chord prefix (like VSCode's terminal allowChords): git
    # verbs need two deliberate keystrokes, and the AltGr-hazardous
    # Ctrl+Alt single-stroke family is gone.
    assert "CHORDS" in js and "_enterChord" in js
    assert "_SESSION_HOTKEYS" not in js
    assert '"kbd">Ctrl+Alt' not in js


def test_index_tooltip_documents_the_palette_shortcut():
    html = client.get("/").text
    # Footer button advertises the always-global Ctrl+P / Cmd+P binding.
    assert "Command palette — Ctrl+P / ⌘P" in client.get("/app.js").text


def test_web_ui_docs_describe_the_chord_tradeoff():
    text = open("docs/web-ui.md", encoding="utf-8").read()
    # The chord bindings, the readline kill-line cost, and the AltGr rationale
    # for retiring Ctrl+Alt are all documented.
    assert "Ctrl+K P" in text
    assert "kill-line" in text
    assert "AltGr" in text


# --------------------------------------------------------------------------- #
# L6 — backlog replay must not re-fire one-shot toasts on every page load.
# --------------------------------------------------------------------------- #


def test_events_js_exposes_is_replay():
    js = client.get("/core/events.js").text
    # Connect epoch: client clock at open, overridden by the server's hello
    # frame (feature-detected via a numeric server_time field).
    assert "_connectEpoch" in js
    assert "server_time" in js
    assert 'env.event === "hello"' in js
    assert "REPLAY_SKEW_S" in js
    # Public API: mindflock.events.isReplay(envelope).
    assert "isReplay: isReplay" in js
    assert "isReplay(envelope)" in js  # documented in the contract comment


def test_events_js_epoch_set_per_connection():
    js = client.get("/core/events.js").text
    # The fallback epoch is (re)stamped in onopen so reconnects re-classify.
    open_block = js[js.index("ws.onopen") : js.index("ws.onmessage")]
    assert "Date.now() / 1000" in open_block


def test_app_js_toast_subscribers_skip_replays():
    js = client.get("/app.js").text
    # Feature-detected accessor (older events.js can't break dispatch).
    assert 'typeof ev.isReplay === "function"' in js
    # Clarify toast/badge arming gated; state repaint (forceActivity) is not.
    assert 'env.new === "clarify" && !isReplay(env)' in js
    # Stage (PR / interrupt / merged) and budget subscribers bail on replays.
    assert js.count("if (isReplay(env)) return;") >= 2
    stage = js[js.index('ev.subscribe("session.stage_changed"') :]
    assert stage.index("isReplay(env)") < stage.index('env.new === "pr"')
    budget = js[js.index('ev.subscribe("session.budget_exceeded"') :]
    assert budget.index("isReplay(env)") < budget.index("exceeded its budget")


# --------------------------------------------------------------------------- #
# L7 — missing-workspace row/pane treatment.
# --------------------------------------------------------------------------- #


def test_app_js_missing_workspace_wiring():
    js = client.get("/app.js").text
    # Feature-detected backend flag.
    assert "workspace_missing" in js
    # Chip is a distinct "missing workspace" label, not a fake stage.
    assert '"missing workspace"' in js
    assert "s-missing" in js
    # Single Clean up action → the existing DELETE endpoint.
    assert "cleanupMissing" in js
    assert '{ method: "DELETE" }' in js
    assert "Clean up — remove session" in js
    # Pane placeholder with the same action; no terminal is created.
    assert "missing-pane" in js
    assert "missing-cleanup" in js
    # Next-step button and diff context line are suppressed.
    assert "if (inst.workspace_missing) return null;" in js
    assert "workspace_missing ? null : inst.diff_stat" in js
    # Panes swap between live and placeholder when the flag flips.
    assert "missing-pane" in js


def test_style_css_missing_workspace_rules():
    css = client.get("/style.css").text
    for sel in (
        ".inst.ws-missing .inst-row",
        ".inst.ws-missing .dot",
        ".stagechip.s-missing",
        ".inst .kill.cleanup",
        ".pane.missing-pane",
        ".missing-body",
        ".missing-body .missing-cleanup",
    ):
        assert sel in css, sel
    # Theme-var driven (muted/border/red), so dark AND light are covered.
    block = css[css.index(".stagechip.s-missing") :]
    block = block[: block.index("}")]
    assert "var(--muted)" in block and "var(--border)" in block


# --------------------------------------------------------------------------- #
# L8 — no-origin push guidance.
# --------------------------------------------------------------------------- #


def test_app_js_no_origin_hint():
    js = client.get("/app.js").text
    # Feature-detected: only an explicit false changes the button.
    assert "inst.has_origin === false" in js
    # A runnable example, not a placeholder — and SSH is the one spelled out,
    # because MindFlock never rewrites a remote and pushes with plain git.
    assert "git remote add origin" in js
    assert "git@github.com:owner/repo.git" in js
    assert "https://github.com/owner/repo.git" in js
    assert "No remote — add origin…" in js
    # Clicking copies the command and toasts.
    assert '"command copied"' in js
    # Rendered as a subdued, non-destructive hint.
    assert "nextstep-hint" in js
    # Push API errors (e.g. the friendly 400) surface as toasts, not alerts.
    # (Error text is extracted via the shared errMsg() helper.)
    assert 'toast("Push failed: " + errMsg(err)' in js
    push_fn = js[js.index("async function pushSession") :]
    push_fn = push_fn[: push_fn.index("}\n")]
    assert "alert(" not in push_fn


def test_style_css_no_origin_hint_rules():
    css = client.get("/style.css").text
    assert ".pane-head .actions .nextstep.nextstep-hint" in css
    block = css[css.index(".nextstep.nextstep-hint") :]
    block = block[: block.index("}")]
    # Subdued: no accent fill, dashed border, copy cursor.
    assert "dashed" in block and "cursor: copy" in block


# --------------------------------------------------------------------------- #
# L9 — light-theme stage-chip contrast on focused/selected rows.
# --------------------------------------------------------------------------- #


def test_light_committed_chip_uses_deep_text_color():
    css = client.get("/style.css").text
    block = css[css.index(".light .stagechip.s-committed {") :]
    block = block[: block.index("}")]
    # Deep blue replaces the pale #7fd1ff, which vanished on light rows.
    assert "#0a6aa8" in block
    assert "#7fd1ff" not in block


def test_light_focused_rows_outline_all_chips():
    css = client.get("/style.css").text
    # Generic fix: every chip keeps its pill shape on active/hovered/focused
    # surfaces whose background matches the chip's own (--panel-2).
    idx = css.index(".light .inst.active .stagechip")
    block = css[idx : css.index("}", idx)]
    assert ".light .pane.focused .stagechip" in block
    assert "inset 0 0 0 1px var(--border)" in block


# --------------------------------------------------------------------------- #
# ssh-friendly-github — the GitHub CLI is optional in every UI surface.
#
# The contributor bug: `gh` absent -> a blocking modal echoing "GitHub CLI (gh)
# is not installed", from four independent entry points, for someone whose only
# sin was an SSH remote. These pin the degrade-don't-abort behaviour into the
# shipped bundle so it cannot regress the next time the surfaces are touched.
# --------------------------------------------------------------------------- #

# The user-facing remedy, verbatim. Also asserted in the docs and the backend.
PR_REMEDY = "add a GitHub token in Settings → PR review, or install the GitHub CLI"


def test_app_js_pr_actions_never_alert_about_gh():
    js = client.get("/app.js").text
    # The remedy sentence exists exactly once — one constant, no copies to drift.
    assert js.count(PR_REMEDY) == 1
    # No surface renders the backend's old gh sentence.
    assert "GitHub CLI (gh) is not installed" not in js
    # Make PR / Merge report through toasts; the blocking alert() is gone.
    for fn in ("async function submitMakePr", "async function mergeSession"):
        body = js[js.index(fn) :]
        body = body[: body.index("\n}\n")]
        assert "alert(" not in body, fn
        assert "toast(" in body, fn
    assert 'toast("Make PR failed: " + errMsg(err)' in js
    assert 'toast("Merge failed: " + errMsg(err)' in js


def test_app_js_handles_the_browser_fallback_responses():
    js = client.get("/app.js").text
    # 200 + ok:false is the "I can't file it for you" answer, not an error: the
    # prefilled compare / PR URL is opened (or offered as a clickable toast when
    # the popup blocker eats it).
    assert "r.ok === false" in js
    assert "r.compare_url" in js
    assert "r.pr_url" in js
    assert "click to open" in js


def test_app_js_gates_pr_entry_points_on_the_github_capability():
    js = client.get("/app.js").text
    # Feature-detected against an explicit false, like has_origin: a server that
    # never reports the capability keeps its buttons.
    assert "github !== false" in js
    # Unavailable != disabled — the step stays runnable, just marked as a hint
    # that lands in the browser.
    assert "Make PR ↗" in js
    assert "Merge on GitHub ↗" in js
    assert "opens GitHub" in js


def test_app_js_github_test_says_gh_is_optional_once():
    js = client.get("/app.js").text
    # One helper behind the setup checklist, PR review and Git issues.
    assert js.count("gh not installed (optional)") == 1
    assert 'gh not installed"' not in js
    # The ✓/✗ verdict is token-driven, so no-gh is not a red ✗.
    assert 'source !== "none"' in js


def test_app_js_doctor_and_tour_stop_selling_gh_as_required():
    js = client.get("/app.js").text
    # Doctor no longer lists gh among the required probes.
    assert "Probes git, tmux, gh," not in js
    assert "plus gh, which is optional" in js
    # The welcome tour leads with the token; `gh auth login` is no longer the
    # onboarding's concrete instruction.
    assert "gh auth login" not in js
    assert "That token is the whole setup" in js


def test_mobile_js_offers_a_link_not_a_raw_gh_error():
    js = client.get("/mobile.js").text
    assert PR_REMEDY in js
    assert "j.ok === false" in js
    assert "j.compare_url" in js
    assert "j.pr_url" in js
