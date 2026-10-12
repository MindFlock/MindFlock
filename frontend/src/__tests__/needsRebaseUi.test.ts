/** Update-from-base is gone, and the wedge watchdog that replaced its
 * attention role calls only real unfinished work "stuck".
 *
 * Moved from tests/unit/test_needs_rebase.py's bundle checks. The watchdog
 * lives in lib/sessionState.ts (one session state for every channel); it is
 * driven here rather than grepped. */

import { describe, expect, it } from "vitest";
import type { Instance } from "../api/types";
import { deriveSessionState, STUCK_IDLE_S } from "../lib/sessionState";
import { ALL_CODE } from "./sourceText";

describe("no update-from-base wiring anywhere in the app", () => {
  it.each(['data-act="updatebase"', "/update-from-base", "_withBehind", "behind_base"])("%s is gone", (s) => {
    expect(ALL_CODE).not.toContain(s);
  });
});

describe("the wedge watchdog", () => {
  const NOW = 1_800_000_000_000;
  const idle = (diff: unknown, stage = "agent") =>
    deriveSessionState(
      {
        title: "s",
        status: "running",
        activity: "idle",
        stage,
        diff_stat: diff as Instance["diff_stat"],
        activity_since: NOW / 1000 - STUCK_IDLE_S - 60,
      } as Partial<Instance>,
      { now: NOW, activity: "idle" }
    );

  it("calls an idle session on uncommitted additions OR deletions stuck", () => {
    expect(STUCK_IDLE_S).toBe(20 * 60);
    const adds = idle({ files: 1, additions: 4, deletions: 0, uncommitted: { files: 1, additions: 4, deletions: 0 } });
    expect(adds.reason).toBe("stuck");
    expect(adds.inBell).toBe(true);
    const dels = idle({ files: 1, additions: 0, deletions: 4, uncommitted: { files: 1, additions: 0, deletions: 4 } });
    expect(dels.reason).toBe("stuck");
  });

  it("does not call a committed-and-walked-away session stuck", () => {
    // A committed branch is finished as far as git is concerned (the header is
    // asking for a push, not reporting a problem): counting it would put every
    // session anyone committed and walked away from on the bell, claiming to
    // be wedged. Only the UNCOMMITTED counts decide — never the branch total,
    // and never the stage.
    for (const stage of ["committed", "agent"]) {
      const ss = idle({ files: 3, additions: 40, deletions: 9, uncommitted: { files: 0, additions: 0, deletions: 0 } }, stage);
      expect(ss.reason, stage).toBeUndefined();
      expect(ss.inBell, stage).toBe(false);
      expect(idle({ files: 3, additions: 40, deletions: 9 }, stage).reason, stage).toBeUndefined();
    }
  });
});
