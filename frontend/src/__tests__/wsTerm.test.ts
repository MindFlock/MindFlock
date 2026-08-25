/** The reconnect rule behind the Verify dialog's inline run watcher.
 *
 * Pinned here rather than left inside the effect because vitest is node-only in
 * this repo — no DOM, no WebSocket — so the decision has to be a pure function
 * for anything to be able to check it. It encodes a bug that was live: pressing
 * Run mounts the watcher immediately, the session does not exist yet (its
 * worktree is still being cut), the socket is refused with 4404, and the old
 * code recorded that as "disconnected" forever. A blank box, every run.
 */
import { describe, it, expect } from "vitest";
import { isStarting, retryAfter } from "../lib/wsTerm";

describe("retryAfter", () => {
  it("keeps waiting while the session is still starting", () => {
    // 4404 = the engine has no such session YET. This is the normal path for
    // the first seconds of every run and must never read as a failure.
    expect(retryAfter(4404, 0, true)).toBeGreaterThan(0);
    expect(retryAfter(4404, 5, true)).toBeGreaterThan(0);
    // 4409 = the session exists but its pane is not attachable yet.
    expect(retryAfter(4409, 3, true)).toBeGreaterThan(0);
  });

  it("gives up eventually rather than polling a session that never arrives", () => {
    expect(retryAfter(4404, 100, true)).toBe(null);
  });

  it("leaves the grid panes exactly as they were", () => {
    // `reconnect` is opt-in: SpecialPane passes false, and its behaviour must
    // not change because the dialog needed something.
    expect(retryAfter(4404, 0, false)).toBe(null);
    expect(retryAfter(1006, 0, false)).toBe(null);
  });

  it("retries a dropped socket, but not a refusal it has already seen", () => {
    // An abnormal close or a server going away is usually the server restarting
    // under a live session.
    expect(retryAfter(1006, 2, true)).toBeGreaterThan(0);
    expect(retryAfter(1001, 2, true)).toBeGreaterThan(0);
    // A clean close after we have already been connected is the far end saying
    // no on purpose — one look, then stop.
    expect(retryAfter(1000, 0, true)).toBeGreaterThan(0);
    expect(retryAfter(1000, 1, true)).toBe(null);
  });

  it("backs off, then settles into a flat interval", () => {
    const first = retryAfter(4404, 0, true)!;
    const second = retryAfter(4404, 1, true)!;
    const late = retryAfter(4404, 40, true)!;
    expect(second).toBeGreaterThan(first);
    // Flat, not doubling: the thing being waited on is a worktree checkout, and
    // a doubling curve would be checking every two minutes by the time it
    // finished.
    expect(late).toBe(retryAfter(4404, 41, true));
    expect(late).toBeLessThanOrEqual(5000);
  });
});

describe("isStarting", () => {
  it("tells 'not up yet' apart from 'went away'", () => {
    // Only the wording the user sees depends on this — "starting the session…"
    // rather than "reconnecting…".
    expect(isStarting(4404)).toBe(true);
    expect(isStarting(4409)).toBe(true);
    expect(isStarting(1006)).toBe(false);
  });
});
