/** Verify, round 4: the verdict leaves the dialog, the list says one thing per
 * row, and the words mean one thing each. The rules themselves (groups,
 * verdicts, the badge) are driven in testPlans.test.ts; this pins the wiring
 * that only shows in the source. */

import { describe, expect, it } from "vitest";
import { code, css, has } from "./sourceText";

const ROW = code("components/sidebar/SidebarRow.tsx");
const PANE = code("components/grid/Pane.tsx");
const CHIP = code("components/dialogs/VerdictChip.tsx");
const SOURCES = code("components/intake/RepoSources.tsx");
const CSS = css("components/dialogs/VerifyDialog.css");

describe("the verdict where merges happen", () => {
  it("is on the session's sidebar row and its pane head", () => {
    expect(has(ROW, '<VerdictChip inst={inst} where="row" />')).toBe(true);
    expect(has(PANE, '<VerdictChip inst={inst} where="pane" />')).toBe(true);
  });

  it("flies the Sky to the session's behaviors — Verify is a stop on the route, not a place", () => {
    expect(has(CHIP, "openBehaviors({ planId: plan.id, title: inst.title, branch });")).toBe(true);
    expect(CHIP).not.toContain('openDialogFor("verify"');
  });

  it("is never a .stagechip — a pane head hides those", () => {
    expect(CHIP).not.toMatch(/stagechip/);
    expect(CSS).toMatch(/\.vchip\s*\{/);
  });
});

describe("the repository form", () => {
  it("warns before you add a repository when GitHub isn't connected", () => {
    // The add flow shows the GitHub line with its real state, up front…
    expect(has(SOURCES, '<GithubStatusLine needFor="a repository can\'t be read without it" />')).toBe(true);
    // …and Verify's watched repositories say so under their line.
    const chrome = code("components/behaviors/WatchedRepositories.tsx");
    expect(has(chrome, "useGithubConnected(")).toBe(true);
    expect(has(chrome, "These repositories need GitHub connected")).toBe(true);
  });

  it("speaks the glossary — no plan, no flock, no 'goes due'", () => {
    const strings = SOURCES.match(/"[^"\n]*"|>[^<>{}\n]+</g) || [];
    for (const s of strings) expect(s).not.toMatch(/\bplan\b|\bflock\b|goes due|THE knob/i);
  });
});
