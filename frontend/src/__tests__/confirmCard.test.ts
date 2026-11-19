/** The corner question card (lib/errorPop confirmPop) answers the keyboard,
 * like the row menu's confirm strip:
 *
 *  - it takes focus on open, on Cancel (Enter never destroys); an error card
 *    with no Cancel focuses its primary; one with no buttons leaves focus be;
 *  - Escape closes it as Cancel, and stops there (nothing under it hears it);
 *  - a destructive answer (Delete) is drawn red (`danger`), not as the
 *    accent primary, and every Delete that asks on the card passes it.
 *
 * The suite runs under node (no jsdom): a small DOM stand-in carries what
 * errorPop touches. */

/// <reference types="vite/client" />
import { beforeEach, describe, expect, it, vi } from "vitest";
import { actClass, confirmPop, errorPop, focusIndex } from "../lib/errorPop";
import { code, css } from "./sourceText";

interface FakeEl {
  tagName: string;
  id: string;
  className: string;
  textContent: string;
  title: string;
  type: string;
  dataset: Record<string, string>;
  children: FakeEl[];
  parent: FakeEl | null;
  onclick: (() => void) | null;
  listeners: Record<string, Array<(e: unknown) => void>>;
  appendChild(c: FakeEl): FakeEl;
  removeChild(c: FakeEl): FakeEl;
  remove(): void;
  setAttribute(k: string, v: string): void;
  addEventListener(t: string, fn: (e: unknown) => void): void;
  querySelectorAll(sel: string): FakeEl[];
  focus(): void;
}

let focused: FakeEl | null = null;

function mk(tagName: string): FakeEl {
  const el: FakeEl = {
    tagName: tagName.toUpperCase(),
    id: "",
    className: "",
    textContent: "",
    title: "",
    type: "",
    dataset: {},
    children: [],
    parent: null,
    onclick: null,
    listeners: {},
    appendChild(c) {
      c.parent = el;
      el.children.push(c);
      return c;
    },
    removeChild(c) {
      el.children = el.children.filter((x) => x !== c);
      c.parent = null;
      return c;
    },
    remove() {
      el.parent?.removeChild(el);
    },
    setAttribute() {},
    addEventListener(t, fn) {
      (el.listeners[t] ||= []).push(fn);
    },
    // Only ".cls" selectors: all errorPop asks for.
    querySelectorAll(sel) {
      const cls = sel.replace(/^\./, "");
      const out: FakeEl[] = [];
      const walk = (n: FakeEl) => {
        for (const c of n.children) {
          if (c.className.split(/\s+/).includes(cls)) out.push(c);
          walk(c);
        }
      };
      walk(el);
      return out;
    },
    focus() {
      focused = el;
    },
  };
  return el;
}

let body: FakeEl;

beforeEach(() => {
  body = mk("body");
  focused = null;
  const byId = (id: string, n: FakeEl): FakeEl | null => {
    if (n.id === id) return n;
    for (const c of n.children) {
      const hit = byId(id, c);
      if (hit) return hit;
    }
    return null;
  };
  vi.stubGlobal("document", {
    body,
    createElement: (t: string) => mk(t),
    getElementById: (id: string) => byId(id, body),
  });
});

const cards = () => body.children[0]?.children ?? [];
const buttons = (card: FakeEl) => card.querySelectorAll("cs-error-act");
const escape = (card: FakeEl) => {
  const e = { key: "Escape", preventDefault: vi.fn(), stopPropagation: vi.fn() };
  for (const fn of card.listeners.keydown || []) fn(e);
  return e;
};

describe("the question card takes the keyboard", () => {
  it("focuses Cancel when it opens, so Enter never deletes", () => {
    const del = vi.fn();
    confirmPop("Delete a?", "Deletes the folder.", [{ label: "Delete", primary: true, danger: true, run: del }]);
    const card = cards()[0];
    const [yes, no] = buttons(card);
    expect(no.textContent).toBe("Cancel");
    expect(focused).toBe(no);
    expect(yes.className).toContain("danger");
    expect(del).not.toHaveBeenCalled();
  });

  it("closes on Escape, as Cancel, and the key goes no further", () => {
    const del = vi.fn();
    confirmPop("Delete b?", "x", [{ label: "Delete", primary: true, danger: true, run: del }]);
    const card = cards()[0];
    const e = escape(card);
    expect(cards()).toHaveLength(0);
    expect(e.preventDefault).toHaveBeenCalled();
    expect(e.stopPropagation).toHaveBeenCalled();
    expect(del).not.toHaveBeenCalled();
  });

  it("ignores other keys", () => {
    confirmPop("Delete c?", "x", [{ label: "Delete", run: vi.fn() }]);
    const card = cards()[0];
    for (const fn of card.listeners.keydown || []) fn({ key: "Enter", preventDefault: vi.fn(), stopPropagation: vi.fn() });
    expect(cards()).toHaveLength(1);
  });

  it("an error card with buttons focuses its primary; one without buttons leaves focus alone", () => {
    errorPop("Push blocked", "x", [{ label: "Open map", run: vi.fn() }, { label: "Push anyway", primary: true, run: vi.fn() }]);
    const [, anyway] = buttons(cards()[0]);
    expect(focused).toBe(anyway);
    focused = null;
    errorPop("Delete failed", "y");
    expect(focused).toBeNull();
    // Escape still closes a plain error card.
    escape(cards()[1]);
    expect(cards()).toHaveLength(1);
  });

  it("picks the button to focus and the classes each one wears", () => {
    expect(focusIndex([{ label: "Delete", primary: true, run: () => {} }, { label: "Cancel", cancel: true, run: () => {} }])).toBe(1);
    expect(focusIndex([{ label: "A", run: () => {} }, { label: "B", primary: true, run: () => {} }])).toBe(1);
    expect(focusIndex([{ label: "A", run: () => {} }])).toBe(-1);
    expect(actClass({ label: "Delete", primary: true, danger: true, run: () => {} })).toBe("cs-error-act primary danger");
    expect(actClass({ label: "Cancel", cancel: true, run: () => {} })).toBe("cs-error-act cancel");
  });
});

describe("Delete is red on the card", () => {
  it("every Delete asked on the card passes danger", () => {
    const src = code("lib/sessionActions.ts");
    expect(src).toContain('{ label: "Delete", primary: true, danger: true, run: () => void cleanupMissing(title, true) }');
    expect(src).toMatch(/label: "Delete",\s*primary: true,\s*danger: true,\s*run: \(\) =>\s*void deleteSession\(title, true\)/);
  });

  it("the card's danger answer is styled red, over the question's accent primary", () => {
    const sheet = css("components/ConnBanner.css");
    expect(sheet).toContain("#cs-errors .cs-confirm .cs-error-act.danger");
    const rule = sheet.slice(sheet.indexOf("#cs-errors .cs-error-act.danger,"));
    expect(rule.slice(0, rule.indexOf("}"))).toContain("color: var(--red)");
  });
});
