/** lib/sessionState — the ONE session state every channel reads.
 *
 * Pinned: every row of the table (state, tone, label, who is in the bell,
 * restartable), limit with and without a known resume time, idle that turns
 * "stuck" only after 20 minutes on uncommitted work, offline that is
 * restartable locally but not on a proxied device row, and the notify switch
 * each state answers to. */

import { describe, expect, it } from "vitest";
import type { Instance } from "../api/types";
import {
  STUCK_IDLE_S,
  attentionPriority,
  attentionReason,
  deriveSessionState,
  fmtClock,
  notifyRuleFor,
} from "../lib/sessionState";

const inst = (o: Partial<Instance>): Partial<Instance> => ({ title: "t", status: "running", ...o });
const NOW = 1_800_000_000_000; // epoch ms, fixed

describe("deriveSessionState — the table", () => {
  it.each([
    ["starting (loading)", inst({ status: "loading" }), "starting", "muted", "Starting", false],
    ["starting (pending)", inst({ pending: true }), "starting", "muted", "Starting", false],
    ["starting (setup running)", inst({ setup: { state: "running" } as Instance["setup"] }), "starting", "muted", "Starting", false],
    ["working", inst({ activity: "working" }), "working", "working", "Working", false],
    ["needs you — question", inst({ activity: "clarify" }), "needs_you", "attention", "Question", true],
    ["needs you — pre-commit", inst({ activity: "idle", stage: "interrupt" }), "needs_you", "attention", "Pre-commit failed", true],
    [
      "needs you — checks",
      inst({ activity: "idle", stage: "committed", check: { state: "failed" } as Instance["check"] }),
      "needs_you",
      "attention",
      "Checks failing",
      true,
    ],
    ["needs you — setup", inst({ setup: { state: "failed" } as Instance["setup"] }), "needs_you", "attention", "Setup failed", true],
    ["idle", inst({ activity: "idle", stage: "agent" }), "idle", "neutral", "Idle", false],
    ["limit", inst({ activity: "limit" }), "limit", "waiting", "Waiting on limit", false],
    ["offline", inst({ activity: "offline" }), "offline", "broken", "Offline", true],
  ] as const)("%s", (_name, row, state, tone, label, inBell) => {
    const ss = deriveSessionState(row, { now: NOW, activity: row.activity as string | undefined });
    expect(ss.state).toBe(state);
    expect(ss.tone).toBe(tone);
    expect(ss.label).toBe(label);
    expect(ss.inBell).toBe(inBell);
  });

  it("names the reason a session needs you", () => {
    const act = (a: string, o: Partial<Instance> = {}) =>
      deriveSessionState(inst({ activity: a, ...o }), { now: NOW, activity: a }).reason;
    expect(act("clarify")).toBe("question");
    expect(act("idle", { stage: "interrupt" })).toBe("precommit_failed");
    expect(act("idle", { check: { state: "failed" } as Instance["check"] })).toBe("checks_failing");
    expect(act("idle", { setup: { state: "failed" } as Instance["setup"] })).toBe("setup_failed");
    expect(act("working")).toBeUndefined();
  });

  it("nobody-acts states and the agent's own state never reach the bell", () => {
    for (const a of ["working", "limit"])
      expect(deriveSessionState(inst({ activity: a }), { now: NOW, activity: a }).inBell).toBe(false);
    expect(deriveSessionState(inst({ status: "loading" }), { now: NOW }).inBell).toBe(false);
  });

  it("a stale check result is not 'checks failing'", () => {
    const ss = deriveSessionState(
      inst({ activity: "idle", check: { state: "failed", stale: true } as unknown as Instance["check"] }),
      { now: NOW, activity: "idle" }
    );
    expect(ss.state).toBe("idle");
    expect(ss.reason).toBeUndefined();
  });

  it("setup failed outranks the agent's activity (prompts are held)", () => {
    const ss = deriveSessionState(inst({ activity: "working", setup: { state: "failed" } as Instance["setup"] }), {
      now: NOW,
      activity: "working",
    });
    expect(ss.reason).toBe("setup_failed");
  });

  it("a working agent with a failed pre-commit is Working — the agent acts, not you", () => {
    const ss = deriveSessionState(inst({ activity: "working", stage: "interrupt" }), { now: NOW, activity: "working" });
    expect(ss.state).toBe("working");
    expect(ss.inBell).toBe(false);
  });
});

describe("limit", () => {
  it("says when it resumes once the window is known", () => {
    const until = NOW / 1000 + 1800;
    const ss = deriveSessionState(
      inst({ activity: "limit", queue: { limited_until: until } as Instance["queue"] }),
      { now: NOW, activity: "limit" }
    );
    expect(ss.resumeAt).toBe(until);
    expect(ss.label).toBe("Waiting on limit, resumes " + fmtClock(until));
    expect(ss.label).toMatch(/resumes \d\d:\d\d$/);
    expect(ss.tone).toBe("waiting");
    expect(ss.inBell).toBe(false);
  });

  it("stays a plain 'Waiting on limit' without one, or with an expired one", () => {
    const none = deriveSessionState(inst({ activity: "limit" }), { now: NOW, activity: "limit" });
    expect(none.resumeAt).toBeUndefined();
    expect(none.label).toBe("Waiting on limit");
    const past = deriveSessionState(
      inst({ activity: "limit", queue: { limited_until: NOW / 1000 - 5 } as Instance["queue"] }),
      { now: NOW, activity: "limit" }
    );
    expect(past.resumeAt).toBeUndefined();
    expect(past.label).toBe("Waiting on limit");
  });
});

describe("idle → stuck", () => {
  const dirty = { files: 2, additions: 12, deletions: 3, uncommitted: { files: 2, additions: 12, deletions: 3 } };

  it("joins the bell only after 20 minutes idle on uncommitted work", () => {
    const at = (idleS: number, diff = dirty) =>
      deriveSessionState(
        inst({ activity: "idle", stage: "agent", diff_stat: diff as unknown as Instance["diff_stat"], activity_since: NOW / 1000 - idleS }),
        { now: NOW, activity: "idle" }
      );
    const fresh = at(STUCK_IDLE_S - 60);
    expect(fresh.inBell).toBe(false);
    expect(fresh.reason).toBeUndefined();
    const stuck = at(STUCK_IDLE_S + 60);
    expect(stuck.state).toBe("idle");
    expect(stuck.reason).toBe("stuck");
    expect(stuck.inBell).toBe(true);
    // Not the plain idle word: how long, in the tone the bell's rows wear.
    expect(stuck.label).toBe("Idle 21m");
    expect(stuck.tone).toBe("attention");
    expect(fresh.label).toBe("Idle");
    expect(fresh.tone).toBe("neutral");
    expect(attentionReason(stuck)).toMatch(/^Idle 21m with unfinished work/);
  });

  it("a committed-and-left branch is not unfinished work", () => {
    const ss = deriveSessionState(
      inst({ activity: "idle", stage: "committed", diff_stat: { files: 0, additions: 0, deletions: 0 } as unknown as Instance["diff_stat"], activity_since: NOW / 1000 - 36000 }),
      { now: NOW, activity: "idle" }
    );
    expect(ss.inBell).toBe(false);
  });

  it("an unknown activity_since never counts as idle time", () => {
    const ss = deriveSessionState(
      inst({ activity: "idle", stage: "agent", diff_stat: dirty as unknown as Instance["diff_stat"], activity_since: 0 }),
      { now: NOW, activity: "idle" }
    );
    expect(ss.inBell).toBe(false);
  });
});

describe("offline", () => {
  it("is restartable on a local row, and offers the chip's Restart", () => {
    const ss = deriveSessionState(inst({ activity: "offline" }), { now: NOW, activity: "offline" });
    expect(ss.restartable).toBe(true);
    expect(ss.tone).toBe("broken");
    expect(ss.inBell).toBe(true);
  });

  it("is not restartable on a row proxied from another device (read-only)", () => {
    const ss = deriveSessionState(inst({ title: "mini::t", device: "mini", activity: "offline" }), {
      now: NOW,
      activity: "offline",
    });
    expect(ss.state).toBe("offline");
    expect(ss.restartable).toBe(false);
  });

  it("killed and coming back by itself: out of the bell, says why, still restartable", () => {
    const reason = "The agent was killed (likely out of memory) — it restarts by itself once memory has recovered";
    const ss = deriveSessionState(inst({ activity: "offline", auto_revive: { state: "pending", reason } }), {
      now: NOW,
      activity: "offline",
    });
    expect(ss.state).toBe("offline");
    expect(ss.inBell).toBe(false);
    expect(ss.restartable).toBe(true);
    expect(ss.hint).toBe(reason);
  });

  it("left Offline after too many kills: back in the bell, with the reason", () => {
    const reason = "Killed 4 times in the last hour (likely out of memory) — left Offline";
    const ss = deriveSessionState(inst({ activity: "offline", auto_revive: { state: "gave_up", reason } }), {
      now: NOW,
      activity: "offline",
    });
    expect(ss.inBell).toBe(true);
    expect(ss.hint).toBe(reason);
  });
});

describe("outside the table", () => {
  it("paused is muted, 'Paused', not in the bell", () => {
    const ss = deriveSessionState(inst({ status: "paused", activity: "clarify" }), { now: NOW, activity: "clarify" });
    expect(ss.label).toBe("Paused");
    expect(ss.tone).toBe("muted");
    expect(ss.inBell).toBe(false);
  });

  it("a missing folder is broken, 'Missing', not restartable and not in the bell", () => {
    const ss = deriveSessionState(inst({ workspace_missing: true, activity: "offline" }), { now: NOW, activity: "offline" });
    expect(ss.label).toBe("Missing");
    expect(ss.tone).toBe("broken");
    expect(ss.restartable).toBe(false);
    expect(ss.inBell).toBe(false);
  });
});

describe("the bell's order and the notify switches", () => {
  const of = (o: Partial<Instance>, a: string) => deriveSessionState(inst({ activity: a, ...o }), { now: NOW, activity: a });

  it("ranks a question first, then broken, then checks", () => {
    expect(attentionPriority(of({}, "clarify"))).toBe(0);
    expect(attentionPriority(of({ stage: "interrupt" }, "idle"))).toBe(1);
    expect(attentionPriority(of({ setup: { state: "failed" } as Instance["setup"] }, "idle"))).toBe(1);
    expect(attentionPriority(of({}, "offline"))).toBe(1);
    expect(attentionPriority(of({ check: { state: "failed" } as Instance["check"] }, "idle"))).toBe(2);
  });

  it("names the Settings → Notifications switch that gates each toast and badge", () => {
    expect(notifyRuleFor(of({}, "clarify"))).toBe("needs_input");
    expect(notifyRuleFor(of({ stage: "interrupt" }, "idle"))).toBe("precommit_failed");
    expect(notifyRuleFor(of({ setup: { state: "failed" } as Instance["setup"] }, "idle"))).toBe("setup_failed");
    expect(notifyRuleFor(of({ check: { state: "failed" } as Instance["check"] }, "idle"))).toBe("checks_failing");
    expect(notifyRuleFor(of({}, "offline"))).toBe("session_offline");
    expect(notifyRuleFor(of({}, "working"))).toBe("");
  });
});
