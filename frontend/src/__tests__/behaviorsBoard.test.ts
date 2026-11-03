/** The Behaviors board (docs/behaviors.md §3, docs/behaviors/board.md): its
 * rows (the behaviors with no session frame in the Sky), grouped by tone;
 * when it is on the Sky; how an `openBehaviors` request is taken (the
 * session's frame, else the board with that row open); its look, drawn
 * server-side; and the few lines that wire it into SkyView and the drawer.
 *
 * `behaviorSummary` is stubbed to read the tone from the fixture (its body is
 * vs-card's), so these pin the board's rules, not the verdict wording. */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { code } from "./sourceText";
import type { Instance, TestPlan } from "../api/types";
import type { BehaviorSummary, BehaviorTone } from "../lib/behaviors";

vi.mock("../api/client", async () => ({
  ...(await vi.importActual<typeof import("../api/client")>("../api/client")),
  api: vi.fn(() => new Promise(() => {})),
  instApi: vi.fn(() => new Promise(() => {})),
}));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));
vi.mock("../lib/behaviors", async () => {
  const real = await vi.importActual<typeof import("../lib/behaviors")>("../lib/behaviors");
  const SHORT: Record<string, string> = { you: "Needs you · 1", bad: "Failed · 1", busy: "Checking…", quiet: "Not checked", ok: "2 of 2 verified" };
  return {
    ...real,
    // The fixture's `summary` names its tone.
    behaviorSummary: (p: TestPlan): BehaviorSummary => {
      const tone = (p.summary || "quiet") as BehaviorTone;
      return { tone, status: tone, short: SHORT[tone], sentence: "", action: null, total: 2, verified: 0, failed: 0, yours: 0 };
    },
  };
});

const B = await import("../components/flights/BehaviorsBoard");
const O = await import("../lib/skyObjects");
const { useBehaviorsFocus, openBehaviors } = await import("../lib/behaviorsFocus");
const { queryClient } = await import("../state/queries");

const NOW = 1_800_000_000;
const plan = (id: string, tone: BehaviorTone, over: Partial<TestPlan> = {}): TestPlan =>
  ({ id, title: id, summary: tone, branch: "", repo_root: "/r/app", state: tone === "you" ? "due" : "done", steps: [], runs: [], ...over }) as unknown as TestPlan;
const verified = (id: string, at: number) => plan(id, "ok", { runs: [{ at } as TestPlan["runs"][number]] });
const row = (title: string, over: Partial<Instance> = {}): Instance => ({ title, branch: "", ...over }) as Instance;

beforeEach(() => {
  useBehaviorsFocus.setState({ request: null, openPlan: "", boardRequested: false });
});

describe("the rows, grouped by tone", () => {
  it("Needs you · Failed · Checking · Not checked · Verified today, in that order, empty groups left out", () => {
    const plans = [plan("q", "quiet"), plan("ok", "ok"), plan("y1", "you"), plan("bad", "bad"), plan("busy", "busy"), plan("y2", "you")].map((p) =>
      p.id === "ok" ? verified("ok", NOW - 60) : p
    );
    const g = B.boardGroups(plans, NOW);
    expect(g.map((x) => x.label)).toEqual(["Needs you", "Failed", "Checking", "Not checked", "Verified today"]);
    expect(g[0].plans.map((p) => p.id)).toEqual(["y1", "y2"]);
    expect(B.boardGroups([plan("bad", "bad")], NOW).map((x) => x.label)).toEqual(["Failed"]);
    expect(B.boardGroups([], NOW)).toEqual([]);
  });

  it("verified behaviors stay a day after their last check, then leave the board", () => {
    expect(B.lastCheckedAt(verified("a", 100))).toBe(100);
    expect(B.lastCheckedAt(plan("b", "ok"))).toBe(0);
    const g = B.boardGroups([verified("fresh", NOW - 3600), verified("old", NOW - B.VERIFIED_TODAY_S - 1), plan("never", "ok")], NOW);
    expect(g).toHaveLength(1);
    expect(g[0].plans.map((p) => p.id)).toEqual(["fresh"]);
  });

  it("needs you and failed are urgent; the rest are not", () => {
    expect((["you", "bad", "busy", "quiet", "ok"] as BehaviorTone[]).map((t) => B.isUrgent(plan("x", t)))).toEqual([true, true, false, false, false]);
  });
});

describe("when the board is on the Sky", () => {
  const s = (over: Partial<Parameters<typeof B.boardShown>[0]> = {}) => B.boardShown({ allowed: true, pref: false, requested: false, urgent: [], dismissed: [], ...over });

  it("hidden by default; turned on, asked for, or holding a row that needs you or failed shows it", () => {
    expect(s()).toBe(false);
    expect(s({ pref: true })).toBe(true);
    expect(s({ requested: true })).toBe(true);
    expect(s({ urgent: ["a"] })).toBe(true);
  });

  it("its × puts the urgent rows away until a new one comes", () => {
    expect(s({ urgent: ["a"], dismissed: ["a"] })).toBe(false);
    expect(s({ urgent: ["a", "b"], dismissed: ["a"] })).toBe(true);
  });

  it("never on a phone or in This session, whatever asks", () => {
    expect(s({ allowed: false, pref: true, requested: true, urgent: ["a"] })).toBe(false);
  });

  /** The hook's visibility and rows, drawn once (no effects run server-side). */
  const probe = (plans: TestPlan[], rows: Instance[], titles: string[], allowed = true) => {
    queryClient.setQueryData(["test-plans"], { plans, live_branch: "main" });
    let got: ReturnType<typeof B.useBehaviorsBoard> | null = null;
    const Probe = () => {
      got = B.useBehaviorsBoard({ rows, titles, allowed });
      return null;
    };
    renderToStaticMarkup(createElement(QueryClientProvider, { client: queryClient }, createElement(Probe)));
    return got!;
  };

  it("the rows are the behaviors with no frame here; a worker drawn in its lead's line has one", () => {
    const plans = [plan("lead", "quiet"), plan("worker", "quiet"), plan("closed", "quiet")];
    const rows = [row("lead"), row("worker", { parent: "lead" } as Partial<Instance>)];
    const b = probe(plans, rows, ["lead"]);
    expect(b.rows.map((p) => p.id)).toEqual(["closed"]);
    expect(b.lineOfTitle("worker")).toBe("lead");
    expect(b.lineOfTitle("closed")).toBe("");
    expect(b.lineForPlan(plans[1])).toBe("lead");
    expect(b.lineForPlan(plans[2])).toBe("");
    // Nothing urgent, nothing asked: hidden.
    expect(b.on).toBe(false);
  });

  it("an orphan that needs you brings it out; one on a frame does not", () => {
    expect(probe([plan("closed", "you")], [], []).on).toBe(true);
    expect(probe([plan("open", "you")], [row("open")], ["open"]).on).toBe(false);
    expect(probe([plan("closed", "bad")], [], [], false).on).toBe(false);
  });

  it("the per-device switch shows it (a storage that throws reads as off); a request's boardRequested too", () => {
    const store: Record<string, string> = {};
    vi.stubGlobal("localStorage", { getItem: (k: string) => store[k] ?? null, setItem: (k: string, v: string) => void (store[k] = v), removeItem: (k: string) => void delete store[k] });
    try {
      expect(probe([], [], []).on).toBe(false);
      store[B.BEHAVIORS_PREF] = "1";
      expect(probe([], [], []).on).toBe(true);
      expect(probe([], [], [], false).on).toBe(false);
      vi.stubGlobal("localStorage", { getItem: () => { throw new Error("blocked"); } });
      expect(probe([], [], []).on).toBe(false);
    } finally {
      vi.unstubAllGlobals();
    }
    const src = code("components/flights/BehaviorsBoard.tsx");
    expect(src).toContain('export const BEHAVIORS_PREF = "mf_sky_behaviors";');
    // The store's flag is read live (server-side rendering sees only its initial state).
    expect(src).toContain("const requested = useBehaviorsFocus((s) => s.boardRequested);");
    expect(src).toContain("const on = boardShown({ allowed, pref, requested, urgent, dismissed });");
    // ×: off, the request cleared, the urgent rows put away.
    expect(src).toMatch(/const hide = useCallback\(\(\) => \{\s*setPref\(false\);\s*setDismissed\(urgent\);\s*setExpanded\(""\);\s*useBehaviorsFocus\.getState\(\)\.setBoardRequested\(false\);/);
  });
});

describe("a request (openBehaviors)", () => {
  const plans = [plan("fix-login", "quiet", { branch: "fix-login" }), plan("gone", "bad", { branch: "old-branch" }), plan("asks", "you")];
  const lines: Record<string, string> = { "fix-login": "fix-login" };
  const lineForPlan = (p: TestPlan) => lines[p.id] || "";
  const lineOfTitle = (t: string) => (t === "fix-login" || t === "no-plan-yet" ? t : "");
  const aim = (t: Parameters<typeof B.aimFor>[0], list = plans) => B.aimFor(t, list, lineForPlan, lineOfTitle);

  it("a plan with a frame: the frame (and its card); without one: the board, that row open", () => {
    expect(aim({ planId: "fix-login" })).toEqual({ to: "line", line: "fix-login", plan: plans[0] });
    expect(aim({ planId: "gone" })).toEqual({ to: "board", expand: "gone" });
    expect(aim({ branch: "old-branch" })).toEqual({ to: "board", expand: "gone" });
    expect(aim({ title: "fix-login" })).toMatchObject({ to: "line", line: "fix-login" });
  });

  it("a session with a frame but no behaviors yet: its frame, no card; nothing known: the board", () => {
    expect(aim({ title: "no-plan-yet" })).toEqual({ to: "line", line: "no-plan-yet", plan: null });
    expect(aim({ planId: "nope" })).toEqual({ to: "board", expand: "" });
    expect(aim({ board: true })).toEqual({ to: "board", expand: "" });
  });

  it("next: the next behaviors that need you, else the board", () => {
    expect(aim({ next: true })).toMatchObject({ to: "board", expand: "asks" });
    expect(aim({ next: true }, [plans[0]])).toEqual({ to: "board", expand: "" });
  });

  it("taking one: the card opens in the tile (openPlan) or the board shows with the row open, and the request is gone", () => {
    const show = vi.fn();
    const board = { plans, lineForPlan, lineOfTitle, show };
    useBehaviorsFocus.setState({ request: { planId: "fix-login", n: 1 } });
    expect(B.applyRequest(useBehaviorsFocus.getState().request!, board)).toEqual({ id: "fix-login", reveal: '.sky-line-slot[data-slot="fix-login"] [data-plan="fix-login"]' });
    expect(useBehaviorsFocus.getState().openPlan).toBe("fix-login");
    expect(useBehaviorsFocus.getState().request).toBeNull();
    expect(show).not.toHaveBeenCalled();
    expect(B.applyRequest({ planId: "gone", n: 2 }, board)).toEqual({ id: B.BEHAVIORS_KEY, reveal: '.sky-bh [data-bh-row="gone"]' });
    expect(show).toHaveBeenCalledWith("gone");
    expect(B.applyRequest({ board: true, n: 3 }, board)).toEqual({ id: "behaviors", reveal: "" });
  });

  it("openBehaviors from anywhere leaves a request in the store for the Sky to take when it mounts", () => {
    openBehaviors({ planId: "gone" });
    expect(useBehaviorsFocus.getState().request).toMatchObject({ planId: "gone" });
    openBehaviors({ board: true });
    expect(useBehaviorsFocus.getState().boardRequested).toBe(true);
  });

  it("the hook waits for the plans and a placed Sky, flies (retrying a few seconds), then reveals the open card", () => {
    const src = code("components/flights/BehaviorsBoard.tsx");
    expect(src).toContain("if (!request || !board.loaded || !ready) return;");
    expect(src).toContain("setAim({ ...applyRequest(request, boardRef.current), at: Date.now() });");
    expect(src).toContain("if (Date.now() - aim.at > FLY_GIVE_UP_MS) setAim(null);");
    expect(src).toContain("new Event(TOUR_REVEAL, { bubbles: true, cancelable: true })");
  });
});

describe("the board, drawn", () => {
  const html = (over: Partial<Parameters<typeof B.BehaviorsBoard>[0]> = {}) =>
    renderToStaticMarkup(
      createElement(QueryClientProvider, { client: queryClient }, createElement(B.BehaviorsBoard, { plans: [], expanded: "", onExpand() {}, now: NOW, ...over }))
    );

  it("the calm look: a title bar to drag by, the ⋯ settings, the ×, what it is for", () => {
    const h = html({ onHide() {}, onHeadDown() {} });
    expect(h).toContain('class="sky-bh"');
    expect(h).toContain('aria-label="Behaviors"');
    expect(h).toContain("data-sky-head");
    expect(h).toContain('aria-label="Behaviors settings"');
    expect(h).toContain('aria-label="Hide the Behaviors board"');
    expect(h).toContain(B.BEHAVIORS_EXPLAIN.replace("'", "&#x27;"));
    expect(h).toContain("Nothing here");
    expect(h).not.toMatch(/checklist|test plan|\bstep/i);
  });

  it("rows by group: the name, the short words; the urgent count on the bar; an open row draws the card", () => {
    const plans = [plan("closed-session", "you", { title: "Fix login" }), plan("other", "bad", { branch: "feat/x" }), plan("idle", "quiet")];
    const h = html({ plans, expanded: "other" });
    expect(h.indexOf("Needs you")).toBeLessThan(h.indexOf("Failed"));
    expect(h.indexOf("Failed")).toBeLessThan(h.indexOf("Not checked"));
    expect(h).toContain("Fix login");
    expect(h).toContain("Needs you · 1");
    expect(h).toContain("Failed · 1");
    expect(h).toContain('<span class="sky-bh-count" title="2 need you or failed">2</span>');
    expect(h).toContain('data-bh-row="other"');
    expect(h).toContain("feat/x");
    expect(h).toMatch(/data-bh-row="other"[\s\S]*aria-expanded="true"[\s\S]*class="sky-bh-card"[\s\S]*data-plan="other"/);
    expect(h).not.toContain('data-plan="idle"');
  });

  it("the registry draws it from the view's behaviors (rows, open row, ×)", () => {
    const def = O.SKY_OBJECTS.find((d) => d.kind === "behaviors")!;
    const hide = vi.fn();
    const node = def.render({
      key: "behaviors",
      zoom: 1,
      lod: "near",
      layoutEntry: undefined,
      pos: { x: 0, y: 0 },
      draggable: true,
      selected: false,
      dragging: false,
      target: false,
      onHeadDown() {},
      onResize() {},
      onMeasure() {},
      onLayout() {},
      view: { behaviors: { rows: [plan("orphan", "bad")], expanded: "", setExpanded() {}, hide } } as unknown as Parameters<typeof def.render>[0]["view"],
    });
    const h = renderToStaticMarkup(createElement(QueryClientProvider, { client: queryClient }, node));
    expect(h).toContain('data-bh-row="orphan"');
    expect((node as { props: { onHide(): void } }).props.onHide).toBe(hide);
  });
});

describe("the wiring", () => {
  it("SkyView: the board's hook by the lines, the request by the camera, the ⋯ toggle, the view's data", () => {
    const v = code("components/flights/SkyView.tsx");
    expect(v).toContain('const behaviors = useBehaviorsBoard({ rows, titles, allowed: !narrow && mode !== "one" });');
    expect(v).toContain("({ mode, narrow, intakeOn, paletteOn, links, devices, notes, behaviorsOn })");
    expect(v).toContain("useBehaviorsRequest(behaviors, { ready: narrow || (!!vp.w && !!vp.h && instQ.data !== undefined && settled), fly: flyToBehaviors });");
    expect(v).toContain("goTo(centerOn(r, vp.w, vp.h, Math.max(camRef.current.z, 0.85)));");
    expect(v).toContain('label: (behaviors.on ? "✓ " : "") + "Show the Behaviors board",');
    expect(v).toContain("onSelect: behaviors.toggle,");
    expect(v).toContain("behaviors: { rows: behaviors.rows, expanded: behaviors.expanded, setExpanded: behaviors.setExpanded, hide: behaviors.hide },");
  });

  it("the drawer's Needs-you rows fly to the behaviors, not the old Verify dialog", () => {
    const d = code("components/flights/SkyDrawer.tsx");
    expect(d).toContain("onOpen={(planId) => openBehaviors({ planId })}");
    expect(d).not.toContain('openDialogFor("verify"');
    expect(d).toContain('"Show the behaviors that need you"');
    expect(d).not.toContain("Open this checklist in Verify");
    expect(d).toContain("Waiting items and behaviors still work.");
  });
});
