import { describe, it, expect } from "vitest";
import {
  SIDEBAR_BARS,
  DEFAULT_VISIBLE_BARS,
  DEFAULT_SECTION_ORDER,
  EXT_BAR_PREFIX,
  SESSIONS_KEY,
  defaultHiddenBars,
  orderedSections,
  orderedBars,
} from "../components/sidebar/barDefs";

describe("defaultHiddenBars", () => {
  it("hides every bar that is not one of the essentials", () => {
    const hidden = defaultHiddenBars();
    expect(hidden).toEqual(["pr-review", "issue-handling", "verify"]);
    // The headline feature's bar is visible out of the box.
    expect(DEFAULT_VISIBLE_BARS).toContain("ingestion");
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
      "verify",
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
      "verify",
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

/** Extension bars ("ext:" + id) are extra keys the resolvers accept alongside
 * the built-ins. The rule that matters: one the saved order has never seen is
 * inserted immediately BEFORE the sessions anchor, never tail-appended like a
 * missing built-in — a user with a years-old saved order must find a freshly
 * installed extension's bar just above the session list, not below it. */
describe("orderedSections with extension bars", () => {
  const DB = EXT_BAR_PREFIX + "dbclient";
  const OTHER = EXT_BAR_PREFIX + "other";

  it("leaves the built-in resolution untouched when no extras are given", () => {
    expect(orderedSections([])).toEqual(orderedSections([], []));
  });
  it("inserts a never-seen extension bar right before the sessions anchor", () => {
    const out = orderedSections([], [DB]);
    expect(out).toEqual([...SIDEBAR_BARS.map((b) => b.key), DB, SESSIONS_KEY]);
  });
  it("inserts before the anchor even when the saved order moved sessions to the top", () => {
    const out = orderedSections([SESSIONS_KEY, "usage"], [DB]);
    // Missing built-ins still tail-append (existing behaviour); the extra lands
    // just above the anchor, which here is the first slot.
    expect(out).toEqual([
      DB,
      "sessions",
      "usage",
      "ingestion",
      "pr-review",
      "issue-handling",
      "verify",
      "assistant",
    ]);
  });
  it("keeps the saved place of an extension bar the order already mentions", () => {
    const out = orderedSections([DB, "usage", SESSIONS_KEY], [DB]);
    expect(out.slice(0, 3)).toEqual([DB, "usage", SESSIONS_KEY]);
    // It is not inserted a second time.
    expect(out.filter((k) => k === DB)).toHaveLength(1);
  });
  it("inserts several missing extras together, in the given order", () => {
    const out = orderedSections(["usage", SESSIONS_KEY, "assistant"], [DB, OTHER]);
    expect(out.indexOf(DB)).toBe(out.indexOf(SESSIONS_KEY) - 2);
    expect(out.indexOf(OTHER)).toBe(out.indexOf(SESSIONS_KEY) - 1);
  });
  it("drops an extension bar the saved order mentions once its extension is gone", () => {
    // Uninstalled (or disabled) extension: its key is no longer an extra, so it
    // is unknown and dropped like any other stale key.
    expect(orderedSections([DB, "usage"])).not.toContain(DB);
    expect(orderedSections([DB, "usage"], [OTHER])).not.toContain(DB);
  });
  it("dedupes a repeated extra key", () => {
    const out = orderedSections([], [DB, DB]);
    expect(out.filter((k) => k === DB)).toHaveLength(1);
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
      "verify",
    ]);
  });
  it("carries extension bar defs (key + label) through, in resolved order", () => {
    const db = { key: EXT_BAR_PREFIX + "dbclient", label: "Database" };
    const bars = orderedBars([], [db]);
    // Fresh order: built-ins, then the extra (which sat just above sessions).
    expect(bars[bars.length - 1]).toEqual(db);
    expect(bars.every((b) => b.key !== SESSIONS_KEY)).toBe(true);
    // A saved order that already places the extension bar is honoured.
    const saved = orderedBars([db.key, "usage", SESSIONS_KEY], [db]);
    expect(saved.slice(0, 2)).toEqual([db, { key: "usage", label: "Usage" }]);
  });
  it("drops a saved extension key that has no def any more", () => {
    const bars = orderedBars([EXT_BAR_PREFIX + "gone", "usage"], []);
    expect(bars.map((b) => b.key)).toEqual(SIDEBAR_BARS.map((b) => b.key));
  });
});
