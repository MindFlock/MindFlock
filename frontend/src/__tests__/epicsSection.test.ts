import { describe, expect, it } from "vitest";
import { epicMeta } from "../components/intake/EpicsSection";

describe("epicMeta", () => {
  it("says the state, your open stories of the total, and auto", () => {
    expect(epicMeta({ state: "On Deck", mine_open: 3, stories_total: 13, auto: true })).toBe("On Deck · 3 of yours to do (of 13) · starts by itself");
    expect(epicMeta({ state: "to do", mine_open: null, stories_total: 5, auto: false })).toBe("to do · 5 stories");
    expect(epicMeta({ state: "", mine_open: 0, stories_total: 0, auto: false })).toBe("0 of yours to do");
  });
});
