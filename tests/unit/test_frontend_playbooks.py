"""Fast-track (the ONE "how far does this session go" control) and what is
left of the playbook UI: structural checks against the COMMITTED bundle.

The pane's fork-icon menu was first "Work with other sessions" (four
playbooks that PASTED a prompt), then "Ship & split" (lanes, ship now, split,
move out, Message…). Both are gone. Fast-track is now one control under one
name: the pane head's ⏩ button names the target and opens the picker
(grid/FastTrackMenu.tsx — Off / Commit / Push / Open a PR / Merge when
green, and "Ask me before it ships"), every item a ``POST
/api/instances/{t}/lane``. The row › menu (sidebar/SessionRowItems.tsx), the
palette and Ctrl+K F open that same picker; Split into parallel pieces… and
Move out of a group are row › menu (and palette) actions of their own; the
New and Commit dialogs draw the same choices (dialogs/FastTrackChoice.tsx).
The pure half is pinned by vitest (fastTrack.test.ts, autopilot.test.ts,
newList.test.ts).

Every removal is pinned as the OLD call site being ABSENT, not only the new
behaviour being present: both could be true at once, and only the absence
proves the old surface no longer runs. The same goes for
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

# Every module the fast-track controls live in. None may call a dead dialog,
# and none may paste.
SHIP_MODULES = (
    "src/lib/laneActions.ts",
    "src/lib/runStart.ts",
    "src/lib/playbooks.ts",
    "src/components/grid/FastTrackMenu.tsx",
    "src/components/sidebar/SessionRowItems.tsx",
    "src/components/dialogs/FastTrackChoice.tsx",
    "src/components/dialogs/NewList.tsx",
    "src/components/dialogs/SplitCheck.tsx",
    "src/components/dialogs/CommitDialog.tsx",
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


# --- The paste playbooks and Ship & split are gone ------------------------------


def test_the_paste_surfaces_are_absent(js):
    # The old paste menu and its strings, by name.
    assert "src/components/grid/PlaybookMenu.tsx" not in js
    assert "Work with other sessions" not in js
    assert "pastes the prompt" not in js
    assert "Pastes the prompt into" not in js
    assert "function openPlaybookMenu(" not in js
    assert "function menuModel(" not in js
    assert "function askTargets(" not in js
    assert "function withSplit(" not in js
    assert '"playbook-menu"' not in js
    # No fast-track surface pastes, at the source and in the bundle.
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


def test_the_ship_and_split_menu_is_gone_everywhere(js):
    """The fork-icon menu, its "Ship it now", its lane rows and every door to
    it: the old CALL SITES are absent, not just the new control present."""
    assert "src/components/grid/ShipMenu.tsx" not in js
    assert "src/components/sidebar/PlaybookRowItems.tsx" not in js
    for gone in (
        "function ShipMenu(",
        "function PlaybookRowItems(",
        "function openShipMenu(",
        "function shipMenuModel(",
        "function runShipEntry(",
        "function shipEntries(",
        "function shipNow(",
        "function shipShown(",
        "var LANE_MENU = ",
        "setPlaybookMenu(",
        "playbookMenu",
        '"ship-menu"',
        '"act playbooks',
    ):
        assert gone not in js, gone
    # Its words, on any screen.
    for words in (
        "Ship & split",
        "Ship &amp; split",
        "Ship it now",
        "Ship: open a PR when done",
        "Ship: commit when done",
        "When it's done",
        "When each is done",
        "When it\\'s done",
        '"Leave it"',
        "“Leave it”",
        "its lane now",
        "Merge when checks pass",
    ):
        assert words not in js, words
    # ship-now is the Outbox's approval and nothing else's.
    assert js.count('"/ship-now"') == 1
    assert '"/ship-now"' in _region(js, "src/components/outbox/OutboxDialog.tsx")


def test_new_modules_never_call_a_dead_dialog(js):
    for mod in SHIP_MODULES:
        hits = DEAD_DIALOG.findall(_region(js, mod))
        assert not hits, f"{mod} calls {hits}, a no-op in Electron"
    # And at the source, where the bundle could have renamed nothing away.
    for mod in SHIP_MODULES:
        src = (ROOT / "frontend" / mod).read_text(encoding="utf-8")
        assert not DEAD_DIALOG.search(src), mod
    # The fast-track helpers that confirm()ed a merge are gone with the
    # one-press ⏩ (the picker's "Merge when green" is the explicit choice).
    for gone in (
        "function startFastTrack(",
        "function stopFastTrack(",
        "function resolveDepth(",
    ):
        assert gone not in js, gone
    assert "Fast-track will commit, push, open a PR and MERGE it" not in js
    assert 'alert("Commit failed: "' not in js


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


def test_fast_track_split_and_move_out_are_server_calls(js):
    lane = _function(js, "setLane")
    assert in_bundle('instApi(title, "/lane", { json: {', lane)
    assert "ask_first: askFirstApplies(lane) && askFirst" in lane
    pick = _function(js, "pickFastTrack")
    assert "setLane(title, lane, askFirst, opts)" in pick
    # The row flips at once and rolls back on a refusal.
    assert pick.count("patchInstance(title,") == 3
    # A copy window / a lead / a one-for-all member is locked in the picker.
    lock = _function(js, "laneLockReason")
    assert "drives this branch — set its fast-track from that window" in lock
    # Every run route goes through lib/runsApi's one path builder.
    out = _function(js, "moveOutOfGroup")
    assert 'taskPath(runId, taskId, "skip")' in out
    assert '"/api/runs/"' in _region(js, "src/lib/runsApi.ts")
    split = _function(js, "startSplitOf")
    assert in_bundle('api("/api/runs", { json: {', split)
    assert "split: true" in split and "lead: inst.title" in split
    assert in_bundle('grouping: "together"', split)
    # The row › menu and the palette split through one function.
    assert "startSplitOf(inst, name," in _function(js, "splitSession")
    assert "splitSession(inst, name)" in _region(
        js, "src/components/sidebar/SessionRowItems.tsx"
    )
    assert "splitSession(inst, name)" in _region(
        js, "src/components/palette/CommandPalette.tsx"
    )


def test_a_new_single_session_gets_its_fast_track_once_it_can_take_it(js):
    fn = _function(js, "setLaneWhenReady")
    assert 'if (lane === "leave") return true;' in fn
    # 409 "workspace not ready" is retried; anything else is said.
    assert "err.status === 409" in fn
    assert "Couldn't fast-track " in fn
    dlg = _region(js, "src/components/dialogs/NewSessionDialog.tsx")
    assert 'if (lane !== "leave") setLaneWhenReady(inst.title, lane, askFirst);' in dlg


# --- The ⏩ button and its picker -------------------------------------------------


def test_the_fast_track_button_names_the_target_and_opens_the_picker(js):
    pane = _region(js, "src/components/grid/Pane.tsx")
    assert "const ft = fastTrackStep(inst);" in pane
    assert in_bundle(
        'className: "nextstep nextstep-fast" + (ft.active ? " is-on" : "") + '
        '(ft.halted ? " nextstep-fast-halted" : "") + (ft.lane !== "leave" ? " is-set" : "") + '
        '(ftMenu ? " open" : "")',
        pane,
    )
    assert '"aria-haspopup": "menu"' in pane
    assert "useUi.getState().setFastTrackMenu(ftMenu ? null : { title });" in pane
    assert in_bundle("jsx)(FastTrackMenu, {", pane)
    assert 'className: "ft-ask"' in pane
    # A control, not a status chip (a .stagechip in a pane head is never drawn).
    btn = pane[pane.find('className: "nextstep nextstep-fast"') :]
    btn = btn[: btn.find("rs && ")]
    assert "stagechip" not in btn
    # The old one-press toggle is gone: a click never arms by itself.
    assert "ft.run()" not in pane
    assert "aria-pressed" not in btn
    step = _function(js, "fastTrackStep")
    assert in_bundle('label: "⏩ " + LANE_SHORT[lane] + (halted ? " ✗" : "")', step)
    assert "Click to change it or turn it off (Ctrl+K F)." in step
    short = js[js.find("var LANE_SHORT = {") :]
    short = short[: short.find("};")]
    for k, v in (
        ("leave", "off"),
        ("commit", "Commit"),
        ("push", "Push"),
        ("pr", "PR"),
        ("merge", "Merge"),
    ):
        assert f'{k}: "{v}"' in short, k


def test_the_picker_offers_every_rung_and_ask_first(js):
    menu = _region(js, "src/components/grid/FastTrackMenu.tsx")
    assert 'id: "fast-track-menu"' in menu
    assert '"⏩ Fast-track"' in menu
    assert '"When the agent is done, go as far as"' in menu
    assert "ASK_FIRST_LABEL" in menu and "fastTrackModel(inst || {})" in menu
    # Each pick is ONE call, through the server.
    assert "pickFastTrack(title, name, e.lane, cur.askFirst)" in menu
    assert "pickFastTrack(title, name, cur.lane, !model.ask.on)" in menu
    # The current rung is ticked and pre-selected.
    assert in_bundle("model.items.findIndex((i) => i.current)", menu)
    # Arrows, Enter, Esc and each item's letter.
    for key in ('"ArrowDown"', '"ArrowUp"', '"Escape"', '"Enter"'):
        assert key in menu, key
    assert "keyOf(x) === k.toUpperCase()" in menu
    # Nothing in it pastes or messages: Message… stays in the Thread.
    assert "threadOpen(" not in menu
    labels = js[js.find("var LANE_LABEL = {") :]
    labels = labels[: labels.find("};")]
    keys = js[js.find("var LANE_KEY = {") :]
    keys = keys[: keys.find("};")]
    for lane, label, key in (
        ("leave", "Off", "O"),
        ("commit", "Commit", "C"),
        ("push", "Push", "U"),
        ("pr", "Open a PR", "P"),
        ("merge", "Merge when green", "M"),
    ):
        assert f'{lane}: "{label}"' in labels, lane
        assert f'{lane}: "{key}"' in keys, lane
    # All five are offered — Push included (the old menu lacked it).
    assert in_bundle(
        'var LANE_ORDER = [ "leave", "commit", "push", "pr", "merge" ];', js
    )
    assert "var LANE_CHOICES = LANE_ORDER;" in js
    assert 'var ASK_FIRST_LABEL = "Ask me before it ships";' in js


def test_the_picker_holds_the_keyboard_like_a_modal(js):
    keymap = _region(js, "src/lib/keymap.ts")
    ids = keymap[keymap.find("MODAL_DOM_IDS = [") :]
    ids = ids[: ids.find("];")]
    assert '"fast-track-menu"' in ids
    assert '"ship-menu"' not in ids


def test_the_picker_items_are_announced(js):
    menu = _region(js, "src/components/grid/FastTrackMenu.tsx")
    assert '"aria-activedescendant": itemId(sel)' in menu
    assert "id: itemId(i)" in menu
    assert '"menuitemradio"' in menu and '"menuitemcheckbox"' in menu
    assert menu.count("tabIndex: -1") >= 2  # the menu + its items


# --- The other doors: row › menu, palette, chords ---------------------------------


def test_row_menu_items(js):
    row = _region(js, "src/components/sidebar/SidebarRow.tsx")
    assert in_bundle("jsx)(SessionRowItems, { inst })", row)
    group = _region(js, "src/components/sidebar/SessionRowItems.tsx")
    # Fast-track… opens THE picker, never a second copy of its choices.
    assert "openFastTrackMenu(title);" in group
    assert '"Fast-track…"' in group and '"Ctrl+K F"' in group
    assert "setLane(" not in group and "pickFastTrack(" not in group
    # Split is its own action; Move out is a member's; Message… stays.
    assert '"Split into parallel pieces…"' in group
    assert "detachableGroup(inst)" in group
    assert "moveOutOfGroup(group.id, group.task)" in group
    assert '"Move out of ", group.name' in group or "Move out of " in group
    assert in_bundle("threadOpen(title, { composeTo: title })", group)
    assert "if (inst.pending) return null;" in group
    assert in_bundle('className: "menu-sep"', group)
    assert "pb-row-sub" not in group and "When it" not in group


def test_palette_entries(js):
    palette = _region(js, "src/components/palette/CommandPalette.tsx")
    for label in (
        'label: "Start several sessions…"',
        "label: `Fast-track… — ${t}`",
        "label: `Split into parallel pieces… — ${t}`",
        "label: `Thread — ${t}`",
    ):
        assert in_bundle(label, palette), label
    assert 'run: () => ui.openNewWith("")' in palette
    assert "run: () => openFastTrackMenu(t)" in palette
    # The two hard-wired lane entries and the menu entry are gone.
    assert "Ship:" not in palette and "Ship & split" not in palette
    assert "setLane(" not in palette


def test_chords_s_f_t_and_no_l(js):
    keymap = _region(js, "src/lib/keymap.ts")
    assert in_bundle(
        's: { desc: "Message…", run: (t) => useUi.getState().threadOpen(t, { composeTo: t }) }',
        keymap,
    )
    assert in_bundle(
        'f: { desc: "Fast-track…", run: (t) => openFastTrackMenu(t) }', keymap
    )
    assert in_bundle(
        't: { desc: "Thread — workers and messages", run: (t) => useUi.getState().threadOpen(t) }',
        keymap,
    )
    # Ctrl+K L only ever opened the removed menu.
    chords = keymap[keymap.find("var CHORDS = {") :]
    chords = chords[: chords.find("\n};")]
    assert "\tl: {" not in chords and "\n  l: {" not in chords
    assert "(lane)" not in keymap and "openShipMenu" not in keymap


def test_review_chords_taken_by_an_older_rebinding(js):
    keymap = _region(js, "src/lib/keymap.ts")
    assert (
        "const cid = chordForKey(key.toLowerCase());" in keymap
    )  # Vite 8 inlines `pressed`
    assert "function chordShadowedBy(id)" in keymap
    sheet = _region(js, "src/components/palette/ShortcutsSheet.tsx")
    assert "chordShadowedBy(k)" in sheet and '" (taken)"' in sheet


# --- The dialogs draw the same choice ---------------------------------------------


def test_new_and_commit_dialogs_draw_one_fast_track_choice(js):
    choice = _region(js, "src/components/dialogs/FastTrackChoice.tsx")
    assert "function FastTrackChoice(" in choice and "function Seg(" in choice
    assert "lanes = LANE_CHOICES" in choice
    assert "LANE_LABEL[l]" in choice and "ASK_FIRST_LABEL" in choice
    new = _region(js, "src/components/dialogs/NewList.tsx")
    assert '"Fast-track each to" : "Fast-track to"' in new
    assert in_bundle("jsx)(FastTrackChoice, {", new)
    # THE default is Settings', for every shape: the preview's own is unread.
    assert "defaultLaneFor(o.fasttrackDefault)" in new
    assert "lane_default" not in new
    dlg = _region(js, "src/components/dialogs/NewSessionDialog.tsx")
    assert 'id: "new-ft-row"' in dlg
    assert (
        "fasttrackDefault: config?.fasttrack_default ?? config?.fasttrack_depth" in dlg
    )
    # The shared-folder radio turns fast-track off instead of being disabled.
    assert "(turns fast-track off — it commits for this session" in dlg
    assert 'if (laneNeedsWorktree) draft.setLane("leave");' in dlg
    assert "(not with a lane" not in dlg
    commit = _region(js, "src/components/dialogs/CommitDialog.tsx")
    assert '"Then fast-track to"' in commit
    assert in_bundle(
        "pickFastTrack(title, useUi.getState().aliases[title] || title, chosen.lane, chosen.askFirst, { message: m })",
        commit,
    )
    assert in_bundle('var AFTER_COMMIT = [ "leave", "push", "pr", "merge" ];', js)
    assert '"commit-depth"' not in commit and "Then keep going" not in commit
    assert "startFastTrack(" not in commit


def test_settings_holds_the_one_default_and_off_is_an_answer(js):
    ws = _region(js, "src/components/settings/screens/Workspace.tsx")
    assert '"Fast-track goes as far as"' in ws
    for value, label in (
        ("off", "Off"),
        ("commit", "Commit"),
        ("push", "Push"),
        ("pr", "Open a PR"),
        ("merge", "Merge when green"),
    ):
        assert in_bundle(f'value: "{value}", label: "{label}"', ws), value
    # The hint no longer calls it "where the ⏩ button stops" (⏩ opens a picker).
    assert "button stops" not in ws


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
        ".pane-head .actions .nextstep.nextstep-fast.open",
        ".pane-head .actions .nextstep-fast .ft-ask",
        ".pb-menu",
        ".pb-menu .pb-item.sel",
        ".pb-menu .ft-check",
        ".nf-split-pill",
        ".light .pb-menu",
        "#commit-form #commit-ft-row",
    ):
        assert sel in css, sel
    # A set target keeps the button on screen in a narrow pane.
    assert (
        ".pane-head .actions .nextstep.nextstep-fast:not(.is-on):not(.is-set):not(.nextstep-fast-halted)"
        in css
    )
    # The fork-icon button, the row's unfolded lanes and the Ask › picker went.
    for gone in (
        ".pane-head .act.playbooks",
        ".pb-row-sub",
        ".pb-sub",
        "#commit-depth",
    ):
        assert gone not in css, gone


def test_review_remote_rows_get_no_split_and_one_children_rule(js):
    """A remote device's row gets no Split or Move out (only
    /api/instances/dev::… is forwarded, so the group and split routes would
    404); its ⏩ stays, since /lane is forwarded. One "children of" rule for
    every surface."""
    remote = _function(js, "isRemote")
    assert 'String(inst.title || "").includes("::")' in remote
    group = _region(js, "src/components/sidebar/SessionRowItems.tsx")
    assert "const remote = isRemote(inst);" in group
    assert 'const splitWhy = remote ? "" : splitBlockReason' in group
    assert "const group = remote ? null : detachableGroup(inst);" in group
    palette = _region(js, "src/components/palette/CommandPalette.tsx")
    assert '!inst.device && !inst.pending && !t.includes("::")' in palette
    rule = _function(js, "isChildOf")
    assert in_bundle(
        'String(row.parent || "") === title && !row.pending && !row.device', rule
    )
    assert "childrenOf(title, rows)" in _function(js, "familyOf")
    assert "isChildOf(r, p)" in _function(js, "childrenByParent")
