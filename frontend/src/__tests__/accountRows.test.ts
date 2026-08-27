import { describe, it, expect } from "vitest";
import type { AuthProfile } from "../api/types";
import { accountRows, swapLabel } from "../lib/accountRows";

const P = (id: string, label?: string): AuthProfile => ({ id, label, kind: "account" });
const PROFILES = [P("work", "Work"), P("personal", "Personal")];

describe("accountRows", () => {
  it("checkmarks the app-default row for a session that pins nothing", () => {
    const rows = accountRows("", "work", PROFILES);
    expect(rows[0]).toEqual({ id: "", label: "App default (Work)", current: true });
    // ...and NOT the profile the default resolves to — clicking that row is
    // what would silently convert an inheriting session into a pinned one.
    expect(rows.find((r) => r.id === "work")?.current).toBe(false);
  });

  it("offers no inherit row when there is no app default", () => {
    const rows = accountRows("", "", PROFILES);
    expect(rows.map((r) => r.id)).toEqual(["default", "work", "personal"]);
    // "" and "default" mean the same thing here, so the ambient row is current.
    expect(rows[0].current).toBe(true);
  });

  it("checkmarks the ambient row for an explicit 'default' pin", () => {
    const rows = accountRows("default", "work", PROFILES);
    expect(rows.find((r) => r.id === "default")?.current).toBe(true);
    expect(rows.find((r) => r.id === "")?.current).toBe(false);
  });

  it("checkmarks a pinned profile", () => {
    const rows = accountRows("personal", "work", PROFILES);
    expect(rows.filter((r) => r.current).map((r) => r.id)).toEqual(["personal"]);
  });

  it("never marks two rows current", () => {
    for (const pin of ["", "default", "work", "personal", "ghost"]) {
      for (const def of ["", "work", "ghost"]) {
        const n = accountRows(pin, def, PROFILES).filter((r) => r.current).length;
        expect(n, `pin=${pin} default=${def}`).toBeLessThanOrEqual(1);
      }
    }
  });

  it("falls back to the id when a profile has no label", () => {
    expect(accountRows("", "bare", [P("bare")])[0].label).toBe("App default (bare)");
  });

  it("ignores a dangling app default", () => {
    // The default names a profile that has since been removed: there is
    // nothing to inherit, so the menu must not offer a row for it.
    const rows = accountRows("", "gone", PROFILES);
    expect(rows.map((r) => r.id)).toEqual(["default", "work", "personal"]);
  });
});

describe("swapLabel", () => {
  it("names each tri-state in words", () => {
    expect(swapLabel("default", PROFILES)).toBe("the CLI's own login");
    expect(swapLabel("", PROFILES)).toBe("the app default account");
    expect(swapLabel("work", PROFILES)).toBe("Work");
    expect(swapLabel("ghost", PROFILES)).toBe("ghost");
  });
});
