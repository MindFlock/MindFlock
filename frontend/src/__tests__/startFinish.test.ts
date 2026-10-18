/** Starting and finishing work never loses input or reports a failure as
 * success (v0.10.0, W5). Source-level pins for the wiring; the pure pieces are
 * tested by behaviour where they live. */

import { describe, expect, it } from "vitest";
import { code, fn, squash } from "./sourceText";
import { splitLogins } from "../components/intake/kit";
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
