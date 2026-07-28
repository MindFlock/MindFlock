"""Frontend wave 4 (J2 palette, J3 context line, J4 presets, J5 budget toast,
round-3 CSS fix): structural contract checks.

Same style as test_frontend_wave2.py / test_frontend_polish.py — the live UI is
verified with screenshots/CDP; these pin the markup hooks, JS wiring, and CSS
rules so they can't silently regress.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from backend.web import server

client = TestClient(server.app)


# --------------------------------------------------------------------------- #
# J2 — command palette (Ctrl+P / Ctrl+Shift+P)
# --------------------------------------------------------------------------- #


def test_index_has_palette_markup():
    html = client.get("/").text
    assert '"palette"' in client.get("/app.js").text
    assert '"palette-panel"' in client.get("/app.js").text
    assert '"palette-input"' in client.get("/app.js").text
    assert '"palette-list"' in client.get("/app.js").text
    # The shortcut is surfaced in the sidebar footer.
    assert '"palette-btn"' in client.get("/app.js").text
    # The palette lives on the always-global Ctrl+P / Ctrl+Shift+P (Cmd on
    # Mac); Ctrl+K is the chord prefix for focused-session git actions.
    assert "Ctrl+P" in client.get("/app.js").text


def test_app_js_palette_wiring():
    js = client.get("/app.js").text
    # Ctrl+P / Ctrl+Shift+P (Cmd on Mac) toggle the palette via the
    # capture-phase keymap, so it works while a terminal has focus.
    assert "togglePalette" in js
    assert "e.ctrlKey || e.metaKey" in js
    # Fuzzy subsequence filter + the action list.
    assert "fuzzyScore" in js
    assert "palette-item" in js
    for label in (
        '"New session…"',
        '"Focus: "',
        '"Open Settings"',
        '"Open Doctor"',
        '"Open Setup checklist"',
        '"Toggle sidebar"',
        '"New from Recently closed…"',
    ):
        assert label in js, label
    # Focused-session actions reuse the existing handlers.
    assert "commitSession(t)" in js
    assert "pushSession(t)" in js
    assert "makePrSession(t)" in js
    assert "ideSession(t)" in js
    # Keyboard: arrows + Enter run, Escape closes.
    assert "ArrowDown" in js and "ArrowUp" in js
    assert "filtered[sel]" in js
    assert "closeDialog" in js


def test_style_css_has_palette_rules():
    css = client.get("/style.css").text
    for sel in (
        "#palette-panel",
        "#palette-input",
        ".palette-item",
        ".palette-item.selected",
        ".palette-hint",
        "#palette-btn",
    ):
        assert sel in css, sel


# --------------------------------------------------------------------------- #
# J3 — session context line (diff stat), feature-detected off /api/instances.
# --------------------------------------------------------------------------- #


def test_app_js_context_line_wiring():
    js = client.get("/app.js").text
    # Feature detection: backend may not send diff_stat yet.
    assert "diff_stat" in js
    assert "diff_stat" in js and "ctx-files" in js
    # Rendered in the pane header only; the sidebar card no longer carries the
    # diff-stat line (removed to keep the card uncluttered).
    assert "inst-ctx" not in js
    assert "ctx-add" in js
    # Hidden when null / all-zero.
    assert "hasDiffStat" in js


def test_style_css_has_context_line_rules():
    css = client.get("/style.css").text
    for sel in (
        ".ctxline",
        ".ctxline .ctx-add",
        ".ctxline .ctx-del",
        ".pane-head .ctxline",
    ):
        assert sel in css, sel
    # Green/red tints come from the theme vars, so both themes are covered.
    # (Block-extracted, not line-matched, so the check survives CSS reflow.)
    add = css[css.index(".ctxline .ctx-add") :]
    assert "--green" in add[: add.index("}")]
    dele = css[css.index(".ctxline .ctx-del") :]
    assert "--red" in dele[: dele.index("}")]


# --------------------------------------------------------------------------- #
# J4 — prompt presets in the New-session dialog.
# --------------------------------------------------------------------------- #


def test_index_has_prompt_and_preset_markup():
    html = client.get("/").text
    assert '"new-prompt"' in client.get("/app.js").text
    assert '"new-preset"' in client.get("/app.js").text
    assert '"preset-save"' in client.get("/app.js").text
    assert '"preset-del"' in client.get("/app.js").text


def test_app_js_preset_wiring():
    js = client.get("/app.js").text
    # Persisted under the documented localStorage key.
    assert "mindflock.prompt_presets" in js
    # The four built-ins.
    for name in (
        "Fix failing tests",
        "Address PR review comments",
        "Write tests for recent changes",
        "Refactor for clarity — no behavior change",
    ):
        assert name in js, name
    # Save / delete affordances + fill-on-select.
    assert "loadUserPresets" in js and "saveUserPresets" in js
    assert "mindflock.prompt_presets" in js
    # The seed prompt rides along on the create payload.
    assert "body.prompt = promptVal" in js


# --------------------------------------------------------------------------- #
# J5 — budget guardrail frontend: toast subscription + notify rule + setting.
# --------------------------------------------------------------------------- #


def test_app_js_subscribes_budget_exceeded():
    js = client.get("/app.js").text
    assert '"session.budget_exceeded"' in js
    assert "exceeded its budget" in js
    # Click focuses the session; toast body carries $cost of $budget.
    assert "d.cost" in js and "d.budget" in js


def test_notify_config_covers_budget_exceeded():
    rules = client.get("/api/notify/config").json()["rules"]
    budget = [r for r in rules if r["event"] == "session.budget_exceeded"]
    assert len(budget) == 1
    # No old/new constraint: the event itself is the signal.
    assert budget[0]["old"] is None and budget[0]["new"] is None
    assert "{session}" in budget[0]["title"]


def test_settings_dialog_has_budget_field():
    html = client.get("/").text
    assert '"session_budget_usd"' in client.get("/app.js").text
    assert "Per-session budget (USD, 0 = off)" in client.get("/app.js").text


# --------------------------------------------------------------------------- #
# Round-3 CSS fix: sidebar row title/branch flex weights.
# --------------------------------------------------------------------------- #


def test_inst_title_wins_free_width_over_branch():
    css = client.get("/style.css").text
    title = css[css.index(".inst .title {") :]
    title = title[: title.index("}")]
    branch = css[css.index(".inst .branch {") :]
    branch = branch[: branch.index("}")]
    # Title grows into free width (was flex: 0 1 auto → ellipsized at ~5 chars
    # while the branch span won all free width as an illegible sliver).
    assert "flex: 1 1 auto" in title
    assert "min-width: 34px" in title  # legible floor kept (H4)
    # Branch yields: content-sized, capped at 40%, with its own ellipsis.
    assert "flex: 0 1 auto" in branch
    assert "max-width: 40%" in branch
    assert "text-overflow: ellipsis" in branch
