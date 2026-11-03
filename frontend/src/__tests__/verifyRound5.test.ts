/** Verify, round 5's rules that outlived the Verify dialog: "Not written yet"
 * apart from "Failed", no behaviors for a branch with nothing on it, a config
 * block waits on you, and the verdict beside Open PR (the session menu's
 * "Behaviors" item flies to the Sky — docs/behaviors.md §4). */

import { describe, expect, it, vi } from "vitest";
import { code, has } from "./sourceText";
import {
  checklistTargets,
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
const openBehaviors = vi.fn();
vi.mock("../lib/behaviorsFocus", () => ({ openBehaviors: (...a: unknown[]) => openBehaviors(...a) }));
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
  it("is not red, not Failed", () => {
    const p = plan({ state: "failed", error: "the agent didn't answer in 3 minutes. Rewrite to try again" });
    expect(isUnwritten(p)).toBe(true);
    expect(planGroup(p)).toBe("failed");
    expect(planVerdict(p)).toMatchObject({ label: "Not written yet", short: "Not written", tone: "wait" });
    // The headline stops at the reason; the Rewrite button says the rest.
    expect(planStatus(p).line).toBe("Not written yet — the agent didn't answer in 3 minutes.");
  });
});

describe("a checklist blocked on configuration waits on YOU (m10)", () => {
  it("files under Needs you", () => {
    const p = plan({
      steps: [step("s1")],
      live_problem: "Origin has no branch called production, so this can never come due — set the live branch under Edit repositories.",
    });
    expect(isConfigBlocked(p)).toBe(true);
    expect(planGroup(p)).toBe("due");
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

  it("rides on Open PR, and a failed behavior asks first", () => {
    expect(verdictNote(failed)).toBe("Behaviors ✗ Failed");
    expect(prVerdictConfirm(failed)).toBe("A behavior failed — open the PR anyway?");
    const m = withVerifyVerdict(menu(), { title: "p1" }, failed);
    const ship = m.groups.find((g) => g.id === "ship")!;
    const pr = ship.items.find((i) => i.key === "pr")!;
    expect(pr.note).toBe("Behaviors ✗ Failed");
    // Its own question card — never the menu's `confirm` strip, which is Delete's.
    expect(pr.confirm).toBeUndefined();
    pr.run();
    expect(confirmPop).toHaveBeenCalledWith(
      "A behavior failed — open the PR anyway?",
      expect.any(String),
      [
        expect.objectContaining({ label: "Show the behaviors" }),
        expect.objectContaining({ label: "Open PR anyway", primary: true }),
      ],
    );
    const item = ship.items.find((i) => i.key === "checklist")!;
    expect(item.label).toBe("Behaviors");
    // It flies the Sky to the behaviors on the route — no Verify dialog.
    openBehaviors.mockReset();
    void item.run();
    expect(openBehaviors).toHaveBeenCalledWith({ planId: "p1", title: "p1", branch: "feature/p1" });
    const show = confirmPop.mock.calls.at(-1)![2][0] as { run(): void };
    openBehaviors.mockReset();
    show.run();
    expect(openBehaviors).toHaveBeenCalledWith({ planId: "p1", title: "p1", branch: "feature/p1" });
  });

  it("says nothing, and asks nothing, with no checklist", () => {
    const m = withVerifyVerdict(menu(), { title: "p1" }, null);
    const pr = m.groups[0].items[0];
    expect(pr.note).toBeUndefined();
    expect(prVerdictConfirm(null)).toBe("");
  });
});

describe("the wiring that outlived the dialog", () => {
  const ROW = code("components/sidebar/SidebarRow.tsx");
  const PANE = code("components/grid/Pane.tsx");

  it("puts the verdict into the pane's SHIP menu, and truncates row names at the end", () => {
    expect(has(PANE, "withVerifyVerdict(")).toBe(true);
    expect(ROW).toMatch(/\(-\\d\+\|\\\(\\d\+\\\)\)\$\/\.test\(shown\) \? <MidName text=\{shown\} \/> : shown/);
  });

  it("no Verify dialog is left to open", () => {
    expect(() => code("components/dialogs/VerifyDialog.tsx")).toThrow();
    expect(() => code("components/dialogs/VerifyPlan.tsx")).toThrow();
    expect(() => code("components/dialogs/VerifyStep.tsx")).toThrow();
    expect(() => code("components/dialogs/VerifyChrome.tsx")).toThrow();
  });
});
