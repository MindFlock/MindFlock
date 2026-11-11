/** Intake → GitHub PRs: "Run PR review" on an open PR's row (owner request
 * 2026-11-11). It runs the team's saved PR review route on the PR by its link
 * (lib/prReview.ts): the server finds the PR's session — or opens one for its
 * branch — and starts the route there, or slots the review stops in after the
 * current stop when that session is already on a route. Works whether or not
 * the row already has a session, and while its agent is still working.
 *
 * Drawn in the row's action column (WorkItemRow's `actionExtra`), under Start
 * or Open session ›. An older server without the action hides it after its
 * first 404 (prReviewSupported). */

import { useEffect, useState } from "react";
import { onPrReviewSupport, prReviewSupported, startPrReview } from "../../lib/prReview";

/** What the row's button does: the review on the PR's link. Resolves whether
 * it started. Exported so the click is testable without a DOM. */
export async function reviewOpenPr(prUrl: string): Promise<boolean> {
  if (!prUrl) return false;
  return !!(await startPrReview({ prUrl }));
}

export function PrReviewAction({ prUrl, reference }: { prUrl?: string; reference: string }) {
  const [supported, setSupported] = useState(prReviewSupported);
  const [busy, setBusy] = useState(false);
  useEffect(() => onPrReviewSupport(() => setSupported(prReviewSupported())), []);
  if (!prUrl || !supported) return null;
  return (
    <button
      type="button"
      className="linklike ik-pr-review"
      data-pr-review={prUrl}
      disabled={busy}
      title={
        "Run your team's PR review route (Settings → Workspace → PR route) on " +
        reference +
        " — in its session, after the current stop if it is already on a route"
      }
      onClick={() => {
        setBusy(true);
        void reviewOpenPr(prUrl).finally(() => setBusy(false));
      }}
    >
      {busy ? "Starting review…" : "Run PR review"}
    </button>
  );
}
