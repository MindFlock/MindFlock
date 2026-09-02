/** What each Intake list searches, as one pure rule per list.
 *
 * The lists here are the ones that grow without bound — every open PR on every
 * watched repo, every issue, every assigned ticket in every workflow state —
 * and until now the only way to a particular row was scrolling past the rest.
 * Recently closed already solved that with a Ctrl+F
 * filter, and Verify's checklist list now shares it; these are the same
 * mechanism (``matchesTokens``: every whitespace-separated token has to appear
 * somewhere in the row) applied to the four Intake panels.
 *
 * SEPARATE FROM THE TABS on purpose. Each tab is a settings screen with a work
 * list inside it, hundreds of lines of provider-specific plumbing; the rule for
 * "does this row match" is four lines and is the only part worth pinning. The
 * node-only vitest suite can reach it here, which it cannot inside a component
 * that imports the settings context.
 *
 * WHAT GOES IN THE FIELD LIST is what the row SHOWS, plus the one thing it
 * shows as colour rather than words: whether auto ingestion has queued it. So
 * "queued" narrows a repo's list to what is about to start on its own, and a
 * skip reason ("already handled", "too new") is searchable in the words the row
 * prints. Anything the reader cannot see on the row is left out — a filter that
 * matches invisible text reads as broken.
 */

import { matchesTokens } from "../../lib/rowSearch";

/** The annotations every Intake row carries, whatever kind it is. */
interface Annotated {
  eligible?: boolean;
  reasons?: string[];
}

function annotations(item: Annotated): string[] {
  return [
    item.eligible ? "queued for auto" : "",
    ...(item.reasons || []),
  ];
}

export interface SearchableIssue extends Annotated {
  repo?: string;
  number: number;
  title?: string;
  author?: string;
}

export function issueMatches(issue: SearchableIssue, tokens: string[]): boolean {
  return matchesTokens(
    [
      issue.repo,
      "#" + issue.number,
      String(issue.number),
      issue.title,
      issue.author,
      ...annotations(issue),
    ],
    tokens,
  );
}

export interface SearchablePr extends Annotated {
  repo?: string;
  number: number;
  title?: string;
  author?: string;
  head_ref?: string;
  base_ref?: string;
}

export function prMatches(pr: SearchablePr, tokens: string[]): boolean {
  return matchesTokens(
    [
      pr.repo,
      "#" + pr.number,
      String(pr.number),
      pr.title,
      pr.author,
      // The branch names are on the row and are how a PR is often remembered —
      // "the one off feature/coupon-flow" — and `base_ref` is what a reviewer
      // asks about when a PR targets the wrong branch.
      pr.head_ref,
      pr.base_ref,
      ...annotations(pr),
    ],
    tokens,
  );
}

export interface SearchableTicket extends Annotated {
  source?: string;
  source_label?: string;
  id?: string;
  slug?: string;
  name?: string;
  bucket?: string;
  assignee?: string;
}

export function ticketMatches(ticket: SearchableTicket, tokens: string[]): boolean {
  return matchesTokens(
    [
      ticket.source,
      ticket.source_label,
      ticket.id,
      ticket.slug,
      ticket.name,
      // The workflow state is a HEADING the row sits under rather than text on
      // the row itself, and it is the most useful thing to narrow by after the
      // name: "shortcut ready for dev" is a question people actually have.
      ticket.bucket,
      // Only present on a source that ingests anyone's tickets — which is
      // exactly when "whose is this?" is worth filtering on.
      ticket.assignee,
      ...annotations(ticket),
    ],
    tokens,
  );
}

export interface SearchableQueued {
  reference: string;
  title: string;
  group: string;
  state?: string;
  assignee?: string;
  kind?: string;
}

export function queuedMatches(item: SearchableQueued, tokens: string[]): boolean {
  return matchesTokens(
    [
      item.reference,
      item.title,
      item.group,
      item.state,
      item.assignee,
      // "tickets" / "prs" / "issues": the roll-up mixes all three, so the kind
      // is the one column the other lists do not need.
      item.kind,
      item.kind === "prs" ? "pull request" : "",
    ],
    tokens,
  );
}
