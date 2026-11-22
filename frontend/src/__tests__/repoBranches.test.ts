/** Settings → Workspace → Per-repo branches (lib/repoBranches.ts): pasted
 * remotes normalize to `owner/name`, half-typed rows never reach settings,
 * and the screen renders the editor. */

import { describe, expect, it } from "vitest";
import { mapFrom, normalizeRepo, rowsFrom, usedLine, usedNote } from "../lib/repoBranches";

const WORKSPACE = Object.values(
  import.meta.glob<string>("../components/settings/screens/Workspace.tsx", { query: "?raw", import: "default", eager: true })
)[0];

const REPO_BRANCHES = Object.values(
  import.meta.glob<string>("../components/settings/screens/RepoBranches.tsx", { query: "?raw", import: "default", eager: true })
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

describe("what each known repo will use", () => {
  const side = (branch: string, why: string, note = "") => ({ branch, source: "", why, note, error: "" });

  it("says the branch and why, and the PR target only when it differs", () => {
    const same = { repo: "acme/app", base: side("main", "main · the repository's default"), pr: side("main", "main · the repository's default") };
    expect(usedLine(same)).toBe("main · the repository's default");
    const split = { repo: "acme/site", base: side("staging", "staging · your setting"), pr: side("release", "release · your setting for this repository") };
    expect(usedLine(split)).toBe("staging · your setting · PRs: release · your setting for this repository");
    expect(usedLine({ repo: "x/y", base: side("", ""), pr: side("", "") })).toBe("no base branch found — set one below");
  });

  it("says a skipped default once", () => {
    const n = "Using main: this repository has no staging branch";
    const row = { repo: "acme/app", base: side("main", "", n), pr: side("main", "", n) };
    expect(usedNote(row)).toBe(n);
    expect(usedNote({ repo: "a/b", base: side("main", ""), pr: side("main", "") })).toBe("");
  });

  it("is listed on the same screen as the override rows", () => {
    expect(REPO_BRANCHES).toContain('api<{ repos?: RepoBaseRow[] }>("/api/repos/bases")');
    expect(REPO_BRANCHES).toContain("usedLine(u)");
  });
});
