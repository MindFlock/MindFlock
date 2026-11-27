/** lib/popout.ts, the pure half: which window this is (`?popout=<title>`),
 * the URL a pop-out loads, its stable window name, and that a pop-out can't
 * pop anything out itself. */

import { afterEach, describe, expect, it } from "vitest";
import { isPopoutWindow, popoutName, popoutSupported, popoutTitle, popoutUrl } from "../lib/popout";

const g = globalThis as Record<string, unknown>;
const had = { location: "location" in g, window: "window" in g };
const prev = { location: g.location, window: g.window };

afterEach(() => {
  for (const k of ["location", "window"] as const) {
    if (had[k]) g[k] = prev[k];
    else delete g[k];
  }
});

describe("popoutTitle", () => {
  it("reads the title off the query, decoded", () => {
    expect(popoutTitle("?popout=fix-login")).toBe("fix-login");
    expect(popoutTitle("?popout=caf%C3%A9%20bar")).toBe("café bar");
    expect(popoutTitle("?x=1&popout=a%2Fb%3Fc%26d")).toBe("a/b?c&d");
    // A form-encoded space (what URLSearchParams writes) is a space.
    expect(popoutTitle("?popout=two+words")).toBe("two words");
  });

  it("is null in the main window: no param, an empty or blank one", () => {
    expect(popoutTitle("")).toBeNull();
    expect(popoutTitle("?view=sky")).toBeNull();
    expect(popoutTitle("?popout=")).toBeNull();
    expect(popoutTitle("?popout=%20%20")).toBeNull();
  });

  it("reads location.search when given nothing", () => {
    g.location = { search: "?popout=from-the-url", origin: "http://h" };
    expect(popoutTitle()).toBe("from-the-url");
    expect(isPopoutWindow()).toBe(true);
    g.location = { search: "", origin: "http://h" };
    expect(isPopoutWindow()).toBe(false);
  });
});

describe("popoutUrl", () => {
  it("is the app's own page on the same origin, the title encoded", () => {
    expect(popoutUrl("fix-login", "http://127.0.0.1:9000")).toBe("http://127.0.0.1:9000/?popout=fix-login");
    expect(popoutUrl("a b/c", "http://h")).toBe("http://h/?popout=a%20b%2Fc");
  });

  it("round-trips through popoutTitle, whatever the title holds", () => {
    for (const t of ["a+b", "a&popout=b", "ünï cödé", "x#y", "100%"]) {
      expect(popoutTitle(new URL(popoutUrl(t, "http://h")).search)).toBe(t);
    }
  });

  it("defaults to this window's origin", () => {
    g.location = { search: "", origin: "https://box.example" };
    expect(popoutUrl("s")).toBe("https://box.example/?popout=s");
  });
});

describe("popoutName", () => {
  it("is stable for a title (window.open reuses the same window)", () => {
    expect(popoutName("fix-login")).toBe(popoutName("fix-login"));
    expect(popoutName("fix-login")).toMatch(/^mf-popout-[0-9a-z]+$/);
  });

  it("differs between titles", () => {
    const names = new Set(["a", "b", "ab", "ba", "fix-login", "fix-logim", ""].map(popoutName));
    expect(names.size).toBe(7);
  });
});

describe("popoutSupported", () => {
  it("a wide browser main window can pop out; the pop-out itself never can", () => {
    g.window = { innerWidth: 1400 };
    g.location = { search: "", origin: "http://h" };
    expect(popoutSupported()).toBe(typeof BroadcastChannel !== "undefined");
    g.location = { search: "?popout=s", origin: "http://h" };
    expect(popoutSupported()).toBe(false);
  });

  it("not at phone width", () => {
    g.window = { innerWidth: 600 };
    g.location = { search: "", origin: "http://h" };
    expect(popoutSupported()).toBe(false);
  });
});

describe("pop-out size", () => {
  it("never opens smaller than MIN_SIZE, and keeps a bigger pane's size", async () => {
    const { openPopout, MIN_SIZE } = await import("../lib/popout");
    const asked: Array<Record<string, unknown>> = [];
    const mfwin = { open: async (o: Record<string, unknown>) => (asked.push(o), true) };
    g.location = { search: "", origin: "http://h" };
    g.window = { mfwin, mfshell: {}, innerWidth: 1440 };
    // A pane off a nine-pane grid: too small for a window of its own.
    expect(await openPopout("small", { x: 10, y: 20, width: 380, height: 300 })).toBe(true);
    expect(asked[0]).toMatchObject({ title: "small", x: 10, y: 20, width: MIN_SIZE.width, height: MIN_SIZE.height });
    // A big pane keeps its size.
    await openPopout("big", { width: 1300, height: 820 });
    expect(asked[1]).toMatchObject({ width: 1300, height: 820 });
  });
});
