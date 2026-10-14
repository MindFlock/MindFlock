/** A repository's uses (PR comments · New issues · Verify) → the ONE settings
 * patch that writes them (components/intake/repoUses.ts). Storage keys are
 * unchanged; the rules each list carries live here once. */

import { describe, expect, it } from "vitest";
import {
  fieldPatch,
  githubSlugOf,
  knownRepos,
  usesOf,
  usesPatch,
} from "../components/intake/repoUses";

const settings = (github: Record<string, unknown> = {}, repository: Record<string, unknown> = {}) => ({
  github,
  repository,
});

describe("usesOf", () => {
  it("reads every list, owner/name case-insensitively", () => {
    const s = settings({ repos: ["Acme/API"], issue_repos: ["acme/api"] }, { verify_repos: ["acme/web"] });
    expect(usesOf(s, "acme/api")).toEqual(["pr", "issue"]);
    expect(usesOf(s, "ACME/WEB")).toEqual(["verify"]);
    expect(usesOf(s, "acme/none")).toEqual([]);
  });
});

describe("usesPatch", () => {
  it("writes several groups in ONE patch (one POST, one pipeline restart)", () => {
    const s = settings({ repos: ["acme/api"], enabled: true }, { verify_repos: [] });
    const p = usesPatch(s, "acme/web", ["pr", "verify"]);
    expect(p).toEqual({
      github: { repos: ["acme/api", "acme/web"] },
      repository: { verify_repos: ["acme/web"] },
    });
  });

  it("saves the first PR repository of a setup that never chose with PR comments OFF", () => {
    expect(usesPatch(settings({}), "acme/api", ["pr"])).toEqual({
      github: { repos: ["acme/api"], enabled: false },
    });
    // A setup that chose keeps its choice; a second repository adds nothing.
    expect(usesPatch(settings({ enabled: true }), "acme/api", ["pr"])).toEqual({
      github: { repos: ["acme/api"] },
    });
    expect(usesPatch(settings({ repos: ["acme/x"] }), "acme/api", ["pr"])).toEqual({
      github: { repos: ["acme/x", "acme/api"] },
    });
  });

  it("never switches issue handling or Verify on — membership only", () => {
    const p = usesPatch(settings({}), "acme/api", ["issue", "verify"]);
    expect(p).toEqual({ github: { issue_repos: ["acme/api"] }, repository: { verify_repos: ["acme/api"] } });
    expect(JSON.stringify(p)).not.toContain("issues_enabled");
    expect(JSON.stringify(p)).not.toContain("verify_enabled");
  });

  it("drops an Intake use's block with it, but KEEPS Verify's (the user's own words)", () => {
    const s = settings(
      { repos: ["acme/api"], repo_settings: { "acme/api": { agent: "codex" }, "acme/x": { agent: "claude" } } },
      { verify_repos: ["acme/api"], verify_repo_settings: { "acme/api": { prompt: "UI on :3000" } } }
    );
    const p = usesPatch(s, "acme/api", []);
    expect(p.github).toEqual({ repos: [], repo_settings: { "acme/x": { agent: "claude" } } });
    expect(p.repository).toEqual({ verify_repos: [] });
    expect(JSON.stringify(p)).not.toContain("verify_repo_settings");
  });

  it("is empty when nothing changes", () => {
    const s = settings({ repos: ["acme/api"] });
    expect(usesPatch(s, "ACME/api", ["pr"])).toEqual({});
  });
});

describe("fieldPatch", () => {
  it("composes from the editor's own copy, and blank inherits", () => {
    let blocks = {};
    const a = fieldPatch(blocks, "pr", "acme/api", "agent", "codex");
    blocks = a.blocks;
    const b = fieldPatch(blocks, "pr", "acme/api", "min_age_minutes", "30");
    expect(b.patch).toEqual({ github: { repo_settings: { "acme/api": { agent: "codex", min_age_minutes: "30" } } } });
    const c = fieldPatch(b.blocks, "pr", "acme/api", "agent", "  ");
    expect(c.blocks).toEqual({ "acme/api": { min_age_minutes: "30" } });
    const d = fieldPatch(c.blocks, "pr", "acme/api", "min_age_minutes", "");
    expect(d.blocks).toEqual({});
  });

  it("stores skip_authors as a list and Verify fields in Verify's map", () => {
    expect(fieldPatch({}, "issue", "a/b", "skip_authors", "dependabot, renovate ").patch).toEqual({
      github: { issue_repo_settings: { "a/b": { skip_authors: ["dependabot", "renovate"] } } },
    });
    expect(fieldPatch({}, "verify", "a/b", "live_branch", "staging").patch).toEqual({
      repository: { verify_repo_settings: { "a/b": { live_branch: "staging" } } },
    });
  });
});

describe("suggestions", () => {
  it("names a ticket source's GitHub repo as owner/name", () => {
    expect(githubSlugOf("git@github.com:acme/api.git")).toBe("acme/api");
    expect(githubSlugOf("https://github.com/acme/web/")).toBe("acme/web");
    expect(githubSlugOf("git@gitlab.com:acme/api.git")).toBe("");
  });

  it("lists every known repository once", () => {
    const s = settings({ repos: ["acme/api"], issue_repos: ["Acme/API", "acme/web"] }, { verify_repos: ["acme/ops"] });
    expect(knownRepos(s, ["acme/web", "acme/tix", ""])).toEqual(["acme/api", "acme/web", "acme/ops", "acme/tix"]);
  });
});
