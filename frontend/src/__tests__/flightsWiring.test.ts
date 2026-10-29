/** Flights' wiring outside the sky: the rail and the bell read a flight
 * parked on a *you* step as Needs you, and an arrival's Fly posts the old
 * route with its plan. (The top bar, the keys, the palette, the hash and the
 * store's routing of the retired Flights dialog's addresses are
 * flatTopBar.test.ts's.) */

/// <reference types="vite/client" />
import { afterEach, describe, expect, it, vi } from "vitest";
import { code, css } from "./sourceText";
import type { Instance, OutboxWaiting } from "../api/types";
import type { FlightRow } from "../lib/flights";

vi.mock("../api/client", () => ({ api: vi.fn(), ApiError: class extends Error {} }));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));

const { useUi } = await import("../state/store");
const { deriveSessionState, flightYouLabel, needsYouAction, notifyRuleFor } = await import("../lib/sessionState");
const { chipState } = await import("../lib/stage");
const { withFlightYou } = await import("../lib/needsYou");
const { flyRequest } = await import("../lib/flightsApi");
const { sourcePlan } = await import("../components/flights/SkyDrawer");

afterEach(() => {
  useUi.getState().closeDialog();
  useUi.getState().setView("grid");
});

describe("a flight perched on you is Needs you", () => {
  const row = (fl: FlightRow | null): Partial<Instance> => ({ title: "s", status: "running", activity: "idle", flight: fl });
  const fl: FlightRow = {
    id: "f_1",
    plan_name: "Ticket to merge",
    cursor: 4,
    total: 6,
    state: "waiting_you",
    step: { id: "s5", kind: "you", title: "Approve the merge", state: "waiting_you", actor: "you" },
  };

  it("on the rail, labelled Perch: <stop title>, in the bell, under its own notify rule", () => {
    const ss = deriveSessionState(row(fl), { activity: "idle" });
    expect(ss).toMatchObject({ state: "needs_you", reason: "flight_you", label: "Perch: Approve the merge", inBell: true });
    expect(notifyRuleFor(ss)).toBe("flight_needs_you");
    expect(flightYouLabel("")).toBe("Perch");
    // A robot step running, or a flight that is merely in the air, is not.
    expect(deriveSessionState(row({ ...fl, state: "running" }), { activity: "idle" }).reason).toBeUndefined();
    expect(deriveSessionState(row({ ...fl, step: { ...fl.step, actor: "robot" } }), { activity: "idle" }).reason).toBeUndefined();
    // A stop already titled "Perch: …" (or the old "You: …") is not prefixed again.
    expect(deriveSessionState(row({ ...fl, step: { ...fl.step, title: "Perch: approve" } }), { activity: "idle" }).label).toBe("Perch: approve");
    expect(deriveSessionState(row({ ...fl, step: { ...fl.step, title: "You: approve" } }), { activity: "idle" }).label).toBe("Perch: approve");
    // Its chip is capped with an ellipsis (the full words in its title), and
    // a click opens the bell, where Done is.
    const chip = chipState(row(fl) as Instance, ss);
    expect(chip.cls).toContain("chip-flight");
    expect(chip.title).toContain("Perch: Approve the merge");
    expect(needsYouAction(ss)?.act).toBe("bell");
    expect(css("components/sidebar/SidebarRow.css")).toMatch(
      /\.inst \.stagechip\.chip-flight \{[^}]*max-width: 9\.5em;[^}]*text-overflow: ellipsis;/
    );
  });

  it("a paused flight is a calm Paused flight, out of the bell", () => {
    const ss = deriveSessionState(row({ ...fl, state: "paused", step: { ...fl.step, state: "pending" } }), { activity: "idle" });
    expect(ss).toMatchObject({ state: "idle", label: "Paused flight", tone: "neutral", inBell: false });
    expect(ss.reason).toBeUndefined();
    expect(withFlightYou([], [{ ...row({ ...fl, state: "paused" }), title: "a" } as Instance])).toEqual([]);
  });

  it("the bell gets a flight_you row for it unless the outbox already lists one", () => {
    const inst = { ...row(fl), title: "a" } as Instance;
    const other = { ...row(null), title: "b" } as Instance;
    const got = withFlightYou([], [inst, other]);
    expect(got).toEqual([
      {
        key: "flight:f_1",
        title: "a",
        kind: "flight_you",
        reason: "Perched on you: Approve the merge",
        text: "Ticket to merge",
        actions: ["done", "skip", "open"],
      },
    ]);
    const listed: OutboxWaiting[] = [{ title: "a", kind: "flight_you" }];
    expect(withFlightYou(listed, [inst])).toEqual(listed);
    expect(code("components/outbox/WaitingRow.tsx")).toContain('if (w.kind === "flight_you") return <FlightYouRow');
    expect(code("components/outbox/WaitingRow.tsx")).toContain("{perchedOnYou(");
  });
});

describe("a waiting item's Fly", () => {
  it("posts the old route and body, plus the picked plan and the row's own picks", () => {
    expect(flyRequest({ kind: "tickets", source: "sc", id: "12" }, "")).toEqual({
      path: "/api/tickets/start",
      body: { source: "sc", id: "12" },
    });
    expect(flyRequest({ kind: "prs", repo: "acme/api", number: 7 }, "builtin:pr", { agent: "codex", depth: "" })).toEqual({
      path: "/api/github/prs/review",
      body: { repo: "acme/api", number: 7, agent: "codex", plan: "builtin:pr" },
    });
    expect(flyRequest({ kind: "issues", repo: "acme/api", number: 9 }, "p_1")).toEqual({
      path: "/api/github/issues/start",
      body: { repo: "acme/api", number: 9, plan: "p_1" },
    });
  });

  it("defaults to the source's own plan setting", () => {
    const settings = {
      ticketing: { sources: [{ id: "sc", plan: "p_tix" }] },
      github: { repo_settings: { "acme/api": { plan: "p_pr" } }, issue_repo_settings: {} },
    };
    expect(sourcePlan(settings, { kind: "tickets", source: "sc", id: "1" })).toBe("p_tix");
    expect(sourcePlan(settings, { kind: "prs", repo: "acme/api", number: 1 })).toBe("p_pr");
    expect(sourcePlan(settings, { kind: "issues", repo: "acme/api", number: 1 })).toBe("");
    expect(sourcePlan(undefined, { kind: "tickets", source: "x", id: "1" })).toBe("");
  });

  it("opens the session as soon as its row exists, and Take off does too", () => {
    const apiSrc = code("lib/flightsApi.ts");
    expect(apiSrc).toContain("if (title) void openWhenReady(title);");
    expect(apiSrc).toContain("void openWhenReady(flight.session);");
    expect(apiSrc).toContain("selectSession(title);");
  });
});

describe("the pane strip replaces the guided next step while a flight is in the air", () => {
  it("hides the guided button and the ⏩ toggle while the flight drives the session", () => {
    const pane = code("components/grid/Pane.tsx");
    expect(pane).toContain("const flying = flightDrives(inst.flight);");
    expect(pane).toContain("const ft = flying ? null : fastTrackStep(inst);");
    expect(pane).toContain('flying || (ns && ns.label === "Commit…" && !hasUncommitted(inst)) ? null : ns');
  });

  it("renders in its own row above the terminal, never inside .pane-head", () => {
    const pane = code("components/grid/Pane.tsx");
    const head = pane.indexOf('className={"pane-head pane-head-fit"');
    const strip = pane.indexOf("<PaneFlightStrip inst={inst} />");
    const body = pane.indexOf('<div className="pane-body" ref={bodyRef}>');
    expect(head).toBeGreaterThan(0);
    expect(head < strip && strip < body).toBe(true);
  });
});
