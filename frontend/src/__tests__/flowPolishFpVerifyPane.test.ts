/** Flow polish (fp-verify-pane): starting, following and answering a Verify
 * from the pane and the row are each one obvious step.
 *
 *  1. Start form: what it should prove + where, in place; the queued toast.
 *  2. While it runs: "Step 4 of 5: … · quiet 3 min", queued, the file error,
 *     Steps / Stop verifying, and the stop toast that keeps the session.
 *  3. Failed: "Let its agent fix it" in the session, older-server fallback.
 *  4. Confirm: delivered / saved toast, per-step ✓ / ✗, why, Sign off.
 *  5. Settled: Dismiss on failed/blocked, "All 5 steps passed", a glyph each.
 *  6. The dead code is gone.
 *
 * vitest is node-only: pure helpers and renderToStaticMarkup (a zustand read
 * under SSR is its initial state, so the form is rendered on its own). */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Instance, SessionVerify } from "../api/types";
import { code } from "./sourceText";

const api = vi.fn();
const toast = vi.fn();
const errorPop = vi.fn();
const selected: string[] = [];
vi.mock("../api/client", async (orig) => ({ ...((await orig()) as object), api: (...a: unknown[]) => api(...a) }));
vi.mock("../lib/toast", () => ({ toast: (...a: unknown[]) => toast(...a) }));
vi.mock("../lib/errorPop", () => ({ errorPop: (...a: unknown[]) => errorPop(...a) }));
vi.mock("../lib/sessionActions", async (orig) => ({
  ...((await orig()) as object),
  selectSession: (t: string) => selected.push(t),
}));
vi.mock("../state/queries", async (orig) => ({
  ...((await orig()) as object),
  refreshInstances: () => Promise.resolve(),
  refreshTestPlans: () => Promise.resolve(),
}));

const VA = await import("../lib/verifyActions");
const { PaneVerifyStrip, verifyStripView, quietWord } = await import("../components/grid/PaneVerifyStrip");
const { VerifyConfirm, confirmView, confirmSteps } = await import("../components/behaviors/VerifyConfirm");
const { VerifyStartForm, startFormWhere, useVerifyForm, markVerifyRowMounted, openVerifyForm } = await import(
  "../components/behaviors/VerifyStartForm"
);
const { verifyMenuItem } = await import("../components/sidebar/verifyMenu");
const { queryClient } = await import("../state/queries");

const NOW_S = 1_800_000_000;
const verify = (over: Partial<SessionVerify> = {}): SessionVerify => ({
  state: "verifying",
  plan: "sc-1",
  steps: { total: 6, agent_done: 3, human: 1 },
  human_steps: [],
  failed_steps: [],
  detail: "",
  at: NOW_S - 30,
  ...over,
});
const HUMAN = [
  { id: "h1", text: "Open the app on a phone", expect: "the banner shows" },
  { id: "h2", text: "Sign in with the QA login", expect: "", why: "it needs a real phone" },
];
const visible = (html: string) => html.replace(/<[^>]+>/g, " ").replace(/&#x27;/g, "'").replace(/\s+/g, " ");

beforeEach(() => {
  api.mockReset();
  toast.mockReset();
  errorPop.mockReset();
  selected.length = 0;
});

describe("1. starting a Verify", () => {
  it("the body carries what to prove and where; This branch sends no environment", async () => {
    api.mockResolvedValue({ started: true, plan: "sc-1" });
    await VA.requestSessionVerify("sc-1", "  the login works ", "staging");
    expect(api).toHaveBeenLastCalledWith("/api/instances/sc-1/verify", { method: "POST", json: { notes: "the login works", environment: "staging" } });
    await VA.requestSessionVerify("sc-1", "", "branch");
    expect(api).toHaveBeenLastCalledWith("/api/instances/sc-1/verify", { method: "POST", json: {} });
  });

  it("queued: the toast says when it starts, never that it is writing its steps", async () => {
    expect(VA.verifyStartedMessage("sc-1", { queued: true, session: "sc-1" })).toBe("Verify starts when sc-1's current turn ends");
    expect(VA.verifyStartedMessage("sc-1", {})).toBe("Verifying sc-1 — its agent writes its steps and works them in its terminal");
    api.mockResolvedValue({ started: true, plan: "sc-1", queued: true, session: "sc-1" });
    expect(await VA.startSessionVerify("sc-1", { notes: "x", environment: "staging", open: false })).toBe(true);
    expect(toast).toHaveBeenCalledWith("Verify starts when sc-1's current turn ends");
  });

  it("the form: the field, Start, and a Where select only where the repo has environments", () => {
    const html = renderToStaticMarkup(createElement(VerifyStartForm, { title: "sc-1", repo: "shop", onClose() {} }));
    expect(html).toContain('placeholder="What should it prove? (optional)"');
    expect(html).toContain('type="submit"');
    expect(html).toContain(">Start</button>");
    expect(html).not.toContain("<select");
    queryClient.setQueryData(["test-plan-environments"], {
      repos: { "acme/shop": [{ name: "staging", reach: "", branch: "staging", deploy_minutes: 0 }] },
      names: ["staging"],
    });
    const withEnvs = renderToStaticMarkup(createElement(VerifyStartForm, { title: "sc-1", repo: "shop", onClose() {} }));
    expect(withEnvs).toContain("<select");
    expect(visible(withEnvs)).toContain("This branch");
    expect(visible(withEnvs)).toContain("staging");
    queryClient.removeQueries({ queryKey: ["test-plan-environments"] });
  });

  it("startFormWhere: matched on the repo's name; none → no select", () => {
    const envs = { repos: { "acme/shop": [{ name: "live", reach: "", branch: "main", deploy_minutes: 0 }], "acme/other": [{ name: "qa", reach: "", branch: "qa", deploy_minutes: 0 }] }, names: ["live", "qa"] };
    expect(startFormWhere(envs, "Shop").map((c) => c.label)).toEqual(["This branch", "live"]);
    expect(startFormWhere(envs, "nope")).toEqual([]);
    expect(startFormWhere(null, "shop")).toEqual([]);
  });

  it("the ⋯ menu's Verify opens the form in a pane that shows the row", () => {
    const unmount = markVerifyRowMounted("sc-1");
    verifyMenuItem({ title: "sc-1" } as Partial<Instance>)!.run();
    expect(useVerifyForm.getState().open).toBe("sc-1");
    expect(api).not.toHaveBeenCalled();
    useVerifyForm.setState({ open: "", opener: null });
    unmount();
  });

  it("with no row showing, it opens the session's pane first", () => {
    vi.useFakeTimers();
    try {
      openVerifyForm("sc-2");
      expect(selected).toEqual(["sc-2"]);
      expect(useVerifyForm.getState().open).toBe("sc-2");
      api.mockResolvedValue({ started: true, plan: "sc-2" });
      vi.advanceTimersByTime(1000);
      // No row ever came: starts at once, as before the form.
      expect(api).toHaveBeenCalledWith("/api/instances/sc-2/verify", { method: "POST", json: {} });
      expect(useVerifyForm.getState().open).toBe("");
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("2. following it", () => {
  it("which step, and quiet only after two minutes", () => {
    const v = verify({ current: { id: "s4", text: "GET /api/thing returns 200" }, progress_at: NOW_S - 200 });
    expect(verifyStripView(v, "sc-1", NOW_S).line).toBe("Step 4 of 5: GET /api/thing returns 200 · quiet 3 min");
    expect(verifyStripView({ ...v, progress_at: NOW_S - 60 }, "sc-1", NOW_S).line).toBe("Step 4 of 5: GET /api/thing returns 200");
    expect(quietWord(5400)).toBe("1 h 30 min");
  });

  it("queued, an older server, and the file error", () => {
    expect(verifyStripView(verify({ queued: true }), "sc-1", NOW_S).line).toBe("Waiting for sc-1's turn to end");
    expect(verifyStripView(verify(), "sc-1", NOW_S).line).toBe("Verifying — 3 of 5 steps done");
    expect(verifyStripView(verify(), "sc-1", NOW_S).fileError).toBe("");
    const html = renderToStaticMarkup(createElement(PaneVerifyStrip, { inst: { title: "sc-1", verify: verify({ file_error: "Its results file isn't valid JSON." }) } }));
    expect(html).toContain("Its results file isn&#x27;t valid JSON.");
  });

  it("while verifying: Steps, Open its agent and Stop verifying; the ⋯ menu offers Stop too", () => {
    const html = renderToStaticMarkup(createElement(PaneVerifyStrip, { inst: { title: "sc-1", verify: verify() } }));
    expect(html).toContain(">Steps</button>");
    expect(html).toContain(">Stop verifying</button>");
    expect(html).toContain(">Open its agent</button>");
    expect(verifyMenuItem({ title: "sc-1", verify: verify() } as Partial<Instance>)?.label).toBe("Stop verifying");
  });

  it("Stop verifying: the cancel route, and a toast that keeps the session", async () => {
    api.mockResolvedValue({ ok: true, session: "sc-1", verify_stopped: true });
    expect(await VA.stopVerifying("sc-1", "sc-1")).toBe(true);
    expect(api).toHaveBeenCalledWith("/api/test-plans/sc-1/cancel", { method: "POST" });
    expect(toast).toHaveBeenCalledWith("Stopped verifying sc-1 — the session keeps running");
    expect(VA.cancelMessage("sc-1", { session: "verify-sc-1-ab12" })).toBe("Stopped verifying sc-1 — closed verify-sc-1-ab12");
    expect(VA.cancelMessage("sc-1", {})).toBe("Stopped verifying sc-1 — the session keeps running");
  });
});

describe("3. failed — hand it back", () => {
  it("Let its agent fix it is the primary, Verify again second, then Steps and Dismiss", () => {
    const view = verifyStripView(verify({ state: "failed", failed_steps: [{ id: "s2", text: "x", note: "y" }] }), "sc-1", NOW_S);
    expect(view.actions.map((a) => a.label)).toEqual(["Let its agent fix it", "Verify again", "Steps", "Dismiss"]);
    expect(view.actions[0].primary).toBe(true);
    expect(view.glyph).toBe("✗");
  });

  it("fixInSession posts in_session and says where the fix happens", async () => {
    api.mockResolvedValue({ session: "sc-1", in_session: true, queued: true });
    expect(await VA.fixInSession("sc-1", "sc-1")).toBe(true);
    expect(api).toHaveBeenCalledWith("/api/test-plans/sc-1/fix", { method: "POST", json: { in_session: true } });
    expect(toast.mock.calls[0][0]).toBe("Sent what failed to sc-1's agent — it starts once its current turn ends; Verify runs again after it fixes it");
    // An older server ignores in_session and opens a new session.
    expect(VA.fixMessage("sc-1", { session: "sc-1-fix" })).toBe("Opened a new session, sc-1-fix, to fix what failed");
  });
});

describe("4. answering it", () => {
  it("the toast says whether the note reached the agent", async () => {
    expect(VA.confirmMessage(false, { delivered: true }, "sc-1")).toBe("Sent to sc-1's agent");
    expect(VA.confirmMessage(false, {}, "sc-1")).toBe("Saved on the check");
    expect(VA.confirmMessage(true, {}, "sc-1")).toBe("Confirmed verified");
    api.mockResolvedValue({ ok: true, delivered: true });
    await VA.confirmVerified("sc-1", false, "broken", { steps: { h1: "fail", h2: "pass" }, session: "sc-1" });
    expect(api).toHaveBeenCalledWith("/api/test-plans/sc-1/confirm", { method: "POST", json: { verified: false, note: "broken", steps: { h1: "fail", h2: "pass" } } });
    expect(toast).toHaveBeenCalledWith("Sent to sc-1's agent");
  });

  it("per step ✓ / ✗ with more than one step, and why the agent couldn't", () => {
    const html = renderToStaticMarkup(createElement(VerifyConfirm, { verify: { plan: "sc-1", human_steps: HUMAN } }));
    expect(html.match(/vc-mark-pass/g)?.length).toBe(2);
    expect(visible(html)).toContain("The agent couldn't do this: it needs a real phone");
    expect(html).toContain(">Confirm verified</button>");
    expect(html).not.toContain("the agent gets this");
    const one = renderToStaticMarkup(createElement(VerifyConfirm, { verify: { plan: "sc-1", human_steps: HUMAN.slice(0, 1) } }));
    expect(one).not.toContain("vc-mark-pass");
    const hidden = renderToStaticMarkup(createElement(VerifyConfirm, { verify: { plan: "sc-1", human_steps: HUMAN }, hideSteps: true }));
    expect(hidden).not.toContain("vc-steps");
  });

  it("the answers sent: all on a confirm; on Not verified only with a ✗; one ✗ needs a note", () => {
    expect(confirmSteps(HUMAN, {}, true, true)).toEqual({ h1: "pass", h2: "pass" });
    expect(confirmSteps(HUMAN, {}, true, false)).toBeNull();
    expect(confirmSteps(HUMAN, { h2: "fail" }, true, false)).toEqual({ h1: "pass", h2: "fail" });
    expect(confirmSteps(HUMAN, {}, false, true)).toBeNull();
    expect(confirmView({ human_steps: HUMAN }, { h2: "fail" }).anyFail).toBe(true);
  });

  it("no person steps: the heading is the server's detail and the button is Sign off", () => {
    const v = { plan: "sc-1", human_steps: [], detail: "The agent checked all 4 steps — sign off if that's what you wanted" };
    const html = renderToStaticMarkup(createElement(VerifyConfirm, { verify: v }));
    expect(visible(html)).toContain(v.detail);
    expect(html).toContain(">Sign off</button>");
    expect(html).not.toContain("do yours");
    const strip = renderToStaticMarkup(createElement(PaneVerifyStrip, { inst: { title: "sc-1", verify: verify({ state: "needs_you", ...v, steps: { total: 4, agent_done: 4, human: 0 } }) } }));
    expect(strip).not.toContain("do yours");
    expect(strip).toContain(">Sign off</button>");
  });
});

describe("5. settled results", () => {
  it("a glyph per state — lib/sessionState verifyGlyph's, one source", () => {
    const g = (state: SessionVerify["state"]) => verifyStripView(verify({ state }), "sc-1", NOW_S).glyph;
    expect([g("verifying"), g("needs_you"), g("passed"), g("failed"), g("blocked")]).toEqual(["…", "?", "✓", "✗", "!"]);
  });

  it("passed: one line with Steps; blocked: Verify again first and Dismiss", () => {
    const passed = verifyStripView(verify({ state: "passed", steps: { total: 5, agent_done: 5, human: 0 } }), "sc-1", NOW_S);
    expect(passed.line).toBe("All 5 steps passed");
    expect(passed.actions.map((a) => a.label)).toEqual(["Steps"]);
    const blocked = verifyStripView(verify({ state: "blocked", detail: "no results file" }), "sc-1", NOW_S);
    expect(blocked.actions.map((a) => a.label)).toEqual(["Verify again", "Steps", "Open its agent", "Dismiss"]);
  });

  it("Dismiss posts the dismiss route", async () => {
    api.mockResolvedValue({ ok: true });
    expect(await VA.dismissVerify("a/b", "a/b")).toBe(true);
    expect(api).toHaveBeenCalledWith("/api/test-plans/a%2Fb/dismiss", { method: "POST" });
  });

  it("the row's first line has one fixed height (no jump over the terminal)", () => {
    expect(code("components/grid/PaneVerifyStrip.css")).toMatch(/\.pv-row \{[^}]*height: 24px;/);
  });
});

describe("6. dead code", () => {
  it("writePlanFor / writePlanForBranch and the walk option are gone", () => {
    const src = code("lib/verifyActions.ts");
    expect(src).not.toContain("writePlanFor");
    expect(src).not.toContain("TestRunWalk");
  });
});
