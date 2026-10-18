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
  it("a fresh opening lands on page 1, the sentence card; a failed create reopens on the form at Name", () => {
    // v0.7's flow, restored on the owner's word ("I like how it used to look
    // in v0.7"): the landing card first, the form one click behind it. Round
    // 4: an opening that still holds a typed draft keeps it (and its page) —
    // see openingMode in newSessionDraft.test.ts.
    expect(DIALOG).toContain("const [page, setPage] = useState(1);");
    expect(DIALOG).toContain("const openedPage = useRef(1);");
    const reset = DIALOG.slice(DIALOG.indexOf("const resetForm = (prefill: string) => {"), DIALOG.indexOf("setNewFolderOk(false);"));
    expect(reset).toContain("openedPage.current = 1;");
    expect(reset).toContain("setPage(1);");
    expect(reset).toContain('openedFocus.current = "describe";');
    // A fresh opening forgets last time's base-branch choices with the branch.
    expect(reset).toContain('setBaseBranch("");');
    expect(reset).toContain('setContinueOn("");');
    expect(reset).toContain('setStrategy("worktree");');
    expect(DIALOG).not.toContain("useState(2)");
    // The caret goes to the sentence box; Name only after a failed create,
    // whose reopen is about fixing the form failCreate put back on screen.
    expect(SRC).toContain("const el = shown ? (toTitle ? titleRef.current : describeRef.current) : null;");
    const failed = DIALOG.slice(
      DIALOG.indexOf("if (failedReopen.current) {"),
      DIALOG.indexOf("const prefill = useUi.getState().takeNewPrefill();")
    );
    expect(failed).toContain("openedPage.current = 2;");
    expect(failed).toContain('openedFocus.current = "title";');
    expect(SUBMIT).toContain("setPage(2);");
  });

  it("Enter in Name is the form's submit, never a describe call", () => {
    expect(enterAction({ field: "name", shift: false, runMode: false })).toBe("create");
    expect(enterAction({ field: "other", shift: false, runMode: false })).toBe("create");
    // round 5: Enter in the box is Start — no Enter runs the model any more
    expect(enterAction({ field: "describe", shift: false, runMode: false })).toBe("start");
    expect(enterAction({ field: "describe", shift: true, runMode: false })).toBe("newline");
    expect(enterAction({ field: "describe", shift: false, runMode: true })).toBe("newline");
  });

  it("the create goes through submit → postCreate, which POSTs /api/instances and never /api/session-plan", () => {
    const submit = SUBMIT.slice(SUBMIT.indexOf("const submit = async (over"), SUBMIT.indexOf("return { failCreate"));
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
  it("draws the box and ONE line saying where Start runs, and nothing of the form", () => {
    expect(DESCRIBE).toContain('"What do you want to work on?"');
    expect(DESCRIBE).toContain('placeholder="e.g. fix the login bug in acme-api"');
    // Round 5: "Runs in [folder ▾] · own branch … · change" — the folder is
    // a real picker (recent folders + Browse…) and "change" opens the form.
    expect(DESCRIBE).toContain("<StartsInLine");
    expect(DESCRIBE).toContain("Runs in");
    expect(DESCRIBE).toContain('id="new-describe-folder"');
    expect(DESCRIBE).toContain('data-tour="new-folder"');
    expect(DESCRIBE).toContain('"Recent folders"');
    expect(DESCRIBE).toContain('<option value={BROWSE_VALUE}>Browse…</option>');
    expect(DESCRIBE).toContain('id="new-describe-change"');
    expect(DIALOG).toContain('" · own branch "');
    // The paragraph that explained three buttons is gone with them.
    expect(DESCRIBE).not.toContain("<b>Review details first</b>");
    expect(DESCRIBE).not.toContain("<b>Create session</b>");
    expect(DESCRIBE).not.toContain("Examples:");
    // The form's own sections are page 2's alone — the box is not on the form.
    const p1 = DIALOG.slice(DIALOG.indexOf("{page === 1 ? ("), DIALOG.indexOf("<TemplatesStrip"));
    expect(p1).toContain("<DescribeSection {...describeProps} />");
    expect(DIALOG.split("<DescribeSection").length - 1).toBe(1);
    // The collapsed "Describe it instead…" door is gone with the form's box.
    expect(SRC).not.toContain("describeOpen");
    expect(SRC).not.toContain('id="new-describe-open"');
    expect(SRC).not.toContain("Describe it instead…");
  });

  it("has ONE primary, Start ↵, with More options and Let the agent set it up… beside it", () => {
    const skip = FOOTER.indexOf('id="new-describe-skip"');
    const go = FOOTER.indexOf('id="new-describe-go"');
    const start = FOOTER.indexOf('id="new-describe-start"');
    expect(skip).toBeGreaterThan(-1);
    expect(skip).toBeLessThan(go);
    expect(go).toBeLessThan(start);
    // More options → the form, with what was typed as its Task, no model turn.
    const skipBtn = FOOTER.slice(skip, go);
    expect(skipBtn).toContain('className="nf-quiet"');
    expect(skipBtn).toContain("onClick={setUpMyself}");
    expect(skipBtn).toContain("More options");
    expect(FOOTER).not.toContain("Set it up myself instead");
    // Let the agent set it up… → the model fills the form in; a click, not Enter.
    const goBtn = FOOTER.slice(go, start);
    expect(goBtn).toContain('onClick={() => void runDescribe("fill")}');
    expect(goBtn).toContain("Let the agent set it up…");
    expect(goBtn).not.toContain("↵");
    expect(FOOTER).not.toContain("Review details first");
    // Start ↵ → create now, without the form and without waiting on the agent
    // (it is not disabled by a read) — only by a folder that isn't there.
    const startBtn = FOOTER.slice(start);
    expect(startBtn).toContain("onClick={runMode ? () => void startRun() : startNow}");
    expect(startBtn).toContain("disabled={draft.starting || (runMode && describing) || (!runMode && !!startBlocked)}");
    expect(startBtn).toContain("{START_LABEL}");
    expect(startBtn).toContain("↵");
    // The accent is Start's (NewSessionDialog.css).
  });

  it("Enter in the box is Start; Shift+Enter (and a list's Enter) a new line", () => {
    const key = DESCRIBE.slice(DESCRIBE.indexOf("onKeyDown={(e) => {"), DESCRIBE.indexOf("{describing && ("));
    expect(key).toContain('enterAction({ field: "describe", shift: e.shiftKey, runMode }) === "newline"');
    expect(key).toContain("onEnterStart();");
    expect(key).not.toContain("runDescribe");
    expect(key).toContain("e.preventDefault();");
    expect(DIALOG).toContain("onEnterStart: startNow,");
  });

  it("the plan lands on page 2 for review, and Create waits for a breath", () => {
    const run = DESCRIBE.slice(DESCRIBE.indexOf("const runDescribe = async"), DESCRIBE.indexOf("return { applyPlan"));
    expect(run).toContain("applyPlan(a)");
    expect(run).toContain("setPage(2);");
    expect(run).toContain("submitArmAt.current = Date.now() + SUBMIT_ARM_MS");
    // A failure (or the 30s cap) offers the form, pre-filled with the text:
    // the button beside the error says so, the error only says what failed.
    expect(run).toContain("setPlanError(errMsg(err));");
    expect(run).toContain("setDescribeFailed(true);");
  });

  it("page 2 has ← Back to the sentence, and Start is held while the model is reading", () => {
    const form = FOOTER.slice(FOOTER.indexOf(") : ("));
    expect(form).toContain('id="new-back"');
    expect(form).toContain("onClick={() => setPage(1)}");
    // Back for every opening, not only a batch; Clear lives on page 1 only.
    expect(form).not.toContain("{batch && (");
    expect(form).not.toContain('id="new-clear-form"');
    expect(SRC).toContain("disabled={draft.starting || describing || !!startBlocked}");
  });
});

describe("Ctrl/Cmd+Enter never creates a session nobody has seen", () => {
  it("on page 1 is Start (like Enter); a list keeps starting its rows; page 2 submits the form", () => {
    expect(ctrlEnterAction({ page: 1, runMode: false })).toBe("start");
    expect(ctrlEnterAction({ page: 1, runMode: true })).toBe("submit");
    expect(ctrlEnterAction({ page: 2, runMode: false })).toBe("submit");
    expect(ctrlEnterAction({ page: 2, runMode: true })).toBe("submit");
  });

  it("the modal root's Ctrl/Cmd+Enter goes through ctrlEnterAction, not straight to submit", () => {
    const at = DIALOG.indexOf('e.key === "Enter" && (e.ctrlKey || e.metaKey)');
    expect(at).toBeGreaterThan(-1);
    const branch = DIALOG.slice(at, at + 700);
    expect(branch).toContain("ctrlEnterAction({ page, runMode })");
    expect(branch).toContain("startNow()");
    expect(branch).not.toContain("runDescribe");
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

describe("Templates strip — one list, shown once there is one", () => {
  it("hides when empty, and its All templates… opens the React dialog", () => {
    const strip = TEMPLATES.slice(TEMPLATES.indexOf('id="new-templates"'));
    expect(strip).toContain('id="new-templates-manage"');
    expect(strip).toContain('openDialogFor("templates", templatesTarget({ fromNew: true, schedule: null }))');
    expect(strip).toContain("All templates…");
    // No templates: no strip at all (Save as template… makes the first).
    expect(TEMPLATES).toContain("if (!templates.length) return null;");
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
