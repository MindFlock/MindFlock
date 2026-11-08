/** A first launch's welcome settles What's new: the version just installed is
 * recorded as seen, so a newcomer who closes the welcome before GET
 * /api/doctor answers (seconds, on a first probe) isn't then shown the whole
 * changelog as "new" over the Get set up checklist. */

import { describe, expect, it, vi } from "vitest";

const posted: unknown[] = [];
vi.mock("../api/client", () => ({ api: vi.fn(async (_p: string, o?: { json?: unknown }) => (o?.json && posted.push(o.json), {})) }));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));

const storage = new Map<string, string>();
(globalThis as Record<string, unknown>).localStorage = {
  getItem: (k: string) => (storage.has(k) ? storage.get(k)! : null),
  setItem: (k: string, v: string) => void storage.set(k, String(v)),
  removeItem: (k: string) => void storage.delete(k),
  clear: () => storage.clear(),
};

const { newcomerSeen, settleWhatsNewForNewcomer } = await import("../components/onboarding/WelcomeTour");
const { WHATS_NEW_LS, WHATS_NEW_PREF, useWhatsNew } = await import("../components/whatsNew/seen");
const { planWhatsNew } = await import("../lib/whatsNew");
const { RELEASES } = await import("../whatsNew/releases");

describe("newcomerSeen", () => {
  it("is the running release, never lower than one already seen", () => {
    expect(newcomerSeen("0.12.3", null)).toBe("0.12.3");
    expect(newcomerSeen("0.12.3", "0.11.0")).toBe("0.12.3");
    expect(newcomerSeen("0.12.3", "0.13.0")).toBe("0.13.0");
  });

  it("records nothing for a source tree or an unknown version", () => {
    expect(newcomerSeen("", null)).toBeNull();
    expect(newcomerSeen(undefined, null)).toBeNull();
    expect(newcomerSeen("dev", "0.11.0")).toBeNull();
  });
});

describe("settleWhatsNewForNewcomer", () => {
  it("saves the version to this browser and the synced prefs, so What's new then has nothing to pop", () => {
    storage.clear();
    posted.length = 0;
    useWhatsNew.setState({ seen: null });
    // The bug: closing the welcome first flips `onboarded`, and a never-seen
    // version then reads as an update from before What's new.
    const running = "0.12.3";
    expect(planWhatsNew({ releases: RELEASES, running, lastSeen: null, onboarded: true }).pop).toBe(true);

    settleWhatsNewForNewcomer(running);
    expect(storage.get(WHATS_NEW_LS)).toBe(running);
    expect(posted).toContainEqual({ [WHATS_NEW_PREF]: running });
    expect(useWhatsNew.getState().seen).toBe(running);
    expect(planWhatsNew({ releases: RELEASES, running, lastSeen: running, onboarded: true }).pop).toBe(false);
  });

  it("does nothing without a release version", () => {
    storage.clear();
    posted.length = 0;
    settleWhatsNewForNewcomer("");
    expect(storage.has(WHATS_NEW_LS)).toBe(false);
    expect(posted).toEqual([]);
  });
});
