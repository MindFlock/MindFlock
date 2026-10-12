/** Touch scrolling on a desktop-grid terminal (lib/terminals attachWheelScroll).
 *
 * On a phone there are no wheel events, and the agent TUI is an alt-screen app
 * whose history lives on the tmux side — xterm's local scrollback is empty, so
 * a touch drag had nothing to scroll and the terminal appeared frozen. A
 * vertical one-finger drag is translated into the same SGR mouse-wheel ticks
 * the wheel path sends down the PTY (natural direction: content follows the
 * finger), three ticks per cell dragged (1:1 felt "far too slow"), capped at
 * 24 per event.
 *
 * Moved from tests/unit/test_frontend_mobile.py, which grepped the built
 * bundle for the handler's source; here the handler is driven. The /m view's
 * hand-written mobile.js twin stays pinned in Python. */

import { describe, expect, it, vi } from "vitest";
import type { Terminal } from "@xterm/xterm";
import { attachWheelScroll } from "../lib/terminals";

const OPEN = 1;
type Listener = (ev: unknown) => void;

/** A host 800×480 at the origin, 80×24 cells: a cell is 10×20 px. */
function rig(readyState = OPEN) {
  const listeners: Record<string, { fn: Listener; opts: AddEventListenerOptions }> = {};
  const host = {
    style: {} as Record<string, string>,
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 800, height: 480 }),
    addEventListener: (type: string, fn: Listener, opts: AddEventListenerOptions) => {
      listeners[type] = { fn, opts };
    },
  };
  const sent: string[] = [];
  const ws = { readyState, send: (s: string) => sent.push(s) };
  attachWheelScroll(host as unknown as HTMLElement, { rows: 24, cols: 80 } as Terminal, () => ws as unknown as WebSocket);
  const touch = (type: string, pts: Array<[number, number]>) => {
    const ev = {
      touches: pts.map(([clientX, clientY]) => ({ clientX, clientY })),
      preventDefault: vi.fn(),
      stopPropagation: vi.fn(),
    };
    listeners[type].fn(ev);
    return ev;
  };
  return { host, listeners, sent, touch };
}

/** The SGR wheel ticks in one send: 64 = wheel up, 65 = wheel down. */
const ticks = (seq: string) => [...seq.matchAll(/\x1b\[<(\d+);(\d+);(\d+)M/g)].map((m) => m.slice(1).map(Number));

describe("touch scroll on a PTY terminal", () => {
  it("turns the browser's own gestures off and listens to all three touch phases", () => {
    const { host, listeners } = rig();
    expect(host.style.touchAction).toBe("none");
    for (const t of ["touchstart", "touchmove", "touchend"]) expect(listeners[t], t).toBeDefined();
    // touchmove must be non-passive to be able to preventDefault page panning.
    expect(listeners.touchmove.opts).toEqual({ capture: true, passive: false });
    expect(listeners.wheel.opts).toEqual({ capture: true, passive: false });
  });

  it("finger down reveals earlier output: wheel UP, 3 ticks per cell dragged", () => {
    const { sent, touch } = rig();
    touch("touchstart", [[400, 100]]);
    const ev = touch("touchmove", [[400, 140]]); // 40px = 2 cells → 6 ticks
    expect(sent).toHaveLength(1);
    const t = ticks(sent[0]);
    expect(t).toHaveLength(6);
    // Same SGR encoding and cell maths as the wheel: col 41, row 8.
    expect(t.every(([btn, col, row]) => btn === 64 && col === 41 && row === 8)).toBe(true);
    expect(ev.preventDefault).toHaveBeenCalled();
  });

  it("finger up scrolls toward newer output: wheel DOWN", () => {
    const { sent, touch } = rig();
    touch("touchstart", [[400, 300]]);
    touch("touchmove", [[400, 260]]);
    expect(ticks(sent[0]).map(([btn]) => btn)).toEqual([65, 65, 65, 65, 65, 65]);
  });

  it("caps one event at 24 ticks", () => {
    const { sent, touch } = rig();
    touch("touchstart", [[400, 0]]);
    touch("touchmove", [[400, 470]]);
    expect(ticks(sent[0])).toHaveLength(24);
  });

  it("leaves pinches and multi-touch alone", () => {
    const { sent, touch } = rig();
    touch("touchstart", [[400, 100], [500, 100]]);
    const ev = touch("touchmove", [[400, 200], [500, 200]]);
    expect(sent).toEqual([]);
    expect(ev.preventDefault).not.toHaveBeenCalled();
  });

  it("only scrolls a clearly vertical drag: taps and horizontal gestures pass through", () => {
    const { sent, touch } = rig();
    touch("touchstart", [[400, 100]]);
    const sideways = touch("touchmove", [[460, 130]]); // |dy| <= |dx|
    const tiny = touch("touchmove", [[400, 105]]); // under the 8px threshold
    expect(sent).toEqual([]);
    expect(sideways.preventDefault).not.toHaveBeenCalled();
    expect(tiny.preventDefault).not.toHaveBeenCalled();
  });

  it("steps aside for xterm's own scrolling when there is no live PTY", () => {
    const { sent, touch } = rig(3 /* CLOSED */);
    touch("touchstart", [[400, 100]]);
    const ev = touch("touchmove", [[400, 200]]);
    expect(sent).toEqual([]);
    expect(ev.preventDefault).not.toHaveBeenCalled();
  });

  it("the wheel sends through the same tick sender", () => {
    const { sent, listeners } = rig();
    listeners.wheel.fn({
      deltaY: -3,
      deltaMode: 1,
      clientX: 400,
      clientY: 150,
      timeStamp: 1000,
      preventDefault: () => {},
      stopPropagation: () => {},
    });
    expect(ticks(sent[0])).toEqual([
      [64, 41, 8],
      [64, 41, 8],
      [64, 41, 8],
    ]);
  });
});
