import { describe, it, expect } from "vitest";
import {
  SIDEBAR_BARS,
  DEFAULT_VISIBLE_BARS,
  DEFAULT_SECTION_ORDER,
  SESSIONS_KEY,
  defaultHiddenBars,
  orderedSections,
  orderedBars,
} from "../components/sidebar/barDefs";

describe("defaultHiddenBars", () => {
  it("hides every bar that is not one of the essentials", () => {
    const hidden = defaultHiddenBars();
    expect(hidden).toEqual(["ingestion", "pr-review", "issue-handling"]);
    // The essentials are never hidden.
    for (const key of DEFAULT_VISIBLE_BARS) expect(hidden).not.toContain(key);
    // Every bar is accounted for as either visible or hidden.
    expect(hidden.length + DEFAULT_VISIBLE_BARS.length).toBe(SIDEBAR_BARS.length);
  });
});

describe("orderedSections", () => {
  it("returns the full default order for an empty saved order", () => {
    expect(orderedSections([])).toEqual(DEFAULT_SECTION_ORDER);
  });
  it("honors the saved order, then appends the sections it omitted", () => {
    expect(orderedSections([SESSIONS_KEY, "usage"])).toEqual([
      "sessions",
      "usage",
      "ingestion",
      "pr-review",
      "issue-handling",
      "assistant",
    ]);
  });
  it("drops unknown keys from the saved order", () => {
    expect(orderedSections(["bogus", "assistant"])).toEqual([
      "assistant",
      "usage",
      "ingestion",
      "pr-review",
      "issue-handling",
      "sessions",
    ]);
  });
  it("dedupes repeated keys, keeping the first occurrence", () => {
    expect(orderedSections(["usage", "usage"])).toEqual(DEFAULT_SECTION_ORDER);
  });
  it("always includes the sessions anchor even for an order that predates it", () => {
    const legacy = SIDEBAR_BARS.map((b) => b.key); // bars only, no sessions
    const out = orderedSections(legacy);
    expect(out).toContain(SESSIONS_KEY);
    // The anchor lands after the known bars it did not mention.
    expect(out[out.length - 1]).toBe(SESSIONS_KEY);
  });
});

describe("orderedBars", () => {
  it("returns the bar defs in section order, without the sessions anchor", () => {
    const bars = orderedBars([]);
    expect(bars.map((b) => b.key)).toEqual(SIDEBAR_BARS.map((b) => b.key));
    expect(bars.every((b) => b.key !== SESSIONS_KEY)).toBe(true);
  });
  it("mirrors a reordered section list, still dropping sessions", () => {
    const bars = orderedBars([SESSIONS_KEY, "assistant", "usage"]);
    expect(bars.map((b) => b.key)).toEqual([
      "assistant",
      "usage",
      "ingestion",
      "pr-review",
      "issue-handling",
    ]);
  });
});
