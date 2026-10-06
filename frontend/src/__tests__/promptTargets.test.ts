/** Customize → Prompts' target picker — see lib/promptTargets.ts.
 *
 * Saved prompts owns "put text into sessions that are already running"
 * (templates only start sessions), including the paste into ALL of them that
 * used to live in the templates modal. Also pins the ⋮ preview fix: the rect
 * is read in the click handler, never inside the setPop updater, where React
 * has already cleared e.currentTarget (it threw and blanked the app).
 */
import promptsSrc from "../components/dialogs/PromptsDialog.tsx?raw";
import { describe, it, expect } from "vitest";
import {
  ALL_RUNNING,
  isRunningTarget,
  pasteIntoAll,
  pastedAllToast,
  promptTargets,
  resolveTarget,
  runningTitles,
} from "../lib/promptTargets";

const name = (t: string) => "<" + t + ">";

describe("isRunningTarget", () => {
  it("is the templates filter: status running, or started", () => {
    expect(isRunningTarget({ title: "a", status: "running" })).toBe(true);
    expect(isRunningTarget({ title: "a", status: "ready", started: true })).toBe(true);
    expect(isRunningTarget({ title: "a", status: "ready", started: false })).toBe(false);
    expect(isRunningTarget({ status: "running" })).toBe(false);
    expect(isRunningTarget(null)).toBe(false);
  });
  it("leaves out a paused session even though it has started (no tmux to paste into)", () => {
    expect(isRunningTarget({ title: "a", status: "paused", started: true })).toBe(false);
  });
  it("leaves out verify sessions, which the rail hides", () => {
    expect(isRunningTarget({ title: "verify-plan-1", status: "running" })).toBe(false);
  });
});

describe("runningTitles", () => {
  it("orders by the rail, unlisted rows after in snapshot order", () => {
    const rows = [
      { title: "c", status: "running" },
      { title: "a", status: "running" },
      { title: "x", status: "paused", started: true },
      { title: "b", started: true },
    ];
    expect(runningTitles(rows, ["b", "a"])).toEqual(["b", "a", "c"]);
    expect(runningTitles(rows)).toEqual(["c", "a", "b"]);
    expect(runningTitles(undefined)).toEqual([]);
  });
});

describe("promptTargets", () => {
  it("puts the focused session first, then the others, then All (n)", () => {
    expect(promptTargets("b", ["a", "b", "c"], name)).toEqual([
      { value: "b", label: "<b>" },
      { value: "a", label: "<a>" },
      { value: "c", label: "<c>" },
      { value: ALL_RUNNING, label: "All running sessions (3)" },
    ]);
  });
  it("offers All only from two running sessions up", () => {
    expect(promptTargets("a", ["a"], name)).toEqual([{ value: "a", label: "<a>" }]);
    expect(promptTargets(null, [], name)).toEqual([]);
  });
  it("keeps a focused session that is not running (today's default)", () => {
    expect(promptTargets("p", ["a", "b"], name).map((o) => o.value)).toEqual([
      "p",
      "a",
      "b",
      ALL_RUNNING,
    ]);
  });
});

describe("resolveTarget", () => {
  const opts = promptTargets("a", ["a", "b"], name);
  it("defaults to the focused session", () => {
    expect(resolveTarget(null, "a", opts)).toBe("a");
  });
  it("keeps a pick while it is still offered", () => {
    expect(resolveTarget(ALL_RUNNING, "a", opts)).toBe(ALL_RUNNING);
    expect(resolveTarget("b", "a", opts)).toBe("b");
  });
  it("falls back when the picked session has gone", () => {
    expect(resolveTarget("gone", "a", opts)).toBe("a");
    expect(resolveTarget("gone", null, [])).toBe("");
  });
});

describe("pasteIntoAll", () => {
  it("pastes into each and collects partial failures without throwing", async () => {
    const sent: string[] = [];
    const out = await pasteIntoAll(["a", "b", "c"], async (t) => {
      if (t === "b") throw new Error("busy");
      sent.push(t);
    });
    expect(sent.sort()).toEqual(["a", "c"]);
    expect(out.ok).toEqual(["a", "c"]);
    expect(out.failed).toEqual([{ title: "b", error: "busy" }]);
  });
  it("says nothing has run yet", () => {
    expect(pastedAllToast(3)).toBe("Pasted into 3 sessions — press Enter in each to send");
    expect(pastedAllToast(1)).toMatch(/press Enter there/);
  });
});

describe("PromptsPanel source", () => {
  const src = promptsSrc as string;
  it("reads the ⋮ rect before setPop, never inside its updater", () => {
    const code = src.replace(/\/\/[^\n]*/g, "");
    const updaters = code.match(/setPop\(\(cur\)[\s\S]*?\);/g) || [];
    expect(updaters.length).toBeGreaterThan(0);
    for (const u of updaters) expect(u).not.toMatch(/currentTarget/);
    expect(code).toMatch(
      /const anchor = e\.currentTarget\.getBoundingClientRect\(\);\s*setPop\(/
    );
  });
  it("sends submit:false + dialog_safe to every target and keeps the picker's id", () => {
    expect(src).toContain('id="prompts-target"');
    expect(src.match(/"\/send"/g)?.length).toBe(1);
    expect(src).toContain("submit: false, dialog_safe: true");
    expect(src).toContain("pasteIntoAll(titles, send)");
    expect(src).toContain("errorPop(");
  });
  it("closes Customize only while it is still the open dialog", () => {
    expect(src).toContain('useUi.getState().openDialog === "prompts"');
    expect(src).not.toMatch(/\n\s*closeDialog\(\);/);
  });
});
