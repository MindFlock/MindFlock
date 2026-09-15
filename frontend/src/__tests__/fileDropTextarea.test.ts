/** `attachFileDropZone` — dropping a file into a text box.
 *
 * The sibling of fileDrop.test.ts. That one covers the PTY gesture; this one
 * covers the two textareas (the new-session prompt and the new-ticket brief),
 * where the destination is a CONTROLLED React input rather than a terminal, so
 * the helper cannot just write `host.value` — it hands back the whole new
 * string and the component owns it.
 *
 * Environment is node (see vitest.config), so the host is a stub. What is
 * pinned here is the DECISIONS: which drags to claim, where the text lands when
 * the box is not focused, and that a readOnly box refuses.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { attachFileDropZone, spliceAtCaret } from "../lib/fileDropTextarea";

/** A drop ZONE wrapping a textarea — the shape the helper is given. The zone
 * carries the listeners and the cue class; the box inside carries the text. */
function fakeTextarea(value = "", opts: { readOnly?: boolean; disabled?: boolean } = {}) {
  const handlers = new Map<string, Set<(ev: unknown) => void>>();
  const classes = new Set<string>();
  const written: string[] = [];
  const zone = {
    written,
    // The textarea the zone will hand back from querySelector.
    value,
    readOnly: !!opts.readOnly,
    disabled: !!opts.disabled,
    selectionStart: value.length,
    selectionEnd: value.length,
    querySelector(sel: string): unknown {
      return sel === "textarea" ? zone : null;
    },
    // setControlledValue writes through the prototype setter then dispatches
    // `input`; the stub records what the component's onChange would have seen.
    tagName: "TEXTAREA",
    parentElement: null,
    dispatchEvent: (ev: { type: string }) => {
      if (ev.type === "input") written.push(zone.value);
      return true;
    },
    handlers,
    classes,
    addEventListener(type: string, fn: (ev: unknown) => void) {
      if (!handlers.has(type)) handlers.set(type, new Set());
      handlers.get(type)!.add(fn);
    },
    removeEventListener(type: string, fn: (ev: unknown) => void) {
      handlers.get(type)?.delete(fn);
    },
    classList: {
      add: (c: string) => void classes.add(c),
      remove: (c: string) => void classes.delete(c),
    },
    contains: () => false,
    focus() {},
    setSelectionRange(a: number, b: number) {
      this.selectionStart = a;
      this.selectionEnd = b;
    },
    fire(type: string, ev: Record<string, unknown>) {
      for (const fn of handlers.get(type) || []) fn(ev);
      return ev;
    },
    count(type: string) {
      return handlers.get(type)?.size ?? 0;
    },
  };
  return zone;
}

function dragEvent(types: string[], files: unknown[] = []) {
  return {
    preventDefault: vi.fn(),
    stopPropagation: vi.fn(),
    dataTransfer: { types, files, dropEffect: "" },
  };
}

/** The document/fetch the upload path reaches: a toast and one POST. */
function stubBrowser(path: string) {
  const g = globalThis as Record<string, unknown>;
  g.document = {
    activeElement: null,
    getElementById: () => null,
    createElement: () => ({
      style: {},
      classList: { add() {}, remove() {}, toggle() {} },
      addEventListener() {},
      remove() {},
    }),
    body: { appendChild() {} },
  };
  g.fetch = vi.fn(async () => ({
    status: 200,
    ok: true,
    text: async () => JSON.stringify({ path }),
  }));
  g.Event = class { type: string; constructor(t: string) { this.type = t; } };
}

/** Let the drop's upload promise and its `.then` settle. */
const settle = () => new Promise((r) => setTimeout(r, 0));

/** The hook's callback-ref behaviour without a React renderer: attach on a
 * node, detach on null, mirror the element for callers that also need it. */
function makeCallbackRef(opts: Record<string, never>, mirror?: { current: unknown }) {
  let detach: (() => void) | null = null;
  return (el: unknown) => {
    detach?.();
    detach = null;
    if (mirror) mirror.current = el;
    if (el) detach = attachFileDropZone(el as never, () => opts);
  };
}

describe("spliceAtCaret", () => {
  it("does not start an empty box with a space", () => {
    expect(spliceAtCaret("", 0, 0, "/tmp/a.png").value).toBe("/tmp/a.png ");
  });

  it("separates the path from a word already to the left", () => {
    // Without the pad this reads "look at/tmp/a.png", which is a different
    // (and nonexistent) path rather than two tokens.
    const r = spliceAtCaret("look at", 7, 7, "/tmp/a.png");
    expect(r.value).toBe("look at /tmp/a.png ");
    expect(r.caret).toBe(r.value.length);
  });

  it("does not double a space the user already typed", () => {
    expect(spliceAtCaret("look at ", 8, 8, "/tmp/a.png").value).toBe("look at /tmp/a.png ");
  });

  it("replaces a selection rather than adding to it", () => {
    expect(spliceAtCaret("keep DROP keep", 5, 9, "/p").value).toBe("keep /p keep");
  });

  it("clamps offsets that outran the text while the upload was in flight", () => {
    // The caret is read at drop time; the user can shorten the text before the
    // upload returns. Landing late beats overwriting what they just typed.
    expect(spliceAtCaret("ab", 99, 120, "/p").value).toBe("ab /p ");
  });

  it("leaves a trailing space so the next drop is a separate path", () => {
    const one = spliceAtCaret("", 0, 0, "/a.png");
    const two = spliceAtCaret(one.value, one.caret, one.caret, "/b.png");
    expect(two.value).toBe("/a.png /b.png ");
  });
});

describe("attachFileDropZone", () => {
  beforeEach(() => {
    const g = globalThis as Record<string, unknown>;
    delete g.document;
    delete g.fetch;
  });

  it("claims a drag that carries files, and marks the box", () => {
    const host = fakeTextarea();
    attachFileDropZone(host as never, () => ({}));
    const ev = dragEvent(["Files"]);
    host.fire("dragover", ev);
    expect(ev.preventDefault).toHaveBeenCalled();
    expect(ev.dataTransfer.dropEffect).toBe("copy");
    expect(host.classes.has("ta-file-drop")).toBe(true);
  });

  it("ignores a drag carrying only text, so selecting and dragging still works", () => {
    const host = fakeTextarea();
    attachFileDropZone(host as never, () => ({}));
    const ev = dragEvent(["text/plain"]);
    host.fire("dragover", ev);
    expect(ev.preventDefault).not.toHaveBeenCalled();
    expect(host.classes.has("ta-file-drop")).toBe(false);
  });

  it("stops the event, so the grid does not read it as a pane being moved", () => {
    const host = fakeTextarea();
    attachFileDropZone(host as never, () => ({}));
    const ev = dragEvent(["Files"]);
    host.fire("dragover", ev);
    expect(ev.stopPropagation).toHaveBeenCalled();
  });

  it("uploads the drop and hands back the path as the new value", async () => {
    stubBrowser("/ws/uploads/shot.png");
    const host = fakeTextarea("look at");
    attachFileDropZone(host as never, () => ({}));
    host.fire("drop", dragEvent(["Files"], [{ name: "shot.png", type: "image/png" }]));
    await settle();
    expect(host.written).toEqual(["look at /ws/uploads/shot.png "]);
  });

  it("appends at the END when the box does not hold the caret", async () => {
    // A drop does not focus the textarea, and an unfocused one reports
    // selectionStart 0 — which would splice the path in front of everything
    // the user wrote. This is the guard against that.
    stubBrowser("/ws/uploads/a.png");
    const host = fakeTextarea("already written");
    host.selectionStart = 0;
    host.selectionEnd = 0;
    attachFileDropZone(host as never, () => ({}));
    host.fire("drop", dragEvent(["Files"], [{ name: "a.png" }]));
    await settle();
    expect(host.written).toEqual(["already written /ws/uploads/a.png "]);
  });

  it("refuses a drop while the box is readOnly", async () => {
    stubBrowser("/ws/uploads/a.png");
    const host = fakeTextarea("brief", { readOnly: true });
    attachFileDropZone(host as never, () => ({}));
    const ev = dragEvent(["Files"], [{ name: "a.png" }]);
    host.fire("drop", ev);
    await settle();
    expect(host.written).toEqual([]);
    // Not claimed either: the page's global guard still swallows it, so nothing
    // navigates away — it simply does not land in a box that is filing.
    expect(ev.preventDefault).not.toHaveBeenCalled();
  });

  it("does not write anything when every upload fails", async () => {
    const g = globalThis as Record<string, unknown>;
    stubBrowser("/unused");
    g.fetch = vi.fn(async () => ({ status: 500, ok: false, text: async () => "nope" }));
    const host = fakeTextarea("keep me");
    attachFileDropZone(host as never, () => ({}));
    host.fire("drop", dragEvent(["Files"], [{ name: "a.png" }]));
    await settle();
    expect(host.written).toEqual([]);
  });

  it("reads its options at drop time, so a re-render is not a stale closure", async () => {
    // The listeners are bound once and the options come from an inline object
    // that is new on every render. Whatever the zone reads has to be the CURRENT
    // one — here that decides which session's workspace receives the upload.
    stubBrowser("/ws/uploads/a.png");
    const host = fakeTextarea("");
    let current = "old-session";
    attachFileDropZone(host as never, () => ({ session: current }));
    current = "new-session"; // the component re-rendered between wiring and drop
    host.fire("drop", dragEvent(["Files"], [{ name: "a.png" }]));
    await settle();
    const url = (globalThis as unknown as { fetch: { mock: { calls: string[][] } } }).fetch.mock
      .calls[0][0];
    expect(url).toContain("session=new-session");
    expect(url).not.toContain("old-session");
  });

  // The regression this file exists to prevent. The new-session dialog renders
  // its prompt form and the ticket pane on mutually exclusive tabs, so the
  // textarea is NOT present for the life of the component that wires it. The
  // first version used useRef + useEffect, bound once at mount, found null, and
  // left the box permanently dead for anyone who opened the dialog on the other
  // tab — with no error anywhere to say so.
  it("re-wires when the textarea is unmounted and mounted again", async () => {
    stubBrowser("/ws/uploads/a.png");
    const mirror: { current: unknown } = { current: null };
    const ref = makeCallbackRef({}, mirror);

    const first = fakeTextarea("");
    ref(first as never);
    expect(first.count("drop")).toBe(1);
    expect(mirror.current).toBe(first);

    ref(null); // tab switched away
    expect(first.count("drop")).toBe(0);
    expect(mirror.current).toBe(null);

    const second = fakeTextarea("back"); // tab switched back: a NEW element
    ref(second as never);
    second.fire("drop", dragEvent(["Files"], [{ name: "a.png" }]));
    await settle();
    expect(second.written).toEqual(["back /ws/uploads/a.png "]);
  });

  // Page 1 of the new-session form is one question and three buttons: the prompt
  // fold is on page 2, so there is no textarea in the card at all. A zone that
  // only knew how to write into a box would accept the drop and lose the file.
  it("falls back to the card when no textarea is mounted", async () => {
    stubBrowser("/ws/uploads/a.png");
    const host = fakeTextarea("");
    host.querySelector = () => null; // page 1: nothing to write into
    const onInsert = vi.fn();
    attachFileDropZone(host as never, () => ({ onInsert }));
    host.fire("drop", dragEvent(["Files"], [{ name: "a.png" }]));
    await settle();
    expect(onInsert).toHaveBeenCalledWith("/ws/uploads/a.png");
  });

  it("does not claim the drag when there is neither a box nor a fallback", () => {
    const host = fakeTextarea("");
    host.querySelector = () => null;
    attachFileDropZone(host as never, () => ({}));
    const ev = dragEvent(["Files"]);
    host.fire("dragover", ev);
    expect(ev.preventDefault).not.toHaveBeenCalled();
    expect(host.classes.has("ta-file-drop")).toBe(false);
  });

  it("unwires every listener it added", () => {
    const host = fakeTextarea();
    const detach = attachFileDropZone(host as never, () => ({}));
    expect(host.count("dragover") + host.count("dragleave") + host.count("drop")).toBe(3);
    detach();
    expect(host.count("dragover") + host.count("dragleave") + host.count("drop")).toBe(0);
  });
});
