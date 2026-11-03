/** What's new (docs/vision/whats-new.md): the pure rules in lib/whatsNew.ts,
 * the release data in whatsNew/releases.ts, and the wiring (App mounts it once;
 * the palette, Settings → Help and the top bar's version reopen it). */

import { describe, expect, it } from "vitest";
import {
  compareVersions,
  isReleaseVersion,
  knownAction,
  laterVersion,
  minorOf,
  parseVersion,
  planWhatsNew,
  reopenEntries,
  splitEntries,
  whatsNewTitle,
} from "../lib/whatsNew";
import { RELEASES, type WhatsNewRelease } from "../whatsNew/releases";
import { code, src } from "./sourceText";

const rel = (version: string, big: boolean): WhatsNewRelease => ({
  version,
  date: "2026-01-01",
  big,
  headline: "h " + version,
  features: [{ title: "t " + version, body: "b" }],
});

// Newest first, as the file is written.
const LIST = [rel("0.12.1", false), rel("0.12.0", true), rel("0.11.1", false), rel("0.11.0", true), rel("0.10.6", false)];
const versions = (rs: readonly WhatsNewRelease[]) => rs.map((r) => r.version);

describe("version compare", () => {
  it("is numeric, part by part", () => {
    expect(compareVersions("0.10.0", "0.9.9")).toBeGreaterThan(0);
    expect(compareVersions("0.11.0", "0.11.0")).toBe(0);
    expect(compareVersions("1.0.0", "0.99.99")).toBeGreaterThan(0);
    expect(compareVersions("0.10.6", "0.10.10")).toBeLessThan(0);
  });

  it("reads a leading v and ignores a local/dev tail", () => {
    expect(parseVersion("v0.11.0")).toEqual([0, 11, 0]);
    expect(parseVersion("0.11.0.dev3+g1234")).toEqual([0, 11, 0]);
    expect(parseVersion("nope")).toBeNull();
  });

  it("knows a source tree is not a release", () => {
    expect(isReleaseVersion("0.10.6")).toBe(true);
    expect(isReleaseVersion("0+unknown")).toBe(false);
    expect(isReleaseVersion("")).toBe(false);
    expect(isReleaseVersion(null)).toBe(false);
  });

  it("names the minor release", () => {
    expect(minorOf("0.11.0")).toBe("0.11");
    expect(minorOf("1.2.3")).toBe("1.2");
  });
});

describe("planWhatsNew: when it pops and what it lists", () => {
  it("pops after an update to a release with an unseen big entry, listing every unseen entry newest first", () => {
    const p = planWhatsNew({ releases: LIST, running: "0.12.1", lastSeen: "0.11.0", onboarded: true });
    expect(p.pop).toBe(true);
    expect(versions(p.entries)).toEqual(["0.12.1", "0.12.0", "0.11.1"]);
    expect(p.markSeen).toBeNull();
  });

  it("does not pop when only small entries are unseen", () => {
    const p = planWhatsNew({ releases: LIST, running: "0.11.1", lastSeen: "0.11.0", onboarded: true });
    expect(p.pop).toBe(false);
    expect(p.entries).toEqual([]);
  });

  it("does not pop once the running version is seen", () => {
    expect(planWhatsNew({ releases: LIST, running: "0.12.0", lastSeen: "0.12.0", onboarded: true }).pop).toBe(false);
  });

  it("never shows an entry above the running version (written ahead, a downgrade, a dev build behind)", () => {
    const ahead = planWhatsNew({ releases: LIST, running: "0.10.6", lastSeen: "0.10.5", onboarded: true });
    expect(ahead.pop).toBe(false);
    const down = planWhatsNew({ releases: LIST, running: "0.11.0", lastSeen: "0.12.1", onboarded: true });
    expect(down.pop).toBe(false);
    const mid = planWhatsNew({ releases: LIST, running: "0.11.1", lastSeen: "0.10.6", onboarded: true });
    expect(versions(mid.entries)).toEqual(["0.11.1", "0.11.0"]);
  });

  it("a fresh install never pops: it marks the running version seen", () => {
    const p = planWhatsNew({ releases: LIST, running: "0.12.0", lastSeen: null, onboarded: false });
    expect(p.pop).toBe(false);
    expect(p.markSeen).toBe("0.12.0");
  });

  it("someone who used MindFlock before What's new existed (nothing seen, onboarded) gets it", () => {
    const p = planWhatsNew({ releases: LIST, running: "0.11.0", lastSeen: undefined, onboarded: true });
    expect(p.pop).toBe(true);
    expect(versions(p.entries)).toEqual(["0.11.0", "0.10.6"]);
  });

  it("an unknown running version pops nothing and records nothing", () => {
    for (const running of ["", "0+unknown", null]) {
      expect(planWhatsNew({ releases: LIST, running, lastSeen: null, onboarded: false })).toEqual({
        pop: false,
        entries: [],
        markSeen: null,
      });
    }
  });

  it("orders by version even if the file were out of order", () => {
    const shuffled = [LIST[3], LIST[0], LIST[4], LIST[1], LIST[2]];
    const p = planWhatsNew({ releases: shuffled, running: "0.12.1", lastSeen: "0.10.6", onboarded: true });
    expect(versions(p.entries)).toEqual(["0.12.1", "0.12.0", "0.11.1", "0.11.0"]);
  });
});

describe("reopening and closing", () => {
  it("by hand shows the latest release down to its big entry", () => {
    expect(versions(reopenEntries(LIST, "0.12.1"))).toEqual(["0.12.1", "0.12.0"]);
    expect(versions(reopenEntries(LIST, "0.11.0"))).toEqual(["0.11.0"]);
    // Below every big entry: the newest one alone.
    expect(versions(reopenEntries(LIST, "0.10.6"))).toEqual(["0.10.6"]);
    // A dev build (no release version) reads everything as at or below it.
    expect(versions(reopenEntries(LIST, "0+unknown"))).toEqual(["0.12.1", "0.12.0"]);
  });

  it("closing records the running version, never lower than the seen one", () => {
    expect(laterVersion("0.12.0", "0.11.0")).toBe("0.12.0");
    expect(laterVersion("0.11.0", "0.12.0")).toBe("0.12.0");
    expect(laterVersion("0.11.0", null)).toBe("0.11.0");
    expect(laterVersion("0+unknown", "0.11.0")).toBe("0.11.0");
    expect(laterVersion("", null)).toBeNull();
  });

  it("titles the window with the big entry's minor release and folds the small ones", () => {
    const shown = [LIST[0], LIST[1], LIST[2]];
    expect(whatsNewTitle(shown)).toBe("What's new in MindFlock 0.12");
    const { big, small } = splitEntries(shown);
    expect(versions(big)).toEqual(["0.12.0"]);
    expect(versions(small)).toEqual(["0.12.1", "0.11.1"]);
  });

  it("drops a button whose action id is unknown", () => {
    const known = (id: string) => id === "sky";
    expect(knownAction({ action: { label: "Show me", run: "sky" } }, known)).toEqual({ label: "Show me", run: "sky" });
    expect(knownAction({ action: { label: "Show me", run: "nope" } }, known)).toBeUndefined();
    expect(knownAction({}, known)).toBeUndefined();
  });
});

describe("the release entries", () => {
  it("are newest first, with valid versions and dates", () => {
    for (let i = 1; i < RELEASES.length; i++) {
      expect(compareVersions(RELEASES[i - 1].version, RELEASES[i].version)).toBeGreaterThan(0);
    }
    for (const r of RELEASES) {
      expect(r.version).toMatch(/^\d+\.\d+\.\d+$/);
      expect(r.date).toMatch(/^\d{4}-\d{2}-\d{2}$/);
      expect(r.features.length).toBeGreaterThan(0);
    }
  });

  it("0.11.0 is big, with 4-6 features, and 0.10.6 is a small one", () => {
    const r11 = RELEASES.find((r) => r.version === "0.11.0")!;
    expect(r11.big).toBe(true);
    expect(r11.features.length).toBeGreaterThanOrEqual(4);
    expect(r11.features.length).toBeLessThanOrEqual(6);
    expect(RELEASES.find((r) => r.version === "0.10.6")!.big).toBe(false);
  });

  it("every action id is one the window knows", () => {
    const table = code("components/whatsNew/actions.ts");
    for (const r of RELEASES)
      for (const f of r.features)
        if (f.action) expect(table, f.action.run).toMatch(new RegExp(`(^|[\\s{,])"?${f.action.run}"?: \\(\\)`, "m"));
  });

  it("use the user-facing words (docs/vocabulary.md), no internal names", () => {
    const text = JSON.stringify(RELEASES).toLowerCase();
    for (const banned of ["chain", "flyway", "perch", "fast-track", "checklist", "test plan", "changelog"]) {
      expect(text, banned).not.toContain(banned);
    }
  });
});

describe("wiring", () => {
  it("App mounts the window once", () => {
    expect(code("App.tsx").match(/<WhatsNew \/>/g)).toHaveLength(1);
  });

  it("the palette, Settings → Help and the top bar's version reopen it", () => {
    expect(code("components/palette/CommandPalette.tsx")).toContain(`label: "What's new"`);
    expect(code("components/settings/screens/General.tsx")).toContain("onClick={openWhatsNew}");
    expect(code("components/TopBar.tsx")).toMatch(/className="tb-version"[^>]*onClick=\{openWhatsNew\}/);
  });

  it("closing by any way records seen; the seen version rides the synced prefs with a localStorage fallback", () => {
    const view = code("components/whatsNew/WhatsNew.tsx");
    expect(view).toContain("onClose={closeWhatsNew}");
    expect(view).toContain("Got it");
    const seen = src("components/whatsNew/seen.ts");
    expect(seen).toContain('"/api/prefs"');
    expect(seen).toContain("localStorage.setItem");
  });
});
