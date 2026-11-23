import { describe, expect, it } from "vitest";
import { destinationLine, pickDraft, sentLine, type FeedbackDraft } from "../lib/feedback";

const draft = (id: string, created: number): FeedbackDraft => ({
  id,
  title: "t-" + id,
  body: "b",
  kind: "bug",
  session: "",
  created,
  removed: 0,
});

describe("feedback", () => {
  it("says where a report goes and as whom before Send", () => {
    expect(destinationLine({ repo: "MindFlock/MindFlock", signed_in: true, login: "octo" })).toBe(
      "Posted publicly on GitHub (MindFlock/MindFlock) as octo."
    );
    expect(destinationLine({ repo: "MindFlock/MindFlock", signed_in: true, login: "" })).toBe(
      "Posted publicly on GitHub (MindFlock/MindFlock)."
    );
    expect(destinationLine({ repo: "MindFlock/MindFlock", signed_in: false, login: "" })).toContain("pre-filled GitHub page");
    expect(destinationLine(null)).toBe("");
  });

  it("every destination line says the report is public", () => {
    for (const signed_in of [true, false]) {
      expect(destinationLine({ repo: "r", signed_in, login: "" }).toLowerCase()).toContain("public");
    }
  });

  it("says what happened after Send", () => {
    expect(sentLine({ ok: true, via: "github", url: "u", number: 12 })).toBe("Thanks — filed as issue #12.");
    expect(sentLine({ ok: true, via: "browser", url: "u" })).toContain("press Submit there");
  });

  it("opens on the draft asked for, else the newest, else blank", () => {
    const drafts = [draft("new", 2), draft("old", 1)];
    expect(pickDraft(drafts, "old")?.id).toBe("old");
    expect(pickDraft(drafts, "gone")?.id).toBe("new");
    expect(pickDraft(drafts, null)?.id).toBe("new");
    expect(pickDraft([], "x")).toBeNull();
  });
});
