/** Intake → Tickets → Merge into…: the two rules the drawer renders off.
 *
 * `mergeOutcome` is the one that earns a test file. A merge is four writes
 * against somebody else's tracker and the server reports each independently,
 * so there is no single boolean to render — and the difference between
 * "finished" and "the duplicate is still sitting there, go and delete it" is
 * the whole reason the feature can be trusted. A green tick over an un-deleted
 * ticket is the one thing this surface must never say.
 */

import { describe, expect, it } from "vitest";
import { mergeOutcome, mergeTargets, type MergeResult } from "../components/intake/merge";

const ROW = { source: "sc", id: "41", slug: "sc-41", name: "Login spinner" };

const ALL = [
  ROW,
  { source: "sc", id: "38", slug: "sc-38", name: "Spinner bug", bucket: "In Progress" },
  { source: "sc", id: "12", slug: "sc-12", name: "Dark mode", bucket: "Backlog" },
  { source: "jira", id: "PROJ-1", slug: "jira-PROJ-1", name: "Spinner elsewhere" },
];

function result(over: Partial<MergeResult> = {}): MergeResult {
  return {
    from: { id: "41", slug: "sc-41" },
    into: { id: "38", slug: "sc-38" },
    comments_copied: 0,
    attachments_moved: [],
    attachments_failed: [],
    attachments_linked: [],
    deleted: true,
    delete_error: "",
    ...over,
  };
}

describe("mergeTargets", () => {
  it("offers the other tickets on the same source", () => {
    expect(mergeTargets(ALL, ROW, "").map((t) => t.slug)).toEqual(["sc-38", "sc-12"]);
  });

  it("never offers the ticket itself", () => {
    expect(mergeTargets(ALL, ROW, "").some((t) => t.id === ROW.id)).toBe(false);
  });

  it("never offers another tracker's tickets", () => {
    // The server refuses a cross-source merge — the files cannot follow a
    // ticket into another tracker and the survivor belongs to a different
    // queue's repo and agent. Filtering here is what stops the picker OFFERING
    // something the server would reject.
    expect(mergeTargets(ALL, ROW, "").some((t) => t.source === "jira")).toBe(false);
  });

  it("narrows on slug, title and state, all tokens required", () => {
    expect(mergeTargets(ALL, ROW, "spinner").map((t) => t.slug)).toEqual(["sc-38"]);
    expect(mergeTargets(ALL, ROW, "backlog").map((t) => t.slug)).toEqual(["sc-12"]);
    expect(mergeTargets(ALL, ROW, "sc-38")).toHaveLength(1);
    expect(mergeTargets(ALL, ROW, "spinner backlog")).toHaveLength(0);
  });
});

describe("mergeOutcome", () => {
  it("is only ok when the content landed AND the original is gone", () => {
    const { tone, text } = mergeOutcome(result());
    expect(tone).toBe("ok");
    expect(text).toContain("sc-41 merged into sc-38");
    expect(text).toContain("deleted");
  });

  it("counts what came across", () => {
    const { text } = mergeOutcome(
      result({ comments_copied: 2, attachments_moved: ["a.png", "b.log"] }),
    );
    expect(text).toContain("2 comments");
    expect(text).toContain("2 files");
  });

  it("says 'file links' when the files stayed put but are still reachable", () => {
    // GitHub and Linear uploads outlive the ticket they were posted on, so
    // nothing moves and nothing failed. A silent zero there would read as loss.
    const { tone, text } = mergeOutcome(result({ attachments_linked: ["shot.png"] }));
    expect(tone).toBe("ok");
    expect(text).toContain("1 file link");
  });

  it("warns, and says what to do, when the tracker refused the delete", () => {
    const { tone, text } = mergeOutcome(
      result({ deleted: false, delete_error: "needs admin on the repo" }),
    );
    expect(tone).toBe("warn");
    expect(text).toContain("could NOT be deleted");
    expect(text).toContain("needs admin on the repo");
    expect(text).toContain("by hand");
  });

  it("warns when files were lost with the deleted ticket", () => {
    const { tone, text } = mergeOutcome(result({ attachments_failed: ["trace.log"] }));
    expect(tone).toBe("warn");
    expect(text).toContain("trace.log");
    expect(text).toContain("gone with it");
  });

  it("reports a refused delete even when files also failed", () => {
    // The un-deleted duplicate is the bigger problem and the one with an
    // action attached, so it is the sentence that gets shown.
    const { text } = mergeOutcome(
      result({ deleted: false, delete_error: "403", attachments_failed: ["x.png"] }),
    );
    expect(text).toContain("could NOT be deleted");
  });
});
