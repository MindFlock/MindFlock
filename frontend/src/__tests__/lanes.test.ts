/** Ship lanes on the rail: the lane-first status line (agentMessages.shipLine).
 * The line leads with where the session is going ("→ PR"), so a narrow rail
 * cuts the detail and never the lane — `lead` and `rest` are separate spans
 * for exactly that, and these tests pin which words land in which. */
import { describe, it, expect } from "vitest";
import type { Instance } from "../api/types";
import { checksText, escalationText, laneOf, prNumber, shipLine } from "../lib/agentMessages";

const NOW = 1_800_000_000;
const row = (extra: Partial<Instance> = {}) =>
  ({ title: "s", status: "running", activity: "idle", stage: "agent", ...extra }) as Instance;
const lane = (target: string, ask_first = false) => ({ target, ask_first });
const ap = (o: Record<string, unknown>) =>
  ({ depth: "pr", state: "running", step: "", reason: "", source: "run", item: "", ...o }) as never;

describe("shipLine: what the rail says about a session's lane", () => {
  it("says nothing for a session MindFlock isn't carrying", () => {
    expect(shipLine(row())).toBeNull();
    // "Leave it" on a session of its own is no lane at all…
    expect(shipLine(row({ lane: lane("leave") }))).toBeNull();
    // …but a group member still gets a line (its group carries it).
    const m = shipLine(row({ lane: lane("leave"), run: { id: "r", name: "G", task: "t", role: "task", grouping: "each" } }));
    expect(m?.lead).toBe("fast-track off");
  });

  it("leads with the lane while the agent works: → PR · working 12m", () => {
    const l = shipLine(row({ lane: lane("pr"), activity: "working", activity_since: NOW - 720 }), { now: NOW })!;
    expect(l.lead).toBe("→ PR");
    expect(l.rest).toBe(" · working 12m");
    expect(l.state).toBe("working");
    expect(l.cls).toBe("rep-lane");
  });

  it("puts 'asks first' IN the lead, so it is never the part that gets cut", () => {
    const l = shipLine(row({ lane: lane("commit", true) }), { act: "idle" })!;
    expect(l.lead).toBe("→ commit, asks first");
    expect(l.rest).toBe(" · idle");
    expect(l.title).toMatch(/asks you first in the bell before anything leaves this machine/);
    expect(l.title).not.toMatch(/Outbox/);
  });

  it("says ⇡ and the next outward step while MindFlock ships it", () => {
    const pushed = shipLine(row({ lane: lane("pr"), stage: "pushed", autopilot: ap({ step: "push", note: "opening PR" }) }))!;
    expect(pushed.lead).toBe("⇡ opening PR");
    expect(pushed.state).toBe("shipping");
    expect(pushed.cls).toBe("rep-ship");
    const committed = shipLine(row({ lane: lane("pr"), stage: "committed", autopilot: ap({ step: "commit" }) }))!;
    expect(committed.lead).toBe("⇡ pushing");
    // The run's own word for it counts too, before autopilot has a step.
    const t = shipLine(row({ lane: lane("pr"), stage: "agent" }), { task: { state: "shipping" } })!;
    expect(t.lead).toBe("⇡ committing");
    // A running chain that hasn't acted yet is still the agent's turn.
    const waiting = shipLine(row({ lane: lane("pr"), autopilot: ap({ step: "" }) }), { act: "idle" })!;
    expect(waiting.lead).toBe("→ PR");
  });

  it("says what the lane produced once it is reached: ✓ PR #318 · checks ✓", () => {
    const ms = { number: 318, url: "", state: "clean", mergeable: true, checks: "ok", can_merge: true, blockers: [] };
    const l = shipLine(row({ lane: lane("pr"), stage: "pr", pr_url: "https://x/pull/318", merge_state: ms }))!;
    expect(l.lead).toBe("✓ PR #318");
    expect(l.rest).toBe(" · checks ✓");
    expect(l.cls).toBe("rep-done");
    const red = shipLine(row({ lane: lane("pr"), stage: "pr", merge_state: { ...ms, checks: "failed" } }))!;
    expect(red.rest).toBe(" · checks ✗");
    expect(red.restCls).toBe("bad");
    expect(shipLine(row({ lane: lane("commit"), stage: "committed" }))!.lead).toBe("✓ committed");
    expect(shipLine(row({ lane: lane("push"), stage: "pr" }))!.lead).toBe("✓ pushed");
    const merged = shipLine(row({ lane: lane("merge"), stage: "agent", pr_url: "https://x/pull/9", autopilot: ap({ depth: "merge", state: "done", step: "merge" }) }))!;
    expect(merged.lead).toBe("✓ merged");
    expect(merged.rest).toBe(" · PR #9");
  });

  it("puts a prompt first: ? needs your answer", () => {
    const l = shipLine(row({ lane: lane("pr"), stage: "pr", activity: "clarify" }))!;
    expect(l.lead).toBe("? needs your answer");
    expect(l.cls).toBe("rep-ask");
  });

  it("says an escalation in red and points at the bell", () => {
    const l = shipLine(row({ lane: lane("pr") }), { task: { state: "needs_you", reason: "ship_halted" } })!;
    expect(l.lead).toBe("! hooks failed twice");
    expect(l.rest).toBe(" — open the bell");
    expect(l.cls).toBe("rep-blocked");
    expect(l.state).toBe("escalated");
    // A prompt is not an escalation (the clarify line says it), and an
    // approval is a lane waiting on your OK, not a failure.
    expect(shipLine(row({ lane: lane("pr") }), { task: { state: "needs_you", reason: "prompt" } })!.state).not.toBe("escalated");
    const ok = shipLine(row({ lane: lane("commit", true) }), { task: { state: "needs_you", reason: "approve" } })!;
    expect(ok.lead).toBe("→ commit, asks first");
    expect(ok.rest).toBe(" · ready — approve in the bell");
    expect(ok.title).toMatch(/the bell shows the commit message and PR title before anything is pushed/);
    expect(ok.state).toBe("approve");
    // A failed line with no reason points at the bell too.
    expect(shipLine(row({ lane: lane("pr") }), { task: { state: "failed" } })!.rest).toBe(" — open the bell");
    // The clarify line says where it can be answered.
    expect(shipLine(row({ lane: lane("pr") }), { act: "clarify" })!.title).toMatch(/answer it here, in the bell, or in its pane/);
    // A fast-track that stopped is an escalation too.
    const halted = shipLine(row({ lane: lane("pr"), autopilot: ap({ state: "halted", reason: "no origin remote" }) }))!;
    expect(halted.lead).toBe("! fast-track stopped");
    expect(halted.rest).toBe(" — no origin remote");
  });

  it("reads the lane off an armed fast-track when the server sends no `lane` field", () => {
    expect(laneOf(row({ autopilot: ap({ depth: "pr" }) }))).toEqual({ target: "pr", ask_first: false });
    expect(laneOf(row({ autopilot: ap({ depth: "agent" }) }))).toEqual({ target: "leave", ask_first: false });
    expect(laneOf(row({ lane: lane("commit", true), autopilot: ap({ depth: "pr" }) }))?.target).toBe("commit");
    expect(laneOf(row())).toBeNull();
  });

  it("names the window that really carries a duplicated branch", () => {
    const l = shipLine(row({ title: "foo-copy", lane: { target: "pr", ask_first: false, owner: "foo" } }))!;
    expect(l.title).toMatch(/shares its branch with “foo”, which carries it/);
  });

  it("words the helpers", () => {
    expect(prNumber(row({ pr_url: "https://github.com/a/b/pull/42" }))).toBe("42");
    expect(prNumber(row())).toBe("");
    expect(checksText(row()).text).toBe("");
    expect(escalationText("stuck")).toBe("stalled twice — no diff, no report");
    expect(escalationText("the remote refused the push")).toBe("the remote refused the push");
  });
});
