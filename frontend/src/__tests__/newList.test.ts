/** The New dialog's runs, pure half (lib/runStart): reading a batch's box,
 * the rows, ✕, the POST /api/runs body (SPEC §5), the auto-split's N, the
 * button and the sentence — and that a typed box is still exactly today's
 * single session. */
import { describe, expect, it } from "vitest";
import {
  clampConcurrency,
  clampSplit,
  defaultLaneFor,
  fallbackName,
  isTicketToken,
  itemRows,
  localItems,
  removeItem,
  requestItems,
  runBody,
  startLabel,
  startTogetherText,
  summarySentence,
  type PreviewResponse,
} from "../lib/runStart";
import { optionsSummary } from "../components/dialogs/NewList";

describe("one plain line is today's single session", () => {
  const one = localItems("fix the login bug in acme-api");

  it("keeps 'Create session', and starts Off whatever Settings says", () => {
    expect(one).toEqual([{ kind: "task", text: "fix the login bug in acme-api" }]);
    expect(startLabel(1)).toBe("Create session");
    // "Off unless I pick": a single session never reads the setting.
    for (const setting of ["merge", "pr", "commit", "off", "", undefined])
      expect(defaultLaneFor(false, setting), String(setting)).toBe("leave");
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

});

describe("auto-split into up to N", () => {
  it("N is at least 2 and never above the server's cap", () => {
    expect(clampSplit(3)).toBe(3);
    expect(clampSplit(1)).toBe(2);
    expect(clampSplit(12)).toBe(8);
    expect(clampSplit(6, 4)).toBe(4);
    expect(clampSplit(NaN, 8)).toBe(3);
  });

  it("is an optional split of the whole box, capped at N", () => {
    const body = runBody({
      name: "",
      items: [{ kind: "task", text: "line one\nline two" }],
      lane: "leave",
      askFirst: true,
      grouping: "together",
      concurrency: 3,
      program: "claude",
      repoPath: "/r",
      split: true,
      maxPieces: 4,
    });
    expect(body).toMatchObject({
      items: [{ kind: "task", text: "line one\nline two" }],
      split: true,
      split_optional: true,
      max_pieces: 4,
      // Off can't be a split's lane: Commit keeps it on this machine.
      policy: { lane: "commit", ask_first: false, grouping: "together", release: "ask" },
    });
  });

  it("a batch never carries the split's fields", () => {
    const body = runBody({
      name: "PAY tickets",
      items: [{ kind: "ticket", source: "jira", id: "PAY-1" }],
      lane: "pr",
      askFirst: false,
      grouping: "each",
      concurrency: 3,
      program: "",
      repoPath: "",
      split: false,
    });
    expect(body).not.toHaveProperty("split_optional");
    expect(body).not.toHaveProperty("max_pieces");
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

  it("a batch starts on the Settings default (never the preview's own), Off when unset", () => {
    // An explicit stored value keeps working exactly as before.
    expect(defaultLaneFor(true, "pr")).toBe("pr");
    expect(defaultLaneFor(true, "commit")).toBe("commit");
    expect(defaultLaneFor(true, "merge")).toBe("merge");
    // Unset (or anything that isn't a rung) is Off.
    expect(defaultLaneFor(true, "off")).toBe("leave");
    expect(defaultLaneFor(true, "agent")).toBe("leave");
    expect(defaultLaneFor(true, "")).toBe("leave");
    expect(defaultLaneFor(true, undefined)).toBe("leave");
    expect(defaultLaneFor(true, "junk")).toBe("leave");
  });

  it("the button says how many; an auto-split may stay one session", () => {
    expect(startLabel(6)).toBe("Start 6 sessions");
    expect(startLabel(1)).toBe("Create session");
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
      tail: "Nothing merges; each PR shows on its row.",
    });
  });

  it("points at the row a PR shows on, never at a log elsewhere", () => {
    // There is no Outbox to point at any more, and a session's PR was never
    // looked for there: the row's next-step chip already carries the link.
    expect(summarySentence({ ...base, lane: "merge" })!.tail).toBe("Each PR shows on its row.");
    for (const lane of ["leave", "commit", "push", "pr", "merge"] as const)
      for (const grouping of ["each", "together"] as const)
        for (const askFirst of [false, true])
          expect(
            JSON.stringify(summarySentence({ ...base, lane, grouping, askFirst }))
          ).not.toMatch(/Outbox/);
  });

  it("all at once when the pace covers the list; one shared branch for one-for-all", () => {
    const s = summarySentence({ ...base, n: 2, lane: "pr", grouping: "together" })!;
    expect(s.lead).toMatch(/^2 sessions, all at once\./);
    expect(s.lead).toMatch(/one shared branch/);
    expect(s.lead).toMatch(/opens one PR/);
  });

  it("ask first is said; an auto-split says it may not split, its N and the Thread tab", () => {
    expect(summarySentence({ ...base, lane: "commit", askFirst: true })!.tail).toMatch(
      /stops and asks you first, in the bell\./
    );
    const sp = summarySentence({ ...base, n: 1, lane: "pr", split: true, maxPieces: 4 })!;
    expect(sp.lead).toMatch(/^One session/);
    expect(sp.lead).toMatch(/it does the task itself/);
    expect(sp.lead).toMatch(/up to 4 pieces/);
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

describe("the single-session Options fold", () => {
  it("names the rung it is hiding, and says when it will ask first", () => {
    // A closed fold must still say what a start does: New's first page is
    // one box, and the summary is the only trace of the choice under it.
    expect(optionsSummary("leave", false)).toBe("Options · Fast-track: Off");
    expect(optionsSummary("pr", false)).toBe("Options · Fast-track: Open a PR");
    expect(optionsSummary("commit", true)).toBe("Options · Fast-track: Commit, asks first");
  });
});
