/** The keyboard stays on a session you rename (lib/renameFocus).
 *
 * A rename remounts the session's rail row, pane and Sky line under the new
 * title, and whatever of theirs had the keyboard went with it: focus fell to
 * <body> although the selection followed. Pinned here, over a small fake DOM
 * (the test environment is node):
 *
 *  - `focusSpot` names the element of THIS title holding the keyboard (its
 *    kind, and whether its terminal had it), past nested rows naming others;
 *  - `keepFocusAcrossRename` focuses the same kind of element under the new
 *    title once it is mounted — the terminal inside it when that is where the
 *    keyboard was — leaves focus alone when the person moved it elsewhere,
 *    and gives up after its budget;
 *  - renameSession takes the spot before the request, looks again under both
 *    names after it (the `session.renamed` event often lands first), falls
 *    back to the selected session's terminal, and hands it on after the move;
 *  - the rail's rename input hands the keyboard back to its row on Enter and
 *    Escape. */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { code } from "./sourceText";

const { focusSpot, keepFocusAcrossRename, PANE_TERMINAL } = await import("../lib/renameFocus");

class El {
  tagName: string;
  classList: { contains: (c: string) => boolean; [i: number]: string };
  parentElement: El | null = null;
  children: El[] = [];
  attrs: Record<string, string>;
  constructor(tag: string, cls: string[], attrs: Record<string, string> = {}) {
    this.tagName = tag;
    const list = [...cls];
    this.classList = Object.assign(Object.fromEntries(list.map((c, i) => [i, c])), {
      contains: (c: string) => list.includes(c),
    });
    this.attrs = attrs;
  }
  getAttribute(k: string) {
    return k in this.attrs ? this.attrs[k] : null;
  }
  add(child: El): El {
    child.parentElement = this;
    this.children.push(child);
    return child;
  }
  get isConnected(): boolean {
    let e: El | null = this;
    while (e.parentElement) e = e.parentElement;
    return e === doc.root;
  }
  contains(o: El | null): boolean {
    for (let e = o; e; e = e.parentElement) if (e === this) return true;
    return false;
  }
  all(): El[] {
    return [this, ...this.children.flatMap((c) => c.all())];
  }
  querySelector(sel: string): El | null {
    const cls = sel.replace(/^\./, "");
    return this.all().find((e) => e !== this && e.classList.contains(cls)) || null;
  }
  focus = vi.fn(() => {
    doc.activeElement = this;
  });
  remove() {
    const p = this.parentElement;
    if (p) p.children = p.children.filter((c) => c !== this);
    this.parentElement = null;
    if (!doc.activeElement?.isConnected) doc.activeElement = doc.body;
  }
}

const doc = {
  root: new El("HTML", []),
  body: new El("BODY", []),
  activeElement: null as El | null,
  querySelectorAll(sel: string): El[] {
    const attr = sel.slice(1, -1);
    return doc.root.all().filter((e) => e.getAttribute(attr) !== null);
  },
};
doc.root.add(doc.body);

let frames: Array<() => void> = [];
const frame = () => {
  const run = frames;
  frames = [];
  for (const f of run) f();
};

beforeEach(() => {
  doc.body.children = [];
  doc.activeElement = doc.body;
  frames = [];
  vi.stubGlobal("document", doc);
  vi.stubGlobal("requestAnimationFrame", (f: () => void) => frames.push(f));
});
afterEach(() => vi.unstubAllGlobals());

function railRow(title: string): El {
  return doc.body.add(new El("LI", ["inst", "active"], { "data-title": title }));
}
function pane(title: string): { pane: El; term: El } {
  const p = doc.body.add(new El("SECTION", ["pane", "focused"], { "data-title": title }));
  // The pane's Thread lists its workers by THEIR titles, round the terminal.
  const worker = p.add(new El("DIV", ["th-worker"], { "data-title": "a-worker" }));
  void worker;
  const term = p.add(new El("TEXTAREA", ["xterm-helper-textarea"]));
  return { pane: p, term };
}

describe("focusSpot", () => {
  it("names the rail row whose rename input has the keyboard", () => {
    const row = railRow("old");
    const input = row.add(new El("INPUT", ["inst-rename"]));
    doc.activeElement = input;
    expect(focusSpot("old")).toEqual({ attr: "data-title", tag: "LI", cls: "inst", term: false });
  });

  it("names a pane's terminal, past rows that name other sessions", () => {
    const { term } = pane("old");
    doc.activeElement = term;
    expect(focusSpot("old")).toEqual({ attr: "data-title", tag: "SECTION", cls: "pane", term: true });
  });

  it("names a Sky line by data-line", () => {
    const line = doc.body.add(new El("DIV", ["skl", "skl-frame"], { "data-line": "old" }));
    doc.activeElement = line;
    expect(focusSpot("old")).toEqual({ attr: "data-line", tag: "DIV", cls: "skl", term: false });
  });

  it("is null for the page, another session, or no DOM", () => {
    expect(focusSpot("old")).toBeNull();
    doc.activeElement = railRow("other");
    expect(focusSpot("old")).toBeNull();
    vi.unstubAllGlobals();
    expect(focusSpot("old")).toBeNull();
  });
});

describe("keepFocusAcrossRename", () => {
  it("puts the keyboard on the renamed row once it is mounted", () => {
    const old = railRow("old");
    doc.activeElement = old;
    const spot = focusSpot("old");
    keepFocusAcrossRename(spot, "new");
    old.remove(); // the remount: the old row goes…
    frame();
    expect(doc.activeElement).toBe(doc.body);
    const row = railRow("new"); // …and the new one arrives a frame later
    frame();
    expect(row.focus).toHaveBeenCalledWith({ preventScroll: true });
    expect(doc.activeElement).toBe(row);
  });

  it("goes back into the terminal when that is where the keyboard was", () => {
    const { pane: oldPane, term } = pane("old");
    doc.activeElement = term;
    keepFocusAcrossRename(focusSpot("old"), "new");
    oldPane.remove();
    const fresh = pane("new");
    frame();
    expect(doc.activeElement).toBe(fresh.term);
    expect(fresh.pane.focus).not.toHaveBeenCalled();
  });

  it("the selected session's terminal when nothing of it had the keyboard", () => {
    keepFocusAcrossRename(PANE_TERMINAL, "new");
    const fresh = pane("new");
    railRow("new");
    frame();
    expect(doc.activeElement).toBe(fresh.term);
  });

  it("leaves focus where the person put it meanwhile", () => {
    const old = railRow("old");
    doc.activeElement = old;
    keepFocusAcrossRename(focusSpot("old"), "new");
    old.remove();
    const elsewhere = doc.body.add(new El("INPUT", ["composer"]));
    doc.activeElement = elsewhere;
    const row = railRow("new");
    frame();
    expect(row.focus).not.toHaveBeenCalled();
    expect(doc.activeElement).toBe(elsewhere);
    expect(frames).toHaveLength(0); // and stops looking
  });

  it("gives up after its budget", () => {
    const now = vi.spyOn(Date, "now").mockReturnValue(1000);
    keepFocusAcrossRename(PANE_TERMINAL, "new", 500);
    frame();
    expect(frames).toHaveLength(1);
    now.mockReturnValue(1600);
    frame();
    expect(frames).toHaveLength(0);
    pane("new");
    expect(doc.activeElement).toBe(doc.body);
    now.mockRestore();
  });

  it("does nothing without a spot or a DOM", () => {
    keepFocusAcrossRename(null, "new");
    expect(frames).toHaveLength(0);
    vi.unstubAllGlobals();
    expect(() => keepFocusAcrossRename(PANE_TERMINAL, "new")).not.toThrow();
  });
});

describe("wiring", () => {
  it("renameSession takes the spot, looks again under both names, then hands it on after the move", () => {
    const src = code("lib/renameSession.ts");
    const fn = src.slice(src.indexOf("export async function renameSession"));
    const before = fn.indexOf("let spot = focusSpot(title)");
    const request = fn.indexOf('instApi<RenameAnswer>(title, "/rename"');
    const again = fn.indexOf("spot = spot || focusSpot(title) || focusSpot(now) ||");
    const move = fn.indexOf("moveSessionTitle(title, now)");
    const keep = fn.indexOf("keepFocusAcrossRename(spot, now)");
    expect(before).toBeGreaterThan(-1);
    expect(before).toBeLessThan(request);
    expect(request).toBeLessThan(again);
    expect(again).toBeLessThan(move);
    expect(move).toBeLessThan(keep);
    expect(fn).toMatch(/sel === title \|\| sel === now \? PANE_TERMINAL : null/);
  });

  it("the rail's rename input hands the keyboard back to its row on Enter and Escape", () => {
    const src = code("components/sidebar/SidebarRow.tsx");
    const back = src.indexOf("const backToRow");
    expect(back).toBeGreaterThan(-1);
    expect(src.slice(back, back + 400)).toMatch(/row\.focus\(\{ preventScroll: true \}\)/);
    const enter = src.indexOf('e.key === "Enter"', back);
    const escape = src.indexOf('e.key === "Escape"', enter);
    expect(src.slice(enter, escape)).toContain("backToRow()");
    expect(src.slice(escape, escape + 400)).toContain("backToRow()");
  });
});
