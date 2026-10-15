/** UX round 4 on New and Templates/Prompts: typed text is never thrown away
 * (Set it up myself carries it, a reopen keeps it until Create or Clear),
 * Create session starts from the raw text without a model turn, the branch
 * preview names the REAL branch (the "-2" one) and who else works in the
 * repo, templates have a visible door (run / schedule on each chip, on the
 * landing too), one vocabulary between New and Templates, and Prompts
 * opened from New doesn't offer to paste into a running session. */

import { describe, expect, it } from "vitest";
import DIALOG from "../components/dialogs/NewSessionDialog.tsx?raw";
import DESCRIBE from "../components/dialogs/newSession/DescribeSection.tsx?raw";
import FOOTER from "../components/dialogs/newSession/FooterActions.tsx?raw";
import STRIP from "../components/dialogs/newSession/TemplatesStrip.tsx?raw";
import FOLDER from "../components/dialogs/newSession/FolderPicker.tsx?raw";
import WORKSPACE from "../components/dialogs/newSession/WorkspaceFold.tsx?raw";
import PROMPT from "../components/dialogs/newSession/PromptFold.tsx?raw";
import TEMPLATES_DIALOG from "../components/dialogs/TemplatesDialog.tsx?raw";
import TEMPLATE_EDITOR from "../components/dialogs/TemplateEditor.tsx?raw";
import PROMPTS_DIALOG from "../components/dialogs/PromptsDialog.tsx?raw";
import FASTTRACK from "../components/dialogs/FastTrackChoice.tsx?raw";
import SUBMIT from "../lib/newSessionSubmit.ts?raw";
import {
  freeTitle,
  hasTypedDraft,
  openingMode,
  otherSessionsLine,
  plannedName,
  sessionsInRepo,
  takenNames,
} from "../components/dialogs/newSession";
import { readTemplatesTarget, templatesTarget } from "../lib/templates";

describe("never throw away typed text", () => {
  it("a typed draft is what was typed — not the opening's own template pre-fill", () => {
    const blank = { describe: "", title: "", prompt: "", prefilledPrompt: "" };
    expect(hasTypedDraft(blank)).toBe(false);
    expect(hasTypedDraft({ ...blank, describe: "fix it" })).toBe(true);
    expect(hasTypedDraft({ ...blank, title: "x" })).toBe(true);
    expect(hasTypedDraft({ ...blank, prompt: "do it" })).toBe(true);
    // The last-used template wrote the Task by itself: not a draft.
    expect(hasTypedDraft({ ...blank, prompt: "nightly", prefilledPrompt: "nightly" })).toBe(false);
    expect(hasTypedDraft({ ...blank, prompt: "nightly, edited", prefilledPrompt: "nightly" })).toBe(true);
  });

  it("an opening keeps a draft until a Create; a failed create and a hand-back always keep; Intake's list starts fresh", () => {
    const o = { failedReopen: false, resume: false, prefill: false, draft: false, spent: false };
    expect(openingMode(o)).toBe("fresh");
    expect(openingMode({ ...o, draft: true })).toBe("resume");
    expect(openingMode({ ...o, draft: true, spent: true })).toBe("fresh");
    expect(openingMode({ ...o, resume: true })).toBe("resume");
    expect(openingMode({ ...o, failedReopen: true, spent: true })).toBe("failed");
    expect(openingMode({ ...o, draft: true, resume: true, prefill: true })).toBe("fresh");
  });

  it("the dialog spends the draft only on a Create, and offers Clear while one is there", () => {
    expect(DIALOG).toContain("draftSpent.current = true;");
    // Through the closeDialog newSessionSubmit calls once a create is sent.
    const sub = DIALOG.slice(DIALOG.indexOf("} = newSessionSubmit({"));
    expect(sub.slice(0, sub.indexOf("});"))).toContain("draftSpent.current = true;");
    expect(DIALOG).toContain("onClear: draftTyped ? clearDraft : undefined");
    // Round 5: Clear lives with the box on page 1; the form's ← Back leads
    // to it (one way to leave, one way to reset).
    expect(DESCRIBE).toContain('id="new-clear"');
    expect(FOOTER).not.toContain('id="new-clear-form"');
    expect(DIALOG).toContain('resetForm("");');
  });

  it("Set it up myself carries the sentence into Task, and keeps a Task edited since", () => {
    const fn = DESCRIBE.slice(DESCRIBE.indexOf("const setUpMyself = () => {"));
    const body = fn.slice(0, fn.indexOf("\n  };"));
    expect(body).toContain("(!prompt.trim() || prompt.trim() === carried.current)");
    expect(body).toContain("setPrompt(text);");
    expect(body).toContain("setPage(2);");
    expect(FOOTER).toContain("onClick={setUpMyself}");
  });
});

describe("Create session starts now, from the words as typed", () => {
  it("never waits on a model turn", () => {
    const start = DESCRIBE.slice(DESCRIBE.indexOf("const startNow = () => {"));
    const body = start.slice(0, start.indexOf("\n  };"));
    expect(body).toContain("startRaw();");
    expect(body).not.toContain("runDescribe(");
    const raw = DIALOG.slice(DIALOG.indexOf("const startRaw = () => {"));
    const rawBody = raw.slice(0, raw.indexOf("\n  };"));
    expect(rawBody).toContain("submitRaw({ prompt: text })");
    expect(rawBody).toContain("setPrompt(text);");
    expect(rawBody).not.toContain("session-plan");
    // The create path takes the sentence as the Task.
    expect(SUBMIT).toContain("prompt: over.prompt ?? prompt,");
  });

  it("says where Start runs it, on one line under the box", () => {
    expect(DESCRIBE).not.toContain("starts right away");
    expect(DESCRIBE).toContain("Runs in");
    expect(DESCRIBE).toContain('{"📁 " + leafName(path)}');
    expect(FOOTER).toContain("Start it now in the folder shown, with what you typed as its task (Enter)");
  });

  it("points at Intake for work that starts from a ticket, PR or issue", () => {
    expect(DESCRIBE).toContain('id="new-intake-door"');
    expect(DESCRIBE).toContain('openDialogFor("intake")');
  });
});

describe("a second session on the same repo", () => {
  it("numbers a taken name the way the server does", () => {
    expect(freeTitle("fix", new Set())).toBe("fix");
    expect(freeTitle("fix", new Set(["fix"]))).toBe("fix-2");
    expect(freeTitle("fix", new Set(["fix", "fix-2"]))).toBe("fix-3");
  });

  it("previews the REAL branch, and says why it is numbered", () => {
    const taken = new Set(["add-readme-section-about-setup"]);
    const p = plannedName({ prefix: "me/", title: "", prompt: "Add a README section about setup", taken });
    expect(p.branch).toBe("me/add-readme-section-about-setup-2");
    expect(p.renumbered).toBe("add-readme-section-about-setup");
    expect(p.from).toBe("task");
    // A typed name is never renumbered: the server refuses a taken one.
    const typed = plannedName({ prefix: "", title: "add-readme-section-about-setup", prompt: "", taken });
    expect(typed.clash).toBe(true);
    expect(typed.branch).toBe("add-readme-section-about-setup");
    // Nothing typed yet: the name will come from the task.
    expect(plannedName({ prefix: "me/", title: "", prompt: "", taken }).from).toBe("none");
    expect(WORKSPACE).toContain('" from your task"');
    expect(WORKSPACE).toContain('id="new-name-clash"');
  });

  it("takes renames into account, and not another device's rows", () => {
    const t = takenNames([{ title: "a" }, { title: "rig::b" }], { a: "nice-name" });
    expect([...t].sort()).toEqual(["a", "nice-name"]);
  });

  it("names who else works in the repo, however they work in it", () => {
    const rows = [
      { title: "one", path: "/r/app" },
      { title: "two", path: "/r/app/" },
      { title: "verify-x", path: "/r/app", hidden_by: "verify" },
      { title: "rig::three", path: "/r/app" },
      { title: "other", path: "/r/else" },
    ];
    expect(sessionsInRepo(rows, ["/r/app"])).toEqual(["one", "two"]);
    expect(otherSessionsLine(["one"], true)).toBe(
      "“one” is already working in this repo — a separate worktree keeps you apart."
    );
    expect(otherSessionsLine(["one", "two"], false)).toBe("2 other sessions are already working in this repo.");
    expect(otherSessionsLine([], true)).toBe("");
    expect(WORKSPACE).toContain('id="new-others"');
  });
});

describe("templates have a visible door", () => {
  it("the strip is on the landing too, with ▶ and ⏰ on every chip", () => {
    const p1 = DIALOG.slice(DIALOG.indexOf("{page === 1 ? ("), DIALOG.indexOf(") : (", DIALOG.indexOf("{page === 1 ? (")));
    expect(p1).toContain("<TemplatesStrip");
    expect(p1).toContain("landing");
    expect(STRIP).toContain('className="nt-act nt-run"');
    expect(STRIP).toContain('className={"nt-act nt-sched"');
    expect(STRIP).toContain("disabled={!!problem}");
    expect(DIALOG).toContain("void runTemplateNow(t.name);");
    expect(DIALOG).toContain('templatesTarget({ fromNew: true, schedule: t.name })');
  });

  it("the Templates dialog's target says who opened it and what to schedule", () => {
    for (const t of [
      { fromNew: false, schedule: null },
      { fromNew: true, schedule: null },
      { fromNew: false, schedule: "" },
      { fromNew: true, schedule: "" },
      { fromNew: true, schedule: "nightly deps|x" },
    ])
      expect(readTemplatesTarget(templatesTarget(t))).toEqual(t);
    // The values other code already sends keep their meaning.
    expect(readTemplatesTarget("new")).toEqual({ fromNew: true, schedule: null });
    expect(readTemplatesTarget("schedule")).toEqual({ fromNew: false, schedule: "" });
    expect(readTemplatesTarget(null)).toEqual({ fromNew: false, schedule: null });
  });

  it("the saved-template toast is a link, up long enough to read", () => {
    const save = DIALOG.slice(DIALOG.indexOf("Saved template “${body.name}”"));
    expect(save.slice(0, 400)).toContain("onClick: () =>");
  });

  it("an editor opened for a template the list hasn't brought yet waits for it", () => {
    expect(TEMPLATES_DIALOG).toContain("if (ui.editing && !t) return;");
    expect(TEMPLATES_DIALOG).toContain("}, [ui.editing, templates]);");
  });
});

describe("one vocabulary and one set of controls, New and Templates", () => {
  it("the Templates editor draws the New form's where-it-works controls", () => {
    for (const c of ["<WorksInGroup", "<ProvisionCheck", "<StrategyPicker", "<InitRepoCheck"])
      expect(TEMPLATE_EDITOR).toContain(c);
    expect(TEMPLATES_DIALOG).toContain("<TemplateEditor");
    for (const src of [TEMPLATES_DIALOG, TEMPLATE_EDITOR]) {
      expect(src).not.toContain("Provisioned worktree");
      expect(src).not.toContain("Create a git repo in the folder");
    }
  });

  it("a template carries its run as far as an autopilot depth", () => {
    // Templates store `autopilot` (commit / push / pr — never an unattended
    // merge); the editor offers exactly those, the New form's Fast-track keeps
    // its own control (and "Ask me before it ships" says why it is grey).
    expect(TEMPLATE_EDITOR).toContain('id="tpl-autopilot"');
    expect(TEMPLATE_EDITOR).toContain("AUTOPILOT_CHOICES.map");
    expect(FASTTRACK).toContain("(needs fast-track)");
  });

  it("Auto-split and Plan first get one plain line each", () => {
    expect(DIALOG).toContain('id="new-split-help"');
    expect(DIALOG).toContain("you approve the split first.");
    expect(PROMPT).toContain("the agent lists the files it will change and waits for your OK");
  });

  it("New's Task picker says saved text, not saved prompt", () => {
    // Round 5 (r5-new finding 15): templates and prompts read alike; the
    // picker says what it inserts.
    expect(PROMPT).toContain("Insert saved text…");
    expect(PROMPT).not.toContain("Insert a saved prompt");
  });

  it("Prompts opened from New fills the Task box instead of offering Paste into", () => {
    expect(PROMPTS_DIALOG).toContain("{!onUse && (");
    expect(PROMPTS_DIALOG).toContain("onClick={() => (onUse ? onUse(p.prompt) : pastePrompt(p.prompt))}");
    expect(PROMPTS_DIALOG).toContain("newSessionResume.prompt = text;");
    expect(DIALOG).toContain("const picked = newSessionResume.prompt;");
  });
});

describe("the form's smaller fixes", () => {
  it("the folder suggestions don't just repeat the field", () => {
    expect(FOLDER).toContain("suggestions.every((s) => s.path === folderPath)");
    expect(FOLDER).not.toContain("nf-suggest-legend\">📦 git repo");
  });

  it("Base branch is drawn (loading) from the first open of More options", () => {
    expect(WORKSPACE).toContain("Loading branches…");
  });
});
