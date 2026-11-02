/** The new-session block's pure half (lib/newBlock, docs/flights.md "As
 * built: round 12 new block"): More options' defaults and memory, the closed
 * fold's summary line, the base-branch chips, and From a session — which
 * sessions it offers, the filter, the worker title, the fork's options and
 * lineage keys. */

import { describe, expect, it } from "vitest";
import {
  MORE_KEY,
  branchChoices,
  defaultOptions,
  filterParents,
  forkFields,
  forkOptions,
  modelOf,
  optionsSummary,
  parentChoices,
  readMoreOpen,
  workerTitle,
  workspaceWords,
  writeMoreOpen,
} from "../lib/newBlock";

const kv = (init: Record<string, string> = {}) => {
  const m = new Map(Object.entries(init));
  return { getItem: (k: string) => m.get(k) ?? null, setItem: (k: string, v: string) => void m.set(k, v), m };
};

describe("More options", () => {
  it("starts as the New dialog's defaults: a worktree, the server's base, this device, no account or template", () => {
    expect(defaultOptions()).toEqual({
      profileId: "",
      profileModel: "",
      inPlace: false,
      baseBranch: "",
      baseRemote: false,
      template: "",
      provision: false,
      strategy: "worktree",
      initRepo: false,
      device: "",
    });
  });

  it("remembers open / folded in mf_sky_new_more, folded by default, and survives a storage that throws", () => {
    expect(MORE_KEY).toBe("mf_sky_new_more");
    const s = kv();
    expect(readMoreOpen(s)).toBe(false);
    writeMoreOpen(true, s);
    expect(s.m.get("mf_sky_new_more")).toBe("1");
    expect(readMoreOpen(s)).toBe(true);
    writeMoreOpen(false, s);
    expect(readMoreOpen(s)).toBe(false);
    const broken = {
      getItem: () => {
        throw new Error("denied");
      },
      setItem: () => {
        throw new Error("denied");
      },
    };
    expect(readMoreOpen(broken)).toBe(false);
    expect(() => writeMoreOpen(true, broken)).not.toThrow();
    expect(readMoreOpen(null)).toBe(false);
  });

  it("the model: a key account's pick, else a --model in the launch flags", () => {
    expect(modelOf("anthropic/claude-sonnet-4.5", "--model opus")).toBe("anthropic/claude-sonnet-4.5");
    expect(modelOf("", "--verbose --model opus")).toBe("opus");
    expect(modelOf("", "--model=sonnet")).toBe("sonnet");
    expect(modelOf("", "-m o3")).toBe("o3");
    expect(modelOf("", "--model --verbose")).toBe("");
    expect(modelOf("", "")).toBe("");
  });

  it("the workspace in words", () => {
    expect(workspaceWords({ inPlace: false, baseBranch: "", provision: false })).toBe("worktree");
    expect(workspaceWords({ inPlace: false, baseBranch: "main", provision: false })).toBe("worktree from main");
    expect(workspaceWords({ inPlace: true, baseBranch: "main", provision: false })).toBe("in place");
    expect(workspaceWords({ inPlace: false, baseBranch: "", provision: true })).toBe("provisioned");
  });

  it("the closed fold's line: agent · model · workspace, then template and device", () => {
    const o = defaultOptions();
    expect(optionsSummary({ program: "claude", options: { ...o, baseBranch: "main" }, launchArgs: "--model opus" })).toBe("claude · opus · worktree from main");
    expect(optionsSummary({ program: "codex", options: o, launchArgs: "" })).toBe("codex · worktree");
    expect(
      optionsSummary({ program: "claude", options: { ...o, inPlace: true, template: "Nightly", device: "mac-mini.ts.net" }, launchArgs: "", deviceName: "mac-mini" })
    ).toBe("claude · in place · “Nightly” · on mac-mini");
  });

  it("base branch chips: the default, then the checked-out one, local before remote, one per name, at most six", () => {
    expect(branchChoices(null)).toEqual([]);
    const b = {
      default: "main",
      current: "feature",
      branches: [
        { name: "origin/x", remote: true },
        { name: "zeta", remote: false },
        { name: "feature", remote: false },
        { name: "main", remote: false },
        { name: "main", remote: false },
      ],
    };
    expect(branchChoices(b).map((c) => c.value)).toEqual(["main", "feature", "zeta", "origin/x"]);
    expect(branchChoices(b).at(-1)).toEqual({ value: "origin/x", label: "origin/x", remote: true });
    expect(branchChoices(b, 2).map((c) => c.value)).toEqual(["main", "feature"]);
  });
});

describe("From a session", () => {
  const row = (title: string, extra: Record<string, unknown> = {}) => ({ title, path: "/code/app", branch: "mf/" + title, provider: "claude", ...extra });

  it("offers this device's live sessions by name — not another device's, a pending one, a hidden one or a locked one", () => {
    const rows = [
      row("zed"),
      row("alpha", { branch: "" }),
      row("mac::far", { device: "mac" }),
      row("making", { pending_create: true }),
      row("verify-1", { hidden_by: "verify" }),
      row("guest", { locked: true }),
      row("nopath", { path: "" }),
    ];
    const names: Record<string, string> = { zed: "Zed work" };
    const got = parentChoices(rows, (t) => names[t] || "");
    expect(got.map((c) => c.title)).toEqual(["alpha", "zed"]);
    expect(got[1]).toEqual({ title: "zed", name: "Zed work", repo: "/code/app", branch: "mf/zed", provider: "claude" });
    expect(got[0].branch).toBe("");
  });

  it("filters on every word of the name, title or repository", () => {
    const c = parentChoices([row("api-fix", { path: "/code/api" }), row("web-ui", { path: "/code/web" })]);
    expect(filterParents(c, "").length).toBe(2);
    expect(filterParents(c, "API").map((x) => x.title)).toEqual(["api-fix"]);
    expect(filterParents(c, "code web").map((x) => x.title)).toEqual(["web-ui"]);
    expect(filterParents(c, "nothing")).toEqual([]);
  });

  it("names the worker as spawn_session does: the lowest free <parent>-wN, slugged, at most 40 characters", () => {
    expect(workerTitle("r12", [])).toBe("r12-w1");
    expect(workerTitle("r12", ["r12-w1", "r12-w2", "r12-w4"])).toBe("r12-w3");
    expect(workerTitle("Fix the login!", [])).toBe("Fix-the-login-w1");
    expect(workerTitle("x".repeat(60), [])).toBe("x".repeat(40) + "-w1");
    expect(workerTitle("!!!", [])).toBe("worker-1");
  });

  it("a fork is a worktree on this device based on the parent's branch, and carries parent + spawned", () => {
    const o = { ...defaultOptions(), inPlace: true, provision: true, strategy: "clone", initRepo: true, device: "mac", baseBranch: "dev", baseRemote: true, template: "T" };
    expect(forkOptions(o, { branch: "mf/r12" })).toEqual({
      ...o,
      inPlace: false,
      provision: false,
      strategy: "worktree",
      initRepo: false,
      device: "",
      baseBranch: "mf/r12",
      baseRemote: false,
    });
    expect(forkFields({ title: "r12" })).toEqual({ parent: "r12", spawned: true });
  });
});
