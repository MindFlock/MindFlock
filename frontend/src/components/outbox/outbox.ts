/** What waits on you, as data — pure, so the bell's arithmetic is unit-tested
 * in node. (The folder keeps its old name: `GET /api/outbox` is still the
 * route these rows come from.)
 *
 * One `GET /api/outbox?group=all` feeds the bell's waiting rows
 * (`needsAttention` merges them into the bell's one list) and a finished
 * group's summary, which its rail header's ⋯ menu copies (`summaryFor`).
 *
 * Rows are de-duplicated on `key` — the server's `(repo, branch)` — because the
 * owner runs two windows on one branch ("foo" + "foo-copy"): one branch's PR is
 * one row, never two. */

import type { OutboxResponse, OutboxSummary, OutboxWaiting } from "../../api/types";
import { releaseChoices } from "../../lib/splitRun";
import type { AttentionItem } from "../sidebar/ordering";

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

/** A finished group's summary (the Markdown its header's "Copy summary"
 * copies), or null when the server has none for it. */
export function summaryFor(data: OutboxResponse | null | undefined, runId: string): OutboxSummary | null {
  if (!runId) return null;
  const s = (data?.summaries || []).find((x) => x.run === runId);
  return s && String(s.text_md || "").trim() ? s : null;
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
      ? "stays local — its fast-track ends at commit"
      : "its fast-track ends at " + (STEP_WORD[s] || s);
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
  if (w.kind === "stray") return { text: w.reason || "changes no piece owns in the lead's folder", cls: "warn" };
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

/** A group-level row (its title is the group's lead): the plan to approve,
 * the one PR to open, the check that failed on the merged branch. */
export const LEAD_KINDS: ReadonlySet<string> = new Set(["plan", "release", "check_failed", "conflict", "stray"]);

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
      for (const c of releaseChoices(w.preview?.lane, w.preview?.local_origin))
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

/** One row of the bell's "Needs attention" list: a session's attention item
 * (an answer, a broken session, failing checks, ready for a PR) or a
 * waiting item from `GET /api/outbox` (an approval, an escalation, a group's plan or budget). */
export interface NeedsRow {
  /** The session's title, or for a group-level item with none the server's
   * own waiting key ("run::<id>", "run::<id>::<task>"; "run:<id>" if it sent
   * none). Address a group's rows by `waiting.run.id`, never by this key. */
  key: string;
  title: string;
  /** 0 answer · 1 approve / escalation · 2 broken · 3 checks failing · 4 ready. */
  rank: number;
  attn?: AttentionItem;
  waiting?: OutboxWaiting;
}

/** The bell's ONE list of what waits on you: the sidebar's attention items
 * merged with the server's waiting rows, one row per session. A session whose
 * agent is asking keeps its answer row (the server's "prompt" item is that same
 * question, so it is dropped); otherwise an approval or escalation outranks the
 * session's other attention reasons. Ordered answer → approvals/escalations →
 * broken → checks failing → ready, stable within a rank. */
export function needsAttention(
  attn: readonly AttentionItem[],
  waiting: readonly OutboxWaiting[] | undefined
): NeedsRow[] {
  const rows = new Map<string, NeedsRow>();
  for (const a of attn) {
    if (!rows.has(a.title)) rows.set(a.title, { key: a.title, title: a.title, rank: a.p === 0 ? 0 : a.p + 1, attn: a });
  }
  for (const w of dedupe(waiting).filter((x) => x.kind !== "prompt")) {
    // Only a row about a SESSION folds into that session's row. A group-level
    // item with no session (a budget pause, a ticket line that never resolved,
    // a lead-less ask) keeps the server's own key: one run can hold several,
    // and each carries its own Retry / Skip / Raise budget.
    if (w.title) {
      const had = rows.get(w.title);
      if (had && (had.rank === 0 || had.waiting)) continue;
      rows.set(w.title, { key: w.title, title: w.title, rank: 1, waiting: w });
      continue;
    }
    const key = w.key || "run:" + (w.run?.id || "") + (w.run?.task ? ":" + w.run.task : "");
    if (rows.has(key)) continue;
    rows.set(key, { key, title: "", rank: 1, waiting: w });
  }
  return [...rows.values()].sort((a, b) => a.rank - b.rank);
}
