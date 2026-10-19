/** Starting and finishing work never loses input or reports a failure as
 * success (v0.10.0, W5). Source-level pins for the wiring; the pure pieces are
 * tested by behaviour where they live. */

import { describe, expect, it } from "vitest";
import { code, filesWith, fn, squash } from "./sourceText";
import { splitLogins } from "../components/intake/kit";
import { installTarget } from "../components/setup/AgentBlocked";
import { SETTINGS_SEARCH } from "../components/palette/settingsSearch";
import { insertSaved } from "../components/dialogs/newSession/PromptFold";

const SETTINGS = code("components/settings/useSettings.tsx");
const AUTOSTART = code("components/intake/AutoStartSwitches.tsx");
const REPOS = code("components/intake/RepoSources.tsx");

describe("a failed settings save is never followed by a success line", () => {
  it("saveField and saveGroups resolve to whether the save landed", () => {
    expect(SETTINGS).toContain("saveGroups(patches: Record<string, Json>, okMsg?: string): Promise<boolean>;");
    expect(SETTINGS).toContain("saveField(group: string, field: string, value: unknown): Promise<boolean>;");
    // Both catch blocks report false after their "Save failed" toast.
    expect(SETTINGS.match(/toast\("Save failed: [^\n]*\n[^\n]*\n\s*return false;/g)).toHaveLength(2);
  });

  it("Pause all stops before 'All auto-start is off' when the save failed", () => {
    const body = squash(AUTOSTART);
    const guard = body.indexOf('!(await settings.saveGroup("github", patch))) return;');
    expect(guard).toBeGreaterThan(-1);
    expect(guard).toBeLessThan(body.indexOf("All auto-start is off"));
  });

  it("adding a repository stays on the step and re-sweeps only on success", () => {
    const commit = squash(fn("components/intake/RepoSources.tsx", "AddRepoFlow"));
    expect(commit).toContain("const ok = await s.saveGroups(");
    expect(commit).toMatch(/if \(!ok\) \{ .*setError\(.*return; \} resweep\(fresh\);/);
    expect(REPOS).toContain(".then((ok) => ok && resweep([...added, ...dropped]));");
    expect(REPOS).toContain(".then((ok) => ok && resweep([use]));");
  });
});

describe("typed fields save as you type, not on blur alone", () => {
  it("a repository's text fields and notes go through useAutosave", () => {
    const block = squash(fn("components/intake/RepoSources.tsx", "BlockInput"));
    expect(block).toContain("useAutosave<string>");
    expect(block).toContain("onChange={(e) => autosave.schedule(e.target.value)}");
    expect(block).toContain("onBlur={() => autosave.flush()}");
    expect(REPOS).toContain("onChange={(e) => promptSave.schedule(e.target.value)}");
  });

  it("'Ignore comments from' / 'Ignore issues from' use LoginListInput", () => {
    for (const f of ["components/intake/PullRequestsTab.tsx", "components/intake/IssuesTab.tsx"]) {
      const c = code(f);
      expect(c).toContain("<LoginListInput");
      expect(c).not.toContain("skipDraft");
    }
  });

  it("Insert saved text goes in at the caret, or after a blank line — never over the Task", () => {
    expect(insertSaved("", "Fix it", null)).toBe("Fix it");
    expect(insertSaved("Do A\n", "Fix it", null)).toBe("Do A\n\nFix it");
    expect(insertSaved("Do A then B", "X ", { start: 8, end: 8 })).toBe("Do A theX n B");
    expect(insertSaved("Do A then B", "C", { start: 10, end: 11 })).toBe("Do A then C");
    expect(insertSaved("abc", "Z", { start: 99, end: 99 })).toBe("abcZ");
    expect(code("components/dialogs/newSession/PromptFold.tsx")).toContain(
      "setPrompt((cur) => insertSaved(cur, p.prompt, caret.current));"
    );
  });

  it("splitLogins trims and drops blanks", () => {
    expect(splitLogins(" dependabot, ,renovate ,")).toEqual(["dependabot", "renovate"]);
    expect(splitLogins("")).toEqual([]);
  });
});

describe("a fixed agent problem doesn't stay on screen", () => {
  it("New asks again which agents are installed and signed in on a keeping reopen", () => {
    const nsd = code("components/dialogs/NewSessionDialog.tsx");
    const body = squash(nsd);
    expect(body).toContain("const refreshAgentState = () => {");
    // The resume path (a typed draft, a hand-back) and the failed-create reopen.
    expect(body).toMatch(/\.then\(\(list\) => setTemplates\(list\)\) \.catch\(\(\) => \{\}\); refreshAgentState\(\); return; \}/);
    expect(body).toContain("setAdvancedOpen(true); refreshAgentState(); return;");
  });

  it("Install for an agent other than the default opens Settings → Agents on it", () => {
    expect(installTarget("codex", "claude")).toBe("agents");
    expect(installTarget("claude", "claude")).toBe("setup");
    expect(installTarget("codex", "")).toBe("agents");
    const blocked = code("components/setup/AgentBlocked.tsx");
    expect(blocked).toContain("Install {agentName(block.name)}…");
    expect(blocked).not.toContain('openDialogFor("setup");\n          }}');
    expect(code("components/settings/screens/CodingCli.tsx")).toContain("const [otherProvider, setOtherProvider] = useState(focusAgent);");
  });
});

describe("Verify keeps an edit or a retry when the server refused", () => {
  it("editPlanStep and writePlanFor resolve whether they took", () => {
    const va = squash(code("lib/verifyActions.ts"));
    expect(va).toContain("fields: Partial<TestStep> ): Promise<boolean> {");
    expect(va).toContain("export async function writePlanFor(target: string): Promise<boolean> {");
    expect(va).toMatch(/errorPop\("Couldn't change that step", errMsg\(err\)\); return false;/);
    expect(fn("lib/verifyActions.ts", "writePlanFor")).toMatch(/errorPop\("Couldn't write a checklist", errMsg\(err\)\);\s*return false;/);
  });

  it("the step editor closes, and the banner clears, only on success", () => {
    expect(code("components/dialogs/VerifyStep.tsx")).toContain(
      "if ((await onEdit(step, fields)) !== false) setEditing(false);"
    );
    expect(code("components/dialogs/VerifyDialog.tsx")).toContain(
      'if (await writePlanFor(missingTarget.title)) setMissingBranch("");'
    );
  });
});

describe("one name per thing", () => {
  it("New's link to Schedule says Schedule, and so does its save toast", () => {
    const strip = code("components/dialogs/newSession/TemplatesStrip.tsx");
    expect(strip).toContain("Open Schedule…");
    expect(strip).not.toContain("All templates…");
    expect(code("components/dialogs/NewSessionDialog.tsx")).not.toContain("in Templates at the top of New");
  });

  it("page 1's only way to the form is the footer's 'More options'", () => {
    const d = code("components/dialogs/newSession/DescribeSection.tsx");
    // The "Runs in" line's own More options link duplicated the footer's
    // button word for word: one move, one place.
    expect(d).not.toContain('id="new-describe-change"');
    expect(squash(d)).not.toMatch(/>\s*More options\s*<\/button>/);
    expect(d).not.toContain("function NewFolderRow");
    expect(code("components/dialogs/newSession.ts")).not.toContain("then press Create.");
  });

  it("the welcome tour names no agent of its own", () => {
    const tour = code("lib/tours/welcome.ts");
    expect(tour).not.toContain("Claude");
    expect(tour).toContain("① Install your agent");
  });

  it("Intake's scheduled rows say THE schedule line", () => {
    expect(code("components/intake/ScheduledTemplates.tsx")).toContain("line: templateScheduleLine(t, status, now),");
  });

  it("the New dialog's disclosures draw their chevron, not a 10px glyph", () => {
    const css = code("components/dialogs/NewSessionDialog.css");
    for (const sel of [".nf-advanced>summary::before {", ".rt-fold>summary::before {"]) {
      const rule = css.slice(css.indexOf(sel), css.indexOf("}", css.indexOf(sel)));
      expect(rule).toContain('content: "";');
      expect(rule).toContain("border-right: 1.5px solid currentColor;");
    }
  });
});

describe("accessible names and live regions", () => {
  it("New's error is an alert and the sentence box is named by its question", () => {
    expect(code("components/dialogs/newSession/FooterActions.tsx")).toContain(
      '<p id="new-error" className="error" role="alert">'
    );
    expect(code("components/dialogs/newSession/DescribeSection.tsx")).toContain('aria-labelledby="new-describe-label"');
  });

  it("Browse… and Manage sit outside the <label>s", () => {
    expect(code("components/dialogs/newSession/FolderPicker.tsx")).toContain("<label htmlFor={ids.input}>Folder</label>");
    const agent = code("components/dialogs/newSession/AgentAccount.tsx");
    expect(agent).toContain('<label htmlFor={scope + "-program"}>Agent</label>');
    expect(agent).not.toContain('<label className="nf-agent">');
  });

  it("Settings and Setup are modal dialogs to assistive tech", () => {
    expect(code("components/settings/SettingsDialog.tsx")).toContain(
      '<div id="settings-panel" role="dialog" aria-modal="true" aria-labelledby="settings-title">'
    );
    expect(code("components/dialogs/SetupDialog.tsx")).toContain(
      '<div id="setup-panel-dlg" role="dialog" aria-modal="true" aria-labelledby="setup-title">'
    );
  });

  it("the template editors' error line is mounted before it has anything to say", () => {
    const ed = code("components/dialogs/TemplateEditor.tsx");
    expect(ed).not.toContain('{p.error && (\n          <p className="error"');
    expect(ed.match(/<p className="error" aria-live="polite">\n\s*\{p\.error/g)).toHaveLength(2);
  });
});

describe("finishing says what happened", () => {
  it("Run now in Intake closes Intake on the new session", () => {
    expect(squash(code("components/intake/ScheduledTemplates.tsx"))).toContain(
      "const title = await runTemplateNow(r.name); if (title) openSessionFromIntake(title);"
    );
  });

  it("a scheduled save toasts its schedule line; Stop schedule offers Undo", () => {
    const d = squash(code("components/dialogs/TemplatesDialog.tsx"));
    expect(d).toContain("toast(`Saved “${body.name}” — ${templateScheduleLine(body, st)}`)");
    expect(d).toContain('action: { label: "Undo", onClick: () => void undoStop(before) }');
    expect(d).toContain("onStopSchedule={() => void save({ ...form, scheduled: false }, true)}");
  });
});

describe("settings search reaches advanced-only controls", () => {
  // Every AdvancedFold a basic screen hides entirely (no `basic`).
  const hidden = new Set<string>();
  for (const f of filesWith("<AdvancedFold", /screens\/.*\.tsx$/))
    for (const m of code(f).matchAll(/<AdvancedFold\s+id="([^"]+)"([^>]*)>/g)) if (!/\bbasic\b/.test(m[2])) hidden.add(m[1]);

  it("an entry whose anchor is an advanced-only fold turns advanced on first", () => {
    expect(hidden.has("general-advanced")).toBe(true);
    for (const e of SETTINGS_SEARCH) {
      const id = e.anchor?.startsWith("#") ? e.anchor.slice(1) : "";
      expect(!!e.advanced, e.label).toBe(hidden.has(id));
    }
    expect(squash(code("components/palette/settingsSearch.ts"))).toContain(
      "if (e.advanced && !readAdvancedMode()) writeAdvancedMode(true); openSettings(e.key);"
    );
    expect(squash(code("components/settings/SettingsDialog.tsx"))).toContain("if (adv && !advanced) changeMode(true);");
  });
});
