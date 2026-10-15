/** The joint check (lib/peerCheck.ts + settings/screens/peer/JointCheck.tsx):
 * the verdict line in every state, the one-step-per-line editor format, the
 * card's states (steps by side, answers only on my person's steps, the older
 * peer and asking lines, which buttons show) and the session chip. */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import type { JointCheckView } from "../lib/peerCheck";

let links: unknown[] = [];
vi.mock("../state/queries", async (orig) => ({
  ...((await orig()) as object),
  usePeerLinks: () => ({ data: { links } }),
}));

const { jointCheckChip, parseStepLines, stepsToLines, verdictLine, verdictOf } = await import("../lib/peerCheck");
const { JointCheck, JointVerdictChip } = await import("../components/settings/screens/peer/JointCheck");

const STEPS: JointCheckView["steps"] = [
  { id: "s1", text: "GET /items answers", expect: "a list", actor: "agent", side: "mine" },
  { id: "s2", text: "The client prints them", actor: "agent", side: "theirs" },
  { id: "s3", text: "The list looks right", actor: "human", side: "theirs" },
  { id: "s4", text: "The log shows the call", actor: "human", side: "mine" },
];

function check(mine: Record<string, string> = {}, theirs: Record<string, string> = {}, extra = {}): JointCheckView {
  const side = (r: Record<string, string>) => ({
    results: Object.fromEntries(Object.entries(r).map(([k, v]) => [k, { result: v as "pass" }])),
    tested_sha: "",
  });
  return { id: "joint:jt_0123456789ab", rev: 1, steps: STEPS, mine: side(mine), theirs: side(theirs), link_id: "ab".repeat(16), ...extra };
}

const ALL = { mine: { s1: "pass", s4: "pass" }, theirs: { s2: "pass", s3: "pass" } };

describe("verdictLine", () => {
  it("says every state in the card's words", () => {
    expect(verdictLine(check(ALL.mine, ALL.theirs), "Bob")).toBe("Verified — every step passed on both sides");
    expect(verdictLine(check(ALL.mine, { ...ALL.theirs, s2: "fail" }), "Bob")).toBe("Failed — step 2 on Bob's side");
    expect(verdictLine(check({ ...ALL.mine, s1: "fail" }, ALL.theirs), "Bob")).toBe("Failed — step 1 on your side");
    expect(verdictLine(check({ s1: "pass" }, ALL.theirs), "Bob")).toBe("Needs you — 1 step");
    expect(verdictLine(check(ALL.mine, { s2: "pass" }), "Bob")).toBe("Needs Bob — 1 step");
    expect(verdictLine(check({ s4: "pass" }, { s3: "pass" }), "Bob")).toBe("In progress");
    expect(verdictLine(check(ALL.mine, { s2: "blocked", s3: "pass" }), "Bob")).toBe(
      "Needs you — 1 step couldn't be checked"
    );
    expect(verdictLine(null, "Bob")).toBe("No checklist yet");
    expect(verdictOf({ steps: [] })).toBe("empty");
  });
});

describe("the editor format", () => {
  it("reads tags anywhere, a -> for what should happen, and skips blanks", () => {
    const got = parseStepLines(
      "Call GET /items -> a list\n\n[theirs] Run the client\n- [theirs] [human] Looks right → two rows\n[human] Check the log"
    );
    expect(got).toEqual([
      { text: "Call GET /items", expect: "a list", actor: "agent", side: "mine" },
      { text: "Run the client", expect: "", actor: "agent", side: "theirs" },
      { text: "Looks right", expect: "two rows", actor: "human", side: "theirs" },
      { text: "Check the log", expect: "", actor: "human", side: "mine" },
    ]);
  });

  it("round-trips the steps", () => {
    const back = parseStepLines(stepsToLines(STEPS));
    expect(back.map((s) => [s.text, s.actor, s.side])).toEqual(STEPS.map((s) => [s.text, s.actor, s.side]));
    expect(back[0].expect).toBe("a list");
  });
});

const render = (c: JointCheckView | null, peerName = "Bob") =>
  renderToStaticMarkup(createElement(JointCheck, { check: c, peerName }));

describe("JointCheck", () => {
  it("renders nothing without a check", () => {
    expect(render(null)).toBe("");
  });

  it("groups the steps by side with who checks them, answers only on my person's steps", () => {
    const html = render(check({ s1: "pass" }));
    expect(html).toContain("Your side");
    expect(html).toContain("Bob&#x27;s side");
    expect(html.indexOf("GET /items answers")).toBeLessThan(html.indexOf("Bob&#x27;s side"));
    expect(html).toContain("Needs you — 1 step");
    expect(html).toContain('data-tone="you"');
    // Pass / Fail / Can't check once: only s4 (mine, a person's).
    expect(html.match(/aria-label="Answer step \d"/g)).toEqual(['aria-label="Answer step 4"']);
    expect(html).toContain("Can&#x27;t check");
    expect(html.match(/>person</g)?.length).toBe(2);
    expect(html).toContain("Write the checklist");
    expect(html).toContain("Ask my agent");
    expect(html).toContain("Run my steps");
  });

  it("says verified and failed in the chip's look", () => {
    expect(render(check(ALL.mine, ALL.theirs))).toContain('data-tone="ok"');
    const failed = render(check(ALL.mine, { ...ALL.theirs, s2: "fail" }));
    expect(failed).toContain("Failed — step 2 on Bob&#x27;s side");
    expect(failed).toContain('data-tone="bad"');
  });

  it("an empty checklist still offers Write and Ask, not Run", () => {
    const html = render({ steps: [], link_id: "ab".repeat(16), verdict: "empty" });
    expect(html).toContain("No steps yet.");
    expect(html).toContain("Write the checklist");
    expect(html).toContain("Ask my agent");
    expect(html).not.toContain("Run my steps");
  });

  it("says an older peer can't report, and that my agent is writing", () => {
    const html = render(check({}, {}, { theirs_unsupported: true, ask: { state: "asking" } }));
    expect(html).toContain("Bob&#x27;s MindFlock is older — their side can&#x27;t report");
    expect(html).toContain("Your agent is writing the checklist…");
    expect(html).toMatch(/<button[^>]*disabled=""[^>]*>Ask my agent/);
  });

  it("without a link id it is read-only", () => {
    const html = render(check({ s1: "pass" }, {}, { link_id: "" }));
    expect(html).toContain("Needs you — 1 step");
    expect(html).not.toContain('aria-label="Answer step');
    expect(html).not.toContain("Write the checklist");
  });

  it("shows why an ask failed", () => {
    const html = render(check({}, {}, { ask: { state: "failed", error: "Your agent didn't write the checklist in 3 minutes" } }));
    expect(html).toContain("in 3 minutes");
  });
});

describe("the session chip", () => {
  const LINK = "ab".repeat(16);
  const chip = (where: "row" | "pane", linkId = LINK) =>
    renderToStaticMarkup(createElement(JointVerdictChip, { linkId, peerName: "Bob", where }));

  it("shows only for a link with a joint checklist", () => {
    links = [{ link_id: LINK, peer_name: "Bob", joint: { id: "jt_x", state: "active" } }];
    expect(chip("row")).toBe("");
    expect(chip("row", "")).toBe("");
    links = [{ link_id: LINK, peer_name: "Bob", joint: { check: { verdict: "verified", steps: 3, passed: 3 } } }];
    expect(chip("row", "cd".repeat(16))).toBe("");
  });

  it("reuses the Verify chip's look: the mark on a row, words in the pane head", () => {
    links = [{ link_id: LINK, peer_name: "Bob", joint: { check: { verdict: "failed", steps: 3, passed: 1 } } }];
    const row = chip("row");
    expect(row).toContain('class="vchip vchip-row peer-joint-chip"');
    expect(row).toContain('data-tone="bad"');
    expect(row).toContain("Joint check with Bob: a step failed. Open it in People.");
    expect(row).not.toContain("Joint failed");
    expect(chip("pane")).toContain("Joint failed");
  });

  it("words in progress with the count", () => {
    expect(jointCheckChip({ verdict: "in_progress", steps: 4, passed: 1 }, "Bob")?.title).toBe(
      "Joint check with Bob: 1 of 4 passed so far. Open it in People."
    );
    expect(jointCheckChip(null, "Bob")).toBeNull();
  });
});
