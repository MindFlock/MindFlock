/** UI polish round 3 (sidebar addon bars, session-row chips, provisioning copy,
 * pane-header shrink order, the ingestion dot, draggable booting panes): the
 * CSS rules and markup hooks those fixes introduced, pinned on the module that
 * owns each one.
 *
 * Moved from tests/unit/test_frontend_polish.py, which grepped the built
 * app.js/style.css. core/slots.js, addons/notify.js (hand-written, served
 * as-is), the served index.html and GET /api/config stay there. Source
 * helpers: sourceText.ts. */

import { describe, expect, it } from "vitest";
import { code, codeFilesWith, css, cssFilesWith, cssRules, squash } from "./sourceText";

describe("generic addon bars (core/slots.js) are styled like the bespoke bars", () => {
  it("bar, label and toggle rules — active, hover and a disabled affordance", () => {
    const bars = css("components/sidebar/toolbars.css");
    for (const sel of [
      "#addon-bars .addon-bar",
      "#addon-bars .addon-label",
      "#addon-bars .addon-toggle",
      "#addon-bars .addon-toggle.active",
      "#addon-bars .addon-toggle:disabled",
    ]) {
      expect(bars, sel).toContain(sel);
    }
  });
});

describe("the notify toggle lives only in Settings → Notifications", () => {
  it("the bell's duplicate toggle is gone; Settings drives the addon and stays in sync", () => {
    // The bell is the list of what needs you, not a second home for the knob.
    expect(codeFilesWith('"notif-toggle"')).toEqual([]);
    const screen = code("components/settings/screens/Notifications.tsx");
    expect(screen).toContain("mindflockAddons"); // _notifApi() drives the addon
    expect(screen).toContain("mf-notify-state"); // stays in sync with the addon
  });
});

describe("session rows: title / stage chip never collide", () => {
  it("the title keeps a legible floor and the chip ellipsizes in its own box", () => {
    expect(css("components/sidebar/SidebarRow.css")).toContain("min-width: 34px");
    expect(css("components/grid/DiffTab.css")).toContain("max-width: 96px");
  });

  it("the 'from another repo' ⇄ chip is gone; truncated titles still reveal the full name", () => {
    expect(codeFilesWith('">⇄</span>')).toEqual([]);
    expect(codeFilesWith(">⇄</span>")).toEqual([]);
    expect(codeFilesWith("From another repo: ")).toEqual([]);
    // Alias-aware hover: an aliased row shows "alias · real-title".
    expect(code("components/grid/Pane.tsx")).toContain('(displayName !== title ? title + "  ·  " : "")');
  });
});

describe("provisioning fields: current names, repo-neutral copy, no sitecheck-era spellings", () => {
  it("reads and sends only the current field names", () => {
    expect(code("components/dialogs/NewSessionDialog.tsx")).toContain("provisioning_available");
    const submit = code("lib/newSessionSubmit.ts");
    expect(submit).toContain("body.provisioned = true");
    expect(submit).toContain("body.workspace_strategy");
    expect(code("components/dialogs/RecentDialog.tsx")).toContain("e.provisioned"); // recently-closed badge
    for (const old of ["sitecheck_available", "sitecheck_mode", "body.sitecheck", "inst.sitecheck", "e.sitecheck"]) {
      expect(codeFilesWith(old), old).toEqual([]);
    }
  });

  it("no code mentions sitecheck at all", () => {
    expect(codeFilesWith("sitecheck")).toEqual([]);
  });

  it("the New-session dialog's provision copy and ids", () => {
    const fold = code("components/dialogs/newSession/WorkspaceFold.tsx");
    for (const id of ['"new-provision-row"', '"new-provision"', '"provision-opts"', '"new-workspace-strategy"']) {
      expect(fold, id).toContain(id);
    }
    const text = squash(fold);
    expect(text).toContain("Provision the worktree");
    expect(text).toContain("run the repo's setup");
    expect(text).toContain("warm test caches");
    expect(text).toContain("Workspace strategy");
    expect(text).toContain("shared base clone (worktree)");
    expect(text).toContain("full clone");
    expect(css("components/dialogs/NewSessionDialog.css")).toContain("#provision-opts");
    expect(cssFilesWith("#sitecheck-opts")).toEqual([]);
  });

  it("the unused-worktree sweep's copy is repo-neutral", () => {
    expect(code("components/dialogs/newSession/WorkspaceFold.tsx")).toContain("shared base clone");
    // It names kinds of directory, never a repo or a cache.
    expect(code("components/dialogs/recentRows.ts")).toContain("never a repository");
    expect(codeFilesWith("testmon refresher")).toEqual([]);
  });

  it("the provisioning placeholder hint is repo-neutral", () => {
    expect(code("components/grid/Pane.tsx")).toContain("The first provisioned run clones the base repo");
    expect(codeFilesWith("sitecheck-bot")).toEqual([]);
  });
});

describe("pane header: a long title costs its OWN characters, never the controls", () => {
  it("the title is the shrink target and every .actions rule refuses to shrink", () => {
    // A 60-character session name used to slice the Commit button, the
    // fast-track toggle and the live-step indicator off the right edge,
    // because `.actions` was made shrinkable. The title is a LABEL whose full
    // text is one hover away; those are CONTROLS.
    const titles = cssRules(".pane-head .title");
    expect(titles.length).toBeGreaterThan(0);
    for (const t of titles) {
      // factor 3 against the context line's 1; may truncate hard
      expect(t).toContain("flex: 0 3 auto");
      expect(t).toContain("text-overflow: ellipsis");
    }
    // EVERY .pane-head .actions rule, in every stylesheet, must refuse to
    // shrink; one that yields is what clipped the controls.
    const actions = cssRules(".pane-head .actions");
    expect(actions.length).toBeGreaterThan(0);
    for (const b of actions) {
      expect(b).toContain("flex: none");
      expect(b).not.toContain("flex: 0 1 auto");
    }
    // Its container can no longer shrink, so the step label bounds itself.
    const step = cssRules(".pane-head .actions .stepnow-text");
    expect(step.length).toBeGreaterThan(0);
    expect(step[0]).toContain("max-width: 15ch");
  });
});

describe("the ingestion dot cannot be deformed by the global .error rule", () => {
  it("uses a scoped dc-error state and pins its own box on every automation bar", () => {
    // The red state used to carry a bare `error` class; `.error` sets
    // min-height: 16px for error TEXT and rendered a 9x16 oval.
    expect(code("components/sidebar/AutomationBar.tsx")).toContain('"dc-error"');
    expect(code("components/sidebar/VerifyBar.tsx")).toContain('"dc-error"');
    expect(codeFilesWith('"dc-dot " + (netIssue ? "error"')).toEqual([]);
    expect(codeFilesWith('(netIssue ? "error"')).toEqual([]);
    const bars = css("components/sidebar/toolbars.css");
    expect(bars).toContain(".dc-dot.dc-error");
    // Spelled out in full: ALL the bars share the one rule that pins the box,
    // and a fuzzy match would pass while a new bar's dot went unpinned.
    const dot = cssRules(
      "#mindflock-bar .dc-dot,\n#pr-review-bar .dc-dot,\n#git-issue-bar .dc-dot,\n#verify-bar .dc-dot"
    );
    expect(dot.length).toBeGreaterThan(0);
    for (const decl of ["min-height: 0", "min-width: 0", "border-radius: 50%"]) {
      expect(dot[0], decl).toContain(decl);
    }
  });
});

describe("a booting window can be dragged", () => {
  it("the loading pane renders the grip and the dims-while-dragging class", () => {
    // Provisioning is the LONGEST a window is ever in one state; the drag is
    // pure layout and touches nothing about the session.
    const pane = code("components/grid/Pane.tsx");
    expect(pane).toContain("loading-pane");
    expect(pane).toContain('className={"pane loading-pane" + (dragging ? " dragging" : "")}');
    const loading = pane.slice(pane.indexOf('"pane loading-pane"'));
    expect(loading.slice(0, loading.indexOf("</section>"))).toContain("Drag to move this window");
  });
});
