/** Update-from-base is gone, and the idle watchdog that replaced its
 * attention role notes only real UNCOMMITTED changes (calmly: out of the
 * bell, lib/sessionState `uncommitted`).
 *
 * Moved from tests/unit/test_needs_rebase.py's bundle checks. The watchdog
 * lives in lib/sessionState.ts (one session state for every channel); it is
 * driven here rather than grepped. */

import { describe, expect, it } from "vitest";
import type { Instance } from "../api/types";
import { deriveSessionState, UNCOMMITTED_IDLE_S } from "../lib/sessionState";
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
        activity_since: NOW / 1000 - UNCOMMITTED_IDLE_S - 60,
      } as Partial<Instance>,
      { now: NOW, activity: "idle" }
    );

  it("notes an idle session on uncommitted additions OR deletions, out of the bell", () => {
    expect(UNCOMMITTED_IDLE_S).toBe(20 * 60);
    const adds = idle({ files: 1, additions: 4, deletions: 0, uncommitted: { files: 1, additions: 4, deletions: 0 } });
    expect(adds.uncommitted?.files).toBe(1);
    expect(adds.inBell).toBe(false);
    const dels = idle({ files: 1, additions: 0, deletions: 4, uncommitted: { files: 1, additions: 0, deletions: 4 } });
    expect(dels.uncommitted?.deletions).toBe(4);
  });

  it("says nothing of a committed-and-walked-away session", () => {
    // A committed branch is finished as far as git is concerned (the header is
    // asking for a push, not reporting a problem): counting it would put every
    // session anyone committed and walked away from on the bell, claiming to
    // be wedged. Only the UNCOMMITTED counts decide — never the branch total,
    // and never the stage.
    for (const stage of ["committed", "agent"]) {
      const ss = idle({ files: 3, additions: 40, deletions: 9, uncommitted: { files: 0, additions: 0, deletions: 0 } }, stage);
      expect(ss.uncommitted, stage).toBeUndefined();
      expect(ss.inBell, stage).toBe(false);
      expect(idle({ files: 3, additions: 40, deletions: 9 }, stage).uncommitted, stage).toBeUndefined();
    }
  });
});
