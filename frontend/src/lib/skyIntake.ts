/** The Sky's intake lane, as data: the ticket ingestion pipeline drawn as one
 * block per configured source (docs/flights-intake-lane.md).
 *
 *   poll → eligible → fly → session
 *
 * A SOURCE BLOCK is one thing the pipeline polls — a ticket source, a PR repo
 * monitor, an issue repo monitor — with how it polls (the auto-start chip),
 * the saved stop its items fly by default, the items that are eligible and have no
 * session yet (the same `useWaiting` list the Board's Waiting to fly reads),
 * and the sessions it already started (the lane's arrow stubs, which the Sky
 * joins to each session's line).
 *
 * Pure, so the lane, its collapsed rail and the tests read one rule. */

import type { Instance, MindflockStatus } from "../api/types";
import type { QueuedItem } from "../components/intake/queue";
import { SOURCE_OFF_LABEL } from "./autopilot";
import { builtinPlanFor, flockPrRoute, sourceReach, waitingAuto, type FlightPlan, type FlockPrRoute, type WaitingKind } from "./flights";

/** The pipeline's poll defaults when nothing is set (backend/ticket_ingestion/
 * config.py; the same numbers the Auto-start tab shows): a ticket source every
 * 20 s, PR comments and new issues every 60 s. */
export const LANE_POLL_DEFAULT: Record<WaitingKind, number> = { tickets: 20, prs: 60, issues: 60 };

/** How many waiting chips a block lists before "n more". */
export const LANE_WAITING_SHOWN = 6;

/** One waiting item, as `useWaiting` returns it. */
export interface LaneWaiting {
  item: QueuedItem;
  auto: boolean;
}

/** A session a block started: the lane draws a stub out of the block
 * labelled with it, and the Sky draws the arrow from there to its line. */
export interface LaneStarted {
  title: string;
  /** The item's reference when the panel still lists it ("sc-12", "o/r#7"). */
  reference: string;
}

export interface SourceBlock {
  /** Unique across kinds: "tickets:<source id>", "prs:<owner/name>",
   * "issues:<owner/name>". */
  key: string;
  kind: WaitingKind;
  /** The source id (tickets) or owner/name (PRs, issues). */
  id: string;
  /** "Shortcut · quickpay", "PRs · owner/name", "Issues · owner/name". */
  label: string;
  /** The block's title: the label without the kind word its badge already
   * says ("Shortcut · quickpay", "owner/name"). */
  name: string;
  /** Intake's deep link for the block's ⋯ ("tickets:sc": the tab and the
   * source). */
  target: string;
  /** Its items start by themselves (the Board's "flies by itself"). */
  auto: boolean;
  /** How often its poll runs, in seconds. */
  every: number;
  /** "auto-start on · every 20 s" or "manual". */
  autoText: string;
  /** The saved stop its items fly: the source's own, else the kind's built-in
   * (cut at its depth). `words` is where its items go, in the words Intake's
   * "Goes as far as" uses ("Open PR", "Payments stops", "Stop after the
   * agent"), from lib/flights `sourceReach`. */
  flyway: { id: string; name: string; own: boolean; words?: string };
  /** Eligible, no session yet — oldest first (the queue's order). */
  waiting: LaneWaiting[];
  started: LaneStarted[];
  /** Not in settings: an item arrived from a source the settings doc does not
   * name (an older server, a source removed mid-poll). Drawn all the same so
   * nothing waiting is ever hidden. */
  stray?: boolean;
}

/** The narrow settings shape the lane reads (the `/api/settings` document). */
interface LaneSettings {
  ticketing?: { sources?: Array<Record<string, unknown>> };
  /** The flock's PR route (lib/flights flockPrRoute). */
  flights?: { pr_review_plan?: string };
  github?: {
    repos?: string[];
    issue_repos?: string[];
    poll_interval_seconds?: number;
    issue_poll_interval_seconds?: number;
    repo_settings?: Record<string, { plan?: string; depth?: string }>;
    issue_repo_settings?: Record<string, { plan?: string; depth?: string }>;
  };
}

/** The panels' rows carry the session title each item starts (or started)
 * as — the server's `session_title` — which is how a started session is tied
 * back to its source while the item is still listed. */
export interface LanePanels {
  tickets?: { tickets?: Array<{ source?: string; id?: string; slug?: string; session?: string; has_session?: boolean }>; source_labels?: Record<string, string> } | null;
  prs?: { prs?: Array<{ repo?: string; number?: number; session?: string; has_session?: boolean }> } | null;
  issues?: { issues?: Array<{ repo?: string; number?: number; session?: string; has_session?: boolean }> } | null;
}

export interface SourceBlockOpts {
  plans?: readonly Pick<FlightPlan, "id" | "name">[] | null;
  status?: Partial<MindflockStatus> | null;
  /** The ticketing catalog: provider id → its display name ("Shortcut"). */
  providers?: ReadonlyArray<{ id: string; label: string }> | null;
  panels?: LanePanels | null;
}

const KIND_WORD: Record<WaitingKind, string> = { tickets: "Tickets", prs: "PRs", issues: "Issues" };

/** "every 20 s" / "every minute" / "every 5 min". */
export function everyWords(seconds: number): string {
  const s = Math.max(1, Math.round(seconds));
  if (s < 60) return "every " + s + " s";
  if (s === 60) return "every minute";
  return "every " + Math.round(s / 60) + " min";
}

function positive(v: unknown): number {
  const n = Number(v);
  return Number.isFinite(n) && n > 0 ? n : 0;
}

/** The repo's last path segment, sanitised the way the server's `pr_slug` /
 * `issue_slug` do it — what PR and issue session titles are built from. */
export function repoSlugName(repo: string): string {
  const name = repo.replace(/\/+$/, "").split("/").pop() || "";
  return name.replace(/[^A-Za-z0-9._-]/g, "-");
}

/** The session title a PR / issue of this repo starts as, up to the number:
 * `pr-<name>-` / `issue-<name>-` (backend core/pr_review.py, issue_start.py). */
export function titlePrefix(kind: "prs" | "issues", repo: string): string {
  return (kind === "prs" ? "pr-" : "issue-") + repoSlugName(repo) + "-";
}

/** A row's title without its device namespace ("mini::pr-app-7" → "pr-app-7"):
 * the pipeline on another of your devices starts sessions under their own
 * names there. */
function bareTitle(title: string): string {
  const cut = title.indexOf("::");
  return cut < 0 ? title : title.slice(cut + 2);
}

/** A ticket source's display name: "<Provider> · <label>", or the provider
 * alone when the label is empty or already names it. */
export function ticketSourceLabel(source: Record<string, unknown>, providers?: SourceBlockOpts["providers"], panelLabel = ""): string {
  const provider = String(source.provider || "");
  const prov = providers?.find((p) => p.id === provider)?.label || panelLabel || provider || "Tickets";
  const detail = String(source.label || "").trim();
  if (!detail) return prov;
  const named = [prov, provider].some((n) => !!n && detail.toLowerCase().startsWith(n.toLowerCase()) && !/[a-z0-9]/i.test(detail.charAt(n.length)));
  return named ? detail : prov + " · " + detail;
}

function flywayOf(kind: WaitingKind, own: string, plans: SourceBlockOpts["plans"], depth = "", flock: FlockPrRoute | null = null): SourceBlock["flyway"] {
  // The server's chain: its own saved stop while it exists, else the built-in
  // cut at its depth — the same reading Intake's card shows. A PR repo with
  // neither follows the flock's PR route (backend core/pr_route.py).
  const reach = sourceReach(kind, { plan: own, depth }, plans, flock);
  if (reach.own) return { id: own, name: reach.words, own: true, words: reach.words };
  const id = builtinPlanFor(kind, plans);
  if (reach.choice === "flock") return { id: reach.planId || id, name: reach.words, own: false, words: reach.words };
  return { id, name: plans?.find((p) => p.id === id)?.name || "built-in route", own: false, words: reach.words };
}

const sameRepo = (a: string, b: string) => a.toLowerCase() === b.toLowerCase();

/** The lane's blocks, in pipeline order: ticket sources (as settings lists
 * them), then PR repos, then issue repos. */
export function sourceBlocks(
  settings: Record<string, unknown> | null | undefined,
  waiting: readonly LaneWaiting[],
  rows: readonly Instance[],
  opts: SourceBlockOpts = {}
): SourceBlock[] {
  const s = (settings || {}) as LaneSettings;
  const gh = s.github || {};
  const status = opts.status || null;
  const plans = opts.plans || null;
  const flock = flockPrRoute(s.flights?.pr_review_plan ?? "", plans);
  const labels = opts.panels?.tickets?.source_labels || {};
  const blocks: SourceBlock[] = [];

  const make = (kind: WaitingKind, id: string, label: string, every: number, own: string, stray = false, depth = ""): SourceBlock => {
    const auto = waitingAuto(kind, status);
    return {
      key: kind + ":" + id,
      kind,
      id,
      label,
      name: kind === "tickets" ? label : id,
      target: kind + ":" + id,
      auto,
      every,
      autoText: auto ? "auto-start on · " + everyWords(every) : "manual",
      flyway: flywayOf(kind, own, plans, depth, kind === "prs" ? flock : null),
      waiting: [],
      started: [],
      ...(stray ? { stray: true } : {}),
    };
  };

  for (const src of s.ticketing?.sources || []) {
    const id = String(src.id || "");
    if (!id) continue;
    blocks.push(
      make("tickets", id, ticketSourceLabel(src, opts.providers, labels[id]), positive(src.poll_interval_seconds) || LANE_POLL_DEFAULT.tickets, String(src.plan || ""), false, String(src.depth || ""))
    );
  }
  const repoBlocks = (kind: "prs" | "issues", repos: unknown, every: number, per: Record<string, { plan?: string; depth?: string }> | undefined) => {
    for (const r of Array.isArray(repos) ? repos : []) {
      const repo = String(r || "").trim();
      if (!repo || blocks.some((b) => b.kind === kind && sameRepo(b.id, repo))) continue;
      blocks.push(make(kind, repo, KIND_WORD[kind] + " · " + repo, every, per?.[repo]?.plan || "", false, per?.[repo]?.depth || ""));
    }
  };
  repoBlocks("prs", gh.repos, positive(gh.poll_interval_seconds) || LANE_POLL_DEFAULT.prs, gh.repo_settings);
  repoBlocks("issues", gh.issue_repos, positive(gh.issue_poll_interval_seconds) || LANE_POLL_DEFAULT.issues, gh.issue_repo_settings);

  // Waiting items, each into its source's block (a stray block when settings
  // doesn't name the source).
  for (const w of waiting) {
    const t = w.item.target;
    const id = t.kind === "tickets" ? t.source : t.repo;
    let block = blocks.find((b) => b.kind === t.kind && (t.kind === "tickets" ? b.id === id : sameRepo(b.id, id)));
    if (!block) {
      const kind = t.kind as WaitingKind;
      const every = kind === "tickets" ? LANE_POLL_DEFAULT.tickets : positive(kind === "prs" ? gh.poll_interval_seconds : gh.issue_poll_interval_seconds) || LANE_POLL_DEFAULT[kind];
      block = make(kind, id, kind === "tickets" ? w.item.group : KIND_WORD[kind] + " · " + id, every, "", true);
      blocks.push(block);
    }
    block.waiting.push({ item: w.item, auto: w.auto });
  }

  // Started sessions: a live row whose title the panel names for one of the
  // block's items, else (an item the panel no longer lists) the title shape
  // the server gives that kind — `pr-<repo name>-<n>`, `issue-<repo name>-<n>`
  // — or, for a ticket, a branch cut from `<source id>-…`.
  const live = rows.filter((r) => !r.hidden_by && !r.locked);
  const byTitle = new Map<string, Instance>();
  for (const r of live) {
    byTitle.set(r.title, r);
    if (!byTitle.has(bareTitle(r.title))) byTitle.set(bareTitle(r.title), r);
  }
  const claimed = new Set<string>();
  const claim = (block: SourceBlock, row: Instance, reference: string) => {
    if (claimed.has(row.title)) return;
    claimed.add(row.title);
    block.started.push({ title: row.title, reference });
  };
  const blockFor = (kind: WaitingKind, id: string) => blocks.find((b) => b.kind === kind && (kind === "tickets" ? b.id === id : sameRepo(b.id, id)));

  for (const t of opts.panels?.tickets?.tickets || []) {
    const row = t.session ? byTitle.get(t.session) : undefined;
    const block = row && blockFor("tickets", t.source || "");
    if (row && block) claim(block, row, t.slug || t.id || "");
  }
  for (const kind of ["prs", "issues"] as const) {
    const list = kind === "prs" ? opts.panels?.prs?.prs : opts.panels?.issues?.issues;
    for (const p of list || []) {
      const row = p.session ? byTitle.get(p.session) : undefined;
      const block = row && blockFor(kind, p.repo || "");
      if (row && block) claim(block, row, (p.repo || "") + "#" + (p.number ?? "?"));
    }
  }
  for (const row of live) {
    if (claimed.has(row.title)) continue;
    const title = bareTitle(row.title);
    for (const b of blocks) {
      if (b.kind === "tickets") {
        if (row.branch && new RegExp("(^|/)" + escapeRe(b.id) + "-", "i").test(row.branch) && looksLikeTicket(title)) {
          claim(b, row, "");
          break;
        }
        continue;
      }
      const m = new RegExp("^" + escapeRe(titlePrefix(b.kind, b.id)) + "(\\d+)$").exec(title);
      if (m) {
        claim(b, row, b.id + "#" + m[1]);
        break;
      }
    }
  }
  return blocks;
}

function escapeRe(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** A ticket session's title is its slug ("sc-12", "PROJ-7"): a row the user
 * named themselves on a similar branch is not one the source started. */
function looksLikeTicket(title: string): boolean {
  return /^[A-Za-z][A-Za-z0-9]*-\d+$/.test(title);
}

/** The stops chip's words — the same words Intake's "Goes as far as" shows
 * for the source: its own saved stop by name ("Payments stops"), else the
 * rung its depth goes to ("Open PR", "Stop after the agent"). Never the
 * built-in plan's name, which only repeated the kind badge beside it. */
export function stopsWords(flyway: SourceBlock["flyway"]): string {
  return flyway.own ? flyway.name : flyway.words || SOURCE_OFF_LABEL;
}

/** The stops chip's tooltip: what pressing it does, what the source flies
 * now, and — while its editor chain is on the board — that it goes there. */
export function stopsTip(block: Pick<SourceBlock, "name" | "flyway">, editing = false): string {
  const words = stopsWords(block.flyway);
  const now = block.flyway.own ? "its own: " + block.flyway.name : words === SOURCE_OFF_LABEL ? "stops after the agent" : "goes as far as " + words;
  return editing ? "Editing on the Sky — go to the editor for " + block.name + " (" + now + ")" : "Edit the default route for " + block.name + " (" + now + ")";
}

/** A title split after each "/" so a repo path wraps at its owner, not mid
 * word ("quickpay/" + "quickpay"); the view puts a break chance after each
 * part but the last. */
export function nameParts(name: string): string[] {
  return name.split(/(?<=\/)/);
}

/** What the collapsed rail shows per block: its waiting count. */
export function laneCount(block: SourceBlock): number {
  return block.waiting.length;
}

/** The chips a block lists and how many more there are. */
export function shownWaiting(block: SourceBlock, max = LANE_WAITING_SHOWN): { shown: LaneWaiting[]; more: number } {
  return { shown: block.waiting.slice(0, max), more: Math.max(0, block.waiting.length - max) };
}
