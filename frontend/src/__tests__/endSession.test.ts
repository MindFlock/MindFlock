/** One way to end a session, and shortcuts that cannot fire by accident.
 *
 *  - the Ctrl+W / Delete guard: any dialog or overlay (store slot OR a visible
 *    [role=dialog] / .modal / .dialog / .palette / [data-modal] in the DOM),
 *    and any typing target (inputs, contenteditable, a terminal) stands it down
 *  - the row menu's two end verbs: Close (keeps the worktree) and Delete… (an
 *    inline confirm strip), and no "End" / "Delete + wipe" wording anywhere
 *  - the 409 "folder in use" card, and the "N worker sessions kept running" note
 *  - no native confirm() / prompt() / alert() anywhere under src/ — the desktop
 *    app implements none of them, so each one is a button that silently does
 *    nothing there
 *
 * The DOM in these tests is a hand-rolled stand-in (the suite runs under the
 * node environment; jsdom is not a dependency of this frontend). The guard
 * reads only `querySelectorAll`, `getElementById`, `activeElement`, `closest`,
 * `classList` and `hidden`, which the stand-in provides. */

/// <reference types="vite/client" />
import { describe, it, expect, afterEach, vi } from "vitest";
import { ApiError } from "../api/client";
import { KEYMAP, OVERLAY_SELECTOR, endShortcutAllowed, modalOpen, overlayOpen, typingTarget } from "../lib/keymap";
import { useUi, type DialogName } from "../state/store";
import { folderInUse, folderInUseText, keptRunningText } from "../lib/sessionActions";

/** Every module the app ships (never the tests), source keyed by its path
 * under src/. Vite's glob rather than a directory walk: this project installs
 * no node types. */
const SOURCES: Record<string, string> = Object.fromEntries(
  Object.entries(
    import.meta.glob<string>(["../**/*.{ts,tsx}", "!../__tests__/**"], {
      query: "?raw",
      import: "default",
      eager: true,
    })
  ).map(([path, text]) => [path.replace(/^\.\.\//, ""), text])
);
const read = (rel: string): string => {
  const text = SOURCES[rel];
  if (text === undefined) throw new Error("no such source: " + rel);
  return text;
};
const codeFiles = () => Object.keys(SOURCES).filter((p) => /\.tsx?$/.test(p));

/** Source minus comments and string literals: the app explains at length WHY
 * there is no confirm(), and that explanation is not a call. */
const strip = (s: string) =>
  s
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/(^|[^:\\"'`])\/\/[^\n]*/g, "$1")
    .replace(/`(?:[^`\\]|\\.)*`/g, "``")
    .replace(/"(?:[^"\\\n]|\\.)*"/g, '""')
    .replace(/'(?:[^'\\\n]|\\.)*'/g, "''");

// --- a tiny DOM stand-in -----------------------------------------------------

interface FakeEl {
  tagName: string;
  className?: string;
  hidden?: boolean;
  isContentEditable?: boolean;
  parent?: FakeEl | null;
  classList: { contains(c: string): boolean };
  hasAttribute(name: string): boolean;
  closest(sel: string): FakeEl | null;
}

/** `sel` is matched the simple way: ".cls" against the class list, "[hidden]"
 * against the hidden flag, "tag" against the tag name — enough for the few
 * selectors the guard uses (".hidden, [hidden]", ".xterm, …", ".cm-root"). */
function el(tagName: string, opts: Partial<FakeEl> = {}): FakeEl {
  const classes = String(opts.className || "").split(/\s+/).filter(Boolean);
  const node: FakeEl = {
    tagName,
    className: opts.className,
    hidden: !!opts.hidden,
    isContentEditable: !!opts.isContentEditable,
    parent: opts.parent ?? null,
    classList: { contains: (c) => classes.includes(c) },
    hasAttribute: (name) => name === "hidden" && !!opts.hidden,
    closest(sel) {
      const alts = sel.split(",").map((s) => s.trim());
      // eslint-disable-next-line @typescript-eslint/no-this-alias
      let cur: FakeEl | null = this;
      while (cur) {
        const c = cur;
        if (
          alts.some((a) =>
            a.startsWith(".")
              ? c.classList.contains(a.slice(1))
              : a === "[hidden]"
                ? !!c.hidden
                : c.tagName.toLowerCase() === a.toLowerCase()
          )
        )
          return cur;
        cur = cur.parent ?? null;
      }
      return null;
    },
  };
  return node;
}

const g = globalThis as Record<string, unknown>;
const hadDoc = "document" in g;
const prevDoc = g.document;
const hadWin = "window" in g;
const prevWin = g.window;

function withDom(opts: { overlays?: FakeEl[]; active?: FakeEl | null; byId?: Record<string, FakeEl> } = {}) {
  const overlays = opts.overlays || [];
  g.document = {
    activeElement: opts.active ?? el("BODY"),
    querySelectorAll: (sel: string) => {
      expect(sel).toBe(OVERLAY_SELECTOR);
      return overlays;
    },
    getElementById: (id: string) => opts.byId?.[id] ?? null,
  };
  // No getComputedStyle: the stand-in has no layout, so the guard must decide
  // on the class/attribute signals alone.
  g.window = {};
}

afterEach(() => {
  useUi.getState().closeDialog();
  useUi.setState({ focused: null } as never);
  if (hadDoc) g.document = prevDoc;
  else delete g.document;
  if (hadWin) g.window = prevWin;
  else delete g.window;
});

// --- the guard -----------------------------------------------------------------

describe("Ctrl+W / Delete guard: dialogs and overlays", () => {
  const closeBinding = () => KEYMAP.find((e) => e.id === "close")!;
  const deleteBinding = () => KEYMAP.find((e) => e.aliasOf === "close")!;

  it("fires only with a focused session, nothing open and nothing focused for typing", () => {
    withDom();
    expect(endShortcutAllowed()).toBe(false); // no focused session
    useUi.setState({ focused: "sess-A" } as never);
    expect(endShortcutAllowed()).toBe(true);
    expect(closeBinding().when!()).toBe(true);
    expect(deleteBinding().when!()).toBe(true);
  });

  it("stands down for EVERY dialog in the store slot — including the ones the old allow-list missed", () => {
    useUi.setState({ focused: "sess-A" } as never);
    withDom();
    const names: DialogName[] = [
      "palette",
      "settings",
      "make-pr",
      "setup",
      "todo",
      "shortcuts",
      "assistant-agent",
      "new-session",
      "recent",
      "commit",
      "rename",
      "intake",
      "customize",
      "prompts",
      "verify",
      "extension",
      "red-zones",
    ];
    for (const name of names) {
      useUi.getState().openDialogFor(name);
      expect(overlayOpen(), name).toBe(true);
      expect(modalOpen(), name).toBe(true);
      expect(closeBinding().when!(), name).toBe(false);
      expect(deleteBinding().when!(), name).toBe(false);
      useUi.getState().closeDialog();
    }
    expect(endShortcutAllowed()).toBe(true);
  });

  it("stands down for any VISIBLE dialog-shaped element the store never heard of", () => {
    useUi.setState({ focused: "sess-A" } as never);
    for (const cls of ["modal", "dialog", "palette", "templates-modal"]) {
      withDom({ overlays: [el("DIV", { className: cls })] });
      expect(endShortcutAllowed(), cls).toBe(false);
    }
    // A [role=dialog] or [data-modal] node reaches the guard through the same
    // selector: whatever querySelectorAll hands back counts when visible.
    withDom({ overlays: [el("SECTION")] });
    expect(endShortcutAllowed()).toBe(false);
  });

  it("ignores a dialog-shaped element that is hidden (class, attribute, or a hidden ancestor)", () => {
    useUi.setState({ focused: "sess-A" } as never);
    withDom({ overlays: [el("DIV", { className: "modal hidden" })] });
    expect(endShortcutAllowed()).toBe(true);
    withDom({ overlays: [el("DIV", { className: "modal", hidden: true })] });
    expect(endShortcutAllowed()).toBe(true);
    const wrap = el("DIV", { className: "hidden" });
    withDom({ overlays: [el("DIV", { className: "modal", parent: wrap })] });
    expect(endShortcutAllowed()).toBe(true);
  });

  it("still counts the keyboard-holding popovers that are not dialogs (bell, ⏩ picker, Prompts menu, break screen)", () => {
    useUi.setState({ focused: "sess-A" } as never);
    for (const id of ["notif-pop", "fast-track-menu", "prompts-menu", "break-screen"]) {
      withDom({ byId: { [id]: el("DIV") } });
      expect(endShortcutAllowed(), id).toBe(false);
      withDom({ byId: { [id]: el("DIV", { className: "hidden" }) } });
      expect(endShortcutAllowed(), id).toBe(true);
    }
  });

  it("the selector names every overlay shape the app draws", () => {
    for (const part of ['[role="dialog"]', ".modal", ".dialog", ".palette", "[data-modal]"]) {
      expect(OVERLAY_SELECTOR).toContain(part);
    }
  });
});

describe("Ctrl+W / Delete guard: typing targets", () => {
  it("names inputs, textareas, selects, contenteditable and anything inside a terminal", () => {
    expect(typingTarget(el("INPUT") as unknown as Element)).toBe(true);
    expect(typingTarget(el("TEXTAREA") as unknown as Element)).toBe(true);
    expect(typingTarget(el("SELECT") as unknown as Element)).toBe(true);
    expect(typingTarget(el("DIV", { isContentEditable: true }) as unknown as Element)).toBe(true);
    const term = el("DIV", { className: "xterm" });
    expect(typingTarget(el("TEXTAREA", { className: "xterm-helper-textarea", parent: term }) as unknown as Element)).toBe(true);
    expect(typingTarget(el("CANVAS", { parent: term }) as unknown as Element)).toBe(true);
    expect(typingTarget(el("BUTTON") as unknown as Element)).toBe(false);
    expect(typingTarget(null)).toBe(false);
  });

  it("BOTH bindings stand down while typing — Ctrl+W at a shell is delete-word, not 'end this session'", () => {
    useUi.setState({ focused: "sess-A" } as never);
    const close = KEYMAP.find((e) => e.id === "close")!;
    const del = KEYMAP.find((e) => e.aliasOf === "close")!;
    const term = el("DIV", { className: "xterm" });
    for (const active of [
      el("INPUT"),
      el("TEXTAREA"),
      el("SELECT"),
      el("DIV", { isContentEditable: true }),
      el("TEXTAREA", { className: "xterm-helper-textarea", parent: term }),
    ]) {
      withDom({ active });
      expect(close.when!(), active.tagName + "/" + active.className).toBe(false);
      expect(del.when!(), active.tagName + "/" + active.className).toBe(false);
    }
    withDom({ active: el("BUTTON", { className: "foot-link" }) });
    expect(close.when!()).toBe(true);
    expect(del.when!()).toBe(true);
  });
});

// --- the two verbs ---------------------------------------------------------------

describe("the row menu ends a session in exactly two ways", () => {
  const row = read("components/sidebar/SidebarRow.tsx");

  it("Close keeps the worktree and says so under the verb", () => {
    expect(row).toMatch(/✕ Close<span className="kbd">Ctrl\+W<\/span>/);
    expect(row).toContain("keeps the worktree; reopen from Recently closed");
    expect(row).toMatch(/className="end-close"[\s\S]{0,400}killSession\(title\)/);
  });

  it("Delete… asks on an inline strip with Delete / Cancel — no confirm()", () => {
    expect(row).toMatch(/>\s*Delete…\s*<\/button>/);
    expect(row).toContain("Removes the worktree and branch");
    const armed = row.slice(row.indexOf("deleteArmed ? ("), row.indexOf("end-delete"));
    expect(armed).toMatch(/className="danger"/);
    expect(armed).toMatch(/>\s*Delete\s*<\/button>/);
    expect(armed).toMatch(/>\s*Cancel\s*<\/button>/);
    // Focus lands on Cancel: a second Enter on the item never deletes.
    expect(armed).toContain("autoFocus onClick={() => setDeleteArmed(false)}");
    expect(armed.split("autoFocus").length - 1).toBe(1);
    expect(armed).toContain("deleteSession(title, true)");
  });

  it("Pause says what it does", () => {
    expect(row).toContain('"Pause (stops the agent, keeps the worktree)"');
  });

  it("the ✕ button is Close, and no 'End' / 'Delete + wipe' wording survives anywhere in the frontend", () => {
    expect(row).toMatch(/title=\{[\s\S]{0,200}"Close session — keeps the worktree/);
    const offenders: string[] = [];
    for (const rel of codeFiles()) {
      const s = read(rel);
      if (/Delete \+ wipe/.test(s) || /\bEnd session\b/.test(s) || /\bEnd selected\b/.test(s) || /"Session ended/.test(s))
        offenders.push(rel);
    }
    expect(offenders).toEqual([]);
  });

  it("the palette offers Close, Delete…, and Pause or Resume", () => {
    const pal = read("components/palette/CommandPalette.tsx");
    expect(pal).toContain("`Close session — ${t}`");
    expect(pal).toContain("`Delete session… — ${t}`");
    expect(pal).toContain("`Pause session — ${t}`");
    expect(pal).toContain("`Resume session — ${t}`");
    expect(pal).toMatch(/inst\.status === "paused"\s*\)?\s*\n?\s*acts\.push\(\{ label: `Resume session/);
  });

  it("the bulk bar is reachable from the rail rows and asks inline with the same two verbs", () => {
    // Picked in place — Ctrl/⌘/Shift-click or the row menu's Select — never a
    // footer button (it read as an afterthought down there).
    const side = read("components/sidebar/Sidebar.tsx");
    expect(side).not.toContain('id="bulk-btn"');
    const row = read("components/sidebar/SidebarRow.tsx");
    expect(row).toContain("e.ctrlKey || e.metaKey || e.shiftKey || picking");
    expect(row).toContain("useUi.getState().pickRow(title, e.shiftKey)");
    expect(row).toContain('{picked ? "Unselect" : "Select"}');
    const bar = read("components/sidebar/BulkBar.tsx");
    expect(bar).toContain("if (!bulkMode) return null;");
    expect(bar).toMatch(/>\s*Close\s*<\/button>/);
    expect(bar).toMatch(/>\s*Delete…\s*<\/button>/);
    expect(bar).toContain("Removes their worktrees and branches");
    expect(bar).toContain("Keeps their worktrees");
    expect(strip(bar)).not.toMatch(/\bconfirm\(/);
    // The rail keeps its rows checkbox-free (tests/unit/test_frontend_bulk.py).
    expect(bar).not.toContain('className="bulk-cb"');
  });

  it("the bell says closed for a close and deleted only for a delete", () => {
    const bell = read("components/NotificationsBell.tsx");
    expect(bell).toContain('d.closed && !d.wiped ? "closed" : "deleted"');
  });
});

// --- 409 folder in use, children kept running ------------------------------------

describe("Delete that 409s: folder in use", () => {
  it("folderInUse reads the sessions off the 409 body and nothing else", () => {
    const held = new ApiError(409, "folder in use", { error: "folder in use", sessions: ["foo", "foo-copy"] });
    expect(folderInUse(held)).toEqual(["foo", "foo-copy"]);
    expect(folderInUse(new ApiError(409, "folder in use", { error: "folder in use" }))).toEqual([]);
    expect(folderInUse(new ApiError(409, "other", { error: "red zone", sessions: ["x"] }))).toBeNull();
    expect(folderInUse(new ApiError(500, "boom", { error: "folder in use", sessions: ["x"] }))).toBeNull();
    expect(folderInUse(new Error("folder in use"))).toBeNull();
  });

  it("the sentence names the holders and what to do", () => {
    expect(folderInUseText(["foo", "foo-copy"])).toBe("Folder in use by foo, foo-copy. Close those first.");
    expect(folderInUseText([])).toBe("Folder in use by another session. Close those first.");
  });

  it("deleteSession resolves to the holders instead of raising an error card", async () => {
    vi.resetModules();
    vi.doMock("../api/client", async () => {
      const real = await vi.importActual<typeof import("../api/client")>("../api/client");
      // Thrown as the re-imported module's own ApiError: folderInUse checks
      // `instanceof`, and resetModules gave this registry a fresh class.
      const instApi = vi.fn(async () => {
        throw new real.ApiError(409, "folder in use", { error: "folder in use", sessions: ["foo-copy"] });
      });
      return { ...real, instApi };
    });
    const popped: string[] = [];
    vi.doMock("../lib/errorPop", () => ({
      errorPop: (t: string) => popped.push(t),
      confirmPop: (t: string) => popped.push(t),
    }));
    const { deleteSession } = await import("../lib/sessionActions");
    const client = await import("../api/client");
    await expect(deleteSession("foo", true)).resolves.toEqual({ inUse: ["foo-copy"] });
    expect(client.instApi).toHaveBeenCalledWith("foo", "/cleanup", { method: "POST" });
    expect(popped).toEqual([]);
    vi.doUnmock("../api/client");
    vi.doUnmock("../lib/errorPop");
    vi.resetModules();
  });

  it("the row draws the holders on an inline card under the row, outside the menu", () => {
    const row = read("components/sidebar/SidebarRow.tsx");
    expect(row).toContain("if (r && r !== \"done\") setInUse(r.inUse);");
    expect(row).toMatch(/<div className="folder-in-use" role="status">\s*<span className="fiu-text">\{folderInUseText\(inUse\)\}<\/span>/);
    // Under the row's tail, after the (conditional) menu — not inside it.
    expect(row.indexOf('className="folder-in-use"')).toBeGreaterThan(row.indexOf('className="inst-actions"'));
  });

  it("'N worker sessions kept running' comes from the response's children", () => {
    expect(keptRunningText({ children: [] })).toBeNull();
    expect(keptRunningText(null)).toBeNull();
    expect(keptRunningText({ children: ["w1"] })).toBe("1 worker session kept running");
    expect(keptRunningText({ children: ["w1", "w2", "w3"] })).toBe("3 worker sessions kept running");
  });
});

// --- confirmations are not errors; no native dialogs --------------------------------

describe("confirmations and failures", () => {
  const acts = read("lib/sessionActions.ts");

  it("questions go on the neutral confirm card, failures on the error card, successes on a toast", () => {
    expect(acts).toMatch(/confirmPop\(\s*"Merge this PR\?"/);
    expect(acts).toMatch(/confirmPop\(\s*"Delete “"/);
    expect(acts).toMatch(/confirmPop\(\s*"Delete " \+ displayName\(title\) \+ "\?"/);
    for (const title of ["Push failed", "Open PR failed", "Merge failed", "Pause failed", "Resume failed", "Delete failed", "Reopen failed", "Close failed"]) {
      expect(acts, title).toContain(`errorPop("${title}"`);
      expect(acts, title).not.toContain(`toast("${title}`);
    }
    // Delete and the missing-workspace Delete (was "Clean up") both report as "Delete failed".
    expect(acts.split('errorPop("Delete failed"').length - 1).toBeGreaterThanOrEqual(2);
    expect(acts).not.toMatch(/errorPop\(\s*"Merge this PR\?"/);
    expect(acts).not.toMatch(/errorPop\(\s*"Delete “"/);
  });

  it("the confirm card is the error card without the alarm", () => {
    const pop = read("lib/errorPop.ts");
    expect(pop).toContain('card.className = kind === "confirm" ? "cs-error cs-confirm" : "cs-error";');
    // confirmPop always ends with the harmless answer, so Enter never destroys.
    expect(pop).toContain('cancel: string = "Cancel"');
  });

  it("no native confirm() / prompt() / alert() call remains under src/", () => {
    const call = /(?<![\w.$])(?:window\.)?(confirm|prompt|alert)\s*\(/g;
    const hits: string[] = [];
    for (const rel of codeFiles()) {
      const s = strip(read(rel));
      let m: RegExpExecArray | null;
      while ((m = call.exec(s))) hits.push(rel + ": " + s.slice(Math.max(0, m.index - 30), m.index + 20).replace(/\s+/g, " "));
    }
    expect(hits).toEqual([]);
  });
});
