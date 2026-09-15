/** Merging one ticket into another: which tickets may receive it, and what to
 * say once it has happened.
 *
 * Pure, and split out of TicketsTab for the same reason `search.ts` and
 * `queue.ts` are: the component is hundreds of lines of provider plumbing, and
 * the two rules worth pinning are four lines each. The node-only vitest suite
 * can reach them here.
 *
 * The second rule is the one that earns its keep. A merge is FOUR writes
 * against somebody else's tracker (append, carry files, comment, delete) and
 * the server reports each independently, because the order it runs them in
 * guarantees that a late failure is survivable rather than destructive (see
 * backend/web/core/ticket_merge.py). So there is no single boolean to render:
 * "merged and deleted", "merged but the original is still there", and "merged,
 * deleted, but two files did not come along" are three different things that
 * the person who pressed the button has to be told apart, and only one of them
 * means they are finished.
 */

/** The subset of an assigned-ticket row this module needs. */
export interface MergeCandidate {
  source: string;
  id: string;
  slug: string;
  name?: string;
  bucket?: string;
  merge_ready?: boolean;
}

/** What POST /api/tickets/merge answers with. */
export interface MergeResult {
  from: { id: string; slug: string; name?: string; url?: string };
  into: { id: string; slug: string; name?: string; url?: string };
  comments_copied?: number;
  attachments_moved?: string[];
  attachments_failed?: string[];
  /** Files that stayed where they are and are still reachable from the copied
   * links — a provider whose uploads outlive the ticket they were posted on. */
  attachments_linked?: string[];
  comment_error?: string;
  deleted?: boolean;
  delete_error?: string;
}

/** The tickets `row` may be merged INTO, narrowed by `query`.
 *
 * Same source only, and the server enforces that too — merging across trackers
 * would delete a Jira issue into a Shortcut story, where the files cannot
 * follow and the survivor belongs to a different queue's repo and agent. Doing
 * the filter here as well is not belt-and-braces: it is what stops the picker
 * OFFERING a merge the server would refuse.
 *
 * Ordering is the caller's — the panel hands rows in the order it renders them
 * (bucket by bucket, newest first), which is the order the user just read.
 */
export function mergeTargets(
  all: MergeCandidate[],
  row: MergeCandidate,
  query: string,
): MergeCandidate[] {
  const tokens = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
  return all.filter((t) => {
    if (t.source !== row.source) return false;
    if (String(t.id) === String(row.id)) return false;
    if (!tokens.length) return true;
    const hay = [t.slug, t.name, t.bucket].join(" ").toLowerCase();
    return tokens.every((tok) => hay.includes(tok));
  });
}

/** What actually happened, as one sentence and a tone.
 *
 * `tone` is "ok" only when the whole thing landed: the content is on the
 * survivor AND the original is gone. Anything left undone — a tracker that
 * refused the delete, a file that would not copy — is "warn", because it means
 * there is still something for a person to do, and a green tick over an
 * un-deleted duplicate is exactly the lie this surface cannot tell.
 */
export function mergeOutcome(r: MergeResult): { tone: "ok" | "warn"; text: string } {
  const moved = r.attachments_moved || [];
  const failedFiles = r.attachments_failed || [];
  const linked = r.attachments_linked || [];
  const carried: string[] = [];
  if (r.comments_copied) {
    carried.push(r.comments_copied + " comment" + (r.comments_copied === 1 ? "" : "s"));
  }
  if (moved.length) carried.push(moved.length + " file" + (moved.length === 1 ? "" : "s"));
  else if (linked.length) {
    carried.push(
      linked.length + " file link" + (linked.length === 1 ? "" : "s"),
    );
  }
  const what = carried.length ? " with its " + carried.join(" and ") : "";

  if (!r.deleted) {
    return {
      tone: "warn",
      text:
        r.from.slug +
        " was copied into " +
        r.into.slug +
        what +
        ", but it could NOT be deleted — " +
        (r.delete_error || "the tracker gave no reason") +
        ". Delete it by hand, or the duplicate stays in the queue.",
    };
  }
  if (failedFiles.length) {
    return {
      tone: "warn",
      text:
        r.from.slug +
        " was merged into " +
        r.into.slug +
        " and deleted, but " +
        failedFiles.length +
        " file" +
        (failedFiles.length === 1 ? "" : "s") +
        " could not be carried over (" +
        failedFiles.join(", ") +
        ") — they are gone with it.",
    };
  }
  return {
    tone: "ok",
    text: r.from.slug + " merged into " + r.into.slug + what + ", and deleted.",
  };
}
