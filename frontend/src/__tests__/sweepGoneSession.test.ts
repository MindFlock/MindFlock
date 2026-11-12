/** Stability sweep (2026-11-12): a route parked on you (or halted) whose
 * session is closed is listed under Needs you. Its row offered only Retry /
 * Skip / Done and "Let the agent fix it" — Retry and the fix fail again on a
 * session that is gone, and the name flew the sky to a session that no
 * longer exists — so nothing there ended the route. It now says the session
 * is closed, drops the fix, and offers Stop the route (the server's cancel
 * takes any unfinished route). */
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import type { Flight, FlightStep } from "../lib/flights";

vi.mock("../api/client", () => ({ api: vi.fn(() => new Promise(() => {})), instApi: vi.fn(() => new Promise(() => {})), ApiError: class extends Error {} }));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));

const { QueryClient, QueryClientProvider } = await import("@tanstack/react-query");
const { PerchRow } = await import("../components/flights/SkyDrawer");

(globalThis as Record<string, unknown>).document ??= {
  hidden: false,
  addEventListener: () => {},
  removeEventListener: () => {},
  getElementById: () => null,
  querySelector: () => null,
};

const step = (id: string, kind = "agent"): FlightStep => ({ id, kind, title: "Stop " + id, config: {}, on_fail: "halt" });
const rec = (state: string) => ({ state, since: 0, note: "", detail: {}, rounds: 0 }) as Flight["step_states"][string];
const HALTED = {
  id: "f_1",
  plan_id: "",
  plan_name: "Ticket to merge",
  steps: [step("s1"), step("s2"), step("s3")],
  state: "halted",
  cursor: 1,
  step_states: { s1: rec("done"), s2: rec("failed"), s3: rec("pending") },
  session: "sc-12",
  reason: "Stop s2 failed",
  subject: { kind: "manual", source: "", id: "", title: "", url: "" },
} as unknown as Flight;

function row(rows: Array<{ title: string }>) {
  const c = new QueryClient({ defaultOptions: { queries: { retry: false, enabled: false } } });
  c.setQueryData(["instances"], rows);
  return renderToStaticMarkup(createElement(QueryClientProvider, { client: c }, createElement(PerchRow, { flight: HALTED, onFocus: () => {} })));
}

describe("a parked route whose session is gone can be ended from its Needs-you row", () => {
  it("session open: Retry, the fix, Skip; the name flies to it; no Stop", () => {
    const html = row([{ title: "sc-12" }]);
    expect(html).toContain(">Retry<");
    expect(html).toContain("Let the agent fix it");
    expect(html).not.toContain("Stop the route");
    expect(html).toMatch(/<button[^>]*class="sky-perch-name"/);
  });

  it("session closed: Stop the route, no fix, and the name is not a dead link", () => {
    const html = row([{ title: "other" }]);
    expect(html).toContain(">Stop the route<");
    expect(html).not.toContain("Let the agent fix it");
    expect(html).not.toMatch(/<button[^>]*class="sky-perch-name"/);
    expect(html).toContain("is closed");
  });

  it("rows still loading: not taken for gone", () => {
    const c = new QueryClient({ defaultOptions: { queries: { retry: false, enabled: false } } });
    const html = renderToStaticMarkup(createElement(QueryClientProvider, { client: c }, createElement(PerchRow, { flight: HALTED, onFocus: () => {} })));
    expect(html).not.toContain("Stop the route");
  });
});

