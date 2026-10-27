/** Progressive-disclosure UX round (moved from tests/unit/test_frontend_ux.py,
 * which grepped the built app.js / style.css).
 *
 * Pinned, each in the module that owns it: the sidebar session filter, the
 * client-side session rename (alias), a file dropped on a terminal (the
 * assistant window included — one helper, two callers, nothing on the
 * read-only windows), the keyboard-shortcut sheet, undo toasts, the favicon
 * attention dot, the absence of pane maximize, the New-session dialog's folds
 * and templates strip, the queue panel's run-list, persisted hidden sessions,
 * the keyed sidebar and visibility-aware polling. The store's persistence and
 * the keymap's bindings are exercised; JSX/CSS wiring is read from source. */

import { describe, expect, it, vi } from "vitest";
import { CSS, SRC, allCode, normCode, fn, has } from "./sourceText";

/** A Map-backed localStorage, installed BEFORE the store module loads. */
const storage = new Map<string, string>();
vi.stubGlobal("localStorage", {
  getItem: (k: string) => (storage.has(k) ? storage.get(k)! : null),
  setItem: (k: string, v: string) => void storage.set(k, String(v)),
  removeItem: (k: string) => void storage.delete(k),
  clear: () => storage.clear(),
});

const { useUi } = await import("../state/store");
const { KEYMAP, installKeymap } = await import("../lib/keymap");
const { SESSION_ACTIONS } = await import("../lib/sessionActionTable");

const ALL = allCode();
const ALL_CSS = Object.values(CSS).join("\n");
const WS_TERM = normCode("lib/wsTerm.ts");
const TERMINALS = normCode("lib/terminals.ts");
const CLIPBOARD = normCode("lib/clipboard.ts");
const SPECIAL = normCode("components/grid/SpecialPane.tsx");
const SIDEBAR = normCode("components/sidebar/Sidebar.tsx");
const ROW = normCode("components/sidebar/SidebarRow.tsx");
const STORE = normCode("state/store.ts");

/** Occurrences of `needle` in `hay`. */
const count = (hay: string, needle: string) => hay.split(needle).length - 1;

/** Run one keymap entry's action against a recording host. */
function runKey(key: string): string[] {
  const calls: string[] = [];
  const host = new Proxy({} as Parameters<typeof installKeymap>[0], {
    get: (_t, name) => () => void calls.push(String(name)),
  });
  const g = globalThis as Record<string, unknown>;
  const had = "document" in g;
  const prev = g.document;
  g.document = { addEventListener() {}, removeEventListener() {} };
  const off = installKeymap(host);
  try {
    const entry = KEYMAP.find((e) => e.key === key);
    expect(entry, "keymap entry " + key).toBeDefined();
    entry!.run({} as KeyboardEvent);
  } finally {
    off();
    if (had) g.document = prev;
    else delete g.document;
  }
  return calls;
}

describe("sidebar filter", () => {
  it("renders the search box and its input", () => {
    const filter = normCode("components/sidebar/SessionFilter.tsx");
    expect(has(filter, '<div id="sidebar-search">')).toBe(true);
    expect(has(filter, 'id="session-filter"')).toBe(true);
  });

  it("filters only the sidebar rows, through matchesFilter", () => {
    expect(has(normCode("components/sidebar/ordering.ts"), "export function matchesFilter(")).toBe(true);
    // Filtering only re-renders the sidebar (grid untouched).
    expect(has(SIDEBAR, "allRows.filter((i) => matchesFilter(i, ui.filter, ui.aliases))")).toBe(true);
  });

  it("lets Customize → Filter box decide whether it shows, and persists that (mf_show_filter)", () => {
    useUi.getState().setShowFilter(false);
    expect(storage.get("mf_show_filter")).toBe("false");
    expect(has(STORE, 'showFilter: load<boolean>("mf_show_filter", true)')).toBe(true);
  });

  it('focuses the filter on "/" via the global keymap', () => {
    expect(runKey("/")).toEqual(["focusFilter"]);
  });

  it("styles the box, the input and the empty state", () => {
    const css = normCode("components/sidebar/SessionFilter.css");
    for (const sel of ["#sidebar-search", "#session-filter", ".filter-empty"]) expect(css, sel).toContain(sel);
  });
});

describe("session rename (alias)", () => {
  it("has a Rename dialog, persisted client-side aliases, and one table row both menus read", () => {
    expect(normCode("components/dialogs/RenameDialog.tsx")).toContain("Rename session");
    // Aliases persist client-side; the real title still keys operations.
    expect(has(STORE, 'aliases: load<Record<string, string>>("mf_aliases", {})')).toBe(true);
    expect(has(STORE, 'save("mf_aliases", aliases);')).toBe(true);
    // Rename surfaced in the row menu and the palette (one table row both read).
    expect(SESSION_ACTIONS.rename.label).toBe("Rename…");
    expect(normCode("components/palette/CommandPalette.tsx")).toContain('"rename",');
    expect(normCode("lib/sessionMenu.ts")).toContain('fromTable("rename")');
    expect(ROW).toContain("<SessionMenu");
    // Rows carry the stable title as a data attr (so focus/lookup don't depend
    // on the displayed text, which may be an alias).
    expect(has(ROW, "data-title={title}")).toBe(true);
  });

  it("never restyles an alias", () => {
    // A renamed row reads exactly like every other row — no italic, no
    // separate ".aliased" styling (the real title lives in the tooltip).
    expect(ALL_CSS).not.toContain("aliased");
  });
});

// The agent CLIs run on this machine and cannot see the browser, so every
// terminal that TAKES INPUT turns a dropped file into an upload and types the
// saved path back into the PTY. One helper (clipboard.attachFileDrop), two
// callers: the session terminal and the assistant's useWsTerm window.
describe("drop / paste a file onto a terminal", () => {
  it("wires the assistant window (the interactive useWsTerm) to take dropped files", () => {
    expect(has(SPECIAL, 'useWsTerm(hostRef, "/api/assistant/terminal", true)')).toBe(true);
    expect(WS_TERM).toMatch(/interactive\s*\?\s*attachFileDrop\(\s*host\s*,\s*term\s*\)/);
    expect(WS_TERM).toContain("Drop or paste a file to hand over its path");
  });

  it("uploads the assistant's files with no session; a session terminal's into its workspace", () => {
    // `attachFileDrop(host, term)` — two arguments — sends the bytes to
    // /api/paste-image with no ?session=, landing them in ~/.mindflock/pastes.
    expect(WS_TERM).toContain("attachFileDrop(host, term)");
    expect(WS_TERM).not.toContain("attachFileDrop(host, term,");
    // The ";" pins the call site: the helper's signature names `session` too.
    expect(TERMINALS).toContain("attachFileDrop(host, term, session);");
    // Session terminals APPEND to an existing title; useWsTerm overwrites its
    // own. Pinned so a future unification of the two is a deliberate edit.
    expect(TERMINALS).toContain('host.title += " · drop / paste files to upload"');
  });

  it("wires the gesture in exactly one place", () => {
    expect(count(ALL, "function attachFileDrop(")).toBe(1);
    expect(has(CLIPBOARD, "export function attachFileDrop(")).toBe(true);
    // Every host-level drag listener belongs to that helper.
    expect(count(ALL, 'host.addEventListener("dragover"')).toBe(1);
    expect(count(ALL, 'host.addEventListener("drop"')).toBe(1);
    expect(count(CLIPBOARD, 'host.addEventListener("dragover"')).toBe(1);
    expect(count(CLIPBOARD, 'host.addEventListener("drop"')).toBe(1);
    // The definition plus its two callers. Any fourth is an unguarded call
    // site — e.g. a read-only window claiming a drop.
    expect(count(ALL, "attachFileDrop(")).toBe(3);
  });

  it("leaves a drop on a read-only window alone", () => {
    // A log tail has no PTY to paste a path into: the drag falls through to
    // installGlobalDropGuards and the grid's pane-rearrange drop handler.
    expect(has(SPECIAL, 'useWsTerm(hostRef, "/api/mindflock/logs", false)')).toBe(true);
    expect(count(ALL, "attachFileDrop(")).toBe(3);
    expect(has(CLIPBOARD, "export function installGlobalDropGuards()")).toBe(true);
  });

  it("unwires the drop listeners on teardown", () => {
    // The host div is React's and OUTLIVES the effect: without the disposer a
    // reconnect re-run would stack listeners and upload each file twice.
    const at = WS_TERM.indexOf("detachFiles?.();");
    expect(at).toBeGreaterThan(-1);
    // ...in the cleanup, just before the socket is closed.
    expect(WS_TERM.slice(at, at + 160)).toContain("ws?.close();");
    // ...and the effect really does re-run on these, which is why it matters.
    expect(WS_TERM).toMatch(/\[\s*hostRef,\s*wsPath,\s*interactive,\s*reconnect\s*\]/);
  });

  it("paints the drop cue", () => {
    expect(normCode("components/grid/paneOverlays.css")).toContain(".file-drop");
  });
});

describe("keyboard shortcut cheat-sheet", () => {
  it("is generated from the live keymap + chord table, one <kbd> chip per combo", () => {
    const sheet = normCode("components/palette/ShortcutsSheet.tsx");
    expect(sheet).toContain("shortcuts-overlay");
    expect(sheet).toContain("Keyboard shortcuts");
    expect(sheet).toContain("KEYMAP");
    expect(sheet).toContain("CHORDS");
    for (const cls of ["kbd-sep", "shortcut-desc", "shortcut-keys"]) expect(sheet, cls).toContain(cls);
  });

  it('toggles on "?" via the global keymap, and from the palette', () => {
    expect(runKey("?")).toEqual(["toggleShortcuts"]);
    expect(has(normCode("components/palette/CommandPalette.tsx"), "run: () => host.toggleShortcuts()")).toBe(true);
  });

  it("styles the overlay, the card and the rows", () => {
    const css = normCode("components/palette/ShortcutsSheet.css");
    for (const sel of [".shortcuts-overlay", ".shortcuts-card", ".shortcut-row"]) expect(css, sel).toContain(sel);
  });
});

describe("undo toasts, favicon dot, no maximize", () => {
  it("offers Undo on a single hide and on a bulk hide", () => {
    expect(normCode("lib/sessionActions.ts")).toContain("— click to undo");
    expect(normCode("components/sidebar/BulkBar.tsx")).toContain("— click to undo");
  });

  it("dots the favicon off the same clarify count that badges the title", () => {
    const badge = normCode("lib/titleBadge.ts");
    expect(has(badge, "function updateFavicon(")).toBe(true);
    expect(has(badge, "updateFavicon(n);")).toBe(true);
    // Only redraws when the dot state changes (not every poll).
    expect(has(fn("lib/titleBadge.ts", "updateFavicon"), "if (want === faviconState) return;")).toBe(true);
  });

  it("has no pane maximize UI", () => {
    // Removed — it added no value in the single-/few-pane layouts people use.
    expect(ALL).not.toContain("maximize-btn"); // no header button
    expect(ALL).not.toContain('head.addEventListener("dblclick"'); // no double-click-to-maximize
    expect(ALL).not.toContain("Maximize — "); // no command-palette action
  });
});

describe("New-session dialog progressive disclosure", () => {
  it("shows Works in on the form and folds the rest under More options", () => {
    const fold = normCode("components/dialogs/newSession/WorkspaceFold.tsx");
    // WorksInGroup, drawn by New as scope "new": #new-in-place, #new-worktree.
    expect(has(fold, 'scope="new"')).toBe(true);
    expect(has(fold, 'id={scope + "-in-place"}')).toBe(true); // this folder directly
    expect(has(fold, 'id={scope + "-worktree"}')).toBe(true); // separate worktree
    expect(has(fold, 'id="new-init-repo"')).toBe(true); // git init lives here
    const dlg = normCode("components/dialogs/NewSessionDialog.tsx");
    expect(has(dlg, 'id="new-advanced" className="nf-advanced"')).toBe(true);
    expect(dlg).toContain("More options");
  });

  it("picks the agent from the configured providers, with a Manage link to Coding CLI", () => {
    const agent = normCode("components/dialogs/newSession/AgentAccount.tsx");
    // #new-program / #new-agent-manage (scope "new"; a template's editor is "tpl").
    expect(has(agent, 'scope = "new"')).toBe(true);
    expect(has(agent, '<select id={scope + "-program"}')).toBe(true);
    expect(has(agent, 'id={scope + "-agent-manage"}')).toBe(true);
    expect(agent).toContain('"coding"'); // Manage jumps to Coding CLI
    expect(normCode("components/dialogs/NewSessionDialog.tsx")).toContain('"/api/providers/manage"');
  });

  it("starts from a template strip; git stays optional", () => {
    const strip = normCode("components/dialogs/newSession/TemplatesStrip.tsx");
    expect(has(strip, 'id="new-templates"')).toBe(true);
    expect(has(strip, 'id="new-templates-list"')).toBe(true);
    expect(has(strip, 'id="new-templates-manage"')).toBe(true);
    // A chip's name prefills the form (▶ runs it, ⏰ schedules it); All
    // templates… opens the templates editor.
    expect(has(strip, "onClick={() => onPick(t)}")).toBe(true);
    expect(has(strip, "onClick={() => onRun(t)}")).toBe(true);
    expect(has(strip, "onClick={() => onSchedule(t)}")).toBe(true);
    expect(has(strip, 'useUi.getState().openDialogFor("templates", templatesTarget({ fromNew: true, schedule: null }))')).toBe(true);
    expect(normCode("components/dialogs/NewSessionDialog.tsx")).toContain('"/api/templates"');
    // A plain folder works — git is offered as an init-repo toggle.
    expect(normCode("components/dialogs/newSession/WorkspaceFold.tsx")).toContain('id="new-init-repo"');
  });

  it("styles the folded options and the template strip", () => {
    expect(normCode("components/dialogs/NewSessionDialog.css")).toContain(".nf-advanced {");
    // The strip's rules live with the other template chips.
    expect(normCode("components/grid/budgetNotifyTemplates.css")).toContain(".new-templates {");
  });
});

describe("queue panel, hidden sessions, keyed sidebar, polling", () => {
  it("shows the queue as an ordered run-list with a next marker and a limit hold", () => {
    const tab = normCode("components/grid/QueueTab.tsx");
    for (const hook of ["queue-status", "queue-item-pos", "queue-next", "limited_until", "queue-limited"])
      expect(tab, hook).toContain(hook);
    const css = normCode("components/grid/QueueTab.css");
    expect(css).toContain(".queue-item.queue-next");
    expect(css).toContain(".queue-status.queue-limited");
  });

  it("persists the hidden set (mf_hidden); expanded menus stay transient", () => {
    useUi.getState().setHidden("alpha", true);
    expect(JSON.parse(storage.get("mf_hidden") ?? "[]")).toContain("alpha");
    expect(has(STORE, 'hidden: new Set(load<string[]>("mf_hidden", []))')).toBe(true);
    expect(ROW).toContain("useState<RowMenuOpen | null>(null)");
  });

  it("renders keyed rows that update in place", () => {
    expect(SIDEBAR).toContain("<SidebarRow");
    expect(SIDEBAR).toContain('<ul id="instance-list">');
    expect(ROW).toContain("stagechip");
    expect(SESSION_ACTIONS.duplicate.label).toBe("Duplicate session");
  });

  it("binds a terminal's onData once, writing to the CURRENT socket", () => {
    expect(
      // (It also notes when you last typed there — the ask pop-up's "you are
      // already answering it" rule, lib/ask.ts.)
      has(TERMINALS, "term.onData((data) => { typedAt.set(title, Date.now()); if (ws && ws.readyState === WebSocket.OPEN) ws.send(data); });")
    ).toBe(true);
  });

  it("backs the polls off while the tab is hidden", () => {
    const queries = normCode("state/queries.ts");
    expect(has(queries, "const POLL_VISIBLE_MS = 4_000;")).toBe(true);
    expect(has(queries, "const POLL_HIDDEN_MS = 30_000;")).toBe(true);
    expect(has(queries, "return document.hidden ? POLL_HIDDEN_MS : POLL_VISIBLE_MS;")).toBe(true);
    expect(ALL).not.toContain("setInterval(refreshMindFlock");
    expect(Object.keys(SRC).length).toBeGreaterThan(100);
  });
});
