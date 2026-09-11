/** `attachFileDrop` — the gesture that gets a file from the browser to an agent.
 *
 * The agent CLI runs on this machine and cannot see the browser, so a dropped
 * file has to become an uploaded path typed into the PTY. Two callers share the
 * helper (session terminals, and the assistant window through `useWsTerm`), and
 * the assistant one is why it exists as a helper at all: it went without the
 * wiring, so dropping a screenshot on the chat did nothing while the same drop
 * on the pane beside it worked.
 *
 * The environment is node (see vitest.config), so the host and the events are
 * stubs — which suits this file: what is being pinned is the DECISIONS (what to
 * claim, what to leave alone, what to unwire), not xterm or the DOM.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { attachFileDrop } from "../lib/clipboard";
import type { Terminal } from "@xterm/xterm";

/** A stand-in for the terminal host element: the four things the helper uses. */
function fakeHost() {
  const handlers = new Map<string, Set<(ev: unknown) => void>>();
  const classes = new Set<string>();
  return {
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
    /** Fire every listener registered for `type`, in order. */
    fire(type: string, ev: Record<string, unknown>) {
      for (const fn of handlers.get(type) || []) fn(ev);
      return ev;
    },
    count(type: string) {
      return handlers.get(type)?.size ?? 0;
    },
  };
}

function dragEvent(types: string[], files: unknown[] = []) {
  return {
    preventDefault: vi.fn(),
    stopPropagation: vi.fn(),
    dataTransfer: { types, files, dropEffect: "" },
  };
}

const term = () => ({ paste: vi.fn() }) as unknown as Terminal;

/** The document/fetch the upload path reaches: a toast and one POST. */
function stubBrowser(path: string) {
  const g = globalThis as Record<string, unknown>;
  g.document = {
    getElementById: () => null,
    createElement: () => ({
      style: {},
      classList: { add() {}, remove() {}, toggle() {} },
      addEventListener() {},
      remove() {},
    }),
    body: { appendChild() {} },
  };
  const fetchMock = vi.fn(async () => ({
    status: 200,
    ok: true,
    text: async () => JSON.stringify({ path }),
  }));
  g.fetch = fetchMock;
  return fetchMock;
}

describe("attachFileDrop", () => {
  beforeEach(() => {
    const g = globalThis as Record<string, unknown>;
    delete g.document;
    delete g.fetch;
  });

  it("claims a drag that carries files, and shows where it will land", () => {
    const host = fakeHost();
    attachFileDrop(host as unknown as HTMLElement, term());
    const ev = dragEvent(["Files"]);
    host.fire("dragover", ev);
    expect(ev.preventDefault).toHaveBeenCalled();
    expect(ev.dataTransfer.dropEffect).toBe("copy");
    expect(host.classes.has("file-drop")).toBe(true);
    host.fire("dragleave", { relatedTarget: null });
    expect(host.classes.has("file-drop")).toBe(false);
  });

  it("ignores a drag that carries no files", () => {
    // A window row being dragged across the grid to rearrange panes. Claiming
    // it here would break the rearrange and paint a drop cue over a terminal
    // nothing is being dropped on.
    const host = fakeHost();
    attachFileDrop(host as unknown as HTMLElement, term());
    const ev = dragEvent(["text/plain"]);
    host.fire("dragover", ev);
    expect(ev.preventDefault).not.toHaveBeenCalled();
    expect(host.classes.has("file-drop")).toBe(false);
    const drop = dragEvent(["text/plain"]);
    host.fire("drop", drop);
    expect(drop.preventDefault).not.toHaveBeenCalled();
  });

  it("uploads a dropped file and types its saved path into the terminal", async () => {
    const fetchMock = stubBrowser("/home/me/.mindflock/pastes/paste-a1-shot.png");
    const host = fakeHost();
    const t = term();
    attachFileDrop(host as unknown as HTMLElement, t);
    const file = { name: "shot.png", type: "image/png" };
    const ev = dragEvent(["Files"], [file]);
    host.fire("drop", ev);
    // The pane underneath carries its own drop handler for rearranging
    // windows; a file dropped on a terminal is not a pane being moved.
    expect(ev.preventDefault).toHaveBeenCalled();
    expect(ev.stopPropagation).toHaveBeenCalled();
    await vi.waitFor(() => expect(t.paste).toHaveBeenCalled());
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const url = String((fetchMock.mock.calls[0] as unknown[])[0]);
    // No session: a window with no workspace of its own uploads to the global
    // pastes directory, and the path handed over is absolute either way.
    expect(url).toBe("/api/paste-image?name=shot.png");
    expect(t.paste).toHaveBeenCalledWith(
      "/home/me/.mindflock/pastes/paste-a1-shot.png ",
    );
  });

  it("sends a session's uploads into that session's workspace", () => {
    const fetchMock = stubBrowser("/work/sc-1/.mindflock_pastes/paste-a1-shot.png");
    const host = fakeHost();
    attachFileDrop(host as unknown as HTMLElement, term(), "sc-1");
    host.fire("drop", dragEvent(["Files"], [{ name: "shot.png", type: "image/png" }]));
    expect(String((fetchMock.mock.calls[0] as unknown[])[0])).toBe(
      "/api/paste-image?session=sc-1&name=shot.png",
    );
  });

  it("unwires everything, so a re-run cannot upload a drop twice", () => {
    // The assistant's host div belongs to React and outlives the effect that
    // wires this up; without the disposer a re-run stacks a second set of
    // listeners on the same element and every dropped file uploads twice.
    const host = fakeHost();
    const detach = attachFileDrop(host as unknown as HTMLElement, term());
    host.classList.add("file-drop");
    expect(host.count("drop")).toBe(1);
    detach();
    for (const type of ["paste", "dragover", "dragleave", "drop"])
      expect(host.count(type)).toBe(0);
    expect(host.classes.has("file-drop")).toBe(false);
  });
});
