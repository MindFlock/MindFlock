/** The merged Recently-closed page's pure half — row identity and, above all,
 * the words its destructive sweep asks permission with.
 *
 * vitest here has no DOM, so the dialog itself is never rendered; these
 * functions are where the copy of an irreversible action can be pinned at all.
 * The server-side guarantees are asserted in tests/unit/test_workspaces.py. */

import { describe, it, expect } from "vitest";
import {
  dirNote,
  dirtyMessage,
  entryLabel,
  nothingMessage,
  pruneMessage,
  prunedMessage,
  rowSearchFields,
  rowSortValue,
  staleDays,
  sumBytes,
  whenText,
  type PruneResult,
  type RecentRow,
} from "../components/dialogs/recentRows";

const row = (over: Partial<RecentRow> = {}): RecentRow => ({
  id: "id-1",
  source: "closed",
  name: "feature/x_ab12",
  path: "/w/feature/x_ab12",
  folder: "/w/feature/x_ab12",
  kind: "worktree",
  worktree: true,
  exists: true,
  ...over,
});

const result = (over: Partial<PruneResult> = {}): PruneResult => ({
  ok: true,
  dry_run: true,
  days: 7,
  candidates: [],
  candidate_count: 0,
  total_bytes: 0,
  dirty_count: 0,
  kept: { active: [], recent: 0, not_worktree: 0, protected: 0 },
  ...over,
});

describe("entryLabel", () => {
  it("prefers the saved name, then the branch, then the title", () => {
    const e = row({ branch: "feature/big-thing", title: "shortcut-21018" });
    expect(entryLabel(e, "rapisynth")).toBe("rapisynth");
    expect(entryLabel(e)).toBe("feature/big-thing");
    expect(entryLabel(row({ title: "shortcut-21018" }))).toBe("shortcut-21018");
  });

  it("falls back to the directory name for a row that was never a session", () => {
    // The whole point of the merge: a leftover directory has no session
    // identity at all, and "(untitled)" would be a worse answer than its path.
    expect(entryLabel(row({ source: "disk", name: "pr-2623", title: null }))).toBe(
      "pr-2623"
    );
  });
});

describe("dirNote", () => {
  it("names the directory when the headline does not", () => {
    // Eight closed in-place sessions all read "main" otherwise.
    const e = row({ branch: "main", path: "/home/u/sitecheck-bot7", kind: "in-place" });
    expect(dirNote(e)).toBe("sitecheck-bot7");
  });

  it("stays quiet when the headline already contains it", () => {
    expect(dirNote(row({ name: "feature/x_ab12", path: "/w/feature/x_ab12" }))).toBe("");
  });
});

describe("rowSortValue", () => {
  it("dates closed sessions and leftover directories on ONE axis", () => {
    const closed = row({ last_used: 1000 });
    const disk = row({ source: "disk", last_used: 2000 });
    expect(rowSortValue(closed, "date")).toBe(1000);
    expect(rowSortValue(disk, "date")).toBe(2000);
  });

  it("returns null for a size that has not been computed yet", () => {
    // sortRows parks nulls at the bottom, so the list holds its order until the
    // ?sizes=1 pass lands instead of flickering row by row.
    expect(rowSortValue(row(), "size")).toBeNull();
    expect(rowSortValue(row({ size_bytes: 4096 }), "size")).toBe(4096);
  });
});

describe("rowSearchFields", () => {
  it("makes every badge the row SHOWS searchable", () => {
    const fields = rowSearchFields(
      row({ exists: false, in_place: true, provisioned: true, stale: true }),
      "saved"
    ).filter(Boolean);
    for (const word of ["saved", "in-place", "provisioned", "worktree gone", "unused"]) {
      expect(fields).toContain(word);
    }
  });
});

describe("sumBytes", () => {
  it("counts a shared directory once", () => {
    // A session and its copy produce two closed rows for ONE worktree (the
    // store dedupes on folder AND title), each sized by its own du.
    const rows = [
      row({ id: "a", size_bytes: 100 }),
      row({ id: "b", size_bytes: 100 }),
      row({ id: "c", path: "/w/other", size_bytes: 5 }),
    ];
    expect(sumBytes(rows)).toBe(105);
  });
});

describe("staleDays", () => {
  it("reports whole days since the last signal", () => {
    const now = 10_000 * 86400 * 1000;
    expect(staleDays(row({ last_used: 10_000 * 86400 - 9 * 86400 }), now)).toBe(9);
    expect(staleDays(row({ last_used: null }), now)).toBeNull();
  });
});

describe("whenText", () => {
  it("dates a closed session by its close and a directory by its last touch", () => {
    const closedAt = new Date(Date.now() - 3 * 86400_000).toISOString();
    expect(whenText(row({ closed_at: closedAt }))).toBe("closed 3d ago");
    expect(
      whenText(row({ source: "disk", last_used: Date.now() / 1000 - 2 * 86400 }))
    ).toBe("used 2d ago");
  });

  it("says nothing rather than guessing when there is no timestamp", () => {
    expect(whenText(row({ closed_at: null }))).toBe("");
  });
});

describe("nothingMessage", () => {
  it("says WHY nothing was found, per rule", () => {
    const msg = nothingMessage(
      result({
        kept: {
          active: ["busy"],
          recent: 3,
          not_worktree: 2,
          outside_root: 1,
          protected: 1,
        },
      })
    );
    expect(msg).toContain("No unused worktrees to remove");
    expect(msg).toContain("busy");
    expect(msg).toContain("3 used within the last 7 days");
    // The hard rule, said out loud in the one place a user asks "why not that
    // one?" — a repo or a clone is never removed.
    expect(msg).toContain("not a worktree");
    expect(msg).toContain("never removes");
    // A worktree the user cut in their own repo is a different refusal, and the
    // message has to distinguish it from "we could not find it".
    expect(msg).toContain("outside MindFlock's own worktrees folder");
  });
});

describe("pruneMessage", () => {
  const r = result({
    candidates: [
      { name: "feature/a_1", path: "/w/a", size_bytes: 1024 },
      { name: "feature/b_2", path: "/w/b", size_bytes: 2048, dirty: true },
    ],
    candidate_count: 2,
    total_bytes: 3072,
    dirty_count: 1,
  });

  it("names every row it will delete", () => {
    const msg = pruneMessage(r);
    expect(msg).toContain("feature/a_1");
    expect(msg).toContain("feature/b_2");
    expect(msg).toContain("Delete 2 unused worktrees");
  });

  it("promises what can never be deleted, and what a delete costs", () => {
    const msg = pruneMessage(r);
    expect(msg).toContain("never a repository");
    expect(msg).toContain("7 days");
    expect(msg).toContain("commits stay in the repository");
    expect(msg).toContain("uncommitted changes");
    // The dirty check cannot see git-ignored content (every worktree has some),
    // so the confirmation has to say that it goes too — this is the only place
    // the user is told.
    expect(msg).toContain("Files git ignores");
    expect(msg).toContain("cannot be undone");
  });
});

describe("dirtyMessage", () => {
  it("offers all-or-only-the-clean when both kinds are present", () => {
    const msg = dirtyMessage(
      result({
        candidates: [
          { name: "clean_1", path: "/w/1" },
          { name: "clean_2", path: "/w/2" },
          { name: "dirty_3", path: "/w/3", dirty: true },
        ],
        dirty_count: 1,
      })
    );
    expect(msg).toContain("dirty_3");
    expect(msg).toContain("OK — delete all 3.");
    expect(msg).toContain("Cancel — delete only the 2");
  });

  it("asks plainly when every candidate holds uncommitted work", () => {
    const msg = dirtyMessage(
      result({
        candidates: [{ name: "dirty_1", path: "/w/1", dirty: true }],
        dirty_count: 1,
      })
    );
    expect(msg).toContain("Delete it anyway?");
    expect(msg).toContain("Committed work stays on the branch");
  });
});

describe("prunedMessage", () => {
  it("reports what happened, including what it held back", () => {
    const msg = prunedMessage(
      result({
        dry_run: false,
        removed: ["a", "b"],
        removed_count: 2,
        freed_bytes: 1024,
        kept_dirty: ["c"],
      })
    );
    expect(msg).toContain("Removed 2 worktrees");
    expect(msg).toContain("1.0 KB freed");
    expect(msg).toContain("kept 1 with uncommitted work");
  });
});
