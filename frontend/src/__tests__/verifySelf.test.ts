/** Verify is the session's own agent (owner request 2026-11-10): "Verify"
 * has it write its own actionable steps and work them in its terminal, and
 * tell the person whatever only a person can do; when there are such steps, a
 * person confirms verification is complete. Pinned here:
 *
 *  - each `verify.state`'s pill (label, tone, in the bell, the hint) and its
 *    rank against working, idle and the other needs-you states;
 *  - a row with no `verify` (an older server) reads exactly as before;
 *  - Confirm verified / Not verified… and Verify call their routes;
 *  - the Behaviors card shows the person's steps and the confirm buttons
 *    when it waits on them, and Verify as its one action otherwise;
 *  - the pane's verify row, the bell's line and the ⋯ menu's Verify.
 *
 * vitest is node-only: models and helpers, and renderToStaticMarkup. */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Instance, SessionVerify, TestPlan } from "../api/types";
import { code } from "./sourceText";

const api = vi.fn();
const toast = vi.fn();
const selected: string[] = [];
vi.mock("../api/client", async (orig) => ({ ...((await orig()) as object), api: (...a: unknown[]) => api(...a) }));
vi.mock("../lib/toast", () => ({ toast: (...a: unknown[]) => toast(...a) }));
vi.mock("../lib/errorPop", () => ({ errorPop: vi.fn() }));
vi.mock("../lib/sessionActions", async (orig) => ({
  ...((await orig()) as object),
  selectSession: (t: string) => selected.push(t),
}));
let instances: Instance[] = [];
vi.mock("../state/queries", async (orig) => ({
  ...((await orig()) as object),
  useInstances: () => ({ data: instances }),
  useFlights: () => ({ data: [] }),
  refreshInstances: () => Promise.resolve(),
  refreshTestPlans: () => Promise.resolve(),
}));

const S = await import("../lib/sessionState");
const { deriveSessionState, needsYouAction, notifyRuleFor, attentionPriority } = S;
const { confirmVerified, requestVerifyConfirm, requestSessionVerify, startSessionVerify } = await import("../lib/verifyActions");
const { BehaviorsCard } = await import("../components/behaviors/BehaviorsCard");
const { VerifyConfirm } = await import("../components/behaviors/VerifyConfirm");
const { verifyCardView } = await import("../components/behaviors/cardModel");
const { PaneVerifyStrip } = await import("../components/grid/PaneVerifyStrip");
const { attentionItems } = await import("../components/sidebar/ordering");
const { canStartVerify, withVerifyItem } = await import("../components/sidebar/verifyMenu");
const { stateChip } = await import("../lib/stage");
const { behaviorSummary } = await import("../lib/behaviors");
const { useUi } = await import("../state/store");

const NOW = 1_800_000_000_000;
const verify = (over: Partial<SessionVerify> = {}): SessionVerify => ({
  state: "verifying",
  plan: "sc-1",
  steps: { total: 6, agent_done: 3, human: 1 },
  human_steps: [],
  failed_steps: [],
  detail: "",
  at: 1,
  ...over,
});
const HUMAN = [
  { id: "h1", text: "Open the app on a phone", expect: "the banner fits" },
  { id: "h2", text: "Sign in with the QA login", expect: "" },
];
const FAILED = [{ id: "s2", text: "GET /api/thing", note: "500 with a stack trace" }];
const row = (o: Partial<Instance> = {}): Partial<Instance> => ({ title: "sc-1", status: "running", activity: "idle", branch: "feature/sc-1", repo: "shop", ...o });
const ss = (o: Partial<Instance>, activity = (o.activity as string) || "idle") => deriveSessionState(row(o), { now: NOW, activity });

beforeEach(() => {
  api.mockReset();
  toast.mockReset();
  selected.length = 0;
  instances = [];
});

describe("the verify pill", () => {
  it("verifying: its own tone, not in the bell, how far it is in the hint", () => {
    const s = ss({ verify: verify() });
    expect(s).toMatchObject({ state: "working", label: "Verifying", tone: "verifying", inBell: false });
    expect(s.hint).toBe("Checking its work: 3 of 5 steps done · 1 step needs a person after");
    // Mid-turn or between its steps, the same pill.
    expect(ss({ verify: verify() }, "working").label).toBe("Verifying");
    expect(S.verifyProgress(verify({ steps: { total: 0, agent_done: 0, human: 0 } }))).toBe("writing its steps");
    expect(S.verifyProgress(verify({ steps: { total: 3, agent_done: 9, human: 0 } }))).toBe("3 of 3 steps done");
  });

  it("needs_you: Confirm verified, in the bell, the hint lists the person's steps", () => {
    const s = ss({ verify: verify({ state: "needs_you", human_steps: HUMAN, steps: { total: 5, agent_done: 3, human: 2 } }) });
    expect(s).toMatchObject({ state: "needs_you", reason: "verify_confirm", label: "Confirm verified", tone: "attention", inBell: true });
    expect(s.hint).toContain("• Open the app on a phone — the banner fits");
    expect(s.hint).toContain("• Sign in with the QA login");
    expect(s.hint).toContain("Confirm verified");
    expect(needsYouAction(s)).toMatchObject({ act: "verify" });
    expect(notifyRuleFor(s)).toBe("verify_needs_you");
    // Never priority 0: the bell draws a question's answer strip for those.
    expect(attentionPriority(s)).toBe(1);
  });

  it("failed: Verify failed, in the bell, in the broken tone, the hint lists what failed", () => {
    const s = ss({ verify: verify({ state: "failed", failed_steps: FAILED }) });
    expect(s).toMatchObject({ state: "needs_you", reason: "verify_failed", label: "Verify failed", tone: "broken", inBell: true });
    expect(s.hint).toContain("• GET /api/thing — 500 with a stack trace");
    expect(needsYouAction(s)?.act).toBe("verify");
  });

  it("blocked: Verify blocked, with why", () => {
    const s = ss({ verify: verify({ state: "blocked", detail: "no browser on this machine" }) });
    expect(s).toMatchObject({ state: "needs_you", reason: "verify_blocked", label: "Verify blocked", inBell: true });
    expect(s.hint).toContain("no browser on this machine");
  });

  it("passed: a calm Verified, out of the bell", () => {
    const s = ss({ verify: verify({ state: "passed" }) });
    expect(s).toMatchObject({ state: "idle", label: "Verified", tone: "verified", inBell: false });
    // The rail chip shows it (not the ship stage an Idle row would show).
    expect(stateChip(s)).toMatchObject({ label: "Verified" });
    expect(stateChip(s).cls).toContain("tone-verified");
    expect(stateChip(ss({ verify: verify() })).cls).toContain("tone-verifying");
  });

  it("ranks: a dialog question, a usage limit and a dead terminal win; a working agent outranks a finished verify", () => {
    const v = verify({ state: "needs_you", human_steps: HUMAN });
    expect(ss({ verify: v }, "clarify").reason).toBe("question");
    expect(ss({ verify: verify() }, "limit").state).toBe("limit");
    expect(ss({ verify: verify() }, "offline").state).toBe("offline");
    expect(ss({ verify: v }, "working").label).toBe("Working");
    expect(ss({ verify: verify({ state: "failed" }) }, "working").label).toBe("Working");
    expect(ss({ verify: verify({ state: "passed" }) }, "working").label).toBe("Working");
    // Its reply IS the person's steps: Confirm verified, not "Question".
    const asking = { kind: "question" as const, text: "Can you check it on a phone?", ts: 1, dialog_id: null, parsed: false, source: null };
    expect(ss({ verify: v, asking }).label).toBe("Confirm verified");
    // Ahead of a stuck idle, and of failing checks.
    expect(ss({ verify: v, check: { state: "failed" } as Instance["check"] }).label).toBe("Confirm verified");
  });

  it("a row with no verify (an older server, or null) reads exactly as before", () => {
    for (const v of [undefined, null]) {
      expect(ss({ verify: v })).toEqual(ss({}));
      expect(ss({ verify: v }, "working")).toEqual(ss({}, "working"));
      expect(ss({ verify: v, check: { state: "failed" } as Instance["check"] })).toEqual(ss({ check: { state: "failed" } as Instance["check"] }));
    }
    expect(ss({}).label).toBe("Idle");
    expect(ss({}, "working").label).toBe("Working");
    expect(S.verifyOf({ verify: { nope: 1 } } as unknown as Instance)).toBeNull();
  });
});

describe("the requests", () => {
  it("Confirm verified posts {verified: true} to the plan's confirm route", async () => {
    api.mockResolvedValue({ ok: true, plan: "sc-1" });
    expect(await confirmVerified("sc-1", true)).toBe(true);
    expect(api).toHaveBeenCalledWith("/api/test-plans/sc-1/confirm", { method: "POST", json: { verified: true } });
    expect(toast).toHaveBeenCalledWith("Confirmed verified");
  });

  it("Not verified… posts {verified: false, note}; an empty note is left out", async () => {
    api.mockResolvedValue({ ok: true, plan: "sc-1" });
    await confirmVerified("sc-1", false, "  the banner overlaps the menu ");
    expect(api).toHaveBeenLastCalledWith("/api/test-plans/sc-1/confirm", { method: "POST", json: { verified: false, note: "the banner overlaps the menu" } });
    await requestVerifyConfirm("a/b", false);
    expect(api).toHaveBeenLastCalledWith("/api/test-plans/a%2Fb/confirm", { method: "POST", json: { verified: false } });
  });

  it("a 409 (not waiting any more) resolves false", async () => {
    api.mockRejectedValue(new Error("plan is not waiting"));
    expect(await confirmVerified("sc-1", true)).toBe(false);
  });

  it("Verify posts to the session's verify route and opens its Agent tab", async () => {
    api.mockResolvedValue({ started: true, plan: "sc-1" });
    expect(await startSessionVerify("sc-1")).toBe(true);
    expect(api).toHaveBeenCalledWith("/api/instances/sc-1/verify", { method: "POST", json: {} });
    expect(selected).toEqual(["sc-1"]);
    expect(useUi.getState().lastTab?.["sc-1"]).toBe("agent");
    await requestSessionVerify("sc-1", " the coupon flow ");
    expect(api).toHaveBeenLastCalledWith("/api/instances/sc-1/verify", { method: "POST", json: { notes: "the coupon flow" } });
  });
});

const plan = (over: Partial<TestPlan> = {}): TestPlan =>
  ({
    id: "sc-1",
    title: "sc-1",
    repo_root: "/r/shop",
    branch: "feature/sc-1",
    sha: "a1b2c3d4",
    live_branch: "main",
    effective_live_branch: "main",
    state: "generated",
    error: "",
    live_at: 0,
    merged_at: 0,
    steps: [
      { id: "s1", text: "Run the unit tests", expect: "all green", actor: "agent" },
      { id: "h1", text: "Open the app on a phone", expect: "the banner fits", actor: "human" },
    ],
    runs: [],
    run_session: "",
    ...over,
  }) as TestPlan;
const visible = (html: string) => html.replace(/<[^>]*>/g, " ").replace(/&[a-z#0-9]+;/g, " ").replace(/\s+/g, " ");
const card = (p: TestPlan) => renderToStaticMarkup(createElement(BehaviorsCard, { plan: p }));

describe("the Verify card", () => {
  it("needs_you: the server's line, the person's step listed ONCE (in the rows), Confirm verified / Not verified…", () => {
    instances = [row({ verify: verify({ state: "needs_you", human_steps: HUMAN.slice(0, 1), detail: "1 step needs you — check it, then confirm." }) }) as Instance];
    const html = card(plan());
    expect(html).toContain('<span class="bh-status" data-tone="you">Confirm verified</span>');
    expect(visible(html)).toContain("1 step needs you — check it, then confirm.");
    // VerifyConfirm is passed hideSteps: the rows below already list the step.
    expect(html).not.toContain("vc-steps");
    expect(visible(html).split("Open the app on a phone")).toHaveLength(2);
    expect(html).not.toContain("bh-signoff");
    expect(html).toMatch(/class="btn-primary vc-yes"[^>]*>Confirm verified</);
    expect(html).toMatch(/class="test-btn vc-no"[^>]*>Not verified…</);
    // Nothing to start while it waits on you.
    expect(html).not.toContain("bh-primary");
  });

  it("a sign-off with NO person steps: the server's line and Sign off — never \"steps need a person\"", () => {
    instances = [row({ verify: verify({ state: "needs_you", steps: { total: 1, agent_done: 1, human: 0 }, human_steps: [], detail: "Every step passed — confirm it works." }) }) as Instance];
    const html = card(plan({ steps: [{ id: "s1", text: "Run the unit tests", expect: "all green", actor: "agent" }] }));
    expect(visible(html)).toContain("Every step passed — confirm it works.");
    expect(html).toContain('<div class="bh-signoff">Sign off</div>');
    expect(visible(html)).not.toMatch(/need a person|needs a person/);
    expect(html).toMatch(/class="btn-primary vc-yes"[^>]*>Sign off</);
  });

  it("no verify: Verify is its one action (the session's own agent)", () => {
    instances = [row() as Instance];
    const html = card(plan());
    expect(html).toMatch(/class="btn-primary bh-primary" data-action="verify"[^>]*>Verify</);
    expect(html).not.toContain("vc-confirm");
  });

  it("verifying: Open its agent; failed: what failed and Let its agent fix it", () => {
    instances = [row({ verify: verify() }) as Instance];
    let html = card(plan());
    expect(html).toContain('data-tone="busy">Verifying</span>');
    expect(html).toMatch(/data-action="open"[^>]*>Open its agent</);
    instances = [row({ verify: verify({ state: "failed", failed_steps: FAILED, detail: "1 step failed" }) }) as Instance];
    html = card(plan());
    expect(html).toContain('data-tone="bad">Verify failed</span>');
    expect(visible(html)).toContain("GET /api/thing 500 with a stack trace");
    expect(html).toMatch(/data-action="agentfix"[^>]*>Let its agent fix it</);
  });

  it("the view: with the session gone, Verify still offered (the older run route)", () => {
    const p = plan();
    expect(verifyCardView(p, behaviorSummary(p), null, "").action).toMatchObject({ key: "verify", label: "Verify" });
    expect(verifyCardView(plan({ state: "running" }), behaviorSummary(plan({ state: "running" })), null, "sc-1").action).toBeNull();
  });

  it("VerifyConfirm alone: the steps, then the two buttons", () => {
    const html = renderToStaticMarkup(createElement(VerifyConfirm, { verify: { plan: "sc-1", human_steps: HUMAN } }));
    expect(html.indexOf("vc-steps")).toBeLessThan(html.indexOf("vc-yes"));
    expect(renderToStaticMarkup(createElement(VerifyConfirm, { verify: { plan: "sc-1", human_steps: HUMAN }, hideSteps: true }))).not.toContain("vc-steps");
  });
});

describe("the pane's verify row", () => {
  const strip = (v: SessionVerify | null) => renderToStaticMarkup(createElement(PaneVerifyStrip, { inst: { title: "sc-1", verify: v } }));

  it("verifying: one line, how far it is, and a way to its agent", () => {
    const html = strip(verify());
    expect(visible(html)).toContain("Verifying — 3 of 5 steps done");
    expect(html).toContain(">Open its agent</button>");
    expect(html).not.toContain("vc-confirm");
  });

  it("needs_you: the person's steps with Confirm verified", () => {
    const html = strip(verify({ state: "needs_you", human_steps: HUMAN }));
    expect(html).toContain('class="pv-strip is-needs_you"');
    expect(html).toContain(">Confirm verified</button>");
    expect(html).toContain(">Not verified…</button>");
  });

  it("failed: what failed and Verify again; passed: one line; none: nothing", () => {
    const html = strip(verify({ state: "failed", failed_steps: FAILED }));
    expect(visible(html)).toContain("Verify failed — 1 step");
    expect(html).toContain(">Verify again</button>");
    // Flow polish: passed says so on the row for its ten minutes.
    expect(visible(strip(verify({ state: "passed" })))).toContain("All 6 steps passed");
    expect(strip(null)).toBe("");
  });
});

describe("the bell and the ⋯ menu", () => {
  it("the bell lists Confirm verified with the person's steps under it", () => {
    const items = attentionItems([row({ verify: verify({ state: "needs_you", human_steps: HUMAN }) }) as Instance]);
    expect(items).toEqual([
      { p: 1, title: "sc-1", reason: "Confirm verified", snippet: "Open the app on a phone · Sign in with the QA login", question: undefined },
    ]);
    expect(attentionItems([row({ verify: verify({ state: "failed", failed_steps: FAILED }) }) as Instance])[0]).toMatchObject({ reason: "Verify failed", snippet: "GET /api/thing" });
    expect(attentionItems([row({ verify: verify() }) as Instance])).toEqual([]);
  });

  it("the bell's row carries the steps with Confirm verified / Not verified…, its name opening its Verify steps", () => {
    const bell = code("components/NotificationsBell.tsx");
    expect(bell).toContain('if (v?.state === "needs_you")');
    expect(bell).toContain("<VerifyConfirm verify={v} />");
    // Not a role="button" row (its buttons would also jump): the name does —
    // to the session's Verify steps, where the chip and the Ship menu go.
    expect(bell).toMatch(/className="attn-title linklike" title="See its Verify steps" onClick=\{\(\) => toSteps\(it\.title\)\}/);
    expect(bell).toContain("openBehaviors({ planId: v?.plan || title, title });");
  });

  it("Verify sits before Behaviors… in Ship — Stop verifying while it runs, nothing while it waits", () => {
    const model = { groups: [{ id: "ship" as const, label: "Ship", items: [{ key: "push", label: "Push", run() {} }, { key: "checklist", label: "Behaviors…", run() {} }] }], more: [] };
    const keys = (inst: Partial<Instance>) => withVerifyItem(model, inst).groups[0].items.map((i) => i.label);
    expect(keys(row())).toEqual(["Push", "Verify", "Behaviors…"]);
    expect(keys(row({ verify: verify({ state: "failed" }) }))).toEqual(["Push", "Verify again", "Behaviors…"]);
    expect(keys(row({ verify: verify() }))).toEqual(["Push", "Stop verifying", "Behaviors…"]);
    expect(keys(row({ verify: verify({ state: "needs_you" }) }))).toEqual(["Push", "Behaviors…"]);
    expect(canStartVerify(row({ device: "mac-mini" } as Partial<Instance>))).toBe(false);
    expect(canStartVerify(row({ workspace_missing: true }))).toBe(false);
  });
});
