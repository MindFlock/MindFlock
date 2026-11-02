/** Waiting to fly: the Intake work items (tickets, pull requests, issues)
 * that are eligible and have no session yet — each one a flight that has not
 * taken off. Read from the same three cached panels and the same eligibility
 * rule (intake/queue.ts `collectQueued`) the Auto-start tab lists, so the
 * Board, the Auto-start tab and the top-bar badge can never disagree on what
 * is waiting. */

import { useMindflockStatus, usePanelQuery } from "../../state/queries";
import { collectQueued, type QueuePayloads, type QueuedItem } from "../intake/queue";
import { waitingAuto } from "../../lib/flights";

export interface Waiting {
  item: QueuedItem;
  /** A switch will start it by itself (false = only Fly by hand will). */
  auto: boolean;
}

/** `fetch` false reads the panels' cache only — the top bar counts what the
 * lists already hold and never fans out upstream itself (App's
 * useIntakeWarm keeps them warm). */
export function useWaiting(fetch = true): { waiting: Waiting[]; loaded: boolean; refresh(): void } {
  const tickets = usePanelQuery<NonNullable<QueuePayloads["tickets"]> & { stale?: boolean }>("tickets", { enabled: fetch });
  const prs = usePanelQuery<NonNullable<QueuePayloads["prs"]> & { stale?: boolean }>("github-prs", { enabled: fetch });
  const issues = usePanelQuery<NonNullable<QueuePayloads["issues"]> & { stale?: boolean }>("github-issues", { enabled: fetch });
  const status = useMindflockStatus().data;
  const items = collectQueued({
    tickets: tickets.data ?? null,
    prs: prs.data ?? null,
    issues: issues.data ?? null,
  });
  return {
    waiting: items.map((item) => ({ item, auto: waitingAuto(item.kind, status) })),
    loaded: !!(tickets.data || prs.data || issues.data),
    refresh: () => {
      tickets.refresh();
      prs.refresh();
      issues.refresh();
    },
  };
}
