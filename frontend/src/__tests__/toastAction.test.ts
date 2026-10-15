/** The toast's action button (r5-new finding 10): "Closed “x”" used to make
 * the WHOLE toast the Undo. Now the toast can carry a real button — focusable,
 * labelled, closing the toast when pressed — and the Close toast uses it. The
 * older whole-toast onClick keeps working for the callers that use it. */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { toast } from "../lib/toast";
import { CLOSED_UNDO_HINT } from "../lib/sessionActions";
import { code, normCode } from "./sourceText";

/** Just enough DOM for lib/toast.ts. */
class FakeEl {
  id = "";
  type = "";
  className = "";
  title = "";
  tagName: string;
  children: FakeEl[] = [];
  attrs: Record<string, string> = {};
  onclick: ((e: { stopPropagation(): void }) => void) | null = null;
  onmouseenter: (() => void) | null = null;
  onmouseleave: (() => void) | null = null;
  onfocus: (() => void) | null = null;
  onblur: (() => void) | null = null;
  private text = "";
  private classes = new Set<string>();
  classList = {
    add: (...c: string[]) => c.forEach((x) => this.classes.add(x)),
    remove: (...c: string[]) => c.forEach((x) => this.classes.delete(x)),
    toggle: (c: string, on: boolean) => (on ? this.classes.add(c) : this.classes.delete(c)),
    contains: (c: string) => this.classes.has(c),
  };
  constructor(tag: string) {
    this.tagName = tag.toUpperCase();
  }
  get textContent(): string {
    return this.children.length ? this.children.map((c) => c.textContent).join("") : this.text;
  }
  set textContent(v: string) {
    this.children = [];
    this.text = v;
  }
  setAttribute(k: string, v: string) {
    this.attrs[k] = v;
  }
  removeAttribute(k: string) {
    if (k === "title") this.title = "";
    delete this.attrs[k];
  }
  replaceChildren(...kids: FakeEl[]) {
    this.text = "";
    this.children = kids;
  }
  appendChild(kid: FakeEl) {
    this.children.push(kid);
  }
  button(): FakeEl | undefined {
    return this.children.find((c) => c.tagName === "BUTTON");
  }
}

const g = globalThis as unknown as { document?: unknown };
let body: FakeEl;
let prevDoc: unknown;

function box(): FakeEl {
  return body.children.find((c) => c.id === "cs-toast")!;
}

beforeEach(() => {
  vi.useFakeTimers();
  prevDoc = g.document;
  body = new FakeEl("body");
  g.document = {
    body,
    createElement: (t: string) => new FakeEl(t),
    getElementById: (id: string) => body.children.find((c) => c.id === id) || null,
  };
});

afterEach(() => {
  vi.useRealTimers();
  if (prevDoc === undefined) delete g.document;
  else g.document = prevDoc;
});

describe("toast action button", () => {
  it("renders a labelled button next to the message, and the toast itself is not the target", () => {
    const pressed = vi.fn();
    toast("Closed “fix-typo”", { action: { label: "Undo", onClick: pressed }, title: CLOSED_UNDO_HINT });
    const el = box();
    const btn = el.button()!;
    expect(btn.textContent).toBe("Undo");
    expect(btn.type).toBe("button");
    expect(btn.className).toBe("cs-toast-act");
    expect(btn.attrs["aria-label"]).toBe("Undo: Closed “fix-typo”");
    expect(el.children[0].textContent).toBe("Closed “fix-typo”");
    expect(el.classList.contains("has-action")).toBe(true);
    expect(el.classList.contains("clickable")).toBe(false);
    expect(el.onclick).toBeNull();
    expect(el.title).toBe(CLOSED_UNDO_HINT);
    expect(pressed).not.toHaveBeenCalled();
  });

  it("pressing it stops propagation, closes the toast and runs the action once", () => {
    const pressed = vi.fn();
    toast("Closed “x”", { action: { label: "Undo", onClick: pressed, ariaLabel: "Undo closing “x”" } });
    const el = box();
    expect(el.button()!.attrs["aria-label"]).toBe("Undo closing “x”");
    const stop = vi.fn();
    el.button()!.onclick!({ stopPropagation: stop });
    expect(stop).toHaveBeenCalled();
    expect(pressed).toHaveBeenCalledTimes(1);
    expect(el.classList.contains("show")).toBe(false);
    expect(el.classList.contains("has-action")).toBe(false);
  });

  it("holds while hovered, then lingers briefly after the pointer leaves", () => {
    toast("Closed “x”", { action: { label: "Undo", onClick: () => {} }, duration: 7000 });
    const el = box();
    el.onmouseenter!();
    vi.advanceTimersByTime(20000);
    expect(el.classList.contains("show")).toBe(true);
    el.onmouseleave!();
    vi.advanceTimersByTime(2100);
    expect(el.classList.contains("show")).toBe(false);
  });

  it("holds while its button has focus", () => {
    toast("Closed “x”", { action: { label: "Undo", onClick: () => {} } });
    const btn = box().button()!;
    btn.onfocus!();
    vi.advanceTimersByTime(20000);
    expect(box().classList.contains("show")).toBe(true);
    btn.onblur!();
    vi.advanceTimersByTime(2100);
    expect(box().classList.contains("show")).toBe(false);
  });

  it("leaves plain and whole-toast-click toasts as they were", () => {
    toast("Closed “x”", { action: { label: "Undo", onClick: () => {} }, title: "hint" });
    toast("Copied 12 chars");
    let el = box();
    expect(el.textContent).toBe("Copied 12 chars");
    expect(el.button()).toBeUndefined();
    expect(el.title).toBe("");
    expect(el.classList.contains("has-action")).toBe(false);
    expect(el.onmouseenter).toBeNull();

    const opened = vi.fn();
    toast("PR ready — click to open", { onClick: opened });
    el = box();
    expect(el.classList.contains("clickable")).toBe(true);
    el.onclick!({ stopPropagation: () => {} });
    expect(opened).toHaveBeenCalledTimes(1);
    expect(el.classList.contains("show")).toBe(false);
  });
});

describe("the Close toast uses it", () => {
  it("passes Undo as an action, not as a whole-toast onClick", () => {
    const actions = code("lib/sessionActions.ts");
    expect(actions).toContain('action: { label: "Undo", onClick: () => void undoLastClose()');
    expect(actions).toContain("title: CLOSED_UNDO_HINT");
    expect(actions).not.toContain("{ onClick: () => void undoLastClose()");
    expect(CLOSED_UNDO_HINT).toContain("Ctrl+Shift+T");
  });

  it("styles the button in a sheet the manifest imports", () => {
    expect(normCode("styles/index.css")).toContain('@import "../lib/toast.css" layer(components);');
    const css = normCode("lib/toast.css");
    expect(css).toContain("#cs-toast.has-action {");
    // A long name must not shrink-wrap into half a narrow window.
    expect(css).toContain("max-width: calc(100vw - 32px)");
    expect(css).toContain("#cs-toast .cs-toast-act {");
    expect(css).toContain("#cs-toast .cs-toast-act:focus-visible {");
  });
});
