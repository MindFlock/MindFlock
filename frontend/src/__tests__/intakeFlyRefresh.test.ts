/** Stability sweep (2026-11-12): a waiting item flown from the Sky (the
 * intake lane's Run, the drawer's Fly) leaves the waiting list at once. The
 * waiting list is read from the tickets / PRs / issues panels, which nothing
 * re-read after such a start: the item stayed listed (and counted) with an
 * enabled Run for minutes, and a second press got "session … already
 * exists". The Intake dialog's own row start already re-sweeps its panel. */
import { beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.fn((..._args: unknown[]): Promise<unknown> => Promise.resolve({}));
vi.mock("../api/client", () => ({ api: (...a: unknown[]) => api(...a), ApiError: class extends Error {} }));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));

const { flyLaneItem } = await import("../components/flights/IntakeLane");
const { flyWaiting } = await import("../lib/flightsApi");

(globalThis as Record<string, unknown>).document ??= {
  hidden: false,
  addEventListener: () => {},
  removeEventListener: () => {},
  getElementById: () => null,
  querySelector: () => null,
};

const lane = (target: Record<string, unknown>, reference = "sc-1") =>
  ({ item: { key: "k", kind: target.kind, reference, title: "t", target } }) as unknown as Parameters<typeof flyLaneItem>[0];
const FLYWAY = { flyway: { id: "", name: "", own: false } };
const paths = () => api.mock.calls.map((c) => c[0] as string);

beforeEach(() => api.mockClear());

describe("a start from the Sky re-reads the waiting list it came from", () => {
  it("the lane's Run: tickets, PRs, issues", async () => {
    await flyLaneItem(lane({ kind: "tickets", source: "sc", id: "1" }), FLYWAY);
    expect(paths()).toContain("/api/tickets?fresh=1");
    api.mockClear();
    await flyLaneItem(lane({ kind: "prs", repo: "acme/app", number: 4 }), FLYWAY);
    expect(paths()).toContain("/api/github/prs?fresh=1");
    api.mockClear();
    await flyLaneItem(lane({ kind: "issues", repo: "acme/app", number: 5 }), FLYWAY);
    expect(paths()).toContain("/api/github/issues?fresh=1");
  });

  it("the drawer's Fly", async () => {
    await flyWaiting({ kind: "issues", repo: "acme/app", number: 5 }, "", {}, "x");
    expect(paths()).toContain("/api/github/issues?fresh=1");
  });

  it("a refused start re-reads nothing and still throws", async () => {
    api.mockImplementationOnce(() => Promise.reject(new Error("session sc-1 already exists")));
    await expect(flyLaneItem(lane({ kind: "tickets", source: "sc", id: "1" }), FLYWAY)).rejects.toThrow("already exists");
    expect(paths().filter((p) => p.includes("fresh=1"))).toEqual([]);
  });
});
