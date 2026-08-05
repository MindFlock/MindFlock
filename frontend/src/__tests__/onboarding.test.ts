/// <reference types="vite/client" />
import { describe, it, expect } from "vitest";
import { tourDecision } from "../state/store";
import { shouldAutoShowSetup } from "../components/dialogs/SetupDialog";

describe("tourDecision (welcome tour auto-open)", () => {
  it("opens for a user the server has never seen get anywhere", () => {
    expect(tourDecision({ tourDone: false, hintsEnabled: true, onboarded: false })).toBe("open");
  });

  it("leaves a returning user alone even when this browser has forgotten them", () => {
    // The whole bug: cleared storage / a second profile / a second machine.
    expect(tourDecision({ tourDone: false, hintsEnabled: true, onboarded: true })).toBe("skip");
  });

  it("waits rather than guessing while /api/config is still in flight", () => {
    expect(tourDecision({ tourDone: false, hintsEnabled: true, onboarded: undefined })).toBe("wait");
  });

  it("short-circuits locally, so a finished tour never waits on the network", () => {
    expect(tourDecision({ tourDone: true, hintsEnabled: true, onboarded: undefined })).toBe("skip");
    expect(tourDecision({ tourDone: false, hintsEnabled: false, onboarded: undefined })).toBe("skip");
  });

  it("honors hints-off for a brand new user too", () => {
    expect(tourDecision({ tourDone: false, hintsEnabled: false, onboarded: false })).toBe("skip");
  });
});

describe("shouldAutoShowSetup (first-run checklist auto-open)", () => {
  it("opens for a first-run user with something failing", () => {
    expect(shouldAutoShowSetup({ failing: true, onboarded: false })).toBe(true);
  });

  it("never ambushes a veteran with a first-run checklist", () => {
    expect(shouldAutoShowSetup({ failing: true, onboarded: true })).toBe(false);
  });

  it("stays shut while the onboarded flag is unknown", () => {
    expect(shouldAutoShowSetup({ failing: true, onboarded: undefined })).toBe(false);
  });

  it("keeps opening for the same first-run user until the tools are there", () => {
    // No per-browser "already saw it" flag can suppress this: a missing tmux is
    // still missing on the next load, and the checklist is the only thing that
    // says so before the user tries to create a session.
    expect(shouldAutoShowSetup({ failing: true, onboarded: false })).toBe(true);
    expect(shouldAutoShowSetup({ failing: true, onboarded: false })).toBe(true);
  });

  it("stays shut when doctor is happy, whoever is asking", () => {
    expect(shouldAutoShowSetup({ failing: false, onboarded: false })).toBe(false);
    expect(shouldAutoShowSetup({ failing: false, onboarded: undefined })).toBe(false);
  });
});

/** Source with its comments removed.
 *
 * The modules below deliberately name the retired endpoint and flags in prose so
 * nobody reinvents them, so a raw substring search would flag the very
 * documentation that keeps them retired. The `[^:"'`]` guard is what stops a
 * `https://` inside a string from being read as the start of a comment. */
function code(text: string): string {
  return text.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:"'`\\])\/\/.*$/gm, "$1");
}

/** Every .ts/.tsx module the app itself ships, as source keyed by path. The glob
 * is Vite's rather than a directory walk because this project installs no node
 * types, and @types/node is not worth adding for one readdir. */
const sources = Object.entries(
  import.meta.glob<string>(["../**/*.{ts,tsx}", "!../__tests__/**"], {
    query: "?raw",
    import: "default",
    eager: true,
  })
).map(([path, text]) => ({ path: path.replace(/^\.\.\//, ""), text: code(text) }));

describe("first-run state has exactly one authority", () => {
  // These read source rather than behaviour because both regressions were side
  // effects inside components, and this suite runs without a DOM — the tour's own
  // close handler cannot be rendered here. What is checkable is that no module
  // can reach the offending write at all.

  it("finds sources to scan at all", () => {
    // A glob that silently matched nothing would make both checks below pass
    // without looking at anything.
    expect(sources.length).toBeGreaterThan(20);
    expect(sources.map((f) => f.path)).toContain("App.tsx");
  });

  it("never tells the server the welcome tour happened", () => {
    // general.onboarded means "this user has a session". Writing it because a
    // slideshow closed took away the grid's setup card and the auto-opening
    // dependency checklist from a user who had installed nothing and created
    // nothing — the two surfaces that were the way out.
    const offenders = sources.filter((f) => f.text.includes("/api/onboarded"));
    expect(offenders.map((f) => f.path)).toEqual([]);
  });

  it("keeps the writer-less per-browser setup flags out", () => {
    // mf_setup_done and mf_ever_created were read but never written, so the
    // dismissal they gated could not fire and the comments describing it were
    // fiction. Either key coming back needs a writer and a reason.
    const offenders = sources.filter(
      (f) => f.text.includes("mf_setup_done") || f.text.includes("mf_ever_created")
    );
    expect(offenders.map((f) => f.path)).toEqual([]);
  });
});
