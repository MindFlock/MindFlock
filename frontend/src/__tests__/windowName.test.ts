/** What a notification calls a session — see lib/windowName.ts.
 *
 * The bug: a desktop notification named its window by the raw title off the
 * event envelope ("shortcut-21431") while the rail showed a rename, or the
 * pipeline label built from the branch. A push about a session nobody can find
 * under that name is the one mistake this channel cannot make.
 */
import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { nameTakenBy, slotNumber, windowName, publishWindowName } from "../lib/windowName";
import { useUi } from "../state/store";

type Row = { title: string; branch?: string; display_title?: string; device?: string };

function withSessions(rows: Row[]) {
  (globalThis as unknown as { mindflock?: Record<string, unknown> }).mindflock = {
    sessions: () => rows,
  };
}

beforeEach(() => {
  useUi.setState({ aliases: {}, railOrder: [] });
  withSessions([]);
});

afterEach(() => {
  delete (globalThis as unknown as { mindflock?: unknown }).mindflock;
});

describe("windowName", () => {
  it("uses the rename before anything else", () => {
    useUi.setState({ aliases: { "shortcut-21431": "social scan noise" } });
    withSessions([
      { title: "shortcut-21431", branch: "feature/shortcut-21431/social-scan-noise" },
    ]);
    expect(windowName("shortcut-21431")).toBe("social scan noise");
  });

  it("falls back to the label the sidebar shows, not the raw slug", () => {
    withSessions([
      { title: "shortcut-21431", branch: "feature/shortcut-21431/social-scan-noise" },
    ]);
    expect(windowName("shortcut-21431")).toBe(
      "(tix) social-scan-noise/shortcut-21431"
    );
  });

  it("leaves a hand-made session's name alone — that IS its name", () => {
    withSessions([{ title: "my-refactor", branch: "my-refactor" }]);
    expect(windowName("my-refactor")).toBe("my-refactor");
  });

  it("names a proxied session by its own title, matched either way", () => {
    // A remote row is keyed "<device>::<title>" locally, but its device's
    // events arrive under the bare title. Falling through to raw here would
    // undo the whole point for every remote session.
    withSessions([
      {
        title: "laptop::sc-12345",
        display_title: "sc-12345",
        branch: "feature/sc-12345/add-dark-mode",
      },
    ]);
    expect(windowName("sc-12345")).toBe("(tix) add-dark-mode/sc-12345");
  });

  it("answers the raw title for a session the poll has not seen", () => {
    expect(windowName("sc-12345")).toBe("sc-12345");
  });

  it("never throws when the snapshot accessor does", () => {
    (globalThis as unknown as { mindflock?: Record<string, unknown> }).mindflock = {
      sessions: () => {
        throw new Error("no bus");
      },
    };
    expect(windowName("sc-12345")).toBe("sc-12345");
  });

  it("returns nothing for nothing", () => {
    expect(windowName("")).toBe("");
  });
});

describe("publishWindowName", () => {
  it("puts the resolver on the extension API for notify.js to feature-detect", () => {
    withSessions([{ title: "my-refactor", branch: "my-refactor" }]);
    publishWindowName();
    const mf = (globalThis as unknown as { mindflock: { displayName?: unknown } })
      .mindflock;
    expect(typeof mf.displayName).toBe("function");
    expect((mf.displayName as (t: string) => string)("my-refactor")).toBe(
      "my-refactor"
    );
  });
});


describe("slotNumber", () => {
  it("reads the rail's published order — a window row shifts session numbers", () => {
    // Once the sidebar has published railOrder (sessions AND window rows,
    // grouping and filter applied), the number comes from THAT, verbatim.
    useUi.setState({ railOrder: ["\u0000assistant-chat", "a", "b"] });
    withSessions([{ title: "a" }, { title: "b" }]);
    expect(slotNumber("a")).toBe("2");
    expect(slotNumber("b")).toBe("3");
  });

  it("resolves a display_title through the snapshot against the rail", () => {
    useUi.setState({ railOrder: ["raw-slug"] });
    withSessions([{ title: "raw-slug", display_title: "Nice Name" }]);
    expect(slotNumber("Nice Name")).toBe("1");
  });

  it("numbers sessions the way the rail does: saved order first, 1-based", () => {
    useUi.setState({ order: ["b", "a"], filter: "" });
    withSessions([{ title: "a" }, { title: "b" }, { title: "c" }]);
    expect(slotNumber("b")).toBe("1");
    expect(slotNumber("a")).toBe("2");
    expect(slotNumber("c")).toBe("3"); // unlisted-in-order rows file after
  });

  it("excludes verify sessions, exactly as the rail does", () => {
    useUi.setState({ order: [], filter: "" });
    withSessions([{ title: "verify-x-abc" }, { title: "real" }]);
    expect(slotNumber("real")).toBe("1");
    expect(slotNumber("verify-x-abc")).toBe("");
  });

  it("renumbers under the live filter — the number shown IS the number sent", () => {
    useUi.setState({ order: [], filter: "bot", aliases: {} });
    withSessions([{ title: "alpha" }, { title: "bot-1" }, { title: "bot-2" }]);
    expect(slotNumber("bot-1")).toBe("1");
    expect(slotNumber("bot-2")).toBe("2");
    expect(slotNumber("alpha")).toBe("");
  });

  it("goes blank from the tenth row on, mirroring the rail's hotkey slots", () => {
    useUi.setState({ order: [], filter: "" });
    withSessions(Array.from({ length: 11 }, (_, i) => ({ title: "s" + i })));
    expect(slotNumber("s8")).toBe("9");
    expect(slotNumber("s9")).toBe("");
  });

  it("answers blank, never throws, for the unknown and the unlisted", () => {
    useUi.setState({ order: [], filter: "" });
    withSessions([{ title: "a" }]);
    expect(slotNumber("nope")).toBe("");
    expect(slotNumber("")).toBe("");
    delete (globalThis as unknown as { mindflock?: unknown }).mindflock;
    expect(slotNumber("a")).toBe("");
  });

  it("is published on the bridge next to displayName", () => {
    publishWindowName();
    const w = globalThis as unknown as { mindflock?: Record<string, unknown> };
    expect(typeof w.mindflock?.slotNumber).toBe("function");
  });
});

describe("nameTakenBy — a rename is the name everywhere, so names are unique", () => {
  it("finds another session by its rename, its title or its rail label", () => {
    useUi.setState({ aliases: { alpha: "Login fix" } });
    withSessions([
      { title: "alpha" },
      { title: "sc-12345", branch: "feature/sc-12345/dark-mode" },
      { title: "beta" },
    ]);
    expect(nameTakenBy("beta", "Login fix")).toBe("alpha");
    expect(nameTakenBy("beta", "alpha")).toBe("alpha");
    expect(nameTakenBy("beta", "(tix) dark-mode/sc-12345")).toBe("sc-12345");
  });

  it("never collides a session with itself, and matches exactly", () => {
    useUi.setState({ aliases: { alpha: "Login fix" } });
    withSessions([{ title: "alpha" }, { title: "beta" }]);
    expect(nameTakenBy("alpha", "Login fix")).toBe("");
    expect(nameTakenBy("beta", "login fix")).toBe("");
    expect(nameTakenBy("beta", "  ")).toBe("");
  });

  it("ignores another device's rows (the server checks this device only)", () => {
    withSessions([{ title: "rig::alpha", display_title: "alpha", device: "rig" } as Row]);
    expect(nameTakenBy("beta", "rig::alpha")).toBe("");
  });
});

describe("pickRow — selecting rail rows in place", () => {
  beforeEach(() => useUi.getState().setBulkMode(false));

  it("Ctrl-click toggles one row and turns select mode on", () => {
    useUi.setState({ railOrder: ["a", "b", "c"] });
    useUi.getState().pickRow("b", false);
    expect(useUi.getState().bulkMode).toBe(true);
    expect([...useUi.getState().bulkSelected]).toEqual(["b"]);
    useUi.getState().pickRow("b", false);
    expect(useUi.getState().bulkSelected.size).toBe(0);
  });

  it("Shift-click adds the range from the last pick, skipping window rows", () => {
    useUi.setState({ railOrder: ["a", "\u0000verify:x", "b", "c", "d"] });
    useUi.getState().pickRow("a", false);
    useUi.getState().pickRow("c", true);
    expect([...useUi.getState().bulkSelected].sort()).toEqual(["a", "b", "c"]);
  });

  it("Shift-click with nothing picked yet just picks that row", () => {
    useUi.setState({ railOrder: ["a", "b", "c"] });
    useUi.getState().pickRow("c", true);
    expect([...useUi.getState().bulkSelected]).toEqual(["c"]);
  });

  it("leaving select mode drops the picks and the anchor", () => {
    useUi.setState({ railOrder: ["a", "b"] });
    useUi.getState().pickRow("a", false);
    useUi.getState().setBulkMode(false);
    expect(useUi.getState().bulkSelected.size).toBe(0);
    expect(useUi.getState().bulkAnchor).toBe(null);
  });
});
