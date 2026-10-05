"""MindFlock MCP from the UI, the playbook menu (unit F1): structural checks.

The pane's fork-icon button and its "Work with other sessions" menu
(grid/PlaybookMenu.tsx), the row › menu group (sidebar/PlaybookRowItems.tsx),
the palette entries, the Ctrl+K S / F / T chords, the New dialog's "Split
across workers" box (dialogs/SplitCheck.tsx) and the store's threadOpen.
lib/playbooks.ts's pure half is pinned by vitest (playbookMenu.test.ts), and
the rendered result was checked in the screenshot harness. These tests pin the
COMMITTED bundle so the wiring can't regress silently.

The palette's two old entries asked for their text with ``window.prompt``.
Electron never implements it, so in the desktop app those entries did nothing.
The fix is pinned as the OLD call sites being ABSENT, not only the new
behaviour being present: both can be true at once, and only the absence proves
the dead prompt no longer runs. The same goes for every module this unit adds:
none of them calls ``prompt``/``confirm``/``alert``.

Multi-token snippets go through ``tests/_bundle.py`` (Vite 8 / Rolldown lays
the bundle out with tabs and line breaks).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests._bundle import in_bundle

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "backend" / "web" / "static"
SRC = ROOT / "frontend" / "src"

# Silent no-ops in Electron. A word boundary so `sendMessagePrompt(` doesn't
# count, and an optional `window.` because both spellings reach the same API.
DEAD_DIALOG = re.compile(r"(?<![\w.$])(?:window\.)?(?:prompt|confirm|alert)\(")

F1_MODULES = (
    "src/lib/playbooks.ts",
    "src/components/grid/PlaybookMenu.tsx",
    "src/components/sidebar/PlaybookRowItems.tsx",
    "src/components/dialogs/SplitCheck.tsx",
)


@pytest.fixture(scope="module")
def js() -> str:
    return (STATIC / "app.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def css() -> str:
    return (STATIC / "style.css").read_text(encoding="utf-8")


def _region(js: str, module: str) -> str:
    """The bundled text of one source module (Rolldown's ``//#region`` markers)."""
    start = js.find("//#region " + module + "\n")
    assert start >= 0, f"{module} is not in the bundle"
    end = js.find("//#endregion", start)
    assert end > start
    return js[start:end]


def _function(js: str, name: str) -> str:
    """The text of a top-level bundled function, up to the next top-level one."""
    m = re.search(r"^(?:async )?function " + re.escape(name) + r"\(", js, re.M)
    assert m, f"function {name} not in bundle"
    nxt = re.search(r"^(?:async )?function |^//#endregion", js[m.end() :], re.M)
    return js[m.start() : m.end() + (nxt.start() if nxt else len(js))]


# --- The dead window.prompt paths ---------------------------------------------


def test_palette_send_and_queue_no_longer_use_window_prompt(js):
    # The OLD call sites are gone, by their text and by their function names.
    assert "Send a message to " not in js
    assert "Queue a prompt for " not in js
    assert "function sendMessagePrompt(" not in js
    assert "function queuePromptPrompt(" not in js
    palette = _region(js, "src/components/palette/CommandPalette.tsx")
    assert not DEAD_DIALOG.search(palette), "the palette calls a dead dialog"
    # The entries stay, and now open the place the text is typed.
    assert in_bundle("label: `Send message… — ${t}`", palette)
    assert in_bundle("run: () => ui.threadOpen(t, { composeTo: t })", palette)
    assert in_bundle("label: `Queue prompt… — ${t}`", palette)
    assert in_bundle("run: () => focusQueueInput(t)", palette)


def test_queue_prompt_focuses_the_queue_tab_textarea(js):
    fn = _function(js, "focusQueueInput")
    assert in_bundle('setLastTab(title, "queue")', fn)
    assert '".pane-queue .queue-input"' in fn
    assert "box.focus()" in fn


def test_new_modules_never_call_a_dead_dialog(js):
    for mod in F1_MODULES:
        hits = DEAD_DIALOG.findall(_region(js, mod))
        assert not hits, f"{mod} calls {hits}, a no-op in Electron"
    # And at the source, where the bundle could have renamed nothing away.
    for mod in F1_MODULES:
        src = (ROOT / "frontend" / mod).read_text(encoding="utf-8")
        assert not DEAD_DIALOG.search(src), mod


# --- A paste is never a send ---------------------------------------------------


def test_a_playbook_is_rendered_then_typed_never_submitted(js):
    # One paste in flight per session, whichever surface asked (review
    # 2026-10-05: the row › menu had no guard, a double click typed twice).
    guard = _function(js, "pastePlaybook")
    assert "if (pasting.has(title)) return false;" in guard
    assert "pasting.delete(title)" in guard
    fn = _function(js, "pasteNow")
    assert in_bundle('"/api/playbooks/" + encodeURIComponent(pb.id) + "/render"', fn)
    assert in_bundle("json: { title, args }", fn)
    # dialog_safe: the server re-checks the agent and never types the paste
    # into a dialog that came up after the menu was drawn (review 2026-10-05).
    assert in_bundle(
        'api("/api/instances/" + encodeURIComponent(title) + "/send", { json: { text, submit: false, dialog_safe: true } })',
        fn,
    )
    # On the Agent tab, with the keyboard in the terminal for the task + Enter.
    assert in_bundle('ui.setLastTab(title, "agent")', fn)
    assert "focusTerm(title)" in fn
    assert "add the task, then press Enter" in fn


def test_the_menu_reads_the_server_list_per_session(js):
    fn = _function(js, "fetchPlaybooks")
    assert in_bundle(
        'api("/api/playbooks" + (title ? "?title=" + encodeURIComponent(title)', fn
    )
    # A palette entry asks the server first, so a refusal is its sentence.
    run = _function(js, "runPlaybook")
    assert "pb.disabled_reason" in run
    # The Thread's "Merge <w> into <t>" names itself in the toast (F3).
    assert in_bundle(
        "pastePlaybook(title, label ? { id: pb.id, label } : pb, args)", run
    )


# --- The fork-icon button and the menu -----------------------------------------


def test_fork_button_is_first_in_head_tail_and_gated_on_the_cap(js):
    pane = _region(js, "src/components/grid/Pane.tsx")
    assert "const forkShown = mcpCapable(caps, inst);" in pane
    assert 'const forkBlocked = forkShown ? forkBlockReason(inst) : "";' in pane
    tail = pane.rfind('className: "head-tail"')
    fork = pane.find('className: "act playbooks"', tail)
    copy = pane.find('className: "act copyhist"', tail)
    assert tail >= 0 and 0 <= fork < copy, "the fork button must lead .head-tail"
    assert (
        "Work with other sessions — split across workers, ask, review, hand off (Ctrl+K F)"
        in pane
    )
    gate = _function(js, "mcpCapable")
    assert "m.enabled" in gate and "(m.providers || []).includes(provider)" in gate


def test_fork_button_blocks_with_the_reason(js):
    block = _function(js, "forkBlockReason")
    assert "inst.mcp_attached === false" in block
    assert in_bundle('inst.activity === "clarify" || inst.activity === "limit"', block)
    assert '"Restart this agent to give it the MindFlock tools"' in js
    assert '"Answer its prompt first — pasting now would answer the dialog"' in js


def test_menu_header_sections_footer_and_keys(js):
    menu = _region(js, "src/components/grid/PlaybookMenu.tsx")
    assert '"Work with other sessions"' in menu
    assert 'id: "playbook-menu"' in menu
    assert "'s workers · " in menu
    assert '"Message…"' in menu and '"Ctrl+K S"' in menu
    assert "'s input. Add the task, press Enter — nothing runs until you do." in menu
    # Arrows, Enter, Esc and each item's letter; Ask › opens its submenu.
    for key in ('"ArrowDown"', '"ArrowUp"', '"ArrowRight"', '"Escape"', '"Enter"'):
        assert key in menu, key
    assert "letterOf(x.pb) === k.toUpperCase()" in menu
    assert in_bundle("pastePlaybook(title, askPb, { session: t.title })", menu)
    # Message… is the user's own words: the Thread composer, not a paste.
    assert in_bundle("useUi.getState().threadOpen(title, { composeTo: title })", menu)


def test_menu_holds_the_keyboard_like_a_modal(js):
    keymap = _region(js, "src/lib/keymap.ts")
    ids = keymap[keymap.find("MODAL_DOM_IDS = [") :]
    ids = ids[: ids.find("];")]
    assert '"playbook-menu"' in ids


def test_row_menu_group(js):
    row = _region(js, "src/components/sidebar/SidebarRow.tsx")
    assert "PlaybookRowItems" in row
    group = _region(js, "src/components/sidebar/PlaybookRowItems.tsx")
    assert "mcpCapable(config?.caps, inst)" in group
    assert in_bundle("pastePlaybook(title, pb, { session: t.title })", group)
    assert in_bundle('className: "menu-sep"', group)


def test_palette_entries(js):
    palette = _region(js, "src/components/palette/CommandPalette.tsx")
    for label in (
        "label: `Split across workers… — ${t}`",
        "label: `Ask a session… — ${t}`",
        "label: `Check on workers — ${t}`",
        "label: `Wrap up workers — ${t}`",
        "label: `Work with other sessions… — ${t}`",
        "label: `Thread — ${t}`",
    ):
        assert in_bundle(label, palette), label
    assert in_bundle('runPlaybook(t, "split")', palette)
    assert in_bundle('runPlaybook(t, "wrapup")', palette)
    # The worker ones only while the session has workers.
    assert "liveChildren(t, rows).length" in palette


def test_chords_s_f_t(js):
    keymap = _region(js, "src/lib/keymap.ts")
    assert in_bundle(
        's: { desc: "Message…", run: (t) => useUi.getState().threadOpen(t, { composeTo: t }) }',
        keymap,
    )
    assert in_bundle(
        'f: { desc: "Work with other sessions…", run: (t) => openPlaybookMenu(t) }',
        keymap,
    )
    assert in_bundle(
        't: { desc: "Thread — workers and messages", run: (t) => useUi.getState().threadOpen(t) }',
        keymap,
    )


# --- Store: threadOpen and the "thread" tab -------------------------------------


def test_thread_open_and_last_seen(js):
    store = _region(js, "src/state/store.ts")
    assert in_bundle("_selectSession(title, { noKeyboard: true })", store)
    assert in_bundle('get().setLastTab(title, "thread")', store)
    assert in_bundle("to: opts?.composeTo || title", store)
    # Persisted through the store's own load/save (the bundler may rename
    # `load`, so only the call's arguments are quoted).
    assert re.search(r'threadLastSeen: load\$?\w*\("mf_thread_seen", \{\}\)', store)
    assert 'save("mf_thread_seen", threadLastSeen)' in store
    # sessionActions registers the one selection rule with the store.
    assert "setSessionSelector(selectSession);" in js


def test_pane_draws_the_thread_tab_and_falls_back_to_agent(js):
    pane = _region(js, "src/components/grid/Pane.tsx")
    # Unit F3 landed the Thread tab's body, so "thread" is a tab the pane can
    # draw; anything else unknown still shows the Agent tab, never an empty
    # pane. (test_frontend_thread.py pins the tab itself.)
    body_tabs = pane[pane.find("var BODY_TABS = ") :]
    body_tabs = body_tabs[: body_tabs.find(";")]
    assert in_bundle(
        'new Set([ "agent", "shell", "diff", "queue", "map", "thread" ])', body_tabs
    )
    fn = _function(js, "paneTab")
    assert 'if (!BODY_TABS.has(t)) return "agent";' in fn


# --- New dialog: Split across workers ------------------------------------------


def test_new_dialog_split_check(js):
    dlg = _region(js, "src/components/dialogs/NewSessionDialog.tsx")
    # Two boxes, one state: the Describe page and the Prompt fold.
    assert 'id: "new-split"' in dlg and 'id: "new-split-prompt"' in dlg
    assert dlg.count("onSplit: toggleSplit") == 2
    # Reset on every open (the failed-reopen early return covers it too).
    reset = dlg[dlg.find("if (failedReopen.current) {") :]
    reset = reset[: reset.find('folderDo({ t: "reopen" });')]
    assert "setSplit(false);" in reset
    # Gated by the agent in the form; the body carries the playbook.
    assert in_bundle("splitGate(config?.caps, canonAgent(program))", dlg)
    assert "const splitOn = split && mcpOk.ok;" in dlg
    assert "return withSplit(body, splitOn);" in dlg
    # A ticked box keeps its worktree over a plan's "in this folder".
    assert "setInPlace(planInPlace(a) && !splitOn);" in dlg
    body = _function(js, "withSplit")
    assert in_bundle('playbook: "split"', body)
    assert "next.in_place = false" in body


def test_split_check_copy_and_pill(js):
    box = _region(js, "src/components/dialogs/SplitCheck.tsx")
    assert '"Split across workers"' in box
    assert "suggestionPill(sug)" in box
    assert "Runs in a new worktree." in box
    assert "answer from the rail." in box
    pill = _function(js, "suggestionPill")
    assert in_bundle('["suggested", ...sug.pieces].join(" · ")', pill)


def test_styles(css):
    for sel in (
        ".pane-head .act.playbooks",
        ".pane-head .act.playbooks.open",
        ".pane-head .act.playbooks.is-blocked",
        ".pb-menu",
        ".pb-menu .pb-item.sel",
        ".pb-sub",
        ".inst-actions .pb-row-sub",
        ".nf-split-pill",
        ".nf-split-nudge",
        ".light .pb-menu",
    ):
        assert sel in css, sel


def test_review_remote_rows_get_no_playbooks_and_one_children_rule(js):
    """Review 2026-10-05: a remote device's row got the fork button, row items
    and palette entries (only /api/instances/dev::… is forwarded, so
    /api/playbooks 404s); four helpers counted workers four ways."""
    cap = _function(js, "mcpCapable")
    assert "if (isRemote(inst)) return false;" in cap
    remote = _function(js, "isRemote")
    assert 'String(inst.title || "").includes("::")' in remote
    rule = _function(js, "isChildOf")
    assert in_bundle(
        'String(row.parent || "") === title && !row.pending && !row.device', rule
    )
    assert "return childrenOf(title, rows);" in _function(js, "liveChildren")
    assert "childrenOf(title, rows)" in _function(js, "familyOf")
    assert "isChildOf(r, p)" in _function(js, "childrenByParent")


def test_review_menu_items_are_announced(js):
    menu = _region(js, "src/components/grid/PlaybookMenu.tsx")
    assert '"aria-activedescendant": askOpen && askPb' in menu
    assert "id: itemId(i)" in menu and "id: sessId(i)" in menu
    assert menu.count("tabIndex: -1") >= 4  # the menu + its three item kinds


def test_review_chords_taken_by_an_older_rebinding(js):
    keymap = _region(js, "src/lib/keymap.ts")
    assert (
        "const cid = chordForKey(key.toLowerCase());" in keymap
    )  # Vite 8 inlines `pressed`
    assert "function chordShadowedBy(id)" in keymap
    sheet = _region(js, "src/components/palette/ShortcutsSheet.tsx")
    assert "chordShadowedBy(k)" in sheet and '" (taken)"' in sheet
