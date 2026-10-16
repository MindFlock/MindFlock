/** Settings → Workspace → Per-repo branches (lib/repoBranches.ts): pasted
 * remotes normalize to `owner/name`, half-typed rows never reach settings,
 * and the screen renders the editor. */

import { describe, expect, it } from "vitest";
import { mapFrom, normalizeRepo, rowsFrom } from "../lib/repoBranches";

const WORKSPACE = Object.values(
  import.meta.glob<string>("../components/settings/screens/Workspace.tsx", { query: "?raw", import: "default", eager: true })
)[0];

describe("repo branches", () => {
  it("normalizes slugs and GitHub remotes", () => {
    expect(normalizeRepo("acme/beacon")).toBe("acme/beacon");
    expect(normalizeRepo("https://github.com/acme/beacon.git")).toBe("acme/beacon");
    expect(normalizeRepo("git@github.com:acme/beacon.git")).toBe("acme/beacon");
    expect(normalizeRepo("ssh://git@github.com/acme/beacon")).toBe("acme/beacon");
    expect(normalizeRepo("beacon")).toBe("");
    expect(normalizeRepo("")).toBe("");
  });

  it("keeps only rows that name a repo and override something", () => {
    const map = mapFrom([
      { repo: "acme/beacon", base: " main ", pr: "" },
      { repo: "acme/drift", base: "", pr: "release" },
      { repo: "", base: "main", pr: "" },
      { repo: "acme/empty", base: "", pr: "" },
    ]);
    expect(map).toEqual({ "acme/beacon": { base_branch: "main" }, "acme/drift": { pr_base_branch: "release" } });
    expect(rowsFrom(map)).toEqual([
      { repo: "acme/beacon", base: "main", pr: "" },
      { repo: "acme/drift", base: "", pr: "release" },
    ]);
    expect(rowsFrom(undefined)).toEqual([]);
  });

  it("is rendered on the Workspace screen", () => {
    expect(WORKSPACE).toContain("<RepoBranches />");
  });
});
