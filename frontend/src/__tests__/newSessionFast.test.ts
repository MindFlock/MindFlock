/** The New dialog's fast path, its secondary describe door, and the Base
 * branch picker — the pure pieces NewSessionDialog.tsx exports, plus the
 * dialog's own source where a behaviour is only visible there (the vitest
 * environment is node; the dialog is read as text, the way
 * newSession.test.ts pins the rest of it). */

import { describe, expect, it } from "vitest";
import SRC from "../components/dialogs/NewSessionDialog.tsx?raw";
import {
  baseBranchFields,
  enterAction,
  foldSummaryFor,
  rememberedProfileVerdict,
} from "../components/dialogs/NewSessionDialog";
import { forgetNewSessionProfile, lastNewSessionChoice, rememberNewSessionChoice } from "../state/store";


function memKV(init: Record<string, string> = {}) {
  const data = { ...init };
  return {
    data,
    getItem: (k: string) => (k in data ? data[k] : null),
    setItem: (k: string, v: string) => {
      data[k] = v;
    },
  };
}

describe("fast path — Name → Enter creates, no model turn", () => {
  it("opens on the form: page 2 is the initial page and every opening lands there", () => {
    expect(SRC).toContain("useState(2)");
    expect(SRC).toContain("openedPage.current = prefill ? 1 : 2;");
    // and the opening focus goes to Name, not to the sentence box
    expect(SRC).toContain("openedPage.current === 1 ? describeRef.current : titleRef.current");
  });

  it("Enter in Name is the form's submit, never a describe call", () => {
    expect(enterAction({ field: "name", shift: false, runMode: false })).toBe("create");
    expect(enterAction({ field: "other", shift: false, runMode: false })).toBe("create");
    // the only Enter that runs the model is the describe box's own
    expect(enterAction({ field: "describe", shift: false, runMode: false })).toBe("describe");
    expect(enterAction({ field: "describe", shift: true, runMode: false })).toBe("newline");
    expect(enterAction({ field: "describe", shift: false, runMode: true })).toBe("newline");
  });

  it("the create goes through submit → postCreate, which POSTs /api/instances and never /api/session-plan", () => {
    const submit = SRC.slice(SRC.indexOf("const submit = async () => {"), SRC.indexOf("const savePreset = () => {"));
    expect(submit).toContain("postCreate(");
    expect(submit).not.toContain("runDescribe");
    expect(submit).not.toContain("session-plan");
  });

  it("pre-fills the template, agent and account the last session in that folder used", () => {
    expect(SRC).toContain("applyLastUsed.current(tpls, provs)");
    expect(SRC).toContain("fillFromTemplate(t, { quiet: true })");
    // remembered on a successful create, for this device's folder
    expect(SRC).toContain("rememberNewSessionChoice(String(body.repo_path || \"\")");
  });

  it("remembers the choice per repo, and the last folder for the moment before the suggestion lands", () => {
    const kv = memKV();
    expect(lastNewSessionChoice("/r/a", kv)).toEqual({ folder: "", choice: null });
    rememberNewSessionChoice("/r/a", { template: "fix-tests", program: "claude", profile_id: "" }, kv);
    rememberNewSessionChoice("/r/b", { template: "", program: "codex", profile_id: "p1" }, kv);
    expect(lastNewSessionChoice("/r/a", kv).choice).toEqual({ template: "fix-tests", program: "claude", profile_id: "" });
    expect(lastNewSessionChoice("/r/b", kv).choice?.program).toBe("codex");
    // an unknown folder (nothing typed yet) answers for the last folder used
    expect(lastNewSessionChoice("", kv)).toEqual({
      folder: "/r/b",
      choice: { template: "", program: "codex", profile_id: "p1" },
    });
    expect(lastNewSessionChoice("/r/never", kv).choice).toBeNull();
    // garbage in storage reads as never set
    expect(lastNewSessionChoice("/r/a", memKV({ mf_new_session_last: "{nope" })).choice).toBeNull();
  });
});

describe("describe — the secondary door on the form", () => {
  it("is a button on the form that expands the box; its Enter runs the model turn with a visible thinking state and a Cancel", () => {
    expect(SRC).toContain('id="new-describe-open"');
    expect(SRC).toContain("Describe it instead…");
    const row = SRC.slice(SRC.indexOf('id="new-describe-open"'), SRC.indexOf('id="new-templates"'));
    expect(row).toContain('void runDescribe("fill")');
    expect(row).toContain("Thinking…");
    expect(row).toContain('id="new-describe-cancel"');
    expect(row).toContain("onClick={cancelDescribe}");
  });

  it("keeps the parsing flow: a landed plan is applied by applyPlan and holds Create for a breath", () => {
    const run = SRC.slice(SRC.indexOf("const runDescribe = async"), SRC.indexOf("const armInitRepo"));
    expect(run).toContain("applyPlan(a)");
    expect(run).toContain("submitArmAt.current = Date.now() + SUBMIT_ARM_MS");
    // the old escape hatch is gone with page 1: the form is already there
    expect(SRC).not.toContain("Set it up myself instead.");
  });

  it("Create is held while the model is reading, so a stray Enter cannot create from half-written fields", () => {
    expect(SRC).toContain("disabled={draft.starting || describing}");
  });
});

describe("Base branch — the Git & workspace fold's picker", () => {
  it("sends base_ref and base_branch together, for a new worktree only", () => {
    const body = SRC.slice(SRC.indexOf("const buildBody = ("), SRC.indexOf("/** POST the create"));
    expect(body).toContain("baseBranchFields(base, !!p.baseBranchRemote, !!p.continueBranch)");
    // a remote-tracking pick cuts from "origin/x" but compares against "x"
    expect(baseBranchFields("origin/x", true, false)).toEqual({ base_ref: "origin/x", base_branch: "x" });
    expect(baseBranchFields("release/1.2", false, false)).toEqual({ base_ref: "release/1.2", base_branch: "release/1.2" });
    expect(baseBranchFields("", true, true)).toEqual({});
    // inside the non-provisioned branch, and only when not in place
    expect(body).toContain("if (base && !p.inPlace)");
    expect(body.indexOf("baseBranchFields(")).toBeGreaterThan(body.indexOf("} else {"));
    // the form's submit passes the pick along
    expect(SRC).toContain("provisioned: provision,\n        baseBranch,");
  });

  it("is populated from GET /api/repos/branches for the folder in the field, and forgets the pick when the folder changes", () => {
    expect(SRC).toContain('"/api/repos/branches?path=" + encodeURIComponent(path)');
    const eff = SRC.slice(SRC.indexOf("// The Base branch menu for the folder"), SRC.indexOf("const cancelDescribe"));
    expect(eff).toContain('setBaseBranch("")');
    expect(eff).toContain("looksLikePath(path)");
  });

  it("draws the picker only for a plain worktree, with the server's own choice as the first row", () => {
    expect(SRC).toContain("worktreeMode && branches?.is_git");
    expect(SRC).toContain('id="new-base-branch"');
    const pick = SRC.slice(SRC.indexOf('id="new-base-branch"'), SRC.indexOf("OUTSIDE the mode group"));
    expect(pick).toContain('<option value="">');
  });

  it("a remote pick offers a second choice: work on the branch itself (continue_branch)", () => {
    // the default stays a new branch; the second choice sends continue_branch, not base_branch
    expect(baseBranchFields("origin/feature/x", true, true)).toEqual({
      base_ref: "origin/feature/x",
      continue_branch: "feature/x",
    });
    // a LOCAL pick never continues — "Work on x itself" is only for a pushed branch
    expect(baseBranchFields("feature/x", false, true)).toEqual({ base_ref: "feature/x", base_branch: "feature/x" });
    const pick = SRC.slice(SRC.indexOf('id="new-base-branch"'), SRC.indexOf("OUTSIDE the mode group"));
    expect(pick).toContain("{canContinue && remoteBranchName(baseBranch) && (");
    expect(pick).toContain('id="new-base-mode-new"');
    expect(pick).toContain("Start a new branch from {baseBranch}");
    expect(pick).toContain('id="new-base-mode-continue"');
    expect(pick).toContain("(shared with whoever pushed it)");
    // the choice is keyed by the pick, so another pick is a new branch again
    expect(SRC).toContain("continueBranch: !!baseBranch && continueOn === baseBranch,");
    const base = { provisioned: false, strategy: "worktree", inPlace: false, baseBranch: "origin/feat", current: "main", initRepo: false };
    expect(foldSummaryFor({ ...base, continueBranch: true })).toBe("worktree on feat (shared branch)");
    expect(foldSummaryFor(base)).toBe("new worktree from origin/feat");
  });

  it("the shut fold's summary names the mode and the base", () => {
    const base = { provisioned: false, strategy: "worktree", inPlace: false, baseBranch: "", current: "main", initRepo: false };
    expect(foldSummaryFor(base)).toBe("new worktree from main");
    expect(foldSummaryFor({ ...base, baseBranch: "release/1.2" })).toBe("new worktree from release/1.2");
    expect(foldSummaryFor({ ...base, current: "" })).toBe("new worktree");
    expect(foldSummaryFor({ ...base, inPlace: true, initRepo: true })).toBe("in this folder · git init");
    expect(foldSummaryFor({ ...base, provisioned: true, strategy: "clone" })).toBe("provisioned clone");
    expect(SRC).toContain('id="new-advanced-sum"');
    // and the fold starts shut on every open
    expect(SRC).toContain("setAdvancedOpen(false);");
  });
});

describe("Templates strip — one list, always manageable", () => {
  it("shows Manage… whether or not a template exists, and opens the React dialog", () => {
    const strip = SRC.slice(SRC.indexOf('id="new-templates"'), SRC.indexOf('id="new-title"'));
    expect(strip).toContain('id="new-templates-manage"');
    expect(strip).toContain('openDialogFor("templates", "new")');
    expect(strip).toContain("No templates yet");
    expect(SRC).not.toContain("mindflockAddons");
  });
});

describe("remembered account — only while it still exists", () => {
  it("applies a known or default account, drops a removed one, waits for the list", () => {
    const profiles = [{ id: "p1" }, { id: "p2" }];
    expect(rememberedProfileVerdict("p1", profiles)).toBe("apply");
    expect(rememberedProfileVerdict("default", null)).toBe("apply");
    expect(rememberedProfileVerdict("gone", profiles)).toBe("drop");
    expect(rememberedProfileVerdict("gone", [])).toBe("drop");
    expect(rememberedProfileVerdict("p1", undefined)).toBe("wait");
    expect(rememberedProfileVerdict("", profiles)).toBe("none");
  });

  it("goes through setAccount, and forgets a removed account from the memory entry", () => {
    const settle = SRC.slice(SRC.indexOf("settleProfile.current = () => {"), SRC.indexOf("}, [authProfiles]);"));
    expect(settle).toContain('if (verdict === "apply") setAccount(pend.id, pend.program);');
    expect(settle).toContain('else if (verdict === "drop") forgetNewSessionProfile(pend.repo);');
    // the old unconditional restore is gone
    expect(SRC).not.toContain("if (choice.profile_id) setProfileId(choice.profile_id);");
    const kv = memKV();
    rememberNewSessionChoice("/r/a", { template: "t", program: "claude", profile_id: "gone" }, kv);
    rememberNewSessionChoice("/r/b", { template: "", program: "codex", profile_id: "p1" }, kv);
    forgetNewSessionProfile("/r/a", kv);
    expect(lastNewSessionChoice("/r/a", kv).choice).toEqual({ template: "t", program: "claude", profile_id: "" });
    // the rest of memory, and the last folder, are untouched
    expect(lastNewSessionChoice("", kv)).toEqual({ folder: "/r/b", choice: { template: "", program: "codex", profile_id: "p1" } });
  });
});
