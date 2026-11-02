/** Intake / Verify sources: "add once, edit where it shows". The source text
 * pins for the guardrails the move had to keep — the rules themselves are
 * driven in repoUses.test.ts, sourceForm.test.ts and intakeSurfaceUi.test.ts. */

import { describe, expect, it } from "vitest";
import { code, has } from "./sourceText";

const TICKETS = code("components/intake/TicketsTab.tsx");
const PRS = code("components/intake/PullRequestsTab.tsx");
const ISSUES = code("components/intake/IssuesTab.tsx");
const REPOS = code("components/intake/RepoSources.tsx");
const KIT = code("components/intake/kit.tsx");
const DIALOG = code("components/intake/IntakeDialog.tsx");
const VERIFY = code("components/dialogs/VerifyDialog.tsx");
const CHROME = code("components/dialogs/VerifyChrome.tsx");
const SETTINGS = code("components/settings/useSettings.tsx");

describe("one door to add a source", () => {
  it("is the dialog head's + Add source, opening at the FOOT of the tab that owns the kind", () => {
    expect(has(DIALOG, 'id="intake-add-source"')).toBe(true);
    expect(has(DIALOG, '? [{ kind: "repo", uses: ["pr"] }, "prs"]')).toBe(true);
    // The tab grows downward: the flow is the last thing in each tab's
    // fragment, after the work, and scrolls itself into view — never a
    // scroll-to-top that yanks what was being read.
    expect(has(DIALOG, "scrollTo({ top: 0 })")).toBe(false);
    for (const src of [TICKETS, PRS, ISSUES]) {
      const flow = src.lastIndexOf("{addFlow}");
      expect(flow).toBeGreaterThan(src.indexOf("set-block-hint"));
      expect(src.indexOf("{addFlow}")).toBe(flow);
    }
    expect(has(KIT, 'el.scrollIntoView({ block: "nearest" })')).toBe(true);
    // Verify's door adds a repository with Verify ticked and nothing else.
    expect(has(VERIFY, 'id="vf-repo-add-btn"')).toBe(true);
    expect(has(VERIFY, 'only="verify"')).toBe(true);
  });

  it("no tab keeps a card list or its own + Add under the work", () => {
    for (const src of [TICKETS, PRS, ISSUES, CHROME]) {
      expect(src).not.toContain("RepoSourceList");
      expect(src).not.toContain("SourceCard");
    }
    expect(TICKETS).not.toContain('"+ Add ticket source"');
  });
});

describe("the tracker flow keeps the guardrails", () => {
  it("a draft auto-starts nothing until a state is picked", () => {
    expect(has(TICKETS, 'const fresh = { id, provider, needs_filter: "on" } as Source;')).toBe(true);
  });

  // Round 5: Test used to save the draft first (so its verdict could be
  // recorded) — a Test then Cancel left a broken source behind and ticked
  // "Connect your tracker". Test is a look now; Finish is the one save.
  it("Test saves nothing; Finish stores the source, then records its test", () => {
    const test = TICKETS.slice(TICKETS.indexOf("const testAndContinue = async"), TICKETS.indexOf("const finish = async"));
    expect(test).toContain("await testDraftSource(src, draftMeta)");
    expect(test).not.toContain("persist(");
    expect(test).not.toContain("runTest(");
    // The look-only test never asks the server to record anything.
    const look = TICKETS.slice(TICKETS.indexOf("const testDraftSource = useCallback"), TICKETS.indexOf("const persist = useCallback"));
    expect(look).toContain('"/api/settings/test/ticketing"');
    expect(look).not.toContain("record");
    // Finish: the one save (draft included), THEN the recorded test.
    const fin = TICKETS.slice(TICKETS.indexOf("const finish = async"));
    expect(has(TICKETS, "await persist(latest.current || [], { changed: draft.id, withDraft: true });")).toBe(true);
    expect(fin.indexOf("withDraft: true")).toBeGreaterThan(-1);
    expect(fin.indexOf("withDraft: true")).toBeLessThan(fin.indexOf("await runTest(stored, draftMeta)"));
    // Every other save leaves the unsaved draft out.
    expect(TICKETS).toContain("const pending = opts.withDraft ? \"\" : pendingDraft.current;");
    // An edit to the draft is local.
    expect(TICKETS).toContain("updateDraft(id, patch);");
  });

  it("Finish refuses Anyone's without a state, and a source with no repo", () => {
    expect(has(TICKETS, 'const anyone = src.assignee_scope === "anyone";')).toBe(true);
    expect(has(TICKETS, "if (anyone && !hasFilter)")).toBe(true);
    expect(has(TICKETS, "if (!sourceRepos(src).length)")).toBe(true);
  });

  it("never switches Auto-start tickets on by itself", () => {
    const flow = TICKETS.slice(TICKETS.indexOf("const beginDraft ="), TICKETS.indexOf("const stored = sources.filter"));
    expect(flow).not.toContain("/api/mindflock/");
    expect(flow).not.toContain("toggle(");
  });
});

describe("edit where it shows", () => {
  it("every configured ticket source has a heading — even filtered to nothing", () => {
    expect(has(TICKETS, "const sourceOrder = sources.map((s) => s.id);")).toBe(true);
    expect(has(TICKETS, '"no match"')).toBe(true);
    expect(has(TICKETS, "No ticket matches")).toBe(true);
  });

  it("every watched repository has a heading, whatever the filter", () => {
    expect(has(KIT, "export function repoGroupOrder<T>(repos: string[], byRepo: Map<string, T[]>): string[]")).toBe(true);
    for (const src of [PRS, ISSUES]) {
      expect(has(src, "repoGroupOrder(")).toBe(true);
      expect(has(src, '"no match"')).toBe(true);
    }
  });

  it("a broken source's heading says Fix, and its note opens the editor", () => {
    expect(has(KIT, '{editing ? "Done" : broken ? "Fix" : "Edit"}')).toBe(true);
    expect(has(TICKETS, "onDetail={() => onOpenEditor(src)}")).toBe(true);
  });

  it("a deep link names the source, and its editor opens", () => {
    expect(has(KIT, "export function useEditing(focus: string | undefined)")).toBe(true);
    for (const src of [TICKETS, PRS, ISSUES]) expect(has(src, "useEditing(focus)")).toBe(true);
  });

  it("Remove arms, then confirms — never a native confirm()", () => {
    expect(has(KIT, "export function ArmButton(")).toBe(true);
    expect(has(TICKETS, 'armedLabel={"Confirm — remove " + name}')).toBe(true);
    expect(has(REPOS, "<ArmButton")).toBe(true);
    for (const src of [TICKETS, REPOS, KIT]) expect(src).not.toMatch(/\bconfirm\(/);
  });

  it("Use-it-for ticks save every group in ONE request", () => {
    expect(has(SETTINGS, 'const r = await api<{ settings?: Json }>("/api/settings", { json: patches });')).toBe(true);
    expect(has(REPOS, "s.saveGroups(")).toBe(true);
    expect(REPOS).not.toContain("saveGroup(");
  });
});

describe("view, not settings", () => {
  it("whose tickets and which states the panel SHOWS are one View menu", () => {
    expect(has(TICKETS, '<ViewMenu id="tk-view"')).toBe(true);
    // The per-state ✕ and the two selects are gone from the toolbar/headings.
    expect(TICKETS).not.toContain('id="tk-bucket-add"');
    expect(TICKETS).not.toContain("onHide={() => hideBucket(b)}");
  });
});
