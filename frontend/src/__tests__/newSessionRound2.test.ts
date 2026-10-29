/** UX round 2 on the New dialog: a separate worktree by default with the
 * branch shown before Create, a warning before two sessions share one
 * checkout, the Task on the form, names from the task, a 30 s cap on the
 * describe read with a pre-filled fallback, uninstalled agents marked and
 * refused, one "Save as template…" that saves the whole form, and Manage…
 * trips that hand the form back as it was. */

import { describe, expect, it } from "vitest";
import DIALOG from "../components/dialogs/NewSessionDialog.tsx?raw";
import DESCRIBE from "../components/dialogs/newSession/DescribeSection.tsx?raw";
import FOOTER from "../components/dialogs/newSession/FooterActions.tsx?raw";
import TEMPLATES_DIALOG from "../components/dialogs/TemplatesDialog.tsx?raw";
import PROMPTS_DIALOG from "../components/dialogs/PromptsDialog.tsx?raw";
import AGENT from "../components/dialogs/newSession/AgentAccount.tsx?raw";
import {
  titleFromFolder,
  DESCRIBE_CAP_MS,
  agentMissingReason,
  plannedName,
  elapsedLabel,
  preferInstalled,
  sanitizeBranch,
  sessionsInFolder,
  sharedCheckoutWarning,
  templateFromForm,
  titleFromPrompt,
} from "../components/dialogs/newSession";

describe("a session is named from its task", () => {
  // The same cases as tests/unit/test_session_create.py::test_title_from_prompt
  // — the preview and the server must agree on the name.
  it.each([
    ["Add a README section about setup", "add-readme-section-about-setup"],
    ["  \n\nFix the login bug in acme-api!\nmore detail", "fix-login-bug-acme-api"],
    ["please update the CHANGELOG for 0.9", "update-changelog-0-9"],
    ["a the to", "a-the-to"],
    ["🙂 ✨", ""],
    ["", ""],
    ["x".repeat(50), "x".repeat(32)],
    ["refactor authentication middleware session handling cleanup tests", "refactor-authentication"],
  ])("%j → %j", (prompt, title) => {
    expect(titleFromPrompt(prompt)).toBe(title);
  });

  it("the Name placeholder says so", () => {
    expect(DIALOG).toContain("— optional; empty names it from the task");
    // The name Create will really use (numbered past a taken one), or — with
    // no task yet — that it will come from the task, never "untitled".
    // Round 5 (r5-new finding 18): the placeholder is the name alone; "Enter
    // starts it" is the ↵ on the Start button.
    expect(DIALOG).toContain('placeholder={planned.from === "none" ? "named from the task" : planned.title}');
    expect(DIALOG).not.toContain("Enter creates it");
  });
});

describe("Works in: a separate worktree by default", () => {
  it("defaults to the worktree, on every open", () => {
    expect(DIALOG).toContain("const [inPlace, setInPlace] = useState(false);");
    expect(DIALOG).toContain("setInPlace(false);\n    setInitRepo(false);");
  });

  it("previews the branch the worktree will be cut on", () => {
    const none = new Set<string>();
    expect(plannedName({ prefix: "me/", title: "", prompt: "Fix the login bug", taken: none }).branch).toBe("me/fix-login-bug");
    expect(plannedName({ prefix: "me/", title: "My Thing", prompt: "x", taken: none }).branch).toBe("me/my-thing");
    expect(plannedName({ prefix: "", title: "", prompt: "", taken: none }).branch).toBe("untitled");
    // No task but a folder: named for the folder, as the server does
    // (session_create.title_from_place); numbered past a taken one.
    const inFolder = plannedName({ prefix: "me/", title: "", prompt: "", taken: none, folder: "/home/me/Allure/sitecheck-bot7/" });
    expect([inFolder.title, inFolder.branch, inFolder.from]).toEqual(["sitecheck-bot7", "me/sitecheck-bot7", "folder"]);
    expect(plannedName({ prefix: "", title: "", prompt: "", taken: new Set(["app"]), folder: "C:\\\\work\\\\App" }).title).toBe("app-2");
    expect(titleFromFolder("")).toBe("");
    expect(titleFromFolder("/tmp/---")).toBe("");
    expect(sanitizeBranch("--A b//")).toBe("a-b");
  });

  it("warns before a second session shares one checkout", () => {
    const rows = [
      { title: "one", path: "/r/app", in_place: true },
      { title: "two", path: "/r/app/", in_place: false },
      { title: "three", path: "/r/other", in_place: true },
    ];
    expect(sessionsInFolder(rows, ["/r/app"])).toEqual(["one"]);
    expect(sessionsInFolder(rows, ["", "/r/other/"])).toEqual(["three"]);
    expect(sessionsInFolder(rows, [""])).toEqual([]);
    expect(sharedCheckoutWarning([])).toBe("");
    expect(sharedCheckoutWarning(["one"])).toContain("“one” is already editing this folder directly");
    expect(sharedCheckoutWarning(["a", "b"])).toContain("2 sessions are already editing");
  });
});

describe("uninstalled agents", () => {
  const have = { claude: true, codex: false };
  it("are refused at Create, with the way out", () => {
    expect(agentMissingReason("codex", have)).toContain("isn't installed on this device");
    expect(agentMissingReason("codex", have)).toContain("Settings → Agents");
    expect(agentMissingReason("claude", have)).toBe("");
    // Unknown never blocks: another device, an older server, a custom name.
    expect(agentMissingReason("codex", null)).toBe("");
    expect(agentMissingReason("aider", have)).toBe("");
  });
  it("never start as the default when another one is installed", () => {
    expect(preferInstalled("codex", ["codex", "claude"], have)).toBe("claude");
    expect(preferInstalled("claude", ["codex", "claude"], have)).toBe("claude");
    expect(preferInstalled("codex", ["codex"], { codex: false })).toBe("codex");
    expect(preferInstalled("codex", ["codex", "claude"], null)).toBe("codex");
  });
  it("are marked in the picker, and read from /api/providers/status", () => {
    expect(AGENT).toContain('installed && installed[p.name] === false ? " (not installed)" : ""');
    expect(DIALOG).toContain('"/api/providers/status"');
    expect(DIALOG).toContain("if (agentMissing && !runMode) {");
  });
});

describe("Save as template… saves the whole form", () => {
  const form = {
    name: " nightly ",
    program: "claude",
    repoPath: "/r",
    prompt: "do it",
    inPlace: false,
    provision: false,
    strategy: "worktree",
    initRepo: false,
    lane: "pr",
    askFirst: true,
  };
  it("folder, agent, works in, task and its autopilot depth", () => {
    expect(templateFromForm(form, [])).toEqual({
      name: "nightly",
      program: "claude",
      repo_path: "/r",
      prompt: "do it",
      provisioned: false,
      in_place: false,
      workspace_strategy: "worktree",
      init_repo: false,
      autopilot: "pr",
    });
  });
  it("fast-track off stores no key; a fast-track never runs in place; merge saves as pr", () => {
    const off = templateFromForm({ ...form, lane: "leave", inPlace: true }, []);
    expect(off).not.toHaveProperty("autopilot");
    expect(templateFromForm({ ...form, lane: "merge" }, []).autopilot).toBe("pr");
    expect(off.in_place).toBe(true);
    expect(templateFromForm({ ...form, inPlace: true }, []).in_place).toBe(false);
  });
  it("saving over a scheduled template keeps its schedule", () => {
    const sched = { at: "21:00", days: "weekdays" };
    expect(templateFromForm(form, [{ name: "NIGHTLY", schedule: sched }]).schedule).toEqual(sched);
  });
  it("is one button under More options, with an inline name row (no prompt())", () => {
    expect(DIALOG).toContain("<SaveTemplateRow");
    expect(FOOTER).toContain("Save as template…");
    expect(FOOTER).toContain('id="new-save-tpl-name"');
    expect(FOOTER).not.toContain("window.prompt(");
    // The strip repaints when the list changes — no reopen needed.
    expect(DIALOG).toContain("if (!deviceRef.current) setTemplates(templatesNow());");
  });
});

describe("describe-first: a clock, a cap and a way out", () => {
  it("caps the read at 30 s and offers the form with the text in it", () => {
    expect(DESCRIBE_CAP_MS).toBe(30000);
    expect(DESCRIBE).toContain("}, DESCRIBE_CAP_MS);");
    // Round 5: ONE recovery — start it anyway, in the folder the line shows
    // ("Start it in repo instead"); with no folder, the form picks one.
    expect(DESCRIBE).toContain('"Start it in " + leafName(repoPath.trim()) + " instead"');
    expect(DESCRIBE).toContain("onClick={repoPath.trim() ? startInstead : fillFormInstead}");
    expect(DESCRIBE).not.toContain("Fill the form with what I typed");
    // Who is asked and for how long at most, while it waits.
    expect(DESCRIBE).toContain("Asking {agentLabel} to fill in the details…");
    expect(DESCRIBE).toContain("(up to {Math.round(DESCRIBE_CAP_MS / 1000)} s)");
    // The way to the form still carries the text into Task (setUpMyself).
    expect(DESCRIBE).toContain("fillFormInstead: setUpMyself");
    expect(DESCRIBE).toContain("setPrompt(text);");
  });
  it("puts the spinner and the clock on the button that was pressed", () => {
    expect(FOOTER).toContain('describing && describeMode === "fill"');
    expect(FOOTER).toContain('describing && describeMode === "start"');
    expect(elapsedLabel(7.9)).toBe("0:07");
    expect(elapsedLabel(65)).toBe("1:05");
  });
});

describe("Manage… trips hand the form back", () => {
  it("Templates and Prompts reopen New as it was", () => {
    expect(TEMPLATES_DIALOG).toContain("if (readTemplatesTarget(target).fromNew) {");
    expect(PROMPTS_DIALOG).toContain('if (target === "new") {');
    for (const src of [TEMPLATES_DIALOG, PROMPTS_DIALOG]) expect(src).toContain("newSessionResume.keep = true;");
    expect(DIALOG).toContain("resume: newSessionResume.keep,");
    expect(DIALOG).toContain('if (mode === "resume") {');
  });
});

describe("Templates vs Prompts: one line each", () => {
  it("says which starts a session and which talks to a running one", () => {
    expect(TEMPLATES_DIALOG).toContain("Templates start a <strong>new</strong> session");
    expect(TEMPLATES_DIALOG).toContain("To send text to a session that's");
    expect(PROMPTS_DIALOG).toContain("Saved text you send to a running session.");
    // The contradictions the judges quoted are gone.
    expect(TEMPLATES_DIALOG).not.toContain("is also what the Prompts bar pastes");
    expect(TEMPLATES_DIALOG).not.toContain("a prompt-only template is a saved prompt");
  });
});
