/** The New dialog's "Describe it" page and the auto-split stepper, as wired.
 *
 * Moved from tests/unit/test_frontend_describe.py, which grepped the built
 * bundle. What is pinned: the input, the sentence that tells a first-time
 * reader what to type, the buttons — and the `type="button"` that keeps each
 * of them from creating a session out of whatever the form is holding (a
 * <button>'s default type is "submit", and these sit inside
 * <form id="new-form" onSubmit={submit}>). The workspace mode is a radio
 * group, page 1 has its three ways out, and the auto-split ships its stepper,
 * its planning copy and its "didn't split" toast.
 *
 * Behaviour that can run here runs (the lead line, the toast, the start
 * label); the JSX wiring and the CSS are pinned in the module that owns them. */

import { describe, expect, it, vi } from "vitest";
import { ALL_CODE, attr, cssRule, src, tagsWithId } from "./sourceText";

const DESCRIBE = src("components/dialogs/newSession/DescribeSection.tsx");
const FOOTER = src("components/dialogs/newSession/FooterActions.tsx");
const WORKSPACE = src("components/dialogs/newSession/WorkspaceFold.tsx");
const SPLIT = src("components/dialogs/SplitCheck.tsx");
const CSS = src("components/dialogs/NewSessionDialog.css");

/** The one element with this id in `source` (every copy, when the page draws
 * it in two layouts) — and there IS one. */
function tags(source: string, id: string) {
  const found = tagsWithId(source, id);
  expect(found.length, id).toBeGreaterThan(0);
  return found;
}

describe("the describe box", () => {
  it("is a strip with a textarea and the placeholder that teaches it", () => {
    // The strip itself, first child of .nf-body and above the Templates row.
    expect(tags(DESCRIBE, "new-describe").every((t) => t.name === "div")).toBe(true);
    // A textarea, so a longer task can be typed or pasted. However many lines,
    // it is ONE prompt: only Intake's "Start together…" opens New as a batch.
    const boxes = tags(DESCRIBE, "new-describe-text");
    expect(boxes.map((t) => t.name)).toEqual(boxes.map(() => "textarea"));
    // The placeholder is the whole tutorial: the only place that says a
    // sentence may name a folder AND ask for a worktree. Page 1's box carries
    // exactly this text.
    expect(boxes.map((t) => attr(t, "placeholder"))).toContain("e.g. fix the login bug in acme-api");
    // No list shape: typed lines never become a list.
    expect(ALL_CODE).not.toContain("one per line: PAY-412");
    expect(DESCRIBE).toContain('"What do you want to work on?"');
    expect(DESCRIBE).toContain('"Starting together — one session per line or ticket ID"');
  });

  it("has a Cancel that is a button, never a submit", () => {
    // Cancel only exists mid-turn, and it is the only way out of a slow CLI
    // start that does not involve closing the dialog.
    for (const t of tags(DESCRIBE, "new-describe-cancel")) {
      expect(t.name).toBe("button");
      expect(attr(t, "type")).toBe("button");
    }
  });

  it("is styled: the row, the box, the note and the error", () => {
    // Without these the row is a stacked input and a raw browser-chrome
    // button in a card that styles everything else.
    expect(cssRule(CSS, ".nf-describe-row")).not.toBe("");
    // min-width:0, or the input refuses to shrink and pushes the button off
    // the end of the card.
    expect(cssRule(CSS, "#new-describe-text")).not.toBe("");
    // The server's note and the inline failure, both asides under the row.
    expect(cssRule(CSS, ".nf-describe-note")).not.toBe("");
    expect(cssRule(CSS, "#new-describe-error")).not.toBe("");
  });
});

describe("the fill button", () => {
  it("is type=button, so it can never submit the form", () => {
    // Asserted as the pair: an id that survived a refactor which dropped the
    // type would pass a check for either one alone. Without it clicking
    // "Review details first" CREATES a session in whatever folder the
    // suggestion pre-fill happened to leave behind.
    for (const t of tags(FOOTER, "new-describe-go")) {
      expect(t.name).toBe("button");
      expect(attr(t, "type")).toBe("button");
    }
  });

  it("is labelled, and says it is still working on a slow start", () => {
    // A button with no label is not an affordance, and both busy labels ship:
    // the ring alone reads as a hang long before the server's 75s budget.
    // Round 5: the secondary "Let the agent pick the folder…"; the line under the
    // box says who is asked and the cap ("Asking Claude Code to fill in the
    // details… 0:01 (up to 30 s)").
    expect(FOOTER).toContain("Let the agent pick the folder…");
    expect(FOOTER).toContain('"Asking…"');
    expect(FOOTER).toContain('"Still asking…"');
  });
});

describe("the workspace mode is a choice with something always selected", () => {
  // A plan that picks a worktree must SHOW that it picked one: a cleared
  // checkbox cannot tell "the other mode" from "nobody decided", so the modes
  // are radios sharing one name — exactly one of them is always on.
  // Provisioning is the THIRD answer to the same question (the worktree answer
  // plus setup), not a checkbox beside it.
  // The row is WorksInGroup (New draws it as scope "new": #new-worktree and
  // #new-in-place in the radio group new-workspace-mode).
  it.each(['scope + "-worktree"', 'scope + "-in-place"'])("%s is a radio of the scope's workspace-mode", (id) => {
    const group = WORKSPACE.slice(WORKSPACE.indexOf("export function WorksInGroup("));
    const at = group.indexOf("id={" + id + "}");
    expect(at).toBeGreaterThan(0);
    const input = group.slice(group.lastIndexOf("<input", at), group.indexOf("/>", at));
    expect(input).toContain('type="radio"');
    expect(input).toContain('name={scope + "-workspace-mode"}');
  });
  it("New draws that row as scope new", () => {
    const worksIn = WORKSPACE.slice(WORKSPACE.indexOf("export function WorksIn("), WORKSPACE.indexOf("export interface WorksInGroupProps"));
    expect(worksIn).toContain("<WorksInGroup");
    expect(worksIn).toContain('scope="new"');
  });

  it("provisioning is a box on top of the separate worktree, not a third answer", () => {
    // Round 4: one ProvisionCheck, drawn by the New form AND the Templates
    // editor, so the two can't describe it differently.
    const [t] = tags(WORKSPACE, "new-provision");
    expect(t.name).toBe("ProvisionCheck");
    const check = WORKSPACE.slice(WORKSPACE.indexOf("export function ProvisionCheck("));
    expect(check.slice(0, check.indexOf("</label>"))).toContain('<input type="checkbox" id={id}');
    // Ticking it picks the worktree.
    expect(WORKSPACE).toContain("if (on) setInPlace(false);");
  });

  it("reads as one labelled question, and the radios are styled", () => {
    expect(WORKSPACE).toContain("Works in");
    expect(WORKSPACE).toContain("Separate worktree");
    expect(WORKSPACE).toContain("This folder directly");
    // The default is marked, and it is the default in the dialog's state.
    expect(WORKSPACE).toContain('<span className="nf-rec">recommended</span>');
    expect(cssRule(CSS, ".nf-mode")).not.toBe("");
    // Without a radio rule these render as raw browser chrome beside the
    // accent-styled checkbox directly under them.
    expect(CSS).toContain('input[type="radio"]');
  });
});

describe("the dialog is two pages with the sentence first", () => {
  // Page 1 ships three ways out — fill the form and check it, start the
  // session without looking, or skip the model entirely — plus Back to it.
  it.each(["new-describe-go", "new-describe-start", "new-describe-skip", "new-back"])("%s is a button", (id) => {
    for (const t of tags(FOOTER, id)) {
      expect(t.name).toBe("button");
      expect(attr(t, "type")).toBe("button");
    }
  });

  it("starts immediately from a BUTTON labelled Start (Enter does the same)", async () => {
    // Round 5: Start ↵ is page 1's one primary; a batch's still reads
    // "Start N sessions".
    const { startLabel } = await import("../lib/runStart");
    expect(startLabel(3)).toBe("Start 3 sessions");
    const start = tags(FOOTER, "new-describe-start")[0];
    expect(attr(start, "type")).toBe("button");
    expect(FOOTER).toContain("draft.listMode && draft.count >= 2 ? (");
    expect(FOOTER).toContain("startLabel(draft.count)");
    expect(FOOTER).toContain("{START_LABEL}");
  });
});

describe("auto-split: stepper, planning copy and the dissolved toast", () => {
  it("puts the N stepper OUTSIDE the checkbox's label", () => {
    // Clicking − or + must not toggle the box: the label's text ends, the
    // label closes, and only then does the stepper open.
    expect(SPLIT).toMatch(/Auto-split into up to\s*<\/label>\{" "\}[\s\S]*?<span className="rt-step nf-split-n"/);
    expect(SPLIT).toContain("disabled={!ok || maxPieces <= SPLIT_MIN}");
    expect(SPLIT).toContain("disabled={!ok || maxPieces >= limit}");
    // The stepper's CSS ships with it.
    expect(cssRule(CSS, "#new-form .nf-split .nf-split-n")).not.toBe("");
    expect(cssRule(CSS, "#new-form .nf-split .nf-split-n button")).not.toBe("");
  });

  it("an optional run's lead is deciding, not proposing", async () => {
    const { leadLine } = await import("../lib/splitRun");
    const run = (optional: boolean) =>
      ({ id: "r", state: "planning", optional, tasks: [] }) as unknown as Parameters<typeof leadLine>[0];
    expect(leadLine(run(true))?.text).toBe("deciding whether to split…");
    expect(leadLine(run(false))?.text).toBe("proposing the pieces…");
  });
});

describe("the dissolved toast", () => {
  it("toasts on a live run.changed → dissolved, never on replay or another state", async () => {
    vi.resetModules();
    const toast = vi.fn();
    vi.doMock("../lib/toast", () => ({ toast }));
    vi.doMock("react", async (orig) => {
      const R = (await orig()) as typeof import("react");
      // The bridge is mounted from an effect: run it now.
      return { ...R, useEffect: (f: () => void) => void f() };
    });
    vi.doMock("@tanstack/react-query", async (orig) => ({
      ...((await orig()) as object),
      useQuery: () => ({}),
    }));
    const subs: Record<string, Array<(env: unknown) => void>> = {};
    const replayed = new Set<unknown>();
    const g = globalThis as Record<string, unknown>;
    const hadWindow = "window" in g;
    const oldWindow = g.window;
    g.window = {
      mindflock: {
        events: {
          subscribe: (name: string, fn: (env: unknown) => void) => (subs[name] ||= []).push(fn),
          isReplay: (env: unknown) => replayed.has(env),
        },
      },
    };
    try {
      const { useRuns, dissolvedText } = await import("../state/runs");
      useRuns();
      const fire = (env: unknown) => (subs["run.changed"] || []).forEach((f) => f(env));
      fire({ data: { state: "planning", lead: "lead-a" } });
      expect(toast).not.toHaveBeenCalled();
      const old = { data: { state: "dissolved", lead: "lead-a" } };
      replayed.add(old);
      fire(old);
      expect(toast).not.toHaveBeenCalled();
      fire({ data: { state: "dissolved", lead: "lead-a" } });
      expect(toast).toHaveBeenCalledTimes(1);
      expect(toast.mock.calls[0][0]).toBe("lead-a didn't split it — it's doing the task itself");
      expect(dissolvedText("lead-a", "")).toBe(toast.mock.calls[0][0]);
    } finally {
      if (hadWindow) g.window = oldWindow;
      else delete g.window;
      vi.doUnmock("../lib/toast");
      vi.doUnmock("react");
      vi.doUnmock("@tanstack/react-query");
      vi.resetModules();
    }
  });
});
