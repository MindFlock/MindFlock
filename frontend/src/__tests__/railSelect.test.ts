/** Selecting rows on the unified rail — the capped-view demotion must judge
 * visibility over the UNION the grid renders (sessions AND windows, one cap,
 * one MRU). The bug this pins: sessions-only computeVisible over-reported, so
 * selecting a session evicted the on-screen window and swapped in a hidden
 * session instead of taking the focused session's spot. */
import { describe, it, expect, beforeEach } from "vitest";
import type { Instance } from "../api/types";
import { queryClient } from "../state/queries";
import { useUi, windowKey } from "../state/store";
import { selectRailKey, selectSession } from "../lib/sessionActions";
import { computeVisibleSlots } from "../components/grid/layout";

// selectWindow schedules a best-effort scroll; the node test env has no rAF
// and the callback must simply never run.
(globalThis as Record<string, unknown>).requestAnimationFrame ??= () => 0;

const insts = (...titles: string[]): Instance[] =>
  titles.map((title) => ({ title }) as unknown as Instance);
const CHAT = windowKey("chat");

/** The grid's slot list, exactly as TerminalGrid derives it. */
function slots(): string[] {
  const s = useUi.getState();
  return computeVisibleSlots(
    (queryClient.getQueryData(["instances"]) as Instance[]) || [],
    s.specialOpen.map((k) => windowKey(k)),
    { hidden: s.hidden, viewMode: s.viewMode, mru: s.mru, order: s.order }
  );
}

describe("selectSession under a capped view with a window on screen", () => {
  beforeEach(() => {
    queryClient.setQueryData(["instances"], insts("A", "B", "C"));
    useUi.setState({
      viewMode: "2" as never,
      specialOpen: ["chat"],
      verifyPanes: [],
      extPanes: [],
      mru: [CHAT, "A", "B", "C"],
      order: [],
      hidden: new Set(),
      focused: "A",
    });
  });

  it("the incoming session takes the FOCUSED session's spot; the window stays", () => {
    expect(slots()).toEqual(["A", CHAT]); // the precondition the grid shows
    selectSession("C", { noKeyboard: true });
    expect(slots()).toEqual(["C", CHAT]);
  });

  it("selectRailKey routes a sentinel to window selection: MRU head, focus untouched", () => {
    selectRailKey(CHAT);
    expect(useUi.getState().mru[0]).toBe(CHAT);
    expect(useUi.getState().focused).toBe("A");
  });
});
