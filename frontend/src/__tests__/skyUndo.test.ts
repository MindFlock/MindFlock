/** Round 14 (docs/flights.md "As built: round 14 sky"): the sky's undo stack
 * (lib/skyUndo). Every removal pushes one entry; Ctrl+Z pops the newest; the
 * removal's toast Undo takes ITS entry, so the two never apply it twice. At
 * most 20 entries, each good for 10 minutes; an entry that no longer applies
 * (`valid`) is passed over. */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { UNDO_MAX, UNDO_TTL_MS, clearUndo, peekUndo, popUndo, pushUndo, takeUndo, undoAction, undoCount } from "../lib/skyUndo";

const entry = (label: string, undo = vi.fn(async () => undefined)) => ({ label, undo });

beforeEach(() => {
  clearUndo();
  vi.useFakeTimers();
  vi.setSystemTime(new Date("2026-10-30T12:00:00Z"));
});
afterEach(() => {
  vi.useRealTimers();
});

describe("the stack", () => {
  it("pops newest first; empty pops null", () => {
    pushUndo(entry("Remove “a”"));
    pushUndo(entry("Remove “b”"));
    expect(undoCount()).toBe(2);
    expect(peekUndo()?.label).toBe("Remove “b”");
    expect(popUndo()?.label).toBe("Remove “b”");
    expect(popUndo()?.label).toBe("Remove “a”");
    expect(popUndo()).toBeNull();
    expect(peekUndo()).toBeNull();
    expect(undoCount()).toBe(0);
  });

  it("holds at most 20: the oldest falls off", () => {
    for (let i = 0; i < UNDO_MAX + 5; i++) pushUndo(entry("e" + i));
    expect(UNDO_MAX).toBe(20);
    expect(undoCount()).toBe(20);
    const labels: string[] = [];
    for (let e = popUndo(); e; e = popUndo()) labels.push(e.label);
    expect(labels[0]).toBe("e24");
    expect(labels[labels.length - 1]).toBe("e5");
  });

  it("each entry is good for 10 minutes", () => {
    expect(UNDO_TTL_MS).toBe(600_000);
    pushUndo(entry("old"));
    vi.advanceTimersByTime(6 * 60_000);
    pushUndo(entry("new"));
    vi.advanceTimersByTime(4 * 60_000);
    // "old" is exactly 10 minutes old: gone; "new" has 6 minutes left.
    expect(undoCount()).toBe(1);
    expect(popUndo()?.label).toBe("new");
    pushUndo(entry("later"));
    vi.advanceTimersByTime(UNDO_TTL_MS);
    expect(popUndo()).toBeNull();
  });

  it("an entry that no longer applies is passed over", () => {
    let open = false;
    pushUndo(entry("Close “a”"));
    pushUndo({ ...entry("Close “b”"), valid: () => !open });
    open = true; // reopened from its own toast meanwhile
    expect(popUndo()?.label).toBe("Close “a”");
  });

  it("the time is a parameter too (no clock needed)", () => {
    pushUndo(entry("x"), 1000);
    expect(undoCount(1000 + UNDO_TTL_MS - 1)).toBe(1);
    expect(undoCount(1000 + UNDO_TTL_MS)).toBe(0);
  });
});

describe("the toast's Undo and Ctrl+Z share one entry", () => {
  it("the toast takes its own entry, once", () => {
    const a = pushUndo(entry("a"));
    const b = pushUndo(entry("b"));
    expect(takeUndo(a)).toBe(true);
    expect(takeUndo(a)).toBe(false);
    expect(popUndo()?.id).toBe(b.id);
  });

  it("after Ctrl+Z popped it, the toast's button does nothing", async () => {
    const undo = vi.fn(async () => undefined);
    const e = pushUndo(entry("Remove “Review”", undo));
    const act = undoAction(e, "Undo: put “Review” back on the line");
    expect(act.label).toBe("Undo");
    expect(act.ariaLabel).toBe("Undo: put “Review” back on the line");
    const popped = popUndo();
    await popped?.undo();
    act.onClick();
    expect(undo).toHaveBeenCalledTimes(1);
  });

  it("after the toast ran it, Ctrl+Z goes on to the one before", () => {
    const first = vi.fn(async () => undefined);
    const second = vi.fn(async () => undefined);
    pushUndo(entry("first", first));
    const e = pushUndo(entry("second", second));
    undoAction(e).onClick();
    expect(second).toHaveBeenCalledTimes(1);
    expect(popUndo()?.label).toBe("first");
  });

  it("an expired entry's toast button does nothing", () => {
    const undo = vi.fn(async () => undefined);
    const e = pushUndo(entry("late", undo));
    vi.advanceTimersByTime(UNDO_TTL_MS + 1);
    undoAction(e).onClick();
    expect(undo).not.toHaveBeenCalled();
  });
});
