/** The Outbox, as data — pure, so the tab arithmetic is unit-tested in node.
 *
 * One `GET /api/outbox?group=all` feeds everything: the top-bar badge, the tab
 * strip and every section. The tabs filter that one response client-side, so a
 * tab's badge is the length of exactly the list the tab shows (the Intake rule:
 * badges count what the tab SHOWS) and can never disagree with it.
 *
 * Rows are de-duplicated on `key` — the server's `(repo, branch)` — because the
 * owner runs two windows on one branch ("foo" + "foo-copy"): one branch's PR is
 * one row, never two. */

import type {
  Instance,
  OutboxQueued,
  OutboxResponse,
  OutboxShipped,
  OutboxShipping,
  OutboxSummary,
  OutboxWaiting,
} from "../../api/types";
import { releaseChoices } from "../../lib/splitRun";

export type OutboxTabKey = "all" | "own" | string;

export interface OutboxView {
  waiting: OutboxWaiting[];
  shipping: OutboxShipping[];
  shipped: OutboxShipped[];
  queued: OutboxQueued[];
  summaries: OutboxSummary[];
}

export interface OutboxTab {
  key: OutboxTabKey;
  label: string;
  count: number;
}

type Keyed = { key?: string; title?: string };

/** First of each `(repo, branch)` key (title when the server sent no key). */
export function dedupe<T extends Keyed>(list: readonly T[] | undefined): T[] {
  const seen = new Set<string>();
  const out: T[] = [];
  for (const it of list || []) {
    const k = it.key || it.title || "";
    if (k && seen.has(k)) continue;
    if (k) seen.add(k);
    out.push(it);
  }
  return out;
}

/** The group a row belongs to: its own `run`, else its session's (a shipping or
 * shipped row may carry only a title). "" = on its own. */
export function runOf(
  item: { run?: { id?: string } | null; title?: string },
  rowOf: (title: string) => Instance | undefined
): string {
  if (item.run?.id) return item.run.id;
  if (item.title) return rowOf(item.title)?.run?.id || "";
  return "";
}

/** Every section, de-duplicated, narrowed to one tab. */
export function viewFor(
  data: OutboxResponse | null | undefined,
  tab: OutboxTabKey,
  rowOf: (title: string) => Instance | undefined
): OutboxView {
  const g = data?.groups;
  const keep = (it: { run?: { id?: string } | null; title?: string }) => {
    if (tab === "all") return true;
    const r = runOf(it, rowOf);
    return tab === "own" ? !r : r === tab;
  };
  return {
    waiting: dedupe(g?.waiting).filter(keep),
    shipping: dedupe(g?.shipping).filter(keep),
    shipped: dedupe(g?.shipped).filter(keep),
    queued: (g?.queued || []).filter((q) => tab === "all" || (tab !== "own" && q.run?.id === tab)),
    summaries: (data?.summaries || []).filter((s) => tab === "all" || s.run === tab),
  };
}

/** How many rows a tab shows (summaries are cards, not rows). */
export function viewCount(v: OutboxView): number {
  return v.waiting.length + v.shipping.length + v.shipped.length + v.queued.length;
}

/** The top-bar badge: what's waiting on YOU, across every group. 0 hides it. */
export function waitingCount(data: OutboxResponse | null | undefined): number {
  if (!data) return 0;
  if (data.groups?.waiting) return dedupe(data.groups.waiting).length;
  return Number(data.counts?.waiting) || 0;
}

/** The tab strip: All · one tab per group with something in it · On their own.
 * A tab with nothing to show is left out (except All), and a group's name comes
 * from the rows themselves, falling back to `names` (the runs list). */
export function outboxTabs(
  data: OutboxResponse | null | undefined,
  rowOf: (title: string) => Instance | undefined,
  names: (id: string) => string = () => ""
): OutboxTab[] {
  const tabs: OutboxTab[] = [{ key: "all", label: "All", count: viewCount(viewFor(data, "all", rowOf)) }];
  const g = data?.groups;
  const order: string[] = [];
  const label = new Map<string, string>();
  const note = (id: string, name?: string) => {
    if (!id) return;
    if (!label.has(id)) order.push(id);
    if (name || !label.get(id)) label.set(id, name || label.get(id) || "");
  };
  const all = [...(g?.waiting || []), ...(g?.shipping || []), ...(g?.shipped || [])];
  for (const it of all) {
    const id = runOf(it, rowOf);
    note(id, it.run?.name || (it.title ? rowOf(it.title)?.run?.name : "") || "");
  }
  for (const q of g?.queued || []) note(q.run?.id || "", q.run?.name);
  for (const id of order) {
    const count = viewCount(viewFor(data, id, rowOf));
    if (count) tabs.push({ key: id, label: label.get(id) || names(id) || "Group", count });
  }
  const own = viewCount(viewFor(data, "own", rowOf));
  if (own && order.length) tabs.push({ key: "own", label: "On their own", count: own });
  return tabs;
}

/** The button that carries an approved ship one step, named for that step. */
export function shipVerb(step: string | undefined): string {
  switch (String(step || "")) {
    case "push":
      return "Push";
    case "pr":
    case "make_pr":
      return "Open the PR";
    case "merge":
      return "Merge";
    default:
      return "Commit";
  }
}

const LADDER = ["commit", "push", "pr", "merge"];
const STEP_WORD: Record<string, string> = { commit: "commit", push: "push", pr: "PR", merge: "merge" };

/** What happens after the approved step, given the session's lane:
 * "stays local — this session's lane ends at commit", or "then push, PR". */
export function thenText(step: string | undefined, laneTarget: string | undefined): string {
  const s = String(step || "commit") === "make_pr" ? "pr" : String(step || "commit");
  const lane = String(laneTarget || s);
  const from = LADDER.indexOf(s);
  const to = LADDER.indexOf(lane);
  if (from < 0 || to <= from) {
    return s === "commit"
      ? "stays local — this session's lane ends at commit"
      : "this session's lane ends at " + (STEP_WORD[s] || s);
  }
  return "then " + LADDER.slice(from + 1, to + 1).map((x) => STEP_WORD[x]).join(", ");
}

/** "its agent is asking" / "ready to commit — you asked to see it first" /
 * an escalation's reason — the chip on a Waiting row. */
export function waitingChip(w: OutboxWaiting): { text: string; cls: string } {
  if (w.kind === "prompt") return { text: w.reason || "its agent is asking", cls: "warn" };
  if (w.kind === "plan") return { text: w.reason || "the lead proposed the pieces — approve them", cls: "warn" };
  if (w.kind === "release") return { text: w.reason || "one PR is ready to open", cls: "" };
  if (w.kind === "check_failed") return { text: w.reason || "the check failed on the merged branch", cls: "bad" };
  if (w.kind === "approve")
    return {
      text: "ready to " + shipVerb(w.step).toLowerCase().replace("open the pr", "open the PR") + " — you asked to see it first",
      cls: "",
    };
  return { text: w.reason || "needs you", cls: "bad" };
}

/** The identity of ONE approval card (the session + when its lane was armed):
 * a message typed over the preview belongs to that card only — a later
 * approval of the same session starts from the server's preview again. */
export function approvalKey(w: Pick<OutboxWaiting, "title" | "armed_at" | "since">): string {
  return w.title + "@" + String(w.armed_at || w.since || "");
}

/** The first line of a (possibly multi-line) commit message, with "…" when
 * there is a body — what the card shows; editing keeps the whole message. */
export function messageHead(msg: string | null | undefined): string {
  const s = String(msg || "");
  const i = s.indexOf("\n");
  return i < 0 ? s : s.slice(0, i).trimEnd() + " …";
}

/** "5 files +210 −32" from an approval preview. */
export function statText(p: { files?: number; add?: number; del?: number } | null | undefined): string {
  if (!p) return "";
  const bits: string[] = [];
  if (p.files) bits.push(p.files + (p.files === 1 ? " file" : " files"));
  if (p.add || p.del) bits.push("+" + (p.add || 0) + " −" + (p.del || 0));
  return bits.join(" ");
}

/** "PR #318 · checks ✓" for a shipped row. */
export function shippedChip(s: OutboxShipped): { text: string; cls: string } {
  const m = String(s.pr_url || "").match(/\/pull\/(\d+)/);
  const pr = m ? "PR #" + m[1] : s.pr_url ? "PR" : "";
  const state = String(s.pr_state || "").toLowerCase();
  const checks = s.checks === "pass" || s.checks === "ok" ? "checks ✓" : s.checks === "fail" || s.checks === "failed" ? "checks ✗" : s.checks === "pending" ? "checks…" : "";
  const parts = [pr + (state === "merged" ? " merged" : state === "closed" ? " closed" : ""), checks].filter(Boolean);
  const bad = checks === "checks ✗" || state === "closed";
  // No PR: say how far it went — a commit or push lane's row is not a PR.
  const noPr = s.lane === "commit" ? "committed" : s.lane === "push" ? "pushed" : "shipped";
  return { text: parts.join(" · ") || noPr, cls: bad ? "bad" : "ok" };
}

/** A group-level row (its title is the group's lead): the plan to approve,
 * the one PR to open, the check that failed on the merged branch. */
export const LEAD_KINDS: ReadonlySet<string> = new Set(["plan", "release", "check_failed", "conflict"]);

export interface WaitAction {
  key:
    | "approve"
    | "release"
    | "release_merge"
    | "retry_check"
    | "retry"
    | "retry_fresh"
    | "open"
    | "skip"
    | "cancel_group";
  label: string;
  primary: boolean;
  title?: string;
}

/** The buttons of a Waiting row that is not a prompt or an approval, in the
 * order they sit — ONE horizontal row, the primary first (the mockup's
 * Retry · Open ↗ · Skip). Only what the server offered (`actions`) and what
 * this row can address (a run, a task, a live session) is listed. */
export function waitingActions(
  w: Pick<OutboxWaiting, "kind" | "actions" | "preview">,
  can: { row: boolean; run: boolean; task: boolean }
): WaitAction[] {
  const a = new Set(w.actions || []);
  const out: WaitAction[] = [];
  const open = (title?: string) => {
    if (can.row && (a.has("open") || LEAD_KINDS.has(w.kind)))
      out.push({ key: "open", label: LEAD_KINDS.has(w.kind) ? "Open the Thread ↗" : "Open ↗", primary: false, title });
  };
  if (w.kind === "plan") {
    const n = w.preview?.pieces?.length || 0;
    if (a.has("approve") && can.run)
      out.push({
        key: "approve",
        label: n ? "Start " + n + (n === 1 ? " worker" : " workers") : "Approve the plan",
        primary: true,
        title: "MindFlock starts one worker per piece, each fenced to its paths",
      });
    open("Read the plan in the lead's Thread tab — edit a piece there");
    return out;
  }
  if (w.kind === "release") {
    // Labelled from the group's own lane: "Open the PR" never merges.
    if (a.has("release") && can.run)
      for (const c of releaseChoices(w.preview?.lane))
        out.push({ key: c.merge ? "release_merge" : "release", label: c.label, primary: c.primary, title: c.title });
    open("The lead's Thread has the PR's title, body and the merged diff");
    return out;
  }
  if (w.kind === "lead_gone") {
    // Nothing can start, merge or ship without the lead: the way out is
    // cancelling the group (its sessions and branches are kept).
    if (a.has("cancel_group") && can.run)
      out.push({
        key: "cancel_group",
        label: "Cancel the group",
        primary: true,
        title: "Stop the group — its sessions and branches are kept",
      });
    return out;
  }
  if (w.kind === "check_failed") {
    if (a.has("retry_check") && can.run)
      out.push({ key: "retry_check", label: "Run the check again", primary: true });
    open();
    return out;
  }
  if (a.has("retry") && can.run && can.task) out.push({ key: "retry", label: "Retry", primary: true });
  if (a.has("retry_fresh") && can.run && can.task)
    out.push({ key: "retry_fresh", label: "Retry fresh", primary: false, title: "Start it again on a new branch; the old one is kept" });
  if (can.row) out.push({ key: "open", label: w.kind === "conflict" ? "Open the Thread ↗" : "Open ↗", primary: false });
  if (a.has("skip") && can.run && can.task)
    out.push({ key: "skip", label: "Skip", primary: false, title: "Take it out of the group — its session and branch stay" });
  return out;
}
