/** Intake → Tickets "+ Add source": ‹ Back and a tab switch keep the draft
 * (a pasted token included), and overlapping saves settle per card
 * (components/intake/addDraft.ts). */

import { describe, expect, it } from "vitest";
import type { TicketingSource } from "../api/types";
import {
  backStep,
  beginSave,
  continueWith,
  finishRefusal,
  restoreStash,
  settleSave,
  stashOf,
} from "../components/intake/addDraft";
import { code, squash } from "./sourceText";

const src = (id: string, extra: Record<string, string> = {}) =>
  ({ id, provider: "shortcut", ...extra }) as unknown as TicketingSource;

describe("the draft survives Back and a tab switch", () => {
  it("Back goes one step back — step 2 to the tiles, not out of the flow", () => {
    expect(backStep(3)).toBe(2);
    expect(backStep(2)).toBe(1);
  });

  it("Continue on the same tracker resumes the draft; another replaces it", () => {
    const d = { id: "sc", step: 1 as const };
    expect(continueWith(d, "shortcut", "shortcut")).toBe("resume");
    expect(continueWith(d, "shortcut", "jira")).toBe("replace");
    expect(continueWith(null, undefined, "jira")).toBe("replace");
    expect(continueWith(d, "shortcut", "repo")).toBe("repo");
  });

  it("an unsaved draft is stashed with what was typed, and comes back", () => {
    const list = [src("old"), src("sc", { api_token: "tok" })];
    const stash = stashOf(list, { id: "sc", step: 2 }, new Set(["old"]));
    expect(stash).toEqual({ source: src("sc", { api_token: "tok" }), step: 2 });
    const back = restoreStash([src("old")], stash);
    expect(back.draft).toEqual({ id: "sc", step: 2 });
    expect(back.list.map((s) => s.id)).toEqual(["old", "sc"]);
  });

  it("nothing is stashed for a stored source, and a taken id isn't doubled", () => {
    expect(stashOf([src("sc")], { id: "sc", step: 3 }, new Set(["sc"]))).toBeNull();
    expect(stashOf([src("sc")], null, new Set())).toBeNull();
    const r = restoreStash([src("sc")], { source: src("sc"), step: 2 });
    expect(r.draft).toBeNull();
    expect(r.list).toHaveLength(1);
  });

  it("the tab wires it: Back keeps the draft, the dialog holds the stash", () => {
    const t = squash(code("components/intake/TicketsTab.tsx"));
    expect(t).toContain("setDraft({ id: draft.id, step: backStep(draft.step) });");
    expect(t).not.toContain('className="ik-quiet-btn" onClick={dropDraft}');
    expect(t).toContain("restoreStash(sources, draftStash?.current ?? null)");
    expect(squash(code("components/intake/IntakeDialog.tsx"))).toContain("draftStash.current = null;");
  });
});

describe("overlapping saves settle per card", () => {
  it("an older save landing after a newer one started still marks its card saved", () => {
    let seqs = beginSave({ card: {}, ok: 0 }, 1, ["a"]);
    seqs = beginSave(seqs, 2, ["b"]);
    const r = settleSave(seqs, 1, ["a"], true);
    expect(r.saved).toEqual(["a"]);
    expect(r.seqs.ok).toBe(1);
  });

  it("a card a newer save carries is left to that save", () => {
    let seqs = beginSave({ card: {}, ok: 0 }, 1, ["a"]);
    seqs = beginSave(seqs, 2, ["a"]);
    expect(settleSave(seqs, 1, ["a"], true).saved).toEqual([]);
    expect(settleSave(seqs, 1, ["a"], false).failed).toEqual([]);
  });

  it("a late failure after a newer success is not a failure", () => {
    let seqs = beginSave({ card: {}, ok: 0 }, 1, ["a"]);
    seqs = beginSave(seqs, 2, ["b"]);
    seqs = settleSave(seqs, 2, ["b"], true).seqs;
    const late = settleSave(seqs, 1, ["a"], false);
    expect(late.failed).toEqual([]);
    expect(late.saved).toEqual(["a"]);
  });

  it("a plain failure marks its cards failed", () => {
    const seqs = beginSave({ card: {}, ok: 0 }, 1, ["a"]);
    expect(settleSave(seqs, 1, ["a"], false)).toMatchObject({ saved: [], failed: ["a"] });
  });

  it("Finish says what failed when nothing is marked", () => {
    const fix = "Fix the marked fields first.";
    expect(finishRefusal({ ok: false, fieldErrors: { sc: { api_token: "bad" } } }, "sc", fix)).toBe(fix);
    expect(finishRefusal({ ok: false, fieldErrors: {}, message: "Failed to fetch" }, "sc", fix)).toBe(
      "Not saved — Failed to fetch. Try again."
    );
  });
});
