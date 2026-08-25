import { describe, it, expect } from "vitest";
import { dropIndex, hoverSlot, reorderItems } from "../components/grid/queueDnd";

describe("hoverSlot", () => {
  it("maps the top half of a row to 'before it' and the bottom half to 'after it'", () => {
    expect(hoverSlot(2, 10, 40)).toBe(2); // upper half → slot 2
    expect(hoverSlot(2, 30, 40)).toBe(3); // lower half → slot 3
    expect(hoverSlot(0, 20, 40)).toBe(1); // exactly the midpoint counts as below
  });
});

describe("dropIndex", () => {
  it("keeps the slot when dragging upward", () => {
    expect(dropIndex(3, 1)).toBe(1);
  });

  it("shifts down by one when dragging downward (the item's own removal moves the slot)", () => {
    expect(dropIndex(0, 3)).toBe(2);
  });

  it("treats a drop onto the item's own edges as a no-op", () => {
    expect(dropIndex(2, 2)).toBeNull(); // just above itself
    expect(dropIndex(2, 3)).toBeNull(); // just below itself
  });
});

describe("reorderItems", () => {
  it("moves an item without mutating the original list", () => {
    const items = ["a", "b", "c", "d"];
    expect(reorderItems(items, 3, 0)).toEqual(["d", "a", "b", "c"]);
    expect(reorderItems(items, 0, 2)).toEqual(["b", "c", "a", "d"]);
    expect(items).toEqual(["a", "b", "c", "d"]);
  });
});

describe("a full drag, end to end", () => {
  it("dragging the last item over the top half of the first row puts it first", () => {
    const items = ["a", "b", "c"];
    const slot = hoverSlot(0, 5, 40);
    const to = dropIndex(2, slot);
    expect(to).toBe(0);
    expect(reorderItems(items, 2, to as number)).toEqual(["c", "a", "b"]);
  });

  it("dragging the first item below the last row puts it last", () => {
    const items = ["a", "b", "c"];
    const slot = hoverSlot(2, 35, 40); // bottom half of the last row → slot 3
    const to = dropIndex(0, slot);
    expect(to).toBe(2);
    expect(reorderItems(items, 0, to as number)).toEqual(["b", "c", "a"]);
  });
});
