/** Frontend wave-2/3 wiring (A4, C2, D2/D5, E1–E3, F3/F4/F5/F8): the markup
 * hooks, event subscriptions and CSS rules those waves introduced, pinned on
 * the module that owns each one.
 *
 * Moved from tests/unit/test_frontend_wave2.py, which grepped the built
 * app.js/style.css. What stays there: the served index.html, core/events.js
 * (hand-written, not bundled) and the API routes the UI calls (a
 * backend↔frontend contract). Source helpers: sourceText.ts. */

import { describe, expect, it } from "vitest";
import { code, codeFilesWith, css, cssFilesWith } from "./sourceText";

describe("A4 — slots.js is loaded by app.js after first paint", () => {
  it("main.tsx injects /core/slots.js", () => {
    expect(code("main.tsx")).toContain("/core/slots.js");
  });
});

describe("wave-2 markup hooks", () => {
  it("E1 — the connection-lost banner", () => {
    expect(code("components/ConnBanner.tsx")).toContain('"conn-banner"');
  });

  it("C2 — the setup dialog (reopened via the doctor chip + palette)", () => {
    expect(code("components/dialogs/SetupDialog.tsx")).toContain('"setup-dialog"');
  });

  it("E3 — settings test buttons and the per-source ticketing list", () => {
    expect(code("components/settings/screens/CodingCli.tsx")).toContain('"agent-test-btn"');
    const tickets = code("components/intake/TicketsTab.tsx");
    expect(tickets).toContain('"ticketing-sources"');
    expect(tickets).toContain('"ticketing-add"');
  });

  it("the Doctor screen in Settings", () => {
    expect(code("components/settings/SettingsDialog.tsx")).toContain('"doctor"');
    expect(code("components/settings/screens/Doctor.tsx")).toContain('"settings-doctor"');
  });

  it("D2 — the IDE picker: select + custom-command escape hatch", () => {
    const ide = code("components/settings/screens/Ide.tsx");
    expect(ide).toContain('"ide-select"');
    expect(ide).toContain('"ide-custom-row"');
    // still persisted via the generic data-group/data-field settings path
    expect(ide).toContain('"ide_command"');
  });
});

describe("wave-2 JS wiring", () => {
  it("activity folds into one phase chip with client-side debounce", () => {
    const stage = code("lib/stage.ts");
    expect(stage).toContain("effectiveActivity");
    expect(stage).toContain("s-running");
    // usage-limit gets its own (red) pill, distinct from clarify
    expect(stage).toContain("s-limit");
    expect(stage).toContain("noteActivity");
    expect(stage).toContain("forceActivity");
  });

  it("E2 — event-driven notifications + tab-title badge", () => {
    const toasts = code("components/EventToasts.tsx");
    expect(toasts).toContain("session.activity_changed");
    expect(toasts).toContain("session.stage_changed");
    expect(toasts).toContain("updateTitleBadge");
    expect(code("lib/titleBadge.ts")).toContain("updateTitleBadge");
  });

  it("C2 — the doctor-backed first-run checklist", () => {
    const model = code("components/setup/setupModel.ts");
    expect(model).toContain("Pick a repo");
    expect(model).toContain("Sign in to your agent");
  });

  it("E1 + events bus feed: the poll feeds sessions() to window.mindflock", () => {
    expect(code("state/queries.ts")).toContain("__setSessions");
    expect(code("components/ConnBanner.tsx")).toContain("conn-banner");
  });
});

describe("wave-2 CSS rules", () => {
  it("phase chips, banner, setup card, doctor checks, test results", () => {
    const diff = css("components/grid/DiffTab.css");
    expect(diff).toContain(".stagechip.s-running");
    expect(diff).toContain(".stagechip.s-limit");
    expect(css("components/ConnBanner.css")).toContain("#conn-banner");
    const setup = css("components/dialogs/SetupDialog.css");
    expect(setup).toContain(".setup-card");
    expect(setup).toContain(".doctor-check");
    expect(setup).toContain(".test-result");
  });
});

describe("wave 3", () => {
  it("F8 — the dismissible doctor warning chip", () => {
    const side = code("components/sidebar/Sidebar.tsx");
    expect(side).toContain('"doctor-warn"');
    expect(side).toContain('"doctor-warn-open"');
    expect(side).toContain('"doctor-warn-dismiss"');
  });

  it("F3 — toast is published on the public extension API", () => {
    expect(code("lib/toast.ts")).toContain(".toast = toast");
  });

  it("F4 — merged-PR detection keys off the PR's OWN state", () => {
    // Via session.pr_state_changed. It must NOT key off the stage leaving
    // "pr" — the stage ladder does that on every edit under an open PR, which
    // toasted "merged or closed" (then "PR open" again) for a PR that never moved.
    const toasts = code("components/EventToasts.tsx");
    expect(toasts).toContain('ev.subscribe("session.pr_state_changed"');
    expect(toasts).toContain('env.old === "OPEN"');
    expect(codeFilesWith('env.old === "pr"')).toEqual([]);
    expect(codeFilesWith('env.new === "merged"')).toEqual([]);
  });

  it("F5 retired: neither the managed-repo label nor the foreign-repo ⇄ chip remain", () => {
    // Keyed on the DOM id F5 deleted, not on the API field `repo_root`: Verify
    // legitimately renders a test plan's repo_root in its plan rows.
    expect(codeFilesWith("repo-root")).toEqual([]);
    expect(codeFilesWith("foreign-chip")).toEqual([]);
  });

  it("F8 — doctor warning chip + inline Shortcut token input", () => {
    expect(code("components/sidebar/Sidebar.tsx")).toContain("doctor-warn");
    expect(code("components/setup/SetupChecklist.tsx")).toContain("setup-shortcut-token");
  });

  it("wave-3 CSS rules", () => {
    expect(css("components/sidebar/Sidebar.css")).toContain("#doctor-warn");
    expect(cssFilesWith(".setup-shortcut-token")).toEqual(["components/palette/CommandPalette.css"]);
  });
});
