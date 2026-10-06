/** The New dialog's list mode, pure half (lib/runStart): reading the box,
 * when it becomes a list, the rows, ✕, the POST /api/runs body (SPEC §5),
 * the button and the sentence — and that one plain line is still exactly
 * today's single session. */
import { describe, expect, it } from "vitest";
import {
  clampConcurrency,
  defaultLaneFor,
  fallbackName,
  isListMode,
  isTicketToken,
  itemRows,
  localItems,
  removeItem,
  requestItems,
  runBody,
  splitApplies,
  splitShapeReason,
  startLabel,
  startTogetherText,
  summarySentence,
  type PreviewResponse,
} from "../lib/runStart";

describe("one plain line is today's single session", () => {
  const one = localItems("fix the login bug in acme-api");

  it("is not a list, keeps 'Create session', and ships nothing by default", () => {
    expect(one).toEqual([{ kind: "task", text: "fix the login bug in acme-api" }]);
    expect(isListMode(one)).toBe(false);
    expect(startLabel(1, false)).toBe("Create session");
    expect(defaultLaneFor(false, "pr", "merge")).toBe("leave");
  });

  it("says nothing extra while it is Leave it", () => {
    expect(
      summarySentence({
        n: 1,
        concurrency: 3,
        lane: "leave",
        askFirst: false,
        grouping: "each",
        split: false,
      })
    ).toBeNull();
  });

  it("can be split; a list or a ticket can't", () => {
    expect(splitApplies(one)).toBe(true);
    expect(splitShapeReason(one)).toBe("");
    expect(splitApplies(localItems("a\nb"))).toBe(false);
    expect(splitShapeReason(localItems("a\nb"))).toMatch(/list of 2/);
    expect(splitShapeReason(localItems("PAY-412"))).toMatch(/ticket/);
  });
});

describe("reading the box", () => {
  it("ticket-shaped tokens", () => {
    for (const t of ["PAY-412", "sc-1234", "#318", "acme/api#9", "https://x.y/z"])
      expect(isTicketToken(t)).toBe(true);
    for (const t of ["fix", "PAY-", "-12", "a b"]) expect(isTicketToken(t)).toBe(false);
  });

  it("a line of only IDs is that many tickets; other lines are one task each", () => {
    const items = localItems(
      "PAY-412 PAY-415, PAY-419\n\n  Per-user rate limit on /webhooks  \nPAY-412\nBackfill PAY-9 ledger"
    );
    expect(items).toEqual([
      { kind: "ticket", ref: "PAY-412" },
      { kind: "ticket", ref: "PAY-415" },
      { kind: "ticket", ref: "PAY-419" },
      { kind: "task", text: "Per-user rate limit on /webhooks" },
      // A ticket mentioned inside a sentence is part of that task.
      { kind: "task", text: "Backfill PAY-9 ledger" },
    ]);
  });

  it("list mode: two things, or any ticket at all", () => {
    expect(isListMode(localItems("a\nb"))).toBe(true);
    expect(isListMode(localItems("PAY-412"))).toBe(true);
    expect(isListMode(localItems("one thing"))).toBe(false);
    expect(isListMode(localItems(""))).toBe(false);
  });
});

describe("the rows and ✕", () => {
  const preview: PreviewResponse = {
    items: [
      {
        kind: "ticket",
        source: "jira-payments",
        id: "PAY-412",
        ref: "PAY-412",
        title: "Retry refund webhooks",
        repo: "quickpay",
      },
      { kind: "ticket", ref: "PAY-999", error: "not found in any source" },
      { kind: "task", text: "Per-user rate limit", repo: "quickpay" },
    ],
    name_suggestion: "Q4 payments",
  };

  it("the server's reading when it has one; an unresolved ticket stays red", () => {
    const rows = itemRows([], preview, "fallback", (k) => "<" + k + ">");
    expect(rows.map((r) => [r.kind, r.ref, r.title, r.where, r.error])).toEqual([
      ["ticket", "PAY-412", "Retry refund webhooks", "<jira-payments>", ""],
      ["ticket", "PAY-999", "", "", "not found in any source"],
      ["task", "", "Per-user rate limit", "quickpay", ""],
    ]);
  });

  it("placeholders from the local reading while the server answers", () => {
    const rows = itemRows(localItems("PAY-1\ndo it"), null, "quickpay");
    expect(rows.map((r) => [r.kind, r.pending, r.where])).toEqual([
      ["ticket", true, ""],
      ["task", false, "quickpay"],
    ]);
  });

  it("✕ takes a task's line, or a ticket's token (and an emptied line)", () => {
    const text = "PAY-412 PAY-415\nRate limit\nPAY-419";
    expect(removeItem(text, { kind: "task", token: "Rate limit" })).toBe("PAY-412 PAY-415\nPAY-419");
    expect(removeItem(text, { kind: "ticket", token: "PAY-412" })).toBe("PAY-415\nRate limit\nPAY-419");
    expect(removeItem(text, { kind: "ticket", token: "PAY-419" })).toBe("PAY-412 PAY-415\nRate limit");
    expect(removeItem("A-1, A-2, A-3", { kind: "ticket", token: "A-2" })).toBe("A-1, A-3");
  });

  it("never sends an unresolved row, and sends only §5's fields", () => {
    expect(requestItems(preview)).toEqual([
      { kind: "ticket", source: "jira-payments", id: "PAY-412" },
      { kind: "task", text: "Per-user rate limit" },
    ]);
  });
});

describe("POST /api/runs body (SPEC §5)", () => {
  it("matches the contract", () => {
    expect(
      runBody({
        name: " Q4 payments ",
        items: [{ kind: "task", text: "x" }],
        lane: "pr",
        askFirst: false,
        grouping: "each",
        concurrency: 3,
        program: "claude",
        repoPath: "/r/quickpay ",
        split: false,
      })
    ).toEqual({
      name: "Q4 payments",
      items: [{ kind: "task", text: "x" }],
      policy: { lane: "pr", ask_first: false, grouping: "each", release: "ask" },
      concurrency: 3,
      program: "claude",
      repo_path: "/r/quickpay",
      split: false,
    });
  });

  it("ask_first means nothing on Leave it; a split is always together; pace is 1–8", () => {
    const b = runBody({
      name: "n",
      items: [],
      lane: "leave",
      askFirst: true,
      grouping: "each",
      concurrency: 40,
      program: "claude",
      repoPath: "/r",
      split: true,
    });
    // One for all commits each line into the group's branch: "Leave it" is
    // sent as what it really does (commit — nothing leaves this machine), and
    // "ask first" is the group's release, which always asks.
    expect(b.policy).toEqual({ lane: "commit", ask_first: false, grouping: "together", release: "ask" });
    expect(b.concurrency).toBe(8);
    expect(clampConcurrency(0)).toBe(1);
    expect(clampConcurrency(Number.NaN)).toBe(3);
  });

  it("a batch starts on the server's lane, else the fast-track default, else a PR", () => {
    expect(defaultLaneFor(true, "commit", "merge")).toBe("commit");
    expect(defaultLaneFor(true, "", "merge")).toBe("merge");
    expect(defaultLaneFor(true, "", "agent")).toBe("leave");
    expect(defaultLaneFor(true, undefined, undefined)).toBe("pr");
  });

  it("the button says how many, or that a split starts its lead", () => {
    expect(startLabel(6, false)).toBe("Start 6 sessions");
    expect(startLabel(1, true)).toBe("Start the lead");
  });

  it("a name when the server suggested none", () => {
    expect(fallbackName(localItems("Per-user rate limit on the webhooks\nb"))).toBe(
      "Per-user rate limit on"
    );
    expect(fallbackName(localItems("PAY-1 PAY-2"))).toBe("PAY tickets");
    expect(fallbackName(localItems("PAY-1 #3"))).toBe("Batch");
  });
});

describe("the sentence", () => {
  const base = { n: 6, concurrency: 3, askFirst: false, grouping: "each" as const, split: false };

  it("the mockup's: six, three at a time, one PR each", () => {
    expect(summarySentence({ ...base, lane: "pr" })).toEqual({
      lead:
        "6 sessions, 3 at a time. Each one is committed with a message written from its diff, " +
        "pushed and opened as its own PR once its agent stops and your hooks pass.",
      tail: "Nothing merges; you'll see each PR in the Outbox.",
    });
  });

  it("all at once when the pace covers the list; one shared branch for one-for-all", () => {
    const s = summarySentence({ ...base, n: 2, lane: "pr", grouping: "together" })!;
    expect(s.lead).toMatch(/^2 sessions, all at once\./);
    expect(s.lead).toMatch(/one shared branch/);
    expect(s.lead).toMatch(/opens one PR/);
  });

  it("ask first is said; a split names its lead and the Thread tab", () => {
    expect(summarySentence({ ...base, lane: "commit", askFirst: true })!.tail).toMatch(
      /asks you in the Outbox/
    );
    const sp = summarySentence({ ...base, n: 1, lane: "pr", split: true })!;
    expect(sp.lead).toMatch(/^One lead session/);
    expect(sp.lead).toMatch(/Thread tab/);
  });

  it("one for all never claims nothing is committed, and never promises a per-commit ask", () => {
    const leave = summarySentence({ ...base, n: 3, lane: "leave", grouping: "together" })!;
    expect(leave.lead + leave.tail).not.toMatch(/Nothing is committed/);
    expect(leave.lead).toMatch(/committed as it finishes/);
    const asked = summarySentence({ ...base, n: 3, lane: "commit", askFirst: true, grouping: "together" })!;
    expect(asked.tail).not.toMatch(/Before the first commit/);
    const split = summarySentence({ ...base, n: 1, lane: "pr", askFirst: true, split: true })!;
    expect(split.tail).not.toMatch(/Before the first commit/);
  });

  it("one line with a lane says what happens to it", () => {
    expect(summarySentence({ ...base, n: 1, lane: "commit" })!.lead).toMatch(/commits it/);
  });
});

describe("Intake → Start together", () => {
  it("one line: the tracker's ID, else the slug, else the link", () => {
    expect(
      startTogetherText([
        { slug: "jira-PAY-412", id: "PAY-412" },
        { slug: "sc-21", id: "21" },
        { slug: "Fix the thing", url: "https://app.shortcut.com/x/story/9", id: "9" },
        { slug: "", id: 77 },
      ])
    ).toBe("PAY-412 sc-21 https://app.shortcut.com/x/story/9 77");
  });
});
