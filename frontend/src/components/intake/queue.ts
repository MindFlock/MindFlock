/** What auto ingestion will pick up next, across all three intake sources.
 *
 * The three panels already answer this per item: the server computes
 * ``eligible`` for every ticket, PR and issue by running the SAME skip filters
 * the pipeline itself uses (see ticket_start.skip_reasons, pr_review, and
 * issue_start), and each tab renders it as a "queued for auto …" chip. What was
 * missing is the roll-up — the chips are scattered down three lists, grouped by
 * source and workflow state, so "what is about to start" meant opening three
 * tabs and reading past everything that ISN'T queued.
 *
 * This module is only the rule for which rows belong in that roll-up, kept pure
 * and separate from the tab so it can be tested and so the tab strip's badge and
 * the tab body can never disagree about the count.
 *
 * Deliberately NOT included:
 *
 * - Items already accepted by the pipeline. "queued for ingestion (pending)" is
 *   a skip REASON, so those items are not `eligible`; they have left the queue
 *   and become sessions, which is where you watch them.
 * - Items that already have a session. Ticket eligibility rules those out via
 *   the ledger, but PR and issue eligibility only consult the processed ledger,
 *   so an item manually started seconds ago can still read as eligible. It is
 *   being worked, so calling it "waiting to start" would be wrong.
 */

export type QueueKind = "tickets" | "prs" | "issues";

/** What a start endpoint needs to identify one item.
 *
 * A union rather than a bag of optional fields, so the start handler can't
 * post a ticket's id to the PR route and have it typecheck. */
export type QueueTarget =
  | { kind: "tickets"; source: string; id: string }
  | { kind: "prs"; repo: string; number: number }
  | { kind: "issues"; repo: string; number: number };

/** One row of the roll-up, flattened out of whichever panel it came from. */
export interface QueuedItem {
  kind: QueueKind;
  /** React key; unique across kinds. */
  key: string;
  /** How the item is named in its own system — "sc-1234", "org/repo#42". */
  reference: string;
  title: string;
  url?: string;
  /** Where it came from: a ticket source's label, or a repo. */
  group: string;
  /** ISO timestamp, for the age note. */
  created_at?: string;
  /** Only tickets have one, and only when the provider reports it. */
  state?: string;
  /** Whoever the item is assigned to, when that isn't you. A source set to
   * ingest anyone's tickets puts other people's work in this queue on purpose,
   * and a row that doesn't say so reads as your own. */
  assignee?: string;
  /** Identifies the item to its start endpoint, so a row here can be launched
   * by hand instead of waiting for the sweep. */
  target: QueueTarget;
}

/** The narrow shape this module reads. The tabs keep their own fuller
 * interfaces — this deliberately names only the fields the rule depends on. */
interface Annotated {
  eligible?: boolean;
  has_session?: boolean;
}

export interface QueueTicket extends Annotated {
  source?: string;
  id?: string;
  slug?: string;
  name?: string;
  url?: string;
  created_at?: string;
  bucket?: string;
  mine?: boolean;
  assignee?: string;
}

export interface QueuePr extends Annotated {
  repo?: string;
  number?: number;
  title?: string;
  url?: string;
  created_at?: string;
}

export interface QueueIssue extends QueuePr {}

export interface QueuePayloads {
  tickets?: { tickets?: QueueTicket[]; source_labels?: Record<string, string> } | null;
  prs?: { prs?: QueuePr[] } | null;
  issues?: { issues?: QueueIssue[] } | null;
}

/** Whether an annotated panel row is waiting for the automation to take it.
 *
 * `eligible` is the server's own answer — "the next sweep would start this" —
 * and is checked strictly against `true`: a panel from a server that predates
 * the field omits it entirely, and `undefined` must read as "unknown", never as
 * queued. Claiming a queue that isn't there is the one wrong answer here. */
export function isQueued(item: Annotated): boolean {
  return item.eligible === true && !item.has_session;
}

/** Oldest first — the queue's own order. Both the pipeline's backfill scanner
 * and the PR/issue monitors sort their eligible sets by creation time, so this
 * lists things in the order they will actually be picked up rather than in the
 * newest-first order the panels display for browsing. */
function oldestFirst<T extends { created_at?: string }>(rows: T[]): T[] {
  return rows.slice().sort((a, b) => {
    const ta = Date.parse(a.created_at || "");
    const tb = Date.parse(b.created_at || "");
    // Undated rows sort last rather than randomly: NaN comparisons are always
    // false, which would leave them wherever the input happened to put them.
    if (!isFinite(ta) && !isFinite(tb)) return 0;
    if (!isFinite(ta)) return 1;
    if (!isFinite(tb)) return -1;
    return ta - tb;
  });
}

function ref(repo: string | undefined, number: number | undefined): string {
  return (repo || "") + "#" + (number ?? "?");
}

/** The queued rows of one kind, normalised. */
export function queuedOf(kind: QueueKind, payloads: QueuePayloads): QueuedItem[] {
  if (kind === "tickets") {
    const labels = payloads.tickets?.source_labels || {};
    return oldestFirst((payloads.tickets?.tickets || []).filter(isQueued)).map((t) => ({
      kind,
      key: "tickets:" + (t.source || "") + ":" + (t.id ?? t.slug ?? ""),
      reference: t.slug || String(t.id ?? ""),
      title: t.name || "",
      url: t.url,
      group: labels[t.source || ""] || t.source || "Tickets",
      created_at: t.created_at,
      state: t.bucket,
      assignee: t.mine === false ? t.assignee || "someone else" : undefined,
      target: {
        kind: "tickets" as const,
        source: t.source || "",
        // /api/tickets/start takes the provider's own id; the slug is the
        // display name and is not interchangeable with it.
        id: String(t.id ?? ""),
      },
    }));
  }
  const rows =
    kind === "prs" ? payloads.prs?.prs || [] : payloads.issues?.issues || [];
  return oldestFirst(rows.filter(isQueued)).map((r) => ({
    kind,
    key: kind + ":" + (r.repo || "") + ":" + (r.number ?? ""),
    reference: ref(r.repo, r.number),
    title: r.title || "",
    url: r.url,
    group: r.repo || (kind === "prs" ? "Pull requests" : "Issues"),
    created_at: r.created_at,
    target: { kind, repo: r.repo || "", number: r.number ?? 0 },
  }));
}

/** Everything queued, in tab order: tickets, then PRs, then issues. */
export function collectQueued(payloads: QueuePayloads): QueuedItem[] {
  return [
    ...queuedOf("tickets", payloads),
    ...queuedOf("prs", payloads),
    ...queuedOf("issues", payloads),
  ];
}

/** Whether a kind's automation will actually act on its queue.
 *
 * Four states, because the remedies differ and a single "paused" would send you
 * to the wrong switch:
 *
 * - `unset` — nothing configured for this kind, so there is no queue to run.
 * - `off-engine` — the ingestion pipeline is stopped. This is the one that is
 *   easy to get wrong: `PRMonitor` and `IssueMonitor` are constructed by
 *   `PipelineOrchestrator`, so they only run while the pipeline does. Reading
 *   github.enabled alone would report "auto review is on" with the engine
 *   stopped — an item sitting there that nothing is coming for.
 * - `off-switch` — the engine runs, this kind's own switch is off.
 * - `on` — it will be picked up on the next sweep.
 *
 * For tickets, `off-switch` never occurs: the engine IS their switch. */
export type QueueRunState = "on" | "off-switch" | "off-engine" | "unset";

export function runState(opts: {
  /** Sources (tickets) or repos (PRs, issues) are configured. */
  configured: boolean;
  /** The ingestion engine is installed/reachable at all. */
  engineAvailable: boolean;
  /** The engine is running, or set to run. */
  engineOn: boolean;
  /** This kind's own switch, folded server-side with its repo list.
   * Tickets pass `true` — they have no switch beyond the engine. */
  switchOn: boolean;
}): QueueRunState {
  if (!opts.engineAvailable || !opts.configured) return "unset";
  if (!opts.switchOn) return "off-switch";
  if (!opts.engineOn) return "off-engine";
  return "on";
}

/** Group a kind's rows by source/repo, preserving the queue order within each
 * group and ordering the groups by their oldest item — so the group that will
 * be drawn from next is at the top. */
export function byGroup(items: QueuedItem[]): Array<{ group: string; items: QueuedItem[] }> {
  const out: Array<{ group: string; items: QueuedItem[] }> = [];
  for (const item of items) {
    const found = out.find((g) => g.group === item.group);
    if (found) found.items.push(item);
    else out.push({ group: item.group, items: [item] });
  }
  return out;
}
