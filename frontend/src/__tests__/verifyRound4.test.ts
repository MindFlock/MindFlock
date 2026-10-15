/** Verify, round 4: the verdict leaves the dialog, the list says one thing per
 * row, and the words mean one thing each. The rules themselves (groups,
 * verdicts, the badge) are driven in testPlans.test.ts; this pins the wiring
 * that only shows in the source. */

import { describe, expect, it } from "vitest";
import { code, css, has } from "./sourceText";

const ROW = code("components/sidebar/SidebarRow.tsx");
const PANE = code("components/grid/Pane.tsx");
const CHIP = code("components/dialogs/VerdictChip.tsx");
const PLAN = code("components/dialogs/VerifyPlan.tsx");
const DIALOG = code("components/dialogs/VerifyDialog.tsx");
const SOURCES = code("components/intake/RepoSources.tsx");
const CSS = css("components/dialogs/VerifyDialog.css");

describe("the verdict where merges happen", () => {
  it("is on the session's sidebar row and its pane head", () => {
    expect(has(ROW, '<VerdictChip inst={inst} where="row" />')).toBe(true);
    expect(has(PANE, '<VerdictChip inst={inst} where="pane" />')).toBe(true);
  });

  it("opens Verify on that branch", () => {
    expect(has(CHIP, 'openDialogFor("verify", branch)')).toBe(true);
  });

  it("is never a .stagechip — a pane head hides those", () => {
    expect(CHIP).not.toMatch(/stagechip/);
    expect(CSS).toMatch(/\.vchip\s*\{/);
  });
});

describe("a checklist row", () => {
  it("carries the verdict and a way back to the work", () => {
    expect(has(PLAN, "<VerdictBadge plan={plan} />")).toBe(true);
    // m9: the ⋯ says which session it means — the work, or the checking run.
    for (const label of ['"Open the work session"', '"View diff"', '"Open PR \\u2197"', '"Open the checking run"'])
      expect(has(PLAN, label)).toBe(true);
  });

  it("never calls a pushed branch local", () => {
    expect(PLAN).not.toMatch(/"local"/);
    expect(has(PLAN, '"not merged"')).toBe(true);
  });

  it("fills its button only when the row is asking for you", () => {
    expect(has(PLAN, '(status.tone === "you" ? "btn-primary" : "test-btn") + " vf-primary"')).toBe(
      true,
    );
  });

  it("offers Fix in a new session on a failed row, at normal button weight (m3)", () => {
    expect(has(PLAN, 'className="vf-fix"')).toBe(true);
    expect(has(PLAN, '"Fix in a new session"')).toBe(true);
    expect(PLAN).not.toContain('"Fix…"');
  });

  it("teaches the legend once, under the list — not in every expanded row", () => {
    expect(PLAN).not.toMatch(/vf-legend/);
    expect(has(DIALOG, 'className="vf-legend-once"')).toBe(true);
  });

  it("drops the header's repeat of the two moments", () => {
    expect(DIALOG).not.toMatch(/className="vf-live"/);
  });
});

describe("the repository form", () => {
  it("warns before you add a repository when GitHub isn't connected", () => {
    // The add flow shows the GitHub line with its real state, up front…
    expect(has(SOURCES, '<GithubStatusLine needFor="a repository can\'t be read without it" />')).toBe(true);
    // …and Verify's watched repositories say so under their line.
    const chrome = code("components/dialogs/VerifyChrome.tsx");
    expect(has(chrome, "useGithubConnected(")).toBe(true);
    expect(has(chrome, "These repositories need GitHub connected")).toBe(true);
  });

  it("speaks the glossary — no plan, no flock, no 'goes due'", () => {
    const strings = SOURCES.match(/"[^"\n]*"|>[^<>{}\n]+</g) || [];
    for (const s of strings) expect(s).not.toMatch(/\bplan\b|\bflock\b|goes due|THE knob/i);
  });
});
