"""Ship & split (ship lanes, SPEC §7.C.5) and what is left of the playbook UI:
structural checks against the COMMITTED bundle.

The pane's fork-icon menu used to be "Work with other sessions": four
playbooks that PASTED a prompt into the agent's input for the user to send.
It is now "Ship & split" (grid/ShipMenu.tsx), whose items ACT through the
server — the session's lane (``POST /api/instances/{t}/lane``), ship now,
split into parallel pieces (a split run), move out of a group, and Message….
The row › menu (sidebar/PlaybookRowItems.tsx) and the palette offer the same
items through the same function (lib/laneActions.runShipEntry). The pure half
is pinned by vitest (shipMenu.test.ts, newList.test.ts).

The removal is pinned as the OLD call sites being ABSENT, not only the new
behaviour being present: both could be true at once, and only the absence
proves the paste no longer runs from these surfaces. The same goes for
``window.prompt``/``confirm``/``alert``, which Electron never implements.

What still pastes, on purpose until split runs merge pieces back themselves:
the Thread tab's worker buttons and the rail's wrap-up chip (``runPlaybook`` /
``pastePlaybook``), pinned below so a change there is deliberate.

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

# Every module ship lanes adds or rewrote. None may call a dead dialog, and
# none may paste.
SHIP_MODULES = (
    "src/lib/laneActions.ts",
    "src/lib/runStart.ts",
    "src/lib/playbooks.ts",
    "src/components/grid/ShipMenu.tsx",
    "src/components/sidebar/PlaybookRowItems.tsx",
    "src/components/dialogs/NewList.tsx",
    "src/components/dialogs/SplitCheck.tsx",
    "src/components/palette/CommandPalette.tsx",
)
NO_PASTE_MODULES = tuple(m for m in SHIP_MODULES if m != "src/lib/playbooks.ts")


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


# --- The paste playbooks are gone from the menus -------------------------------


def test_the_paste_surfaces_are_absent(js):
    # The old menu and its strings, by name.
    assert "src/components/grid/PlaybookMenu.tsx" not in js
    assert "Work with other sessions" not in js
    assert "pastes the prompt" not in js
    assert "Pastes the prompt into" not in js
    assert "function openPlaybookMenu(" not in js
    assert "function menuModel(" not in js
    assert "function askTargets(" not in js
    assert "function withSplit(" not in js
    assert '"playbook-menu"' not in js
    # No surface of Ship & split pastes, at the source and in the bundle.
    for mod in NO_PASTE_MODULES:
        region = _region(js, mod)
        assert "pastePlaybook(" not in region, mod
        assert "runPlaybook(" not in region, mod
        src = (ROOT / "frontend" / mod).read_text(encoding="utf-8")
        assert "pastePlaybook(" not in src and "runPlaybook(" not in src, mod
    # The New dialog no longer sends the split PLAYBOOK on a create.
    dlg = _region(js, "src/components/dialogs/NewSessionDialog.tsx")
    assert 'playbook: "split"' not in dlg
    assert "withSplit(" not in dlg


def test_new_modules_never_call_a_dead_dialog(js):
    for mod in SHIP_MODULES:
        hits = DEAD_DIALOG.findall(_region(js, mod))
        assert not hits, f"{mod} calls {hits}, a no-op in Electron"
    # And at the source, where the bundle could have renamed nothing away.
    for mod in SHIP_MODULES:
        src = (ROOT / "frontend" / mod).read_text(encoding="utf-8")
        assert not DEAD_DIALOG.search(src), mod


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


# --- Every item is a server call -------------------------------------------------


def test_lane_ship_now_split_and_move_out_are_server_calls(js):
    lane = _function(js, "setLane")
    assert in_bundle('instApi(title, "/lane", { json: {', lane)
    assert "ask_first: askFirstApplies(lane) && askFirst" in lane
    ship = _function(js, "shipNow")
    assert in_bundle('instApi(title, "/ship-now", {', ship)
    # It sends the lane the row SHOWED: the server never falls back to the
    # Settings default for a row with no lane of its own.
    assert in_bundle("{ lane }", ship)
    assert 'case "shipnow"' in _function(js, "runShipEntry")
    assert "shipNow(title, current.lane)" in _function(js, "runShipEntry")
    # A copy window / a lead / a one-for-all member is locked in the menu.
    assert "drives this branch" in _function(js, "laneLockReason")
    # Every run route goes through lib/runsApi's one path builder.
    out = _function(js, "moveOutOfGroup")
    assert 'taskPath(runId, taskId, "skip")' in out
    assert '"/api/runs/"' in _region(js, "src/lib/runsApi.ts")
    split = _function(js, "startSplitOf")
    assert in_bundle('api("/api/runs", { json: {', split)
    assert "split: true" in split and "lead: inst.title" in split
    assert in_bundle('grouping: "together"', split)
    # One action per item, shared by the pane menu and the row › menu.
    run = _function(js, "runShipEntry")
    for kind in (
        'case "lane"',
        'case "ask"',
        'case "split"',
        'case "shipnow"',
        'case "detach"',
    ):
        assert kind in run, kind
    assert "runShipEntry(e, inst, name, model.current)" in _region(
        js, "src/components/grid/ShipMenu.tsx"
    )
    assert "runShipEntry(e, inst, name, model.current)" in _region(
        js, "src/components/sidebar/PlaybookRowItems.tsx"
    )


def test_a_new_single_session_gets_its_lane_once_it_can_take_it(js):
    fn = _function(js, "setLaneWhenReady")
    assert 'if (lane === "leave") return true;' in fn
    # 409 "workspace not ready" is retried; anything else is said.
    assert "err.status === 409" in fn
    dlg = _region(js, "src/components/dialogs/NewSessionDialog.tsx")
    assert 'if (lane !== "leave") setLaneWhenReady(inst.title, lane, askFirst);' in dlg


# --- The fork-icon button and the menu -------------------------------------------


def test_fork_button_is_first_in_head_tail_for_every_local_session(js):
    pane = _region(js, "src/components/grid/Pane.tsx")
    assert "const forkShown = shipShown(inst);" in pane
    assert "return !isRemote(inst) && !inst.pending;" in _function(js, "shipShown")
    # No longer gated on the MindFlock tools or blocked by a dialog: lanes are
    # server calls; only Split needs the tools, and says so on that item.
    assert "mcpCapable(caps, inst)" not in pane
    assert "is-blocked" not in pane
    tail = pane.rfind('className: "head-tail"')
    fork = pane.find('"act playbooks"', tail)
    copy = pane.find('className: "act copyhist"', tail)
    assert tail >= 0 and 0 <= fork < copy, "the fork button must lead .head-tail"
    assert (
        "Ship & split — how far MindFlock carries this session, split it, message (Ctrl+K F)"
        in pane
    )


def test_menu_header_sections_footer_and_keys(js):
    menu = _region(js, "src/components/grid/ShipMenu.tsx")
    assert 'id: "ship-menu"' in menu
    assert '"Ship & split"' in menu
    assert '"When it\'s done"' in menu and '"Split"' in menu
    assert '"Split into parallel pieces…"' in menu
    assert '"Ship it now"' in menu
    assert '"Message…"' in menu and '"Ctrl+K S"' in menu
    assert "Every item acts right away — nothing is pasted into the agent." in menu
    # The current lane is ticked and pre-selected.
    assert 'e.label + (e.current ? " ✓" : "")' in menu
    assert in_bundle('entries.findIndex((e) => e.kind === "lane" && e.current)', menu)
    # Arrows, Enter, Esc and each item's letter.
    for key in ('"ArrowDown"', '"ArrowUp"', '"Escape"', '"Enter"'):
        assert key in menu, key
    assert "entryKey(x) === k.toUpperCase()" in menu
    # Message… is the user's own words: the Thread composer, not a paste.
    assert in_bundle("useUi.getState().threadOpen(title, { composeTo: title })", menu)
    # The lane rows and their letters.
    lanes = js[js.find("var LANE_MENU = [") :]
    lanes = lanes[: lanes.find("];")]
    for label, key in (
        ("Leave it", "L"),
        ("Commit", "C"),
        ("Open a PR", "P"),
        ("Merge when checks pass", "M"),
    ):
        assert f'label: "{label}"' in lanes and f'key: "{key}"' in lanes, label


def test_menu_holds_the_keyboard_like_a_modal(js):
    keymap = _region(js, "src/lib/keymap.ts")
    ids = keymap[keymap.find("MODAL_DOM_IDS = [") :]
    ids = ids[: ids.find("];")]
    assert '"ship-menu"' in ids


def test_review_menu_items_are_announced(js):
    menu = _region(js, "src/components/grid/ShipMenu.tsx")
    assert '"aria-activedescendant": itemId(sel)' in menu
    assert "id: itemId(i)" in menu
    assert '"menuitemradio"' in menu and '"menuitemcheckbox"' in menu
    assert menu.count("tabIndex: -1") >= 2  # the menu + its items


def test_row_menu_group(js):
    row = _region(js, "src/components/sidebar/SidebarRow.tsx")
    assert "PlaybookRowItems" in row
    group = _region(js, "src/components/sidebar/PlaybookRowItems.tsx")
    assert "shipMenuModel(inst, rows || [], config?.caps)" in group
    assert "if (isRemote(inst) || inst.pending) return null;" in group
    assert "When it's done: " in group
    assert in_bundle('className: "menu-sep"', group)


def test_palette_entries(js):
    palette = _region(js, "src/components/palette/CommandPalette.tsx")
    for label in (
        'label: "Start several sessions…"',
        "label: `Ship: open a PR when done — ${t}`",
        "label: `Ship: commit when done — ${t}`",
        "label: `Split into parallel pieces… — ${t}`",
        "label: `Ship & split… — ${t}`",
        "label: `Thread — ${t}`",
    ):
        assert in_bundle(label, palette), label
    assert 'run: () => ui.openNewWith("")' in palette
    assert "run: () => openShipMenu(t)" in palette


def test_chords_s_f_l_t(js):
    keymap = _region(js, "src/lib/keymap.ts")
    assert in_bundle(
        's: { desc: "Message…", run: (t) => useUi.getState().threadOpen(t, { composeTo: t }) }',
        keymap,
    )
    assert in_bundle(
        'f: { desc: "Ship & split…", run: (t) => openShipMenu(t) }', keymap
    )
    assert in_bundle(
        'l: { desc: "When it\'s done… (lane)", run: (t) => openShipMenu(t, "lane") }',
        keymap,
    )
    assert in_bundle(
        't: { desc: "Thread — workers and messages", run: (t) => useUi.getState().threadOpen(t) }',
        keymap,
    )


def test_review_chords_taken_by_an_older_rebinding(js):
    keymap = _region(js, "src/lib/keymap.ts")
    assert (
        "const cid = chordForKey(key.toLowerCase());" in keymap
    )  # Vite 8 inlines `pressed`
    assert "function chordShadowedBy(id)" in keymap
    sheet = _region(js, "src/components/palette/ShortcutsSheet.tsx")
    assert "chordShadowedBy(k)" in sheet and '" (taken)"' in sheet


# --- What still pastes (Thread buttons, the wrap-up chip) ------------------------


def test_the_remaining_paste_is_rendered_then_typed_never_submitted(js):
    guard = _function(js, "pastePlaybook")
    assert "if (pasting.has(title)) return false;" in guard
    assert "pasting.delete(title)" in guard
    fn = _function(js, "pasteNow")
    assert in_bundle('"/api/playbooks/" + encodeURIComponent(pb.id) + "/render"', fn)
    assert in_bundle(
        'api("/api/instances/" + encodeURIComponent(title) + "/send", { json: { text, submit: false, dialog_safe: true } })',
        fn,
    )
    run = _function(js, "runPlaybook")
    assert "pb.disabled_reason" in run


# --- Store: threadOpen and the "thread" tab -------------------------------------


def test_thread_open_and_last_seen(js):
    store = _region(js, "src/state/store.ts")
    assert in_bundle("_selectSession(title, { noKeyboard: true })", store)
    assert in_bundle('get().setLastTab(title, "thread")', store)
    assert in_bundle("to: opts?.composeTo || title", store)
    assert re.search(r'threadLastSeen: load\$?\w*\("mf_thread_seen", \{\}\)', store)
    assert 'save("mf_thread_seen", threadLastSeen)' in store
    assert "setSessionSelector(selectSession);" in js


def test_pane_draws_the_thread_tab_and_falls_back_to_agent(js):
    pane = _region(js, "src/components/grid/Pane.tsx")
    body_tabs = pane[pane.find("var BODY_TABS = ") :]
    body_tabs = body_tabs[: body_tabs.find(";")]
    assert in_bundle(
        'new Set([ "agent", "shell", "diff", "queue", "map", "thread" ])', body_tabs
    )
    fn = _function(js, "paneTab")
    assert 'if (!BODY_TABS.has(t)) return "agent";' in fn


# --- New dialog: split is a split RUN -------------------------------------------


def test_new_dialog_split_check(js):
    dlg = _region(js, "src/components/dialogs/NewSessionDialog.tsx")
    # One box now, on page 1; the Prompt fold's copy is gone.
    assert 'id: "new-split"' in dlg and 'id: "new-split-prompt"' not in dlg
    reset = dlg[dlg.find("if (failedReopen.current) {") :]
    reset = reset[: reset.find('folderDo({ t: "reopen" });')]
    assert "setSplit(false);" in reset
    # Gated by the agent AND by the box holding exactly one task line.
    assert in_bundle("splitGate(config?.caps, canonAgent(program))", dlg)
    assert "const splitOn = page === 1 && split && mcpOk.ok && draft.oneTask;" in dlg
    assert "shapeReason: splitShapeReason(draft.items)" in dlg
    # A split starts through the run, and its button names the lead.
    assert "draft.start({ split: splitOn })" in dlg
    assert in_bundle('if (split) return "Start the lead";', _function(js, "startLabel"))


def test_split_check_copy_and_pill(js):
    box = _region(js, "src/components/dialogs/SplitCheck.tsx")
    assert '"Split a big line into parallel pieces first"' in box
    assert "Split across workers" not in box
    assert "suggestionPill(sug)" in box
    # What the tick does is said once, by the summary sentence.
    assert "nf-split-nudge" not in box
    assert "approve the split in its Thread tab" in _function(js, "summarySentence")
    pill = _function(js, "suggestionPill")
    assert in_bundle('["suggested", ...sug.pieces].join(" · ")', pill)


def test_styles(css):
    for sel in (
        ".pane-head .act.playbooks",
        ".pane-head .act.playbooks.open",
        ".pb-menu",
        ".pb-menu .pb-item.sel",
        ".inst-actions .pb-row-sub",
        ".inst-actions .pb-row-sub button.on",
        ".nf-split-pill",
        ".light .pb-menu",
    ):
        assert sel in css, sel
    # The Ask › picker went with its playbook.
    assert ".pb-sub" not in css


def test_review_remote_rows_get_no_ship_menu_and_one_children_rule(js):
    """A remote device's row gets no fork button, row items or palette
    entries (only /api/instances/dev::… is forwarded, so the group and split
    routes would 404); one "children of" rule for every surface."""
    remote = _function(js, "isRemote")
    assert 'String(inst.title || "").includes("::")' in remote
    opener = _function(js, "openShipMenu")
    assert "isRemote(inst)" in opener
    rule = _function(js, "isChildOf")
    assert in_bundle(
        'String(row.parent || "") === title && !row.pending && !row.device', rule
    )
    assert "childrenOf(title, rows)" in _function(js, "familyOf")
    assert "isChildOf(r, p)" in _function(js, "childrenByParent")
