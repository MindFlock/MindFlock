/** Fast-track (the ONE "how far does this session go" control) and what is
 * left of the playbook UI, pinned at the source modules that own each piece.
 * Moved from tests/unit/test_frontend_playbooks.py, which grepped the built
 * bundle; the pure half was already pinned here (fastTrack.test.ts,
 * autopilot.test.ts, newList.test.ts).
 *
 * The pane's fork-icon menu was first "Work with other sessions" (four
 * playbooks that PASTED a prompt), then "Ship & split" (lanes, ship now,
 * split, move out, Message…). Both are gone. Fast-track is now one control
 * under one name: the pane head's ⏩ button names the target and opens the
 * picker (grid/FastTrackMenu.tsx — Off / Commit / Push / Open PR / Merge when
 * green, and "Ask me before it ships"), every item a
 * `POST /api/instances/{t}/lane`. The row › menu (sidebar/SessionRowItems.tsx),
 * the palette and Ctrl+K F open that same picker; Split into parallel
 * pieces… and Move out of a group are row › menu (and palette) actions of
 * their own; the New and Commit dialogs draw the same choices
 * (dialogs/FastTrackChoice.tsx).
 *
 * Every removal is pinned as the OLD call site being ABSENT, not only the new
 * behaviour being present: both could be true at once, and only the absence
 * proves the old surface no longer runs. The same goes for
 * `window.prompt`/`confirm`/`alert`, which Electron never implements.
 *
 * What still pastes, on purpose until split runs merge pieces back
 * themselves: the Thread tab's worker buttons and the rail's wrap-up chip
 * (`runPlaybook` / `pastePlaybook`), pinned below so a change there is
 * deliberate.
 *
 * Text assertions run on the source with comments stripped (as the bundle
 * had them), and whitespace is the formatter's business: `norm` collapses it
 * and forgets the space/trailing comma a line break leaves inside brackets —
 * token order, quoting and spelling still have to match. */

/// <reference types="vite/client" />
import { describe, expect, it } from "vitest";
import { ALL_CSS, CSS, SOURCES, SRC, dropComments, has, norm, src } from "./sourceText";

/** One module's code (comments removed), failing loudly if it is gone. */
const mod = (path: string): string => dropComments(src(path));

/** All app code, comments removed (what "not in the bundle" meant). */
const ALL = Object.values(SRC).map(dropComments).join("\n");

/** The text of a top-level function, from its `function name(` to its closing
 * column-0 brace. */
function fn(name: string): string {
  const re = new RegExp("^(?:export )?(?:async )?function " + name + "\\b[<(]", "m");
  const hits = Object.values(SRC)
    .map(dropComments)
    .filter((t) => re.test(t));
  expect(hits.length, `function ${name}`).toBe(1);
  const t = hits[0];
  const start = t.search(re);
  const end = t.indexOf("\n}\n", start);
  return t.slice(start, end + 2);
}

// Silent no-ops in Electron. A word boundary so `sendMessagePrompt(` doesn't
// count, and an optional `window.` because both spellings reach the same API.
const DEAD_DIALOG = /(?<![\w.$])(?:window\.)?(?:prompt|confirm|alert)\(/;

// Every module the fast-track controls live in. None may call a dead dialog,
// and none may paste.
const SHIP_MODULES = [
  "lib/laneActions.ts",
  "lib/runStart.ts",
  "lib/playbooks.ts",
  "components/grid/FastTrackMenu.tsx",
  "lib/sessionMenu.ts",
  "components/sidebar/SessionMenu.tsx",
  "components/dialogs/FastTrackChoice.tsx",
  "components/dialogs/NewList.tsx",
  "components/dialogs/SplitCheck.tsx",
  "components/dialogs/CommitDialog.tsx",
  "components/palette/CommandPalette.tsx",
];
const NO_PASTE_MODULES = SHIP_MODULES.filter((m) => m !== "lib/playbooks.ts");

// NewSessionDialog.tsx and the modules it was split into, the dialog first.
const NEW_DIALOG_MODULES = [
  "components/dialogs/NewSessionDialog.tsx",
  "components/dialogs/newSession.ts",
  "components/dialogs/newSession/DescribeSection.tsx",
  "components/dialogs/newSession/TemplatesStrip.tsx",
  "components/dialogs/newSession/FolderPicker.tsx",
  "components/dialogs/newSession/FolderBrowser.tsx",
  "components/dialogs/newSession/AgentAccount.tsx",
  "components/dialogs/newSession/WorkspaceFold.tsx",
  "components/dialogs/newSession/PromptFold.tsx",
  "components/dialogs/newSession/LaunchFold.tsx",
  "components/dialogs/newSession/FooterActions.tsx",
  "lib/newSessionSubmit.ts",
].map((p) => "" + p);
const newDialog = () => NEW_DIALOG_MODULES.map(mod).join("\n");

const PANE = "components/grid/Pane.tsx";
const MENU = "components/grid/FastTrackMenu.tsx";
// The session's one menu (row ⋯ and pane ⋯ draw the same model).
const ROW_ITEMS = "lib/sessionMenu.ts";
const TABLE = "lib/sessionActionTable.ts";
const PALETTE = "components/palette/CommandPalette.tsx";
const KEYMAP = "lib/keymap.ts";

/** The ids in keymap's POPOVER_IDS (dialogs-like popovers that hold the keyboard). */
function popoverIds(): string {
  const k = mod(KEYMAP);
  const ids = k.slice(k.indexOf("POPOVER_IDS = ["));
  return ids.slice(0, ids.indexOf("];"));
}

const { LANE_CHOICES, LANE_KEY, LANE_LABEL, LANE_ORDER, LANE_SHORT, laneDefault } = await import("../lib/autopilot");
const { ASK_FIRST_LABEL } = await import("../lib/laneActions");
const { defaultLaneFor } = await import("../lib/runStart");
const { isRemote, suggestionPill } = await import("../lib/playbooks");
const { isChildOf, childrenByParent } = await import("../lib/agentMessages");
const { familyOf } = await import("../lib/thread");
const { SESSION_ACTIONS, sessionAction } = await import("../lib/sessionActionTable");

// --- The paste playbooks and Ship & split are gone ------------------------------

describe("the paste playbooks and Ship & split are gone", () => {
  it("the paste surfaces are absent", () => {
    // The old paste menu and its strings, by name.
    expect(SOURCES).not.toHaveProperty(["components/grid/PlaybookMenu.tsx"]);
    for (const gone of [
      "Work with other sessions",
      "pastes the prompt",
      "Pastes the prompt into",
      "function openPlaybookMenu(",
      "function menuModel(",
      "function askTargets(",
      "function withSplit(",
      '"playbook-menu"',
    ])
      expect(ALL, gone).not.toContain(gone);
    // No fast-track surface pastes — checked on the raw source, comments too.
    for (const m of NO_PASTE_MODULES) {
      expect(SRC[m], m).toBeDefined();
      expect(SRC[m], m).not.toContain("pastePlaybook(");
      expect(SRC[m], m).not.toContain("runPlaybook(");
    }
    // The New dialog no longer sends the split PLAYBOOK on a create.
    const dlg = newDialog();
    expect(dlg).not.toContain('playbook: "split"');
    expect(dlg).not.toContain("withSplit(");
  });

  it("the Ship & split menu is gone everywhere: the old CALL SITES are absent, not just the new control present", () => {
    expect(SOURCES).not.toHaveProperty(["components/grid/ShipMenu.tsx"]);
    expect(SOURCES).not.toHaveProperty(["components/sidebar/PlaybookRowItems.tsx"]);
    for (const gone of [
      "function ShipMenu(",
      "function PlaybookRowItems(",
      "function openShipMenu(",
      "function shipMenuModel(",
      "function runShipEntry(",
      "function shipEntries(",
      "function shipNow(",
      "function shipShown(",
      "LANE_MENU",
      "setPlaybookMenu(",
      "playbookMenu",
      '"ship-menu"',
      '"act playbooks',
    ])
      expect(ALL, gone).not.toContain(gone);
    // Its words, on any screen.
    for (const words of [
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
    ])
      expect(ALL, words).not.toContain(words);
    // ship-now is the ask-first approval (the bell's waiting row) and nothing
    // else's.
    expect(ALL.split('"/ship-now"').length - 1).toBe(1);
    expect(mod("components/outbox/WaitingRow.tsx")).toContain('"/ship-now"');
  });

  it("no fast-track module calls a dead dialog", () => {
    // Raw source, comments included: nothing may even look like the call.
    for (const m of SHIP_MODULES) {
      expect(SRC[m], m).toBeDefined();
      expect(SRC[m].match(DEAD_DIALOG), `${m} calls a dead dialog, a no-op in Electron`).toBeNull();
    }
    // The fast-track helpers that confirm()ed a merge are gone with the
    // one-press ⏩ (the picker's "Merge when green" is the explicit choice).
    for (const gone of ["function startFastTrack(", "function stopFastTrack(", "function resolveDepth("])
      expect(ALL, gone).not.toContain(gone);
    expect(ALL).not.toContain("Fast-track will commit, push, open a PR and MERGE it");
    expect(ALL).not.toContain('alert("Commit failed: "');
  });

  it("the palette's Message… and Queue prompt… no longer use window.prompt", () => {
    // The OLD call sites are gone, by their text and by their function names.
    for (const gone of [
      "Send a message to ",
      "Queue a prompt for ",
      "function sendMessagePrompt(",
      "function queuePromptPrompt(",
    ])
      expect(ALL, gone).not.toContain(gone);
    expect(SRC[PALETTE].match(DEAD_DIALOG), "the palette calls a dead dialog").toBeNull();
    expect(SRC[TABLE].match(DEAD_DIALOG), "the action table calls a dead dialog").toBeNull();
    // The entries stay (the table's Message… and Queue prompt… rows, which the
    // palette lists), and now open the place the text is typed.
    const palette = mod(PALETTE);
    expect(palette).toContain('"message",');
    expect(palette).toContain('"queue",');
    const table = mod(TABLE);
    expect(has(table, "run: (t) => useUi.getState().threadOpen(t, { composeTo: t })")).toBe(true);
    expect(has(table, 'label: "Queue prompt…",')).toBe(true);
    expect(has(table, "run: (t) => focusQueueInput(t)")).toBe(true);
    expect(SESSION_ACTIONS.queue.label).toBe("Queue prompt…");
  });

  it("Queue prompt… focuses the Queue tab's textarea", () => {
    const f = fn("focusQueueInput");
    expect(has(f, 'setLastTab(title, "queue")')).toBe(true);
    expect(f).toContain('".pane-queue .queue-input"');
    expect(f).toContain("box.focus()");
  });
});

// --- Every item is a server call -------------------------------------------------

describe("every item is a server call", () => {
  it("fast-track, split and move out", () => {
    const lane = fn("setLane");
    expect(has(lane, 'instApi<LaneResult>(title, "/lane", { json: {')).toBe(true);
    expect(lane).toContain("ask_first: askFirstApplies(lane) && askFirst");
    const pick = fn("pickFastTrack");
    expect(pick).toContain("setLane(title, lane, askFirst, opts)");
    // The row flips at once and rolls back on a refusal.
    expect(pick.split("patchInstance(title,").length - 1).toBe(3);
    // A copy window / a lead / a one-for-all member is locked in the picker.
    expect(fn("laneLockReason")).toContain("drives this branch — set its fast-track from that session");
    // Every run route goes through lib/runsApi's one path builder.
    expect(fn("moveOutOfGroup")).toContain('taskPath(runId, taskId, "skip")');
    expect(mod("lib/runsApi.ts")).toContain('"/api/runs/"');
    const split = fn("startSplitOf");
    expect(has(split, 'api("/api/runs", { json: {')).toBe(true);
    expect(split).toContain("split: true");
    expect(split).toContain("lead: inst.title");
    expect(has(split, 'grouping: "together"')).toBe(true);
    // The row › menu and the palette (through the action table) split
    // through one function.
    expect(fn("splitSession")).toContain("startSplitOf(inst, name,");
    expect(mod(ROW_ITEMS)).toContain("splitSession(inst, name)");
    expect(mod(TABLE)).toContain("splitSession(inst, name)");
    expect(mod(PALETTE)).toContain('"split",');
  });

  it("a new single session gets its fast-track once it can take it", () => {
    const f = fn("setLaneWhenReady");
    expect(f).toContain('if (lane === "leave") return true;');
    // 409 "workspace not ready" is retried; anything else is said.
    expect(f).toContain("err.status === 409");
    expect(f).toContain("Couldn't fast-track ");
    expect(newDialog()).toContain('if (lane !== "leave") void setLaneWhenReady(inst.title, lane, askFirst);');
  });
});

// --- The ⏩ button and its picker -------------------------------------------------

describe("the ⏩ button and its picker", () => {
  it("the button names the target and opens the picker", () => {
    const pane = mod(PANE);
    expect(pane).toContain("const ft = fastTrackStep(inst);");
    expect(
      has(
        pane,
        'className={ "nextstep nextstep-fast" + (ft.active ? " is-on" : "") + ' +
          '(ft.halted ? " nextstep-fast-halted" : "") + (ft.lane !== "leave" ? " is-set" : "") + ' +
          '(ftMenu ? " open" : "") }'
      )
    ).toBe(true);
    expect(pane).toContain('aria-haspopup="menu"');
    expect(pane).toContain("useUi.getState().setFastTrackMenu(ftMenu ? null : { title });");
    expect(pane).toContain("<FastTrackMenu");
    expect(pane).toContain('className="ft-ask"');
    // A control, not a status chip (a .stagechip in a pane head is never drawn).
    let btn = pane.slice(pane.indexOf('"nextstep nextstep-fast"'));
    btn = btn.slice(0, btn.indexOf("{rs && "));
    expect(btn.length).toBeGreaterThan(0);
    expect(btn).not.toContain("stagechip");
    // The old one-press toggle is gone: a click never arms by itself.
    expect(pane).not.toContain("ft.run()");
    expect(btn).not.toContain("aria-pressed");
    const step = fn("fastTrackStep");
    expect(has(step, 'label: "⏩ " + LANE_SHORT[lane] + (halted ? " ✗" : "")')).toBe(true);
    expect(step).toContain("Click to change it or turn it off (Ctrl+K F).");
    expect(LANE_SHORT).toEqual({ leave: "off", commit: "Commit", push: "Push", pr: "PR", merge: "Merge" });
  });

  it("the picker offers every rung and ask-first", () => {
    const menu = mod(MENU);
    expect(menu).toContain('id="fast-track-menu"');
    expect(menu).toContain("<b>⏩ Fast-track</b>");
    expect(menu).toContain(">When the agent is done, go as far as<");
    expect(menu).toContain("ASK_FIRST_LABEL");
    expect(menu).toContain("fastTrackModel(inst || {})");
    // Each pick is ONE call, through the server.
    expect(menu).toContain("pickFastTrack(title, name, e.lane, cur.askFirst)");
    expect(menu).toContain("pickFastTrack(title, name, cur.lane, !model.ask.on)");
    // The current rung is ticked and pre-selected.
    expect(has(menu, "model.items.findIndex((i) => i.current)")).toBe(true);
    // Arrows, Enter, Esc and each item's letter.
    for (const key of ['"ArrowDown"', '"ArrowUp"', '"Escape"', '"Enter"']) expect(menu, key).toContain(key);
    expect(menu).toContain("keyOf(x) === k.toUpperCase()");
    // Nothing in it pastes or messages: Message… stays in the Thread.
    expect(menu).not.toContain("threadOpen(");
    expect(LANE_LABEL).toEqual({ leave: "Off", commit: "Commit", push: "Push", pr: "Open PR", merge: "Merge when green" });
    expect(LANE_KEY).toEqual({ leave: "O", commit: "C", push: "U", pr: "P", merge: "M" });
    // All five are offered — Push included (the old menu lacked it).
    expect(LANE_ORDER).toEqual(["leave", "commit", "push", "pr", "merge"]);
    expect(LANE_CHOICES).toBe(LANE_ORDER);
    expect(ASK_FIRST_LABEL).toBe("Ask me before it ships");
  });

  it("the picker holds the keyboard like a modal", () => {
    const ids = popoverIds();
    expect(ids).toContain('"fast-track-menu"');
    expect(ids).not.toContain('"ship-menu"');
  });

  it("the bell holds the keyboard like a modal", () => {
    // The waiting rows (Retry / Skip / Commit) live in the bell's popover; a
    // Delete or Ctrl+W on one of its buttons ended the focused session behind
    // it until the popover joined the modal guard.
    expect(popoverIds()).toContain('"notif-pop"');
    expect(mod("components/NotificationsBell.tsx")).toContain('id="notif-pop"');
  });

  it("the picker survives its own pane head scrolling", () => {
    // Found with the screenshot harness: on a busy header (a running run's
    // live step) a click on ⏩ scrolls the pane head sideways to show it, and
    // the picker's close-on-any-scroll shut it the instant it opened. A
    // scroll of something that HOLDS the button now repositions the picker;
    // any other scroll still closes it. And ⏩ sits right after the guided
    // button, ahead of the live step's free text, so a scrolled-away target
    // is rare anyway.
    const menu = mod(MENU);
    expect(
      has(menu, "if (t instanceof Node && t !== document && t.contains(anchor)) { placeRef.current(); return; }")
    ).toBe(true);
    expect(menu).toContain("useLayoutEffect(() => placeRef.current())");
    const pane = norm(mod(PANE));
    const guided = pane.indexOf('"nextstep" + (shownStep.hint ?');
    const fast = pane.indexOf('"nextstep nextstep-fast"');
    const live = pane.indexOf('className={"stepnow is-"');
    expect(guided).toBeGreaterThanOrEqual(0);
    expect(guided).toBeLessThan(fast);
    expect(fast).toBeLessThan(live);
  });

  it("the picker items are announced", () => {
    const menu = mod(MENU);
    expect(menu).toContain("aria-activedescendant={itemId(sel)}");
    expect(menu).toContain("id={itemId(i)}");
    expect(menu).toContain('"menuitemradio"');
    expect(menu).toContain('"menuitemcheckbox"');
    expect(menu.split("tabIndex={-1}").length - 1).toBeGreaterThanOrEqual(2); // the menu + its items
  });
});

// --- The other doors: row › menu, palette, chords ---------------------------------

describe("the other doors: row › menu, palette, chords", () => {
  it("row › menu items", () => {
    expect(mod("components/sidebar/SidebarRow.tsx")).toContain("<SessionMenu");
    expect(mod(PANE)).toContain("<SessionMenu");
    const group = mod(ROW_ITEMS);
    // Fast-track… opens THE picker (the table's row), never a second copy of
    // its choices.
    expect(group).toContain('run: () => sessionAction("fastTrack").run(title)');
    expect(group).toContain('label: "Fast-track…"');
    expect(group).toContain('keys: sessionAction("fastTrack").keys');
    expect(SESSION_ACTIONS.fastTrack).toMatchObject({ label: "Fast-track…", keys: "Ctrl+K F", chord: "f" });
    expect(has(mod(TABLE), 'label: "Fast-track…", keys: "Ctrl+K F", chord: "f",')).toBe(true);
    expect(mod(TABLE)).toContain("run: (t) => openFastTrackMenu(t)");
    expect(group).toContain("LANE_SHORT[laneChoice(inst).lane]");
    expect(group).not.toContain("setLane(");
    expect(group).not.toContain("pickFastTrack(");
    // Split is its own action; Move out is a member's; Message… stays.
    expect(group).toContain('label: "Split into parallel pieces…"');
    expect(group).toContain("detachableGroup(inst)");
    expect(group).toContain("moveOutOfGroup(group.id, group.task)");
    expect(group).toContain('label: "Move out of " + group.name');
    expect(group).toContain('fromTable("message")');
    expect(group).toContain("!inst.pending");
    expect(group).not.toContain("pb-row-sub");
    expect(group).not.toContain("When it");
  });

  it("palette entries", () => {
    const palette = mod(PALETTE);
    const table = mod(TABLE);
    // The palette lists the table's rows as "<label> — <session>", the
    // session named the way its rail row reads.
    expect(has(palette, "label: `${actionLabel(id, c)} — ${name}`")).toBe(true);
    expect(has(palette, "const name = windowName(t);")).toBe(true);
    for (const [id, label] of [
      ["fastTrack", 'label: "Fast-track…"'],
      ["split", 'label: "Split into parallel pieces…"'],
      ["thread", 'label: "Thread"'],
    ]) {
      expect(palette, id).toContain(`"${id}",`);
      expect(has(table, label), label).toBe(true);
    }
    expect(SESSION_ACTIONS.split.label).toBe("Split into parallel pieces…");
    expect(SESSION_ACTIONS.thread.label).toBe("Thread");
    // "Start several sessions…" was New under a second name (its box takes a
    // list on its own) — one action, one entry.
    expect(palette).not.toContain("Start several sessions…");
    expect(palette).not.toContain('openNewWith("")');
    expect(table).toContain("run: (t) => openFastTrackMenu(t)");
    // The two hard-wired lane entries and the menu entry are gone.
    expect(palette).not.toContain("Ship:");
    expect(palette).not.toContain("Ship & split");
    expect(palette).not.toContain("setLane(");
  });

  it("chords S, F, T and no L", () => {
    const keymap = mod(KEYMAP);
    const table = mod(TABLE);
    // The chords are the action table's rows that name a letter.
    expect(keymap).toMatch(/export const CHORDS: Record<string, ChordEntry> = \{\s*\.\.\.Object\.fromEntries\(\s*CHORD_ACTIONS\.map\(/);
    expect(
      has(
        table,
        'label: "Message…", keys: "Ctrl+K S", chord: "s", enabled: () => true, ' +
          "run: (t) => useUi.getState().threadOpen(t, { composeTo: t })"
      )
    ).toBe(true);
    expect(has(table, 'label: "Fast-track…", keys: "Ctrl+K F", chord: "f",')).toBe(true);
    expect(has(table, 'label: "Thread", keys: "Ctrl+K T", chord: "t", note: "workers and messages",')).toBe(true);
    expect(SESSION_ACTIONS.message).toMatchObject({ label: "Message…", keys: "Ctrl+K S", chord: "s" });
    expect(SESSION_ACTIONS.thread).toMatchObject({
      label: "Thread",
      keys: "Ctrl+K T",
      chord: "t",
      note: "workers and messages",
    });
    // Ctrl+K L only ever opened the removed menu.
    expect(table).not.toContain('chord: "l"');
    expect(Object.keys(SESSION_ACTIONS).map((id) => sessionAction(id as never).chord)).not.toContain("l");
    expect(keymap).not.toContain("(lane)");
    expect(keymap).not.toContain("openShipMenu");
  });

  it("review: chords taken by an older rebinding", () => {
    const keymap = mod(KEYMAP);
    expect(has(keymap, "const pressed = key.toLowerCase(); const cid = chordForKey(pressed);")).toBe(true);
    expect(keymap).toContain("function chordShadowedBy(id: string)");
    const sheet = mod("components/palette/ShortcutsSheet.tsx");
    expect(sheet).toContain("chordShadowedBy(k)");
    expect(sheet).toContain('" (taken)"');
  });
});

// --- The dialogs draw the same choice ---------------------------------------------

describe("the dialogs draw the same choice", () => {
  it("New and Commit draw one fast-track choice", () => {
    const choice = mod("components/dialogs/FastTrackChoice.tsx");
    expect(choice).toContain("function FastTrackChoice(");
    expect(choice).toMatch(/function Seg<T extends string>\(/);
    expect(choice).toContain("lanes = LANE_CHOICES");
    expect(choice).toContain("LANE_LABEL[l]");
    expect(choice).toContain("ASK_FIRST_LABEL");
    const nl = mod("components/dialogs/NewList.tsx");
    expect(nl).toContain('"Fast-track each to" : "Fast-track to"');
    expect(nl).toContain("<FastTrackChoice");
    // THE default is Settings', for every shape: the preview's own is unread.
    // "Off unless I pick": a single session starts Off, a batch on Settings'
    // default (Off when unset).
    expect(nl).toContain("const batch = !o.single && (listMode || o.split);");
    // Typing never makes a list: only a New opened as a batch reads one.
    expect(nl).toContain("const listMode = o.batch && items.length > 0;");
    expect(nl).toContain("defaultLaneFor(batch, o.fasttrackDefault)");
    expect(nl).not.toContain("lane_default");
    expect(fn("defaultLaneFor")).toContain('return batch ? laneDefault(setting) : "leave";');
    expect(fn("laneDefault")).toContain('return normalizeLane(setting) || "leave";');
    // …and so they behave.
    expect(defaultLaneFor(false, "merge")).toBe("leave");
    expect(defaultLaneFor(true, "merge")).toBe("merge");
    expect(defaultLaneFor(true, undefined)).toBe("leave");
    expect(laneDefault("bogus")).toBe("leave");
    expect(laneDefault(" PR ")).toBe("pr");
    const dlg = newDialog();
    expect(dlg).toContain('id="new-ft-row"');
    // Never the resolved fasttrack_depth (it reads "pr" when nothing is set),
    // and the "Set it up myself" form is a single session.
    expect(dlg).toContain("fasttrackDefault: config?.fasttrack_default,");
    expect(dlg).not.toContain("config?.fasttrack_depth");
    expect(dlg).toContain("single: page !== 1");
    // The shared-folder radio turns fast-track off instead of being disabled.
    expect(dlg).toContain("Turns fast-track off (it commits for this session");
    expect(dlg).toContain('if (laneNeedsWorktree) draft.setLane("leave");');
    expect(dlg).not.toContain("(not with a lane");
    const commit = mod("components/dialogs/CommitDialog.tsx");
    expect(commit).toContain(">Then fast-track to<");
    expect(
      has(
        commit,
        "pickFastTrack(title, useUi.getState().aliases[title] || title, chosen.lane, chosen.askFirst, { message: m })"
      )
    ).toBe(true);
    expect(commit).toContain('const AFTER_COMMIT: readonly Lane[] = ["leave", "push", "pr", "merge"];');
    expect(commit).not.toContain('"commit-depth"');
    expect(commit).not.toContain("Then keep going");
    expect(commit).not.toContain("startFastTrack(");
  });

  it("Settings holds the one default, Off out of the box", () => {
    const ws = mod("components/settings/screens/Workspace.tsx");
    expect(ws).toContain(">Fast-track goes as far as<");
    // Unset is Off, and the screen says who reads it.
    expect(has(ws, '{ value: "", label: "Off (default)" }')).toBe(true);
    expect(ws).not.toContain("Open a PR (default)");
    expect(ws).toContain(">New sessions start Off; batches and ticket runs start at this default.<");
    for (const [value, label] of [
      ["commit", "Commit"],
      ["push", "Push"],
      ["pr", "Open PR"],
      ["merge", "Merge when green"],
    ])
      expect(has(ws, `{ value: "${value}", label: "${label}" }`), value).toBe(true);
    // The hint no longer calls it "where the ⏩ button stops" (⏩ opens a picker).
    expect(ws).not.toContain("button stops");
  });
});

// --- What still pastes (Thread buttons, the wrap-up chip) ------------------------

describe("what still pastes", () => {
  it("is rendered, then typed — never submitted", () => {
    const guard = fn("pastePlaybook");
    expect(guard).toContain("if (pasting.has(title)) return false;");
    expect(guard).toContain("pasting.delete(title)");
    const now = fn("pasteNow");
    expect(has(now, '"/api/playbooks/" + encodeURIComponent(pb.id) + "/render"')).toBe(true);
    expect(
      has(
        now,
        'api("/api/instances/" + encodeURIComponent(title) + "/send", { json: { text, submit: false, dialog_safe: true } })'
      )
    ).toBe(true);
    expect(fn("runPlaybook")).toContain("pb.disabled_reason");
  });
});

// --- Store: threadOpen and the "thread" tab -------------------------------------

describe("the Thread tab", () => {
  it("threadOpen and its last-seen marks", () => {
    const store = mod("state/store.ts");
    expect(has(store, "_selectSession(title, { noKeyboard: true })")).toBe(true);
    expect(has(store, 'get().setLastTab(title, "thread")')).toBe(true);
    expect(has(store, "to: opts?.composeTo || title")).toBe(true);
    expect(store).toMatch(/threadLastSeen: load<Record<string, number>>\("mf_thread_seen", \{\}\)/);
    expect(store).toContain('save("mf_thread_seen", threadLastSeen)');
    expect(mod("lib/sessionActions.ts")).toContain("setSessionSelector(selectSession);");
  });

  it("the pane draws the thread tab and falls back to Agent", () => {
    const pane = mod(PANE);
    expect(pane).toContain(
      'const BODY_TABS: ReadonlySet<string> = new Set(["agent", "shell", "diff", "queue", "map", "thread"]);'
    );
    expect(fn("paneTab")).toContain('if (!BODY_TABS.has(t)) return "agent";');
  });
});

// --- New dialog: auto-split is an OPTIONAL split RUN ----------------------------

describe("New dialog: auto-split is an optional split run", () => {
  it("the split check", () => {
    const dlg = newDialog();
    // One box, on the form (page 2) beside Fast-track; page 1 is only the
    // prompt ("I should be able to just prompt and it fills out the second
    // menu"), so its options appear for an Intake batch alone.
    expect(dlg).toContain('id="new-split"');
    expect(dlg).not.toContain('id="new-split-prompt"');
    expect(dlg.indexOf('id="new-ft-row"')).toBeGreaterThanOrEqual(0);
    expect(dlg.indexOf('id="new-ft-row"')).toBeLessThan(dlg.indexOf('id="new-split-row"'));
    expect(dlg).toContain("splitBox={null}");
    let reset = dlg.slice(dlg.indexOf("const resetForm = (prefill: string) => {"));
    reset = reset.slice(0, reset.indexOf('folderDo({ t: "reopen" });'));
    expect(reset).toContain("setSplit(false);");
    expect(reset).toContain("setMaxPieces(SPLIT_DEFAULT);");
    // Only Intake's prefill opens New as a batch; typed lines are one prompt.
    expect(reset).toContain("setBatch(!!prefill);");
    // Gated by the agent, never by the box's line breaks; a batch has no box.
    expect(has(dlg, "splitGate(config?.caps, canonAgent(program))")).toBe(true);
    expect(dlg).toContain("const splitOn = page === 2 && split && mcpOk.ok;");
    expect(dlg).toContain("const runMode = (page === 1 && draft.listMode) || splitOn;");
    // The form's Prompt is the lead's task, and an empty one is refused there.
    expect(dlg).toContain("text: page === 1 ? describe : prompt,");
    expect(dlg).toContain("Auto-split needs a task");
    expect(ALL).not.toContain("splitShapeReason");
    // A split starts through the run with its N, and its button stays
    // "Create session" — it may well not split.
    expect(dlg).toContain("split: splitOn,");
    expect(dlg).toContain("maxPieces: clampSplit(maxPieces, splitLimit)");
    expect(ALL).not.toContain("Start the lead");
  });

  it("the split check's copy and pill", () => {
    const box = mod("components/dialogs/SplitCheck.tsx");
    expect(box).toMatch(/>\s*Auto-split into up to\s*</);
    expect(box).toContain(">sessions if it's worth it<");
    expect(ALL).not.toContain("Split a big line");
    expect(box).not.toContain("Split across workers");
    expect(box).toContain("suggestionPill(sug)");
    // What the tick does is said once, by the summary sentence.
    expect(box).not.toContain("nf-split-nudge");
    expect(has(fn("summarySentence"), "approve the split in its Thread tab")).toBe(true);
    expect(has(fn("suggestionPill"), '["suggested", ...sug.pieces].join(" · ")')).toBe(true);
    expect(suggestionPill({ pieces: ["billing", "search"] })).toBe("suggested · billing · search");
  });
});

describe("styles", () => {
  it("the picker, the button and the split pill are styled; the removed menus' rules are gone", () => {
    const owners: [string, string][] = [
      [".pane-head .actions .nextstep.nextstep-fast.open", "components/grid/FastTrackMenu.css"],
      [".pane-head .actions .nextstep-fast .ft-ask", "components/grid/FastTrackMenu.css"],
      [".pb-menu", "components/grid/FastTrackMenu.css"],
      [".pb-menu .pb-item.sel", "components/grid/FastTrackMenu.css"],
      [".pb-menu .ft-check", "components/grid/FastTrackMenu.css"],
      [".nf-split-pill", "components/dialogs/NewSessionDialog.css"],
      [".light .pb-menu", "styles/theme-light.css"],
      ["#commit-form #commit-ft-row", "components/dialogs/smallDialogs.css"],
      // A set target keeps the button on screen in a narrow pane.
      [
        ".pane-head .actions .nextstep.nextstep-fast:not(.is-on):not(.is-set):not(.nextstep-fast-halted)",
        "components/grid/Pane.css",
      ],
    ];
    for (const [sel, file] of owners) {
      expect(CSS[file], file).toBeDefined();
      expect(CSS[file], sel).toContain(sel);
    }
    // The fork-icon button, the row's unfolded lanes and the Ask › picker went.
    for (const gone of [".pane-head .act.playbooks", ".pb-row-sub", ".pb-sub", "#commit-depth"])
      expect(ALL_CSS, gone).not.toContain(gone);
  });
});

describe("review: remote rows get no split, and one children-of rule", () => {
  it("a remote device's row gets no Split or Move out; ⏩ stays", () => {
    // Only /api/instances/dev::… is forwarded, so the group and split routes
    // would 404; /lane is forwarded.
    expect(fn("isRemote")).toContain('String(inst.title || "").includes("::")');
    expect(isRemote({ title: "rig::api" })).toBe(true);
    expect(isRemote({ title: "api", device: "rig" })).toBe(true);
    expect(isRemote({ title: "api" })).toBe(false);
    const group = mod(ROW_ITEMS);
    expect(group).toContain("const remote = isRemote(inst);");
    expect(group).toContain('const splitWhy = remote ? "" : splitBlockReason');
    expect(group).toContain("const group = remote ? null : detachableGroup(inst);");
    expect(mod(TABLE)).toContain('!c.inst!.device && !c.title.includes("::")');
  });

  it("one 'children of' rule for every surface", () => {
    expect(has(fn("isChildOf"), 'String(row.parent || "") === title && !row.pending && !row.device')).toBe(true);
    expect(fn("familyOf")).toContain("childrenOf(title, rows)");
    expect(fn("childrenByParent")).toContain("isChildOf(r, p)");
    // …and the surfaces agree on it.
    const rows = [
      { title: "lead" },
      { title: "w1", parent: "lead" },
      { title: "w2", parent: "lead", pending: true },
      { title: "w3", parent: "lead", device: "rig" },
    ];
    expect(rows.filter((r) => isChildOf(r, "lead")).map((r) => r.title)).toEqual(["w1"]);
    expect(familyOf("lead", rows).children.map((r) => r.title)).toEqual(["w1"]);
    expect(childrenByParent(rows).get("lead")?.map((r) => r.title)).toEqual(["w1"]);
  });
});
