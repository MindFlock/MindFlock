/** Verify, round 5: an honest first screen, "Not written yet" apart from
 * "Failed", no checklist for a branch with nothing on it, Start an empty
 * checklist, Check now in sight, the verdict beside Open PR, and the five-step
 * tour's hooks. The rules are driven here; the wiring that only shows in the
 * source is pinned with `has`. */

import { describe, expect, it, vi } from "vitest";
import { code, css, has } from "./sourceText";
import {
  checklistTargets,
  dueCount,
  isConfigBlocked,
  isUnwritten,
  planGroup,
  planStatus,
  planVerdict,
} from "../components/dialogs/verify";
import type { Instance, TestPlan, TestStep } from "../api/types";
import type { SessionMenuModel } from "../lib/sessionMenu";

const confirmPop = vi.fn();
vi.mock("../lib/errorPop", () => ({ confirmPop: (...a: unknown[]) => confirmPop(...a), errorPop: vi.fn() }));
const { verdictNote, prVerdictConfirm, withVerifyVerdict } = await import("../lib/stage");

const step = (id: string, actor: "agent" | "human" = "agent"): TestStep => ({
  id,
  text: "do " + id,
  expect: "",
  actor,
});

const plan = (over: Partial<TestPlan> = {}): TestPlan =>
  ({
    id: "p1",
    title: "p1",
    repo_root: "/repo",
    branch: "feature/p1",
    sha: "a1",
    live_branch: "main",
    effective_live_branch: "main",
    state: "generated",
    error: "",
    generated_at: 10,
    gen_started: Date.now() / 1000,
    gen_attempts: 0,
    merged_at: 0,
    live_at: 0,
    live_problem: "",
    steps: [],
    runs: [],
    run_session: "",
    ...over,
  }) as TestPlan;

const failed = plan({
  state: "done",
  steps: [step("s1")],
  runs: [
    { at: 1, by: "human", session: "", verdict: "fail", results: { s1: { result: "fail", note: "", at: 1, by: "human" } } },
  ],
} as Partial<TestPlan>);

describe("a branch with nothing on it yet (M1)", () => {
  const inst = (title: string, files: number | null): Instance =>
    ({
      title,
      branch: "feature/" + title,
      repo: "app",
      diff_stat: files === null ? null : { files, additions: 0, deletions: 0 },
    }) as unknown as Instance;

  it("is listed, disabled, as \"no commits yet\" — and still offered an empty checklist", () => {
    const targets = checklistTargets([inst("fresh", 0), inst("worked", 2), inst("asleep", null)], [], [], "main");
    expect(targets.map((t) => [t.title, t.reason, !!t.noCommits])).toEqual([
      ["worked", "", false],
      // Unknown (a paused session has no diff_stat) is never guessed empty.
      ["asleep", "", false],
      ["fresh", "no commits yet", true],
    ]);
  });
});

describe("Not written yet is its own grey state (M4)", () => {
  it("is not red, not Failed, not on the badge", () => {
    const p = plan({ state: "failed", error: "the agent didn't answer in 3 minutes. Rewrite to try again" });
    expect(isUnwritten(p)).toBe(true);
    expect(planGroup(p)).toBe("failed");
    expect(planVerdict(p)).toMatchObject({ label: "Not written yet", short: "Not written", tone: "wait" });
    expect(dueCount([p])).toBe(0);
    // The headline stops at the reason; the Rewrite button says the rest.
    expect(planStatus(p).line).toBe("Not written yet — the agent didn't answer in 3 minutes.");
  });
});

describe("a checklist blocked on configuration waits on YOU (m10)", () => {
  it("files under Needs you and counts on the badge", () => {
    const p = plan({
      steps: [step("s1")],
      live_problem: "Origin has no branch called production, so this can never come due — set the live branch under Edit repositories.",
    });
    expect(isConfigBlocked(p)).toBe(true);
    expect(planGroup(p)).toBe("due");
    expect(dueCount([p])).toBe(1);
  });
});

describe("the verdict where you open a PR (M6)", () => {
  const menu = (): SessionMenuModel =>
    ({
      groups: [
        {
          id: "ship",
          label: "Ship",
          items: [
            { key: "pr", label: "Open PR", run: vi.fn() },
            { key: "checklist", label: "Checklist…", run: vi.fn() },
          ],
        },
        { id: "danger", label: "", items: [{ key: "delete", label: "Delete…", confirm: "Delete?", run: vi.fn() }] },
      ],
      more: [],
    }) as unknown as SessionMenuModel;

  it("rides on Open PR, and a failed checklist asks first", () => {
    expect(verdictNote(failed)).toBe("Checklist ✗ Failed");
    expect(prVerdictConfirm(failed)).toBe("Checklist failed — open the PR anyway?");
    const m = withVerifyVerdict(menu(), { title: "p1" }, failed);
    const ship = m.groups.find((g) => g.id === "ship")!;
    const pr = ship.items.find((i) => i.key === "pr")!;
    expect(pr.note).toBe("Checklist ✗ Failed");
    // Its own question card — never the menu's `confirm` strip, which is Delete's.
    expect(pr.confirm).toBeUndefined();
    pr.run();
    expect(confirmPop).toHaveBeenCalledWith(
      "Checklist failed — open the PR anyway?",
      expect.any(String),
      [
        expect.objectContaining({ label: "Show the checklist" }),
        expect.objectContaining({ label: "Open PR anyway", primary: true }),
      ],
    );
    expect(ship.items.find((i) => i.key === "checklist")!.label).toBe("Verify checklist…");
  });

  it("says nothing, and asks nothing, with no checklist", () => {
    const m = withVerifyVerdict(menu(), { title: "p1" }, null);
    const pr = m.groups[0].items[0];
    expect(pr.note).toBeUndefined();
    expect(prVerdictConfirm(null)).toBe("");
  });
});

describe("the dialog's wiring (round 5)", () => {
  const DIALOG = code("components/dialogs/VerifyDialog.tsx");
  const CHROME = code("components/dialogs/VerifyChrome.tsx");
  const PLAN = code("components/dialogs/VerifyPlan.tsx");
  const STEP = code("components/dialogs/VerifyStep.tsx");
  const ROW = code("components/sidebar/SidebarRow.tsx");
  const PANE = code("components/grid/Pane.tsx");

  it("never opens on the repository wizard by itself (B2)", () => {
    expect(has(DIALOG, "const showAdd = addingRepo;")).toBe(true);
    expect(has(DIALOG, "Watch a GitHub repository for every push")).toBe(true);
    expect(has(CHROME, "Verify writes a checklist for each branch your sessions change, then checks it.")).toBe(true);
    expect(has(CHROME, "Nothing to check yet")).toBe(true);
    expect(has(CHROME, 'openDialogFor("new-session")')).toBe(true);
  });

  it("keeps the legend and the tour replay in the ⓘ (m2, tour)", () => {
    expect(has(DIALOG, 'className="vf-legend-once"')).toBe(true);
    expect(has(DIALOG, 'replayTour("verify")')).toBe(true);
    expect(has(DIALOG, "Replay the Verify tour")).toBe(true);
    expect(has(DIALOG, "<TourButton tour=\"verify\" />")).toBe(true);
  });

  it("can start an empty checklist (M2)", () => {
    expect(has(CHROME, '"Start an empty checklist"')).toBe(true);
    expect(code("components/dialogs/verify/emptyChecklist.ts")).toContain("empty: true");
  });

  it("names the switch and the live branch plainly (m4, m7, m8)", () => {
    expect(has(CHROME, 'label="Write a checklist on every push"')).toBe(true);
    expect(has(CHROME, "if (!n) return null;")).toBe(true);
    expect(has(CHROME, "Live branch: {liveOf(r)}")).toBe(true);
    expect(has(CHROME, '"Close editor"')).toBe(true);
    expect(CHROME).not.toMatch(/ships \{liveOf/);
  });

  it("shows Check now beside the main button (M3)", () => {
    expect(has(PLAN, 'className="test-btn vf-checknow"')).toBe(true);
  });

  it("carries the tour's five hooks", () => {
    expect(has(ROW, 'data-tour="verify-chip"')).toBe(true);
    expect(has(STEP, 'data-tour="verify-badges"')).toBe(true);
    expect(has(STEP, 'data-tour="verify-answer"')).toBe(true);
    expect(has(PLAN, 'data-tour="verify-check"')).toBe(true);
    expect(has(PLAN, 'data-tour="verify-merge"')).toBe(true);
  });

  it("puts the verdict into the pane's SHIP menu, and truncates row names at the end", () => {
    expect(has(PANE, "withVerifyVerdict(")).toBe(true);
    expect(ROW).toMatch(/\(-\\d\+\|\\\(\\d\+\\\)\)\$\/\.test\(shown\) \? <MidName text=\{shown\} \/> : shown/);
  });

  it("stacks a step's receipt under its answers on a narrow window (m12)", () => {
    expect(css("components/dialogs/VerifyDialog.css")).toMatch(
      /\.vf-step-actions > \.vf-step-tail \{\s*grid-column: 1 \/ -1;/,
    );
  });
});
