/** Panes that went blank WHITE until a small scroll (owner bug 2026-11-10).
 *
 * The content was never lost — a wheel tick made tmux redraw, xterm rewrote
 * its rows, and that rewrite is what got the pane painted again. Nothing else
 * ever did after the window came back from minimized / covered / another
 * display / a GPU restart. The fix repaints (fit + full refresh) every
 * terminal on screen on those triggers; these pin the rules with fake
 * terminals, since vitest here is node-only (no DOM, no xterm).
 */
import { describe, it, expect } from "vitest";
import {
  installRepaintTriggers,
  isShown,
  repaintShown,
  repaintTerm,
  REPAINT_EVENT,
  type Repaintable,
} from "../lib/wsTerm";

function fakeTerm(rows = 24) {
  const calls: Array<[number, number]> = [];
  return { rows, calls, refresh: (a: number, b: number) => void calls.push([a, b]) };
}

describe("repaintTerm", () => {
  it("fits first, then redraws every row", () => {
    const order: string[] = [];
    const t = { rows: 30, refresh: (a: number, b: number) => void order.push(`refresh ${a}-${b}`) };
    expect(repaintTerm(t, () => order.push("fit"))).toBe(true);
    expect(order).toEqual(["fit", "refresh 0-29"]);
  });

  it("still repaints when the fit throws (hidden / zero-sized)", () => {
    const t = fakeTerm(10);
    expect(
      repaintTerm(t, () => {
        throw new Error("no size");
      }),
    ).toBe(true);
    expect(t.calls).toEqual([[0, 9]]);
  });

  it("skips a missing or disposed terminal without throwing", () => {
    expect(repaintTerm(undefined)).toBe(false);
    expect(repaintTerm(fakeTerm(0))).toBe(false);
    const dead = {
      rows: 5,
      refresh: () => {
        throw new Error("disposed");
      },
    };
    expect(repaintTerm(dead)).toBe(false);
  });
});

describe("isShown", () => {
  it("is false for display:none (no offsetParent) or a zero-sized box", () => {
    expect(isShown(null)).toBe(false);
    expect(isShown({ clientWidth: 400, clientHeight: 300, offsetParent: null })).toBe(false);
    expect(isShown({ clientWidth: 0, clientHeight: 300, offsetParent: {} })).toBe(false);
    expect(isShown({ clientWidth: 400, clientHeight: 300, offsetParent: {} })).toBe(true);
  });
});

describe("repaintShown", () => {
  it("repaints every visible terminal and skips hidden ones", () => {
    const a = fakeTerm(24);
    const b = fakeTerm(40);
    const hidden = fakeTerm(24);
    const items: Repaintable[] = [
      { shown: () => true, repaint: () => repaintTerm(a) },
      { shown: () => false, repaint: () => repaintTerm(hidden) },
      { shown: () => true, repaint: () => repaintTerm(b) },
    ];
    expect(repaintShown(items)).toBe(2);
    expect(a.calls).toEqual([[0, 23]]);
    expect(b.calls).toEqual([[0, 39]]);
    expect(hidden.calls).toEqual([]);
  });

  it("one broken entry never stops the rest", () => {
    const ok = fakeTerm(5);
    const items: Repaintable[] = [
      {
        shown: () => true,
        repaint: () => {
          throw new Error("torn down");
        },
      },
      { shown: () => true, repaint: () => repaintTerm(ok) },
    ];
    expect(repaintShown(items)).toBe(1);
    expect(ok.calls).toEqual([[0, 4]]);
  });
});

/** A tiny event target + manual scheduler: the triggers' wiring without a DOM. */
function harness() {
  const listeners = new Map<string, Set<() => void>>();
  const target = (prefix: string) => ({
    addEventListener(type: string, fn: () => void) {
      const k = prefix + type;
      if (!listeners.has(k)) listeners.set(k, new Set());
      listeners.get(k)!.add(fn);
    },
    removeEventListener(type: string, fn: () => void) {
      listeners.get(prefix + type)?.delete(fn);
    },
  });
  const fire = (k: string) => listeners.get(k)?.forEach((fn) => fn());
  const frames: Array<() => void> = [];
  const timers = new Map<number, () => void>();
  let nextId = 1;
  const schedule = {
    frame: (fn: () => void) => void frames.push(fn),
    later: (fn: () => void) => {
      const id = nextId++;
      timers.set(id, fn);
      return id;
    },
    cancel: (id: unknown) => void timers.delete(id as number),
  };
  const flushFrames = () => frames.splice(0).forEach((fn) => fn());
  const flushTimers = () => {
    const all = [...timers.values()];
    timers.clear();
    all.forEach((fn) => fn());
  };
  const doc = { ...target("doc:"), hidden: false };
  return { win: target("win:"), doc, fire, schedule, flushFrames, flushTimers, listeners };
}

describe("installRepaintTriggers", () => {
  it("repaints on focus, visibility, pageshow and the desktop shell's event", () => {
    for (const ev of ["win:focus", "win:pageshow", "win:" + REPAINT_EVENT, "doc:visibilitychange"]) {
      const h = harness();
      let runs = 0;
      installRepaintTriggers(h.win, h.doc, () => runs++, h.schedule);
      h.fire(ev);
      expect(runs, ev).toBe(0); // deferred to the next frame
      h.flushFrames();
      expect(runs, ev).toBe(1);
      h.flushTimers();
      expect(runs, ev).toBe(2); // the settle pass
    }
  });

  it("coalesces a burst (focus + visibilitychange together) into one frame pass + one settle pass", () => {
    const h = harness();
    let runs = 0;
    installRepaintTriggers(h.win, h.doc, () => runs++, h.schedule);
    h.fire("win:focus");
    h.fire("doc:visibilitychange");
    h.fire("win:" + REPAINT_EVENT);
    h.flushFrames();
    expect(runs).toBe(1);
    h.flushTimers();
    expect(runs).toBe(2);
  });

  it("does nothing while the page is hidden — the show that follows repaints", () => {
    const h = harness();
    let runs = 0;
    installRepaintTriggers(h.win, h.doc, () => runs++, h.schedule);
    h.doc.hidden = true;
    h.fire("doc:visibilitychange");
    h.flushFrames();
    h.flushTimers();
    expect(runs).toBe(0);
    h.doc.hidden = false;
    h.fire("doc:visibilitychange");
    h.flushFrames();
    expect(runs).toBe(1);
  });

  it("uninstalls cleanly", () => {
    const h = harness();
    let runs = 0;
    const off = installRepaintTriggers(h.win, h.doc, () => runs++, h.schedule);
    off();
    for (const set of h.listeners.values()) expect(set.size).toBe(0);
    h.fire("win:focus");
    h.flushFrames();
    h.flushTimers();
    expect(runs).toBe(0);
  });

  it("end to end: a focus repaints the visible terminals only", () => {
    const h = harness();
    const shown = fakeTerm(20);
    const hidden = fakeTerm(20);
    const items: Repaintable[] = [
      { shown: () => true, repaint: () => repaintTerm(shown) },
      { shown: () => false, repaint: () => repaintTerm(hidden) },
    ];
    installRepaintTriggers(h.win, h.doc, () => repaintShown(items), h.schedule);
    h.fire("win:focus");
    h.flushFrames();
    expect(shown.calls).toEqual([[0, 19]]);
    expect(hidden.calls).toEqual([]);
  });
});
