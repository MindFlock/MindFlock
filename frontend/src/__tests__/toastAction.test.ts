/** The toast's action button (r5-new finding 10): "Closed “x”" used to make
 * the WHOLE toast the Undo. Now the toast can carry a real button — focusable,
 * labelled, closing the toast when pressed — and the Close toast uses it. The
 * older whole-toast onClick keeps working for the callers that use it. */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { looksLikeError, toast, toastDuration } from "../lib/toast";
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
  remove() {
    body.children = body.children.filter((c) => c !== this);
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

describe("an action toast keeps its button", () => {
  const held = () => body.children.find((c) => c.id === "cs-toast-held");

  it("a plain message arriving over the Undo stacks below it instead of replacing it", () => {
    const pressed = vi.fn();
    toast("Closed “x”", { action: { label: "Undo", onClick: pressed } });
    toast("Copied 12 chars");
    // The newest message takes the bottom slot…
    expect(box().textContent).toBe("Copied 12 chars");
    expect(box().button()).toBeUndefined();
    // …and the Undo is still there, one slot up, still pressable.
    const up = held()!;
    expect(up.classList.contains("show")).toBe(true);
    expect(up.classList.contains("held")).toBe(true);
    up.button()!.onclick!({ stopPropagation: () => {} });
    expect(pressed).toHaveBeenCalledTimes(1);
  });

  it("never more than two: a second plain message replaces the first, the Undo stays", () => {
    toast("Closed “x”", { action: { label: "Undo", onClick: () => {} } });
    toast("one");
    toast("two");
    expect(box().textContent).toBe("two");
    expect(body.children.filter((c) => c.id === "cs-toast-held")).toHaveLength(1);
    expect(body.children).toHaveLength(2);
  });

  it("the held Undo keeps its own clock, then leaves the page", () => {
    toast("Closed “x”", { action: { label: "Undo", onClick: () => {} } });
    vi.advanceTimersByTime(3000);
    toast("Copied");
    vi.advanceTimersByTime(3300); // the Undo's 6s are up (and its fade); the message's too
    expect(held()).toBeUndefined();
    expect(box().classList.contains("show")).toBe(false);
  });

  it("a new action toast replaces the old one, held or not — one Undo at a time", () => {
    toast("Closed “a”", { action: { label: "Undo", onClick: () => {} } });
    toast("Copied");
    toast("Closed “b”", { action: { label: "Undo", onClick: () => {} } });
    expect(held()).toBeUndefined();
    expect(box().children[0].textContent).toBe("Closed “b”");
  });

  it("an expired action toast is replaced like any other", () => {
    toast("Closed “x”", { action: { label: "Undo", onClick: () => {} } });
    vi.advanceTimersByTime(6100);
    toast("Copied");
    expect(held()).toBeUndefined();
    expect(box().textContent).toBe("Copied");
  });
});

describe("toast timing and announcements", () => {
  it("stays up long enough to read, longer for an error, longest with a button", () => {
    expect(toastDuration("Copied")).toBe(1400);
    expect(toastDuration("x".repeat(100))).toBe(600 + 4500);
    expect(toastDuration("Send failed: timeout")).toBeGreaterThanOrEqual(2500);
    expect(toastDuration("Closed “x”", { action: { label: "Undo", onClick: () => {} } })).toBe(6000);
    expect(toastDuration("x".repeat(1000))).toBe(10000);
    expect(toastDuration("anything", { duration: 9000 })).toBe(9000);
  });

  it("reads failures from the words", () => {
    for (const m of ["Send failed: 500", "Couldn't copy the summary", "Could not reset", "Copy failed"])
      expect(looksLikeError(m), m).toBe(true);
    for (const m of ["Copied 12 chars", "Renamed to “x”", "Sent to fix-typo"]) expect(looksLikeError(m), m).toBe(false);
  });

  it("is a polite live region; an error is an alert", () => {
    toast("Copied 12 chars");
    expect(box().attrs.role).toBe("status");
    expect(box().attrs["aria-live"]).toBe("polite");
    toast("Send failed: offline");
    expect(box().attrs.role).toBe("alert");
    expect(box().attrs["aria-live"]).toBe("assertive");
    toast("Saved", { error: false });
    expect(box().attrs.role).toBe("status");
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
    expect(css).toContain("#cs-toast.has-action, #cs-toast-held.has-action {");
    // A long name must not shrink-wrap into half a narrow window.
    expect(css).toContain("max-width: calc(100vw - 32px)");
    expect(css).toContain("#cs-toast .cs-toast-act, #cs-toast-held .cs-toast-act {");
    expect(css).toContain("#cs-toast .cs-toast-act:focus-visible, #cs-toast-held .cs-toast-act:focus-visible {");
    // The held slot sits one slot up, on the same card.
    expect(css).toContain("#cs-toast-held {");
  });
});
