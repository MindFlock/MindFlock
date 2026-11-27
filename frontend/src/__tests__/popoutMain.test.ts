/** Pop-out windows, the MAIN window's half (lib/popout is the shared API):
 *
 *  - a popped-out session takes no grid slot (computeVisibleSlots), is NOT
 *    hidden and keeps its order and MRU, so it comes back as it left; its
 *    place in the saved rows is parked, so it returns to the very spot;
 *  - the tear-off decision (grid/tearOff dragEndedOutside) is pure: nothing
 *    took the drop AND the pointer is outside the window — with the 0,0
 *    dragend fallback (last drag position, the drag leaving the document);
 *  - the action table: Move to new window / Back to main window / Show its
 *    window, each where it applies; the session menu draws them;
 *  - selecting a popped-out session raises its window and changes nothing
 *    here; docking it closes the window and selects it in the grid;
 *  - a blocked popup offers the window again from a toast button;
 *  - a rename moves the pop-out with the title. */

/// <reference types="vite/client" />
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Caps, Instance } from "../api/types";

const storage = new Map<string, string>();
vi.stubGlobal("localStorage", {
  getItem: (k: string) => (storage.has(k) ? storage.get(k)! : null),
  setItem: (k: string, v: string) => void storage.set(k, String(v)),
  removeItem: (k: string) => void storage.delete(k),
  clear: () => storage.clear(),
});

const focusPopout = vi.fn((t: string) => t.length > 0);
const closePopout = vi.fn();
const renamePopout = vi.fn();
const openPopout = vi.fn(async (..._a: unknown[]) => true);
const focusMainWindow = vi.fn();
let supported = true;
vi.mock("../lib/popout", async (orig) => ({
  ...((await orig()) as object),
  focusPopout: (t: string) => focusPopout(t),
  closePopout: (t: string) => closePopout(t),
  renamePopout: (a: string, b: string) => renamePopout(a, b),
  openPopout: (...a: unknown[]) => openPopout(...a),
  focusMainWindow: () => focusMainWindow(),
  popoutSupported: () => supported,
}));
const toast = vi.fn();
vi.mock("../lib/toast", () => ({ toast: (...a: unknown[]) => toast(...a) }));
const errorPop = vi.fn();
vi.mock("../lib/errorPop", () => ({
  errorPop: (...a: unknown[]) => errorPop(...a),
  confirmPop: vi.fn(),
}));

const { usePopouts } = await import("../lib/popout");
const { computeVisible, computeVisibleSlots, reconcileGridRows, withParked, withoutParked } = await import(
  "../components/grid/layout"
);
const { dragEndedOutside, tearOffPlacement } = await import("../components/grid/tearOff");
const { SESSION_ACTIONS, actionCtx } = await import("../lib/sessionActionTable");
const { sessionMenu } = await import("../lib/sessionMenu");
const { dockSession, popOutSession, selectSession } = await import("../lib/sessionActions");
const { moveSessionTitle } = await import("../lib/renameSession");
const { queryClient } = await import("../state/queries");
const { useUi } = await import("../state/store");

const insts = (...titles: string[]): Instance[] =>
  titles.map((title) => ({ title, status: "ready" }) as unknown as Instance);
const CAPS: Caps = { git: true, tailscale: true, ticketing: true, github: true };
const opts = (o: Partial<Parameters<typeof computeVisibleSlots>[2]> = {}) => ({
  hidden: new Set<string>(),
  viewMode: "auto",
  mru: [] as string[],
  order: [] as string[],
  ...o,
});

beforeEach(() => {
  usePopouts.getState().set([]);
  supported = true;
});
afterEach(() => {
  vi.clearAllMocks();
  queryClient.setQueryData(["instances"], []);
});

describe("a popped-out session leaves the grid, and comes back as it left", () => {
  it("takes no slot while out; the slot set is the same once it returns", () => {
    const all = insts("A", "B", "C");
    const before = computeVisibleSlots(all, [], opts());
    expect(before).toEqual(["A", "B", "C"]);
    expect(computeVisibleSlots(all, [], opts({ poppedOut: new Set(["B"]) }))).toEqual(["A", "C"]);
    expect(computeVisibleSlots(all, [], opts({ poppedOut: new Set() }))).toEqual(before);
    // The rail's sessions-only view agrees.
    expect(computeVisible(all, opts({ poppedOut: new Set(["B"]) })).map((i) => i.title)).toEqual(["A", "C"]);
  });

  it("a capped view gives its slot to the next one, and it wins it back on its MRU", () => {
    const all = insts("A", "B", "C");
    const o = opts({ viewMode: "2", mru: ["B", "A", "C"] });
    expect(computeVisibleSlots(all, [], o)).toEqual(["A", "B"]);
    expect(computeVisibleSlots(all, [], { ...o, poppedOut: new Set(["B"]) })).toEqual(["A", "C"]);
    expect(computeVisibleSlots(all, [], o)).toEqual(["A", "B"]);
  });

  it("its place in the rows is parked (kept, not drawn) and it returns to the very spot", () => {
    const live = new Set(["A", "B", "C", "D"]);
    const prev = [["A", "B"], ["C", "D"]];
    const out = new Set(["B"]);
    const slots = computeVisibleSlots(insts("A", "B", "C", "D"), [], opts({ poppedOut: out }));
    const rows = reconcileGridRows(prev, withParked(slots, prev, out, live, new Set()));
    expect(rows).toEqual(prev); // the saved layout still holds B's place
    expect(withoutParked(rows, out)).toEqual([["A"], ["C", "D"]]); // drawn without it
    // Back: the plain reconcile keeps it exactly there.
    const back = computeVisibleSlots(insts("A", "B", "C", "D"), [], opts());
    expect(reconcileGridRows(rows, back)).toEqual(prev);
  });

  it("without parking it would come back at the END — the reason parking exists", () => {
    const prev = [["A", "B"], ["C", "D"]];
    const gone = reconcileGridRows(prev, ["A", "C", "D"]);
    expect(reconcileGridRows(gone, ["A", "B", "C", "D"]).flat().at(-1)).toBe("B");
  });

  it("a parked place is let go when the session is hidden or gone", () => {
    const prev = [["A", "B"]];
    const out = new Set(["B"]);
    expect(withParked(["A"], prev, out, new Set(["A"]), new Set())).toEqual(["A"]);
    expect(withParked(["A"], prev, out, new Set(["A", "B"]), new Set(["B"]))).toEqual(["A"]);
    expect(withParked(["A"], prev, new Set(), new Set(["A", "B"]), new Set())).toEqual(["A"]);
  });

  it("is not hidden and keeps its order and MRU", () => {
    queryClient.setQueryData(["instances"], insts("A", "B"));
    useUi.setState({ hidden: new Set(), mru: ["B", "A"], order: ["A", "B"], focused: "A" });
    usePopouts.getState().set(["B"]);
    const s = useUi.getState();
    expect(s.hidden.has("B")).toBe(false);
    expect(s.mru).toEqual(["B", "A"]);
    expect(s.order).toEqual(["A", "B"]);
  });
});

describe("the tear-off decision (dragEndedOutside)", () => {
  const win = { screenX: 100, screenY: 50, outerWidth: 1000, outerHeight: 700 };

  it("a drop on a target is never a tear-off, wherever the pointer is", () => {
    expect(dragEndedOutside({ dropEffect: "move", screenX: 2000, screenY: 2000 }, win)).toBeNull();
  });

  it("nothing took it and the pointer is outside: tear off there", () => {
    expect(dragEndedOutside({ dropEffect: "none", screenX: 1200, screenY: 300 }, win)).toEqual({ x: 1200, y: 300 });
    expect(dragEndedOutside({ dropEffect: "none", screenX: 50, screenY: 300 }, win)).toEqual({ x: 50, y: 300 });
    expect(dragEndedOutside({ dropEffect: "none", screenX: 500, screenY: 760 }, win)).toEqual({ x: 500, y: 760 });
  });

  it("nothing took it but the pointer is inside (a cancel, a drop on bare grid): no", () => {
    expect(dragEndedOutside({ dropEffect: "none", screenX: 500, screenY: 300 }, win)).toBeNull();
  });

  it("a 0,0 dragend (some Linux / WSLg builds) falls back to the drag's last position", () => {
    const trail = { left: false, lastX: 1300, lastY: 200 };
    expect(dragEndedOutside({ dropEffect: "none", screenX: 0, screenY: 0 }, win, trail)).toEqual({ x: 1300, y: 200 });
    const inside = { left: false, lastX: 400, lastY: 200 };
    expect(dragEndedOutside({ dropEffect: "none", screenX: 0, screenY: 0 }, win, inside)).toBeNull();
  });

  it("a 0,0 dragend after the drag left the document is a tear-off, with or without a point", () => {
    expect(
      dragEndedOutside({ dropEffect: "none", screenX: 0, screenY: 0 }, win, { left: true, lastX: 1090, lastY: 300 })
    ).toEqual({ x: 1090, y: 300 });
    expect(dragEndedOutside({ dropEffect: "none", screenX: 0, screenY: 0 }, win, { left: true, lastX: null, lastY: null })).toEqual({});
    // A drop still wins over the trail.
    expect(dragEndedOutside({ dropEffect: "move", screenX: 0, screenY: 0 }, win, { left: true, lastX: null, lastY: null })).toBeNull();
  });

  it("the window opens under the grip, at the dragged pane's size", () => {
    expect(tearOffPlacement({ x: 1300, y: 200 }, 40, { width: 640, height: 480 })).toEqual({
      x: 1260,
      y: 188,
      width: 640,
      height: 480,
    });
    expect(tearOffPlacement({}, 40)).toEqual({});
  });
});

describe("the action table and the session menu", () => {
  const ctx = (popped: boolean) =>
    actionCtx("s", { inst: insts("s")[0], caps: CAPS, ideName: "Cursor", hidden: false, popped });

  it("Move to new window: where pop-outs work and it is not out yet", () => {
    expect(SESSION_ACTIONS.popout.label).toBe("Move to new window");
    expect(SESSION_ACTIONS.popout.enabled(ctx(false))).toBe(true);
    expect(SESSION_ACTIONS.popout.enabled(ctx(true))).toBe(false);
    supported = false;
    expect(SESSION_ACTIONS.popout.enabled(ctx(false))).toBe(false);
    supported = true;
    const pending = actionCtx("s", { inst: { ...insts("s")[0], pending: true } as Instance, caps: CAPS, popped: false });
    expect(SESSION_ACTIONS.popout.enabled(pending)).toBe(false);
  });

  it("Back to main window and Show its window: only once it is out", () => {
    expect(SESSION_ACTIONS.dock.label).toBe("Back to main window");
    expect(SESSION_ACTIONS.showWindow.label).toBe("Show its window");
    for (const id of ["dock", "showWindow"] as const) {
      expect(SESSION_ACTIONS[id].enabled(ctx(true))).toBe(true);
      expect(SESSION_ACTIONS[id].enabled(ctx(false))).toBe(false);
    }
  });

  it("the context reads the live popped-out set by default", () => {
    usePopouts.getState().set(["s"]);
    expect(actionCtx("s", { inst: insts("s")[0], caps: CAPS }).popped).toBe(true);
    expect(actionCtx("t", { inst: insts("t")[0], caps: CAPS }).popped).toBe(false);
  });

  it("the session menu draws them in its Session group", () => {
    const keys = (popped: boolean) =>
      sessionMenu(insts("s")[0], ctx(popped)).groups.flatMap((g) => g.items.map((i) => i.key));
    expect(keys(false)).toContain("popout");
    expect(keys(false)).not.toContain("dock");
    expect(keys(true)).toEqual(expect.arrayContaining(["dock", "showWindow"]));
    expect(keys(true)).not.toContain("popout");
  });

  it("Show its window raises it; Back to main window docks it", () => {
    usePopouts.getState().set(["s"]);
    SESSION_ACTIONS.showWindow.run("s");
    expect(focusPopout).toHaveBeenCalledWith("s");
    SESSION_ACTIONS.dock.run("s");
    expect(closePopout).toHaveBeenCalledWith("s");
  });
});

describe("selecting and docking", () => {
  beforeEach(() => {
    queryClient.setQueryData(["instances"], insts("A", "B", "C"));
    useUi.setState({
      viewMode: "auto" as never,
      specialOpen: [],
      verifyPanes: [],
      extPanes: [],
      mru: ["A", "B", "C"],
      order: [],
      hidden: new Set(),
      focused: "A",
      gridRows: [["A", "B"], ["C"]],
    });
  });

  it("selecting a popped-out session raises its window and changes nothing here", () => {
    usePopouts.getState().set(["C"]);
    selectSession("C", { noKeyboard: true });
    expect(focusPopout).toHaveBeenCalledWith("C");
    const s = useUi.getState();
    expect(s.focused).toBe("A");
    expect(s.mru).toEqual(["A", "B", "C"]);
    expect(s.hidden.has("C")).toBe(false);
  });

  it("selecting one that is not out selects it as always", () => {
    selectSession("B", { noKeyboard: true });
    expect(focusPopout).not.toHaveBeenCalled();
    expect(useUi.getState().focused).toBe("B");
  });

  it("docking closes its window, drops it from the set at once, places and selects it", () => {
    usePopouts.getState().set(["C"]);
    dockSession("C", { target: "A", side: "left" });
    expect(closePopout).toHaveBeenCalledWith("C");
    expect(usePopouts.getState().titles.has("C")).toBe(false);
    expect(focusPopout).not.toHaveBeenCalled();
    const s = useUi.getState();
    expect(s.gridRows).toEqual([["C", "A", "B"]]);
    expect(s.focused).toBe("C");
    expect(s.mru[0]).toBe("C");
  });

  it("docking without a drop spot keeps the parked place", () => {
    usePopouts.getState().set(["B"]);
    dockSession("B");
    expect(useUi.getState().gridRows).toEqual([["A", "B"], ["C"]]);
    expect(useUi.getState().focused).toBe("B");
  });
});

describe("popping out", () => {
  it("opens the window at the given place", async () => {
    popOutSession("A", { x: 10, y: 20, width: 300, height: 200 });
    await Promise.resolve();
    expect(openPopout).toHaveBeenCalledWith("A", { x: 10, y: 20, width: 300, height: 200 });
    expect(toast).not.toHaveBeenCalled();
  });

  it("a blocked popup offers it again from a toast button (a click a blocker lets through)", async () => {
    openPopout.mockResolvedValueOnce(false);
    popOutSession("A", { x: 1 });
    await new Promise((r) => setTimeout(r, 0));
    expect(toast).toHaveBeenCalledTimes(1);
    const [msg, o] = toast.mock.calls[0] as [string, { action: { label: string; onClick(): void } }];
    expect(msg).toBe("Your browser blocked the new window");
    expect(o.action.label).toBe("Open in new window");
    openPopout.mockResolvedValueOnce(false);
    o.action.onClick();
    expect(openPopout).toHaveBeenCalledTimes(2);
    expect(openPopout.mock.calls[1]).toEqual(["A", { x: 1 }]);
    await new Promise((r) => setTimeout(r, 0));
    expect(errorPop).toHaveBeenCalledTimes(1);
  });

  it("one already out is shown, not opened again", () => {
    usePopouts.getState().set(["A"]);
    popOutSession("A");
    expect(openPopout).not.toHaveBeenCalled();
    expect(focusPopout).toHaveBeenCalledWith("A");
  });
});

describe("a rename moves its pop-out", () => {
  it("renamePopout follows the title when it is out", () => {
    usePopouts.getState().set(["old"]);
    moveSessionTitle("old", "new");
    expect(renamePopout).toHaveBeenCalledWith("old", "new");
  });

  it("and is not asked when it is not", () => {
    moveSessionTitle("x", "y");
    expect(renamePopout).not.toHaveBeenCalled();
  });
});
