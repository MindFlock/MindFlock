/** The check gate's "Push anyway" card names the session it will push.
 *
 * errorPop drops a card whose title+detail is already on screen. A card that
 * did not name its session meant a second blocked push (another session)
 * reused the first card, and its Push anyway force-pushed the FIRST session —
 * a surprise push past the gate, with the second session's override never
 * offered.
 */

import { describe, it, expect, beforeEach, vi } from "vitest";

const instApi = vi.fn();

vi.mock("../api/client", async (orig) => {
  const actual = (await orig()) as Record<string, unknown>;
  return {
    ...actual,
    api: vi.fn(async () => ({})),
    instApi: (...args: unknown[]) => instApi(...args),
  };
});

vi.mock("../state/queries", async (orig) => {
  const actual = (await orig()) as Record<string, unknown>;
  return { ...actual, refreshInstances: vi.fn(async () => {}) };
});

// Plain DOM in the app; this env has no document.
const errorPop = vi.fn();
vi.mock("../lib/errorPop", () => ({ errorPop: (...args: unknown[]) => errorPop(...args) }));

const { pushSession } = await import("../lib/sessionActions");

type Action = { label: string; run: () => void };

describe("the check gate's Push anyway card", () => {
  beforeEach(() => {
    instApi.mockReset();
    errorPop.mockReset();
    instApi.mockImplementation(async (_title: string, path: string, opts?: { json?: { force?: boolean } }) => {
      if (path === "/push-branch" && !opts?.json?.force) throw new Error("checks haven't passed for this commit");
      return {};
    });
  });

  it("raises one card per session, each pushing its own session", async () => {
    await pushSession("sc-17455");
    await pushSession("sc-17460");

    const cards = errorPop.mock.calls.filter((c) => (c[2] as Action[] | undefined)?.some((a) => a.label === "Push anyway"));
    expect(cards).toHaveLength(2);
    // Distinct title+detail: errorPop's dedupe can't fold one into the other.
    const keys = cards.map((c) => c[0] + " " + c[1]);
    expect(new Set(keys).size).toBe(2);
    expect(cards[0][1]).toContain("sc-17455");
    expect(cards[1][1]).toContain("sc-17460");

    instApi.mockClear();
    const anyway = (c: unknown[]) => (c[2] as Action[]).find((a) => a.label === "Push anyway")!;
    anyway(cards[1]).run();
    await Promise.resolve();
    const pushes = instApi.mock.calls.filter((c) => c[1] === "/push-branch");
    expect(pushes).toHaveLength(1);
    expect(pushes[0][0]).toBe("sc-17460");
    expect(pushes[0][2]).toEqual({ json: { force: true } });
  });
});
