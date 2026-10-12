/** The New dialog's two pages (the v0.7 landing card, then the form), the
 * form's Name → Enter fast path, and the Base branch picker — the pure pieces NewSessionDialog.tsx exports, plus the
 * dialog's own source where a behaviour is only visible there (the vitest
 * environment is node; the dialog is read as text, the way
 * newSession.test.ts pins the rest of it). */

import { describe, expect, it } from "vitest";
import DIALOG from "../components/dialogs/NewSessionDialog.tsx?raw";
import PURE from "../components/dialogs/newSession.ts?raw";
import DESCRIBE from "../components/dialogs/newSession/DescribeSection.tsx?raw";
import TEMPLATES from "../components/dialogs/newSession/TemplatesStrip.tsx?raw";
import FOLDER from "../components/dialogs/newSession/FolderPicker.tsx?raw";
import BROWSER from "../components/dialogs/newSession/FolderBrowser.tsx?raw";
import AGENT from "../components/dialogs/newSession/AgentAccount.tsx?raw";
import WORKSPACE from "../components/dialogs/newSession/WorkspaceFold.tsx?raw";
import PROMPT from "../components/dialogs/newSession/PromptFold.tsx?raw";
import LAUNCH from "../components/dialogs/newSession/LaunchFold.tsx?raw";
import FOOTER from "../components/dialogs/newSession/FooterActions.tsx?raw";
import SUBMIT from "../lib/newSessionSubmit.ts?raw";
import {
  baseBranchFields,
  ctrlEnterAction,
  enterAction,
  foldSummaryFor,
  rememberedProfileVerdict,
} from "../components/dialogs/NewSessionDialog";
import { forgetNewSessionProfile, lastNewSessionChoice, rememberNewSessionChoice } from "../state/store";


/** The dialog's whole source: NewSessionDialog.tsx and the modules it was
 * split into. A slice that has to stay within one piece of code reads that
 * piece's own file. */
const SRC = [DIALOG, PURE, DESCRIBE, TEMPLATES, FOLDER, BROWSER, AGENT, WORKSPACE, PROMPT, LAUNCH, FOOTER, SUBMIT].join("\n");

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
  it("opens on page 1, the sentence card, every time; a failed create reopens on the form at Name", () => {
    // v0.7's flow, restored on the owner's word ("I like how it used to look
    // in v0.7"): the landing card first, the form one click behind it.
    expect(DIALOG).toContain("const [page, setPage] = useState(1);");
    expect(DIALOG).toContain("const openedPage = useRef(1);");
    const reset = DIALOG.slice(DIALOG.indexOf("const prefill = useUi.getState().takeNewPrefill();"), DIALOG.indexOf("setNewFolderOk(false);"));
    expect(reset).toContain("openedPage.current = 1;");
    expect(reset).toContain("setPage(1);");
    expect(DIALOG).not.toContain("useState(2)");
    // The caret goes to the sentence box; Name only after a failed create,
    // whose reopen is about fixing the form failCreate put back on screen.
    expect(SRC).toContain("const el = shown ? (toTitle ? titleRef.current : describeRef.current) : null;");
    const failed = DIALOG.slice(DIALOG.indexOf("if (failedReopen.current) {"), DIALOG.indexOf("setTab(\"session\");"));
    expect(failed).toContain("openedPage.current = 2;");
    expect(failed).toContain('openedFocus.current = "title";');
    expect(failed).toContain('openedFocus.current = "describe";');
    expect(SUBMIT).toContain("setPage(2);");
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
    const submit = SUBMIT.slice(SUBMIT.indexOf("const submit = async () => {"), SUBMIT.indexOf("return { failCreate"));
    expect(submit).toContain("buildBody(");
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

describe("page 1 — the sentence card, as in v0.7", () => {
  it("draws the box, its explanation and examples, and nothing of the form", () => {
    expect(DESCRIBE).toContain('"What do you want to work on?"');
    expect(DESCRIBE).toContain('placeholder="e.g. fix the login bug in acme-api"');
    expect(DESCRIBE).toContain("Your coding CLI reads this and works out which folder to use");
    expect(DESCRIBE).toContain("<b>Nothing is created until you pick one of the buttons below.</b>");
    expect(DESCRIBE).toContain("Also understood:");
    // The form's own sections are page 2's alone — the box is not on the form.
    const p1 = DIALOG.slice(DIALOG.indexOf("{page === 1 ? ("), DIALOG.indexOf("<TemplatesStrip"));
    expect(p1).toContain("<DescribeSection {...describeProps} />");
    expect(DIALOG.split("<DescribeSection").length - 1).toBe(1);
    // The collapsed "Describe it instead…" door is gone with the form's box.
    expect(SRC).not.toContain("describeOpen");
    expect(SRC).not.toContain('id="new-describe-open"');
    expect(SRC).not.toContain("Describe it instead…");
  });

  it("has v0.7's three buttons in increasing order of commitment", () => {
    const skip = FOOTER.indexOf('id="new-describe-skip"');
    const go = FOOTER.indexOf('id="new-describe-go"');
    const start = FOOTER.indexOf('id="new-describe-start"');
    expect(skip).toBeGreaterThan(-1);
    expect(skip).toBeLessThan(go);
    expect(go).toBeLessThan(start);
    // Set it up myself → the form, empty, no model turn.
    const skipBtn = FOOTER.slice(skip, go);
    expect(skipBtn).toContain('className="nf-quiet"');
    expect(skipBtn).toContain("onClick={() => setPage(2)}");
    expect(skipBtn).toContain("Set it up myself instead");
    // Review details first ↵ → the model fills the form in; ↵ says Enter does it.
    const goBtn = FOOTER.slice(go, start);
    expect(goBtn).toContain('onClick={() => void runDescribe("fill")}');
    expect(goBtn).toContain("Review details first");
    expect(goBtn).toContain("↵");
    // Create session (startLabel(1)) → read and create, without the form.
    expect(FOOTER.slice(start)).toContain("onClick={runMode ? () => void startRun() : startNow}");
  });

  it("Enter in the box is Review details first; Shift+Enter (and a list's Enter) a new line", () => {
    const key = DESCRIBE.slice(DESCRIBE.indexOf("onKeyDown={(e) => {"), DESCRIBE.indexOf("{describing && ("));
    expect(key).toContain('enterAction({ field: "describe", shift: e.shiftKey, runMode }) === "newline"');
    expect(key).toContain('void runDescribe("fill")');
    expect(key).toContain("e.preventDefault();");
  });

  it("the plan lands on page 2 for review, and Create waits for a breath", () => {
    const run = DESCRIBE.slice(DESCRIBE.indexOf("const runDescribe = async"), DESCRIBE.indexOf("return { applyPlan"));
    expect(run).toContain("applyPlan(a)");
    expect(run).toContain("setPage(2);");
    expect(run).toContain("submitArmAt.current = Date.now() + SUBMIT_ARM_MS");
    // A failed read names the way out page 1 offers.
    expect(run).toContain('" — use Set it up myself instead."');
  });

  it("page 2 has ← Back to the sentence, and Create is held while the model is reading", () => {
    const form = FOOTER.slice(FOOTER.indexOf(") : ("));
    expect(form).toContain('id="new-back"');
    expect(form).toContain("onClick={() => setPage(1)}");
    // Back for every opening, not only a batch.
    expect(form).not.toContain("{batch && (");
    expect(SRC).toContain("disabled={draft.starting || describing}");
  });
});

describe("Ctrl/Cmd+Enter never creates a session nobody has seen", () => {
  it("on page 1 reads the sentence (like Enter); a list keeps starting its rows; page 2 submits the form", () => {
    expect(ctrlEnterAction({ page: 1, runMode: false })).toBe("describe");
    expect(ctrlEnterAction({ page: 1, runMode: true })).toBe("submit");
    expect(ctrlEnterAction({ page: 2, runMode: false })).toBe("submit");
    expect(ctrlEnterAction({ page: 2, runMode: true })).toBe("submit");
  });

  it("the modal root's Ctrl/Cmd+Enter goes through ctrlEnterAction, not straight to submit", () => {
    const at = DIALOG.indexOf('e.key === "Enter" && (e.ctrlKey || e.metaKey)');
    expect(at).toBeGreaterThan(-1);
    const branch = DIALOG.slice(at, at + 700);
    expect(branch).toContain("ctrlEnterAction({ page, runMode })");
    expect(branch).toContain('void runDescribe("fill")');
    expect(branch.indexOf("ctrlEnterAction")).toBeLessThan(branch.indexOf("submit()"));
  });
});

describe("Base branch — the Git & workspace fold's picker", () => {
  it("sends base_ref and base_branch together, for a new worktree only", () => {
    const body = SUBMIT.slice(SUBMIT.indexOf("const buildBody = ("), SUBMIT.indexOf("/** POST the create"));
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
    const eff = WORKSPACE.slice(WORKSPACE.indexOf("// The Base branch menu for the folder"), WORKSPACE.indexOf("export interface WorkspaceFoldProps"));
    expect(eff).toContain('setBaseBranch("")');
    expect(eff).toContain("looksLikePath(path)");
  });

  it("draws the picker only for a plain worktree, with the server's own choice as the first row", () => {
    expect(SRC).toContain("worktreeMode && branches?.is_git");
    expect(SRC).toContain('id="new-base-branch"');
    const pick = WORKSPACE.slice(WORKSPACE.indexOf('id="new-base-branch"'), WORKSPACE.indexOf("OUTSIDE the mode group"));
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
    const pick = WORKSPACE.slice(WORKSPACE.indexOf('id="new-base-branch"'), WORKSPACE.indexOf("OUTSIDE the mode group"));
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
    const strip = TEMPLATES.slice(TEMPLATES.indexOf('id="new-templates"'));
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
    const settle = DIALOG.slice(DIALOG.indexOf("settleProfile.current = () => {"), DIALOG.indexOf("}, [authProfiles]);"));
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
