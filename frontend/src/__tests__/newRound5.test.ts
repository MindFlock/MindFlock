/** UX round 5, the New dialog and the first minutes after Start (judge
 * r5-new): one primary that starts, a folder that is never made implicitly,
 * the non-git question as two radios, the folds, and the menu's honest SHIP
 * items. The dialog is read as text where a behaviour is only visible in its
 * source (vitest runs in node), like newSessionFast.test.ts. */

import { describe, expect, it } from "vitest";
import DIALOG from "../components/dialogs/NewSessionDialog.tsx?raw";
import DESCRIBE from "../components/dialogs/newSession/DescribeSection.tsx?raw";
import FOLDER from "../components/dialogs/newSession/FolderPicker.tsx?raw";
import AGENT from "../components/dialogs/newSession/AgentAccount.tsx?raw";
import WORKSPACE from "../components/dialogs/newSession/WorkspaceFold.tsx?raw";
import FOOTER from "../components/dialogs/newSession/FooterActions.tsx?raw";
import ROW from "../components/sidebar/SidebarRow.tsx?raw";
import { START_LABEL, advancedSummary, prNeedsGithubReason } from "../components/dialogs/newSession";
import { skipsPermissions } from "../components/dialogs/newSession/LaunchFold";
import { CLOSE_GUARD_MS, closeGuarded } from "../components/sidebar/SidebarRow";
import { FAST_TRACK_LEVELS, NOTHING_TO_PUSH, CONNECT_GITHUB_FOR_PRS, SKIP_PERMS_LABEL, SKIP_PERMS_TITLE } from "../lib/vocab";
import { PR_FALLBACK_HINT, closedToastText, deleteConfirmText } from "../lib/sessionActions";
import { nothingToShip, sessionMenu } from "../lib/sessionMenu";
import { actionCtx } from "../lib/sessionActionTable";
import { TOURS } from "../lib/tours";
import type { Instance } from "../api/types";

describe("one primary that starts (findings 2, 3; firsthour 3, 18)", () => {
  it("is called Start everywhere in New", () => {
    expect(START_LABEL).toBe("Start");
    expect(FOOTER).not.toContain('"Create"');
    expect(FOOTER.split("{START_LABEL}").length - 1).toBe(2);
  });

  it("names who is asked, and offers ONE recovery when the agent doesn't answer", () => {
    expect(DIALOG).toContain("agentLabel: agentName(canonAgent(program)),");
    expect(DESCRIBE).toContain('id="new-describe-wait"');
    expect(DESCRIBE.split('id="new-describe-fallback"').length - 1).toBe(1);
    expect(DIALOG).toContain("startInstead: startRaw,");
  });
});

describe("never create a folder implicitly (finding 1)", () => {
  it("reads a missing folder from the server's check", () => {
    expect(FOLDER).toContain("missing: r.exists === false,");
  });

  it("says No folder at …, blocks Start, and makes it only on Create this folder", () => {
    expect(FOLDER).toContain("No folder at {label}.");
    expect(FOLDER).toContain('id="new-create-folder"');
    expect(DIALOG).toContain('dapi<{ path: string }>("/api/mkdir", { json: { path: parent, name } })');
    // Both Starts are off while it is missing, and submit refuses it too.
    expect(FOOTER).toContain("(!runMode && !!startBlocked)");
    expect(FOOTER).toContain("describing || !!startBlocked");
    const submit = DIALOG.slice(DIALOG.indexOf("const submit = () => {"));
    expect(submit.slice(0, submit.indexOf("\n  };"))).toContain("if (startBlocked) {");
    // A plan's proposed folder asks the same question in the same words.
    expect(DIALOG).toContain("const askFolder = newFolderAsk && !newFolderOk ? newFolderAsk : folderMissing ? missingLabel : \"\";");
  });
});

describe("a folder with no git repo asks one question (finding 4)", () => {
  it("swaps Works in for two radios, recommended only where it can happen", () => {
    expect(WORKSPACE).toContain("export function PlainWorksIn(");
    expect(WORKSPACE).toContain("Make it a git repo first <span className=\"nf-rec\">recommended</span>");
    expect(WORKSPACE).toContain('title="Just run in the folder"');
    expect(DIALOG).toContain("plainFolder={plainFolder}");
    // Never "below" about a folded control.
    expect(WORKSPACE + DIALOG).not.toMatch(/” below/);
  });
});

describe("density (finding 6 and the can't-use-yet list)", () => {
  it("folds uninstalled agents behind More agents…, keeping the chosen one", () => {
    expect(AGENT).toContain("More agents… ({folded} not installed)");
    expect(AGENT).toContain("p.name !== program");
    expect(DIALOG).toContain("foldMissing");
  });

  it("More options holds Name, Fast-track and Save as template; the rest is under Advanced", () => {
    const more = DIALOG.slice(DIALOG.indexOf('id="new-advanced"'), DIALOG.indexOf('id="new-deep"'));
    expect(more).toContain('id="new-title"');
    expect(more).toContain('id="new-ft-row"');
    expect(more).toContain("<SaveTemplateRow");
    const deep = DIALOG.slice(DIALOG.indexOf('id="new-deep"'));
    for (const id of ['id="new-lane-form-ask"', 'id="new-split-row"', "<GitOptions", "<PlanFirstRow", "<LaunchFold"])
      expect(deep).toContain(id);
    expect(advancedSummary([["a", true], ["b", false], ["c", true]])).toBe("a, c");
  });

  it("greys Open PR / Merge without GitHub, with the menu tooltip's own sentence", () => {
    expect(prNeedsGithubReason(PR_FALLBACK_HINT)).toBe(PR_FALLBACK_HINT.split("\n")[0]);
    expect(prNeedsGithubReason(PR_FALLBACK_HINT)).toContain("MindFlock can’t open or merge the PR for you yet");
    expect(DIALOG).toContain('!prOk && (lane === "pr" || lane === "merge")');
  });

  it("labels the skip-permissions flags with what they do", () => {
    expect(SKIP_PERMS_LABEL).toBe("⚠ Skip permission prompts");
    expect(SKIP_PERMS_TITLE).toBe("The agent runs commands and edits files without asking you");
    expect(skipsPermissions("--verbose --dangerously-skip-permissions")).toBe(true);
    expect(skipsPermissions("--verbose")).toBe(false);
  });

  it("one Fast-track label list for every screen", () => {
    expect(FAST_TRACK_LEVELS.map((l) => l.label)).toEqual(["Off", "Commit", "Push", "Open PR", "Merge when green"]);
  });
});

describe("after Start (findings 8, 10, 12, 20)", () => {
  const caps = { git: true, tailscale: true, ticketing: true, github: true };
  const ship = (o: Partial<Instance>, c: Record<string, boolean> = caps) => {
    const inst = { title: "s", status: "ready", branch: "me/x", stage: "pr", ...o } as Instance;
    return sessionMenu(inst, actionCtx("s", { inst, caps: c as never })).groups.find((g) => g.id === "ship")!.items;
  };
  const zero = { files: 0, additions: 0, deletions: 0, uncommitted: null };

  it("greys Push / Open PR while there is nothing to ship", () => {
    expect(nothingToShip({ diff_stat: zero } as Instance)).toBe(true);
    expect(nothingToShip({ diff_stat: null } as Instance)).toBe(false);
    const items = ship({ diff_stat: zero });
    const push = items.find((i) => i.key === "push")!;
    expect(push.disabled).toBe(true);
    expect(push.note).toBe(NOTHING_TO_PUSH);
    expect(items.find((i) => i.key === "pr")!.disabled).toBe(true);
  });

  it("greys Open PR without GitHub, and leaves it alone with it", () => {
    const some = { files: 1, additions: 3, deletions: 0, uncommitted: null };
    const off = ship({ diff_stat: some }, { ...caps, github: false });
    expect(off.find((i) => i.key === "pr")!.note).toBe(CONNECT_GITHUB_FOR_PRS);
    expect(off.find((i) => i.key === "push")!.disabled).toBeFalsy();
    expect(ship({ diff_stat: some }).find((i) => i.key === "pr")!.disabled).toBeFalsy();
  });

  it("Close's toast carries its Undo; Delete says what it costs", () => {
    expect(closedToastText("fix-typo")).toBe("Closed “fix-typo”");
    expect(closedToastText("fix-typo", "2 still running")).toBe("Closed “fix-typo” · 2 still running");
    expect(deleteConfirmText({ branch: "me/fix-typo" })).toBe(
      "Deletes the folder and branch “me/fix-typo”, including uncommitted changes. This can't be undone — use Close to keep it."
    );
    expect(deleteConfirmText({ in_place: true })).toContain("Its folder and branch stay");
  });

  it("the row ✕ ignores a second click inside the guard", () => {
    expect(CLOSE_GUARD_MS).toBe(400);
    const t = 1_000_000;
    expect(closeGuarded(t)).toBe(false);
    expect(closeGuarded(t + 100)).toBe(true);
    expect(closeGuarded(t + CLOSE_GUARD_MS + 1)).toBe(false);
    expect(ROW).toContain("if (closeGuarded(Date.now())) return;");
  });

  it("the New dialog is a modal dialog, and the two session menus have distinct names", () => {
    expect(DIALOG.split('role="dialog"').length - 1).toBe(2);
    expect(DIALOG).toContain('aria-modal="true"');
    expect(ROW).toContain('aria-label={"Session menu for " + shown}');
  });
});

describe("the New tour (judge's four steps)", () => {
  it("task box → folder chip → Start → the pane's tabs and ⋯", () => {
    const t = TOURS.new.steps.map((s) => s.target);
    expect(t).toHaveLength(4);
    expect(t[0]).toContain('[data-tour="new-task"]');
    expect(t[1]).toContain('[data-tour="new-folder"]');
    expect(t[2]).toContain('[data-tour="new-start"]');
    expect(t[3]).toContain('[data-tour="pane-tabs"]');
    expect(t[3]).toContain('[data-tour="pane-menu"]');
    expect(DESCRIBE).toContain('data-tour="new-task"');
    expect(DESCRIBE).toContain('data-tour="new-folder"');
    expect(FOOTER).toContain('data-tour="new-start"');
  });
});
