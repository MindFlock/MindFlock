/** Whether a session is a verify run — the session-identity half of Verify.
 *
 * Split out of verify.ts because it answers a different question from the
 * checklist rules there: not "what is this plan asking of you?" but "is this
 * SESSION one of Verify's own?", which the store, the grid, the window names and
 * the prompt targets ask about every session whether or not the dialog was ever
 * opened. verify.ts re-exports all of it, so either import path works. */

/** The prefix `POST /api/test-plans/{id}/run` builds every verify session's
 * title from (`server.run_test_plan`: `title = "verify-%s" % plan_id`).
 *
 * NOT the signal any more — `hidden_by` on the row is (below). Kept for the
 * legacy fallback in `isVerifySession` and for the places that only ever see a
 * TITLE (an event envelope about a session that failed to start). */
export const VERIFY_SESSION_PREFIX = "verify-";

/** The `hidden_by` value the server records on a checklist run
 * (`POST /api/instances` with `hidden_by: "verify"`, which `run_test_plan`
 * sends). The row carries it back on every `GET /api/instances` entry. */
export const VERIFY_HIDDEN_BY = "verify";

/** Whatever a caller has in hand: a whole row, or just a title. */
export type VerifySessionLike =
  | string
  | { title?: string | null; hidden_by?: string | null };

/** Whether a session is a verify run rather than a piece of the user's work.
 *
 * WHY THE SIDEBAR CARES. A verify run is a real session — it needs a worktree,
 * a checked-out tree and an agent that can actually run commands, and none of
 * that can be faked in a chat pane. But it is not *work*: it is a window you
 * open to watch something for two minutes and then close, like the assistant.
 * Listing it in the left rail put it alongside the branches a person is
 * actually building, where it accumulates, has to be manually deleted, and
 * makes a flock of four sessions look like a flock of eight.
 *
 * So it lives in the grid and not in the rail: `computeVisible` still sees it
 * (that is what gives it a pane), and only the sidebar filters it out. The
 * Verify dialog is where it is reachable — every plan whose session exists
 * offers to open or end it, so nothing is ever stranded by being unlisted.
 *
 * HOW IT IS RECOGNISED. By the row's `hidden_by`, the flag the server records
 * when Verify creates the session. The title used to be the signal, and that
 * hid any session a person happened to name `verify-foo` — no rail row, no
 * pane, no number — with nothing to say why. A server that sends the field
 * is authoritative: `"verify"` hides, anything else (including `""`) lists.
 *
 * LEGACY FALLBACK — the prefix check survives for exactly two callers:
 *   1. a row with NO `hidden_by` at all (`undefined`): an older server, or a
 *      paired device still running one, doesn't send the field. (Runs created
 *      before the flag on an upgraded server don't need it: the server
 *      backfills them at startup from the checklists that name them.)
 *   2. a bare title: an event envelope names the session, with no row to ask.
 * A user-named `verify-foo` on a current server has `hidden_by: ""`, takes the
 * authoritative branch, and stays visible. */
export function isVerifySession(session: VerifySessionLike): boolean {
  if (typeof session !== "string") {
    const by = session?.hidden_by;
    if (by !== undefined && by !== null) return by === VERIFY_HIDDEN_BY;
  }
  // Legacy fallback (see above): no flag to read, so the name has to do.
  const title = typeof session === "string" ? session : session?.title;
  return (title || "").startsWith(VERIFY_SESSION_PREFIX);
}
