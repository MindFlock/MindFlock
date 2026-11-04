/// <reference types="vite/client" />
/* Round 21 "r21-focus": This session with no session is never a dead end —
 * SkyEmpty's card (or, with something waiting in Intake, its plain line
 * with a New session button); with sessions it can't show, it points at the
 * rail. */
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import SKYVIEW_SRC from "../components/flights/SkyView.tsx?raw";

vi.mock("../api/client", () => ({ api: vi.fn(async () => ({})) }));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));

const storage = new Map<string, string>();
(globalThis as Record<string, unknown>).localStorage = {
  getItem: (k: string) => (storage.has(k) ? storage.get(k)! : null),
  setItem: (k: string, v: string) => void storage.set(k, String(v)),
  removeItem: (k: string) => void storage.delete(k),
  clear: () => storage.clear(),
};

const { SkyEmpty } = await import("../components/onboarding/SkyEmpty");
const { QueryClient, QueryClientProvider } = await import("@tanstack/react-query");
const withQuery = (el: ReturnType<typeof createElement>) =>
  renderToStaticMarkup(createElement(QueryClientProvider, { client: new QueryClient() }, el));

describe("This session's empty views", () => {
  it("the plain line carries New session when there is no session at all", () => {
    const html = withQuery(createElement(SkyEmpty, { blank: false, newSession: true, className: "sky-empty muted" }, "No sessions in the sky yet"));
    expect(html).toContain("No sessions in the sky yet");
    expect(html).toContain("sky-empty-new");
    expect(html).toContain(">New session</button>");
    // Without it (a filter, Needs you): the line alone, as before.
    const plain = withQuery(createElement(SkyEmpty, { blank: false, className: "sky-empty muted" }, "Nothing needs you."));
    expect(plain).not.toContain("New session");
  });

  it("SkyView: no dead-end copy; blank in This session too; the rail is where to pick", () => {
    expect(SKYVIEW_SRC).not.toContain("Pick a route to show it alone.");
    expect(SKYVIEW_SRC).toContain('"Pick a session on the left to show its route here."');
    expect(SKYVIEW_SRC.match(/newSession=\{noSessions && !lines\.length\}/g)).toHaveLength(2);
    // The minimap stands down with nothing on the sky.
    expect(SKYVIEW_SRC).toContain("{mapRects.length > 0 && (");
  });
});
